"""Disposable integration repro; generation is a double, the Research/API path is real."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from auth.authentication import get_current_subject
from core import research_runs
from core.research_runs import ResearchSupervisor
from routes import inference as inference_route
from utils.api_errors import install_api_error_handlers
from .test_sf_client_tools_passthrough import _ScriptedBackend, _fixed, _install


@pytest.mark.parametrize("phase", ["planning", "decision", "synthesis_audit"])
@pytest.mark.parametrize("remove_format", [False, True], ids=["reported-request", "format-only-control"])
def test_research_mlx_response_format_repro(monkeypatch, phase, remove_format):
    backend = _ScriptedBackend(_fixed('{"ok": true}'))
    backend.models[backend.active_model_name]["is_mlx"] = True
    _install(monkeypatch, backend, supports_tools=False)
    monkeypatch.setattr(research_runs, "_peek_inference_backend", lambda: backend)
    app = FastAPI()
    app.include_router(inference_route.router, prefix="/v1")
    install_api_error_handlers(app)
    app.dependency_overrides[get_current_subject] = lambda: "repro-user"
    captured = []

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request):
            payload = json.loads(request.content)
            # Capture the actual supervisor payload before the single-field control.
            assert payload["response_format"] == {"type": "json_object"}
            assert payload["tool_choice"] == "none"
            assert payload["enabled_tools"] == []
            offered = dict(payload)
            if remove_format:
                del payload["response_format"]
                request = httpx.Request(
                    request.method, request.url,
                    headers={k: v for k, v in request.headers.items() if k != "content-length"},
                    json=payload,
                )
            response = await self.inner.handle_async_request(request)
            body = await response.aread()
            captured.append({"offered_payload": offered, "sent_payload": payload,
                             "status": response.status_code, "body": body.decode()})
            return response

        async def aclose(self):
            await self.inner.aclose()

    real_client = httpx.AsyncClient
    monkeypatch.setattr(research_runs.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=RecordingTransport(), **kwargs))
    monkeypatch.setattr(research_runs.auth_storage, "create_api_key",
                        lambda **kwargs: ("repro-only", {"id": 1}))
    monkeypatch.setattr(research_runs.auth_storage, "revoke_internal_api_key", lambda key_id: None)
    supervisor = ResearchSupervisor(SimpleNamespace(state=SimpleNamespace(server_port=1)))

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(supervisor, "_check_active", noop)
    monkeypatch.setattr(supervisor, "_note_phase", noop)
    run = {"id": "mlx-repro", "ownerSubject": "repro-user", "config": {
        "model": backend.active_model_name, "budgets": {"modelTimeoutSeconds": 20}}}
    call = supervisor._stream_completion(
        run, [{"role": "user", "content": "Return a JSON object with ok set to true."}],
        json_mode=True, report_progress=False, phase=phase, max_tokens=32, enable_thinking=False,
    )
    if remove_format:
        report, _, finish_reason, _ = asyncio.run(call)
        assert json.loads(report) == {"ok": True}
        assert finish_reason == "stop"
        assert len(backend.calls) == 1
        expected_status = 200
        print(f"PASS CONTROL {phase}: removing only response_format reaches generation (HTTP 200)")
    else:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(call)
        error = caught.value.response.json()["error"]
        assert caught.value.response.status_code == 400
        assert error["code"] == "unsupported_parameter"
        assert error["param"] == "response_format"
        assert "llama.cpp grammar engine" in error["message"]
        assert backend.calls == []
        assert research_runs._safe_error(caught.value) == "Local model request failed with HTTP 400"
        expected_status = 400
        print(f"PASS REPRO {phase}: HTTP 400 unsupported_parameter=response_format; generation calls=0")
    assert len(captured) == 1
    assert captured[0]["status"] == expected_status
    evidence = {"phase": phase, "remove_only_response_format": remove_format,
                "generation_calls": len(backend.calls), "hardware_inference": False,
                "transport": "in-process HTTPX ASGI", **captured[0]}
    if os.environ.get("REPRO_ARTIFACT_DIR"):
        out = Path(os.environ["REPRO_ARTIFACT_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{phase}-{'control' if remove_format else 'reported'}.json").write_text(
            json.dumps(evidence, indent=2))
