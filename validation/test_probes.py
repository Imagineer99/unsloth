# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned installer probes. Real Node/npm; synthetic release responses."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

SOURCE = Path(os.environ["VALIDATION_SOURCE"]).resolve()
sys.path.insert(0, str(SOURCE / "studio"))

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / "studio" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def test_real_node_reuse_and_broken_npm(tmp_path, monkeypatch):
    m = load("validation_node", "install_node_prebuilt.py")
    host = m.detect_host()
    binary = Path(shutil.which("node")).resolve()
    candidates = [binary.parent / "node_modules/npm",
                  binary.parent.parent / "lib/node_modules/npm"]
    npm_source = next((p for p in candidates if (p / "bin/npm-cli.js").is_file()), None)
    assert npm_source, f"Fixture setup: cannot find npm next to {binary}"
    install = tmp_path / "isolated-node"
    target = m.node_binary_path(install, host)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, target)
    npm_target = m.npm_cli_path(install, host).parent.parent
    shutil.copytree(npm_source, npm_target)
    version = subprocess.check_output([str(binary), "-v"], text=True).strip().lstrip("v")
    assert m.installed_node_version(install, host) == version, "Fixture Node does not run"
    npm_major = m.installed_npm_major(install, host)
    assert npm_major is not None and npm_major >= m.NPM_MIN_MAJOR, "Fixture npm below required floor"
    m.write_metadata(install, version=version, asset="real-node-fixture", sha256="fixture")
    calls = []
    original = m._run_node
    def counted(directory, target_host, args, **kwargs):
        calls.append("node -v" if args == ["-v"] else "npm --version")
        return original(directory, target_host, args, **kwargs)
    monkeypatch.setattr(m, "_run_node", counted)
    assert m.existing_install_matches(install, host, version=version)
    first = list(calls)
    assert first == ["node -v", "npm --version"]
    calls.clear()
    assert m.existing_install_matches(install, host, version=version)
    second = list(calls)
    expected = ["npm --version"] if hasattr(m, "_recorded_runtime_matches") else ["node -v", "npm --version"]
    assert second == expected
    dependency = npm_target / "lib/cli.js"
    assert dependency.is_file(), "Fixture npm entry dependency missing before fault injection"
    dependency.unlink()
    assert not m.existing_install_matches(install, host, version=version), "Broken npm accepted"
    print(json.dumps({"revision": os.environ["VALIDATION_REVISION"], "node":version,
                      "npm_major":npm_major,"first":first,"second":second,
                      "broken_npm_rejected":True}))

def test_latest_matches_publication_order(monkeypatch):
    m = load("validation_llama", "install_llama_prebuilt.py")
    releases = [
        {"tag_name":"release-1","published_at":"2026-01-01T00:00:00Z","draft":False,"prerelease":False},
        {"tag_name":"release-2","published_at":"2026-02-01T00:00:00Z","draft":False,"prerelease":False},
    ]
    monkeypatch.setattr(m, "github_releases", lambda *a, **k: releases)
    monkeypatch.setattr(m, "_download_host_latest_release_tag", lambda *a: "release-1")
    monkeypatch.setattr(m, "_METADATA_MEMO", None)
    monkeypatch.delenv("UNSLOTH_LLAMA_DISABLE_DOWNLOAD_HOST_RESOLVE", raising=False)
    host = m.HostInfo(system="Darwin",machine="arm64",is_windows=False,is_linux=False,
                     is_macos=True,is_x86_64=False,is_arm64=True,nvidia_smi=None,
                     driver_cuda_version=None,compute_caps=[],visible_cuda_devices=None,
                     has_physical_nvidia=False,has_usable_nvidia=False,macos_version=(15,0))
    full = next(m.iter_release_payloads_by_time(m.DEFAULT_PUBLISHED_REPO, requested_tag="latest"))["tag_name"]
    assert full == "release-2"
    if hasattr(m, "_expected_release_tag_without_plan"):
        observed = m._expected_release_tag_without_plan(
            {"release_tag":"release-1","tag":"b9001"}, "latest", m.DEFAULT_PUBLISHED_REPO, "",host=host)
    else:
        observed = full  # Base has no marker shortcut; macOS uses publication ordering.
    print(json.dumps({"revision":os.environ["VALIDATION_REVISION"],
                      "full_ordering":full,"marker_answer":observed}))
    assert observed == full, f"marker lookup returns {observed}; full publication ordering selects {full}"
