#!/usr/bin/env python3
"""Turn an unsloth-win-diag results zip into per-area verdicts.

Usage: python analyze.py <zip | dir | results.json> [--out FILE] [--json FILE]
Exit: 1 any REGRESSION, 3 harness finding only, else 0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

HEADS = ("stack", "presence", "combined")
GPU_FAMILY = re.compile(r"^(cu\d+|rocm.*|xpu)$", re.I)


def load(path: Path) -> dict:
    if path.is_dir():
        hits = sorted(path.rglob("results.json"))
        if not hits:
            raise SystemExit(f"no results.json under {path}")
        return json.loads(hits[0].read_text(encoding = "utf-8-sig"))
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "results.json"]
            if not names:
                raise SystemExit(f"no results.json in {path}")
            return json.loads(z.read(sorted(names, key = len)[0]).decode("utf-8-sig"))
    return json.loads(path.read_text(encoding = "utf-8-sig"))


def is_gpu(family) -> bool:
    return bool(family) and bool(GPU_FAMILY.match(str(family)))


def index(rows, *keys):
    return {tuple(r.get(k) for k in keys): r for r in rows or []}


def cell(area, state, shell, verdict, detail):
    return {"area": area, "state": state, "shell": shell, "verdict": verdict, "detail": detail}


def majmin(v) -> str | None:
    m = re.match(r"\s*(\d+)\.(\d+)", str(v or ""))
    return f"{m.group(1)}.{m.group(2)}" if m else None


def judge_decisions(d, cells):
    rows = index(d.get("decisions"), "state", "shell")
    for (state, shell), head in rows.items():
        if state not in HEADS:
            continue
        base = rows.get(("base", shell))
        if not base or not base.get("reached") or not head.get("reached"):
            who = "base" if not base or not base.get("reached") else "head"
            cells.append(cell("decisions", state, shell, "VOID", f"{who} never reached the torch decision"))
            continue
        bf, hf = base.get("family"), head.get("family")
        detail = f"base={bf} head={hf}"
        if bf == hf:
            verdict = "SAME"
        elif not is_gpu(bf) and is_gpu(hf):
            verdict, detail = "EXPECTED_WIDEN", detail + " (review: head found a GPU route base did not)"
        elif is_gpu(bf) and not is_gpu(hf):
            verdict = "REGRESSION"
        elif is_gpu(bf) and is_gpu(hf):
            verdict = "REGRESSION"
        else:
            verdict = "SAME"
        cells.append(cell("decisions", state, shell, verdict, detail))
        if head.get("path_warn") and not base.get("path_warn"):
            cells.append(cell("decisions", state, shell, "INFO",
                              "head printed the path-exactness warning (expected only when elevated or with no trusted Python)"))


def judge_tests(d, cells):
    rows = index(d.get("tests"), "state", "shell", "kind", "file")

    def failed(r):
        return bool(r) and r.get("present", True) and (not r.get("passed") or r.get("timed_out"))

    for (state, shell, kind, file), head in rows.items():
        if state not in HEADS or not head.get("present", True):
            continue
        base = rows.get(("base", shell, kind, file))
        base_present = bool(base) and base.get("present", True)
        checks = ", ".join(head.get("failed_checks") or []) or (
            "timed out" if head.get("timed_out") else f"exit {head.get('exit')}")
        if not failed(head):
            verdict, detail = "SAME", "passed"
        elif not base_present:
            verdict, detail = "REGRESSION", f"head-only test fails: {checks}"
        elif failed(base):
            verdict, detail = "SAME", f"fails on base too (pre-existing): {checks}"
        else:
            verdict, detail = "REGRESSION", f"passes on base, fails on head: {checks}"
        cells.append(cell("tests", state, shell, verdict, f"{kind} {file}: {detail}"))


def judge_probe(d, cells):
    m = d.get("machine") or {}
    for r in d.get("probe") or []:
        state, shell = r.get("state"), r.get("shell")
        # Ground truth: nvidia-smi, or on a spoofed staging host without one, what the planted
        # stand-in libraries were told to report.
        if m.get("nvidia_smi"):
            want_cuda, want_cc, truth = majmin(m.get("smi_cuda")), sorted(set(m.get("smi_cc") or [])), "nvidia-smi"
        elif m.get("expect_cuda"):
            want_cuda, want_cc, truth = majmin(m.get("expect_cuda")), sorted(set(m.get("expect_cc") or [])), "spoof"
        else:
            cells.append(cell("probe", state, shell, "VOID", "no NVIDIA GPU (nvidia-smi) on this host"))
            continue
        if not r.get("available"):
            cells.append(cell("probe", state, shell, "N/A", r.get("error") or "probe function absent in this state"))
            continue
        got_cuda, got_cc = majmin(r.get("cuda")), sorted(set(r.get("cc") or []))
        detail = f"probe cuda={got_cuda} cc={got_cc}; {truth} cuda={want_cuda} cc={want_cc}"
        ok = got_cuda == want_cuda and got_cc == want_cc
        verdict = "SAME" if ok else ("REGRESSION" if state in HEADS else "INFO")
        cells.append(cell("probe", state, shell, verdict, detail))


def judge_presence(d, cells):
    m = d.get("machine") or {}
    # The scan reads WMI for a healthy VEN_10DE adapter, so that is its ground truth; older result
    # files without the count fall back to nvidia-smi.
    adapters = m.get("nvidia_ven_adapters")
    nvidia = bool(adapters) if adapters is not None else bool(m.get("nvidia_smi"))
    for r in d.get("presence") or []:
        state, shell = r.get("state"), r.get("shell")
        if not r.get("available"):
            cells.append(cell("presence", state, shell, "N/A", r.get("error") or "presence scan absent in this state"))
        elif not nvidia:
            if r.get("nvidia_present"):
                cells.append(cell("presence", state, shell, "REGRESSION", "the scan claims an NVIDIA adapter WMI does not list"))
            else:
                cells.append(cell("presence", state, shell, "VOID", "no NVIDIA PCI adapter in WMI on this host (scan agreed)"))
        elif r.get("nvidia_present") is False:
            cells.append(cell("presence", state, shell, "REGRESSION", "NVIDIA host but the adapter scan found no NVIDIA adapter"))
        else:
            cells.append(cell("presence", state, shell, "SAME", f"nvidia_present={r.get('nvidia_present')}"))


def judge_smoke(d, cells):
    rows = index(d.get("smoke"), "state", "shell")

    def bad(r):
        return (r.get("parse_errors") or 0) > 0 or r.get("help_exit") not in (0, None)

    for (state, shell), head in rows.items():
        if state not in HEADS:
            continue
        base = rows.get(("base", shell))
        detail = f"parse_errors={head.get('parse_errors')} help_exit={head.get('help_exit')}"
        if not bad(head):
            verdict = "SAME"
        elif base and bad(base):
            verdict, detail = "SAME", detail + " (base too)"
        else:
            verdict = "REGRESSION"
        cells.append(cell("smoke", state, shell, verdict, detail))


FULL_STEPS = (
    ("install", lambda r: None if r.get("install_exit") is None else r["install_exit"] == 0 and not r.get("install_timed_out")),
    ("cuda_available", lambda r: (r.get("torch") or {}).get("cuda_available")),
    ("matmul", lambda r: (r.get("torch") or {}).get("matmul_ok")),
    ("health", lambda r: r.get("health_ok")),
    ("update", lambda r: None if r.get("update_exit") is None else r["update_exit"] == 0),
    ("shortcuts", lambda r: (r.get("shortcuts") or {}).get("ok")),
    ("shortcuts_removed", lambda r: r.get("shortcuts_removed")),
)


def judge_full(d, cells):
    rows = {r.get("state"): r for r in d.get("full") or []}
    base = rows.get("base")
    for state, head in rows.items():
        if state not in HEADS:
            continue
        for step, get in FULL_STEPS:
            b, h = (get(base) if base else None), get(head)
            if b is None or h is None:
                verdict = "VOID"
            elif b == h:
                verdict = "SAME"
            elif b and not h:
                verdict = "REGRESSION"
            else:
                verdict = "EXPECTED_WIDEN"
            cells.append(cell("full", state, "-", verdict, f"{step}: base={b} head={h}"))


def harness_findings(d) -> list[str]:
    out = []
    r = d.get("restore") or {}
    if r and not r.get("ok", True):
        out.append("restore did not complete cleanly")
    for c in r.get("failures") or []:
        out.append(f"restore could not put back: {c}")
    parked = r.get("parked") or {}
    if parked.get("was_parked") and not parked.get("restored"):
        out.append("an existing Unsloth install was parked and NOT restored (run the script with -Recover)")
    for s, v in (d.get("states") or {}).items():
        if v and v.get("verified") is False:
            out.append(f"state {s} failed source verification")
    out.extend(f"error: {e}" for e in d.get("errors") or [])
    return out


def not_proven(d, cells) -> list[str]:
    m, mode, out = d.get("machine") or {}, d.get("mode"), []
    if m.get("spoof"):
        out.append(f"GPU SPOOFED ({m.get('spoof')}): stand-in NVIDIA binaries, no real driver; routes and probe parsing are exercised, real CUDA is not")
    elif not m.get("nvidia_smi"):
        out.append("no NVIDIA GPU (nvidia-smi) on this host: GPU probe/presence cells are VOID")
    if mode != "full" and not d.get("full"):
        out.append("full pass not run: real install, torch/GPU, Studio health, update, shortcuts, uninstall")
    if not m.get("elevated"):
        out.append("not elevated: elevated installer behaviour not exercised")
    voids = sum(1 for c in cells if c["verdict"] == "VOID")
    if voids:
        out.append(f"{voids} VOID cell(s): the base or head arm produced no usable result")
    return out


def analyze(d: dict) -> tuple[list[dict], list[str]]:
    cells: list[dict] = []
    for f in (judge_decisions, judge_tests, judge_probe, judge_presence, judge_smoke, judge_full):
        f(d, cells)
    return cells, harness_findings(d)


def render(d, cells, harness) -> str:
    m = d.get("machine") or {}
    lines = [
        f"# Unsloth Windows diagnostic ({d.get('mode')}, tool {d.get('tool_version')})",
        "",
        f"- OS: {m.get('os_caption')} build {m.get('os_build')} ({m.get('os_arch')}, PowerShell {m.get('ps_arch')}), elevated={m.get('elevated')}",
        f"- GPUs: {', '.join(m.get('gpus') or []) or 'none'}; nvidia-smi={m.get('nvidia_smi')} cuda={m.get('smi_cuda')} cc={m.get('smi_cc')}",
        "- States: " + ", ".join(f"{k}={str((v or {}).get('sha') or '')[:9]}" + ("" if (v or {}).get("verified", True) else " (UNVERIFIED)")
                                   for k, v in (d.get("states") or {}).items()),
        "",
        "## Harness",
        "",
    ]
    lines += [f"- {h}" for h in harness] or ["- clean (restore ok)"]
    foreign = (d.get("restore") or {}).get("conflicts") or []
    lines += [f"- note, changed by something else during the run and left in place: {c}" for c in foreign]
    counts = {}
    for c in cells:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    lines += ["", "## Totals", "", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no cells"]
    for area in ("decisions", "tests", "probe", "presence", "smoke", "full"):
        rows = [c for c in cells if c["area"] == area]
        if not rows:
            continue
        lines += ["", f"## {area}", "", "| state | shell | verdict | detail |", "| --- | --- | --- | --- |"]
        order = {"REGRESSION": 0, "VOID": 1, "EXPECTED_WIDEN": 2, "INFO": 3, "N/A": 4, "SAME": 5}
        for c in sorted(rows, key = lambda c: (order.get(c["verdict"], 9), c["state"], c["shell"])):
            detail = str(c["detail"]).replace("|", "\\|")
            lines.append(f"| {c['state']} | {c['shell']} | {c['verdict']} | {detail} |")
    lines += ["", "## Not proven on this machine", ""]
    lines += [f"- {x}" for x in not_proven(d, cells)] or ["- nothing listed"]
    return "\n".join(lines) + "\n"


def main(argv = None) -> int:
    ap = argparse.ArgumentParser(description = __doc__.splitlines()[0])
    ap.add_argument("input", type = Path)
    ap.add_argument("--out", type = Path)
    ap.add_argument("--json", type = Path)
    a = ap.parse_args(argv)
    d = load(a.input)
    cells, harness = analyze(d)
    md = render(d, cells, harness)
    sys.stdout.write(md)
    if a.out:
        a.out.write_text(md, encoding = "utf-8")
    if a.json:
        a.json.write_text(json.dumps({"cells": cells, "harness": harness}, indent = 2), encoding = "utf-8")
    if any(c["verdict"] == "REGRESSION" for c in cells):
        return 1
    return 3 if harness else 0


if __name__ == "__main__":
    sys.exit(main())
