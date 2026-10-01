#!/usr/bin/env python3
"""Use a released macOS bundle's installer and pin its manifest backend for #12415.

Runs the bundled installer directly, without launching the native desktop shell.
Intel runners exercise the package backend; this release's macOS app is arm64.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tarfile

release = Path(os.environ["RUNNER_TEMP"]) / "issue-12415-release"
manifest = json.loads((release / "latest.json").read_text())
tag = os.environ["ISSUE_12415_RELEASE_TAG"]
backend = os.environ["ISSUE_12415_BACKEND_VERSION"]
assert manifest["version"] == tag.removeprefix("v"), manifest["version"]
assert manifest["pypi_version"] == backend, manifest["pypi_version"]
archive = release / "Unsloth-Desktop-ARM64.app.tar.gz"
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
metadata = json.loads((release / "release.json").read_text())
asset = next(a for a in metadata["assets"] if a["name"] == archive.name)
if asset.get("digest"):
    assert asset["digest"] == "sha256:" + digest, "Release asset digest mismatch"
with tarfile.open(archive) as bundle:
    info = plistlib.loads(bundle.extractfile("Unsloth.app/Contents/Info.plist").read())
    assert info["CFBundleShortVersionString"] == manifest["version"], info
    installer_bytes = bundle.extractfile("Unsloth.app/Contents/Resources/install.sh").read()
installer = release / "install.sh"
installer.write_bytes(installer_bytes)
constraint = release / "backend-constraint.txt"
constraint.write_text(f"unsloth=={backend}\n")
artifacts = Path(os.environ["STUDIO_ARTIFACT_DIR"])
artifacts.mkdir(parents=True, exist_ok=True)
(artifacts / "release-provenance.json").write_text(json.dumps({
    "release_tag": tag, "release_commit": metadata["target_commitish"],
    "published_at": metadata["published_at"], "asset_url": asset["browser_download_url"],
    "asset_sha256": digest, "installer_sha256": hashlib.sha256(installer_bytes).hexdigest(),
    "desktop_version": info["CFBundleShortVersionString"], "pypi_version": backend,
    "install_mode": "bundled installer, pinned manifest backend, GGUF-only",
    "native_desktop_app_launched": False,
}, indent=2)+"\n")
# The consumer installer uses >=, so constrain resolution to the release's package.
# --no-torch is sufficient for the GGUF discovery and llama.cpp scenario.
env = dict(os.environ, UV_CONSTRAINT=str(constraint),
           UNSLOTH_DESKTOP_BACKEND_VERSION=backend, UNSLOTH_SKIP_AUTOSTART="1")
subprocess.run(["bash", str(installer), "--no-torch"], env=env, check=True)
