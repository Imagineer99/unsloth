import copy
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "windows_diag"))
import analyze  # noqa: E402

BASE_DOC = {
    "schema": 1,
    "tool_version": "t",
    "mode": "quick",
    "machine": {"os_caption": "Windows 11 Pro", "os_build": "26100", "os_arch": "AMD64", "ps_arch": "AMD64",
                "gpus": ["NVIDIA GeForce RTX 5090"], "nvidia_smi": True, "smi_cuda": "12.9", "smi_cc": ["12.0"],
                "elevated": False},
    "states": {"base": {"sha": "c807acbf44da", "verified": True}, "combined": {"sha": "3468c0037ec2", "verified": True}},
    "decisions": [
        {"state": "base", "shell": "powershell", "reached": True, "family": "cu128"},
        {"state": "combined", "shell": "powershell", "reached": True, "family": "cu128"},
    ],
    "tests": [
        {"state": "base", "shell": "pwsh", "kind": "ps1", "file": "a.ps1", "present": True, "passed": True},
        {"state": "combined", "shell": "pwsh", "kind": "ps1", "file": "a.ps1", "present": True, "passed": True},
    ],
    "probe": [{"state": "combined", "shell": "powershell", "available": True, "cuda": "12.9", "cc": ["12.0"]}],
    "presence": [{"state": "combined", "shell": "powershell", "available": True, "nvidia_present": True}],
    "smoke": [
        {"state": "base", "shell": "powershell", "parse_errors": 0, "help_exit": 0},
        {"state": "combined", "shell": "powershell", "parse_errors": 0, "help_exit": 0},
    ],
    "full": [],
    "restore": {"ok": True, "conflicts": [], "parked": {"was_parked": False, "restored": False}},
    "errors": [],
}


def doc(**over):
    d = copy.deepcopy(BASE_DOC)
    d.update(over)
    return d


def verdicts(d, area):
    cells, _ = analyze.analyze(d)
    return [c["verdict"] for c in cells if c["area"] == area]


def run(tmp_path, d):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(d))
    return analyze.main([str(p)])


def test_all_same_exits_zero(tmp_path):
    cells, harness = analyze.analyze(doc())
    assert harness == []
    assert {c["verdict"] for c in cells} == {"SAME"}
    assert run(tmp_path, doc()) == 0


def test_decision_gpu_to_cpu_is_regression(tmp_path):
    d = doc()
    d["decisions"][1]["family"] = "cpu"
    assert verdicts(d, "decisions") == ["REGRESSION"]
    assert run(tmp_path, d) == 1


def test_decision_cpu_to_gpu_is_expected_widen(tmp_path):
    d = doc()
    d["decisions"][0]["family"] = "cpu"
    assert verdicts(d, "decisions") == ["EXPECTED_WIDEN"]
    assert run(tmp_path, d) == 0


def test_decision_gpu_family_change_is_regression():
    d = doc()
    d["decisions"][1]["family"] = "cu126"
    assert verdicts(d, "decisions") == ["REGRESSION"]


def test_decision_not_reached_is_void():
    d = doc()
    d["decisions"][1]["reached"] = False
    assert verdicts(d, "decisions") == ["VOID"]
    d = doc()
    d["decisions"] = d["decisions"][1:]
    assert verdicts(d, "decisions") == ["VOID"]


def test_head_only_failing_test_is_regression():
    d = doc()
    d["tests"] = [{"state": "combined", "shell": "pwsh", "kind": "ps1", "file": "new.ps1", "present": True,
                   "passed": False, "failed_checks": ["x"]}]
    assert verdicts(d, "tests") == ["REGRESSION"]


def test_both_failing_is_same_preexisting():
    d = doc()
    for r in d["tests"]:
        r["passed"] = False
    cells, _ = analyze.analyze(d)
    t = [c for c in cells if c["area"] == "tests"]
    assert [c["verdict"] for c in t] == ["SAME"]
    assert "pre-existing" in t[0]["detail"]


def test_base_pass_head_fail_and_timeout():
    d = doc()
    d["tests"][1]["passed"] = False
    assert verdicts(d, "tests") == ["REGRESSION"]
    d = doc()
    d["tests"][1]["timed_out"] = True
    assert verdicts(d, "tests") == ["REGRESSION"]


def with_base_probe(d, **base):
    row = {"state": "base", "shell": "powershell", "available": True, "cuda": "12.9", "cc": ["12.0"]}
    row.update(base)
    d["probe"].insert(0, row)
    return d


def head_verdicts(d, area):
    cells, _ = analyze.analyze(d)
    return [c["verdict"] for c in cells if c["area"] == area and c["state"] != "base"]


def test_probe_mismatch_is_regression_when_base_reads_the_libraries():
    d = with_base_probe(doc())
    d["probe"][-1]["cc"] = ["8.9"]
    assert head_verdicts(d, "probe") == ["REGRESSION"]
    d = with_base_probe(doc())
    d["probe"][-1]["cuda"] = "12.9.1"
    assert head_verdicts(d, "probe") == ["SAME"]


