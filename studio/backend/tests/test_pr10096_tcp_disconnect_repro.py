# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Live TCP-reset A/B probe for unslothai/unsloth PR #10096.

The probe launches the real Anthropic messages route under Uvicorn with a slow
CPU-only stub backend. It resets the client socket only after generation starts,
then submits a second request to prove that cancellation releases the model slot.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest


TOTAL_TOKENS = 400
STEP_SECONDS = 0.01


def _serve(backend_dir: str, port: int) -> None:
    sys.path.insert(0, backend_dir)

    import uvicorn
    from fastapi import FastAPI, Request

    from core.inference.api_monitor import ApiMonitor
    from models.inference import AnthropicMessagesRequest
    from routes import inference as inference_module

    # The route is declared inside this spawned function. Make the imported
    # annotations visible to FastAPI's postponed-annotation resolver.
    globals()["Request"] = Request
    globals()["AnthropicMessagesRequest"] = AnthropicMessagesRequest

    state = {"emitted": [], "started": 0}
    state_lock = threading.Lock()

    def _new_run() -> int:
        with state_lock:
            run_index = state["started"]
            state["started"] += 1
            state["emitted"].append(0)
            return run_index

    def _emit(kwargs, event):
        run_index = _new_run()
        cancel_event = kwargs["cancel_event"]
        for _ in range(TOTAL_TOKENS):
            if cancel_event.wait(STEP_SECONDS):
                return
            with state_lock:
                state["emitted"][run_index] += 1
            yield event

    def _generate_plain(**kwargs):
        cumulative = ""
        for _ in _emit(kwargs, None):
            cumulative += "x"
            yield cumulative
        if not kwargs["cancel_event"].is_set():
            yield {
                "type": "metadata",
                "usage": {"prompt_tokens": 2, "completion_tokens": TOTAL_TOKENS},
                "finish_reason": "length",
            }

    def _generate_tools(**kwargs):
        yield from _emit(kwargs, {"type": "content", "text": "x"})
        if not kwargs["cancel_event"].is_set():
            yield {
                "type": "metadata",
                "usage": {"prompt_tokens": 2, "completion_tokens": TOTAL_TOKENS},
                "finish_reason": "length",
            }

    backend = SimpleNamespace(
        is_loaded = True,
        is_vision = False,
        supports_tools = True,
        supports_tool_passthrough = False,
        model_identifier = "pr10096-test-model",
        context_length = 2048,
        count_chat_tokens = lambda *args, **kwargs: 2,
        generate_chat_completion = _generate_plain,
        generate_chat_completion_with_tools = _generate_tools,
        effective_parallel_slots = 1,
        base_url = "http://llama.pr10096.test:9999",
    )
    monitor = ApiMonitor(max_entries = 16)
    inference_module.current_date_prompt_line = lambda **kwargs: ""
    inference_module.get_llama_cpp_backend = lambda: backend
    inference_module.api_monitor = monitor

    app = FastAPI()

    @app.get("/healthz")
    async def _healthz():
        return {"ok": True}

    @app.get("/probe")
    async def _probe():
        with state_lock:
            emitted = list(state["emitted"])
            started = state["started"]
        return {
            "active": monitor.active_count(),
            "emitted": emitted,
            "entries": monitor.snapshot(),
            "started": started,
        }

    @app.post("/v1/messages")
    async def _messages(payload: AnthropicMessagesRequest, request: Request):
        return await inference_module.anthropic_messages(
            payload,
            request = request,
            current_subject = "pr10096-probe",
        )

    uvicorn.run(app, host = "127.0.0.1", port = port, log_level = "warning")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_json(url: str, predicate, timeout: float):
    import httpx

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout = 1.0)
            response.raise_for_status()
            last = response.json()
            if predicate(last):
                return last
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {url}; last={last!r}")


def _payload(use_tools: bool) -> dict:
    payload = {
        "max_tokens": TOTAL_TOKENS,
        "messages": [{"role": "user", "content": "write a long answer"}],
    }
    if use_tools:
        payload["tools"] = [
            {"name": "web_search", "input_schema": {"type": "object"}}
        ]
    return payload


@pytest.fixture
def live_server():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    port = _free_port()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target = _serve, args = (backend_dir, port), daemon = True)
    process.start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_json(f"{base_url}/healthz", lambda data: data.get("ok") is True, 30.0)
    try:
        yield base_url
    finally:
        process.terminate()
        process.join(5.0)
        if process.is_alive():
            process.kill()
            process.join(5.0)


@pytest.mark.parametrize("use_tools", [False, True], ids = ["plain", "tools"])
def test_connected_client_is_unchanged(live_server, use_tools):
    """Control: a connected caller still receives the complete 200 response."""
    import httpx

    response = httpx.post(
        f"{live_server}/v1/messages",
        json = _payload(use_tools),
        timeout = 15.0,
    )
    response.raise_for_status()
    result = _wait_json(
        f"{live_server}/probe",
        lambda data: data.get("active") == 0 and len(data.get("entries", [])) == 1,
        3.0,
    )
    print("CONTROL " + json.dumps(result, sort_keys = True))
    assert result["emitted"] == [TOTAL_TOKENS]
    assert result["entries"][0]["status"] == "completed"
    assert result["entries"][0]["stop_reason"] == "max_tokens"


@pytest.mark.parametrize("use_tools", [False, True], ids = ["plain", "tools"])
def test_tcp_reset_cancels_and_releases_the_queue(live_server, use_tools):
    """A mid-generation reset must stop work and promptly admit the next caller."""
    import httpx

    body = json.dumps(_payload(use_tools)).encode()
    port = int(live_server.rsplit(":", 1)[1])
    request_bytes = (
        "POST /v1/messages HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body

    client = socket.create_connection(("127.0.0.1", port), timeout = 3.0)
    client.sendall(request_bytes)
    _wait_json(
        f"{live_server}/probe",
        lambda data: data.get("started") == 1 and data.get("emitted", [0])[0] > 0,
        3.0,
    )
    reset_at = time.monotonic()
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()

    second_result: queue.Queue = queue.Queue()

    def _send_second_request():
        try:
            response = httpx.post(
                f"{live_server}/v1/messages",
                json = _payload(use_tools),
                timeout = 15.0,
            )
            second_result.put((response.status_code, None))
        except Exception as exc:
            second_result.put((None, type(exc).__name__))

    threading.Thread(target = _send_second_request, daemon = True).start()
    result = _wait_json(
        f"{live_server}/probe",
        lambda data: data.get("started", 0) >= 2,
        8.0,
    )
    queue_release_ms = round((time.monotonic() - reset_at) * 1000, 1)
    result["queue_release_ms"] = queue_release_ms
    print("RESET " + json.dumps(result, sort_keys = True))

    first_tokens = result["emitted"][0]
    cancelled = [entry for entry in result["entries"] if entry["status"] == "cancelled"]
    assert queue_release_ms < 2000, (
        f"the next request waited {queue_release_ms} ms after reset; "
        f"first generation emitted {first_tokens}/{TOTAL_TOKENS} tokens"
    )
    assert first_tokens < TOTAL_TOKENS, (
        f"generation continued to completion after reset: {first_tokens}/{TOTAL_TOKENS}"
    )
    assert cancelled, f"no cancelled monitor row: {result['entries']!r}"
    assert cancelled[0]["stop_reason"] is None

