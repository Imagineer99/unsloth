"""Workload for leak_hunt's soak: CPU-only, no model.

Phases (each recorded in the report; one that cannot run is listed with why, never skipped silently):
  login       bootstrap password -> change-password -> login (as tests/studio/studio_api_smoke.py)
  poll        GET /api/sandbox/capability every 1 s
  refresh     GET /api/settings/sandbox?refresh=true + /api/sandbox/capability?refresh=true every 10 s
  crawl       once: every parameter-less GET route in /openapi.json, minus streaming / destructive ones
  tools       helper subprocess (Studio venv python) importing the backend and calling execute_tool
              with adversarial Python / Terminal code (grandchild, setsid double-fork daemon,
              timeout overrun, cancel mid-run); `python drive.py --tool-adversary` is that helper.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_SKIP_WORDS = (
    "stream",
    "events",
    "sse",
    "download",
    "shutdown",
    "restart",
    "update",
    "export",
    "delete",
    "reset",
    "logout",
    "ws",
    "websocket",
    "tail",
    "watch",
    "follow",
    "install",
    "setup",
    "prepare",
    "upgrade",
    "cancel",
    "stop",
    "kill",
    "quit",
    "uninstall",
    "rotate",
)


class Http:
    def __init__(self, base: str):
        self.base, self.token = base.rstrip("/"), None

    def call(
        self,
        method: str,
        path: str,
        body = None,
        timeout: float = 15.0,
    ):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data = data, method = method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout = timeout) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw)
                except ValueError:
                    return r.status, raw[:200]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()[:200]
        except Exception as exc:
            return 0, repr(exc)


def wait_bootstrap(home: Path, timeout: float = 90.0) -> str | None:
    path = home / "auth" / ".bootstrap_password"
    end = time.time() + timeout
    while time.time() < end:
        try:
            pw = path.read_text(encoding = "utf-8").strip()
            if pw:
                return pw
        except OSError:
            pass
        time.sleep(0.5)
    return None


def login(http: Http, home: Path) -> dict:
    old = wait_bootstrap(home)
    if not old:
        return {"ok": False, "why": "no bootstrap password file"}
    code, body = http.call("POST", "/api/auth/login", {"username": "unsloth", "password": old})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        return {"ok": False, "why": f"bootstrap login {code}: {str(body)[:200]}"}
    http.token = body["access_token"]
    new = "LeakHunt-" + secrets.token_urlsafe(16)
    code, body = http.call(
        "POST", "/api/auth/change-password", {"current_password": old, "new_password": new}
    )
    if code != 200:
        return {"ok": False, "why": f"change-password {code}: {str(body)[:200]}"}
    code, body = http.call("POST", "/api/auth/login", {"username": "unsloth", "password": new})
    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "why": f"login {code}"}
    http.token = body.get("access_token")
    return {"ok": bool(http.token)}


def crawl_paths(http: Http) -> list[str]:
    code, spec = http.call("GET", "/openapi.json", timeout = 30)
    if code != 200 or not isinstance(spec, dict):
        return []
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        if "get" not in ops or "{" in path:
            continue
        low = path.lower()
        if any(
            w in low.replace("-", "_").split("/") or f"/{w}" in low or f"_{w}" in low
            for w in _SKIP_WORDS
        ):
            continue
        params = ops["get"].get("parameters") or []
        if any(p.get("required") for p in params):
            continue
        out.append(path)
    return sorted(out)


class Driver:
    def __init__(
        self,
        base_url: str,
        home: Path,
        studio_python: str | None,
        backend_dir: str | None,
        log_dir: Path,
        env: dict,
        stop: threading.Event,
        mode: str = "default",
    ):
        self.http = Http(base_url)
        self.home, self.python, self.backend = home, studio_python, backend_dir
        self.log_dir, self.env, self.stop, self.mode = log_dir, env, stop, mode
        self.report = {
            "login": None,
            "poll": {"n": 0, "codes": {}},
            "refresh": {"n": 0, "codes": {}},
            "crawl": {"ran": False},
            "tools": {"ran": False},
            "not_run": [],
        }
        self.helper: subprocess.Popen | None = None
        self.on_helper = None  # callback(pid) so the monitor tracks the helper as a root

    def _count(self, bucket: str, code) -> None:
        b = self.report[bucket]
        b["n"] += 1
        b["codes"][str(code)] = b["codes"].get(str(code), 0) + 1

    def run(self) -> None:
        if self.mode == "none":
            self.report["not_run"].append("all phases: --drive none")
            return
        self.report["login"] = login(self.http, self.home)
        if self.mode == "api":
            self.report["not_run"].append("tools: --drive api")
        if not self.report["login"].get("ok"):
            self.report["not_run"].append("poll/refresh/crawl: login failed")
        else:
            threading.Thread(target = self._crawl, daemon = True, name = "leak-hunt-crawl").start()
        if self.mode != "api":
            self._start_tools()
        last_refresh = 0.0
        while not self.stop.is_set():
            t0 = time.time()
            if self.http.token:
                code, _ = self.http.call("GET", "/api/sandbox/capability", timeout = 60)
                self._count("poll", code)
                if t0 - last_refresh >= 10:
                    last_refresh = t0
                    for path in (
                        "/api/settings/sandbox?refresh=true",
                        "/api/sandbox/capability?refresh=true",
                    ):
                        code, _ = self.http.call("GET", path, timeout = 90)
                        self._count("refresh", code)
            self.stop.wait(max(0.0, 1.0 - (time.time() - t0)))

    def _crawl(self) -> None:
        paths = crawl_paths(self.http)
        res = {"ran": True, "paths": paths, "codes": {}}
        for p in paths:
            if self.stop.is_set():
                res["truncated_at"] = p
                break
            code, _ = self.http.call("GET", p, timeout = 20)
            res["codes"][p] = code
        self.report["crawl"] = res

    def _start_tools(self) -> None:
        if not self.python or not self.backend:
            self.report["not_run"].append("tools: no --studio-python / backend dir")
            return
        out = self.log_dir / "tool_adversary.json"
        log = open(self.log_dir / "tool_adversary.log", "w")
        env = {**self.env, "PYTHONUNBUFFERED": "1"}
        kw = {"start_new_session": True} if os.name == "posix" else {}
        self.helper = subprocess.Popen(
            [
                self.python,
                os.path.abspath(__file__),
                "--tool-adversary",
                "--backend",
                self.backend,
                "--out",
                str(out),
            ],
            cwd = self.backend,
            env = env,
            stdout = log,
            stderr = subprocess.STDOUT,
            stdin = subprocess.DEVNULL,
            **kw,
        )
        if self.on_helper:
            self.on_helper(self.helper.pid)
        self.report["tools"] = {"ran": True, "helper_pid": self.helper.pid, "out": str(out)}

    def finish(self, timeout: float = 30.0) -> None:
        if self.helper is not None:
            try:
                self.helper.wait(timeout)
            except subprocess.TimeoutExpired:
                self.report["tools"]["helper_timed_out"] = True
                self.helper.kill()
                self.helper.wait()
            path = Path(self.report["tools"]["out"])
            try:
                self.report["tools"]["result"] = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                self.report["tools"]["result"] = {
                    "error": f"no helper result: {exc!r}",
                    "exit": self.helper.returncode,
                }


# ---------------------------------------------------------------- helper (runs in the Studio venv)

_PY_CASES = {
    "py_grandchild": "import subprocess\nsubprocess.Popen(['sleep', '600'])\nprint('spawned')\n",
    "py_daemon": (
        "import os, time\npid = os.fork()\nif pid == 0:\n    os.setsid()\n    if os.fork() == 0:\n"
        "        time.sleep(600)\n    os._exit(0)\nos.waitpid(pid, 0)\nprint('daemonised')\n"
    ),
    "py_overrun": "import time\nwhile True:\n    time.sleep(0.2)\n",
    "py_cancel": "import time\ntime.sleep(600)\n",
}
_SH_CASES = {
    "sh_grandchild": "sleep 600 &\necho spawned",
    "sh_daemon": "setsid sh -c 'sleep 600' </dev/null >/dev/null 2>&1 &\necho daemonised",
    "sh_nohup": "nohup sleep 600 >/dev/null 2>&1 &\necho nohup",
    "sh_overrun": "while true; do sleep 0.2; done",
}


def tool_adversary(backend: str, out: str) -> int:
    sys.path.insert(0, backend)
    os.chdir(backend)
    result = {"cases": {}}
    try:
        from core.inference import tools
    except Exception as exc:
        result["import_error"] = repr(exc)
        Path(out).write_text(json.dumps(result, indent = 1))
        return 2
    cases = dict(_PY_CASES)
    if os.name == "posix":
        cases.update(_SH_CASES)
    for name, code in cases.items():
        is_py = name.startswith("py_")
        tool = "python" if is_py else "terminal"
        args = {"code": code} if is_py else {"command": code}
        cancel = threading.Event()
        timeout = 3 if name.endswith("overrun") else 20
        if name.endswith("cancel"):
            threading.Timer(2.0, cancel.set).start()
        t0 = time.time()
        try:
            res = tools.execute_tool(
                tool,
                args,
                cancel_event = cancel,
                timeout = timeout,
                session_id = "leak-hunt",
                thread_id = "leak-hunt",
            )
            result["cases"][name] = {
                "ok": True,
                "s": round(time.time() - t0, 2),
                "result": str(res)[:300],
            }
        except Exception as exc:
            result["cases"][name] = {
                "ok": False,
                "s": round(time.time() - t0, 2),
                "error": repr(exc)[:300],
            }
        Path(out).write_text(json.dumps(result, indent = 1))
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tool-adversary", action = "store_true")
    ap.add_argument("--backend")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.tool_adversary:
        sys.exit(tool_adversary(a.backend, a.out))
    ap.error("only --tool-adversary is runnable directly")
