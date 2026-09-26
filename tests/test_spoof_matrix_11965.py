# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team.
"""Spoofed-GPU detection matrix for unslothai/unsloth#11965 (AMD visibility-mask handling).

Subjects (the PR's real entrypoints, head vs merge base, one subprocess per cell):
  * studio/setup.sh      GPU-summary detection + selection block (lifted by markers, as
                         tests/sh/test_setup_gpu_summary_probe_sources.sh does) -> $_setup_gfx
  * install.sh           torch-routing arch block (lifted as test_rocm_support.py does)
                         -> $_runtime_gfx
  * install_llama_prebuilt._pick_rocm_gfx_target (rocminfo = ROCr-filtered output, and the
                         unfiltered hipinfo/amd-smi call shape)
  * install_llama_prebuilt.detect_host (platform.system() PATCHED to Linux/Windows/Darwin; probe
                         executables faked at the which/run_capture seams)

Shell cells run under the REAL bash of the runner (Git Bash on Windows, macOS bash + BSD awk),
labelled `<OS>-real`. detect_host cells patch platform.system(), labelled `<OS>-patched`.
_pick_rocm_gfx_target is pure: labelled `<OS>-real(pure)`.

rocminfo / amd-smi / hipinfo / nvidia-smi are stubs. The rocminfo stub applies
ROCR_VISIBLE_DEVICES itself at CALL time (ROCr filters and renumbers agents; unset -> all;
set-empty / -1 -> none; ordinals kept as a prefix up to the first out-of-range or repeated one,
UUID tokens skipped), so install.sh's unmasked re-probe sees the full list. amd-smi is never
ROCr-filtered and carries an identity HIP_ID map.

Every cell's verdict is checked against an independent model of the intended semantics
(`_oracle_head`), and the base arm against a model of the pre-PR semantics (`_oracle_base`).
A cell is INTENDED-CHANGED iff the two models disagree; every other cell must be IDENTICAL
base vs head. HEAD root = $SPOOF_HEAD_ROOT (default: this repo); BASE = tests/_spoof_base_11965.
"""

from __future__ import annotations

import concurrent.futures as _cf
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HEAD_ROOT = Path(os.environ.get("SPOOF_HEAD_ROOT") or HERE.parent).resolve()
BASE_ROOT = HERE / "_spoof_base_11965"
HOST = platform.system() or "Unknown"

SHADOW = {"gfx90c", "gfx1013", "gfx1033", "gfx1035", "gfx1036", "gfx1103", "gfx1153"}
NAMES = {
    "gfx1036": "AMD Radeon Graphics",
    "gfx1201": "AMD Radeon AI PRO R9700",
    "gfx1100": "AMD Radeon RX 7900 XTX",
    "gfx1030": "AMD Radeon RX 6900 XT",
}
DEVSETS = {
    "amd_single_dgpu": ["gfx1100"],
    "amd_hybrid_igpu+dgpu": ["gfx1036", "gfx1201"],
    "amd_multi_dgpu": ["gfx1100", "gfx1201", "gfx1030"],
}
_MASK_VARS = ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")
_CLEAR_VARS = _MASK_VARS + ("UNSLOTH_ROCM_GFX_ARCH", "HSA_OVERRIDE_GFX_VERSION", "HIP_PATH",
                            "ROCM_PATH", "UNSLOTH_ENABLE_AMD_SMI")
UUID = "GPU-4b2c1a9f8d3e6f7a"

# --------------------------------------------------------------------------- semantic models


def _stub_rocr_survivors(P, rocr):
    """What the rocminfo stub lists under ROCR (same rule as its awk)."""
    if rocr is None:
        return list(P)
    m = re.sub(r"\s", "", rocr)
    if m in ("", "-1"):
        return []
    out, seen = [], set()
    for t in m.split(","):
        if not re.fullmatch(r"[0-9]+", t):
            continue
        x = int(t)
        if x >= len(P) or x in seen:
            break
        seen.add(x)
        out.append(P[x])
    return out


