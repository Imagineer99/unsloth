#!/usr/bin/env python3
"""Cross-browser A/B proof for PR 8514's chat-search dialog geometry."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket

from playwright.async_api import async_playwright


BASE_SHA = "66188b8d59a054a5779a79d859de1efa1773268a"
PR_HEAD_SHA = "a6a8e6fa05f0edbb09b5f57e35b34e377fa4b3e7"
COMPONENT = "studio/frontend/src/features/chat/components/chat-search-dialog.tsx"

HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #111318; color: #f5f6f8; font: 14px system-ui; }
    .label { position: fixed; top: 16px; transform: translateX(-50%); font-weight: 700; }
    .surface {
      position: fixed; top: 50%; transform: translate(-50%, -50%); width: min(42vw, 635px);
      border: 1px solid #3b414b; border-radius: 24px; overflow: hidden; background: #1b1f27;
    }
    .base { left: 25%; }
    .head { left: 75%; }
    .header { height: 49px; padding: 15px; border-bottom: 1px solid #3b414b; }
    .list { overflow: auto; padding: 4px; }
    #base-list { max-height: 420px; }
    #head-list { height: 420px; max-height: 60dvh; }
    .row { height: 38px; padding: 10px 12px; border-radius: 20px; }
    .row:nth-child(odd) { background: #252a34; }
  </style>
</head>
<body>
  <div class="label" style="left:25%">BEFORE — base</div>
  <div class="label" style="left:75%">AFTER — PR 8514</div>
  <section id="base-surface" class="surface base"><div class="header">Search chats…</div><div id="base-list" class="list"></div></section>
  <section id="head-surface" class="surface head"><div class="header">Search chats…</div><div id="head-list" class="list"></div></section>
  <script>
    window.setRows = n => {
      for (const id of ['base-list', 'head-list']) {
        const list = document.getElementById(id);
        list.replaceChildren(...Array.from({length:n}, (_, i) => {
          const row = document.createElement('div'); row.className = 'row';
          row.textContent = n === 1 ? 'Loading…' : `Chat ${i + 1}`; return row;
        }));
      }
    };
    window.measure = () => Object.fromEntries(['base', 'head'].map(kind => {
      const list = document.getElementById(`${kind}-list`).getBoundingClientRect();
      const surface = document.getElementById(`${kind}-surface`).getBoundingClientRect();
      return [kind, {listHeight:list.height, surfaceTop:surface.top, surfaceHeight:surface.height}];
    }));
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def free_port() -> int:
    with closing(socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def verify_source(repo: Path) -> dict[str, bool]:
    base = subprocess.check_output(
        ["git", "show", f"{BASE_SHA}:{COMPONENT}"], cwd=repo, text=True
    )
    head = subprocess.check_output(
        ["git", "show", f"{PR_HEAD_SHA}:{COMPONENT}"], cwd=repo, text=True
    )
    checks = {
        "base_has_content_dependent_height": 'max-h-[420px] p-1' in base,
        "base_lacks_fixed_height": 'h-[420px] max-h-[60dvh]' not in base,
        "head_has_fixed_responsive_height": 'h-[420px] max-h-[60dvh]' in head,
    }
    if not all(checks.values()):
        raise AssertionError(f"source shape changed: {checks}")
    return checks


async def run(browser_name: str, output: Path) -> dict:
    repo = Path(__file__).resolve().parents[2]
    source_checks = verify_source(repo)
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    try:
        async with async_playwright() as pw:
            if browser_name in {"chrome", "msedge"}:
                browser = await pw.chromium.launch(channel=browser_name)
            else:
                browser = await getattr(pw, browser_name).launch()
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{port}/")
            user_agent = await page.evaluate("navigator.userAgent")
            dvh = await page.evaluate("CSS.supports('height', '60dvh')")

            for width, height in ((1280, 900), (800, 500), (600, 400)):
                await page.set_viewport_size({"width": width, "height": height})
                states: dict[str, dict] = {}
                for name, rows in (("loading", 1), ("populated", 200), ("filtered", 2)):
                    await page.evaluate("rows => window.setRows(rows)", rows)
                    states[name] = await page.evaluate("window.measure()")
                    await page.screenshot(
                        path=output / f"{browser_name}-{width}x{height}-{name}.png"
                    )

                base_heights = [states[s]["base"]["listHeight"] for s in states]
                base_tops = [states[s]["base"]["surfaceTop"] for s in states]
                head_heights = [states[s]["head"]["listHeight"] for s in states]
                head_tops = [states[s]["head"]["surfaceTop"] for s in states]
                expected_head = min(420, height * 0.6)
                result = {
                    "viewport": [width, height],
                    "states": states,
                    "base_list_resize_px": max(base_heights) - min(base_heights),
                    "base_surface_travel_px": max(base_tops) - min(base_tops),
                    "head_list_resize_px": max(head_heights) - min(head_heights),
                    "head_surface_travel_px": max(head_tops) - min(head_tops),
                    "expected_head_height_px": expected_head,
                }
                assert result["base_list_resize_px"] >= 300, result
                assert result["base_surface_travel_px"] >= 150, result
                assert result["head_list_resize_px"] <= 1, result
                assert result["head_surface_travel_px"] <= 1, result
                assert all(abs(value - expected_head) <= 1 for value in head_heights), result
                results.append(result)
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()

    report = {
        "browser": browser_name,
        "user_agent": user_agent,
        "dvh_supported": dvh,
        "source_checks": source_checks,
        "base_sha": BASE_SHA,
        "pr_head_sha": PR_HEAD_SHA,
        "evidence_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "results": results,
        "pass": bool(dvh),
    }
    assert dvh, report
    (output / f"{browser_name}-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", required=True, choices=["chromium", "firefox", "webkit", "chrome", "msedge"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.browser, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
