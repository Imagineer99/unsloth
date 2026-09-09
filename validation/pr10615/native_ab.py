"""Owned-canary native A/B probe. Invoke from checkout root with --side and --area."""
import argparse
import dataclasses
import csv
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import tempfile
import time
from unittest.mock import patch

PINS = {
    "base": "4b98ad5e507b30b1dc0aa40605d611f5704a4843",
    "head": "a9bf18d5e7dc83d7b865e3b1a09cdd4adc0ab81c",
}
parser = argparse.ArgumentParser()
parser.add_argument("--side", choices=PINS, required=True)
parser.add_argument("--area", type=Path, required=True)
args = parser.parse_args()
repo = Path.cwd().resolve()
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PINS[args.side]
area = args.area.resolve()
area.mkdir(parents=True, exist_ok=False)  # refuse reused canaries/homes
scratch = area / "scratch"
scratch.mkdir()
for variable in ("TMPDIR", "TEMP", "TMP"):
    os.environ[variable] = str(scratch)
tempfile.tempdir = str(scratch)
os.environ["UNSLOTH_STUDIO_HOME"] = str(area / "studio-home")
sys.path.insert(0, str(repo / "studio/backend"))
from core.inference import os_sandbox, tools

results = []
def record(name, **fields):
    row = {"test": name, "side": args.side, "sha": PINS[args.side], "platform": sys.platform, **fields}
    results.append(row)
    print(json.dumps(row), flush=True)
    (area / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

work = area / "work space"
work.mkdir()
canary = area / "outside.txt"
canary.write_text("OWNED_CANARY", encoding="utf-8")
if sys.platform == "win32":
    # SRT is a separate account, not a deny-all overlay on world-readable files.
    # Make this owned private-file canary independent of hosted-runner default ACLs.
    identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip()
    sid = next(csv.reader([identity]))[1]
    subprocess.run(["icacls", str(canary), "/inheritance:r", "/grant:r", "*" + sid + ":(F)"],
                   check=True, capture_output=True, text=True)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(16)
listener.settimeout(0.2)
port = listener.getsockname()[1]
stopping = threading.Event()
def accept_connections():
    while not stopping.is_set():
        try:
            connection, _ = listener.accept()
            connection.close()
        except socket.timeout:
            continue
        except OSError:
            break
threading.Thread(target=accept_connections, daemon=True).start()

payload = work / "owned_probe.py"
payload.write_text(
    "import json, socket, sys\n"
    "from pathlib import Path\n"
    "outside=Path(sys.argv[1]); result={}\n"
    "try: result['read']=outside.read_text()\n"
    "except OSError: result['read']='denied'\n"
    "try: outside.write_text('MODIFIED'); result['write']='allowed'\n"
    "except OSError: result['write']='denied'\n"
    "Path('inside.txt').write_text('inside-ok')\n"
    "result['inside']=Path('inside.txt').read_text()\n"
    "try:\n"
    " s=socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2); s.close(); result['network']='allowed'\n"
    "except OSError: result['network']='denied'\n"
    "print('PROBE_JSON='+json.dumps(result), flush=True)\n",
    encoding="utf-8",
)

def argv_for(kind):
    child = [sys.executable, str(payload), str(canary), str(port)]
    if kind == "python":
        return tuple(child)
    if sys.platform == "win32":
        shell = tools._windows_bash()
        assert shell, "Git bash required on this hosted runner; do not substitute an unsupported shell"
        # Git bash accepts drive:/ paths; quoted backslash paths are not native bash paths.
        command = "exec " + shlex.join([item.replace(chr(92), "/") for item in child])
        return (shell, "--noprofile", "--norc", "-c", command)
    shell = shutil.which("bash")
    assert shell, "Missing bash positive-control shell"
    return (shell, "-c", "exec " + shlex.join(child))

def plan_for(mode, kind="python"):
    return os_sandbox.ToolLaunchPlan(
        argv=argv_for(kind), workdir=str(work),
        env=tools._build_safe_env(str(work)),
        preexec_fn=os.setsid if os.name != "nt" else None,
        requested_mode=mode, execution_kind=kind, timeout_seconds=20,
    )

def close_group_handle(group):
    if isinstance(group, tuple):
        if group[0] == "srt-tree":
            close_group_handle(group[1])
        elif group[0] == "windows-job":
            group[1].close()

def launch(prepared):
    proc = None
    group = None
    try:
        proc = os_sandbox.spawn_prepared_launch(
            prepared,
            **tools._apply_prepared_launch(prepared, {
                "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
            }),
        )
        group = tools._capture_process_group(proc)
        out, err = proc.communicate(timeout=45)
        assert proc.returncode == 0, (proc.returncode, out, err)
        if hasattr(os_sandbox, "verify_prepared_success"):
            os_sandbox.verify_prepared_success(prepared, proc)
        lines = [line.removeprefix("PROBE_JSON=") for line in out.splitlines() if line.startswith("PROBE_JSON=")]
        assert len(lines) == 1, (out, err)
        return json.loads(lines[0])
    finally:
        if proc is not None and proc.poll() is None:
            tools._killpg_captured(group)
            tools._kill_process_tree(proc)
            proc.communicate(timeout=10)
        prepared.cleanup()
        close_group_handle(group)
        assert not getattr(prepared, "cleanup_diagnostics", []), prepared.cleanup_diagnostics

def assert_refused(name, plan):
    # A spy fails if planner ever starts a workload: failure-path proof is bounded to preparation.
    with patch.object(os_sandbox, "spawn_prepared_launch", side_effect=AssertionError("unexpected spawn")) as spawn:
        try:
            tools._prepare_tool_launch(plan)
        except os_sandbox.SandboxUnavailableError:
            pass
        else:
            raise AssertionError(name + " unexpectedly prepared host execution")
        assert spawn.call_count == 0
    record(name, outcome="refused-before-spawn")


def timeout_tree():
    ready = work / "child-ready.txt"
    escaped = work / "child-survived.txt"
    child_code = (
        "from pathlib import Path; import time; "
        "Path('child-ready.txt').write_text('ready'); "
        "time.sleep(3); Path('child-survived.txt').write_text('survived')"
    )
    parent_code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', " + repr(child_code) + "]); time.sleep(30)"
    plan = dataclasses.replace(plan_for("auto"), argv=(sys.executable, "-c", parent_code))
    prepared = tools._prepare_tool_launch(plan)
    proc = None
    group = None
    try:
        proc = os_sandbox.spawn_prepared_launch(
            prepared, **tools._apply_prepared_launch(prepared, {
                "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
            }),
        )
        group = tools._capture_process_group(proc)
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        assert ready.exists(), "Child positive control did not start"
        try:
            proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("Expected owned timeout workload still running")
        tools._killpg_captured(group)
        tools._kill_process_tree(proc)
        proc.communicate(timeout=10)
    finally:
        if proc is not None and proc.poll() is None:
            tools._killpg_captured(group)
            tools._kill_process_tree(proc)
            proc.communicate(timeout=10)
        prepared.cleanup()
        close_group_handle(group)
        assert not getattr(prepared, "cleanup_diagnostics", []), prepared.cleanup_diagnostics
    time.sleep(3.5)
    assert not escaped.exists(), "Owned child survived production timeout cleanup"
    record("timeout-child-cleanup", outcome="pass", child_started=True, survivor_marker=False)

try:
    cap = os_sandbox.capability_snapshot(force=True)
    expected_native = not (sys.platform == "win32" and args.side == "base")
    record("capability", available=cap.available, backend=cap.backend, reason=str(cap.reason))
    assert cap.available == expected_native, "Native setup/probe failed; not evidence of confinement"
    for kind in ("python", "terminal"):
        # Full executes first: permissions, runtime, script and loopback must work outside isolation.
        for mode in ("full", "auto", "required"):
            if not expected_native and mode == "required":
                assert_refused("windows-base-required-" + kind, plan_for(mode, kind))
                continue
            canary.write_text("OWNED_CANARY", encoding="utf-8")
            prepared = tools._prepare_tool_launch(plan_for(mode, kind))
            actual = launch(prepared)
            isolated = mode != "full" and expected_native
            assert actual["inside"] == "inside-ok", actual
            assert actual["read"] == ("denied" if isolated else "OWNED_CANARY"), actual
            assert actual["write"] == ("denied" if isolated else "allowed"), actual
            assert canary.read_text() == ("OWNED_CANARY" if isolated else "MODIFIED")
            assert actual["network"] == ("denied" if isolated and sys.platform == "win32" else "allowed"), actual
            assert prepared.execution_record.os_isolation == isolated
            record("native-" + kind + "-" + mode, outcome="pass", observed=actual,
                   execution=prepared.execution_record.as_dict())

    unavailable = dataclasses.replace(cap, available=False, reason="controlled unavailable")
    with patch.object(os_sandbox, "capability_snapshot", return_value=unavailable):
        assert_refused("required-unavailable", plan_for("required"))
        prepared = tools._prepare_tool_launch(plan_for("auto"))
        try:
            assert not prepared.execution_record.os_isolation
            record("auto-unavailable", outcome="software-fallback-selected")
        finally:
            prepared.cleanup()

    with patch.object(os_sandbox, "prepare_tool_launch", side_effect=ImportError("controlled planner failure")):
        if args.side == "head":
            assert_refused("planner-failure", plan_for("auto"))
        else:
            prepared = tools._prepare_tool_launch(plan_for("auto"))
            try:
                assert not prepared.execution_record.os_isolation
                record("planner-failure", outcome="base-software-fallback-selected")
            finally:
                prepared.cleanup()

    if args.side == "head":
        cancelled = threading.Event()
        cancelled.set()
        assert_refused("prelaunch-cancel", dataclasses.replace(plan_for("auto"), cancel_event=cancelled))
        # Fail the chosen backend builder after capability selection. A host marker must never appear.
        backend_name = {"linux": "sandbox_linux", "darwin": "sandbox_macos", "win32": "sandbox_windows"}[sys.platform]
        import importlib
        backend = importlib.import_module("core.inference." + backend_name)
        with patch.object(backend, "prepare", side_effect=OSError("controlled backend preparation failure")):
            assert_refused("backend-preparation-failure", plan_for("auto"))
    timeout_tree()
    record("complete", outcome="pass")
finally:
    stopping.set()
    listener.close()