def _rocr_prefix_or_all(P, rocr):
    """Intended tool-side survivors over an unfiltered (amd-smi) list: prefix, none -> all."""
    kept = _stub_rocr_survivors(P, rocr)
    return kept or list(P)


def _rocr_inrange_all(P, rocr):
    """Pre-PR install.sh: every in-range ordinal, in mask order, repeats kept; none -> all."""
    out = []
    for t in rocr.split(","):
        t = re.sub(r"\s", "", t)
        if re.fullmatch(r"[0-9]+", t) and int(t) < len(P):
            out.append(P[int(t)])
    return out or list(P)


def _repick(L, sel):
    if sel is None or sel not in SHADOW:
        return sel
    for g in L:
        if g != "gfx906" and g not in SHADOW:
            return g
    return sel


def _shell_idx(vis, n):
    idx = 0
    if vis and vis != "-1":
        first = vis.split(",")[0]
        if re.fullmatch(r"[0-9]+", first):
            idx = int(first)
    return 0 if idx >= n else idx


def _py_index(L, vis):
    v = vis.strip()
    if v in ("", "-1"):
        return None
    try:
        i = int(v.split(",")[0].strip())
        if 0 <= i < len(L):
            return L[i]
    except ValueError:
        pass
    return L[0]


def _uuidish(rocr):
    return bool(re.sub(r"[0-9, \t]", "", rocr))


def _setup_model(P, probe, m, head):
    hip, rocr, cuda = m["HIP_VISIBLE_DEVICES"], m["ROCR_VISIBLE_DEVICES"], m["CUDA_VISIBLE_DEVICES"]
    if probe == "none":
        return ""
    if probe == "rocminfo":
        recs = _stub_rocr_survivors(P, rocr)
        if not recs:
            return ""  # no GPU agent, amd-smi absent: not detected
        smi = False
    else:
        recs, smi = list(P), True
    if head:
        if smi and rocr not in (None, "", "-1"):
            if _uuidish(rocr) and len(set(recs)) > 1:
                return ""  # declined: UUID over unlike adapters
            recs = _rocr_prefix_or_all(recs, rocr)
        vis = hip if hip is not None else (cuda or "")
    else:
        vis = hip if hip else (rocr or "")
    g = recs[_shell_idx(vis, len(recs))]
    if hip is None and rocr is None and cuda is None:
        g = _repick(recs, g)
    return g


def _install_model(P, probe, m, head):
    hip, rocr, cuda = m["HIP_VISIBLE_DEVICES"], m["ROCR_VISIBLE_DEVICES"], m["CUDA_VISIBLE_DEVICES"]
    if probe == "none":
        return ""
    label = ""
    if probe == "rocminfo":
        G = _stub_rocr_survivors(P, rocr)
        if G:
            label = "rocminfo"
        elif rocr is not None or hip is not None:
            G = list(P)  # unmasked rocminfo re-probe
        if not G:
            return ""
    else:
        G = list(P)
    unresolved = False
    if label != "rocminfo" and rocr not in (None, "", "-1"):
        G = _rocr_prefix_or_all(G, rocr) if head else _rocr_inrange_all(G, rocr)
        unresolved = _uuidish(rocr)
    vis = hip if hip is not None else cuda
    g = G[_shell_idx(vis or "", len(G))]
    if unresolved and len(set(G)) > 1:
        g = ""
    return g


def _pick_model(P, filtered, m, head):
    hip, rocr, cuda = m["HIP_VISIBLE_DEVICES"], m["ROCR_VISIBLE_DEVICES"], m["CUDA_VISIBLE_DEVICES"]
    if not filtered:  # hipinfo / amd-smi call shape: unchanged by the PR
        L, order = list(P), (hip, rocr, cuda)
    else:
        L = _stub_rocr_survivors(P, rocr)
        order = (hip, cuda) if head else (hip, rocr, cuda)
    if not L:
        return None
    vis = next((v for v in order if v is not None), None)
    if head and filtered and vis is None and rocr is not None:
        vis = "0" if rocr.strip() not in ("", "-1") else ""
    if vis is not None:
        return _py_index(L, vis)
    return _repick(L, L[0])


