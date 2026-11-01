"""Real Node prebuilt install on this runner with the checked-out studio/install_node_prebuilt.py.

Checks: fresh install picks .zip (Windows) / .tar.xz (else), node -v == pin, npm >= 11, rerun is a
no-op; POSIX also: Python without _lzma installs .tar.gz, that install is kept by a normal rerun,
and a mirror carrying only .tar.gz falls back to it.

    python jobs/any_node_install.py --out node_install.json   # from the unsloth repo root
"""

import argparse
import functools
import http.server
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--repo", default = ".")
p.add_argument("--out", default = "node_install.json")
a = p.parse_args()

INSTALLER = Path(a.repo, "studio", "install_node_prebuilt.py").resolve()
spec = importlib.util.spec_from_file_location("node_prebuilt", INSTALLER)
M = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(INSTALLER.parent))
sys.modules[spec.name] = M
spec.loader.exec_module(M)

host = M.detect_host()
pins = M.load_pins()
version = M.pinned_default_version(pins)
work = Path(tempfile.mkdtemp(prefix = "node_job_", dir = os.environ.get("RUNNER_TEMP")))
checks, runs = {}, []


def check(name, ok, detail):
    checks[name] = {"ok": bool(ok), "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush = True)


def install(
    dest,
    *,
    no_lzma = False,
    env = None,
):
    pre = (
        "import sys; sys.modules.pop('lzma', None); sys.modules['_lzma'] = None; "
        if no_lzma
        else ""
    )
    code = (
        pre
        + f"import runpy, sys; sys.argv = ['x', '--install-dir', {str(dest)!r}]; runpy.run_path({str(INSTALLER)!r}, run_name = '__main__')"
    )
    t = time.time()
    r = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output = True,
        text = True,
        env = {**os.environ, **(env or {})},
        timeout = 900,
    )
    log = r.stdout + r.stderr
    runs.append(
        {
            "dest": str(dest),
            "no_lzma": no_lzma,
            "rc": r.returncode,
            "s": round(time.time() - t, 1),
            "tail": log[-1500:],
        }
    )
    print(log[-800:], flush = True)
    return r.returncode, log


def state(dest):
    meta = M.load_metadata(dest) or {}
    return (
        meta.get("asset"),
        meta.get("sha256"),
        M.installed_node_version(dest, host),
        M.installed_npm_major(dest, host),
    )


def verify(tag, dest, rc, ext):
    asset, sha, node, npm = state(dest)
    want = M.node_asset_name(version, host).rsplit(host.archive_ext, 1)[0] + ext
    check(f"{tag}_rc0", rc == 0, f"rc={rc}")
    check(f"{tag}_asset", asset == want, f"asset={asset} want={want}")
    check(
        f"{tag}_sha_is_pin",
        sha is not None and sha == M.pinned_sha256(pins, version, asset or ""),
        f"sha={sha}",
    )
    check(f"{tag}_node_runs", node == version, f"node={node} pin={version}")
    check(f"{tag}_npm_floor", npm is not None and npm >= 11, f"npm_major={npm}")


try:
    print(
        f"host={platform.platform()} machine={platform.machine()} python={sys.version.split()[0]} "
        f"asset={M.node_asset_name(version, host)}",
        flush = True,
    )
    ext = ".zip" if host.is_windows else ".tar.xz"
    fresh = work / "fresh"
    rc, _ = install(fresh)
    verify("fresh", fresh, rc, ext)
    rc, log = install(fresh)
    check(
        "rerun_no_download",
        rc == 0 and "nothing to do" in log and "downloading" not in log,
        f"rc={rc}",
    )

    if not host.is_windows:
        gz = work / "gz"
        rc, _ = install(gz, no_lzma = True)
        verify("no_lzma", gz, rc, ".tar.gz")
        rc, log = install(gz)
        check(
            "gz_kept_after_switch",
            rc == 0
            and "nothing to do" in log
            and "downloading" not in log
            and state(gz)[0].endswith(".tar.gz"),
            f"rc={rc} asset={state(gz)[0]}",
        )

        gz_asset = state(gz)[0]
        mirror = work / "mirror"
        (mirror / f"v{version}").mkdir(parents = True)
        urllib.request.urlretrieve(
            M.node_download_url(version, gz_asset), mirror / f"v{version}" / gz_asset
        )
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(http.server.SimpleHTTPRequestHandler, directory = str(mirror)),
        )
        threading.Thread(target = srv.serve_forever, daemon = True).start()
        try:
            mdest = work / "mirror_install"
            rc, log = install(mdest, env = {M.NODE_MIRROR_ENV: f"http://127.0.0.1:{srv.server_port}"})
            verify("gz_only_mirror", mdest, rc, ".tar.gz")
            check("gz_only_mirror_fell_back", "unavailable" in log, "fallback logged")
        finally:
            srv.shutdown()
except Exception as exc:  # noqa: BLE001
    check("job_crashed", False, repr(exc))
finally:
    shutil.rmtree(work, ignore_errors = True)

passed = bool(checks) and all(v["ok"] for v in checks.values())
result = {
    "job": "any_node_install",
    "device": platform.platform(),
    "config": {"version": version},
    "versions": {"python": sys.version.split()[0]},
    "checks": checks,
    "runs": runs,
    "passed": passed,
    "error": None if passed else "check failed",
    "skipped": False,
    "finished": True,
}
Path(a.out).write_text(json.dumps(result, indent = 2), encoding = "utf-8")
print(
    "JOB_RESULT "
    + json.dumps(
        {
            "job": "any_node_install",
            "passed": passed,
            "failed": [k for k, v in checks.items() if not v["ok"]],
        }
    ),
    flush = True,
)
sys.exit(0 if passed else 1)
