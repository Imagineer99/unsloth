#!/usr/bin/env python3
"""Deterministic live-Studio UI A/B for upstream PR #9640.

The fake vLLM endpoint emits one malformed Python tool call and then a normal
recovery turn.  Both Studio installations use the real backend and frontend;
only the remote model transport is deterministic and local.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from studio_test_kit.auth import login, seed_init_script
from studio_test_kit.compose import hstack_images
from studio_test_kit.ui import open_chat, send_prompt, wait_for_text


BASE_SHA = "b9792a5b14bd4bbf292d1a3b5404fb7654b50615"
HEAD_SHA = "d453a1986e85c18de64ce9b8b2f0ffd46a53fd54"
MODEL = "pr9640-tool-model"
RECOVERY = "I will fix the argument."
REAL_ERROR = "'int' object has no attribute 'strip'"
ARTIFACTS = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
RUNNER_TEMP = Path(os.environ["RUNNER_TEMP"]).resolve()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, log: Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    if log is None:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as handle:
        subprocess.run(cmd, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True)


def find_unsloth_bin(home: Path) -> Path:
    candidates = [
        home / "bin" / "unsloth",
        home / "unsloth_studio" / "bin" / "unsloth",
        home / ".venv_t5_550" / "bin" / "unsloth",
        home / ".venv_t5_530" / "bin" / "unsloth",
    ]
    candidates.extend(home.glob(".venv*/*/unsloth"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(f"unsloth CLI not found under {home}")


def read_bootstrap_password(home: Path, log_path: Path) -> str | None:
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        try:
            value = (home / rel).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password(?:\s+is)?\s*[:=]?\s+(\S+)",
        log_text,
    )
    return match.group(1).strip().strip(".,") if match else None


def wait_for_health(base_url: str, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(1)
    raise AssertionError(f"Studio did not become healthy: {base_url}")


def start_studio(home: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(find_unsloth_bin(home)), "studio", "-H", "127.0.0.1", "-p", str(port)],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    wait_for_health(f"http://127.0.0.1:{port}")
    return proc


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()


class FakeProvider:
    def __init__(self):
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _json(self, body: object, status: int = 200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
                else:
                    self._json({"ok": True})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(body)
                turn = len(owner.requests)
                if turn == 1:
                    names = {
                        item.get("function", {}).get("name")
                        for item in body.get("tools", [])
                        if isinstance(item, dict)
                    }
                    if "python" not in names:
                        self._json({"error": {"message": f"python tool was not advertised: {sorted(names)}"}}, 400)
                        return
                    chunks = [
                        {
                            "id": "chatcmpl-pr9640-1",
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_bad_arg",
                                                "type": "function",
                                                "function": {"name": "python", "arguments": '{"code":42}'},
                                            }
                                        ],
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": "chatcmpl-pr9640-1",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        },
                    ]
                else:
                    chunks = [
                        {
                            "id": "chatcmpl-pr9640-2",
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": RECOVERY},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": "chatcmpl-pr9640-2",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        },
                    ]
                payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                data = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


async def auth_seed(base_url: str, password: str, provider_base_url: str) -> tuple[str, dict]:
    auth = await login(base_url, "unsloth", password)
    if auth.must_change_password:
        new_password = "UnslothStudioCI2026!"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{base_url}/api/auth/change-password",
                headers={"Authorization": f"Bearer {auth.access_token}"},
                json={"current_password": password, "new_password": new_password},
            )
            response.raise_for_status()
            body = response.json()
        auth.access_token = body["access_token"]
        auth.refresh_token = body.get("refresh_token", "")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{base_url}/api/providers/",
            headers={"Authorization": f"Bearer {auth.access_token}"},
            json={
                "provider_type": "vllm",
                "display_name": "PR 9640 deterministic vLLM",
                "base_url": provider_base_url,
                "models": [MODEL],
                "available_models": [MODEL],
            },
        )
        response.raise_for_status()
        saved_provider = response.json()
    init = seed_init_script(
        auth,
        [],
        extra_local_storage={
            "unsloth_chat_tools_enabled": "true",
            "unsloth_chat_code_tools_enabled": "true",
            "unsloth_chat_confirm_tool_calls": "false",
            "unsloth_chat_permission_mode": "off",
        },
    )
    return init, saved_provider


def tool_result_from_second_request(requests: list[dict]) -> str:
    assert len(requests) >= 2, f"expected recovery request, got {len(requests)}"
    messages = requests[1].get("messages", [])
    tool_messages = [item for item in messages if item.get("role") == "tool"]
    assert tool_messages, messages
    return "\n".join(str(item.get("content", "")) for item in tool_messages)


async def pick_connected_model(sp, model_id: str, side: Path) -> dict:
    """Drive the model picker used by both pinned refs.

    The reusable kit predates the current header-level selector and still
    scopes the trigger under ``form:has(textarea)``.  Assert the actual picker
    and connected tab so a selector miss cannot masquerade as product output.
    """
    page = sp.page
    trigger = page.locator(".unsloth-model-selector-trigger").first
    await trigger.wait_for(state="visible", timeout=30_000)
    trigger_text = (await trigger.inner_text()).strip()
    await trigger.click()
    menu = page.locator(".unsloth-model-selector-menu").last
    await menu.wait_for(state="visible", timeout=15_000)
    await sp.screenshot(side / "picker-open.png", full_page=False)

    connected = menu.get_by_role("tab", name="Connected", exact=True)
    await connected.wait_for(state="visible", timeout=15_000)
    await connected.click()
    await connected.wait_for(state="visible", timeout=15_000)
    assert await connected.get_attribute("aria-selected") == "true"
    await sp.screenshot(side / "picker-connected.png", full_page=False)

    # Connected provider rows intentionally do not carry the
    # data-model-picker-option attribute used by Hub/On Device rows.
    option = menu.get_by_role("button", name=model_id, exact=True)
    await option.wait_for(state="visible", timeout=15_000)
    option_text = (await option.inner_text()).strip()
    assert model_id in option_text, option_text
    await option.click()
    await page.get_by_text(model_id, exact=True).first.wait_for(state="visible", timeout=15_000)
    return {"trigger_before": trigger_text, "selected_option": option_text}


async def capture_side(label: str, sha: str, home: Path, browser: str) -> tuple[Path, dict]:
    side = ARTIFACTS / label
    side.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = side / "studio.log"
    proc = None
    fake = FakeProvider()
    try:
        proc = start_studio(home, port, log_path)
        password = None
        deadline = time.time() + 60
        while password is None and time.time() < deadline:
            password = read_bootstrap_password(home, log_path)
            if password is None:
                time.sleep(0.5)
        assert password, f"bootstrap password not found for {label}"
        init, saved_provider = await auth_seed(base_url, password, fake.base_url)
        async with open_chat(
            base_url,
            init_scripts=[init],
            browser_name=browser,
            viewport=(1440, 1000),
            headless=True,
        ) as sp:
            await sp.page.wait_for_function(
                """providerId => {
                    const providers = JSON.parse(
                        localStorage.getItem('unsloth_chat_external_providers') || '[]'
                    );
                    return providers.some(item => item.id === providerId);
                }""",
                arg=saved_provider["id"],
                timeout=30_000,
            )
            await sp.screenshot(side / "chat-open.png", full_page=False)
            provider_state = await sp.page.evaluate(
                """() => ({
                    providers: JSON.parse(localStorage.getItem('unsloth_chat_external_providers') || '[]'),
                    connectionsEnabled: localStorage.getItem('unsloth_chat_connections_enabled'),
                    toolsEnabled: localStorage.getItem('unsloth_chat_tools_enabled'),
                    codeEnabled: localStorage.getItem('unsloth_chat_code_tools_enabled'),
                    permissionMode: localStorage.getItem('unsloth_chat_permission_mode'),
                })"""
            )
            assert any(
                item.get("id") == saved_provider["id"]
                and item.get("baseUrl") == fake.base_url
                for item in provider_state.get("providers", [])
            ), provider_state
            picker_facts = await pick_connected_model(sp, MODEL, side)
            await send_prompt(sp, "Run Python now. Use the Python tool exactly once.")
            await wait_for_text(sp, RECOVERY, timeout_ms=90_000)
            trigger = sp.page.locator('[data-slot="tool-fallback-trigger"]').filter(has_text="Python").last
            await trigger.wait_for(state="visible", timeout=30_000)
            if await trigger.get_attribute("data-state") != "open":
                await trigger.click()

            expected = "Unknown tool: python" if label == "before" else REAL_ERROR
            await sp.page.get_by_text(expected, exact=False).last.wait_for(state="visible", timeout=30_000)
            card = trigger.locator("xpath=..").first
            await card.scroll_into_view_if_needed()
            card_text = await card.inner_text()
            assert expected in card_text, card_text
            shot = side / f"{label}-{browser}.png"
            await sp.screenshot(shot, full_page=False)

        provider_result = tool_result_from_second_request(fake.requests)
        if label == "before":
            assert provider_result == "Unknown tool: python", provider_result
        else:
            assert provider_result.startswith("Error: tool raised an exception:"), provider_result
            assert REAL_ERROR in provider_result
            assert "Unknown tool" not in provider_result
        facts = {
            "label": label,
            "sha": sha,
            "studio_port": port,
            "studio_home": str(home),
            "fake_provider_port": fake.server.server_port,
            "provider_requests": len(fake.requests),
            "tool_result_sent_to_model": provider_result,
            "recovery_visible": True,
            "browser": browser,
            "provider_state": provider_state,
            "saved_provider_id": saved_provider["id"],
            "picker": picker_facts,
        }
        (side / "facts.json").write_text(json.dumps(facts, indent=2, sort_keys=True), encoding="utf-8")
        return shot, facts
    finally:
        fake.close()
        stop_process(proc)


def install_before(repo: Path, home: Path) -> Path:
    worktree = RUNNER_TEMP / "pr9640-before-repo"
    if worktree.exists():
        raise AssertionError(f"unexpected existing path: {worktree}")
    run(["git", "worktree", "add", "--detach", str(worktree), BASE_SHA], cwd=repo)
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    run(
        ["bash", "./install.sh", "--local", "--no-torch"],
        cwd=worktree,
        env=env,
        log=ARTIFACTS / "before" / "install.log",
    )
    return worktree


async def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    repo = Path.cwd().resolve()
    browser = os.environ.get("STUDIO_BROWSER", "chromium")
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert actual_head == HEAD_SHA, (actual_head, HEAD_SHA)

    after_home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    before_home = RUNNER_TEMP / "pr9640-before-home"
    before_repo = install_before(repo, before_home)
    before_actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=before_repo, text=True).strip()
    assert before_actual == BASE_SHA

    before_shot, before_facts = await capture_side("before", BASE_SHA, before_home, browser)
    after_shot, after_facts = await capture_side("after", HEAD_SHA, after_home, browser)
    composite = hstack_images(
        before_shot,
        after_shot,
        ARTIFACTS / "pr9640-before-after.png",
        label_left=f"BEFORE {BASE_SHA[:10]}",
        label_right=f"AFTER {HEAD_SHA[:10]}",
    )
    meta = {
        "expect": "Malformed Python arguments display Unknown tool before and the real AttributeError after.",
        "before": before_facts,
        "after": after_facts,
        "composite": str(composite),
    }
    (ARTIFACTS / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(meta, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
