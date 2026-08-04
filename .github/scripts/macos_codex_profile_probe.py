#!/usr/bin/env python3
"""Disposable macOS probe for Unsloth's Codex profile routing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import tomllib

from unsloth_cli.commands import start


MODEL_ID = "unsloth/gemma-4-E2B-it-GGUF"
FALLBACK_MODEL = "gpt-5.6-sol"


def _combined_output(command: list[str], env: dict[str, str]) -> tuple[int | None, str]:
    kwargs: dict[str, object] = {"start_new_session": True}
    if os.name == "nt":
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    try:
        output, _ = process.communicate(timeout=10)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate(timeout=5)
        return None, output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("routed", "legacy-rejected"), required=True)
    args = parser.parse_args()

    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("PROBE FAIL: codex is not on PATH")

    version = subprocess.check_output([codex, "--version"], text=True).strip()
    print(f"PROBE codex_path={codex}")
    print(f"PROBE codex_version={version}")
    print(f"PROBE path={os.environ.get('PATH', '')}")

    with tempfile.TemporaryDirectory(prefix="unsloth-codex-macos-") as raw_home:
        home = Path(raw_home)
        (home / "config.toml").write_text(
            f'model = "{FALLBACK_MODEL}"\nmodel_reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        start.write_codex_config(
            "http://127.0.0.1:18765",
            {"id": MODEL_ID, "context_window": 131072},
            home,
        )

        base = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        profile = tomllib.loads(
            (home / "unsloth_api.config.toml").read_text(encoding="utf-8")
        )
        catalog_path = home / "model-catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        assert base["model"] == FALLBACK_MODEL
        assert base["oss_provider"] == "unsloth_api"
        assert profile["model"] == MODEL_ID
        assert profile["model_provider"] == "unsloth_api"
        assert profile["model_catalog_json"] == "model-catalog.json"
        assert catalog["models"][0]["slug"] == MODEL_ID

        print(f"PROBE codex_home={home}")
        print(f"PROBE base_model={base['model']}")
        print(f"PROBE profile_model={profile['model']}")
        print(f"PROBE catalog_slug={catalog['models'][0]['slug']}")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        env["UNSLOTH_STUDIO_AUTH_TOKEN"] = "repro-only-token"
        returncode, output = _combined_output(
            [
                codex,
                "--oss",
                "--profile",
                "unsloth_api",
                "exec",
                "--skip-git-repo-check",
                "Reply exactly: HELLO",
            ],
            env,
        )
        print("PROBE codex_output_begin")
        print(output.rstrip())
        print("PROBE codex_output_end")

        if args.expect == "legacy-rejected":
            assert returncode not in (None, 0)
            assert "config profile `unsloth_api` not found" in output
            print("PROBE PASS expected_legacy_profile_rejection")
            return 0

        assert f"model: {MODEL_ID}" in output
        assert "provider: unsloth_api" in output
        assert "Model metadata for `gpt-5.6-sol` not found" not in output
        assert "config profile `unsloth_api` not found" not in output
        print("PROBE PASS current_codex_routes_unsloth_profile")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
