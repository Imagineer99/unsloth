import asyncio
import json
import os

from starlette.requests import Request
from .alias_helpers import _ALIAS_FAST, _ALIAS_LONG, _ALIAS_TARGET, _wire_alias_config, inference_route
from .test_model_aliases import store
from routes.settings import ModelAliasesPayload, get_model_aliases_setting
from utils.model_aliases import ModelAliasMiddleware, set_model_aliases


def test_alias_selection(monkeypatch, store):
    updated = os.environ["ALIAS_SIDE"] == "head"
    backend, recorder = _wire_alias_config(monkeypatch)
    from utils import openai_auto_switch_settings as settings
    monkeypatch.setattr(settings, "_cached_setting", lambda key, default=None, *args: store.get(key, default))
    entries = [{"name": name, "model": _ALIAS_TARGET, "load_config": config}
               for name, config in [("fast", _ALIAS_FAST), ("long", _ALIAS_LONG)]]
    entries.append({"name": "plain", "model": _ALIAS_TARGET})
    payload = ModelAliasesPayload(aliases=entries)
    set_model_aliases([row.model_dump(exclude_none=True) for row in payload.aliases])
    saved = get_model_aliases_setting().model_dump()["aliases"]
    expected = entries if updated else [{"name": row["name"], "model": row["model"]} for row in entries]
    assert saved == expected

    async def select(name):
        async def receive():
            return {"type": "http.request", "body": json.dumps({"model": name}).encode(), "more_body": False}

        async def route(scope, receive, send):
            request = Request(scope, receive)
            target = (await request.json())["model"]
            assert target == _ALIAS_TARGET
            await inference_route._maybe_auto_switch_model(target, request, "tester")

        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                 "scheme": "http", "server": ("localhost", 8888), "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}
        await ModelAliasMiddleware(route)(scope, receive, None)

    for name, count in [("fast", 1), ("long", 2), ("long", 2), ("fast", 3)]:
        asyncio.run(select(name))
        assert len(recorder.calls) == (count if updated else 0)
    launches = [(r.max_seq_length, r.cache_type_kv, r.speculative_type) for r in recorder.calls]
    expected_launches = [(53248, "q4_0", "auto"), (98304, "q5_1", "ngram"), (53248, "q4_0", "auto")]
    assert launches == (expected_launches if updated else [])
    print(f"{os.environ['ALIAS_SIDE']}: saved={saved}; launches={launches}")
