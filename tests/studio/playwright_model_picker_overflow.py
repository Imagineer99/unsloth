# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Focused rendered-layout check for PR #7403's model-picker toolbar."""

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _playwright_robust import click_and_wait_for_response, wait_for_health  # noqa: E402

BASE = os.environ["BASE_URL"]
NEW_PASSWORD = os.environ["STUDIO_NEW_PW"]
ART = Path(os.environ.get("PW_ART_DIR", "logs/playwright_model_picker_overflow"))
ART.mkdir(parents = True, exist_ok = True)

VIEWPORT_WIDTHS = (320, 420, 520, 646, 1280)
FONT_SIZES = (12, 16, 20)


def step(message: str) -> None:
    print(f"[picker-overflow] STEP {message}", flush = True)


def authenticate(page: Page) -> str:
    page.goto(
        f"{BASE}/change-password",
        wait_until = "domcontentloaded",
        timeout = 60_000,
    )
    page.locator("#new-password").wait_for(state = "visible", timeout = 60_000)
    page.locator("#new-password").fill(NEW_PASSWORD)
    page.locator("#confirm-password").fill(NEW_PASSWORD)
    status, _ = click_and_wait_for_response(
        page,
        url_substr = "/api/auth/change-password",
        method = "POST",
        do_click = lambda: page.locator('button[type="submit"]').click(),
        timeout_ms = 30_000,
        info = lambda message: print(f"[picker-overflow] {message}", flush = True),
    )
    if status is not None and status >= 400:
        raise AssertionError(f"change-password returned HTTP {status}")
    page.locator('textarea[aria-label="Message input"]').wait_for(
        state = "visible",
        timeout = 60_000,
    )
    token = page.evaluate(
        "() => localStorage.getItem('unsloth_auth_token')",
    )
    if not token:
        raise AssertionError("missing auth token after password setup")
    return token


def create_connected_provider(page: Page, token: str) -> None:
    result = page.evaluate(
        """
        async ({ token }) => {
          const response = await fetch("/api/providers/", {
            method: "POST",
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              provider_type: "llama_cpp",
              display_name: "CI Connected",
              base_url: "http://127.0.0.1:65535/v1",
              models: ["ci/connected-model"],
              available_models: ["ci/connected-model"],
            }),
          });
          return { status: response.status, body: await response.text() };
        }
        """,
        {"token": token},
    )
    if result["status"] not in (200, 201):
        raise AssertionError(f"provider create failed: {result}")
    page.reload(wait_until = "domcontentloaded")
    page.locator('textarea[aria-label="Message input"]').wait_for(
        state = "visible",
        timeout = 60_000,
    )


def set_font_size(page: Page, size: int) -> None:
    page.evaluate(
        """
        size => {
          const root = document.documentElement;
          if (size === 16) {
            root.style.removeProperty("--ui-font-scale");
            root.removeAttribute("data-ui-font-size");
          } else {
            root.style.setProperty("--ui-font-scale", String(size / 16));
            root.setAttribute("data-ui-font-size", String(size));
          }
        }
        """,
        size,
    )


def open_picker(page: Page):
    page.keyboard.press("Escape")
    trigger = page.locator('[data-tour="chat-model-selector"]:visible').first
    trigger.wait_for(state = "visible", timeout = 30_000)
    trigger.click()
    picker = page.locator(".unsloth-model-selector-menu:visible").first
    picker.wait_for(state = "visible", timeout = 30_000)
    page.wait_for_timeout(250)
    return picker


