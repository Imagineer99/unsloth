# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Targeted cross-platform A/B proof for PR #7648's MTP-root classification."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run_focused_tests(backend: Path) -> None:
    tests = (
        "tests/test_mtp_drafter_companion.py",
        "tests/test_cached_gguf_routes.py",
        "tests/test_openai_auto_switch.py",
        "hub/tests/test_model_services.py",
    )
    # These tests contain POSIX-path assumptions and fail identically on the
    # unchanged base. Keep the remaining focused suite as the Windows signal.
    windows_deselects = {
        "tests/test_openai_auto_switch.py": (
            "test_idle_loop_deletes_saved_kv_when_unload_fails",
            "test_is_abs_path_id_distinguishes_path_from_repo_id",
            "test_advertised_loader_id_prefers_alias_over_abs_path",
        ),
    }
    for test in tests:
        print(f"FOCUSED_TEST_START {test}", flush = True)
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short", test]
        if sys.platform == "win32" and test in windows_deselects:
            for test_name in windows_deselects[test]:
                node_id = f"{test}::{test_name}"
                command.extend(("--deselect", node_id))
                print(f"WINDOWS_DESELECT {node_id}", flush = True)
        subprocess.run(
            command,
            cwd = backend,
            check = True,
        )
        print(f"FOCUSED_TEST_PASS {test}", flush = True)


def _same_path(actual: str, expected: Path) -> bool:
    """Compare paths across Windows casing and macOS /var -> /private/var aliases."""
    normalized_actual = os.path.normcase(os.path.realpath(os.path.abspath(actual)))
    normalized_expected = os.path.normcase(
        os.path.realpath(os.path.abspath(str(expected)))
    )
    return normalized_actual == normalized_expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type = Path, required = True)
    parser.add_argument("--expect", choices = ("before", "after"), required = True)
    parser.add_argument("--run-focused", action = "store_true")
    args = parser.parse_args()

    backend = args.backend.resolve()
    sys.path.insert(0, str(backend))

    import storage.studio_db as studio_db
    from hub.services.models import local_inventory
    from hub.utils import gguf as hub_gguf
    from utils.models import model_config

    fixed = args.expect == "after"
    with tempfile.TemporaryDirectory(prefix = "pr7648-repro-") as temp_dir:
        outer = Path(temp_dir) / "catalog"
        root = outer / "MTP"
        nested = root / "BF16"
        nested_mtp = root / "repo" / "MTP"
        nested.mkdir(parents = True)
        nested_mtp.mkdir(parents = True)

        main = root / "Qwen3.6-27B-UD-Q6_K_XL.GGUF"
        terminal = root / "gemma-4-12b-it-Q8_0-MTP.gguf"
        prefixed = root / "mtp-gemma-4-12b-it.gguf"
        nested_model = nested / "gemma-4-12b-it-Q8_0-MTP-001-of-002.gguf"
        nested_companion = nested_mtp / "gemma-4-12b-it-Q8_0-MTP.gguf"
        projector = root / "mmproj-F16.gguf"
        big_endian = root / "model-Q4_K_M-be.gguf"

        for path, size in (
            (main, 300),
            (terminal, 20),
            (prefixed, 21),
            (nested_model, 120),
            (nested_companion, 22),
            (projector, 23),
            (big_endian, 24),
        ):
            path.write_bytes(b"x" * size)

        studio_db.list_scan_folders = lambda: [
            {"path": str(outer)},
            {"path": str(root)},
        ]

        rows = local_inventory._scan_custom_folder(root)
        row_paths = {Path(row.path) for row in rows}
        direct = model_config.detect_gguf_model(str(main))
        directory = model_config.detect_gguf_model(str(root))
        core_variants, core_vision = model_config.list_local_gguf_variants(str(root))
        hub_variants, hub_vision = hub_gguf.list_local_gguf_variants(str(root))

        assert main in row_paths, "inventory must reproduce the listed model"
        assert (direct is not None) is fixed
        assert (directory is not None) is fixed
        assert model_config.detect_gguf_model(str(terminal)) is None
        assert model_config.detect_gguf_model(str(prefixed)) is None
        assert model_config.detect_gguf_model(str(nested_model)) is not None
        assert model_config.detect_gguf_model(str(nested_companion)) is None
        assert model_config.detect_gguf_model(str(projector)) is None
        assert model_config.detect_gguf_model(str(big_endian)) is None
        assert core_vision and hub_vision

        if fixed:
            assert terminal not in row_paths
            assert prefixed not in row_paths
            assert _same_path(direct, main)
            assert _same_path(directory, main)
            assert main.name in {variant.filename for variant in core_variants}
            assert main.name in {variant.filename for variant in hub_variants}
            main_quant = next(
                variant.quant for variant in hub_variants if variant.filename == main.name
            )
            selected_variant = model_config._find_local_gguf_by_variant(
                str(root), main_quant
            )
            assert selected_variant is not None and _same_path(selected_variant, main)
            config = model_config.ModelConfig.from_identifier(str(main))
            assert config is not None and config.is_gguf
            assert config.gguf_file is not None and _same_path(config.gguf_file, main)
            print(
                "PASS_AFTER inventory=true detect=true config_is_gguf=true "
                "companions_filtered=true variant_round_trip=true",
                flush = True,
            )
        else:
            assert direct is None and directory is None
            print(
                "REPRODUCED_BEFORE inventory=true detect=false "
                "picker_loader_disagreement=true",
                flush = True,
            )

        studio_db.list_scan_folders = lambda: [{"path": str(outer)}]
        assert model_config.detect_gguf_model(str(main)) is None
        studio_db.list_scan_folders = lambda: []
        assert model_config.detect_gguf_model(str(main)) is None

        ordinary = Path(temp_dir) / "GGUF"
        ordinary.mkdir()
        ordinary_main = ordinary / "Qwen3.6-27B-MTP-Q6_K_XL.gguf"
        ordinary_main.write_bytes(b"x")
        assert model_config.detect_gguf_model(str(ordinary_main)) is not None
        print(
            "BACKWARD_COMPAT ordinary_root=true unregistered_behavior=true "
            "nested_mtp_filter=true",
            flush = True,
        )

    if args.run_focused:
        if not fixed:
            raise AssertionError("focused PR tests only exist on the fixed revision")
        _run_focused_tests(backend)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
