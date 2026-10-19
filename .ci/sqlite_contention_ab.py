# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Check pinned SQLite contention behavior without launching Studio or a model."""

import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

BASE = "ca725b342b6ca1f6265d202fe3cf5efb1c00a692"
HEAD = "1b4e45be871fb6190a79a0246d9f968a273f0ebd"
TEST = "studio/backend/tests/test_chat_write_contention.py"
REGRESSION = "test_chat_writes_survive_a_lock_longer_than_five_seconds"
REPO = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", *args], cwd = REPO, text = True).strip()


def run_side(side, source, test, out, *, negative):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source / "studio/backend")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["UNSLOTH_STUDIO_HOME"] = str(out / "isolated-studio-home")
    report = out / f"{side}.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--noconftest",
        "-q",
        str(test),
        "--tb=short",
        f"--junitxml={report}",
    ]
    if negative:
        command += ["-k", REGRESSION]
    result = subprocess.run(command, cwd = out, env = env, text = True, capture_output = True, timeout = 120)
    print(f"\n{side}: source={BASE if negative else HEAD}, exit={result.returncode}", flush = True)
    print(result.stdout, flush = True)
    print(result.stderr, flush = True)
    if not report.is_file():
        raise AssertionError(f"{side}: missing JUnit report; setup failure is not A/B proof")
    cases = ET.parse(report).getroot().findall(".//testcase")
    errors = [case for case in cases if case.find("error") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    failures = [case for case in cases if case.find("failure") is not None]
    assert not errors and not skipped, f"{side}: setup errors or skipped tests"
    if negative:
        assert (
            result.returncode == 1 and len(cases) == 1 and len(failures) == 1
        ), "A must fail only its regression assertion"
        assert failures[0].get("name") == REGRESSION
        failure = failures[0].find("failure")
        detail = (failure.text or "") + failure.get("message", "")
        assert (
            "database is locked" in detail and "assert not failures" in detail
        ), "A failed for the wrong reason"
    else:
        assert (
            result.returncode == 0 and len(cases) == 5 and not failures
        ), "B must pass all five tests"
    return {
        "side": side,
        "source": BASE if negative else HEAD,
        "test_count": len(cases),
        "expected_failures": len(failures),
        "verified": True,
    }


def main():
    print(
        json.dumps(
            {
                "python": sys.version,
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
                "base": BASE,
                "head": HEAD,
            }
        ),
        flush = True,
    )
    for sha in (BASE, HEAD):
        assert git("rev-parse", f"{sha}^{{commit}}") == sha
    assert git("merge-base", BASE, HEAD) == BASE
    with tempfile.TemporaryDirectory(prefix = "studio-chat-ab-") as scratch:
        scratch = Path(scratch)
        roots = []
        try:
            for name, sha in (("a", BASE), ("b", HEAD)):
                source = scratch / f"source-{name}"
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(source), sha], cwd = REPO, check = True
                )
                roots.append(source)
                assert (
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd = source, text = True
                    ).strip()
                    == sha
                )
            test = scratch / "test_chat_write_contention.py"
            shutil.copyfile(roots[1] / TEST, test)
            before = scratch / "before"
            after = scratch / "after"
            before.mkdir()
            after.mkdir()
            facts = [
                run_side("A", roots[0], test, before, negative = True),
                run_side("B", roots[1], test, after, negative = False),
            ]
            print("A/B VERIFIED " + json.dumps(facts), flush = True)
        finally:
            for source in reversed(roots):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(source)], cwd = REPO, check = True
                )


if __name__ == "__main__":
    main()
