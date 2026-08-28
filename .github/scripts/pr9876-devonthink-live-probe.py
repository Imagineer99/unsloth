#!/usr/bin/env python3
"""Live Studio + Playwright proof for PR 9876.

The probe deliberately does not follow redirects. That matches the failing
DEVONthink model-discovery request and distinguishes the catch-all model lookup
from the direct collection route added by the fix.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from studio_test_kit.auth import login, seed_init_script


def log_result(kind: str, message: str) -> None:
    print(f"{kind} {message}", flush = True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_unsloth(home: Path) -> Path:
    candidates = [
        home / "bin" / "unsloth",
        home / "unsloth_studio" / "bin" / "unsloth",
        home / "unsloth_studio" / "Scripts" / "unsloth.exe",
    ]
    candidates.extend(home.glob(".venv*/*/unsloth"))
    candidates.extend(home.glob(".venv*/Scripts/unsloth.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Unsloth CLI was not installed under {home}")


def read_bootstrap_password(home: Path, log_path: Path) -> str | None:
    for relative in ("auth/.bootstrap_password", ".bootstrap_password"):
        try:
            value = (home / relative).read_text(encoding = "utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    try:
        text = log_path.read_text(encoding = "utf-8", errors = "ignore")
    except OSError:
        return None
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password"
        r"(?:\s+is)?\s*[:=]?\s+(\S+)",
        text,
    )
    return match.group(1).strip().strip(".,") if match else None


async def wait_for_health(base_url: str, timeout_s: int = 180) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout = 3) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}/healthz")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("Studio did not become healthy")


async def authenticated_session(base_url: str, password: str):
    auth = await login(base_url, "unsloth", password)
    if auth.must_change_password:
        new_password = "UnslothStudioCI2026!"
        async with httpx.AsyncClient(timeout = 20) as client:
            response = await client.post(
                f"{base_url}/api/auth/change-password",
                headers = {"Authorization": f"Bearer {auth.access_token}"},
                json = {
                    "current_password": password,
                    "new_password": new_password,
                },
            )
            response.raise_for_status()
            body = response.json()
        auth.access_token = body["access_token"]
        auth.refresh_token = body.get("refresh_token", "")
    return auth


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifact_dir.mkdir(parents = True, exist_ok = True)
    browser_name = os.environ.get("STUDIO_BROWSER", "chromium")
    expect_direct = os.environ.get("EXPECT_DIRECT_SLASH", "true").lower() == "true"
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text = True
    ).strip()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = artifact_dir / "studio.log"
    log_handle = log_path.open("w", encoding = "utf-8")
    process = subprocess.Popen(
        [str(find_unsloth(home)), "studio", "-H", "127.0.0.1", "-p", str(port)],
        env = {**os.environ, "UNSLOTH_STUDIO_HOME": str(home)},
        stdout = log_handle,
        stderr = subprocess.STDOUT,
        start_new_session = os.name != "nt",
    )
    try:
        await wait_for_health(base_url)
        password = read_bootstrap_password(home, log_path)
        if not password:
            raise RuntimeError("Studio bootstrap password was unavailable")
        auth = await authenticated_session(base_url, password)
        jwt_headers = {"Authorization": f"Bearer {auth.access_token}"}
        key_name = "DEVONthink trailing-slash evidence"

        async with httpx.AsyncClient(timeout = 60, follow_redirects = False) as client:
            created = await client.post(
                f"{base_url}/api/auth/api-keys",
                headers = jwt_headers,
                json = {"name": key_name, "expires_in_days": 7},
            )
            created.raise_for_status()
            created_body = created.json()
            api_key = created_body["key"]
            key_id = created_body["api_key"]["id"]

            before = await client.get(
                f"{base_url}/api/auth/api-keys", headers = jwt_headers
            )
            before.raise_for_status()
            before_row = next(
                row for row in before.json()["api_keys"] if row["id"] == key_id
            )
            if before_row["last_used_at"] is not None:
                raise AssertionError("fresh API key was already marked used")

            discovery = await client.get(
                f"{base_url}/v1/models/",
                headers = {"Authorization": f"Bearer {api_key}"},
            )

            after = await client.get(
                f"{base_url}/api/auth/api-keys", headers = jwt_headers
            )
            after.raise_for_status()
            after_row = next(
                row for row in after.json()["api_keys"] if row["id"] == key_id
            )

        expected_status = 200 if expect_direct else 404
        # Before the fix, /models/{model_id:path} consumes the empty segment. It
        # still authenticates the API key before returning model-not-found.
        expected_used = True
        observed_used = after_row["last_used_at"] is not None
        if discovery.status_code != expected_status:
            raise AssertionError(
                f"expected model discovery status {expected_status}, got "
                f"{discovery.status_code}"
            )
        if observed_used != expected_used:
            raise AssertionError(
                f"expected last_used={expected_used}, got {observed_used}"
            )
        if expect_direct:
            payload = discovery.json()
            if payload.get("object") != "list" or not isinstance(payload.get("data"), list):
                raise AssertionError("direct model discovery did not return an OpenAI list")

        init_script = seed_init_script(auth, [])
        async with async_playwright() as playwright:
            browser = await getattr(playwright, browser_name).launch(headless = True)
            context = await browser.new_context(
                viewport = {"width": 1440, "height": 900}
            )
            await context.add_init_script(init_script)
            page = await context.new_page()
            await page.goto(f"{base_url}/chat", wait_until = "domcontentloaded")
            settings = page.get_by_role("button", name = "Settings", exact = True)
            await settings.wait_for(state = "visible", timeout = 30_000)
            await settings.click()
            dialog = page.get_by_role("dialog")
            await dialog.wait_for(state = "visible", timeout = 30_000)
            await page.locator('[data-testid="settings-tab-api-keys"]').click()
            name = page.get_by_text(key_name, exact = True)
            await name.wait_for(state = "visible", timeout = 30_000)
            row = name.locator("xpath=../../..")
            row_text = await row.inner_text()
            expected_text = "Used just now"
            if expected_text not in row_text:
                raise AssertionError(
                    f"API settings row omitted {expected_text!r}: {row_text!r}"
                )
            await dialog.screenshot(
                path = str(
                    artifact_dir
                    / ("after-api-settings.png" if expect_direct else "before-api-settings.png")
                )
            )
            await row.screenshot(
                path = str(
                    artifact_dir
                    / ("after-token-row.png" if expect_direct else "before-token-row.png")
                )
            )

            async def authorize_discovery(route, request):
                headers = await request.all_headers()
                headers["authorization"] = f"Bearer {api_key}"
                await route.continue_(headers = headers)

            await context.route("**/v1/models/", authorize_discovery)
            response_page = await context.new_page()
            browser_response = await response_page.goto(
                f"{base_url}/v1/models/", wait_until = "domcontentloaded"
            )
            if browser_response is None or browser_response.status != expected_status:
                raise AssertionError(
                    "Playwright navigation did not reproduce the API status: "
                    f"{None if browser_response is None else browser_response.status}"
                )
            await response_page.screenshot(
                path = str(
                    artifact_dir
                    / (
                        "after-model-catalog-response.png"
                        if expect_direct
                        else "before-model-not-found-response.png"
                    )
                ),
                full_page = True,
            )
            await context.close()
            await browser.close()

        facts = {
            "commit": commit,
            "expect_direct": expect_direct,
            "model_discovery_status": discovery.status_code,
            "redirect_location": discovery.headers.get("location"),
            "api_key_used": observed_used,
            "catalog_object": discovery.json().get("object") if expect_direct else None,
            "catalog_count": len(discovery.json().get("data", [])) if expect_direct else None,
            "row_text": row_text,
            "studio_home": str(home),
            "studio_port": port,
            "browser": browser_name,
            "browser_status": browser_response.status if browser_response else None,
        }
        (artifact_dir / "facts.json").write_text(
            json.dumps(facts, indent = 2), encoding = "utf-8"
        )
        log_result(
            "PASS",
            f"commit={commit[:12]} status={discovery.status_code} "
            f"api_key_used={observed_used} ui={expected_text!r}",
        )
    finally:
        log_handle.close()
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


if __name__ == "__main__":
    asyncio.run(main())
