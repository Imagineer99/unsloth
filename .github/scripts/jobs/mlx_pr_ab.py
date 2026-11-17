"""Apple Silicon base-vs-head probe for an unsloth PR on a staging leg.

Run from the staged checkout (upstream main + PR). Base = staging `upstream-main` (fallback --base SHA)
in a worktree; the head's test files are copied over it, so both arms run the same tests on their own code.
Per arm: pytest (junit ids), a notebook-style GRPO run through `from trl import`, and `import unsloth` time.
Exit 0 when head passes everything and base fails at least one --expect-fail test; else 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

GRPO_PROBE = r"""
import json, sys, unsloth
from unsloth import FastLanguageModel
out = {"unsloth_file": unsloth.__file__, "device": unsloth.DEVICE_TYPE}
try:
    from unsloth import check_python_modules, create_locked_down_function, execute_with_time_limit
    from unsloth import Benchmarker, is_port_open, launch_openenv, to_sharegpt
    from unsloth import FastLlamaModel, FastQwen2Model, FastQwen3Model, FastMistralModel
    out["imports"] = "ok"
except Exception as exc:
    out["imports"] = f"{type(exc).__name__}: {exc}"
import trl
for stem in ("KTO", "CPO", "SFT"):
    try:
        out[f"{stem}_loss_type"] = getattr(trl, f"{stem}Config")(output_dir="o").loss_type
    except Exception as exc:
        out[f"{stem}_loss_type"] = f"{type(exc).__name__}: {exc}"
try:
    from datasets import Dataset
    from trl import GRPOConfig, GRPOTrainer
    cfg = GRPOConfig(output_dir="grpo_out", num_generations=2, per_device_train_batch_size=2,
                     max_completion_length=8, max_steps=2, learning_rate=1e-5, logging_steps=1,
                     report_to="none")
    out["GRPO_loss_type"] = cfg.loss_type
    model, tok = FastLanguageModel.from_pretrained("mlx-community/SmolLM-135M-Instruct-4bit",
                                                   max_seq_length=128, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(model, r=8, lora_alpha=16,
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    data = Dataset.from_list([{"prompt": f"Count to {i}:"} for i in range(4)])
    def reward(completions, **kw):
        return [float(len(str(c)) % 5) for c in completions]
    trainer = GRPOTrainer(model=model, reward_funcs=[reward], args=cfg, train_dataset=data, processing_class=tok)
    trainer.train()
    out["GRPO_train"] = "ok"
    out["GRPO_trainer_loss_type"] = trainer.args.loss_type
except Exception as exc:
    out["GRPO_train"] = f"{type(exc).__name__}: {str(exc)[:500]}"
print("PROBE_RESULT " + json.dumps(out))
"""


def sh(
    cmd,
    cwd,
    env = None,
    timeout = 1800,
):
    p = subprocess.run(cmd, cwd = cwd, env = env, capture_output = True, text = True, timeout = timeout)
    return p.returncode, p.stdout, p.stderr


def make_base(head, base_sha):
    base = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "base_tree"
    if base.exists():
        shutil.rmtree(base)
    ref = None
    for src, rev in (
        ("origin", "upstream-main"),
        ("https://github.com/unslothai/unsloth.git", base_sha),
    ):
        if rev and sh(["git", "fetch", "--depth", "1", src, rev], head)[0] == 0:
            ref = sh(["git", "rev-parse", "FETCH_HEAD"], head)[1].strip()
            break
    if not ref:
        raise SystemExit("could not fetch a base ref")
    rc, so, se = sh(["git", "worktree", "add", "--detach", str(base), ref], head)
    if rc:
        raise SystemExit(f"worktree add failed: {se}")
    return base, ref


def run_arm(tree, tests, env):
    res = {}
    rc, so, se = sh([sys.executable, "-c", "import unsloth; print(unsloth.__file__)"], tree, env)
    res["unsloth_file"] = (so.strip().splitlines() or [""])[-1]
    res["imports_own_tree"] = res["unsloth_file"].startswith(str(tree))
    junit = tree / "junit.xml"
    rc, so, se = sh(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *tests,
        ],
        tree,
        env,
        timeout = 3600,
    )
    res["pytest_rc"] = rc
    res["pytest_tail"] = (so + se)[-3000:]
    cases = {}
    if junit.exists():
        for tc in ET.parse(junit).getroot().iter("testcase"):
            tid = f"{tc.get('classname')}::{tc.get('name')}"
            state = "passed"
            for tag in ("failure", "error", "skipped"):
                if tc.find(tag) is not None:
                    state = tag
            cases[tid] = state
    res["cases"] = cases
    rc, so, se = sh([sys.executable, "-c", GRPO_PROBE], tree, env, timeout = 3600)
    m = re.search(r"^PROBE_RESULT (.*)$", so, re.M)
    res["probe"] = json.loads(m.group(1)) if m else {"error": (so + se)[-3000:]}
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        rc, so, se = sh([sys.executable, "-X", "importtime", "-c", "import unsloth"], tree, env)
        times.append(round(time.perf_counter() - t0, 3))
        res["imports_requests"] = bool(re.search(r"\|\s+requests$", se, re.M))
    res["import_s"] = times
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default = None)
    ap.add_argument("--tests", nargs = "+", required = True)
    ap.add_argument("--expect-fail", nargs = "*", default = [])
    ap.add_argument("--pip", default = "trl datasets pytest")
    ap.add_argument("--out", default = "job_mlx_pr_ab.json")
    a = ap.parse_args()
    head = Path.cwd().resolve()
    out = {"job": "mlx_pr_ab", "finished": False}
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *a.pip.split()], check = True)
    base, ref = make_base(head, a.base)
    out["base_ref"] = ref
    out["head_ref"] = sh(["git", "rev-parse", "HEAD"], head)[1].strip()
    for t in a.tests:
        (base / t).parent.mkdir(parents = True, exist_ok = True)
        shutil.copy2(head / t, base / t)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("UNSLOTH_IS_PRESENT", "UNSLOTH_COMPILE_DISABLE", "UNSLOTH_FORCE_GPU_PATH")
    }
    arms = {}
    for name, tree in (("base", base), ("head", head)):
        arms[name] = run_arm(tree, a.tests, {**env, "PYTHONPATH": str(tree)})
        print(
            name,
            json.dumps({k: v for k, v in arms[name].items() if k != "pytest_tail"}, indent = 1),
            flush = True,
        )
        print(arms[name]["pytest_tail"], flush = True)
    out["arms"] = arms
    head_ok = (
        all(arms[n]["imports_own_tree"] for n in arms)
        and arms["head"]["pytest_rc"] == 0
        and arms["head"]["cases"]
        and not any(s in ("failure", "error", "skipped") for s in arms["head"]["cases"].values())
        and arms["head"]["probe"].get("GRPO_train") == "ok"
    )
    base_fails = [
        t
        for t, s in arms["base"]["cases"].items()
        if s in ("failure", "error") and any(e in t for e in a.expect_fail)
    ]
    out["base_failing_expected"] = base_fails
    out["passed"] = bool(head_ok and (base_fails or not a.expect_fail))
    out["finished"] = True
    Path(a.out).write_text(json.dumps(out, indent = 1))
    print(
        "JOB_RESULT " + json.dumps({"passed": out["passed"], "base_failing_expected": base_fails})
    )
    sys.exit(0 if out["passed"] else 1)


if __name__ == "__main__":
    main()
