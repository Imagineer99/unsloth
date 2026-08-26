# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _install_logging_stubs() -> None:
    loggers = types.ModuleType("loggers")
    loggers.get_logger = lambda *args, **kwargs: _Logger()
    sys.modules["loggers"] = loggers

    structlog = types.ModuleType("structlog")
    structlog.BoundLogger = _Logger
    structlog.get_logger = lambda *args, **kwargs: _Logger()
    sys.modules["structlog"] = structlog


def _found(fn, *, bom: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="pr9757-") as raw:
        home = Path(raw) / "home"
        downloads = home / "LM Studio Downloads"
        downloads.mkdir(parents=True)
        Path.home = classmethod(lambda cls: home)

        settings = home / ".lmstudio" / "settings.json"
        settings.parent.mkdir(parents=True)
        body = json.dumps({"downloadsFolder": str(downloads)}).encode("utf-8")
        settings.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body)
        return downloads in fn()


def main() -> None:
    arm = os.environ["ARM"]
    source_ref = os.environ["BASE_SHA"] if arm == "base" else os.environ["HEAD_SHA"]
    source = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / f"pr9757-{arm}"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(source), source_ref],
        check=True,
    )

    sys.path.insert(0, str(source / "studio" / "backend"))
    _install_logging_stubs()

    from hub.utils import paths as hub_paths
    from utils.paths import storage_roots

    results = {}
    for label, bom in (("no_bom", False), ("utf8_bom", True)):
        picker_found = _found(storage_roots.lmstudio_model_dirs, bom=bom)
        hub_found = _found(hub_paths.lmstudio_model_dirs, bom=bom)
        expected_hub = not bom or arm == "head"
        assert picker_found, (arm, label, "picker unexpectedly missed folder")
        assert hub_found == expected_hub, (arm, label, hub_found, expected_hub)
        results[label] = {"picker": picker_found, "hub": hub_found}

    shared_definition = hub_paths.lmstudio_model_dirs is storage_roots.lmstudio_model_dirs
    assert shared_definition == (arm == "head"), (arm, shared_definition)
    print(
        "A_B_EXPECTATION_PASS "
        + json.dumps(
            {
                "arm": arm,
                "ref": source_ref,
                "results": results,
                "shared_definition": shared_definition,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