def test_probe_mismatch_shared_with_base_is_not_a_regression():
    # Windows on ARM staging: no stand-in libraries, so neither arm reads anything.
    d = with_base_probe(doc(), cuda=None, cc=[])
    d["probe"][-1].update({"cuda": None, "cc": []})
    assert head_verdicts(d, "probe") == ["SAME"]
    d = doc()  # no base row at all: nothing to be worse than
    d["probe"][-1]["cc"] = ["8.9"]
    assert head_verdicts(d, "probe") == ["SAME"]


def test_nvidia_arm64_index_counts_as_a_gpu_route():
    d = doc()
    d["decisions"][0]["family"] = "nvidia-arm64"
    d["decisions"][1]["family"] = "cpu"
    assert verdicts(d, "decisions") == ["REGRESSION"]


def test_no_nvidia_host_voids_gpu_cells():
    d = doc()
    d["machine"]["nvidia_smi"] = False
    d["presence"][0]["nvidia_present"] = False
    assert verdicts(d, "probe") == ["VOID"]
    assert verdicts(d, "presence") == ["VOID"]


def test_presence_false_on_nvidia_is_regression():
    d = doc()
    d["presence"][0]["nvidia_present"] = False
    assert verdicts(d, "presence") == ["REGRESSION"]


def test_smoke_head_only_break_is_regression():
    d = doc()
    d["smoke"][1]["parse_errors"] = 2
    assert verdicts(d, "smoke") == ["REGRESSION"]


def test_restore_failure_exits_three(tmp_path):
    d = doc()
    d["restore"]["ok"] = False
    d["restore"]["failures"] = ["shortcut could not be put back"]
    _, harness = analyze.analyze(d)
    assert any("could not put back" in h for h in harness)
    assert run(tmp_path, d) == 3


def test_foreign_restore_conflict_is_a_note_not_a_finding(tmp_path):
    d = doc()
    d["restore"]["conflicts"] = ["user environment OneDrive changed during the run; left in place"]
    cells, harness = analyze.analyze(d)
    assert harness == []
    assert "left in place" in analyze.render(d, cells, harness)
    assert run(tmp_path, d) == 0


def test_unrestored_park_and_unverified_state_are_harness_findings():
    d = doc()
    d["restore"]["parked"] = {"was_parked": True, "restored": False}
    d["states"]["combined"]["verified"] = False
    _, harness = analyze.analyze(d)
    assert len(harness) == 2


def test_regression_beats_harness_exit(tmp_path):
    d = doc()
    d["restore"]["ok"] = False
    d["decisions"][1]["family"] = "cpu"
    assert run(tmp_path, d) == 1


def full_row(state, matmul = True):
    return {"state": state, "install_exit": 0, "family": "cu128",
            "torch": {"version": "2.9.0", "cuda_available": True, "device": "RTX 5090", "matmul_ok": matmul},
            "health_ok": True, "update_exit": 0, "shortcuts": {"ok": True, "count": 2, "dangling": 0},
            "shortcuts_removed": True}


def test_full_matmul_regression(tmp_path):
    d = doc(mode = "full", full = [full_row("base"), full_row("combined", matmul = False)])
    cells, _ = analyze.analyze(d)
    bad = [c for c in cells if c["verdict"] == "REGRESSION"]
    assert len(bad) == 1 and bad[0]["detail"].startswith("matmul")
    assert run(tmp_path, d) == 1


def test_full_without_base_is_void():
    d = doc(mode = "full", full = [full_row("combined")])
    assert set(verdicts(d, "full")) == {"VOID"}


@pytest.mark.parametrize("kind", ["zip", "dir"])
def test_zip_and_directory_inputs(tmp_path, kind):
    d = doc()
    d["decisions"][1]["family"] = "cpu"
    if kind == "zip":
        src = tmp_path / "unsloth-diag-box-1.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("unsloth-diag-box-1/results.json", json.dumps(d))
            z.writestr("unsloth-diag-box-1/transcripts/x/results.json", json.dumps(doc()))
    else:
        src = tmp_path / "out"
        (src / "inner").mkdir(parents = True)
        (src / "inner" / "results.json").write_text(json.dumps(d))
    out, js = tmp_path / "s.md", tmp_path / "s.json"
    assert analyze.main([str(src), "--out", str(out), "--json", str(js)]) == 1
    assert "| combined | powershell | REGRESSION |" in out.read_text()
    assert json.loads(js.read_text())["cells"]


def test_render_lists_unproven_areas():
    d = doc()
    d["machine"]["nvidia_smi"] = False
    cells, harness = analyze.analyze(d)
    md = analyze.render(d, cells, harness)
    assert "full pass not run" in md and "no NVIDIA GPU" in md and "not elevated" in md


