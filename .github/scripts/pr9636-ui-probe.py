#!/usr/bin/env python3
"""Deterministic Studio UI scene for PR 9636.

The checked-out backend creates the real flattened MCP result. Playwright then
feeds that result through Studio's real chat SSE adapter and tool fallback UI.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import logging
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import httpx
from PIL import Image
from studio_test_kit.auth import login, seed_init_script
from studio_test_kit.lifecycle import StudioInstall, launch_studio, stop_studio
from studio_test_kit.ui import open_chat, pick_model, send_prompt, wait_for_stream, wait_for_text


MODEL = "pr9636-model"


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def png_payload() -> str:
    image = Image.new("RGB", (192, 192), (36, 123, 230))
    for x in range(32, 160):
        for y in range(32, 160):
            if (x // 16 + y // 16) % 2:
                image.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def load_backend(repo: Path):
    logger_module = types.ModuleType("loggers")
    logger_module.get_logger = logging.getLogger
    sys.modules["loggers"] = logger_module
    path = repo / "studio" / "backend" / "core" / "inference" / "mcp_client.py"
    spec = importlib.util.spec_from_file_location("pr9636_ui_mcp_client", path)
    if spec is None or spec.loader is None:
        fail(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flatten(repo: Path) -> tuple[str, int]:
    backend = load_backend(repo)
    block = types.SimpleNamespace(
        type="resource",
        resource=types.SimpleNamespace(
            uri="file:///out/generated.png",
            mimeType="image/png",
            blob=png_payload(),
        ),
    )
    result = types.SimpleNamespace(content=[block], structured_content=None, is_error=False)
    flat = backend._flatten_result(result)
    marker = "\n" + backend.MCP_IMAGES_SENTINEL
    count = 0
    if marker in flat:
        _, raw = flat.rsplit(marker, 1)
        count = len(json.loads(raw))
    return flat, count


def read_password(home: Path, log_path: Path) -> str | None:
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        try:
            value = (home / rel).read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    return None


async def auth_script(base_url: str, password: str) -> str:
    auth = await login(base_url, "unsloth", password)
    if auth.must_change_password:
        replacement = "UnslothStudioCI2026!"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{base_url}/api/auth/change-password",
                headers={"Authorization": f"Bearer {auth.access_token}"},
                json={"current_password": password, "new_password": replacement},
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
                "provider_type": "openai",
                "display_name": "PR 9636 Local",
                "models": [MODEL],
                "available_models": [MODEL],
            },
        )
        response.raise_for_status()
    # Current Studio synchronizes connections from the backend after boot, so a
    # localStorage-only provider is deliberately pruned. Persisting the fixture
    # through the public provider API makes the repro follow the production path.
    return seed_init_script(auth, [])


def sse_body(flat_result: str) -> str:
    events = [
        {
            "type": "tool_start",
            "tool_name": "mcp__pr9636__render_image",
            "tool_call_id": "call_pr9636",
            "arguments": {},
            "provenance": {"mcp_server": "PR 9636 MCP"},
        },
        {
            "type": "tool_end",
            "tool_call_id": "call_pr9636",
            "result": flat_result,
        },
        {
            "id": "pr9636-final",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "MCP embedded-resource repro complete."},
                    "finish_reason": "stop",
                }
            ],
        },
    ]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


async def run_scene(
    base_url: str,
    password: str,
    browser_name: str,
    artifact_dir: Path,
    flat_result: str,
    label: str,
) -> tuple[dict, list[str]]:
    init = await auth_script(base_url, password)
    page_errors: list[str] = []
    console_errors: list[str] = []
    async with open_chat(
        base_url,
        init_scripts=[init],
        browser_name=browser_name,
        viewport=(1440, 1000),
    ) as studio_page:
        page = studio_page.page
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        async def intercept(route):
            await route.fulfill(
                status=200,
                headers={
                    "content-type": "text/event-stream; charset=utf-8",
                    "cache-control": "no-cache",
                },
                body=sse_body(flat_result),
            )

        await page.route("**/v1/chat/completions", intercept)
        try:
            await pick_model(studio_page, MODEL, timeout_ms=30_000)
            await send_prompt(studio_page, "Render the MCP embedded resource.")
            await wait_for_stream(studio_page, timeout_ms=60_000)
            await wait_for_text(
                studio_page,
                "MCP embedded-resource repro complete.",
                timeout_ms=30_000,
            )
            await page.wait_for_timeout(1500)
        except Exception:
            await studio_page.screenshot(artifact_dir / "diagnostic-error.png")
            raise

        images = page.locator('img[alt^="Tool result"]')
        image_count = await images.count()
        dimensions = []
        for index in range(image_count):
            dimensions.append(
                await images.nth(index).evaluate(
                    "img => [img.naturalWidth, img.naturalHeight, img.complete]"
                )
            )

        await page.evaluate(
            """label => {
              const badge = document.createElement('div');
              badge.id = 'pr9636-evidence-label';
              badge.textContent = label;
              Object.assign(badge.style, {
                position: 'fixed', top: '12px', right: '12px', zIndex: '2147483647',
                padding: '8px 12px', borderRadius: '8px', color: 'white',
                background: label.startsWith('BEFORE') ? '#991b1b' : '#166534',
                font: '700 14px system-ui', boxShadow: '0 2px 8px #0008'
              });
              document.body.appendChild(badge);
            }""",
            label,
        )
        await studio_page.screenshot(artifact_dir / "scene.png")

    facts = {
        "dom_image_count": image_count,
        "result_image_dims": dimensions,
        "page_errors": page_errors,
        "console_errors": console_errors,
    }
    return facts, console_errors


async def main() -> None:
    repo = Path.cwd().resolve()
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    branch = os.environ.get("GITHUB_REF_NAME", "")
    checkout_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    commit = os.environ.get("PR9636_PRODUCT_SHA", checkout_commit)
    browser_name = os.environ.get("STUDIO_BROWSER", "chromium")
    before = branch.endswith("-base")
    label = f"{'BEFORE' if before else 'AFTER'} · {commit[:10]} · {browser_name}"
    expected_images = 0 if before else 1

    flat_result, source_images = flatten(repo)
    install = StudioInstall(home=home, repo=repo, branch=branch)
    port = free_port()
    log_path = artifact_dir / "studio.log"
    try:
        launch_studio(
            install,
            port=port,
            log_path=log_path,
            healthz_timeout_s=240,
            password_timeout_s=5,
        )
        password = install.bootstrap_password or read_password(home, log_path)
        if not password:
            fail("could not read Studio bootstrap password")
        base_url = f"http://127.0.0.1:{port}"
        scene, _ = await run_scene(
            base_url,
            password,
            browser_name,
            artifact_dir,
            flat_result,
            label,
        )
    finally:
        stop_studio(install)

    facts = {
        "branch": branch,
        "commit": commit,
        "checkout_commit": checkout_commit,
        "label": label,
        "browser": browser_name,
        "studio_home": str(home),
        "port": port,
        "expected_image_count": expected_images,
        "flattened_result_text_len": len(flat_result),
        "flattened_result_image_count": source_images,
        **scene,
    }
    (artifact_dir / "facts.json").write_text(
        json.dumps(facts, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(facts, indent=2, sort_keys=True))

    if source_images != expected_images:
        fail(f"backend produced {source_images} images, expected {expected_images}")
    if facts["dom_image_count"] != expected_images:
        fail(f"UI rendered {facts['dom_image_count']} images, expected {expected_images}")
    if expected_images and facts["result_image_dims"] != [[192, 192, True]]:
        fail(f"rendered image dimensions were {facts['result_image_dims']!r}")
    if facts["page_errors"]:
        fail(f"page errors: {facts['page_errors']!r}")
    pass_log(f"Studio UI scene matched expected image count {expected_images}")


if __name__ == "__main__":
    asyncio.run(main())
