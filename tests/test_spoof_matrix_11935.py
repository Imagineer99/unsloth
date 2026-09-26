"""Spoofed-GPU detection matrix for unslothai/unsloth#11935 (RDNA 4 -> AMD gfx120X-all below 7.13).

Staging-only overlay (never committed to the PR). Every cell runs in its OWN subprocess, since the
spoofs (module attrs, sys.version_info, PATH stubs) are process-wide. Each cell is run twice:

  test_head[cell]          the PR head, asserted against the verdict the PR intends
  test_differential[cell]  merge base vs head on the same runner: EXPECT 'same' = identical on the
                           base-observable fields; 'changed:<why>' = differs, base = the old routing

Arms:
  HEAD = $SPOOF_HEAD_ROOT, default the repo root this file sits in (tests/..).
  BASE = tests/_spoof_base_11935/ (merge-base copies of install.sh + studio/install_python_stack.py;
         untouched imports such as install_manifest / backend.utils resolve from HEAD).

Cell kinds (id prefix):
  py:  studio/install_python_stack.py _ensure_rocm_torch() + _rocm_compat_reroute_pending(); the
       OS is PATCHED (IS_WINDOWS / IS_MACOS / IS_LINUX, platform.machine), so every host runs every
       OS cell; nothing in the touched Python depends on the real OS (no path handling).
  sh:  install.sh blocks lifted by marker and executed under the host's bash (Git Bash on
       Windows). The blocks never branch on the OS, so they run for real on whatever runner hosts
       them ("host-real"); install.sh itself is only ever run on Linux/macOS/WSL.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HEAD_ROOT = Path(os.environ.get("SPOOF_HEAD_ROOT") or HERE.parent).resolve()
BASE_DIR = HERE / "_spoof_base_11935"
HOST = platform.system()  # Linux | Windows | Darwin
BASH = shutil.which("bash")
RDNA4_URL = "gfx120X-all"
TORCH_211 = "torch>=2.11.0,<2.12.0"


def _arm_paths(arm: str) -> dict:
    root = HEAD_ROOT if arm == "head" else BASE_DIR
    return {
        "stack": str(root / "studio" / "install_python_stack.py"),
        "install_sh": str(root / "install.sh"),
        "head_studio": str(HEAD_ROOT / "studio"),
    }


# --------------------------------------------------------------------------------------------
# Python child: one process per cell.
# --------------------------------------------------------------------------------------------
PY_CHILD = r'''
import contextlib, importlib.util, json, os, sys
from unittest.mock import MagicMock, patch
sys.dont_write_bytecode = True
cell, paths = json.loads(sys.argv[1]), json.loads(sys.argv[2])
for k in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
          "UNSLOTH_ROCM_GFX_ARCH", "UNSLOTH_TORCH_INDEX_FAMILY", "UNSLOTH_TORCH_INDEX_URL",
          "UNSLOTH_TORCH_BACKEND", "UNSLOTH_ROCM_TORCH_INSTALLED", "HSA_OVERRIDE_GFX_VERSION",
          "UNSLOTH_PYTORCH_MIRROR", "UNSLOTH_AMD_ROCM_MIRROR", "UNSLOTH_TORCH_EXTRA"):
    os.environ.pop(k, None)
studio = paths["head_studio"]
for d in (os.path.join(studio, "backend"), studio):
    sys.path.insert(0, d)
# Import the untouched deps under the REAL interpreter version first, so a patched
# sys.version_info only reaches install_python_stack's own module body.
import install_manifest  # noqa
import backend.utils.wheel_utils, backend.utils.uv_path_safety  # noqa
spec = importlib.util.spec_from_file_location("isp_under_test", paths["stack"])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
real_vi = sys.version_info
if cell.get("py"):
    class _VI(tuple):
        major = property(lambda s: s[0]); minor = property(lambda s: s[1]); micro = property(lambda s: s[2])
    sys.version_info = _VI((*cell["py"], 0, "final", 0))
try:
    spec.loader.exec_module(m)
finally:
    sys.version_info = real_vi
os_ = cell["os"]
MARK = m._TORCH_PROBE_MARKER
installed = {
    "cpu": ("", None),
    "generic64": ("2.8.0+rocm6.4|6.4.43482|", None),
    "generic72": ("2.11.0+rocm7.2|7.2.53211|", None),
    "amd713_gfx120x": ("2.11.0+rocm7.13.0|7.13.99004|", "gfx120x-all"),
    "amd713_gfx1151": ("2.11.0+rocm7.13.0|7.13.99004|", "gfx1151"),
    "cuda": ("2.11.0+cu128||12.8", None),
}[cell.get("installed", "cpu")]
probe_stdout = (MARK + installed[0] + "\n") if installed[0] else "\n"
family = installed[1]
vendor, gfx = cell["vendor"], cell.get("gfx")
ver = tuple(cell["rocm"]) if cell.get("rocm") else None
amd = vendor == "amd_rocm"
pip, pip_try = MagicMock(), MagicMock(return_value=True)
out = {}
with contextlib.ExitStack() as st:
    P = lambda *a, **k: st.enter_context(patch.object(m, *a, **k))
    P("IS_WINDOWS", os_ == "Windows"); P("IS_MACOS", os_ == "Darwin"); P("IS_LINUX", os_ == "Linux")
    P("_TORCH_RUNTIME_PROBE", None); P("_LAST_AMD_GFX_PROBE", None)
    P("_TORCH_BACKEND", "xpu" if vendor == "intel_xpu" else "")
    P("pip_install", pip); P("pip_install_try", pip_try)
    P("_has_usable_nvidia_gpu", return_value=vendor == "nvidia")
    P("_has_rocm_gpu", return_value=amd)
    P("_torch_requires_rocm_sdk", return_value=family is not None)
    P("_installed_rocm_wheel_family", return_value=family)
    P("_infer_linux_amd_gfx_arch", return_value=None)
    P("_kfd_gfx_targets", return_value=[])
    P("_detect_rocm_version", return_value=ver if amd else None)
    P("_detect_amd_gfx_codes", return_value=[gfx] if amd and gfx else [])
    P("_detect_windows_gfx_arch", return_value=gfx if amd else None)
    P("_is_win_arm64_interpreter", return_value=False)
    P("_install_bnb_windows_rocm", return_value=True)
    st.enter_context(patch("platform.machine", return_value=cell.get("machine", "x86_64")))
    st.enter_context(patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=probe_stdout)))
    m._ensure_rocm_torch()
    if amd and gfx and ver:
        out["pending"] = bool(m._rocm_compat_reroute_pending(gfx, ver, installed[0].split("|")[0].lower()))
calls = [c.args for c in pip.call_args_list + pip_try.call_args_list]
idx = torch = None
for a in calls:
    a = [str(x) for x in a]
    if "--index-url" in a:
        idx = a[a.index("--index-url") + 1]
        torch = next((x for x in a if x.startswith("torch") and not x.startswith(("torchvision", "torchaudio"))), None)
        break
tcalls = sum(1 for a in calls if any(str(x).startswith("torch") for x in a))
out.update({"index": idx, "torch": torch, "torch_calls": tcalls})
print("CELL_JSON " + json.dumps(out))
'''


def _run_py(cell: dict, arm: str) -> dict:
    r = subprocess.run(
        [sys.executable, "-c", PY_CHILD, json.dumps(cell), json.dumps(_arm_paths(arm))],
        capture_output=True, text=True, timeout=180,
    )
    line = [l for l in r.stdout.splitlines() if l.startswith("CELL_JSON ")]
    assert r.returncode == 0 and line, f"{arm} child failed rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}"
    return json.loads(line[-1][len("CELL_JSON "):])


# --------------------------------------------------------------------------------------------
# install.sh lifted-block harness (same pattern as tests/studio/install/test_rocm_support.py).
# --------------------------------------------------------------------------------------------
def _sh_fn(source: str, name: str) -> str:
    needle = f"{name}() {{"
    start = source.find(needle)
    assert start >= 0, f"install.sh defines no {name}()"
    depth, i = 0, start + len(needle) - 1
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced {name}()")


def _posix(p) -> str:
    return Path(p).as_posix()


def _stub_bin(d: str) -> str:
    """Shadow host GPU tools so a real rocminfo / amd-smi on the runner cannot leak in."""
    b = os.path.join(d, "stubbin")
    os.makedirs(b)
    for tool in ("rocminfo", "amd-smi", "rocm-smi", "hipconfig", "nvidia-smi", "lspci"):
        p = os.path.join(b, tool)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(p, 0o755)
    return b


def _run_sh_route(cell: dict, arm: str) -> dict:
    src = Path(_arm_paths(arm)["install_sh"]).read_text(encoding="utf-8")
    start = src.find('case "$_torch_index_leaf" in\n    rocm[0-9]*)')
    end = src.find("\nfi  # _torch_index_pinned guard", start)
    assert start >= 0 and end >= 0, "routing block markers moved"
    helpers = "\n".join(_sh_fn(src, n) for n in (
        "_rocm_leaf_below", "_rocminfo_gpu_records", "_amd_smi_gpu_records",
        "_gfx_arch_slots", "_amd_smi_hip_order"))
    with tempfile.TemporaryDirectory() as d:
        venv = os.path.join(d, "venv")
        if cell.get("venv_py") != "missing":
            os.makedirs(os.path.join(venv, "bin"))
            with open(os.path.join(venv, "bin", "python"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"#!/bin/sh\necho {cell.get('venv_py', '3.12')}\n")
            os.chmod(os.path.join(venv, "bin", "python"), 0o755)
        leaf = cell["leaf"]
        script = (
            "set -euo pipefail\n"
            # Git Bash: a C:/ entry would split on its colon, so convert to /c/... via cygpath.
            f'_stub=\'{_posix(_stub_bin(d))}\'; _stub=$(cygpath -u "$_stub" 2>/dev/null || printf %s "$_stub")\n'
            'export PATH="$_stub:$PATH"\n'
            + helpers + "\n"
            + f'TORCH_INDEX_URL="https://download.pytorch.org/whl/{leaf}"\n'
            + f'_torch_index_leaf="{leaf}"\n'
            + "_torch_index_pinned=false\nSKIP_TORCH=false\n_amd_gpu_radeon=false\n"
            + "_gfx_rocm64_target=false\n"
            + "unset HSA_OVERRIDE_GFX_VERSION HIP_VISIBLE_DEVICES ROCR_VISIBLE_DEVICES\n"
            + "unset CUDA_VISIBLE_DEVICES UNSLOTH_PYTORCH_MIRROR UNSLOTH_AMD_ROCM_MIRROR\n"
            + f'export UNSLOTH_ROCM_GFX_ARCH="{cell["gfx"]}"\n'
            + f'VENV_DIR="{_posix(venv)}"\n'
            + src[start:end]
            + '\nprintf "RESULT|%s|%s|%s|%s|%s|%s|%s\\n" "$TORCH_INDEX_URL" "$_torch_index_leaf" '
            '"$_gfx_rocm64_target" "$_amd_gpu_radeon" "${TORCH_CONSTRAINT:-}" '
            '"${_amd_arch_index_routed:-unset}" "${_amd_arch_index_family:-}"\n'
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{arm} bash rc={r.returncode}\n{r.stderr[-3000:]}"
    parts = [l for l in r.stdout.splitlines() if l.startswith("RESULT|")][-1].split("|")[1:]
    keys = ("url", "leaf", "rocm64_target", "radeon", "torch", "routed", "family")
    return dict(zip(keys, parts))


def _run_sh_repair(cell: dict, arm: str) -> dict:
    src = Path(_arm_paths(arm)["install_sh"]).read_text(encoding="utf-8")
    start = src.find("        # Repair ROCm torch if overwritten during migrated install")
    end = src.find("        _gfx906_bnb_prune", start)
    assert start >= 0 and end >= 0, "migrated repair markers moved"
    fns = ["_rocm_leaf_below", "_venv_torch_rocm_below"]
    if "_venv_torch_amd_family() {" in src:
        fns.append("_venv_torch_amd_family")
    with tempfile.TemporaryDirectory() as d:
        venv_py = os.path.join(d, "python")
        fam = cell.get("venv_family", "")
        with open(venv_py, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "#!/bin/sh\n"
                f'exec "{_posix(sys.executable)}" -c "\n'
                "import sys, types\n"
                "t = types.ModuleType('torch')\n"
                f"t.__version__ = '{cell['torch']}'\n"
                "v = types.ModuleType('torch.version')\n"
                f"v.hip = '{cell.get('hip', '')}'\n"
                "t.version = v\n"
                "sys.modules['torch'] = t\n"
                "sys.modules['torch.version'] = v\n"
                "import importlib.metadata as md\n"
                f"fam = '{fam}'\n"
                "if fam: md.requires = lambda n: ['rocm-sdk-libraries-' + fam + '==7.13.0'] if n == 'rocm' else []\n"
                "exec(sys.argv[1])\n"
                '" "$2"\n'
            )
        os.chmod(venv_py, 0o755)
        floor = cell.get("floor", (6, 4))
        script = (
            "set -euo pipefail\n"
            + "\n".join(_sh_fn(src, n) for n in fns) + "\n"
            + "substep() { :; }\n"
            + '_install_torch_default_index() { printf "REINSTALL\\n"; }\n'
            + f'_VENV_PY="{_posix(venv_py)}"\n'
            + f"_gfx_rocm64_target={cell.get('rocm64_target', 'false')}\n"
            + f"_gfx_rocm64_floor_maj={floor[0]}\n_gfx_rocm64_floor_min={floor[1]}\n"
            + f"_amd_arch_index_routed={cell.get('routed', 'false')}\n"
            + f'_amd_arch_index_family="{cell.get("routed_family", "")}"\n'
            + f'_torch_index_leaf="{cell.get("leaf", "rocm7.2")}"\n'
            + src[start:end]
            + '\nprintf "DONE\\n"\n'
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{arm} bash rc={r.returncode}\n{r.stderr[-3000:]}"
    assert "DONE" in r.stdout, r.stdout
    return {"reinstall": "REINSTALL" in r.stdout}


# --------------------------------------------------------------------------------------------
# Cells: (id, kind, params, head expectation, differential expectation)
# --------------------------------------------------------------------------------------------
CELLS: list[tuple] = []


def _cell(cid, kind, params, head, diff):
    CELLS.append((cid, kind, params, head, diff))


def _generic_tag(ver):
    return {(6, 4): "rocm6.4", (7, 2): "rocm7.2", (7, 14): "rocm7.2"}[tuple(ver)]


# --- py: fresh install (CPU torch in the venv), Linux x86_64, every AMD arch x host ROCm -----
for gfx in ("gfx1030", "gfx1100", "gfx1151", "gfx1200", "gfx1201"):
    for ver in ((6, 4), (7, 2), (7, 14)):
        cid = f"Linux-patched|py:amd_rocm_{gfx}_rocm{ver[0]}.{ver[1]}|healthy_cpu_torch"
        p = {"os": "Linux", "vendor": "amd_rocm", "gfx": gfx, "rocm": ver, "installed": "cpu"}
        if gfx in ("gfx1200", "gfx1201"):
            # Every host version maps to a generic tag <= rocm7.2 (< 7.13), so RDNA 4 always moves.
            _cell(cid, "py", p, {"index~": RDNA4_URL, "torch": TORCH_211, "pending": True},
                  f"changed:RDNA4 generic {_generic_tag(ver)} -> AMD {RDNA4_URL}")
        elif gfx == "gfx1151":
            _cell(cid, "py", p, {"index~": "/gfx1151", "torch": TORCH_211, "pending": True}, "same")
        else:
            _cell(cid, "py", p, {"index~": f"/{_generic_tag(ver)}", "pending": False}, "same")

# --- py: update path: what is ALREADY installed decides reinstall vs keep --------------------
for gfx in ("gfx1100", "gfx1151", "gfx1200", "gfx1201"):
    for inst in ("generic72", "amd713_gfx120x", "amd713_gfx1151"):
        cid = f"Linux-patched|py:amd_rocm_{gfx}_rocm7.2|update_from_{inst}"
        p = {"os": "Linux", "vendor": "amd_rocm", "gfx": gfx, "rocm": (7, 2), "installed": inst}
        rdna4 = gfx in ("gfx1200", "gfx1201")
        if rdna4 and inst == "generic72":
            _cell(cid, "py", p, {"index~": RDNA4_URL, "torch": TORCH_211, "pending": True},
                  "changed:RDNA4 on generic rocm7.2 wheel is now repaired to gfx120X-all")
        elif rdna4 and inst == "amd713_gfx120x":
            _cell(cid, "py", p, {"torch_calls": 0, "pending": False}, "same")
        elif rdna4 and inst == "amd713_gfx1151":
            _cell(cid, "py", p, {"index~": RDNA4_URL, "torch": TORCH_211, "pending": True},
                  "changed:RDNA4 on another family's AMD wheel -> gfx120X-all, not generic rocm7.2")
        elif gfx == "gfx1151" and inst == "amd713_gfx1151":
            _cell(cid, "py", p, {"torch_calls": 0, "pending": False}, "same")
        elif gfx == "gfx1151":
            _cell(cid, "py", p, {"index~": "/gfx1151", "torch": TORCH_211, "pending": True}, "same")
        else:
            _cell(cid, "py", p, {"pending": False}, "same")

# --- py: Python 3.9 gate (gfx120X-all is cp310+): RDNA 4 keeps the generic wheels ------------
for gfx in ("gfx1200", "gfx1201", "gfx1151"):
    cid = f"Linux-patched|py:amd_rocm_{gfx}_rocm7.2|python3.9_venv"
    p = {"os": "Linux", "vendor": "amd_rocm", "gfx": gfx, "rocm": (7, 2), "installed": "cpu", "py": (3, 9)}
    if gfx == "gfx1151":
        _cell(cid, "py", p, {"index~": "/gfx1151", "pending": True}, "same")
    else:
        _cell(cid, "py", p, {"index~": "/rocm7.2", "pending": False}, "same")

# --- py: unknown arch, non-x86_64 Linux ------------------------------------------------------
_cell("Linux-patched|py:amd_rocm_gfx9999_rocm7.2|unknown_arch", "py",
      {"os": "Linux", "vendor": "amd_rocm", "gfx": "gfx9999", "rocm": (7, 2), "installed": "cpu"},
      {"pending": False}, "same")
_cell("Linux-patched|py:amd_rocm_gfx1201_rocm7.2|aarch64_host", "py",
      {"os": "Linux", "vendor": "amd_rocm", "gfx": "gfx1201", "rocm": (7, 2), "installed": "cpu",
       "machine": "aarch64"}, {"torch_calls": 0}, "same")

# --- py: other vendors / OSes: the PR must not move any of them ------------------------------
for os_ in ("Linux", "Windows", "Darwin"):
    for vendor in ("nvidia", "intel_xpu", "cpu_only"):
        p = {"os": os_, "vendor": vendor, "installed": "cuda" if vendor == "nvidia" else "cpu"}
        _cell(f"{os_}-patched|py:{vendor}|healthy", "py", p, {"torch_calls": 0}, "same")
    _cell(f"{os_}-patched|py:amd_rocm_gfx1100|healthy", "py",
          {"os": os_, "vendor": "amd_rocm", "gfx": "gfx1100", "rocm": (7, 2), "installed": "cpu"},
          {} if os_ != "Darwin" else {"torch_calls": 0}, "same")
    if os_ != "Linux":  # Linux RDNA 4 covered above
        _cell(f"{os_}-patched|py:amd_rocm_gfx1201|healthy", "py",
              {"os": os_, "vendor": "amd_rocm", "gfx": "gfx1201", "rocm": (7, 2), "installed": "cpu"},
              {"torch_calls": 0} if os_ == "Darwin" else {"index~": RDNA4_URL}, "same")
_cell("Darwin-patched|py:apple_mps|healthy", "py",
      {"os": "Darwin", "vendor": "apple_mps", "installed": "cpu"}, {"torch_calls": 0}, "same")

# --- sh: install.sh routing block (fresh install), host-real ---------------------------------
for gfx in ("gfx1030", "gfx1100", "gfx1102", "gfx1151", "gfx1200", "gfx1201", "gfx9999"):
    for leaf in ("rocm6.1", "rocm6.4", "rocm7.2", "rocm7.13"):
        cid = f"host-real|sh:route_amd_rocm_{gfx}_{leaf}|healthy_py3.12"
        p = {"gfx": gfx, "leaf": leaf}
        below = leaf != "rocm7.13"
        if gfx in ("gfx1200", "gfx1201") and below:
            _cell(cid, "sh_route", p, {"url~": "/gfx120X-all/", "leaf": "gfx120x-all",
                                       "torch": TORCH_211, "routed": "true", "family": "gfx120x-all"},
                  f"changed:RDNA4 {leaf} -> gfx120X-all")
        elif gfx == "gfx1151" and below:
            _cell(cid, "sh_route", p, {"url~": "/gfx1151/", "routed": "true", "family": "gfx1151"}, "same")
        else:
            _cell(cid, "sh_route", p, {"routed": "false"}, "same")
for gfx in ("gfx1200", "gfx1201"):
    for vpy in ("3.9", "missing"):
        cid = f"host-real|sh:route_amd_rocm_{gfx}_rocm7.2|venv_python_{vpy}"
        p = {"gfx": gfx, "leaf": "rocm7.2", "venv_py": vpy}
        if vpy == "3.9":
            _cell(cid, "sh_route", p, {"leaf": "rocm7.2", "routed": "false"}, "same")
        else:  # unreadable venv python is not 3.9: route
            _cell(cid, "sh_route", p, {"leaf": "gfx120x-all", "routed": "true"},
                  "changed:RDNA4 rocm7.2 -> gfx120X-all (venv python unreadable != 3.9)")

# --- sh: install.sh migrated-install repair ----------------------------------------------------
R = [
    # (label, params, head reinstall, diff)
    ("cpu_torch_not_routed", {"torch": "2.11.0+cpu", "hip": ""}, True, "same"),
    ("generic72_routed_rdna4", {"torch": "2.11.0+rocm7.2", "hip": "7.2.0", "rocm64_target": "true",
                                "routed": "true", "routed_family": "gfx120x-all"}, True,
     "changed:migrated generic rocm7.2 wheel reinstalled for the gfx120X-all route"),
    ("generic64_routed_rdna4", {"torch": "2.9.1+rocm6.4", "hip": "6.4.1", "rocm64_target": "true",
                                "routed": "true", "routed_family": "gfx120x-all"}, True,
     "changed:migrated generic rocm6.4 wheel reinstalled for the gfx120X-all route"),
    ("generic61_routed_rdna4", {"torch": "2.5.1+rocm6.1", "hip": "6.1.4", "rocm64_target": "true",
                                "routed": "true", "routed_family": "gfx120x-all"}, True, "same"),
    ("amd713_same_family", {"torch": "2.11.0+rocm7.13.0", "hip": "7.13.0", "rocm64_target": "true",
                            "routed": "true", "routed_family": "gfx120x-all",
                            "venv_family": "gfx120X-all"}, False, "same"),
    ("amd713_other_family", {"torch": "2.11.0+rocm7.13.0", "hip": "7.13.0", "rocm64_target": "true",
                             "routed": "true", "routed_family": "gfx120x-all",
                             "venv_family": "gfx1151"}, True,
     "changed:gfx1151 AMD wheel reused on RDNA4 route is reinstalled"),
    ("amd713_unreadable_family", {"torch": "2.11.0+rocm7.13.0", "hip": "7.13.0",
                                  "rocm64_target": "true", "routed": "true",
                                  "routed_family": "gfx120x-all"}, False, "same"),
    ("strix_generic72_routed", {"torch": "2.11.0+rocm7.2", "hip": "7.2.0", "routed": "true",
                                "routed_family": "gfx1151"}, True,
     "changed:migrated generic rocm7.2 wheel reinstalled for the gfx1151 route"),
    ("generic72_not_routed_rdna3", {"torch": "2.11.0+rocm7.2", "hip": "7.2.0"}, False, "same"),
]
for label, p, rein, diff in R:
    _cell(f"host-real|sh:migrated_repair|{label}", "sh_repair", p, {"reinstall": rein}, diff)


RUNNERS = {"py": _run_py, "sh_route": _run_sh_route, "sh_repair": _run_sh_repair}
IDS = [c[0] for c in CELLS]


def _skip_reason(kind: str):
    if kind.startswith("sh") and not BASH:
        return "bash not on PATH (install.sh blocks need bash; Git Bash on Windows)"
    return None


# Variables the PR introduces (install.sh _amd_arch_index_routed / _family). A pre-PR tree prints
# "unset" for them; only then are they skipped, so the negative control fails on routing alone.
_HEAD_ONLY = {"routed", "family"}


def _check(got: dict, want: dict) -> list[str]:
    bad = []
    for k, v in want.items():
        if k in _HEAD_ONLY and got.get("routed") == "unset":
            continue
        if k.endswith("~"):
            if v not in (got.get(k[:-1]) or ""):
                bad.append(f"{k[:-1]}={got.get(k[:-1])!r} lacks {v!r}")
        elif got.get(k) != v:
            bad.append(f"{k}={got.get(k)!r} != {v!r}")
    return bad


# base-observable fields (head-only bookkeeping vars excluded)
_BASE_FIELDS = {"py": ("index", "torch", "torch_calls"),
                "sh_route": ("url", "leaf", "rocm64_target", "radeon", "torch"),
                "sh_repair": ("reinstall",)}


@pytest.mark.parametrize("cell", CELLS, ids=IDS)
def test_head(cell):
    cid, kind, params, want, _ = cell
    if (r := _skip_reason(kind)):
        pytest.skip(r)
    got = RUNNERS[kind](params, "head")
    bad = _check(got, want)
    assert not bad, f"{cid}: {bad}; got={got}"


@pytest.mark.parametrize("cell", CELLS, ids=IDS)
def test_differential(cell):
    cid, kind, params, _, diff = cell
    if (r := _skip_reason(kind)):
        pytest.skip(r)
    if not BASE_DIR.is_dir():
        pytest.fail(f"vendored base copies missing at {BASE_DIR}")
    head = RUNNERS[kind](params, "head")
    base = RUNNERS[kind](params, "base")
    hv = {k: head.get(k) for k in _BASE_FIELDS[kind]}
    bv = {k: base.get(k) for k in _BASE_FIELDS[kind]}
    if diff == "same":
        assert hv == bv, f"{cid}: PR moved a cell it should not: base={bv} head={hv}"
    else:
        assert hv != bv, f"{cid}: expected change ({diff}) but base == head = {hv}"
