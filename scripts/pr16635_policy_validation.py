"""Pinned native A/B proof for permission-filtered idle-server model selection."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv
import xml.etree.ElementTree as ET

from pr16635_validation import isolated_env, run

BASE = "630179686e4e3de8c1d3aee9acd99f1b34a62037"
HEAD = "6939421353cc868ff0a3334dcf6e1330173a2c03"
SCRIPT = Path(__file__).resolve()


def validate(args):
    root = Path(args.temp).resolve()
    root.mkdir(parents=True, exist_ok=True)
    after = Path(args.after).resolve()
    probe = root / "test_policy_probe.py"
    text = (after / "unsloth_cli/tests/test_start_picker_policy.py").read_text(encoding="utf-8")
    text += '''
import os
from pathlib import Path

@pytest.fixture(autouse=True)
def assert_tested_checkout():
    assert Path(start.__file__).resolve().is_relative_to(Path(os.environ["EXPECTED_SOURCE"]))
'''
    probe.write_text(text, encoding="utf-8")
    for side, directory, sha in (("before", args.before, BASE), ("after", args.after, HEAD)):
        source = Path(directory).resolve()
        assert run(["git", "rev-parse", "HEAD"], cwd=source).strip() == sha
        print(f"REVISION {side} {sha} platform={sys.platform}", flush=True)
        env = isolated_env(source, root / side / "home")
        env["EXPECTED_SOURCE"] = str(source)
        report = root / side / "account.xml"
        command = [sys.executable, "-m", "pytest", "-c", str(source / "pyproject.toml"),
                   str(probe), "-q", "--tb=short", "-o", f"cache_dir={root / side / 'cache'}",
                   "--basetemp", str(root / side / "data"), "--junitxml", str(report)]
        if side == "before":
            command += [str(source / "unsloth_cli/tests/test_start_picker_account.py") + "::test_idle_picker_still_lists_owner_models", "-k", "managed_picker_excludes or still_lists_owner_models"]
        result = subprocess.run(command, cwd=source, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=90)
        detail = result.stdout + result.stderr
        assert report.exists(), detail
        cases = list(ET.parse(report).iter("testcase"))
        assert len(cases) == (5 if side == "before" else 7), detail
        assert not any(case.find("error") is not None or case.find("skipped") is not None
                       for case in cases), detail
        failed = {case.get("name"): case.find("failure").text or ""
                  for case in cases if case.find("failure") is not None}
        if side == "before":
            assert result.returncode == 1 and set(failed) == {
                f"test_managed_picker_excludes_forbidden_cached_models[policy_catalog{i}]"
                for i in range(4)
            }, detail
            for i in range(4):
                failure = failed[f"test_managed_picker_excludes_forbidden_cached_models[policy_catalog{i}]"]
                assert "Denied" in failure and ("HTTPError 403" if i < 2 else "HTTPError 404") in failure, detail
            print("NEGATIVE PASS: 4 expected authorization failures across cached GGUF and "
                  "ordinary models; owner control passes; no setup errors or skips.", flush=True)
        else:
            assert result.returncode == 0 and not failed, detail
            print("POSITIVE PASS: all 7 cache-policy probes pass, including unavailable access checks and "
                  "empty permitted inventories.", flush=True)
            guards = run([sys.executable, "-m", "pytest", "-c", str(source / "pyproject.toml"),
                          "unsloth_cli/tests/test_start_model_picker.py",
                          "unsloth_cli/tests/test_start_model_picker_regressions.py",
                          "unsloth_cli/tests/test_start_picker_variant.py",
                          "unsloth_cli/tests/test_start_picker_account.py",
                          "studio/backend/tests/test_auth_account_endpoint.py",
                          "studio/backend/tests/test_api_key_account_scope.py",
                          "studio/backend/tests/test_account_policy_cache_window.py", "-q",
                          "-o", f"cache_dir={root / side / 'guard-cache'}", "--basetemp",
                          str(root / side / "guard-data")], cwd=source, env=env, timeout=120)
            print(guards, flush=True)
    print("A/B PASS: managed pickers omit forbidden cached models before numbering/default selection; "
          "earlier fixes, owner behavior, legacy fallback and real API-key authorization pass.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bootstrap", "validate"))
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--temp", default="temp/pr16635-policy")
    args = parser.parse_args()
    if args.mode == "validate":
        return validate(args)
    env_root = Path(args.temp).resolve() / "venv"
    venv.EnvBuilder(with_pip=True).create(env_root)
    python = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    print(run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--only-binary=:all:", "-r", str(SCRIPT.with_name("pr16635_account_requirements.txt"))],
              timeout=240), flush=True)
    return subprocess.call([str(python), str(SCRIPT), "validate", "--before", args.before,
                            "--after", args.after, "--temp", args.temp])


if __name__ == "__main__":
    raise SystemExit(main())