# --------------------------------------------------------------------------- cells


def _masks(rocr=None, hip=None, cuda=None):
    return {"HIP_VISIBLE_DEVICES": hip, "ROCR_VISIBLE_DEVICES": rocr, "CUDA_VISIBLE_DEVICES": cuda}


def _mask_label(m):
    def f(v):
        return "unset" if v is None else repr(v) if v in ("",) else v
    return f"ROCR={f(m['ROCR_VISIBLE_DEVICES'])},HIP={f(m['HIP_VISIBLE_DEVICES'])},CUDA={f(m['CUDA_VISIBLE_DEVICES'])}"


ROCR_VALS = [None, "0", "1", "1,0", "2,0", "0,0,1", "0,99,1", "-1", "", f"{UUID},1"]
HIP_VALS = [None, "0", "1", "", "-1"]
FULL_MASKS = [_masks(r, h) for r, h in itertools.product(ROCR_VALS, HIP_VALS)] + [
    _masks(r, h, "1") for r, h in itertools.product([None, "1,0", "2,0"], [None, ""])
]
SMALL_MASKS = [_masks(r, h) for r, h in itertools.product([None, "0", "1", "-1"], [None, "0", "1"])]
DEFECT_MASKS = [_masks(r, h) for r, h in itertools.product(["1", "2,0", "0,0,1"], [None, "1"])]


def _shell_cells(tool):
    cells = []
    for dev, P in DEVSETS.items():
        masks = SMALL_MASKS if dev == "amd_single_dgpu" else FULL_MASKS
        for probe in ("rocminfo", "amdsmi"):
            for m in masks:
                cells.append((tool, dev, probe, m))
        if dev != "amd_single_dgpu":
            for probe in ("rocminfo_fail+amdsmi", "rocminfo_nogpu+amdsmi"):
                for m in DEFECT_MASKS:
                    cells.append((tool, dev, probe, m))
    for m in (_masks(), _masks("1"), _masks(None, "0")):
        cells.append((tool, "cpu_only", "none", m))
    return cells


def _pick_cells():
    cells = []
    for dev, P in DEVSETS.items():
        masks = SMALL_MASKS if dev == "amd_single_dgpu" else FULL_MASKS
        for filtered in (True, False):
            for m in masks:
                cells.append(("pick", dev, "rocminfo" if filtered else "hipinfo/amd-smi", m))
    return cells


DETECT_MASKS = [_masks(), _masks("1"), _masks("1,0"), _masks(None, "1"), _masks(None, None, "1"),
                _masks("-1"), _masks("1", "0")]
DETECT_VENDORS = {
    "Linux": ["nvidia", "amd_single_dgpu", "amd_hybrid_igpu+dgpu", "amd_multi_dgpu",
              "amd_hybrid_igpu+dgpu|rocminfo_fail", "amd_hybrid_igpu+dgpu|rocminfo_absent",
              "intel_xpu", "cpu_only"],
    "Windows": ["nvidia", "amd_single_dgpu", "amd_hybrid_igpu+dgpu", "amd_multi_dgpu",
                "intel_xpu", "cpu_only"],
    "Darwin": ["apple_mps", "cpu_only"],
}


def _detect_cells():
    return [("detect", f"{plat}|{v}", plat, m) for plat, vs in DETECT_VENDORS.items() for v in vs
            for m in DETECT_MASKS]


ALL_CELLS = _shell_cells("setup.sh") + _shell_cells("install.sh") + _pick_cells() + _detect_cells()


def _cell_id(c):
    tool, dev, probe, m = c
    if tool == "detect":
        plat, vendor = dev.split("|", 1)
        return f"detect_host|{plat}-patched|{vendor}|{_mask_label(m)}"
    osl = f"{HOST}-real(pure)" if tool == "pick" else f"{HOST}-real"
    return f"{tool}|{osl}|{dev}|probe={probe}|{_mask_label(m)}"


