"""Check the same sidebar edit in two independent, authenticated Chromium profiles."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


def main(side, origin):
    def request(path, payload, token=None, method="POST"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(origin + path, data=json.dumps(payload).encode(), headers=headers, method=method)
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def login(password):
        return request("/api/auth/login", {"username": "unsloth", "password": password})

    bootstrap = (Path(os.environ["UNSLOTH_STUDIO_HOME"]) / "auth/.bootstrap_password").read_text().strip()
    password = secrets.token_urlsafe(30)
    request("/api/auth/change-password", {"current_password": bootstrap, "new_password": password},
            login(bootstrap)["access_token"])
    credentials = [login(password) for _ in range(2)]
    assert credentials[0]["account_id"] == credentials[1]["account_id"]
    thread = "sidebar-ab-chat"
    title = "Sidebar validation chat"
    now = int(time.time() * 1000)
    token = credentials[0]["access_token"]
    request("/api/chat/threads", {"id": thread, "title": title, "modelType": "base",
                                  "createdAt": now, "updatedAt": now}, token)
    request(f"/api/chat/threads/{thread}/messages/seed", {
        "id": "seed", "threadId": thread, "role": "user",
        "content": [{"type": "text", "text": title}], "createdAt": now,
    }, token, "PUT")

    def profile(browser, credential):
        auth = [{"name": name, "value": credential[key]} for name, key in (
            ("unsloth_auth_token", "access_token"), ("unsloth_auth_refresh_token", "refresh_token"))]
        state = {"cookies": [], "origins": [{"origin": origin, "localStorage": auth}]}
        return browser.new_context(storage_state=state, viewport={"width": 1440, "height": 960}).new_page()

    row = f'[data-testid="recent-thread"][data-thread-id="{thread}"]:visible'
    def section(page):
        return page.locator("[data-sidebar-section]:visible").filter(
            has=page.get_by_text("Research", exact=True))

    endpoint = "/api/chat/sidebar-sections"
    def saved(response):
        if not (response.url.endswith(endpoint) and response.request.method == "POST"):
            return False
        layout = response.request.post_data_json["layout"]
        return thread in layout["sectionByChatId"]

    with sync_playwright() as pw, pw.chromium.launch() as browser:
        a = profile(browser, credentials[0])
        a.goto(origin + "/chat", wait_until="domcontentloaded")
        expect(a.locator(row)).to_contain_text(title, timeout=60000)
        expect(section(a)).to_have_count(0)
        a.locator(row).click(button="right")
        a.get_by_role("menuitem", name="Move to", exact=True).hover()
        a.get_by_role("menuitem", name="New section", exact=True).click()
        dialog = a.get_by_role("dialog")
        dialog.get_by_role("textbox").fill("Research")
        watcher = a.expect_response(saved, timeout=45000) if side == "head" else contextlib.nullcontext()
        with watcher as event:
            dialog.get_by_role("button", name="Create section", exact=True).click()
        if side == "head":
            response = event.value
            assert response.status == 200, f"layout save HTTP {response.status}"
            layout = response.json()
            section_id = layout["sectionByChatId"][thread]
            assert layout["sections"][section_id]["name"] == "Research"
        expect(section(a).locator(row)).to_contain_text(title, timeout=30000)
        print(f"PASS {side} profile A created Research and filed the known chat", flush=True)

        b = profile(browser, credentials[1])
        for visit in ("fresh", "reload"):
            fetch = (b.expect_response(lambda res: res.url.endswith(endpoint) and res.request.method == "GET",
                                       timeout=45000) if side == "head" else contextlib.nullcontext())
            with fetch as received:
                if visit == "fresh":
                    b.goto(origin + "/chat", wait_until="domcontentloaded")
                else:
                    b.reload(wait_until="domcontentloaded")
            expect(b.locator(row)).to_contain_text(title, timeout=60000)
            if side == "head":
                assert received.value.status == 200
                expect(section(b).locator(row)).to_contain_text(title, timeout=30000)
            else:
                expect(section(b)).to_have_count(0)
            print(f"PASS {side} profile B {visit}: known chat visible, Research membership={side == 'head'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("side", choices=("base", "head"))
    parser.add_argument("origin")
    args = parser.parse_args()
    main(args.side, args.origin)
