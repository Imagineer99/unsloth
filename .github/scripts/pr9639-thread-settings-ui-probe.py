# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Live A/B evidence for PR 9639's app-created chat behavior.

The probe deliberately records screenshots and facts before asserting the fixed
behavior.  That makes the merge-base run a useful negative proof rather than a
premature timeout with no reviewable UI evidence.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright


HOME = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
ART = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "studio-live-artifacts")).resolve()
BROWSER_NAME = os.environ.get("STUDIO_BROWSER", "chromium")
TIMEOUT_MS = int(os.environ.get("STUDIO_UI_TIMEOUT_MS", "30000"))
NEW_PASSWORD = "PR9639-repro-password-2026!"
GLOBAL_KEYS = (
    "unsloth_chat_tools_enabled",
    "unsloth_chat_code_tools_enabled",
    "unsloth_chat_permission_mode",
    "unsloth_chat_confirm_tool_calls",
)


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def fail_log(message: str) -> None:
    print(f"FAIL {message}", file=sys.stderr, flush=True)


def git_sha(ref: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", ref], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_unsloth_bin() -> Path:
    candidates = [
        HOME / "bin" / "unsloth",
        HOME / "bin" / "unsloth.exe",
        HOME / "unsloth_studio" / "bin" / "unsloth",
        HOME / "unsloth_studio" / "Scripts" / "unsloth.exe",
    ]
    candidates.extend(HOME.glob(".venv*/*/unsloth"))
    candidates.extend(HOME.glob(".venv*/Scripts/unsloth.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"could not find the installed unsloth CLI under {HOME}")


def start_studio(port: int, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(HOME)
    env.pop("STUDIO_HOME", None)
    kwargs: dict = {
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    command = [str(find_unsloth_bin()), "studio", "-H", "127.0.0.1", "-p", str(port)]
    print("Launching Studio from the checked-out branch", flush=True)
    process = subprocess.Popen(command, **kwargs)
    log_handle.close()
    return process


def wait_for_health(base_url: str, timeout_seconds: int = 240) -> str:
    deadline = time.monotonic() + timeout_seconds
    paths = ("/healthz", "/api/health", "/")
    while time.monotonic() < deadline:
        for path in paths:
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as response:
                    if response.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    raise RuntimeError(f"Studio did not become healthy within {timeout_seconds}s")


def stop_studio(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                process.kill()


def api(page, base_url: str, path: str, method: str = "GET", body=None, token=None):
    result = page.evaluate(
        """async ([url, method, body, token]) => {
            const headers = { "Content-Type": "application/json" };
            if (token) headers.Authorization = `Bearer ${token}`;
            const response = await fetch(url, {
                method,
                headers,
                body: body === null ? undefined : JSON.stringify(body),
            });
            const text = await response.text();
            let parsed = null;
            try { parsed = JSON.parse(text); } catch { parsed = text; }
            return { status: response.status, body: parsed };
        }""",
        [f"{base_url}{path}", method, body, token],
    )
    if result["status"] >= 400:
        raise RuntimeError(f"{method} {path} returned {result['status']}: {result['body']!r}")
    return result["body"]


def sign_in(page, base_url: str) -> str:
    page.goto(f"{base_url}/change-password", wait_until="domcontentloaded", timeout=60_000)
    try:
        page.locator("#new-password").wait_for(state="visible", timeout=20_000)
        page.fill("#new-password", NEW_PASSWORD)
        page.fill("#confirm-password", NEW_PASSWORD)
        endpoint = "/api/auth/change-password"
    except Exception:
        page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=60_000)
        page.locator("#password").wait_for(state="visible", timeout=60_000)
        page.fill("#password", NEW_PASSWORD)
        endpoint = "/api/auth/login"
    with page.expect_response(
        lambda response: endpoint in response.url and response.request.method == "POST",
        timeout=TIMEOUT_MS,
    ) as response_info:
        page.locator('button[type="submit"]').click()
    if response_info.value.status >= 400:
        raise RuntimeError(f"POST {endpoint} returned {response_info.value.status}")
    page.goto(f"{base_url}/chat", wait_until="domcontentloaded", timeout=60_000)
    pill(page, "Search").wait_for(state="visible", timeout=60_000)
    token = page.evaluate("() => localStorage.getItem('unsloth_auth_token')")
    if not token:
        raise RuntimeError("no auth token after sign-in")
    return token


def pill(page, label: str):
    return page.locator(f'button[data-pill-label="{label}"]:visible').first


def permission_pill(page):
    return page.locator('button[aria-label="Permission level for tool calls"]:visible').first


def settle(page) -> None:
    pill(page, "Search").wait_for(state="visible", timeout=TIMEOUT_MS)
    page.wait_for_timeout(1400)


def active(page, label: str) -> bool:
    return pill(page, label).get_attribute("data-active") == "true"


def permission(page) -> str | None:
    return permission_pill(page).get_attribute("data-pill-label")


def choose_permission(page, label: str) -> None:
    if permission(page) == label:
        return
    permission_pill(page).click()
    menu = page.get_by_role("menu").last
    menu.wait_for(state="visible", timeout=TIMEOUT_MS)
    menu.get_by_role("menuitem").filter(has_text=label).first.click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if permission(page) == label:
            return
        page.wait_for_timeout(100)
    raise RuntimeError(f"permission pill never became {label!r}")


def read_globals(page) -> dict[str, str | None]:
    return page.evaluate(
        "(keys) => Object.fromEntries(keys.map((key) => [key, localStorage.getItem(key)]))",
        list(GLOBAL_KEYS),
    )


def seed_thread(page, base_url: str, token: str, title: str, thread_id: str) -> str:
    message_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    api(
        page,
        base_url,
        "/api/chat/threads",
        method="POST",
        token=token,
        body={
            "id": thread_id,
            "title": title,
            "modelType": "base",
            "modelId": "",
            "archived": False,
            "createdAt": now,
            "updatedAt": now,
        },
    )
    api(
        page,
        base_url,
        f"/api/chat/threads/{thread_id}/messages",
        method="PUT",
        token=token,
        body={
            "messages": [
                {
                    "id": message_id,
                    "threadId": thread_id,
                    "parentId": None,
                    "role": "user",
                    "content": [{"type": "text", "text": f"seed for {title}"}],
                    "createdAt": now,
                }
            ]
        },
    )
    return message_id


def open_thread(page, base_url: str, thread_id: str) -> None:
    page.goto(f"{base_url}/chat?thread={thread_id}", wait_until="domcontentloaded", timeout=60_000)
    settle(page)


def wait_for_settings(page, base_url: str, token: str, thread_id: str, seconds: int = 8):
    deadline = time.monotonic() + seconds
    settings = None
    while time.monotonic() < deadline:
        settings = api(page, base_url, f"/api/chat/threads/{thread_id}", token=token).get(
            "settings"
        )
        if settings and settings.get("toolsEnabled") is True:
            return settings
        page.wait_for_timeout(250)
    return settings


def run_browser(base_url: str) -> tuple[dict, list[str]]:
    facts: dict = {
        "expect": (
            "an app-created saved chat persists its own settings, does not leak them to "
            "another chat, and shows its two-fork badge"
        ),
        "workflow_commit": os.environ.get("GITHUB_SHA") or git_sha("HEAD"),
        "implementation_parent": git_sha("HEAD^"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "browser": BROWSER_NAME,
        "studio_home": str(HOME),
        "base_url": base_url,
    }
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, BROWSER_NAME)
        launch_options = {}
        if BROWSER_NAME == "chromium":
            launch_options["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        browser = browser_type.launch(**launch_options)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        token = sign_in(page, base_url)
        settle(page)

        # Pin a deterministic installation default before opening either saved chat.
        if active(page, "Search"):
            pill(page, "Search").click()
            page.wait_for_timeout(650)
        choose_permission(page, "Approve for me")
        defaults = read_globals(page)

        thread_a = f"__LOCALID_{uuid.uuid4().hex}"
        thread_b = str(uuid.uuid4())
        message_a = seed_thread(page, base_url, token, "PR9639 Chat A", thread_a)
        seed_thread(page, base_url, token, "PR9639 Chat B", thread_b)
        for _ in range(2):
            api(
                page,
                base_url,
                f"/api/chat/threads/{thread_a}/fork",
                method="POST",
                token=token,
                body={
                    "messageId": message_a,
                    "newThreadId": str(uuid.uuid4()),
                    "createdAt": int(time.time() * 1000),
                },
            )

        open_thread(page, base_url, thread_a)
        backend_counts = api(page, base_url, f"/api/chat/threads/{thread_a}/forks", token=token)
        user_message = page.locator('.aui-user-message-root[data-role="user"]').first
        user_message.wait_for(state="visible", timeout=TIMEOUT_MS)
        user_message.hover()
        badge = page.locator('span[title="2 forks from this message"]')
        try:
            badge.wait_for(state="visible", timeout=6_000)
            badge_text = badge.inner_text()
        except Exception:
            badge_text = None
        page.screenshot(path=str(ART / "01-fork-count.png"), full_page=False)

        pill(page, "Search").click()
        choose_permission(page, "Ask for approval")
        page.wait_for_timeout(650)
        stored_a = wait_for_settings(page, base_url, token, thread_a)
        globals_after_a = read_globals(page)

        open_thread(page, base_url, thread_b)
        chat_b = {
            "search": active(page, "Search"),
            "code": active(page, "Code"),
            "permission": permission(page),
        }
        page.screenshot(path=str(ART / "02-chat-b-settings.png"), full_page=False)

        facts.update(
            {
                "thread_a": thread_a,
                "thread_b": thread_b,
                "message_a": message_a,
                "defaults": defaults,
                "backend_fork_counts": backend_counts,
                "fork_badge_text": badge_text,
                "stored_chat_a_settings": stored_a,
                "globals_after_chat_a_edit": globals_after_a,
                "chat_b_visible": chat_b,
                "page_errors": page_errors,
            }
        )

        if backend_counts.get("counts", {}).get(message_a) != 2:
            failures.append(f"backend fixture did not contain two forks: {backend_counts!r}")
        if badge_text != "2":
            failures.append(f"fork badge was {badge_text!r}, expected '2'")
        if not stored_a or stored_a.get("toolsEnabled") is not True:
            failures.append(f"Chat A did not persist toolsEnabled=true: {stored_a!r}")
        if not stored_a or stored_a.get("permissionMode") != "ask":
            failures.append(f"Chat A did not persist permissionMode='ask': {stored_a!r}")
        if globals_after_a != defaults:
            failures.append("Chat A's edit leaked into installation defaults")
        if chat_b != {"search": False, "code": False, "permission": "Approve for me"}:
            failures.append(f"Chat B inherited Chat A's settings: {chat_b!r}")
        if page_errors:
            failures.append(f"page errors occurred: {page_errors[:3]!r}")

        context.close()
        browser.close()
    return facts, failures


def main() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    process: subprocess.Popen | None = None
    facts: dict = {
        "workflow_commit": os.environ.get("GITHUB_SHA") or git_sha("HEAD"),
        "implementation_parent": git_sha("HEAD^"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "browser": BROWSER_NAME,
        "studio_home": str(HOME),
        "base_url": base_url,
    }
    failures: list[str] = []
    exit_code = 1
    try:
        process = start_studio(port, ART / "studio.log")
        health_path = wait_for_health(base_url)
        pass_log(f"Studio healthy at {health_path}")
        facts, failures = run_browser(base_url)
        exit_code = 0 if not failures else 1
    except Exception as error:
        failures.append(f"probe setup/runtime error: {type(error).__name__}: {error}")
    finally:
        facts["failures"] = failures
        facts["status"] = "PASS" if not failures and exit_code == 0 else "FAIL"
        (ART / "facts.json").write_text(json.dumps(facts, indent=2, sort_keys=True), encoding="utf-8")
        print("FACTS " + json.dumps(facts, sort_keys=True), flush=True)
        stop_studio(process)

    if failures:
        for failure in failures:
            fail_log(failure)
        raise SystemExit(1)
    pass_log("PR 9639 app-created settings isolation and fork badge")


if __name__ == "__main__":
    main()
