# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Exact-argument A/B probe shared by disposable BEFORE and AFTER branches."""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "studio" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

EXACT_ID = 9_007_199_254_740_993
EXACT_TEXT = '{"id":9007199254740993}'


def backend_probe() -> bool:
    try:
        from core.inference.tool_loop_controller import ToolLoopController

        tool = {"type": "function", "function": {"name": "delete_record"}}
        call = {
            "id": "call_precision",
            "type": "function",
            "function": {
                "name": "delete_record",
                "arguments": json.dumps({"id": EXACT_ID}),
            },
        }
        decision = ToolLoopController(tools=[tool]).prepare_call(call)
        payload = decision.tool_start_payload()
        displayed = payload.get("arguments_text")
        replayed = decision.as_assistant_tool_call()["function"]["arguments"]
        ok = displayed == EXACT_TEXT and replayed == EXACT_TEXT
        print(
            "BACKEND",
            "PASS" if ok else "FAIL",
            f"arguments_text={displayed!r}",
            f"replay={replayed!r}",
        )
        return ok
    except Exception:
        traceback.print_exc()
        return False


def browser_probe(browsers: list[str]) -> bool:
    command = [
        "node",
        "--experimental-strip-types",
        str(Path(__file__).with_name("pr10023_browser_probe.mjs")),
        *browsers,
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode == 0


def main() -> int:
    browsers = sys.argv[1:]
    if not browsers:
        raise SystemExit("provide at least one browser engine")
    backend_ok = backend_probe()
    browser_ok = browser_probe(browsers)
    print(f"A/B RESULT backend={backend_ok} browser={browser_ok}")
    return 0 if backend_ok and browser_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