def measure_picker(picker):
    return picker.evaluate(
        """
        picker => {
          const box = picker.getBoundingClientRect();
          const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return (
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              rect.width > 0 &&
              rect.height > 0
            );
          };
          const descendants = [...picker.querySelectorAll("*")]
            .filter(visible)
            .map(el => {
              const rect = el.getBoundingClientRect();
              return {
                tag: el.tagName,
                role: el.getAttribute("role"),
                aria: el.getAttribute("aria-label"),
                text: (el.textContent || "").trim().slice(0, 80),
                left: rect.left,
                right: rect.right,
              };
            });
          const tablist = picker.querySelector(
            '[role="tablist"][aria-label="Hub section"]',
          );
          const format = picker.querySelector('[aria-label="Filter by format"]');
          const sort = picker.querySelector('[aria-label^="Sort "]');
          const tabButtons = tablist
            ? [...tablist.querySelectorAll('[role="tab"]')]
            : [];
          const labelStyle = tabButtons.map(button => {
            const label = button.querySelector("span");
            const style = label ? getComputedStyle(label) : null;
            return {
              accessibleName: button.getAttribute("aria-label") || button.innerText.trim(),
              label: label ? label.textContent.trim() : "",
              overflow: style?.overflow,
              textOverflow: style?.textOverflow,
              whiteSpace: style?.whiteSpace,
              clientWidth: label?.clientWidth ?? null,
              scrollWidth: label?.scrollWidth ?? null,
            };
          });
          const rect = el => {
            if (!el) return null;
            const value = el.getBoundingClientRect();
            return {
              left: value.left,
              right: value.right,
              top: value.top,
              bottom: value.bottom,
              width: value.width,
            };
          };
          return {
            picker: rect(picker),
            tablist: rect(tablist),
            format: rect(format),
            sort: rect(sort),
            tabLabels: labelStyle,
            overflow: descendants.filter(
              item => item.left < box.left - 1 || item.right > box.right + 1,
            ),
          };
        }
        """
    )


def assert_layout(
    page: Page,
    mode: str,
    width: int,
    font_size: int,
) -> dict:
    set_font_size(page, font_size)
    picker = open_picker(page)
    metrics = measure_picker(picker)
    picker_box = metrics["picker"]
    if not picker_box or not metrics["tablist"]:
        raise AssertionError(f"{mode} {width}px/{font_size}px missing picker controls")
    if picker_box["left"] < -1 or picker_box["right"] > width + 1:
        raise AssertionError(
            f"{mode} {width}px/{font_size}px picker crossed viewport: {picker_box}"
        )
    if metrics["overflow"]:
        raise AssertionError(
            f"{mode} {width}px/{font_size}px descendants crossed picker: "
            f"{json.dumps(metrics['overflow'][:8], indent=2)}"
        )
    expected_tabs = (
        ["Recommended", "On Device", "Connected"]
        if mode == "connected"
        else ["Recommended", "On Device"]
    )
    names = [entry["accessibleName"] for entry in metrics["tabLabels"]]
    if names != expected_tabs:
        raise AssertionError(
            f"{mode} {width}px/{font_size}px tab names changed: {names}"
        )
    for label in metrics["tabLabels"]:
        if (
            label["overflow"] != "hidden"
            or label["textOverflow"] != "ellipsis"
            or label["whiteSpace"] != "nowrap"
        ):
            raise AssertionError(
                f"{mode} {width}px/{font_size}px label cannot truncate: {label}"
            )
    if width == 320 and font_size == 20:
        if not metrics["format"] or not metrics["sort"]:
            raise AssertionError("constrained toolbar controls are missing")
        if metrics["format"]["top"] <= metrics["tablist"]["top"] + 4:
            raise AssertionError(
                f"{mode} constrained toolbar did not wrap: {json.dumps(metrics)}"
            )
        if mode == "connected" and not any(
            (label["scrollWidth"] or 0) > (label["clientWidth"] or 0)
            for label in metrics["tabLabels"]
        ):
            raise AssertionError(
                "connected constrained tabs did not exercise label truncation"
            )
    if width == 1280 and font_size == 16:
        if not metrics["format"]:
            raise AssertionError("wide toolbar format control is missing")
        if abs(metrics["format"]["top"] - metrics["tablist"]["top"]) > 4:
            raise AssertionError(
                f"{mode} wide toolbar wrapped unexpectedly: {json.dumps(metrics)}"
            )
    page.screenshot(
        path = str(ART / f"{mode}-{width}px-font-{font_size}.png"),
        full_page = True,
        animations = "disabled",
    )
    page.keyboard.press("Escape")
    return metrics


def run_matrix(page: Page, mode: str) -> None:
    for width in VIEWPORT_WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        for font_size in FONT_SIZES:
            step(f"{mode}: viewport {width}px, UI font {font_size}px")
            metrics = assert_layout(page, mode, width, font_size)
            print(
                f"[picker-overflow] PASS {mode} {width}px/{font_size}px "
                f"picker={metrics['picker']['width']:.1f}px",
                flush = True,
            )


def main() -> None:
    wait_for_health(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport = {"width": 1280, "height": 900})
        page = context.new_page()
        token = authenticate(page)
        run_matrix(page, "without-connected")
        create_connected_provider(page, token)
        run_matrix(page, "connected")
        browser.close()
    print("[picker-overflow] PASS all rendered layout checks", flush = True)


if __name__ == "__main__":
    main()
