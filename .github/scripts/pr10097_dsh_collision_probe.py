# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Fail unless Unsloth rejects Ubuntu's unrelated Distributed Shell binary."""

import os
import subprocess
from pathlib import Path

import click
import typer

from studio.backend.utils import coding_agents
from unsloth_cli.commands import start


DISTRO_DSH = Path("/usr/bin/dsh")


def main() -> None:
    if not DISTRO_DSH.is_file():
        raise AssertionError("the Ubuntu dsh package did not install /usr/bin/dsh")

    version = subprocess.run(
        [str(DISTRO_DSH), "--version"],
        check = False,
        capture_output = True,
        text = True,
    )
    identity = (version.stdout + version.stderr).strip().replace("\n", " | ")
    print(f"DISTRO_DSH={DISTRO_DSH}")
    print(f"DISTRO_DSH_IDENTITY={identity}")

    # Make the distro package the only dsh candidate. Do not allow the launcher
    # to install anything if it correctly rejects that candidate.
    os.environ["PATH"] = "/usr/bin:/bin"
    start._managed_node_tools = lambda: None
    start._install_agent = lambda *_args, **_kwargs: None

    detected = coding_agents.detect_installed_coding_agents()
    print(f"STUDIO_DETECTED_DSH={'dsh' in detected}")

    resolved = None
    rejected = False
    try:
        resolved = start._require_agent_for_launch(
            "dsh",
            "npm install -g @deepseek-ai/dsh",
            True,
        )
    except (click.exceptions.Exit, typer.Exit):
        rejected = True
    print(f"LAUNCHER_RESOLVED={resolved}")
    print(f"LAUNCHER_REJECTED_DISTRO_DSH={rejected}")

    assert "dsh" not in detected, "Studio misidentified Distributed Shell as DeepSeek Harness"
    assert rejected, "the launcher accepted Distributed Shell as DeepSeek Harness"
    assert resolved is None
    print("AB_RESULT=PASS")


if __name__ == "__main__":
    main()
