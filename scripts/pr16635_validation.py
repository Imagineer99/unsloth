# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Pinned CLI A/B proof. Real native terminals/HTTP; simulated model execution.

No models, accounts, credentials, screenshots or artifacts are uploaded. Results
are printed to Actions logs. Each child gets a distinct home and model cache.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET

BASE = "f22d2cfb153516bc3e2c69ffaa6519341f32e7ca"
HEAD = "1354c9db47ef77f0630969f8d3291918e6a6c574"
SCRIPT = Path(__file__).resolve()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def run(command, *, cwd=None, env=None, timeout=90):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            encoding="utf-8", errors="replace", capture_output=True,
                            timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n"
                           f"{result.stdout}\n{result.stderr}")
    return result.stdout


def isolated_env(source, root):
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("FORCE_COLOR", "CLICOLOR_FORCE", "UNSLOTH_API_KEY", "UNSLOTH_STUDIO_URL",
                "PYTHONHOME", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    env.update({
        "PYTHONPATH": str(source), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "UNSLOTH_HOME": str(root / "unsloth"),
        "UNSLOTH_STUDIO_HOME": str(root / "unsloth" / "web-ui"),
        "HF_HOME": str(root / "hf"), "HF_HUB_CACHE": str(root / "hf" / "hub"),
        "HF_XET_CACHE": str(root / "hf" / "xet"), "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "UNSLOTH_NO_TORCH": "1",
        "NO_COLOR": "1", "TERM": "xterm", "TMPDIR": str(root),
        "TEMP": str(root), "TMP": str(root),
    })
    return env


def assert_source(module, source):
    actual = Path(module.__file__).resolve()
    assert actual.is_relative_to(source), f"wrong import: {actual}; expected {source}"


def case_child(args):
    import faulthandler
    faulthandler.dump_traceback_later(12, exit=True)
    print("CHILD_STAGE importing-cli", flush=True)
    import typer
    import socketserver
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import unsloth_cli.commands.start as start
    from unsloth_cli import _model_catalog as catalog

    source = Path(args.source).resolve()
    assert_source(start, source)
    print("CHILD_STAGE imported-cli", flush=True)
    model_path = Path(args.result).parent / "models with spaces" / "tiny-模型-ü.gguf"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"GGUF-placeholder-not-executed")
    target = str(model_path)
    resident = {"id": target, "loaded": True, "context_length": 4096}
    state = {"models": [resident] if args.scenario == "loaded" else [],
             "loads": [], "starts": [], "inventory_calls": 0,
             "stdin_tty": sys.stdin.isatty(), "stdout_tty": sys.stdout.isatty()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            if self.path.startswith("/api/inference/loaded-models"):
                value = {"data": state["models"]}
            elif self.path.startswith("/api/inference/status"):
                value = ({"yours": False} if args.scenario == "owner" else
                         {"is_gguf": True, "model_identifier": target,
                          "active_model": target} if state["models"] else {})
            else:
                raise AssertionError(f"unexpected GET: {self.path}")
            self.reply(value)

        def do_POST(self):
            assert self.path == "/api/inference/load", self.path
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["loads"].append(payload)
            assert payload["model_path"] == target, payload
            state["models"] = [resident]
            self.reply({"status": "loaded", "model": target, "display_name": target})

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class LoopbackServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer normally reverse-resolves its address here. The hosted
            # macOS runner can block there; this fixture only serves loopback.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.server_address[1]

    server = LoopbackServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("CHILD_STAGE local-api-ready", flush=True)
    base = f"http://127.0.0.1:{server.server_port}"
    absent = args.scenario in ("auto", "no_serve", "no_launch")
    start.find_studio_server = lambda: None if absent else base
    start.verify_studio_identity = lambda unused: True
    start._agent_api_key = lambda *a, **kw: "disposable-test-key"
    start._connection_entry = lambda *a: None
    if args.scenario == "remote":
        # Model a remote identity while keeping all test transport on loopback.
        start.is_loopback_url = lambda unused: False

    def inventory():
        state["inventory_calls"] += 1
        return [catalog.ModelEntry("Downloaded", "OtherModel", "", "org/OtherModel"),
                catalog.ModelEntry("Downloaded", "Tiny-GGUF", "gguf", target)]
    catalog.list_chat_models = inventory

    def auto_start(expected, model, load, options):
        state["starts"].append(model)
        assert model == target
        state["models"] = [resident]
        return base, object()
    start._start_studio_server = auto_start

    code = 0
    try:
        print("CHILD_STAGE resolving-model", flush=True)
        if args.scenario == "chat":
            from rich.console import Console
            import unsloth_cli.commands.chat as chat
            assert_source(chat, source)
            state["selected"] = chat._pick_model(Console())
            assert state["selected"] == target
        else:
            kwargs = {"serve": args.scenario != "no_serve",
                      "launch": args.scenario != "no_launch"}
            if args.scenario == "gguf":
                kwargs["gguf_only"] = True
            result = start._connect(None, target if args.scenario == "explicit" else None,
                                    **kwargs)
            state["selected"] = result[2]["id"]
    except typer.Exit as error:
        code = error.exit_code
        state["exception"] = "Exit"
    except BaseException as error:
        code = 3
        state["exception"] = type(error).__name__
        state["traceback"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        server.shutdown()
        faulthandler.cancel_dump_traceback_later()
        state.update({"exit": code, "source": str(source), "target": target})
        Path(args.result).write_text(json.dumps(state, indent=2), encoding="utf-8")
        print("CASE_COMPLETE", flush=True)
    return code


def inventory_child(args):
    import builtins
    import faulthandler
    faulthandler.dump_traceback_later(25, exit=True)
    from unsloth_cli._inference import ensure_studio_backend_path
    ensure_studio_backend_path()
    original = builtins.__import__
    def without_server(name, *a, **kw):
        if name in ("fastapi", "routes", "torch") or name.startswith(("fastapi.", "routes.", "torch.")):
            raise AssertionError(f"unexpected heavyweight import: {name}")
        return original(name, *a, **kw)
    builtins.__import__ = without_server
    from hub.services.models import cache_inventory, catalog_classification, local_inventory
    from unsloth_cli import _model_catalog as catalog
    assert_source(catalog, Path(args.source).resolve())
    cache_inventory.all_hf_cache_scans = lambda: []
    cache_inventory._scan_cached_gguf = lambda **kwargs: []
    cache_inventory._scan_cached_models = lambda **kwargs: []
    assert catalog.cached_entries() == []
    async def empty_models(unused):
        return type("Response", (), {"models": []})()
    local_inventory.list_local_models_response = empty_models
    assert catalog.local_folder_entries() == []
    assert catalog_classification
    faulthandler.cancel_dump_traceback_later()
    print("INVENTORY_IMPORT_PASS", flush=True)
    return 0


def terminal(command, cwd, env, answer):
    """Real native PTY/ConPTY; never replace isatty or input."""
    deadline = time.monotonic() + 20
    output = ""
    sent = False
    if os.name == "nt":
        from winpty import PtyProcess
        from winpty.enums import Backend
        child = PtyProcess.spawn(command, cwd=str(cwd), env=env,
                                 dimensions=(40, 180), backend=Backend.ConPTY)
        child.fileobj.settimeout(0.1)
        try:
            while time.monotonic() < deadline:
                try:
                    output += child.read(65536)
                except (TimeoutError, OSError):
                    pass
                except EOFError:
                    break
                plain = ANSI.sub("", output)
                if not sent and ("Load [" in plain or "Chat with [" in plain):
                    child.write(answer.replace("\n", "\r"))
                    sent = True
                if "CASE_COMPLETE" in plain:
                    break
            else:
                raise AssertionError(f"ConPTY timeout:\n{output}")
        finally:
            child.close(force=True)
    else:
        import pty
        master, slave = pty.openpty()
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                 stderr=slave, start_new_session=True)
        os.close(slave)
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output += data.decode("utf-8", errors="replace")
                plain = ANSI.sub("", output)
                if not sent and ("Load [" in plain or "Chat with [" in plain):
                    os.write(master, answer.encode())
                    sent = True
                if "CASE_COMPLETE" in plain:
                    break
                if child.poll() is not None:
                    break
            else:
                status = subprocess.run(["ps", "-o", "pid,ppid,state,wchan,command", "-p",
                                         str(child.pid)], capture_output=True, text=True,
                                        timeout=3)
                raise AssertionError(f"PTY timeout:\n{output}\n{status.stdout}\n{status.stderr}")
            child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            os.close(master)
    return ANSI.sub("", output)


def scenario(source, side, name, root, *, piped=False, answer="2\n"):
    case_root = root / side / (name + ("-pipe" if piped else ""))
    env = isolated_env(source, case_root)
    result_path = case_root / "case.json"
    command = [sys.executable, str(SCRIPT), "case", "--source", str(source),
               "--side", side, "--scenario", name, "--result", str(result_path)]
    if piped:
        proc = subprocess.run(command, cwd=source, env=env, input=answer, text=True,
                              encoding="utf-8", errors="replace", capture_output=True,
                              timeout=20)
        output = proc.stdout + proc.stderr
    else:
        output = terminal(command, source, env, answer)
    assert result_path.exists(), f"child did not finish {side}/{name}: {output}"
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data.get("exception") in (None, "Exit"), (data, output)
    assert data["stdin_tty"] is (not piped), (data, output)
    assert data["stdout_tty"] is (not piped), (data, output)
    changes = name in ("idle", "auto", "gguf") and not piped
    succeeds = name in ("explicit", "loaded", "chat") or changes
    assert data["exit"] == (0 if succeeds else 1), (data, output)
    if changes:
        assert "Load [" in output and data["inventory_calls"] == 1, (data, output)
    elif name != "chat":
        assert "Load [" not in output and data["inventory_calls"] == 0, (data, output)
    expected_loads = [{"model_path": data["target"]}] if (name == "explicit" or
                     (changes and name != "auto")) else []
    expected_starts = [data["target"]] if changes and name == "auto" else []
    assert data["loads"] == expected_loads, (data, output)
    assert data["starts"] == expected_starts, (data, output)
    if name == "gguf":
        assert "OtherModel" not in output and "Load [1-1" in output, output
    if name == "owner":
        assert "belongs to another account" in output, output
    if name == "chat":
        assert "Chat with [1-2" in output and data["selected"] == data["target"], (data, output)
    print(f"PASS {side} {name} {'pipes' if piped else 'native-terminal'} "
          f"exit={data['exit']} inventory={data['inventory_calls']} "
          f"loads={len(data['loads'])} starts={len(data['starts'])}", flush=True)
    # Only synthetic terminal output, never environment, headers or credentials.
    if name in ("idle", "auto", "gguf") and not piped:
        print(output, flush=True)


def review_regressions(source, side, root, probe):
    env = isolated_env(source, root / side / "review-tests")
    env["PR16635_EXPECT_SOURCE"] = str(source)
    report = root / side / "review-regressions.xml"
    command = [sys.executable, "-m", "pytest", "-c", str(source / "pyproject.toml"),
               str(probe), "-q", "--basetemp", str(root / side / "review-test-data"),
               "--junitxml", str(report)]
    result = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=90)
    assert report.exists(), result.stdout + result.stderr
    cases = list(ET.parse(report).iter("testcase"))
    failed = [case for case in cases if case.find("failure") is not None]
    errors = [case for case in cases if case.find("error") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    assert len(cases) == 15 and not errors and not skipped, result.stdout + result.stderr
    if side == "before":
        expected = {
            "test_local_selection_wins_over_connection_with_same_model_id": 3,
            "test_non_gguf_directory_suffix_cannot_pass_gguf_filter": 2,
            "test_gguf_autostart_enter_skips_non_gguf_training_directory": 2,
            "test_non_gguf_path_spellings_do_not_become_repository_fallbacks": 4,
            "test_local_selection_survives_server_appearing_during_prompt": 1,
        }
        counts = Counter(case.get("name").split("[")[0] for case in failed)
        assert result.returncode == 1 and counts == expected, result.stdout + result.stderr
        for case in failed:
            name = case.get("name")
            detail = case.find("failure").text or ""
            if name.startswith("test_local_selection_wins"):
                assert "Connection" in detail or "external::" in detail, detail
            elif name.startswith("test_local_selection_survives"):
                assert "external::" in detail, detail
            elif name.startswith("test_gguf_autostart"):
                assert "my-run" in detail and "actual-Q4_K_M.gguf" in detail, detail
            else:
                assert "assert True is False" in detail, detail
            print(f"EXPECTED FAILURE {name}", flush=True)
        print("NEGATIVE PASS: 12 expected correctness failures; 3 controls pass; "
              "no setup errors or skips.", flush=True)
    else:
        assert result.returncode == 0 and not failed, result.stdout + result.stderr
        print("POSITIVE PASS: all 15 review regression probes pass.", flush=True)


def validate(args):
    root = Path(args.temp).resolve()
    fixed = Path(args.after).resolve()
    assert run(["git", "rev-parse", "HEAD"], cwd=fixed).strip() == HEAD
    probe = root / "review-probes" / "test_picker_review_regressions.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixed / "unsloth_cli/tests/test_start_model_picker_regressions.py", probe)
    with probe.open("a", encoding="utf-8") as stream:
        stream.write("\nfrom pathlib import Path as _Path\nimport os as _os\n"
                     "assert _Path(start.__file__).resolve().is_relative_to("
                     "_Path(_os.environ['PR16635_EXPECT_SOURCE']))\n")
    for side, revision in (("before", BASE), ("after", HEAD)):
        source = Path(getattr(args, side)).resolve()
        actual = run(["git", "rev-parse", "HEAD"], cwd=source).strip()
        assert actual == revision, (side, actual, revision)
        print(f"REVISION {side} {revision} platform={sys.platform}", flush=True)
        env = isolated_env(source, root / side / "inventory")
        output = run([sys.executable, str(SCRIPT), "inventory", "--source", str(source)],
                     cwd=source, env=env, timeout=35)
        assert "INVENTORY_IMPORT_PASS" in output, output
        print(f"PASS {side} bounded-inventory-import", flush=True)
        review_regressions(source, side, root, probe)
        for name in ("idle", "auto", "gguf", "explicit", "loaded", "owner", "remote",
                     "no_serve", "no_launch", "chat"):
            scenario(source, side, name, root, answer="1\n" if name == "gguf" else "2\n")
        scenario(source, side, "idle", root, piped=True)
        # Run a small shared regression slice on both sides using their own config.
        env = isolated_env(source, root / side / "pytest")
        output = run([sys.executable, "-m", "pytest", "-c", str(source / "pyproject.toml"),
                      str(source / "unsloth_cli/tests/test_inference_chat.py"),
                      "-k", "pick_model", "-q", "--basetemp", str(root / side / "pytest-data")],
                     cwd=source, env=env)
        print(output, flush=True)
        if side == "after":
            output = run([sys.executable, "-m", "pytest", "-c", str(source / "pyproject.toml"),
                          str(source / "unsloth_cli/tests/test_start_model_picker.py"), "-q",
                          "--basetemp", str(root / side / "picker-data")], cwd=source, env=env)
            print(output, flush=True)
    print("A/B PASS: both review findings reproduce before the fixes and pass after them; "
          "native terminal behavior and existing guards are preserved.", flush=True)
    return 0


def bootstrap(args):
    import venv
    env_root = Path(args.temp).resolve() / "venv"
    venv.EnvBuilder(with_pip=True).create(env_root)
    python = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    lock = SCRIPT.with_name("pr16635_requirements.txt")
    print(run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--only-binary=:all:", "-r", str(lock)], timeout=240), flush=True)
    return subprocess.call([str(python), str(SCRIPT), "validate", "--before", args.before,
                            "--after", args.after, "--temp", args.temp])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bootstrap", "validate", "case", "inventory"))
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--temp", default="temp/pr16635-validation")
    parser.add_argument("--source")
    parser.add_argument("--side", choices=("before", "after"))
    parser.add_argument("--scenario")
    parser.add_argument("--result")
    args = parser.parse_args()
    return {"bootstrap": bootstrap, "validate": validate, "case": case_child,
            "inventory": inventory_child}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
