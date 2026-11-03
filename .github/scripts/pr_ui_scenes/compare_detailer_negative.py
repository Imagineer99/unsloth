from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from studio_test_kit.auth import seed_init_script
from studio_test_kit.ui import open_chat


async def drive(session, out_dir: Path, label: str, **_):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wt_r16149/tests/studio"))
    from playwright_image_model_footprint import _api_payload

    facts = {"fixture": "SDXL and downloaded SAM3 status; no generated pixels"}
    init = seed_init_script(
        type(
            "Auth",
            (),
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
            },
        )(),
        [],
    )
    async with open_chat(
        session.base_url, init_scripts = [init], viewport = (1440, 1100), headless = True
    ) as sp:
        page = sp.page

        async def route_api(route):
            parsed = urlparse(route.request.url)
            payload = _api_payload(parsed.path, parse_qs(parsed.query), full_footprint = True)
            if parsed.path == "/api/inference/images/status":
                payload.update(
                    loaded = True,
                    repo_id = "test/sdxl",
                    base_repo = "test/sdxl",
                    family = "sdxl",
                    device = "cpu",
                    dtype = "float32",
                    model_kind = "diffusers",
                    workflows = ["create", "inpaint"],
                    supports_guidance = True,
                    supports_negative_prompt = True,
                    negative_prompt_min_guidance = 1,
                    generation_defaults = {"steps": 20, "guidance": 1, "width": 512, "height": 512},
                )
            if "segment" in parsed.path and "status" in parsed.path:
                payload = {
                    "supported": True,
                    "downloaded": True,
                    "loaded": False,
                    "download": {"downloading": False},
                }
            await route.fulfill(
                status = 200, content_type = "application/json", body = json.dumps(payload)
            )

        await page.route("**/api/**", route_api)
        await page.goto(session.base_url + "/images", wait_until = "domcontentloaded")
        await page.get_by_role("switch", name = "Detailer (face fix)", exact = True).wait_for(
            timeout = 60000
        )
        await page.locator("textarea").first.fill("a fox")
        await page.locator('input[aria-label="Guidance"]').fill("1")
        await page.locator('input[aria-label="Guidance"]').press("Tab")
        await page.get_by_role("switch", name = "Detailer (face fix)", exact = True).click()
        await page.get_by_label("Detailer CFG", exact = True).fill("7")
        await page.get_by_label("Detailer CFG", exact = True).press("Tab")
        toggle = page.get_by_role("button", name = "Negative prompt", exact = True)
        if await toggle.get_attribute("aria-expanded") != "true":
            await toggle.click()
        await page.get_by_placeholder("What to avoid (optional)", exact = True).fill("red eyes")
        await page.get_by_role("switch", name = "Compare grid", exact = True).click()
        await page.get_by_role("combobox", name = "X type", exact = True).click()
        await page.get_by_role("option", name = "Prompt S/R", exact = True).click()
        await page.get_by_label("X values", exact = True).fill("red, blue")
        panel = page.locator("#images-compare-toggle").locator("xpath=ancestor::div[3]")
        await page.get_by_label("X values", exact = True).press("Tab")
        await page.wait_for_timeout(300)
        facts["compare_text"] = await panel.inner_text()
        facts["run_enabled"] = await page.get_by_role(
            "button", name = re.compile(r"^Generate grid")
        ).is_enabled()
        shot = out_dir / f"{label.lower()}_compare.png"
        await panel.screenshot(path = str(shot))
        return [shot], facts
