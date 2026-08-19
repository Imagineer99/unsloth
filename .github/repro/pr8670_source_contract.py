#!/usr/bin/env python3
"""Deterministic source wiring contract for PR #8670."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_rust_launcher(relative: str) -> None:
    source = read(relative)
    pattern = re.compile(
        r'cmd\.env\(\s*"UNSLOTH_DESKTOP_BACKEND_VERSION"\s*,\s*'
        r'crate::preflight::expected_backend_version\(\)\s*,?\s*\)',
        re.MULTILINE,
    )
    require(
        bool(pattern.search(source)),
        f"{relative} does not stamp the required backend version into its child process",
    )


require_rust_launcher("studio/src-tauri/src/install.rs")
require_rust_launcher("studio/src-tauri/src/update.rs")

install_sh = read("install.sh")
require(
    '_unsloth_desktop_install_spec="unsloth>=${UNSLOTH_DESKTOP_BACKEND_VERSION}"'
    in install_sh,
    "install.sh does not construct the desktop version floor",
)
require(
    install_sh.count('"$_unsloth_release_install_spec"') >= 5,
    "install.sh does not route every release install path through the version floor",
)
require(
    install_sh.count('"$_unsloth_install_pkg"') >= 2,
    "install.sh does not route both package-name install paths through the version floor",
)

install_ps1 = read("install.ps1")
require(
    '"unsloth>=$_desktopMinVer"' in install_ps1,
    "install.ps1 does not construct the desktop version floor",
)
require(
    install_ps1.count('"$_unslothReleaseInstallSpec"') >= 5,
    "install.ps1 does not route every release install path through the version floor",
)
require(
    install_ps1.count('"$_unslothPkg"') >= 2,
    "install.ps1 does not route both package-name install paths through the version floor",
)

python_stack = read("studio/install_python_stack.py")
require(
    python_stack.count('os.environ.get("UNSLOTH_DESKTOP_BACKEND_VERSION", "").strip()') == 2,
    "install_python_stack.py must apply the floor in exactly its two backend upgrade paths",
)
require(
    python_stack.count("unsloth_spec,") == 2,
    "install_python_stack.py does not pass the constrained spec to both upgrade calls",
)

print("PASS: launcher and installer version-floor wiring is complete")
