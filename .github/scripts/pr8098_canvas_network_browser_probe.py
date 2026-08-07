"""Browser-level proof for PR 8098's strict/permissive canvas CSP switch."""

from __future__ import annotations

import asyncio
import html
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio" / "backend"))

from routes import inference as inference_routes  # noqa: E402


requested_probe_cases: list[str] = []


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/frame":
            allow_network = query.get("allow_network") == ["1"]
            response = asyncio.run(
                inference_routes.artifact_preview_frame(allow_network)
            )
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)
            return

        if parsed.path == "/probe.js":
            case = query.get("case", [""])[0]
            requested_probe_cases.append(case)
            body = f"document.documentElement.dataset.networkProbe = {case!r};"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if parsed.path == "/":
            document = """<!doctype html>
<iframe id="strict" sandbox="allow-scripts" src="/frame"></iframe>
<iframe id="permissive" sandbox="allow-scripts" src="/frame?allow_network=1"></iframe>
<script>
for (const id of ["strict", "permissive"]) {
  const frame = document.getElementById(id);
  frame.addEventListener("load", () => {
    if (frame.dataset.sent) return;
    frame.dataset.sent = "1";
    frame.contentWindow.postMessage({
      type: "unsloth:artifact-html",
      html: `<script src="/probe.js?case=${id}"><` + "/script>",
    }, "*");
  });
}
</script>"""
            body = document.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, html.escape(parsed.path))


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(origin, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert "permissive" in requested_probe_cases, requested_probe_cases
    assert "strict" not in requested_probe_cases, requested_probe_cases
    print("PASS allow_network=1 made the browser request the external script")
    print("PASS the strict frame blocked the same script before any request")
    print(f"PASS observed probe requests: {requested_probe_cases!r}")


if __name__ == "__main__":
    main()
