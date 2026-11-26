"""Mark the actual Zoo speculative VLM prefill without changing the model or its events."""
import os
import sys
from pathlib import Path

marker = os.environ.get("PR19738_PREFILL")
if marker and hasattr(sys, "monitoring"):
    mon = sys.monitoring
    tool = 4
    mon.use_tool_id(tool, "PR19738 admission observer")

    def enter(code, offset):
        if code.co_qualname == "SpeculativeDraft.admit" and code.co_filename.endswith("/unsloth_zoo/mlx/speculative.py"):
            mon.set_local_events(tool, code, mon.events.PY_RETURN)
            Path(marker + ".start").touch()
            return None
        return mon.DISABLE

    def leave(code, offset, value):
        Path(marker + ".end").touch()

    mon.register_callback(tool, mon.events.PY_START, enter)
    mon.register_callback(tool, mon.events.PY_RETURN, leave)
    mon.set_events(tool, mon.events.PY_START)
