# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

from __future__ import annotations

import ast
import pathlib
import sys
import threading


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "studio" / "backend"))


def check_route_policy_wiring() -> None:
    route = REPO_ROOT / "studio" / "backend" / "routes" / "inference.py"
    tree = ast.parse(route.read_text(encoding = "utf-8"))
    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"CodexToolPolicy", "ToolLoopPolicy"}:
            continue
        values.extend(
            ast.unparse(keyword.value)
            for keyword in node.keywords
            if keyword.arg == "nudge_tool_calls"
        )
    assert values == ["payload.nudge_tool_calls", "payload.nudge_tool_calls"], values


def check_codex_policy_forwarding() -> None:
    from core.inference import openai_codex_tool_loop as loop_mod

    captured: dict = {}

    def fake_loop(*_args, **kwargs):
        captured.update(kwargs)

        async def gen():
            yield "data: [DONE]\n\n"

        return gen()

    original = loop_mod.stream_with_studio_tools
    loop_mod.stream_with_studio_tools = fake_loop
    try:
        policy = loop_mod.CodexToolPolicy(
            tools = [],
            max_calls = 1,
            timeout = 1,
            permission_mode = "auto",
            confirm_calls = False,
            bypass_permissions = False,
            rag_scope = None,
            nudge_tool_calls = False,
        )
        loop_mod.stream_codex_with_studio_tools(
            object(),
            run = loop_mod.CodexRunContext(
                provider_id = "provider",
                thread_id = None,
                session_id = None,
                messages = [{"role": "user", "content": "hi"}],
                model = "gpt-5.6-sol",
                reasoning_effort = None,
            ),
            policy = policy,
            cancel_event = threading.Event(),
        )
    finally:
        loop_mod.stream_with_studio_tools = original
    assert captured["policy"].nudge_tool_calls is False


def _safetensors_reached_second_turn(*, request_flag: bool | None, process_default: bool) -> bool:
    from core.inference import passthrough_healing
    from core.inference.safetensors_agentic import run_safetensors_tool_loop

    turns = iter([["I'll search the web for that."], ["SECOND TURN"]])

    def single_turn(_messages):
        try:
            chunks = next(turns)
        except StopIteration:
            return
        text = ""
        for chunk in chunks:
            text += chunk
            yield text

    previous_default = passthrough_healing._NUDGE_DEFAULT
    passthrough_healing._NUDGE_DEFAULT = process_default
    try:
        events = list(
            run_safetensors_tool_loop(
                single_turn = single_turn,
                messages = [{"role": "user", "content": "search"}],
                tools = [{"type": "function", "function": {"name": "web_search"}}],
                execute_tool = lambda *_args, **_kwargs: "UNEXPECTED",
                nudge_tool_calls = request_flag,
            )
        )
    finally:
        passthrough_healing._NUDGE_DEFAULT = previous_default
    return any(
        event.get("type") == "content" and "SECOND TURN" in event.get("text", "")
        for event in events
    )


def check_safetensors_default_semantics() -> None:
    assert _safetensors_reached_second_turn(request_flag = None, process_default = True)
    assert not _safetensors_reached_second_turn(request_flag = False, process_default = True)
    assert not _safetensors_reached_second_turn(request_flag = None, process_default = False)


CHECKS = (
    ("route policy wiring", check_route_policy_wiring),
    ("Codex policy forwarding", check_codex_policy_forwarding),
    ("safetensors process default", check_safetensors_default_semantics),
)


def main() -> int:
    failures = 0
    for label, check in CHECKS:
        try:
            check()
        except Exception as exc:
            failures += 1
            print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
