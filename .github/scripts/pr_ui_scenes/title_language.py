"""Regenerate a title from fixed French SSE output on both Studio builds."""

from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from pathlib import Path

from pr_ui_scenes._common import api_get, api_post
from studio_test_kit.auth import ProviderSeed, seed_init_script
from studio_test_kit.ui import open_chat

TITLE = "Café Résumé Matrix"
THREAD = "uidiff-title-language-16413-" + uuid.uuid4().hex[:8]


def seed(session):
    provider = api_post(
        session,
        "/api/providers/",
        {
            "provider_type": "custom",
            "display_name": "Title fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "models": ["title-model"],
        },
    )
    model = f"external::{provider['id']}::title-model"
    api_post(
        session,
        "/api/chat/threads",
        {
            "id": THREAD,
            "title": "Discussion en français",
            "modelType": "base",
            "modelId": model,
            "createdAt": 1767225600000,
            "updatedAt": 1767225600000,
        },
    )
    for index, (role, text) in enumerate(
        [
            ("user", "Explique les différences entre le café et le résumé de notre discussion."),
            ("assistant", "Notre discussion porte sur le café et un résumé de matrices."),
        ]
    ):
        message = f"{THREAD}-{index}"
        payload = {
            "id": message,
            "threadId": THREAD,
            "role": role,
            "parentId": f"{THREAD}-0" if index else None,
            "content": [{"type": "text", "text": text}],
            "createdAt": 1767225600001 + index,
            "metadata": {"custom": {"responseDetails": {"modelId": model}}},
        }
        request = urllib.request.Request(
            f"{session.base_url}/api/chat/threads/{THREAD}/messages/{message}",
            data = json.dumps(payload).encode(),
            method = "PUT",
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session.access_token}",
            },
        )
        with urllib.request.urlopen(request, timeout = 60) as response:
            response.read()
    return provider["id"]


async def drive(session, out_dir: Path, label: str, **_):
    provider_id = seed(session)
    provider = ProviderSeed(
        "openai-compat",
        "Title fixture",
        "http://127.0.0.1:1/v1",
        ["title-model"],
        "",
        id = provider_id,
    )
    auth = type(
        "Auth", (), {"access_token": session.access_token, "refresh_token": session.refresh_token}
    )()
    init = seed_init_script(auth, [provider], extra_local_storage = {"unsloth_locale": "fr"})
    facts = {"fixture_output": TITLE, "_requests": []}

    async with open_chat(
        session.base_url, init_scripts = [init], viewport = (1280, 800), headless = True
    ) as sp:

        async def completion(route):
            body = route.request.post_data_json
            facts["_requests"].append(body)
            chunks = [
                {"choices": [{"delta": {"content": TITLE}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            stream = (
                "".join(
                    "data: " + json.dumps(chunk, ensure_ascii = False) + "\n\n" for chunk in chunks
                )
                + "data: [DONE]\n\n"
            )
            await route.fulfill(status = 200, content_type = "text/event-stream", body = stream)

        await sp.page.route("**/chat/completions", completion)
        await sp.page.goto(
            f"{session.base_url}/chat?thread={THREAD}", wait_until = "domcontentloaded"
        )
        await sp.page.get_by_text("Notre discussion porte sur le café", exact = False).first.wait_for(
            timeout = 60000
        )
        buttons = sp.page.locator("button[aria-label]")
        labels = await buttons.evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
        facts["_button_labels"] = labels
        more = sp.page.get_by_role("button", name = "Options de la discussion", exact = True)
        await more.click()
        await sp.page.get_by_role("menuitem", name = "Régénérer le titre", exact = True).click()
        expected = TITLE if label == "AFTER" else "Caf R sum Matrix"
        for _ in range(100):
            stored = api_get(session, f"/api/chat/threads/{THREAD}")
            if stored.get("title") == expected:
                break
            await asyncio.sleep(0.1)
        else:
            await sp.page.screenshot(path = str(out_dir / f"{label.lower()}_failed.png"))
            raise RuntimeError(f"title did not become {expected!r}: {stored!r}; facts={facts!r}")
        if len(facts["_requests"]) != 1:
            raise RuntimeError(f"expected one title request: {facts!r}")
        facts["title"] = stored["title"]
        facts["language_instruction"] = (
            "same language as the conversation" in facts["_requests"][0]["messages"][0]["content"]
        )
        await sp.page.get_by_text(expected, exact = True).first.wait_for(timeout = 10000)
        shot = out_dir / f"{label.lower()}_title_language.png"
        await sp.page.screenshot(path = str(shot), full_page = False)
    return [shot], facts
