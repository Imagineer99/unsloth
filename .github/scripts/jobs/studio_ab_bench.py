"""Studio tool-call A/B on one VM: the PR head tree against its merge base, every OS (plans/win_studio_call_speed.md).

Workers: `head`, `head'` (A/A) and `base` (the backend of --base-ref, exported with git archive), each with its own
Studio home, all running `execute_tool` in required mode through the OS sandbox (MXC / bwrap / Seatbelt). Same
protocol as sandbox_fair_bench: fingerprint, settle, calibration, interleaved rounds after warmup to a steady state,
every call output-checked and its sandbox backend asserted.

Usage: python .github/scripts/jobs/studio_ab_bench.py --base-ref <sha> --out job_studio_ab.json [--rounds 60]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_bench_common as sbc  # noqa: E402


def export_tree(ref: str, dest: Path) -> dict:
    """studio/ at `ref`, from the checkout's git (fetched shallowly when the commit is not local)."""
    have = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}^{{commit}}"], cwd = sbc.REPO, capture_output = True
    )
    fetch = None
    if have.returncode != 0:
        fetch = subprocess.run(
            ["git", "fetch", "-q", "--depth", "1", "origin", ref],
            cwd = sbc.REPO,
            capture_output = True,
            text = True,
            timeout = 900,
        )
    arch = subprocess.run(
        ["git", "archive", "--format=tar", ref, "studio"],
        cwd = sbc.REPO,
        capture_output = True,
        timeout = 900,
    )
    if arch.returncode != 0:
        raise RuntimeError(
            f"git archive {ref} failed: {arch.stderr[-500:]!r} (fetch: {fetch and fetch.stderr[-300:]})"
        )
    with tarfile.open(fileobj = io.BytesIO(arch.stdout)) as tar:
        tar.extractall(dest, filter = "data") if sys.version_info >= (3, 12) else tar.extractall(dest)  # noqa: S202
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd = sbc.REPO, capture_output = True, text = True
    ).stdout.strip()
    return {
        "base_ref": ref,
        "head": head,
        "backend": str(dest / "studio" / "backend"),
        "fetched": fetch is not None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-ref", required = True)
    p.add_argument("--out", default = "job_studio_ab.json")
    p.add_argument("--rounds", type = int, default = 60)
    p.add_argument("--seed", type = int, default = 3407)
    p.add_argument("--tiny", action = "store_true")  # staging forces it on CPU legs: ignored
    p.add_argument(
        "--defender-toggle", action = "store_true"
    )  # accepted for a shared --job line; unused
    args = p.parse_args()
    out_path = Path(args.out).resolve()
    result = {"job": "studio_ab_bench", "passed": False, "sections": {}, "records": []}

    def flush():
        out_path.write_text(json.dumps(result, indent = 1, default = str))

    def section(name, fn):
        t = time.perf_counter()
        try:
            result["sections"][name] = fn()
        except Exception:  # noqa: BLE001
            result["sections"][name] = {"error": traceback.format_exc()[-3000:]}
        if isinstance(result["sections"][name], dict):
            result["sections"][name]["_section_s"] = round(time.perf_counter() - t, 1)
        flush()
        print(
            f"=== {name} ===\n{json.dumps(result['sections'][name], default = str)[:4000]}",
            flush = True,
        )
        return result["sections"][name]

    workers = {}
    try:
        sbc.ensure_psutil()
        work = Path(tempfile.mkdtemp(prefix = "studioab-"))
        if sys.platform == "win32":
            import mxc_fair_bench as mfb
            section("fingerprint", mfb.fingerprint)
        else:
            if sys.platform.startswith("linux"):
                import sandbox_fair_bench as sfb
                section("ensure_bwrap", sfb.ensure_bwrap)
            section("fingerprint", sbc.posix_fingerprint)
        tree = section("base_tree", lambda: export_tree(args.base_ref, work / "base"))
        if "error" in tree:
            raise RuntimeError("could not export the base tree")
        import mxc_bench as mb

        section("studio_deps", mb._ensure_studio_deps)
        backends = {"head": str(sbc.BACKEND), "head'": str(sbc.BACKEND), "base": tree["backend"]}
        homes = {}
        if sys.platform == "win32":
            from studio_call_profile import windows_setup
            for i, name in enumerate(backends):
                env, rec = windows_setup(work / f"w{i}")
                homes[name] = env
                result["sections"].setdefault("windows_setup", {})[name] = rec
            flush()
        else:
            homes = {
                name: {"UNSLOTH_STUDIO_HOME": str(work / f"w{i}" / "home")}
                for i, name in enumerate(backends)
            }
        section("settle", sbc.settle)
        section("calibration_start", sbc.calibrate)

        def start():
            ready = {}
            # Start order rotates with the seed: a start-order bias then shows as an A/A sign flip across waves.
            names = list(backends)
            k = args.seed % len(names)
            for name in names[k:] + names[:k]:
                env = {
                    **os.environ,
                    "FB_BACKEND": backends[name],
                    "PYTHONIOENCODING": "utf-8",
                    **homes[name],
                }
                w = sbc.Worker(name, env, work)
                ready[name] = w.ready
                if "error" not in w.ready:
                    workers[name] = w
            return ready

        section("workers", start)
        if set(workers) != set(backends):
            raise RuntimeError(f"workers missing: {sorted(set(backends) - set(workers))}")
        section(
            "studio",
            lambda: sbc.studio_rounds(workers, 30, args.rounds, args.seed, result["records"]),
        )
        section("calibration_end", sbc.calibrate)
        result["summary"] = sbc.summarize(result["records"])
        total = len(result["records"])
        bad = sum(not r["ok"] for r in result["records"])
        result["ok_rate"] = round(1 - bad / total, 4) if total else 0
        result["passed"] = bool(total) and result["ok_rate"] >= 0.95
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-4000:]
    finally:
        for w in workers.values():
            w.close()
        flush()
        print(
            "JOB_RESULT "
            + json.dumps(
                {"passed": result["passed"], "summary": result.get("summary")}, default = str
            )[:20000],
            flush = True,
        )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
