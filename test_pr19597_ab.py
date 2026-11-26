# SPDX-License-Identifier: AGPL-3.0-only
"""Directional A/B assertions for two immutable revisions. A pass confirms the predicted behavior."""
import json
import os
import sys
from pathlib import Path

from starlette.requests import Request

import routes.inference as routes
from core.inference import cloud_compaction, studio_tool_loop

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_external_tool_loop_compaction as loop
from test_cloud_summary_compaction import _bodies, _saved_thread, _SummarisingHandler
from test_external_auto_compaction import _proxy
from test_oversized_attachment_fit import FILE, note

SIDE = os.environ["SIDE"]
assert SIDE in ("base", "head")


def report(probe, **observed):
    print(json.dumps({"revision": SIDE, "probe": probe, **observed}))


def test_tool_sequence(monkeypatch):
    reply, runs, calls, _ = loop._run_task(monkeypatch, "summary", None)
    report("six-step", runs=runs, reply=reply, provider_calls=calls)
    if SIDE == "base":
        assert runs == [1, 2, 3, 4] and "LOST TASK" in reply
    else:
        assert runs == [1, 2, 3, 4, 5, 6] and reply.endswith("DONE 1,2,3,4,5,6")


def test_attachment_tail():
    messages = [
        {"role": "system", "content": "Answer in French."},
        {"role": "user", "content": "q0 " + "word " * 600},
        {"role": "assistant", "content": "a0 " + "word " * 600},
        {"role": "user", "content": note() + FILE},
    ]

    class Refused(_SummarisingHandler):
        overflow_chars = len(json.dumps(messages)) - 100

    chunks, _, chats = _bodies(Refused, messages=messages, context_overflow="truncate_oldest",
                               compaction_threshold=200_000, compaction_mode="rolling")
    assert all("error" not in c for c in chunks)
    assert len(chats) == 2
    sent = chats[1]["messages"]
    has_tail = "line 2000:" in json.dumps(sent)
    report("attachment", requests=len(chats), roles=[m["role"] for m in sent], line_2000=has_tail)
    assert has_tail == (SIDE == "base")


def test_saved_summary_retry(monkeypatch):
    thread = _saved_thread("replay-refusal")
    prefix = [{"role": "user", "content": "Earlier question"}, {"role": "assistant", "content": "Earlier answer"}]
    cloud_compaction.save_summary(thread, covered=prefix, summary="## Goal\nPrior questions", model="a-model", manual=False)

    async def connected(self):
        return False

    monkeypatch.setattr(Request, "is_disconnected", connected)
    monkeypatch.setattr(studio_tool_loop, "execute_tool", lambda *args, **kwargs: "RESULT " + "harbor ledger canal " * 150)
    recorded = []

    class Model(loop._RefusesPastALimit):
        def do_POST(self):
            self.server.recorded = recorded
            if len(recorded) != 4:
                return super().do_POST()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recorded.append(body)
            assert len(json.dumps(body["messages"])) <= loop._OVERFLOW_CHARS
            raw = loop._reply({"content": "DONE"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    overflow_fits = []
    original = routes._finish_external_compaction

    async def observe(plan, *args, **kwargs):
        result = await original(plan, *args, **kwargs)
        if plan.force == "overflow":
            overflow_fits.append(len(json.dumps(result[0])))
        return result

    monkeypatch.setattr(routes, "_finish_external_compaction", observe)
    chunks, _ = _proxy("openrouter", [*prefix, {"role": "user", "content": "Run four python steps."}],
                       handler=Model, thread_id=thread, enable_tools=True, enabled_tools=["python"],
                       permission_mode="off", context_overflow="truncate_oldest",
                       compaction_threshold=200_000, compaction_mode="summary")
    sizes = [len(json.dumps(b["messages"])) for b in recorded]
    errors = [c["error"] for c in chunks if "error" in c]
    done = any("DONE" in str(c.get("choices")) for c in chunks)
    limit = loop._OVERFLOW_CHARS
    report("saved-summary", sent_sizes=sizes, prepared_retry=overflow_fits, limit=limit, errors=errors, done=done)
    assert len(overflow_fits) == 1 and overflow_fits[0] <= limit and sizes[3] > limit
    if SIDE == "base":
        assert len(sizes) == 5 and sizes[-1] <= limit and done and not errors
    else:
        assert len(sizes) == 4 and not done and len(errors) == 1
        assert "context_length_exceeded" in str(errors[0])