# --------------------------------------------------------------------------- shell harness


def _bash():
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for cand in (Path(pf) / "Git" / "bin" / "bash.exe", Path(pf) / "Git" / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
        w = shutil.which("bash")
        return w if w and "system32" not in w.lower() else None  # System32\bash.exe is WSL
    return shutil.which("bash")


def _sed_fn(src, name):
    """sed -n '/^name()/,/^}/p'"""
    lines, out, on = src.splitlines(), [], False
    for ln in lines:
        if not on and ln.startswith(f"{name}()"):
            on = True
        if on:
            out.append(ln)
            if ln == "}":
                break
    assert out, f"setup.sh defines no {name}()"
    return "\n".join(out)


def _setup_block(root):
    src = (root / "studio" / "setup.sh").read_text(encoding="utf-8")
    parts = [
        "substep() { :; }\nstep() { :; }\ntimeout() { shift; \"$@\"; }",
        "command_not_found_handle() { printf 'FATAL: extracted setup.sh block called %s\\n' \"$1\" >&2; exit 127; }",
    ]
    for fn in ("_setup_run_smi", "_setup_rocminfo_gpu_records", "_setup_amd_smi_gpu_records",
               "_setup_amd_smi_hip_order", "_amd_gfx_is_shadowing_integrated", "_amd_prefer_discrete_gfx"):
        parts.append(_sed_fn(src, fn))
    lines = src.splitlines()
    i0 = lines.index("_setup_amd_detected=false")
    i1 = lines.index('_setup_amd_records=""', i0)
    parts.append("\n".join(lines[i0:i1 + 1]))
    j0 = lines.index('if [ "$_setup_nvidia_usable" != true ]; then')
    j1 = next(j for j in range(j0, len(lines)) if "UNSLOTH_ROCM_GFX_ARCH env override" in lines[j])
    parts.append("\n".join(lines[j0:j1]))
    parts.append("fi")
    body = "\n".join(parts)
    assert "_setup_amd_record=" in body and "_amd_prefer_discrete_gfx" in body
    return body + '\nprintf \'{"gfx":"%s"}\\n\' "$_setup_gfx"\n'


def _sh_fn_body(source, name):
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
                return source[start:i + 1]
        i += 1
    return source[start:]


def _install_block(root):
    source = (root / "install.sh").read_text(encoding="utf-8")
    helpers = "\n".join(_sh_fn_body(source, n) for n in (
        "_rocm_leaf_below", "_rocminfo_gpu_records", "_amd_smi_gpu_records", "_gfx_arch_slots",
        "_amd_smi_hip_order"))
    start = source.find('case "$_torch_index_leaf" in\n    rocm[0-9]*)')
    end = source.find("\nfi  # _torch_index_pinned guard", start)
    assert start >= 0 and end >= 0, "install.sh routing block markers moved"
    return (helpers + "\n"
            + 'TORCH_INDEX_URL="https://download.pytorch.org/whl/rocm6.1"\n_torch_index_leaf="rocm6.1"\n'
            + "_torch_index_pinned=false\nSKIP_TORCH=false\n_amd_gpu_radeon=false\n_gfx_rocm64_target=false\n"
            + source[start:end]
            + '\nprintf \'{"gfx":"%s"}\\n\' "${_runtime_gfx:-}"\n')


def _stub_text(P, probe):
    out = []
    if probe.startswith("rocminfo"):
        cases = "\n".join(f"      {i}) _sp_g={g}; _sp_m='{NAMES[g]}' ;;" for i, g in enumerate(P))
        allsel = " ".join(str(i) for i in range(len(P)))
        if probe == "rocminfo_fail+amdsmi":
            out.append("rocminfo() { return 1; }")
        else:
            gpus = "" if probe == "rocminfo_nogpu+amdsmi" else f"""
  if [ -n "${{ROCR_VISIBLE_DEVICES+x}}" ]; then
    _sp_sel=$(awk -v m="$ROCR_VISIBLE_DEVICES" -v n={len(P)} 'BEGIN {{ gsub(/[[:space:]]/, "", m); if (m == "" || m == "-1") exit; k = split(m, t, ","); for (i = 1; i <= k; i++) {{ if (t[i] !~ /^[0-9]+$/) continue; x = t[i] + 0; if (x >= n || (x in s)) break; s[x] = 1; print x }} }}')
  else
    _sp_sel="{allsel}"
  fi
  _sp_a=2
  for _sp_i in $_sp_sel; do
    case $_sp_i in
{cases}
    esac
    printf '*******\\nAgent %s\\n*******\\n  Name:                    %s\\n  Marketing Name:          %s\\n  Device Type:             GPU\\n      Name:                amdgcn-amd-amdhsa--%s\\n' "$_sp_a" "$_sp_g" "$_sp_m" "$_sp_g"
    _sp_a=$((_sp_a + 1))
  done"""
            out.append("rocminfo() {\n  printf 'Agent 1\\n*******\\n  Name:                    AMD Ryzen 9 7950X\\n"
                       "  Device Type:             CPU\\n'" + gpus + "\n}")
    if "amdsmi" in probe:
        lst = "".join(f"GPU: {i}  BDF: 0000:0{i + 3}:00.0  UUID: aaaa-{i}  KFD_ID: {i + 1}\\n" for i in range(len(P)))
        hip = "".join(f"GPU: {i}\\n    HIP_ID: {i}\\n" for i in range(len(P)))
        asic = "".join(f"GPU: {i}\\n    ASIC:\\n        MARKET_NAME: {NAMES[g]}\\n        TARGET_GRAPHICS_VERSION: {g}\\n"
                       for i, g in enumerate(P))
        out.append("amd-smi() {\n  case \"$*\" in\n"
                   f"    *'list -e'*) printf '{hip}' ;;\n"
                   f"    list|'list '*) printf '{lst}' ;;\n"
                   f"    *static*) printf '{asic}' ;;\n"
                   "    *) return 1 ;;\n  esac\n}")
    return "\n".join(out) + "\n"


_PROBE_TOOLS = ("rocminfo", "amd-smi", "hipinfo", "hipconfig", "rocm-smi")


def _clean_path():
    keep = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and any(Path(d, t).exists() or Path(d, t + ".exe").exists() for t in _PROBE_TOOLS):
            continue  # a real ROCm tool on this host would answer for the stub's absence
        keep.append(d)
    return os.pathsep.join(keep)


def _child_env(m):
    env = {k: v for k, v in os.environ.items() if k not in _CLEAR_VARS}
    env["PATH"] = _clean_path()
    for k, v in m.items():
        if v is not None:
            env[k] = v
    return env


def _run_shell(block_file, stub_file, m):
    bash = _bash()
    r = subprocess.run(
        [bash, "-c", 'set -euo pipefail; . "$1"; . "$2"', "_",
         Path(stub_file).as_posix(), Path(block_file).as_posix()],
        capture_output=True, text=True, env=_child_env(m), timeout=120)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        return {"error": f"rc={r.returncode} stderr={r.stderr[-800:]!r} stdout={r.stdout[-300:]!r}"}
    return json.loads(lines[-1])


# --------------------------------------------------------------------------- python child

CHILD = r'''
import glob, importlib.util, inspect, json, os, platform, re, shutil, subprocess, sys
a = json.loads(sys.argv[1])
sys.path.insert(0, a["studio_dir"])
spec = importlib.util.spec_from_file_location("ilp_under_test", a["file"])
ILP = importlib.util.module_from_spec(spec)
sys.modules["ilp_under_test"] = ILP
spec.loader.exec_module(ILP)
P, NAMES = a["phys"], a["names"]

def survivors(rocr):
    if rocr is None:
        return list(P)
    m = re.sub(r"\s", "", rocr)
    if m in ("", "-1"):
        return []
    out, seen = [], set()
    for t in m.split(","):
        if not re.fullmatch(r"[0-9]+", t):
            continue
        x = int(t)
        if x >= len(P) or x in seen:
            break
        seen.add(x); out.append(P[x])
    return out

def rocminfo_text(L):
    s = "Agent 1\n*******\n  Name:                    AMD Ryzen 9 7950X\n  Device Type:             CPU\n"
    for i, g in enumerate(L):
        s += ("*******\nAgent %d\n*******\n  Name:                    %s\n  Marketing Name:          %s\n"
              "  Device Type:             GPU\n      Name:                amdgcn-amd-amdhsa--%s\n") % (i + 2, g, NAMES[g], g)
    return s

def hipinfo_text(L):
    return "".join("device#                           %d\nName:                             %s\n"
                   "gcnArchName:                      %s:sramecc-:xnack-\n\n" % (i, NAMES[g], g)
                   for i, g in enumerate(L))

def accepts_rocr_filtered(f):
    return "rocr_filtered" in inspect.signature(f).parameters

if a["mode"] == "pick":
    f = ILP._pick_rocm_gfx_target
    if a["filtered"]:
        out = rocminfo_text(survivors(os.environ.get("ROCR_VISIBLE_DEVICES")))
        # Called exactly as that arm's detect_host calls it on the rocminfo probe.
        g = f(out, rocr_filtered=True) if accepts_rocr_filtered(f) else f(out)
    else:
        g = f(hipinfo_text(P))
    print(json.dumps({"gfx": g}))
    sys.exit(0)

plat, vendor, defect = a["platform"], a["vendor"], a["defect"]
machine = "arm64" if vendor == "apple_mps" else "x86_64"
platform.system = lambda: plat
platform.machine = lambda: machine
platform.mac_ver = lambda: ("14.5", ("", "", ""), machine)
exe = {}
if vendor == "nvidia":
    exe["nvidia-smi"] = "/fake/bin/nvidia-smi"
elif vendor.startswith("amd"):
    if plat == "Linux":
        if defect != "rocminfo_absent":
            exe["rocminfo"] = "/fake/bin/rocminfo"
        exe["amd-smi"] = "/fake/bin/amd-smi"
    elif plat == "Windows":
        exe["hipinfo"] = "C:/fake/bin/hipinfo.exe"
shutil.which = lambda name, *x, **k: exe.get(name)

def fake_run(command, *, timeout=30, check=False, env=None):
    name = os.path.basename(command[0]).lower()
    name = name[:-4] if name.endswith(".exe") else name
    envv = os.environ if env is None else env
    rc, out = 0, ""
    if name == "nvidia-smi":
        if "-L" in command:
            out = "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-11111111-2222-3333-4444-555555555555)\n"
        elif any(c.startswith("--query-gpu") for c in command):
            out = "0, GPU-11111111-2222-3333-4444-555555555555, 8.9\n"
        else:
            out = "NVIDIA-SMI 570.00   Driver Version: 570.00   CUDA Version: 12.8\n"
    elif name == "rocminfo":
        if defect == "rocminfo_fail":
            rc = 1
        else:
            out = rocminfo_text(survivors(envv.get("ROCR_VISIBLE_DEVICES")))
    elif name == "amd-smi":
        out = "".join("GPU: %d\n    BDF: 0000:0%d:00.0\n" % (i, i + 3) for i in range(len(P)))
    elif name == "hipinfo":
        out = hipinfo_text(P)
    else:
        rc = 1
    return subprocess.CompletedProcess(command, rc, out, "")

ILP.run_capture = fake_run
ILP.nvidia_library_inventory = lambda: None
if hasattr(ILP, "proc_driver_version"):
    ILP.proc_driver_version = lambda *x, **k: ""
_isdir, _access, _glob = os.path.isdir, os.access, glob.glob
os.path.isdir = lambda p: False if str(p).startswith("/proc/driver/nvidia") else _isdir(p)
os.access = lambda p, *x, **k: False if str(p).startswith("/opt/rocm") else _access(p, *x, **k)
vend_files = []
if vendor == "intel_xpu" and plat == "Linux":
    import tempfile
    d = tempfile.mkdtemp()
    vend_files = [os.path.join(d, "vendor")]
    open(vend_files[0], "w").write("0x8086\n")
ILP.glob.glob = lambda pat, *x, **k: list(vend_files) if pat.startswith("/sys/class/drm") else _glob(pat, *x, **k)
for fn, val in (("windows_intel_gpu_in_registry", vendor == "intel_xpu"),
                ("windows_amd_gpu_in_registry", False)):
    if hasattr(ILP, fn):
        setattr(ILP, fn, (lambda v: (lambda *x, **k: v))(val))
h = ILP.detect_host()
print(json.dumps({k: getattr(h, k) for k in ("has_usable_nvidia", "has_rocm", "rocm_gfx_target",
      "rocm_gfx_targets", "has_intel_gpu", "has_amd_gpu_without_rocm")}))
'''


def _run_py(root, cell):
    tool, dev, probe, m = cell
    args = {"studio_dir": str((HEAD_ROOT / "studio").resolve()),
            "file": str(root / "studio" / "install_llama_prebuilt.py"), "names": NAMES}
    if tool == "pick":
        args.update(mode="pick", phys=DEVSETS[dev], filtered=(probe == "rocminfo"))
    else:
        plat, vendor = dev.split("|", 1)
        vendor, _, defect = vendor.partition("|")
        args.update(mode="detect", platform=plat, vendor=vendor, defect=defect,
                    phys=DEVSETS.get(vendor, []))
    env = _child_env(m)
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, "-c", CHILD, json.dumps(args)], capture_output=True,
                       text=True, env=env, timeout=120)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        return {"error": f"rc={r.returncode} stderr={r.stderr[-1500:]!r}"}
    return json.loads(lines[-1])


