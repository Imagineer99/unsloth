"""Real Metal small-model control plus safely intercepted low-budget A/B loads.

The VM opt-in is explicit and recorded. This is not a physical-Mac panic test.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--tree", type = Path, required = True)
parser.add_argument("--model", type = Path, required = True)
parser.add_argument("--mode", choices = ["before", "after", "control"], required = True)
parser.add_argument("--output", type = Path, required = True)
args = parser.parse_args()
assert platform.system() == "Darwin" and platform.machine() == "arm64", "Apple Silicon required"
sys.path.insert(0, str(args.tree.resolve() / "studio/backend"))
import mlx.core as mx

assert mx.metal.is_available(), "Metal unavailable"
mx.set_default_device(mx.gpu)
a = mx.ones((16, 16))
mx.eval(a @ a)
from core.inference.llama_cpp import (
    LlamaCppBackend,
    GgufLoadIntent,
    GpuMemoryShortError,
    _metal_device_is_paravirtual,
)

os.environ.pop("UNSLOTH_ALLOW_PARAVIRTUAL_METAL", None)
virtual = _metal_device_is_paravirtual()
# Both sides get exactly the same documented VM opt-in, with actual offload checked below.
if virtual:
    os.environ["UNSLOTH_ALLOW_PARAVIRTUAL_METAL"] = "1"
    print("INFO virtualized Metal: explicitly enabling UNSLOTH_ALLOW_PARAVIRTUAL_METAL=1")
_metal_device_is_paravirtual.cache_clear()
assert not _metal_device_is_paravirtual(), "VM opt-in did not enable the Metal path"
backend = LlamaCppBackend()
model = args.model.resolve()
intent = GgufLoadIntent(
    model_identifier = str(model),
    gguf_path = str(model),
    n_ctx = 512 if args.mode == "control" else 0,
    n_parallel = 1,
    cache_type_kv = "q8_0",
    speculative_type = "none",
    refuse_partial_gpu_fit = True,
)
facts = {
    "mode": args.mode,
    "commit": subprocess.check_output(
        ["git", "-C", str(args.tree), "rev-parse", "HEAD"], text = True
    ).strip(),
    "device": mx.device_info(),
    "virtualized_metal_opt_in": virtual,
    "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
}
try:
    if args.mode == "control":
        assert backend.load_model(intent), "Small-model control failed to load"
        # Metal has no enumerated GPU rows, so this field may be None.
        # Require the actual runtime offload count below instead.
        facts["studio_gpu_status"] = backend._gpu_offload_active
        log_path = Path(backend._llama_log_path)
        log_text = log_path.read_text(errors = "replace")
        assert "metal" in log_text.lower() and re.search(
            r"offloaded [1-9][0-9]*/[0-9]+ layers", log_text
        ), "Positive Metal offload evidence missing"
        chunks = list(
            backend.generate_chat_completion(
                [{"role": "user", "content": "Say hello."}], max_tokens = 16, temperature = 0, seed = 1
            )
        )
        assert any(isinstance(x, str) and x.strip() for x in chunks), "No generated text"
        facts["gpu_offload"] = True
        facts["completion"] = chunks[-1]
        facts["log_path"] = str(getattr(backend, "_llama_log_path", ""))
    else:
        # Use the actual model header and fitter; simulate less remaining memory than its weights.
        budget = max(1024**2, model.stat().st_size // 2)
        real_popen = subprocess.Popen
        facts["injected_budget_bytes"] = budget

        class UnsafeLaunchReached(BaseException):
            pass

        def intercept(cmd, *a, **kw):
            argv = [str(x) for x in cmd] if isinstance(cmd, (list, tuple)) else []
            if (
                argv
                and "llama-server" in Path(argv[0]).name
                and any(x in argv for x in ("-m", "--model"))
            ):
                facts["spawn_reached"] = True
                raise UnsafeLaunchReached()
            return real_popen(cmd, *a, **kw)

        facts["spawn_reached"] = False
        with (
            patch.object(
                LlamaCppBackend, "_apple_metal_memory_budget_bytes", staticmethod(lambda: budget)
            ),
            patch.object(subprocess, "Popen", intercept),
        ):
            try:
                backend.load_model(intent)
            except GpuMemoryShortError as exc:
                facts["shortfall_mib"] = exc.short_mib
                assert args.mode == "after" and exc.short_mib > 0 and not facts["spawn_reached"]
            except UnsafeLaunchReached:
                assert args.mode == "before", "Unsafe load still reached spawn"
            else:
                raise AssertionError("Neither measured refusal nor intercepted launch occurred")
finally:
    backend.unload_model()
args.output.parent.mkdir(parents = True, exist_ok = True)
args.output.write_text(json.dumps(facts, indent = 2, default = str))
print("PASS", args.mode, json.dumps(facts, default = str))
