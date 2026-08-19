#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
setup_sh="$repo_root/studio/setup.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

awk '/if \[ -n "\$INSTALLED_VER" \] && \[ -n "\$LATEST_VER" \] && \[ "\$INSTALLED_VER" = "\$LATEST_VER" \]/ {on=1}
     on && /^        _setup_pin=/ {exit}
     on {print}' "$setup_sh" > "$work/fastpath.sh"
printf '%s\n' 'fi' >> "$work/fastpath.sh"
test -s "$work/fastpath.sh"

mkdir -p "$work/venv/bin"
printf '%s\n' '#!/bin/sh' 'exec python3 -S "$@"' > "$work/venv/bin/python"
chmod +x "$work/venv/bin/python"
printf '%s\n' 'def verify_install():' '    return {"ok": True}' > "$work/install_manifest.py"

run_fastpath() {
    local installed="$1"
    local required="$2"
    (
        INSTALLED_VER="$installed"
        LATEST_VER="$installed"
        UNSLOTH_DESKTOP_BACKEND_VERSION="$required"
        _PKG_NAME="unsloth"
        VENV_DIR="$work/venv"
        SCRIPT_DIR="$work"
        _SKIP_PYTHON_DEPS=false
        step() { :; }
        substep() { :; }
        # shellcheck disable=SC1090
        . "$work/fastpath.sh"
        printf '%s\n' "$_SKIP_PYTHON_DEPS"
    )
}

got="$(run_fastpath '2026.8.14' '2026.8.15')"
if [[ "$got" != "false" ]]; then
    echo "FAIL: stale backend incorrectly stayed on the dependency-skip fast path (got=$got)" >&2
    exit 1
fi

got="$(run_fastpath '2026.8.15' '2026.8.15')"
if [[ "$got" != "true" ]]; then
    echo "FAIL: satisfying backend unnecessarily forced repair (got=$got)" >&2
    exit 1
fi

echo "PASS: setup.sh deterministically repairs stale backends and skips satisfying ones"
python3 "$repo_root/.github/repro/pr8670_source_contract.py"