# --------------------------------------------------------------------------- expectations


def _detect_expect(cell, head):
    """Expected detect_host fields (subset) under the models."""
    _, dev, plat, m = cell
    vendor, _, defect = dev.split("|", 1)[1].partition("|")
    if vendor.startswith("amd"):
        P = DEVSETS[vendor]
        if plat == "Linux":
            rocminfo_gpus = [] if defect in ("rocminfo_fail", "rocminfo_absent") else \
                _stub_rocr_survivors(P, m["ROCR_VISIBLE_DEVICES"])
            if rocminfo_gpus:
                return {"has_rocm": True, "rocm_gfx_target": _pick_model(P, True, m, head)}
            return {"has_rocm": True, "rocm_gfx_target": None}  # amd-smi list: no gfx token
        return {"has_rocm": True, "rocm_gfx_target": _pick_model(P, False, m, head)}
    exp = {"has_rocm": False, "rocm_gfx_target": None}
    if vendor == "intel_xpu":
        exp["has_intel_gpu"] = True
    if vendor == "nvidia" and m == _masks():
        exp["has_usable_nvidia"] = True
    if vendor in ("cpu_only", "apple_mps"):
        exp.update(has_usable_nvidia=False, has_intel_gpu=False, has_amd_gpu_without_rocm=False)
    return exp


