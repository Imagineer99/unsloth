from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


TARGET = "/run/media/runner/TestDrive/models"
SENSITIVE_TARGET = "/run/media/runner/TestDrive/.ssh/models"
DENIED_TARGET = "/run"


class _ExistingScanFolderConn:
    def __init__(self):
        self.params = ()

    def execute(self, _sql, params=()):
        self.params = params
        return self

    def fetchone(self):
        return {"id": 1, "path": self.params[0], "created_at": "fake"}

    def fetchall(self):
        return []

    def commit(self):
        pass

    def close(self):
        pass


def _patch_linux_path_checks(module) -> None:
    module.platform.system = lambda: "Linux"
    module.os.path.realpath = os.path.normpath
    module.os.path.expanduser = lambda p: p
    module.os.path.exists = lambda _p: True
    module.os.path.isdir = lambda _p: True
    module.os.access = lambda _p, _mode: True


def _assert_accept(add_scan_folder, label: str) -> None:
    row = add_scan_folder(TARGET)
    if row["path"] != TARGET:
        raise AssertionError(f"{label}: stored unexpected path {row['path']!r}")


def _assert_reject(add_scan_folder, label: str, target: str, expected: str) -> None:
    try:
        add_scan_folder(target)
    except ValueError as exc:
        if expected not in str(exc):
            raise AssertionError(
                f"{label}: rejected {target!r} for wrong reason: {exc}"
            ) from exc
        return
    raise AssertionError(f"{label}: unexpectedly accepted {target!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--mode", choices=("main-negative", "pr-positive"), required=True)
    args = parser.parse_args()

    backend = Path(args.repo).resolve() / "studio" / "backend"
    sys.path.insert(0, str(backend))
    os.chdir(backend)

    from hub.storage import scan_folders
    from storage import studio_db

    _patch_linux_path_checks(scan_folders)
    _patch_linux_path_checks(studio_db)
    scan_folders._ensure_schema = lambda _conn: None
    scan_folders.get_connection = _ExistingScanFolderConn
    studio_db.get_connection = _ExistingScanFolderConn

    try:
        from utils.paths import external_media
    except ImportError:
        external_media = None
    if external_media is not None:
        external_media.platform.system = lambda: "Linux"

    if args.mode == "main-negative":
        _assert_reject(
            scan_folders.add_scan_folder,
            "hub main",
            TARGET,
            "Path under /run is not allowed",
        )
        _assert_reject(
            studio_db.add_scan_folder,
            "legacy main",
            TARGET,
            "Path under /run is not allowed",
        )
        print("PASS main-negative: both scan-folder paths reject /run/media")
        return

    _assert_accept(scan_folders.add_scan_folder, "hub pr")
    _assert_accept(studio_db.add_scan_folder, "legacy pr")
    _assert_reject(
        scan_folders.add_scan_folder,
        "hub pr",
        DENIED_TARGET,
        "Path under /run is not allowed",
    )
    _assert_reject(
        studio_db.add_scan_folder,
        "legacy pr",
        DENIED_TARGET,
        "Path under /run is not allowed",
    )
    _assert_reject(
        scan_folders.add_scan_folder,
        "hub pr",
        SENSITIVE_TARGET,
        "Credential or configuration",
    )
    _assert_reject(
        studio_db.add_scan_folder,
        "legacy pr",
        SENSITIVE_TARGET,
        "Credential or configuration",
    )
    print("PASS pr-positive: /run/media is allowed and guardrails still hold")


if __name__ == "__main__":
    main()
