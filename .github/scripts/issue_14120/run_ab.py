"""Run identical regression tests on pinned sources; reject unrelated BEFORE failures."""

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

BASE = "2fee36ffa352bfb36f331d92bb81e7ee0c408acb"
HEAD = "f30f104da391f58762a93014063e276466437966"
root = Path.cwd()
output = root / "issue-14120-results"
output.mkdir(exist_ok = True)
tests = ["test_metal_explicit_context_guard.py", "test_multi_model_serving.py"]
for side, sha in [("before", BASE), ("after", HEAD)]:
    tree = root / (".issue-14120-" + side)
    if not tree.exists():
        subprocess.run(["git", "worktree", "add", "--detach", str(tree), sha], check = True)
    actual = subprocess.check_output(
        ["git", "-C", str(tree), "rev-parse", "HEAD"], text = True
    ).strip()
    assert actual == sha, (actual, sha)
    for test in tests:
        contents = subprocess.check_output(["git", "show", HEAD + ":studio/backend/tests/" + test])
        (tree / "studio/backend/tests" / test).write_bytes(contents)
    env = dict(
        os.environ,
        PYTHONPATH = str(tree / "studio/backend"),
        UNSLOTH_STUDIO_HOME = str(output / (side + "-home")),
        UNSLOTH_ALLOW_PARAVIRTUAL_METAL = "1",
    )
    report = output / (side + ".xml")
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml=" + str(report),
        *["studio/backend/tests/" + t for t in tests],
        "-k",
        "MetalAlongsideAdmission or metal_shortfall_evicts_then_rechecks_shared_memory",
    ]
    with (output / (side + ".log")).open("w") as log:
        run = subprocess.run(
            args, cwd = tree, env = env, stdout = log, stderr = subprocess.STDOUT, timeout = 180
        )
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 11 and not any(
        c.find("error") is not None or c.find("skipped") is not None for c in cases
    ), "Setup/collection failure"
    failures = [c for c in cases if c.find("failure") is not None]
    if side == "before":
        expected = {
            "test_weights_over_remaining_memory_never_spawn[9-8-0]",
            "test_weights_over_remaining_memory_never_spawn[9-8-32768]",
            "test_weights_over_remaining_memory_never_spawn[32-8-0]",
            "test_weights_over_remaining_memory_never_spawn[32-8-32768]",
            "test_minimum_context_must_fit_too",
            "test_retry_rechecks_memory_after_eviction",
            "test_metal_shortfall_evicts_then_rechecks_shared_memory",
        }
        assert run.returncode == 1 and {c.attrib["name"] for c in failures} == expected
        for case in failures:
            failure = case.find("failure")
            text = failure.attrib.get("message", "") + (failure.text or "")
            marker = (
                "assert [9] == [9, 32]"
                if case.attrib["name"].startswith("test_metal_shortfall")
                else "DID NOT RAISE"
            )
            assert marker in text, (case.attrib["name"], text)
    else:
        assert run.returncode == 0 and not failures, "Fix regression failed"
    print(
        f"PASS {side} {sha}: {len(failures)} expected failures, {len(cases)-len(failures)} passes",
        flush = True,
    )