def test_spoofed_host_without_smi_uses_the_spoof_as_probe_truth():
    d = doc()
    d["machine"].update({"nvidia_smi": False, "smi_cuda": None, "smi_cc": [], "spoof": "x64-libs-only",
                         "expect_cuda": "13.0", "expect_cc": ["12.0"], "nvidia_ven_adapters": 0})
    with_base_probe(d)
    for r in d["probe"]:
        r.update({"available": True, "cuda": "13.0", "cc": ["12.0"]})
    assert set(verdicts(d, "probe")) == {"SAME"}
    d["probe"][-1]["cc"] = ["8.9"]
    assert "REGRESSION" in verdicts(d, "probe")


def test_presence_is_judged_against_wmi_adapters_not_smi():
    d = doc()
    d["machine"]["nvidia_ven_adapters"] = 0
    for r in d["presence"]:
        r.update({"available": True, "nvidia_present": False})
    assert set(verdicts(d, "presence")) == {"VOID"}
    d["presence"][-1]["nvidia_present"] = True
    assert "REGRESSION" in verdicts(d, "presence")
    d["machine"]["nvidia_ven_adapters"] = 1
    d["presence"][-1]["nvidia_present"] = False
    assert "REGRESSION" in verdicts(d, "presence")


def llama_rows(base_kind, head_kind):
    return [{"state": "base", "install_kind": base_kind, "backend": "x"},
            {"state": "combined", "install_kind": head_kind, "backend": "x"}]


@pytest.mark.parametrize("base_kind, head_kind, want", [
    ("windows-vulkan", "windows-vulkan", "SAME"),
    ("windows-vulkan", "windows-cpu", "REGRESSION"),
    ("windows-cuda", "windows-vulkan", "REGRESSION"),
    ("windows-cpu", "windows-vulkan", "EXPECTED_WIDEN"),
    ("windows-vulkan", "windows-cuda", "EXPECTED_WIDEN"),
    ("windows-rocm", "windows-hip", "REGRESSION"),
])
def test_llama_bundle_verdicts(base_kind, head_kind, want):
    assert verdicts(doc(llama = llama_rows(base_kind, head_kind)), "llama") == [want]


def test_llama_without_an_answer_is_void():
    rows = [{"state": "base", "install_kind": None, "error": "offline"}, {"state": "combined", "install_kind": "windows-vulkan"}]
    assert verdicts(doc(llama = rows), "llama") == ["VOID"]


def test_render_prints_timing_and_the_base_bundle():
    d = doc(llama = llama_rows("windows-vulkan", "windows-vulkan"), timing = {"tests_s": 300.0, "parallel": 4})
    md = analyze.render(d, *analyze.analyze(d))
    assert "llama.cpp bundle at base: windows-vulkan" in md and "tests_s 300.0" in md


def av_doc(base_hits = (), head_hits = (), head_missing = 0):
    blocks = [{"label": f"decision_base_{i}", "line": "blocked by your antivirus"} for i in base_hits]
    blocks += [{"label": f"ps1_combined_pwsh_{i}", "line": "ScriptContainedMaliciousContent"} for i in head_hits]
    states = {"base": {"missing": 0, "changed_pinned": []},
              "combined": {"missing": head_missing, "missing_sample": ["studio/x.py"], "changed_pinned": []}}
    return doc(av = {"products": ["Bitdefender Antivirus"], "blocks": blocks, "states": states, "events": 1})


@pytest.mark.parametrize("base_hits, head_hits, head_missing, want", [
    ((), (), 0, "SAME"),
    ((), ("a",), 0, "REGRESSION"),
    ((), (), 3, "REGRESSION"),
    (("a",), (), 0, "IMPROVED"),
    (("a",), ("b",), 0, "INFO"),
])
def test_av_area_compares_head_with_base(base_hits, head_hits, head_missing, want):
    assert verdicts(av_doc(base_hits, head_hits, head_missing), "av") == [want]


def test_av_blocked_rows_are_not_code_regressions():
    d = av_doc((), ("a",))
    d["tests"][1] = dict(d["tests"][1], passed = False, av_block = "ScriptContainedMaliciousContent")
    d["decisions"][1] = dict(d["decisions"][1], reached = False, av_block = "blocked by your antivirus")
    assert verdicts(d, "tests") == ["AV_BLOCKED"]
    assert verdicts(d, "decisions") == ["AV_BLOCKED"]
    assert verdicts(d, "av") == ["REGRESSION"]


def test_no_av_section_means_no_av_cells():
    assert verdicts(doc(), "av") == []


def test_a_file_refused_on_write_counts_against_that_state():
    d = av_doc()
    d["av"]["states"]["combined"]["blocked_on_write"] = ["scripts/x.py (Access to the path is denied.)"]
    assert verdicts(d, "av") == ["REGRESSION"]
    d["av"]["states"]["base"]["blocked_on_write"] = ["scripts/x.py (Access to the path is denied.)"]
    assert verdicts(d, "av") == ["INFO"]
