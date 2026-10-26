#!/usr/bin/env python3
"""leak_hunt runtime harness: boot Studio with compressed timers, soak, stop, judge.

  python leak_hunt/run.py --studio-python <venv python> --home <fresh UNSLOTH_STUDIO_HOME> \
      --port 18931 --timers timers.json --compress-to 1.0 --soak 60 --post-stop 20 \
      --out leak_hunt_head.json [--label head] [--inject nofile-unlimited] [--drive default|none] \
      [--allow REGEX]... [--studio-cmd "<argv>"] [--install-home <shared install to shim>] [--sites FILE]

Exit 0 CLEAN, 1 LEAK, 2 VOID. Writes <out> (JSON) and <out>.md, run artefacts in <out>.d/.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import accel  # noqa: E402
import drive  # noqa: E402
import monitor  # noqa: E402

CLEAN, LEAK, VOID = 0, 1, 2


def _venv_bin(python: str, name: str) -> str:
    vbin = Path(python).parent
    for cand in (vbin / name, vbin / f"{name}.exe", vbin.parent / "Scripts" / f"{name}.exe"):
        if cand.exists():
            return str(cand)
    return str(vbin / name)


def _backend_dir(python: str) -> str | None:
    try:
        out = subprocess.run(
            [
                python,
                "-c",
                "import os, studio; print(os.path.join(os.path.dirname(studio.__file__), 'backend'))",
            ],
            capture_output = True,
            text = True,
            timeout = 120,
            env = {k: v for k, v in os.environ.items() if not k.startswith("LEAK_HUNT")},
        )
        path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        return path if path and os.path.isdir(path) else None
    except Exception:
        return None


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout = 5) as r:
            return json.loads(r.read()).get("status") == "healthy"
    except Exception:
        return False


def _read_jsonl(d: Path, stem: str) -> list[dict]:
    out = []
    for p in sorted(d.glob(f"{stem}-*.jsonl")):
        for line in p.read_text(encoding = "utf-8", errors = "replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _listening_pid(tracks, port: int):
    import psutil
    for tr in tracks.values():
        if tr.exited_at is not None:
            continue
        try:
            for c in psutil.Process(tr.pid).net_connections(kind = "inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                    return tr.pid
        except Exception:
            continue
    return None


def _preexec_nofile():
    import resource
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except (ValueError, OSError):
        pass


def _stop_server(proc: subprocess.Popen, budget: float) -> dict:
    """Graceful only: signal the launcher, never the tree, so leaks stay visible."""
    steps, end = [], time.time() + budget
    if os.name == "posix":
        sigs = [(signal.SIGINT, budget / 2), (signal.SIGTERM, budget / 2)]
        for sig, wait in sigs:
            if proc.poll() is not None:
                break
            try:
                proc.send_signal(sig)
                steps.append(sig.name)
            except OSError:
                pass
            try:
                proc.wait(max(0.1, min(wait, end - time.time())))
            except subprocess.TimeoutExpired:
                pass
    else:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            steps.append("CTRL_BREAK_EVENT")
            proc.wait(budget / 2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output = True)
            steps.append("taskkill")
            try:
                proc.wait(max(0.1, end - time.time()))
            except subprocess.TimeoutExpired:
                pass
    graceful = proc.poll() is not None
    if not graceful:
        proc.kill()
        steps.append("KILL")
        proc.wait(10)
    return {"steps": steps, "graceful": graceful, "returncode": proc.returncode}


def main(argv = None) -> int:
    ap = argparse.ArgumentParser(
        description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--studio-python", required = True)
    ap.add_argument("--studio-cmd", help = "argv to launch the server; PORT is substituted")
    ap.add_argument("--port", type = int, default = 18931)
    ap.add_argument("--home", required = True, help = "UNSLOTH_STUDIO_HOME for this run (fresh auth)")
    ap.add_argument("--install-home", help = "shared install to shim into --home (local runs)")
    ap.add_argument("--timers", help = "timers.json from leak_hunt/timers.py")
    ap.add_argument("--compress-to", type = float, default = 1.0)
    ap.add_argument("--soak", type = float, default = 60.0)
    ap.add_argument("--post-stop", type = float, default = 20.0)
    ap.add_argument("--stop-budget", type = float, default = 20.0)
    ap.add_argument("--health-timeout", type = float, default = 120.0)
    ap.add_argument("--out", required = True)
    ap.add_argument("--label", default = "")
    ap.add_argument("--inject", choices = ["nofile-unlimited"], action = "append", default = [])
    ap.add_argument(
        "--drive",
        choices = ["default", "api", "none"],
        default = "default",
        help = "default: api + adversarial tools; api: no tool phase; none: idle",
    )
    ap.add_argument("--allow", action = "append", default = [], help = "regex on cmdline to ignore")
    ap.add_argument("--accel", choices = ["pythonpath", "pth"], default = "pythonpath")
    ap.add_argument("--sites", help = "spawn_sites.json from static_audit.py, for coverage")
    for k, v in monitor.DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type = type(v), default = v)
    a = ap.parse_args(argv)

    out = Path(a.out).resolve()
    work = Path(str(out) + ".d")
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents = True)
    home = Path(a.home).resolve()
    if a.install_home:
        sys.path.insert(0, str(HERE.parent))
        from studio_regress.engine import state_home
        state_home(Path(a.install_home), home, venv_shim = True)
    home.mkdir(parents = True, exist_ok = True)

    run_tag = uuid.uuid4().hex[:12]
    report: dict = {
        "label": a.label,
        "run_tag": run_tag,
        "platform": sys.platform,
        "args": vars(a),
        "void_reasons": [],
        "findings": [],
        "errors": [],
    }
    backend = _backend_dir(a.studio_python)
    report["backend_dir"] = backend

    env = {k: v for k, v in os.environ.items() if not k.startswith("LEAK_HUNT")}
    env.update(
        {
            "UNSLOTH_STUDIO_HOME": str(home),
            "LEAK_HUNT_DIR": str(work),
            "LEAK_HUNT_TAG": run_tag,
            "PYTHONUNBUFFERED": "1",
            "UNSLOTH_DISABLE_AUTO_UPDATES": "1",
        }
    )
    pth_paths = []
    if a.accel == "pth":
        pth_paths = accel.install_pth(a.studio_python)
    else:
        env = accel.pythonpath_env(env)
    server_env = dict(env)
    if a.timers:
        server_env.update(
            {
                "LEAK_HUNT_COMPRESS": "1",
                "LEAK_HUNT_TIMERS": str(Path(a.timers).resolve()),
                "LEAK_HUNT_COMPRESS_TO": str(a.compress_to),
            }
        )

    cmd = (
        shlex.split(a.studio_cmd.replace("PORT", str(a.port)))
        if a.studio_cmd
        else [
            _venv_bin(a.studio_python, "unsloth"),
            "studio",
            "--api-only",
            "-H",
            "127.0.0.1",
            "-p",
            str(a.port),
        ]
    )
    if a.inject and os.name == "posix":
        # setrlimit(RLIM_INFINITY) can be refused where the shell builtin is not (macOS); exec keeps the pid
        cmd = [
            "/bin/sh",
            "-c",
            'ulimit -n unlimited 2>/dev/null || ulimit -S -n unlimited 2>/dev/null; exec "$@"',
            "sh",
            *cmd,
        ]
    report["cmd"] = cmd
    mon = monitor.Monitor(run_tag, str(work), markers = (str(home),))
    report["attribution"] = {"subreaper": mon.subreaper, "job": bool(mon.job and mon.job.handle)}
    kw: dict = {}
    if os.name == "posix":
        if a.inject:
            kw["preexec_fn"] = _preexec_nofile
    else:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(work / "studio.log", "w")
    t_launch = time.time()
    proc = subprocess.Popen(
        cmd, env = server_env, stdout = log, stderr = subprocess.STDOUT, stdin = subprocess.DEVNULL, **kw
    )
    mon.add_root(proc.pid, "studio")
    mon.mark_server(proc.pid)
    mon.run_background()

    rc = VOID
    stop_ev = threading.Event()
    drv = None
    try:
        healthy = False
        while time.time() - t_launch < a.health_timeout:
            if proc.poll() is not None:
                break
            if _healthy(a.port):
                healthy = True
                break
            time.sleep(1)
        report["health_s"] = round(time.time() - t_launch, 1)
        if not healthy:
            report["void_reasons"].append(
                f"Studio not healthy in {a.health_timeout:.0f}s (exit {proc.poll()})"
            )
        else:
            lp = _listening_pid(mon.tracks, a.port)
            if lp:
                mon.mark_server(lp)
            report["server_pid"] = lp or proc.pid
            mon.snapshot_server("start", report["server_pid"])
            soak_start = time.time()
            report["soak_start"] = soak_start
            helper_env = {k: v for k, v in env.items()}
            drv = drive.Driver(
                f"http://127.0.0.1:{a.port}",
                home,
                a.studio_python,
                backend,
                work,
                helper_env,
                stop_ev,
                mode = a.drive,
            )
            drv.on_helper = lambda pid: mon.add_root(pid, "driver-tools")
            dthread = threading.Thread(target = drv.run, daemon = True, name = "leak-hunt-drive")
            dthread.start()
            while time.time() - soak_start < a.soak and proc.poll() is None:
                time.sleep(0.5)
            stop_ev.set()
            dthread.join(120)
            drv.finish(90)
            mon.snapshot_server("end", report["server_pid"])
            if proc.poll() is not None:
                report["errors"].append(f"Studio exited during the soak (code {proc.returncode})")
        mon.stop_time = time.time()
        report["stop"] = _stop_server(proc, a.stop_budget)
        mon.server_exit_time = time.time()
        time.sleep(a.post_stop)
        mon.stop_background()
        mon.tick()
        # everything still alive now outlived Studio + the post-stop window
        survivors = []
        for tr in mon.tracks.values():
            if tr.exited_at is None:
                tr.alive_after_stop = True
                survivors.append(tr)
        report["survivors_killed"] = []
        import psutil

        for tr in survivors:
            try:
                p = psutil.Process(tr.pid)
                if round(p.create_time(), 2) == round(tr.ctime, 2):
                    p.kill()
                    report["survivors_killed"].append(tr.pid)
            except psutil.Error:
                pass
    finally:
        stop_ev.set()
        if proc.poll() is None:
            proc.kill()
        mon.stop_background()
        accel.uninstall_pth(pth_paths)
        log.close()

    rows = mon.finalize()
    th = {k: getattr(a, k) for k in monitor.DEFAULTS}
    report["findings"] = monitor.evaluate(
        rows,
        mon.site_live,
        stop_time = mon.stop_time or time.time(),
        soak_start = report.get("soak_start", t_launch),
        allow = a.allow,
        thresholds = th,
    )
    # accel evidence
    server_recs = _read_jsonl(work, "server")
    comp = _read_jsonl(work, "compressed")
    rewrites = [c for c in comp if c.get("name") != "<loaded>"]
    report["accel"] = {
        "mode": a.accel,
        "server_claims": server_recs,
        "modules_loaded": sorted({c["module"] for c in comp if c.get("name") == "<loaded>"}),
        "rewrites": len(rewrites),
        "rewrites_sample": rewrites[:40],
        "processes_with_accel": len(_read_jsonl(work, "procs")),
        "errors": _read_jsonl(work, "errors")[:20],
    }
    if a.timers:
        if not server_recs:
            report["void_reasons"].append(
                "accel never claimed the server process (no server-*.jsonl)"
            )
        elif not rewrites:
            report["void_reasons"].append("accel loaded in the server but rewrote no timer")
    if a.inject:
        nofile = server_recs[0].get("nofile") if server_recs else None
        report["inject"] = {"requested": a.inject, "server_nofile": nofile}
        if not nofile or nofile[0] != "unlimited":
            report["void_reasons"].append(
                f"--inject nofile-unlimited not achieved (server nofile {nofile})"
            )
    spawns = _read_jsonl(work, "spawns")
    by_site: dict = {}
    for s in spawns:
        by_site[s.get("site", "unknown")] = by_site.get(s.get("site", "unknown"), 0) + 1
    report["spawns"] = {
        "total": len(spawns),
        "by_site": dict(sorted(by_site.items(), key = lambda kv: -kv[1])),
    }
    if a.sites:
        try:
            sites = json.loads(Path(a.sites).read_text())
            keys = {
                f"{s['file']}:{s['line']}"
                for s in sites.get("sites", sites if isinstance(sites, list) else [])
            }
            hit = {
                k
                for k in by_site
                if any(k.endswith(x) or x.endswith(k.split("studio/")[-1]) for x in keys)
            }
            report["coverage"] = {"static_sites": len(keys), "exercised_sites": len(hit)}
        except Exception as exc:
            report["coverage"] = {"error": repr(exc)}
    if drv is not None:
        report["drive"] = drv.report
        if a.drive != "none" and not (drv.report.get("login") or {}).get("ok"):
            report["void_reasons"].append(f"login failed: {drv.report.get('login')}")
    report["server_stats"] = mon.server_stats
    report["monitor_errors"] = mon.errors[:20]
    report["tracked"] = len(rows)
    report["tracks"] = [{k: v for k, v in r.items() if k != "_cpu"} for r in rows]
    if report["void_reasons"]:
        rc, verdict = VOID, "VOID"
    elif report["findings"]:
        rc, verdict = LEAK, "LEAK"
    else:
        rc, verdict = CLEAN, "CLEAN"
    report["verdict"], report["exit"] = verdict, rc
    out.write_text(json.dumps(report, indent = 1, default = str))
    Path(str(out) + ".md").write_text(_markdown(report))
    print(
        f"leak_hunt {a.label or ''} {verdict} (exit {rc}): {len(report['findings'])} finding(s), "
        f"{report['tracked']} processes tracked, {report['accel']['rewrites']} timers compressed, "
        f"{report['spawns']['total']} spawns logged -> {out}"
    )
    for f in report["findings"]:
        print(
            f"  {f['kind']:8} pid={f.get('pid', f.get('pids'))} site={f.get('site')} :: {f['detail']} :: "
            f"{(f.get('cmdline') or '')[:140]}"
        )
    for v in report["void_reasons"]:
        print(f"  VOID: {v}")
    return rc


def _markdown(r: dict) -> str:
    lines = [
        f"# leak_hunt {r['label']} on {r['platform']}: **{r['verdict']}**",
        "",
        f"- tracked processes: {r['tracked']}; spawns logged: {r['spawns']['total']}; "
        f"timers compressed: {r['accel']['rewrites']} in {len(r['accel']['modules_loaded'])} modules",
        f"- stop: {r.get('stop')}",
        f"- server stats: {r.get('server_stats')}",
    ]
    if r["void_reasons"]:
        lines += ["", "## VOID"] + [f"- {v}" for v in r["void_reasons"]]
    if r["findings"]:
        lines += [
            "",
            "## Findings",
            "",
            "| kind | pid | site | detail | cmdline |",
            "|---|---|---|---|---|",
        ]
        for f in r["findings"]:
            lines.append(
                f"| {f['kind']} | {f.get('pid', f.get('pids'))} | `{f.get('site')}` | {f['detail']} | "
                f"`{(f.get('cmdline') or '')[:100]}` |"
            )
    lines += ["", "## Top spawn sites", ""] + [
        f"- {n} x `{s}`" for s, n in list(r["spawns"]["by_site"].items())[:15]
    ]
    d = r.get("drive") or {}
    if d:
        lines += [
            "",
            "## Drive",
            f"- login: {d.get('login')}",
            f"- poll: {d.get('poll')}",
            f"- refresh: {d.get('refresh')}",
            f"- crawl paths: {len((d.get('crawl') or {}).get('paths') or [])}",
            f"- tools: {json.dumps((d.get('tools') or {}).get('result'), default=str)[:1500]}",
            f"- not run: {d.get('not_run')}",
        ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
