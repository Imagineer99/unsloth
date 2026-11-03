import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless = True)
    context = browser.new_context(viewport = {"width": 1440, "height": 1000})
    base = os.environ["BASE_URL"]
    response = context.request.post(
        base + "/api/auth/login",
        data = {
            "username": "unsloth",
            "password": os.environ["STUDIO_PW"],
        },
    )
    assert response.status == 200, response.status
    tokens = response.json()
    context.add_init_script(
        "localStorage.setItem('unsloth_auth_token', "
        + json.dumps(tokens["access_token"])
        + ");"
        + "localStorage.setItem('unsloth_auth_refresh_token', "
        + json.dumps(tokens["refresh_token"])
        + ");"
    )
    page = context.new_page()
    page.goto(base + "/chat", wait_until = "domcontentloaded")
    page.get_by_role("button", name = "Settings", exact = True).click(timeout = 60000)
    page.get_by_test_id("settings-tab-chat").click()
    row = page.locator('[data-settings-label="Rich text paste"]')
    badge = row.locator('[data-slot="badge"]')
    badge.wait_for(state = "visible")
    assert badge.inner_text() == "BETA"
    assert row.get_by_role("switch").is_visible()
    out = Path(os.environ["PW_ART_DIR"])
    out.mkdir(parents = True, exist_ok = True)
    facts = {}
    for name, scale in (("default", 0.9375), ("large", 1.5)):
        page.evaluate(
            "s => document.documentElement.style.setProperty('--ui-font-size-scale', String(s))",
            scale,
        )
        page.evaluate("() => new Promise(requestAnimationFrame)")
        page.evaluate("""() => Promise.all(document.getAnimations()
            .filter(a => Number.isFinite(a.effect?.getTiming().iterations))
            .map(a => a.finished.catch(() => {})))""")
        size = badge.evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
        assert abs(size - 10 * scale) < 0.02, (name, size)
        facts[name] = {
            "font_px": size,
            "badge_visible": badge.is_visible(),
            "switch_visible": row.get_by_role("switch").is_visible(),
        }
        row.screenshot(path = str(out / f"rich_paste_{name}.png"), animations = "disabled")
    (out / "rich_paste_facts.json").write_text(json.dumps(facts, indent = 2))
    print("LIVE_STUDIO_RICH_PASTE " + json.dumps(facts))
    context.close()
    browser.close()
