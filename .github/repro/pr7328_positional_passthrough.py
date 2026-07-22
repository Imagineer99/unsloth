#!/usr/bin/env python3
"""A/B probe for PR 7328's positional-model passthrough behavior."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import tempfile
import types
from pathlib import Path


AMBIGUOUS_ARG = "please/review"
FEATURE_MODEL = "unsloth/gemma-4-E2B-it-GGUF"


def _load_start(repo: Path):
    repo = repo.resolve()
    package = types.ModuleType("unsloth_cli")
    package.__path__ = [str(repo / "unsloth_cli")]
    commands = types.ModuleType("unsloth_cli.commands")
    commands.__path__ = [str(repo / "unsloth_cli" / "commands")]
    sys.modules["unsloth_cli"] = package
    sys.modules["unsloth_cli.commands"] = commands
    sys.path.insert(0, str(repo))
    return importlib.import_module("unsloth_cli.commands.start")


def _invoke(start, args: list[str]) -> tuple[str | None, list[str]]:
    from typer.testing import CliRunner

    captured: dict[str, object] = {}

    def fake_connect(api_key, model, load, **kwargs):
        captured["requested_model"] = model
        return (
            "http://127.0.0.1:8888",
            "ci-key",
            {"id": "already-loaded-model", "context_length": 8192},
        )

    def fake_run(base, model, env, command, **kwargs):
        captured["command"] = list(command)

    start._connect = fake_connect
    start._run = fake_run
    result = CliRunner().invoke(
        start.start_app,
        ["claude", "--no-launch", *args],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "command" in captured, f"Claude command was not built: {captured}"
    return captured.get("requested_model"), captured["command"]


def baseline(start) -> None:
    model, command = _invoke(start, [AMBIGUOUS_ARG])
    assert model is None
    assert AMBIGUOUS_ARG in command
    print(f"PASS base forwarded agent argument: {AMBIGUOUS_ARG}")


def controls(start) -> None:
    model, command = _invoke(start, [FEATURE_MODEL, "--continue"])
    assert model == FEATURE_MODEL
    assert FEATURE_MODEL not in command
    assert "--continue" in command

    model, command = _invoke(
        start,
        ["--model", "explicit/model", AMBIGUOUS_ARG],
    )
    assert model == "explicit/model"
    assert AMBIGUOUS_ARG in command

    model, command = _invoke(start, ["--profile", "owner/repo"])
    assert model is None
    assert command[-2:] == ["--profile", "owner/repo"]

    consume = start._consume_positional_model
    for path_arg in ("/models/local.gguf", "./relative.gguf", r"C:\models\local.gguf"):
        model, remaining = consume(None, [path_arg])
        assert model is None and remaining == [path_arg]

    with tempfile.TemporaryDirectory(prefix="pr7328-existing-") as raw:
        previous = Path.cwd()
        try:
            os.chdir(raw)
            Path("owner/repo").mkdir(parents=True)
            model, remaining = consume(None, ["owner/repo"])
            assert model is None and remaining == ["owner/repo"]
        finally:
            os.chdir(previous)

    print("PASS PR controls: model shorthand, explicit model, option values, and paths")


def regression(start) -> None:
    model, command = _invoke(start, [AMBIGUOUS_ARG])
    print(f"OBSERVED requested model: {model!r}")
    print(f"OBSERVED forwarded to Claude: {AMBIGUOUS_ARG in command}")
    assert model is None and AMBIGUOUS_ARG in command, (
        "PR 7328 positional compatibility regression reproduced: a leading "
        "owner/repo-shaped agent argument was consumed as the model"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("baseline", "controls", "regression"))
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    start = _load_start(args.repo)
    {"baseline": baseline, "controls": controls, "regression": regression}[args.mode](start)


if __name__ == "__main__":
    main()
