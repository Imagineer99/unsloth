#!/usr/bin/env python3
"""Capture PR #8264 before/after evidence from a real Windows Tauri WebView2.

Playwright attaches to the running desktop app over WebView2's CDP endpoint. Only backend
startup/auth IPC is replaced: the Tauri executable, native window, bundled frontend, custom
titlebar, sidebar, modal portal, CSS, and pixels under test are the source commit's real code.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright


VARIANT = os.environ["REPRO_VARIANT"]
ARTIFACTS = Path(os.environ.get("PW_ART_DIR", "evidence"))
CDP_URL = os.environ.get("WEBVIEW2_CDP_URL", "http://127.0.0.1:9222")
ARTIFACTS.mkdir(parents = True, exist_ok = True)


def wait_for_cdp(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout = 2) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001 - retain the final connection failure
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"WebView2 CDP endpoint did not appear: {last_error}")


MOCK_DESKTOP_BOUNDARY = r"""
(() => {
  localStorage.setItem("theme", "light");
  localStorage.setItem("palette", "standard");
  localStorage.setItem("sidebar_pinned", "true");
  localStorage.setItem("sidebar_width", "360");
  localStorage.setItem("unsloth_onboarding_done", "true");
  localStorage.removeItem("unsloth_auth_must_change_password");

  const internals = window.__TAURI_INTERNALS__;
  if (!internals || internals.__pr8264Wrapped) return;
  const invoke = internals.invoke.bind(internals);
  internals.invoke = async (command, args, options) => {
    if (command === "desktop_preflight") {
      return {
        disposition: "attached_ready",
        reason: null,
        port: 18888,
        can_auto_repair: false,
        managed_bin: null,
      };
    }
    if (command === "desktop_auth") {
      return { access_token: "pr8264-access", refresh_token: "pr8264-refresh" };
    }
    if (command === "check_health") return true;
    return invoke(command, args, options);
  };
  internals.__pr8264Wrapped = true;
})();
"""


def choose_tauri_page(pages: list[Page]) -> Page:
    for page in pages:
        if page.url.startswith(("tauri://", "http://tauri.localhost")):
            return page
    if pages:
        return pages[0]
    raise RuntimeError("WebView2 exposed no page target")


def average_rgb(image: Image.Image, x: int, y: int, radius: int = 1) -> tuple[float, float, float]:
    pixels: list[tuple[int, int, int]] = []
    for py in range(max(0, y - radius), min(image.height, y + radius + 1)):
        for px in range(max(0, x - radius), min(image.width, x + radius + 1)):
            pixels.append(image.getpixel((px, py)))
    return tuple(sum(pixel[channel] for pixel in pixels) / len(pixels) for channel in range(3))


def luminance(rgb: tuple[float, float, float]) -> float:
    return sum(rgb) / 3


def run() -> None:
    wait_for_cdp()
    screenshot = ARTIFACTS / f"{VARIANT}.png"
    report_path = ARTIFACTS / f"{VARIANT}.json"
    console_path = ARTIFACTS / f"{VARIANT}-console.log"
    console_lines: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout = 120_000)
        pages = [page for context in browser.contexts for page in context.pages]
        page = choose_tauri_page(pages)
        page.on("console", lambda message: console_lines.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console_lines.append(f"pageerror: {error}"))
        page.add_init_script(MOCK_DESKTOP_BOUNDARY)
        page.reload(wait_until = "domcontentloaded", timeout = 120_000)

        page.wait_for_selector('header[aria-label="Window titlebar"]', timeout = 120_000)
        settings = page.locator('button[aria-label="Settings"]:visible')
        deadline = time.monotonic() + 120
        while settings.count() == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        if settings.count() == 0:
            raise AssertionError("Tauri app shell never exposed the Settings button")

        settings.last.click()
        page.wait_for_selector('[data-slot="dialog-overlay"]', state = "visible", timeout = 30_000)
        page.wait_for_timeout(500)

        metrics = page.evaluate(
            """
            () => {
              const header = document.querySelector('header[aria-label="Window titlebar"]');
              const overlay = document.querySelector('[data-slot="dialog-overlay"]');
              const decoration = document.querySelector('[data-slot="window-titlebar-decoration"]');
              if (!header || !overlay) throw new Error('missing desktop chrome or modal overlay');
              const nav = [...header.children].find((element) => element.style.width);
              const sidebarWidth = Number.parseFloat(nav?.style.width || '360');
              const headerBox = header.getBoundingClientRect();
              const oldCornerCount = [...header.querySelectorAll('div')].filter(
                (element) => element.className.includes('size-3') && element.style.left,
              ).length;
              return {
                protocol: location.protocol,
                hostname: location.hostname,
                url: location.href,
                userAgent: navigator.userAgent,
                tauriInternals: Boolean(window.__TAURI_INTERNALS__),
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                devicePixelRatio: window.devicePixelRatio,
                sidebarWidth,
                titlebarHeight: headerBox.height,
                headerZ: getComputedStyle(header).zIndex,
                overlayZ: getComputedStyle(overlay).zIndex,
                decorationZ: decoration ? getComputedStyle(decoration).zIndex : null,
                decorationPresent: Boolean(decoration),
                oldCornerCount,
              };
            }
            """
        )
        is_tauri_url = metrics["protocol"] == "tauri:" or (
            metrics["protocol"] == "http:" and metrics["hostname"] == "tauri.localhost"
        )
        if not metrics["tauriInternals"] or not is_tauri_url:
            raise AssertionError(f"not a real Tauri page: {metrics}")

        page.screenshot(path = str(screenshot), animations = "disabled")
        browser.close()

    console_path.write_text("\n".join(console_lines), encoding = "utf-8")
    image = Image.open(screenshot).convert("RGB")
    scale_x = image.width / metrics["innerWidth"]
    scale_y = image.height / metrics["innerHeight"]
    corner_x = round((metrics["sidebarWidth"] + 4) * scale_x)
    sample_y = round((metrics["titlebarHeight"] + 4) * scale_y)
    adjacent_x = round((metrics["sidebarWidth"] + 20) * scale_x)
    corner_rgb = average_rgb(image, corner_x, sample_y)
    adjacent_rgb = average_rgb(image, adjacent_x, sample_y)
    mismatch = abs(luminance(corner_rgb) - luminance(adjacent_rgb))
    report = {
        **metrics,
        "screenshot": screenshot.name,
        "cornerCss": [metrics["sidebarWidth"] + 4, metrics["titlebarHeight"] + 4],
        "adjacentCss": [metrics["sidebarWidth"] + 20, metrics["titlebarHeight"] + 4],
        "cornerRgb": [round(value, 2) for value in corner_rgb],
        "adjacentRgb": [round(value, 2) for value in adjacent_rgb],
        "luminanceMismatch": round(mismatch, 2),
    }
    report_path.write_text(json.dumps(report, indent = 2), encoding = "utf-8")

    if VARIANT == "before":
        if metrics["decorationPresent"]:
            raise AssertionError(f"before unexpectedly has sibling decoration: {report}")
        if metrics["oldCornerCount"] < 1:
            raise AssertionError(f"before corner is not trapped under the titlebar: {report}")
        if mismatch < 25:
            raise AssertionError(f"before did not reproduce the bright corner: {report}")
        print(f"PASS before reproduced bright Tauri corner mismatch={mismatch:.2f}")
    elif VARIANT == "after":
        if not metrics["decorationPresent"] or metrics["decorationZ"] != "45":
            raise AssertionError(f"after decoration is not the z-45 sibling: {report}")
        if metrics["oldCornerCount"] != 0:
            raise AssertionError(f"after still nests a corner under the titlebar: {report}")
        if mismatch > 5:
            raise AssertionError(f"after still has a bright corner: {report}")
        print(f"PASS after dimmed Tauri corner mismatch={mismatch:.2f}")
    else:
        raise AssertionError(f"unknown REPRO_VARIANT={VARIANT}")

    print(json.dumps(report, sort_keys = True))


if __name__ == "__main__":
    run()
