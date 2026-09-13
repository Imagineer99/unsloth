"""Standalone A/B semantic probe for Unsloth PR #9595.

Run in a fresh process and point --backend at exactly one checkout's
studio/backend directory.  The expected oracle is the proposed fixed behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    backend = Path(args.backend).resolve()
    sys.path.insert(0, str(backend))

    from models.inference import (
        AnthropicMessagesRequest,
        ChatCompletionRequest,
        ChatMessage,
        ResponsesRequest,
    )
    from routes import inference as route
    from utils.inference import inference_config as config

    results: list[dict[str, object]] = []

    def check(name, operation):
        try:
            detail = operation()
        except Exception as exc:  # each case must report independently
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            results.append({"name": name, "ok": True, "detail": detail})

    def assert_equal(actual, expected):
        assert actual == expected, f"actual={actual!r} expected={expected!r}"
        return actual

    def all_omitted():
        return {field: None for field in config.SAMPLING_FIELD_NAMES}

    def route_capture(payload):
        class StopAfterSampling(Exception):
            pass

        async def no_auto_switch(*_args, **_kwargs):
            return None

        llama_backend = SimpleNamespace(
            is_loaded=True,
            model_identifier="unsloth/Qwen3.8-27B-GGUF",
            _is_audio=False,
        )
        request = SimpleNamespace(
            state=SimpleNamespace(skip_api_monitor=True),
            url=SimpleNamespace(path="/v1/chat/completions"),
            method="POST",
            scope={},
        )
        captured = {}
        real_fill = route._fill_recommended_sampling_openai
        originals = {
            "_automatic_model_load_may_run": route._automatic_model_load_may_run,
            "_maybe_auto_switch_model": route._maybe_auto_switch_model,
            "get_llama_cpp_backend": route.get_llama_cpp_backend,
            "_fill_recommended_sampling_openai": route._fill_recommended_sampling_openai,
        }

        def capture_after_sampling(route_payload, model_id):
            real_fill(route_payload, model_id)
            captured.update(
                enable_thinking=route_payload.enable_thinking,
                reasoning_effort=route_payload.reasoning_effort,
                preserve_thinking=route_payload.preserve_thinking,
                temperature=route_payload.temperature,
                top_p=route_payload.top_p,
                presence_penalty=route_payload.presence_penalty,
            )
            raise StopAfterSampling

        route._automatic_model_load_may_run = lambda: False
        route._maybe_auto_switch_model = no_auto_switch
        route.get_llama_cpp_backend = lambda: llama_backend
        route._fill_recommended_sampling_openai = capture_after_sampling
        try:
            try:
                asyncio.run(route.openai_chat_completions(payload, request, "probe-user"))
            except StopAfterSampling:
                pass
        finally:
            for name, value in originals.items():
                setattr(route, name, value)
        if not captured:
            raise AssertionError("chat route did not reach sampling")
        return captured

    assert Path(config.__file__).resolve().is_relative_to(backend)
    assert Path(route.__file__).resolve().is_relative_to(backend)

    check(
        "thinking_sampling_row",
        lambda: assert_equal(
            {
                key: config.resolve_effective_sampling(
                    "unsloth/Qwen3.8-27B-GGUF",
                    all_omitted(),
                    thinking_mode=True,
                )[key]
                for key in ("temperature", "top_p", "top_k", "min_p", "presence_penalty")
            },
            {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
            },
        ),
    )
    check(
        "non_thinking_sampling_row",
        lambda: assert_equal(
            {
                key: config.resolve_effective_sampling(
                    "unsloth/Qwen3.8-27B-GGUF",
                    all_omitted(),
                    thinking_mode=False,
                )[key]
                for key in ("temperature", "top_p", "top_k", "min_p", "presence_penalty")
            },
            {
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
            },
        ),
    )
    check(
        "absent_mode_is_backward_compatible",
        lambda: assert_equal(
            config.load_inference_config("unsloth/Qwen3.8-27B-GGUF"),
            config.load_inference_config("unsloth/Qwen3.8-27B-GGUF", thinking_mode=None),
        ),
    )
    check(
        "cache_separates_reasoning_modes",
        lambda: assert_equal(
            (
                config._recommended_sampling("unsloth/Qwen3.8-27B-GGUF", True)["temperature"],
                config._recommended_sampling("unsloth/Qwen3.8-27B-GGUF", False)["temperature"],
            ),
            (1.0, 0.7),
        ),
    )
    check(
        "explicit_client_value_beats_mode_row",
        lambda: assert_equal(
            config.resolve_effective_sampling(
                "unsloth/Qwen3.8-27B-GGUF",
                {**all_omitted(), "temperature": 0.2},
                thinking_mode=True,
            )["temperature"],
            0.2,
        ),
    )

    def make_chat(**kwargs):
        return ChatCompletionRequest(
            model="local-model",
            messages=[{"role": "user", "content": "hi"}],
            **kwargs,
        )

    expected_routes = [
        (
            "nested_enable_true",
            {"chat_template_kwargs": {"enable_thinking": True}},
            {
                "enable_thinking": True,
                "reasoning_effort": None,
                "preserve_thinking": None,
                "temperature": 1.0,
                "top_p": 0.95,
                "presence_penalty": 0.0,
            },
        ),
        (
            "nested_enable_false",
            {"chat_template_kwargs": {"enable_thinking": False}},
            {
                "enable_thinking": False,
                "reasoning_effort": None,
                "preserve_thinking": None,
                "temperature": 0.7,
                "top_p": 0.8,
                "presence_penalty": 1.5,
            },
        ),
        (
            "invalid_string_boolean_is_ignored",
            {"chat_template_kwargs": {"enable_thinking": "false"}},
            {
                "enable_thinking": None,
                "reasoning_effort": None,
                "preserve_thinking": None,
                "temperature": 0.7,
                "top_p": 0.8,
                "presence_penalty": 1.5,
            },
        ),
        (
            "nested_effort_none",
            {"chat_template_kwargs": {"reasoning_effort": "none"}},
            {
                "enable_thinking": False,
                "reasoning_effort": "none",
                "preserve_thinking": None,
                "temperature": 0.7,
                "top_p": 0.8,
                "presence_penalty": 1.5,
            },
        ),
        (
            "nested_effort_xhigh",
            {"chat_template_kwargs": {"reasoning_effort": "xhigh"}},
            {
                "enable_thinking": True,
                "reasoning_effort": "xhigh",
                "preserve_thinking": None,
                "temperature": 1.0,
                "top_p": 0.95,
                "presence_penalty": 0.0,
            },
        ),
        (
            "typed_boolean_beats_contradictory_effort",
            {"enable_thinking": True, "reasoning_effort": "none"},
            {
                "enable_thinking": True,
                "reasoning_effort": None,
                "preserve_thinking": None,
                "temperature": 1.0,
                "top_p": 0.95,
                "presence_penalty": 0.0,
            },
        ),
    ]
    for name, kwargs, expected in expected_routes:
        check(name, lambda kwargs=kwargs, expected=expected: assert_equal(route_capture(make_chat(**kwargs)), expected))

    def null_and_omitted():
        body = {
            "thinking": {"type": "disabled"},
            "chat_template_kwargs": {"reasoning_effort": "high"},
        }
        with_null = route_capture(make_chat(**body, enable_thinking=None))
        omitted = route_capture(make_chat(**body))
        expected = {
            "enable_thinking": True,
            "reasoning_effort": "high",
            "preserve_thinking": None,
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 0.0,
        }
        assert_equal(with_null, expected)
        assert_equal(omitted, expected)
        return {"with_null": with_null, "omitted": omitted}

    check("explicit_null_matches_omitted", null_and_omitted)

    def fields_set_truthful():
        request = make_chat(thinking={"type": "disabled"}, enable_thinking=None)
        assert request.enable_thinking is False
        assert "enable_thinking" not in request.model_fields_set
        return sorted(request.model_fields_set)

    check("derived_boolean_not_marked_explicit", fields_set_truthful)

    def normalize_idempotent():
        request = make_chat(
            chat_template_kwargs={
                "enable_thinking": True,
                "reasoning_effort": "medium",
                "preserve_thinking": True,
            }
        )
        route._normalize_chat_reasoning_controls(request)
        first = (request.enable_thinking, request.reasoning_effort, request.preserve_thinking)
        route._normalize_chat_reasoning_controls(request)
        second = (request.enable_thinking, request.reasoning_effort, request.preserve_thinking)
        return assert_equal((first, second), ((True, "medium", True), (True, "medium", True)))

    check("normalization_is_idempotent", normalize_idempotent)

    def responses_string_boolean():
        request = route._build_chat_request(
            ResponsesRequest(input="hi", chat_template_kwargs={"enable_thinking": "false"}),
            [ChatMessage(role="user", content="hi")],
            stream=False,
        )
        return assert_equal(request.enable_thinking, None)

    check("responses_invalid_string_boolean_is_ignored", responses_string_boolean)

    def responses_nested_controls():
        request = route._build_chat_request(
            ResponsesRequest(
                input="hi",
                chat_template_kwargs={"reasoning_effort": "none", "preserve_thinking": True},
            ),
            [ChatMessage(role="user", content="hi")],
            stream=True,
        )
        return assert_equal(
            (request.enable_thinking, request.reasoning_effort, request.preserve_thinking),
            (False, "none", True),
        )

    check("responses_forwards_nested_controls", responses_nested_controls)

    def responses_native_precedence():
        request = route._build_chat_request(
            ResponsesRequest(
                input="hi",
                reasoning={"effort": "high"},
                chat_template_kwargs={"reasoning_effort": "none"},
            ),
            [ChatMessage(role="user", content="hi")],
            stream=False,
        )
        return assert_equal((request.enable_thinking, request.reasoning_effort), (True, "high"))

    check("responses_native_effort_wins", responses_native_precedence)

    def anthropic_exact_sentinel():
        observed = {}
        for value in ("disabled", "DISABLED", "Disabled", "adaptive", "enabled"):
            request = AnthropicMessagesRequest.model_validate(
                {
                    "model": "unsloth/Qwen3.8-27B-GGUF",
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {"type": value},
                }
            )
            observed[value] = (
                route._normalized_sampling_thinking_mode(request),
                request.resolved_enable_thinking(),
            )
        assert all(a == b for a, b in observed.values()), observed
        return observed

    check("anthropic_sampling_matches_generation", anthropic_exact_sentinel)

    failed = [result for result in results if not result["ok"]]
    report = {"backend": str(backend), "results": results}
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, indent=2, default=str))
    print(f"SUMMARY passed={len(results) - len(failed)} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