def _expect(cell, head):
    tool, dev, probe, m = cell
    if tool == "detect":
        return _detect_expect(cell, head)
    if tool == "pick":
        return {"gfx": _pick_model(DEVSETS[dev], probe == "rocminfo", m, head)}
    P = DEVSETS.get(dev, [])
    model = _setup_model if tool == "setup.sh" else _install_model
    return {"gfx": model(P, "amdsmi" if "amdsmi" in probe else probe, m, head)}


# --------------------------------------------------------------------------- execution


@pytest.fixture(scope="session")
def results(tmp_path_factory):
    work = tmp_path_factory.mktemp("spoof11965")
    need_bash = any(c[0] in ("setup.sh", "install.sh") for c in ALL_CELLS)
    bash_ok = bool(_bash()) if need_bash else True
    blocks = {}
    if bash_ok:
        for arm, root in (("head", HEAD_ROOT), ("base", BASE_ROOT)):
            for tool, fn in (("setup.sh", _setup_block), ("install.sh", _install_block)):
                p = work / f"{arm}_{tool.replace('.', '_')}.sh"
                p.write_text(fn(root), encoding="utf-8", newline="\n")
                blocks[(arm, tool)] = p
    stubs = {}

    def stub_for(dev, probe):
        key = (dev, probe)
        if key not in stubs:
            p = work / f"stub_{len(stubs)}.sh"
            p.write_text(_stub_text(DEVSETS.get(dev, []), probe), encoding="utf-8", newline="\n")
            stubs[key] = p
        return stubs[key]

    jobs = {}
    for c in ALL_CELLS:
        if c[0] in ("setup.sh", "install.sh"):
            if not bash_ok:
                continue
            s = stub_for(c[1], c[2])
            for arm in ("head", "base"):
                jobs[(_cell_id(c), arm)] = (_run_shell, (blocks[(arm, c[0])], s, c[3]))
        else:
            for arm, root in (("head", HEAD_ROOT), ("base", BASE_ROOT)):
                jobs[(_cell_id(c), arm)] = (_run_py, (root, c))
    out = {}
    with _cf.ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 2) * 2)) as ex:
        futs = {ex.submit(fn, *args): k for k, (fn, args) in jobs.items()}
        for f in _cf.as_completed(futs):
            out[futs[f]] = f.result()
    out["_bash_ok"] = bash_ok
    return out


