# SPDX-License-Identifier: AGPL-3.0-only
"""Native Windows causal A/B: released discovery vs completed adapter discovery.

No GPU is loaded. Hardware and release assets are fixtures; registry operations
are real winreg calls inside a private HKCU subtree, removed after each case.
B is a test-only counterfactual, not a production Windows device enumerator.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

PIN = "cedbb58e4a49befe12d4f28f385abc4393c763e5"
SOURCE_HASHES = {
    "studio/install_llama_prebuilt.py": "7135a9ce78a3487464b26e2695c48bf4be34c23d477a44d7bdda483d686e65af",
    "studio/prebuilt_core.py": "2a7d36fffb53cc37ff4989368b7842946d2d1744eb43dba99ccf779d0913b295",
    "studio/backend/utils/prebuilt/llama_backend.py": "cb9075152c0dabd68b8e6d8a73fc7e9c359afdc1fec5e5d1a47cdc6a5614c982",
}
LEGACY = r"SOFTWARE\Khronos\Vulkan\Drivers"
DISPLAY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"
COMPONENT = r"SYSTEM\CurrentControlSet\Control\Class\{5c4c3332-344d-483c-8739-259e934c9cc8}\0000"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This proof requires native Windows, not a mocked sys.platform.")
    import winreg

    hashes = {name: hashlib.sha256((args.source / name).read_bytes()).hexdigest()
              for name in SOURCE_HASHES}
    if hashes != SOURCE_HASHES:
        raise SystemExit("Source hash mismatch: this runner requires the exact released source bytes.")
    sys.path.insert(0, str(args.source.resolve() / "studio"))
    # Keep the environment independent of installed packages and user GPU masks.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("VK_", "GGML_", "UNSLOTH_", "HIP_", "ROCR_", "CUDA_"))}
    clean_env["UNSLOTH_STUDIO_NATIVE_TLS"] = "0"
    with patch.dict(os.environ, clean_env, clear=True):
        ilp = importlib.import_module("install_llama_prebuilt")
    original_discovery = ilp._amd_vulkan_icd_manifest_paths
    host = ilp.HostInfo(
        system="Windows", machine="amd64", is_windows=True, is_linux=False,
        is_macos=False, is_x86_64=True, is_arm64=False, nvidia_smi=None,
        driver_cuda_version=None, compute_caps=[], visible_cuda_devices=None,
        has_physical_nvidia=False, has_usable_nvidia=False, has_rocm=True,
        rocm_gfx_target="gfx1151", rocm_gfx_targets=["gfx1151"],
    )

    def plans(tag, routed_host, repo, release_tag, **kwargs):
        # Freeze availability only. Actual routing, strict backend filtering,
        # preference guards, fallback ordering and payload generation stay real.
        kind = ("windows-cuda" if routed_host.has_usable_nvidia else
                "windows-hip" if routed_host.has_rocm else
                "windows-vulkan" if routed_host.has_intel_gpu else "windows-cpu")
        kinds = [kind] if kind == "windows-cpu" else [kind, "windows-cpu"]
        return tag, [ilp.InstallReleasePlan(
            requested_tag=tag, llama_tag=tag, release_tag=release_tag,
            attempts=[ilp.AssetChoice(repo=repo, tag=tag, name=k + ".zip",
                url="https://fixture.invalid/" + k, source_label="fixture",
                install_kind=k) for k in kinds], approved_checksums={})]

    def remove_tree(path):
        # The path is always a freshly generated private test subtree.
        assert path.startswith("Software\\UnslothVulkanAB\\")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            children = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        for child in children:
            remove_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

    scenarios = [
        ("adapter_REG_SZ", "adapter", "vulkan", {}),
        ("adapter_REG_MULTI_SZ", "multi", "vulkan", {}),
        ("software_component_REG_SZ", "component", "vulkan", {}),
        ("legacy_enabled", "legacy", "vulkan", {}),
        ("no_registration", "none", "rocm", {}),
        ("legacy_disabled", "disabled", "rocm", {}),
        ("adapter_32bit_only", "wow", "rocm", {}),
        ("adapter_wrong_value_type", "wrong_type", "rocm", {}),
        ("adapter_missing_manifest", "missing_manifest", "rocm", {}),
        ("adapter_missing_library", "missing_library", "rocm", {}),
        ("adapter_HIP_mask", "adapter", "rocm", {"HIP_VISIBLE_DEVICES": "0"}),
        ("adapter_Vulkan_mask", "adapter", "rocm", {"GGML_VK_VISIBLE_DEVICES": "0"}),
        ("adapter_loader_excluded", "adapter", "rocm", {"VK_LOADER_DRIVERS_DISABLE": "*"}),
    ]
    rows = []
    for name, shape, expected_b, extra_env in scenarios:
        root = "Software\\UnslothVulkanAB\\" + uuid.uuid4().hex
        created = []
        with tempfile.TemporaryDirectory(prefix="unsloth-vulkan-ab-") as temp:
            directory = Path(temp)
            manifest = directory / "amd-vulkan64.json"
            library = directory / "fixture-driver.dll"
            # The production gate checks presence, not DLL validity. This file
            # deliberately cannot be loaded as a driver; no Vulkan calls occur.
            library.write_bytes(b"existence-only fixture; never loaded")
            manifest.write_text(json.dumps({"file_format_version": "1.0.0",
                "ICD": {"library_path": str(library), "api_version": "1.3.0"}}), encoding="utf-8")

            def register(subkey, value_name, kind, value):
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, root + "\\" + subkey,
                                       0, winreg.KEY_ALL_ACCESS) as key:
                    winreg.SetValueEx(key, value_name, 0, kind, value)
                created.append((subkey, value_name))

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, root):
                pass
            try:
                if shape in ("legacy", "disabled"):
                    register(LEGACY, str(manifest), winreg.REG_DWORD, int(shape == "disabled"))
                elif shape != "none":
                    value_name = "VulkanDriverNameWow" if shape == "wow" else "VulkanDriverName"
                    kind = (winreg.REG_MULTI_SZ if shape == "multi" else
                            winreg.REG_DWORD if shape == "wrong_type" else winreg.REG_SZ)
                    value = [str(manifest)] if shape == "multi" else 1 if shape == "wrong_type" else str(manifest)
                    register(COMPONENT if shape == "component" else DISPLAY, value_name, kind, value)
                if shape == "missing_library":
                    library.unlink()
                if shape == "missing_manifest":
                    manifest.unlink()

                def isolated_open(hive, subkey, *pos, **kw):
                    if hive != winreg.HKEY_LOCAL_MACHINE:
                        raise AssertionError("Unexpected registry hive")
                    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, root + "\\" + subkey, *pos, **kw)

                # Real registry values and types, with only HKLM redirected to
                # the isolated fixture. Never reads or writes system GPU keys.
                proxy = SimpleNamespace(**{attr: getattr(winreg, attr) for attr in dir(winreg)
                                            if not attr.startswith("__")})
                proxy.OpenKey = isolated_open

                def completed_discovery():
                    paths = list(original_discovery())
                    # Test-only active-device inventory. Production must obtain
                    # these keys from Windows device enumeration, not assume 0000
                    # or scan stale/disconnected adapter registrations.
                    for device_key in (DISPLAY, COMPONENT):
                        try:
                            with proxy.OpenKey(proxy.HKEY_LOCAL_MACHINE, device_key) as key:
                                value, kind = proxy.QueryValueEx(key, "VulkanDriverName")
                        except OSError:
                            continue
                        candidates = [value] if kind == proxy.REG_SZ else value if kind == proxy.REG_MULTI_SZ else []
                        paths.extend(p for p in candidates if Path(p).is_file())
                    return list(dict.fromkeys(paths))

                row = {"case": name, "registry_values": created, "expected_B": expected_b}
                with patch.dict(os.environ, {**clean_env, **extra_env}, clear=True), \
                     patch.dict(sys.modules, {"winreg": proxy}), \
                     patch.object(ilp, "detect_host", return_value=host), \
                     patch.object(ilp, "resolve_simple_install_release_plans", side_effect=plans), \
                     patch.object(ilp, "fetch_json", side_effect=AssertionError("Unexpected network request")):
                    # A2 rules out state/order contamination after running B.
                    for arm, discover in (("A", original_discovery), ("B", completed_discovery),
                                          ("A2", original_discovery)):
                        with patch.object(ilp, "_amd_vulkan_icd_manifest_paths", discover):
                            present = ilp._amd_vulkan_icd_present()
                            payload = ilp.resolve_backends_payload("fixture-v1", args=SimpleNamespace(
                                published_repo=ilp.DEFAULT_PUBLISHED_REPO,
                                published_release_tag="fixture-v1", has_rocm=False, rocm_gfx=None))
                            options = {entry["backend"]: entry for entry in payload["backends"]}
                            auto = options["auto"]
                            assert auto["available"], auto
                            assert options["rocm"]["resolved_backend"] == "rocm", options
                            assert options["vulkan"]["resolved_backend"] == "vulkan", options
                            row[arm] = {"driver_detected": present,
                                "automatic": auto["resolved_backend"],
                                "install_kind": auto["install_kind"],
                                "explicit_rocm": options["rocm"]["resolved_backend"],
                                "explicit_vulkan": options["vulkan"]["resolved_backend"]}
                expected_a = "vulkan" if shape == "legacy" else "rocm"
                assert row["A"]["automatic"] == expected_a, row
                assert row["B"]["automatic"] == expected_b, row
                assert row["A"] == row["A2"], row
                row["passed"] = True
                rows.append(row)
                print(f"PASS {name}: A={row['A']['automatic']} B={row['B']['automatic']} A2={row['A2']['automatic']}")
            finally:
                remove_tree(root)

    causal_cases = rows[:3]
    assert all(row["A"]["automatic"] != "vulkan" and row["B"]["automatic"] == "vulkan"
               for row in causal_cases)
    report = {"source_commit": PIN, "platform": sys.platform, "python": sys.version,
        "source_sha256": hashes, "cases": rows, "passed": True,
        "vulkan_default_contract": {"A_failures": len(causal_cases), "B_failures": 0,
                                    "A2_failures": len(causal_cases)},
        "scope": "Native Windows registry + released backend resolver; controlled hardware/assets; no GPU execution",
        "B_change": "Only complete manifest discovery from fixture active-device VulkanDriverName registrations",
        "limitations": ["No claim about the reporter's actual registry layout",
            "Fixture DLL is existence-only, not a functional Vulkan driver",
            "B uses a fixture active-device inventory; it is not a production fix",
            "Release asset availability is frozen; no downloads or benchmarks"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: {len(rows)} cases, A/B/A2; report: {args.output}")


if __name__ == "__main__":
    main()
