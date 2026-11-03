# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

BASE = "c62288858b3ca6f298b87bf1ccfeb362f7ec77ee"
HEAD = "a0769a3043aad228139220cd497172490a27d9b3"
PREFIX = "tests/test_pp_fsdp2_gloo.py::"
GEMMA = PREFIX + "test_pp2_x_tp2_matches_one_process[gemma3]"
UNFINISHED = [
    PREFIX + "test_pp2_x_tp2_matches_one_process[llama]",
    PREFIX + "test_pp_composition_order_and_meshes_are_refused_before_any_change",
]


def read_junit(path):
    rows = {}
    for case in ET.parse(path).iter("testcase"):
        name = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
        status = (
            "failed"
            if case.find("failure") is not None
            else (
                "error"
                if case.find("error") is not None
                else ("skipped" if case.find("skipped") is not None else "passed")
            )
        )
        rows[name] = status
    return rows


def judge(arms, selected, paired):
    needed = ("base", "head") if paired else ("head",)
    for arm in needed:
        row = arms.get(arm, {})
        results = row.get("results", {})
        if row.get("rc") not in (0, 1) or set(results) != set(selected):
            return "VOID"
        if any(v in ("error", "skipped") for v in results.values()):
            return "VOID"
    failures = {n for n, v in arms["head"]["results"].items() if v == "failed"}
    if not paired:
        return "FAIL_HEAD" if failures else "PASS"
    base_failures = {n for n, v in arms["base"]["results"].items() if v == "failed"}
    if failures - base_failures:
        return "REGRESSION"
    if failures:
        return "PRE_EXISTING"
    return "NO_REGRESSION"


def run(args):
    import torch
    import transformers
    import peft
    from transformers import AutoConfig

    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    assert not torch.cuda.is_available()
    selected = [GEMMA] if args.mode == "gemma" else UNFINISHED
    paired = args.mode == "gemma"
    result = dict(
        job = "mac_fsdp2_control",
        model = "random tiny config models",
        backend = "torch-gloo",
        device = "cpu",
        mode = args.mode,
        versions = {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "peft": peft.__version__,
        },
        host = {"system": platform.system(), "machine": platform.machine()},
        revisions = {"base": BASE, "head": HEAD},
        arms = {},
        checks = {},
        passed = False,
        error = None,
        skipped = False,
        steps = [],
        summary = {},
    )
    for repo in (
        "hf-internal-testing/tiny-random-LlamaForCausalLM",
        "trl-internal-testing/tiny-Qwen3ForCausalLM",
        "hf-internal-testing/tiny-random-Gemma3ForCausalLM",
    ):
        AutoConfig.from_pretrained(repo)
    env = dict(os.environ, HF_HUB_OFFLINE = "1", CUDA_VISIBLE_DEVICES = "", PYTHONUNBUFFERED = "1")
    out = Path(args.out).resolve()
    with tempfile.TemporaryDirectory(
        prefix = "fsdp2-control-", dir = os.environ.get("RUNNER_TEMP")
    ) as tmp:
        root = Path(tmp)
        repo = root / "repo"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--filter=blob:none",
                "--no-checkout",
                "https://github.com/unslothai/unsloth.git",
                str(repo),
            ],
            check = True,
        )
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin", BASE, HEAD], check = True)
        for arm in ("base", "head") if paired else ("head",):
            sha = BASE if arm == "base" else HEAD
            tree = root / arm
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree), sha],
                check = True,
            )
            actual = subprocess.check_output(
                ["git", "-C", str(tree), "rev-parse", "HEAD"], text = True
            ).strip()
            assert actual == sha
            evidence = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.path.insert(0,'tests');import test_pp_native_gloo as t;print(t.ppn.__file__)",
                ],
                cwd = tree,
                env = dict(env, PYTHONPATH = str(tree)),
                text = True,
            ).strip()
            assert str(tree) in evidence, evidence
            xml = root / f"{arm}.xml"
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-vv",
                "--tb=long",
                "--durations=0",
                "--junitxml=" + str(xml),
                *selected,
            ]
            started = time.monotonic()
            p = subprocess.run(
                cmd,
                cwd = tree,
                env = dict(env, PYTHONPATH = str(tree)),
                text = True,
                stdout = subprocess.PIPE,
                stderr = subprocess.STDOUT,
                timeout = 1200,
            )
            print(f"ARM {arm} {actual}\n{p.stdout}", flush = True)
            row = dict(
                sha = actual,
                source = evidence,
                rc = p.returncode,
                elapsed_s = round(time.monotonic() - started, 2),
                results = read_junit(xml) if xml.exists() else {},
                log = p.stdout,
            )
            result["arms"][arm] = row
            out.write_text(json.dumps(result, indent = 2), encoding = "utf-8")
    verdict = judge(result["arms"], selected, paired)
    result["summary"]["verdict"] = verdict
    result["passed"] = verdict in ("PASS", "NO_REGRESSION", "PRE_EXISTING")
    result["checks"]["complete_differential"] = dict(ok = result["passed"], detail = verdict)
    out.write_text(json.dumps(result, indent = 2), encoding = "utf-8")
    print("JOB_RESULT", json.dumps(result), flush = True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices = ("gemma", "unfinished"), required = True)
    parser.add_argument("--out", required = True)
    raise SystemExit(run(parser.parse_args()))