def _get(results, cell, arm):
    if cell[0] in ("setup.sh", "install.sh") and not results["_bash_ok"]:
        pytest.skip("no bash on this runner (Windows without Git Bash): shell cells not resolvable")
    r = results[(_cell_id(cell), arm)]
    assert "error" not in r, f"{arm} arm harness error: {r['error']}"
    return r


def _subset(r, exp):
    return {k: r.get(k) for k in exp}


@pytest.mark.parametrize("cell", ALL_CELLS, ids=[_cell_id(c) for c in ALL_CELLS])
def test_head_verdict(results, cell):
    exp = _expect(cell, head=True)
    got = _get(results, cell, "head")
    assert _subset(got, exp) == exp, f"head {got} != intended {exp}"


@pytest.mark.parametrize("cell", ALL_CELLS, ids=[_cell_id(c) for c in ALL_CELLS])
def test_base_vs_head(results, cell):
    exp_base, exp_head = _expect(cell, head=False), _expect(cell, head=True)
    base = _get(results, cell, "base")
    head = _get(results, cell, "head")
    assert _subset(base, exp_base) == exp_base, f"base arm {base} != pre-PR model {exp_base}"
    if exp_base == exp_head:
        # SAME: the PR must not move this cell (full record compared, not just the subset).
        assert base == head, f"IDENTICAL expected: base {base} head {head}"
    else:
        # CHANGED as intended: base keeps the old verdict, head the new one.
        assert _subset(head, exp_head) == exp_head and base != head, \
            f"CHANGED expected: base {base} -> head {exp_head}, got head {head}"


def test_matrix_summary(results):
    """Prints the cell accounting; fails if a class is empty (a vacuous matrix)."""
    same = changed = 0
    for c in ALL_CELLS:
        if c[0] in ("setup.sh", "install.sh") and not results["_bash_ok"]:
            continue
        if _expect(c, False) == _expect(c, True):
            same += 1
        else:
            changed += 1
    print(f"\n[spoof-matrix 11965] host={HOST} bash={_bash()} cells={len(ALL_CELLS)} "
          f"same={same} intended-changed={changed}")
    assert changed > 0 and same > 0
