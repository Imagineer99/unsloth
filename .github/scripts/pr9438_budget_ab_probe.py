# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Pinned A/B probe for PR #9438's conversation-search budget."""

from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
import sys
import threading


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "studio" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from core.inference.safetensors_agentic import run_safetensors_tool_loop  # noqa: E402


MESSAGES = [
    {"role": "user", "content": "recall the deployment steps. " * 420},
    {"role": "user", "content": "what was the code from earlier"},
]
TOOLS = [{"type": "function", "function": {"name": "search_conversation"}}]
SUPPORTS_GENERATION_USAGE = "generation_stats_holder" in inspect.signature(
    run_safetensors_tool_loop
).parameters


def _model(stats_holder: dict, reported_prompt_tokens: int | None):
    state = {"turn": 0}

    def single_turn(_messages):
        stats_holder["stats"] = None
        state["turn"] += 1
        if state["turn"] > 1:
            yield "done"
            return
        call = (
            "thinking. "
            '<tool_call>{"name":"search_conversation","arguments":'
            '{"query":"the code"}}</tool_call>'
        )
        cumulative = ""
        for char in call:
            cumulative += char
            yield cumulative
        if reported_prompt_tokens is not None:
            stats_holder["stats"] = {
                "usage": {"prompt_tokens": reported_prompt_tokens}
            }

    return single_turn


def budget_after(reported_prompt_tokens: int | None) -> int:
    stats_holder: dict = {}
    seen: dict = {}
    kwargs = {
        "single_turn": _model(stats_holder, reported_prompt_tokens),
        "messages": list(MESSAGES),
        "tools": TOOLS,
        "execute_tool": lambda _name, _arguments, **extra: (
            seen.update(extra) or "an earlier turn"
        ),
        "cancel_event": threading.Event(),
        "max_tool_iterations": 2,
        "thread_id": "pr9438-ab",
        "context_length": 8192,
        "max_tokens": 512,
    }
    if SUPPORTS_GENERATION_USAGE:
        kwargs["generation_stats_holder"] = stats_holder
    list(run_safetensors_tool_loop(**kwargs))
    return seen["conversation_budget_tokens"]


reported_budget = budget_after(6_000)
fallback_budget = budget_after(None)
reported_values = [5_000 + (index % 10) * 100 for index in range(100)]
serial = [budget_after(value) for value in reported_values]
with ThreadPoolExecutor(max_workers = 16) as pool:
    parallel = list(pool.map(budget_after, reported_values))

mismatches = sum(left != right for left, right in zip(serial, parallel))
distinct_budgets = len(set(serial))

print(f"SUPPORTS_GENERATION_USAGE={SUPPORTS_GENERATION_USAGE}", flush = True)
print("REPORTED_PROMPT_TOKENS=6000", flush = True)
print(f"REPORTED_USAGE_BUDGET={reported_budget}", flush = True)
print(f"NO_USAGE_FALLBACK_BUDGET={fallback_budget}", flush = True)
print(f"CONCURRENT_REQUESTS={len(reported_values)}", flush = True)
print(f"CONCURRENCY_MISMATCHES={mismatches}", flush = True)
print(f"DISTINCT_REPORTED_BUDGETS={distinct_budgets}", flush = True)

assert reported_budget < fallback_budget, (
    "reported generation usage had no effect on the conversation-search budget"
)
assert 0 < reported_budget < 1_800, (
    f"reported prompt usage left an unsafe budget: {reported_budget}"
)
assert fallback_budget > 0, "missing usage disabled conversation search"
assert mismatches == 0, "concurrent requests did not match serial results"
assert distinct_budgets >= 10, "request-local prompt usage was not reflected in budgets"

print("PR9438_AB_PASS", flush = True)
