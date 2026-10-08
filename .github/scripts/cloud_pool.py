#!/usr/bin/env python3
"""Remote GPU pool: pick a Colab / Kaggle tier by VRAM, dtype and cost, run a job there, and
keep Colab workers warm for reuse with a per-job rollback.

    python cloud_pool.py select --gb 12 --dtype bf16 --est-min 20 --class train
    python cloud_pool.py run --script job.py --gb 6 --dtype fp16 --est-min 10 [--backend auto|colab|kaggle] [--dry-run] -- args
    python cloud_pool.py status | sweep | check-auth
    python cloud_pool.py kaggle-sync --token-env KAGGLE_API_TOKEN --used-hours 15.8   (website number, display only)
    python cloud_pool.py colab-login --account daniel|michael     # prints the command; --run logs in now
    python cloud_pool.py colab-missing                            # accounts not logged in (file check)
    python cloud_pool.py probe-concurrency --account michael [--tier T4]
    python cloud_pool.py colab-expire --worker NAME --gen G --after S      (spawned detached; not for humans)

Mechanics are notebook_cloud_run.py's (allocation, attach + wipe of a named session, verdict from the
executed notebook, Kaggle packaging); this file only decides WHERE, keeps the books and wraps the job.

State
  host-wide  <gpu_queue lock dir>/switchboard/cloud/ledger.json (flock): live slots per (account, tier
             family) and per Kaggle token (2 kernels each), Colab and Kaggle refusal benches, Kaggle
             wall hours (7 days, display only, optional kaggle-sync baseline). Every unix user shares it.
  per user   ~/.config/switchboard/workers.json (flock): this user's Colab workers. A Colab session's
             token lives in the account HOME of the user who made it, so workers are not shared across
             unix users; the slot accounting is.
  accounts   ~/.config/switchboard/colab/<account>/ (0700) is that account's HOME for the colab CLI
             (colab_cli/auth.py keeps its token at ~/.config/colab-cli/token.json). daniel falls back to
             the real HOME (the existing gcloud ADC login) until its dir holds a token.

Selection: filter by single-device VRAM, dtype (T4 has no bf16/fp8/nvfp4; fp8 needs L4/H100/G4; nvfp4
needs G4), RAM, account tiers and a free slot; credits are never tracked or gated. Kaggle first when it
fits: the
token is a weighted random draw (weight = weekly allowance, 60 : 45), benched tokens skipped (a quota
refusal benches for a day, a busy / 2-kernel / rate-limit refusal for 10 min), the other token is the
retry; tracked hours never gate. Then Colab by expected CU = rate x (roofline-predicted runtime + cold
setup), where a warm idle worker has no setup; michael goes first for T4/T4-HM/L4. A Colab refusal
benches (account, tier family): out of credits / compute units for a day, capacity or too many
sessions for 10 min; with nothing left remote, gpu_queue keeps waiting for a local GPU.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import io
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import notebook_cloud_run as ncr  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Data: tiers, GPUs, accounts. Rates and sizes are the user's measurements (2026-09), and drift:
# <shared>/switchboard/tiers.json overrides any field ({"tiers": {"L4": {"cu_per_h": 1.6}}}).
# ---------------------------------------------------------------------------------------------

# Dense tensor-core TFLOPs (never the sparse figures) and memory bandwidth GB/s, for the roofline.
GPUS = {
    "T4": {"fp16": 65.0, "bw": 320.0},  # nvidia.com T4 page
    "L4": {"fp16": 121.0, "fp8": 242.5, "bw": 300.0},  # nvidia.com L4 page
    "A100-40": {"fp16": 312.0, "bw": 1555.0},  # A100 datasheet
    "A100-80": {"fp16": 312.0, "bw": 2039.0},  # A100 datasheet (SXM)
    "RTXPRO6000": {
        "fp16": 503.8,
        "fp8": 1007.6,
        "nvfp4": 2015.2,
        "bw": 1792.0,
    },  # RTX Blackwell PRO PDF
    "H100": {"fp16": 989.0, "fp8": 1979.0, "bw": 3350.0},  # H100 SXM datasheet; verify Colab's SKU
    "B200": {
        "fp16": 2250.0,
        "fp8": 4500.0,
        "nvfp4": 9000.0,
        "bw": 8000.0,
    },  # datasheet dense, verify
}

# vram_gb/ram_gb: usable single-device VRAM and host RAM. family: tiers that share a slot pool.
TIERS = {
    "kaggle-T4x2": {
        "backend": "kaggle",
        "gpu": "T4",
        "vram_gb": 15.0,
        "gpus": 2,
        "ram_gb": 29.0,
        "dtypes": ["fp16", "fp32"],
        "cu_per_h": 0.0,
        "family": "kaggle",
    },
    "T4": {
        "backend": "colab",
        "gpu": "T4",
        "vram_gb": 15.0,
        "ram_gb": 12.7,
        "dtypes": ["fp16", "fp32"],
        "cu_per_h": 1.07,
        "slots": 3,
        "family": "T4",
        "idle_s": 300,
    },
    "T4-HM": {
        "backend": "colab",
        "gpu": "T4",
        "vram_gb": 15.0,
        "ram_gb": 51.0,
        "dtypes": ["fp16", "fp32"],
        "cu_per_h": 1.27,
        "slots": 3,
        "family": "T4-HM",
        "bench_family": "T4",
        "idle_s": 300,
    },  # its own 3 slots beside T4's 3; a refusal benches both (same card)
    "L4": {
        "backend": "colab",
        "gpu": "L4",
        "vram_gb": 22.5,
        "ram_gb": 53.0,
        "dtypes": ["fp16", "bf16", "fp8", "fp32"],
        "cu_per_h": 1.54,
        "slots": 3,
        "family": "L4",
        "idle_s": 300,
    },
    "A100": {
        "backend": "colab",
        "gpu": "A100-40",
        "vram_gb": 40.0,
        "ram_gb": 83.5,
        "dtypes": ["fp16", "bf16", "fp32"],
        "cu_per_h": 5.3,
        "slots": 3,
        "family": "A100",
        "idle_s": 240,
    },
    "A100-HM": {
        "backend": "colab",
        "gpu": "A100-80",
        "vram_gb": 80.0,
        "ram_gb": 167.0,
        "dtypes": ["fp16", "bf16", "fp32"],
        "cu_per_h": 6.77,
        "slots": 3,
        "family": "A100-HM",
        "idle_s": 180,
        "scarce": True,
        "alloc_wait": 180,
    },
    "G4": {
        "backend": "colab",
        "gpu": "RTXPRO6000",
        "vram_gb": 95.6,
        "ram_gb": 176.9,
        "dtypes": ["fp16", "bf16", "fp8", "nvfp4", "fp32"],
        "cu_per_h": 8.9,
        "slots": 3,
        "family": "G4",
        "idle_s": 180,
    },
    # Rate unknown and supply nearly nil: priced above G4 so it is the last resort, one short try.
    "H100": {
        "backend": "colab",
        "gpu": "H100",
        "vram_gb": 80.0,
        "ram_gb": 170.0,
        "dtypes": ["fp16", "bf16", "fp8", "fp32"],
        "cu_per_h": None,
        "slots": 3,
        "family": "H100",
        "idle_s": 180,
        "scarce": True,
        "alloc_wait": 120,
    },
    # AMD CI (amd_ci_workflow): Strix Halo gfx1151, unified memory, dispatched by amd_ci/scaffold.py,
    # never picked for a CUDA job (backends default excludes it). Observed on oobabooga/unsloth over
    # 4 days (215 self-hosted jobs): ephemeral runners on one Linux (s04) and one Windows (s05) box,
    # max 2 concurrent >5 min jobs per OS (3 across both), queue wait p50 ~1 min, p90 ~20 min,
    # max ~55 min. So: 2 slots per OS (queue up to 4), timing needs the box to itself.
    "amd-linux": {
        "backend": "amd_ci",
        "gpu": "gfx1151",
        "vram_gb": 96.0,
        "ram_gb": 32.0,
        "dtypes": ["fp16", "bf16", "fp32"],
        "cu_per_h": 0.0,
        "slots": 2,
        "perf_slots": 1,
        "family": "amd-linux",
        "max_s": 6 * 3600,
    },
    "amd-windows": {
        "backend": "amd_ci",
        "gpu": "gfx1151",
        "vram_gb": 96.0,
        "ram_gb": 32.0,
        "dtypes": ["fp16", "bf16", "fp32"],
        "cu_per_h": 0.0,
        "slots": 2,
        "perf_slots": 1,
        "family": "amd-windows",
        "max_s": 6 * 3600,
    },
}

COLAB_ACCOUNTS = {
    "daniel": {"email": "danielhanchen@gmail.com", "tiers": None},  # None = all
    # small plan: tried first for its tiers; its refusals bench it, then daniel takes over
    # small plan: up to 3 per tier family (T4, T4-HM, L4), like daniel's; a `probe-concurrency`
    # result lowers the family it probed, and a refusal benches the tier
    "michael": {
        "email": "michaelhanchen2050@gmail.com",
        "tiers": ["T4", "T4-HM", "L4"],
        "prefer": True,
    },
}
# Kaggle also grants 20 h/week of TPU per account; nothing here uses it (GPU jobs only).
KAGGLE_KERNELS_PER_TOKEN = 2  # Kaggle's own limit per account: hard
# Caps, host-wide (every user, run and workspace): by default every Colab session of an (account, tier
# family) and every Kaggle kernel; lower them to keep headroom for people and ad-hoc runs.
COLAB_USE = int(os.environ.get("CLOUD_POOL_COLAB_USE") or 3)  # of 3 per family
KAGGLE_KERNELS_TOTAL = int(os.environ.get("CLOUD_POOL_KAGGLE_KERNELS") or 6)  # of 2 x 3 tokens
KAGGLE_JOBS_PER_KERNEL = 2
COLAB_IDLE_TIMEOUT_S = (
    1800  # ncr kills a Colab job whose cell prints nothing (the job heartbeats every 300 s)
)
COLD_SETUP_S = 300  # allocation + kernel probe + uv/torch install on a fresh VM
WARM_SETUP_S = 60  # venv + torch from the worker's uv cache
DEFAULT_ALLOC_WAIT = 600
COMPUTE_SHARE = {"train": 0.7, "eval": 0.6, "decode": 0.2, "export": 0.3}
# Share of a job that does not scale with the GPU (imports, model load, data, launch latency); the
# roofline alone sent a 20 min B200 job to 7 h on L4. Small jobs (< SMALL_GB) underuse a big card,
# so a B200 reference is scored as SMALL_REF for them (keeps T4 vs L4 apart, unlike a flat cap).
# Both are priors; the learned correction replaces them.
FIXED_SHARE = {"train": 0.25, "eval": 0.35, "decode": 0.3, "export": 0.5}
SMALL_GB = 8.0
SMALL_REF = "A100-80"
ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
MONTH_S = 31 * 24 * 3600
MAX_RUNTIME_S = {"kaggle": 11 * 3600, "colab": 22 * 3600}


def _now():
    return time.time()


# ---------------------------------------------------------------------------------------------
# Paths + locking
# ---------------------------------------------------------------------------------------------


def shared_dir():
    """Host-wide switchboard dir (the gpu_queue lock dir, which is 0777 and shared by all users)."""
    env = os.environ.get("CLOUD_POOL_DIR")
    if env:
        return Path(env)
    try:
        import gpu_queue
        base = Path(gpu_queue.lock_dir())
    except Exception:
        from studio_regress import gpu_pack
        base = Path(gpu_pack.lock_dir_default())
    return base / "switchboard"


def user_dir():
    env = os.environ.get("CLOUD_POOL_USER_DIR")
    return Path(env) if env else Path.home() / ".config" / "switchboard"


def account_home(account):
    return user_dir() / "colab" / account


def _mkdir(d, mode):
    d.mkdir(parents = True, exist_ok = True)
    with contextlib.suppress(OSError):
        os.chmod(d, mode)


class LockTimeout(TimeoutError):
    """A state-file flock was not granted within CLOUD_POOL_LOCK_TIMEOUT_S."""


class LockOrderError(RuntimeError):
    """A lock taken against the global order (or re-taken by the same thread): a latent deadlock."""


# Global acquisition order. Nested locks must be taken in increasing rank. The deadlock seen with
# concurrent launches was an ABBA inversion: select_tier held ledger.json and asked for
# workers.json (account_ready), while claim_worker / release_worker held workers.json and asked
# for ledger.json; flock has no timeout, so both waited forever. flock locks die with their
# process (no stale lock after a kill); the risks are ordering and unbounded waits.
LOCK_RANK_SERVER, LOCK_RANK_WORKERS, LOCK_RANK_LEDGER, LOCK_RANK_OTHER = 10, 20, 30, 40
LOCK_TIMEOUT_S = 300.0
_HELD = threading.local()


def _lock_rank(path):
    name = Path(path).name
    if name.startswith("server_"):
        return LOCK_RANK_SERVER
    if name == "workers.json":
        return LOCK_RANK_WORKERS
    if name == "ledger.json":
        return LOCK_RANK_LEDGER
    return LOCK_RANK_OTHER


def _lock_holder(lock_path):
    with contextlib.suppress(OSError, ValueError):
        info = json.loads(Path(lock_path).read_text() or "{}")
        alive = _pid_alive(info.get("pid")) if info.get("host") == os.uname().nodename else None
        return "pid %s (%s, %s) since %s" % (
            info.get("pid"),
            info.get("user"),
            "alive" if alive else ("dead" if alive is False else "other host"),
            time.strftime("%H:%M:%S", time.localtime(info.get("t", 0))),
        )
    return "unknown holder"


def _flock_with_timeout(fh, lock_path, timeout):
    import fcntl

    deadline = time.monotonic() + timeout
    delay = 0.02
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, PermissionError):
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    "cloud_pool: %s was not granted within %.0f s; held by %s. flock is released when "
                    "its holder exits, so a live holder is stuck (inspect it), not stale. Raise "
                    "CLOUD_POOL_LOCK_TIMEOUT_S to wait longer."
                    % (lock_path, timeout, _lock_holder(lock_path))
                )
            time.sleep(delay)
            delay = min(0.5, delay * 1.5)


@contextlib.contextmanager
def _json_locked(
    path,
    mode = 0o666,
    dir_mode = 0o777,
    timeout = None,
):
    """Read-modify-write a JSON file under an exclusive flock. A corrupt file is kept aside
    (never silently read as empty and overwritten). Locks nest only in LOCK_RANK order
    (LockOrderError otherwise, instead of a silent cross-process deadlock), and a wait longer
    than CLOUD_POOL_LOCK_TIMEOUT_S (default 300 s) raises LockTimeout naming the holder."""
    held = getattr(_HELD, "stack", None)
    if held is None:
        held = _HELD.stack = []
    rank, key = _lock_rank(path), str(Path(path).resolve())
    for hrank, hkey in held:
        if hkey == key:
            raise LockOrderError(
                "cloud_pool: %s re-locked by the thread that holds it (self-deadlock)" % path
            )
        if hrank >= rank:
            raise LockOrderError(
                "cloud_pool: %s (rank %d) taken while holding %s (rank %d); nested "
                "locks must follow server < workers < ledger" % (path, rank, hkey, hrank)
            )
    if timeout is None:
        try:
            timeout = float(os.environ.get("CLOUD_POOL_LOCK_TIMEOUT_S", LOCK_TIMEOUT_S))
        except ValueError:
            timeout = LOCK_TIMEOUT_S
    _mkdir(path.parent, dir_mode)
    lock_path = str(path) + ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, mode)
    with contextlib.suppress(OSError):
        os.fchmod(fd, mode)
    with os.fdopen(fd, "r+") as fh:
        _flock_with_timeout(fh, lock_path, timeout)
        held.append((rank, key))
        try:
            with contextlib.suppress(OSError):  # diagnostics for a LockTimeout elsewhere
                fh.seek(0)
                fh.truncate()
                fh.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "user": getpass.getuser(),
                            "host": os.uname().nodename,
                            "t": round(_now(), 1),
                        }
                    )
                )
                fh.flush()
            yield from _json_locked_body(path, mode)
        finally:
            held.remove((rank, key))


def _json_locked_body(path, mode):
    """The read / yield / atomic-write part of _json_locked (runs with the flock held)."""
    state = {}
    if path.exists():
        try:
            state = json.loads(path.read_text() or "{}")
        except ValueError:
            aside = path.with_name(path.name + ".corrupt.%d" % int(_now()))
            path.replace(aside)
            state = {"__corrupt__": aside.name}  # the caller (workers()) may rebuild from it
            # the books restart empty (CU / Kaggle hours / live slots): say so, loudly
            print(
                "[cloud_pool] WARNING: %s was corrupt, moved to %s; its counts restart at zero"
                % (path, aside.name),
                file = sys.stderr,
                flush = True,
            )
    yield state
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(state, indent = 1))
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)
    tmp.replace(path)


def ledger_path():
    return shared_dir() / "cloud" / "ledger.json"


def workers_path():
    """Host-wide: every unix user and workspace sees (and can claim) every pool worker. The records
    hold no secret (a session's runtime token comes from the claimer's own Colab login, see
    attach_state), so the file is world-readable like the ledger."""
    return shared_dir() / "cloud" / "workers.json"


def legacy_workers_path():
    """Where workers lived when they were per unix user (merged into the shared file once)."""
    return user_dir() / "workers.json"


@contextlib.contextmanager
def ledger():
    with _json_locked(ledger_path()) as st:
        st.setdefault(
            "slots", {}
        )  # id -> {backend, account|token_fp, tier, family, user, t, expires_at}
        st.setdefault(
            "kaggle", {}
        )  # token_fp -> [[t, hours, "settled", id]]  wall hours, display only
        st.setdefault(
            "kaggle_bench", {}
        )  # token_fp -> {"until": t, "why": str}  host-wide refusal benches
        st.setdefault("colab_bench", {})  # "<account>:<family>" -> {"until": t, "why": str}
        st.setdefault("accounts", {})  # account -> {"slots": n, "probed_at": t}
        st.setdefault("corrections", {})  # tier -> {class: ratio}
        st.setdefault("sync", {})  # "kaggle:<fp>" {used_h, t, env}
        st.pop("__corrupt__", None)
        _prune(st)
        yield st


@contextlib.contextmanager
def workers():
    with _json_locked(workers_path()) as st:
        st.setdefault("workers", {})
        if st.pop("__corrupt__", None):
            _rebuild_workers(st)
        _merge_legacy_workers(st)
        yield st


def _rebuild_workers(st):
    """A corrupt shared workers.json (kept aside by _json_locked) must not read as "no workers": every
    pool VM would look like an orphan, and adopting one mid-job would hand it to a second job. So the
    records come back from the host-wide pool registry (server_<account>.json: endpoint -> worker,
    written by each owner) as REBUILT: never claimed, never stopped. A REBUILT worker whose owner
    still runs the job is released normally by that owner; sweep turns it into IDLE + needs_clean
    once its ledger slot (held for the job) has lapsed."""
    now, n = _now(), 0
    slots = {}
    with contextlib.suppress(Exception):
        with ledger() as lst:  # workers -> ledger: the lock order
            slots = {k: dict(v) for k, v in lst["slots"].items()}
    for acct in COLAB_ACCOUNTS:
        try:  # read-only and without its lock: the server lock ranks below workers
            c = json.loads((shared_dir() / "cloud" / ("server_%s.json" % acct)).read_text() or "{}")
        except (OSError, ValueError):
            c = {}
        fam = {
            x.get("endpoint"): x.get("family")
            for x in c.get("sessions") or []
            if isinstance(x, dict)
        }
        listed = set(fam) if c.get("ok") else set()
        reg = {ep: v for ep, v in _pool_journal(acct).items() if ep in listed}
        reg.update(c.get("pool") or {})
        for ep, v in reg.items():
            name = (v or {}).get("worker") or ""
            if (
                not name.startswith(POOL_PREFIX)
                or name in st["workers"]
                or (now - float(v.get("t", 0)) > POOL_RUNTIME_TTL_S and ep not in listed)
            ):
                continue
            st["workers"][name] = {
                "tier": _tier_from_name(name, fam.get(ep)),
                "account": acct,
                "state": "REBUILT",
                "gen": 1,
                "owner_pid": None,
                "created": now,
                "endpoint": ep,
                "slot": name,
                "jobs": 0,
                "needs_clean": True,
                "rebuilt_at": now,
                "user": v.get("user"),
            }
            who = slots.get(name) or {}
            if who.get("owner_pid") and owner_alive(who):
                # its job still runs: BUSY under that owner, never cleaned out from under it
                st["workers"][name].update(
                    state = "BUSY",
                    needs_clean = False,
                    **{
                        k: who.get(k)
                        for k in ("owner_pid", "owner_start", "owner_boot", "owner_pidns")
                    },
                )
            n += 1
    print(
        "[cloud_pool] WARNING: rebuilt %d pool worker record(s) from the pool registry; none is stopped, "
        "each is cleaned before its next job" % n,
        file = sys.stderr,
        flush = True,
    )


def _tier_from_name(name, fallback):
    """sb-<tier>-<account>-<hex> -> the tier name in TIERS (sb-a100-hm-daniel-xx -> A100-HM)."""
    tiers = load_tiers()
    body = name[len(POOL_PREFIX) :].rsplit("-", 2)[0] if name.count("-") >= 3 else ""
    for t in tiers:
        if t.lower() == body:
            return t
    return fallback if fallback in tiers else body.upper()


def _merge_legacy_workers(st):
    """Fold this user's old per-user workers.json into the shared one (once; the file is renamed)."""
    legacy = legacy_workers_path()
    if not legacy.exists():
        return
    try:
        old = json.loads(legacy.read_text() or "{}").get("workers", {})
    except (OSError, ValueError):
        old = {}
    me = getpass.getuser()
    for name, w in old.items():
        if name not in st["workers"] and isinstance(w, dict):
            st["workers"][name] = dict(w, user = w.get("user") or me)
    with contextlib.suppress(OSError):
        legacy.replace(legacy.with_name(legacy.name + ".migrated"))


def _prune(st):
    now = _now()
    st.pop("colab_cu", None)  # credits are no longer tracked
    for fp, entries in list(st["kaggle"].items()):
        keep = [e for e in entries if e[0] >= now - 7 * 24 * 3600]
        if keep:
            st["kaggle"][fp] = keep
        else:
            st["kaggle"].pop(fp)
    st["slots"] = {k: v for k, v in st["slots"].items() if v.get("expires_at", now + 1) > now}
    st["kaggle_bench"] = {
        k: v for k, v in st.get("kaggle_bench", {}).items() if v.get("until", 0) > now
    }
    st["colab_bench"] = {
        k: v for k, v in st.get("colab_bench", {}).items() if v.get("until", 0) > now
    }


def load_tiers():
    tiers = json.loads(json.dumps(TIERS))
    p = shared_dir() / "tiers.json"
    try:
        over = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        over = {}
    for name, fields in (over.get("tiers") or {}).items():
        tiers.setdefault(name, {}).update(fields)
    return tiers


def hf_read_token():
    """The shared read-only HF token (public repos, rate limits only)."""
    with contextlib.suppress(OSError):
        tok = (shared_dir() / "hf_read_token").read_text().strip()
        if tok:
            return tok
    try:
        import gpu_queue
        return gpu_queue.hf_read_token() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Books: CU, slots, Kaggle hours
# ---------------------------------------------------------------------------------------------

SYNC_MAX_AGE_S = 7 * 24 * 3600


def account_slots(st, account, tier):
    """Sessions the pool may hold for `account` in `tier`'s family: the tier's slots capped at
    COLAB_USE ($CLOUD_POOL_COLAB_USE, 3 of 3), lowered by a probe of that same family (a probe that
    names no tier is an account-wide cap, account_cap)."""
    tiers = load_tiers()
    n = min(int(tiers[tier].get("slots", 3)), COLAB_USE)
    probe = st["accounts"].get(account, {})
    ptier = probe.get("tier")
    if (
        probe.get("slots")
        and ptier
        and tiers.get(ptier, {}).get("family") == tiers[tier].get("family")
    ):
        n = min(n, int(probe["slots"]))
    return n


def account_cap(st, account):
    """Account-wide session cap, or None: COLAB_ACCOUNTS `slots`, or a probe recorded without a tier."""
    probe = st["accounts"].get(account, {})
    caps = [
        int(x)
        for x in (
            COLAB_ACCOUNTS[account].get("slots"),
            probe.get("slots") if not probe.get("tier") else None,
        )
        if x
    ]
    return min(caps) if caps else None


def slots_used(st, account, family):
    return sum(
        1
        for v in st["slots"].values()
        if v.get("backend") == "colab" and v.get("account") == account and v.get("family") == family
    )


def account_used(st, account):
    return sum(
        1
        for v in st["slots"].values()
        if v.get("backend") == "colab" and v.get("account") == account
    )


AUTH_BAD_TTL_S = 600


def account_ready(account):
    """False when this unix user cannot drive `account`: no token in its oauth2 HOME (no network
    call; ncr's own check would sit 120 s on the interactive prompt), or a CREDENTIAL ERROR within
    AUTH_BAD_TTL_S. Per unix user, so it lives in the user's auth.json (auth_state), not a host-wide file."""
    home, auth = _account_auth(account)
    if (
        home
        and auth == "oauth2"
        and not (Path(home) / ".config" / "colab-cli" / "token.json").exists()
    ):
        return False
    with contextlib.suppress(Exception):
        with auth_state() as ws:
            bad = ws.get("auth_bad", {}).get(account)
            if bad and _now() - bad < AUTH_BAD_TTL_S:
                return False
    return True


@contextlib.contextmanager
def auth_state():
    """This unix user's credential cooldowns: another user's rejected login says nothing about ours."""
    with _json_locked(user_dir() / "auth.json", mode = 0o600, dir_mode = 0o700) as st:
        yield st


def mark_auth_bad(account):
    with contextlib.suppress(Exception):
        with auth_state() as ws:
            ws.setdefault("auth_bad", {})[account] = round(_now(), 1)


COLAB_CREDIT_BENCH_S = 24 * 3600
COLAB_BUSY_BENCH_S = 10 * 60
# A fresh worker that has not reached `colab exec` this long after its claim is stuck (observed: 49 min
# PROVISIONING with a live owner and no session on the server, its slot held the whole time). The
# owner stops waiting and sweep reclaims it; either benches the (account, family) for a while.
POOL_PROVISION_DEADLINE_S = int(os.environ.get("POOL_PROVISION_DEADLINE_S") or 900)
PROVISION_BENCH_S = 30 * 60
PROVISION_RC = 124
# Raw `colab new` refusals (ncr quotes them after "Colab said:" / "failed:"). Credits are checked
# first: a plan with no compute units left can also say "quota".
COLAB_CREDIT_MARKERS = (
    "compute unit",
    "out of compute",
    "no compute",
    "insufficient compute",
    "purchase more",
    "buy more",
    "billing",
    "payment required",
    "402",
    "subscription",
    "credits",
)
COLAB_BUSY_MARKERS = (
    "too many active sessions",
    "too many sessions",
    "toomanyassignments",
    "precondition failed",
    " 412",
    "429",
    "rate limit",
    "session count",
    "resource exhausted",
    "no accelerator",
    "quota exceeded",
    "unavailable",
    "capacity",
    "not available",
)


def classify_colab_refusal(text):
    """ "credits" (bench a day), "busy" (bench 10 min) or None (not a refusal) for `colab new` output."""
    low = (text or "").lower()
    for m in ("colab said:", "failed:"):
        if m in low:  # only the platform's words, not ncr's own advice around them
            low = low.split(m, 1)[1]
    if any(m in low for m in COLAB_CREDIT_MARKERS):
        return "credits"
    if any(m in low for m in COLAB_BUSY_MARKERS):
        return "busy"
    return None


def _bench_family(t):
    return t.get("bench_family") or t["family"]


def colab_benched(
    st,
    account,
    tier,
    tiers = None,
):
    fam = _bench_family((tiers or load_tiers())[tier])
    b = st.get("colab_bench", {}).get("%s:%s" % (account, fam))
    return bool(b and b.get("until", 0) > _now())


def bench_colab(
    account,
    tier,
    kind,
    why = "",
):
    fam = _bench_family(load_tiers()[tier])
    hold = {"credits": COLAB_CREDIT_BENCH_S, "stuck": PROVISION_BENCH_S}.get(
        kind, COLAB_BUSY_BENCH_S
    )
    with ledger() as st:
        st["colab_bench"]["%s:%s" % (account, fam)] = {
            "until": round(_now() + hold, 1),
            "why": why[:200],
        }


SERVER_TTL_S = 60  # one `colab sessions` per account per minute, host-wide
POOL_RUNTIME_TTL_S = 26 * 3600  # a Colab VM lives at most 24 h
_SESSION_LINE = re.compile(
    r"^\[(?P<name>[^\]]*)\]\s+(?P<endpoint>\S+)\s*\|\s*Hardware:\s*(?P<hw>[^|]+?)\s*"
    r"\|\s*Shape:\s*(?P<shape>[^|]+?)\s*(?:\||$)"
)


def session_family(hardware, shape):
    """Tier family of a server-side session line (`Hardware: A100 | Shape: High-RAM` -> A100-HM)."""
    hw = (hardware or "").strip().upper()
    if hw in ("A100", "T4"):
        return hw + "-HM" if "HIGH" in (shape or "").upper() else hw
    return hw


def parse_sessions(text):
    """`colab sessions` lines -> [{name, endpoint, hardware, shape, family}]. `[?]` = a session this
    HOME's local state does not know (another unix user, workspace, or a per-session --config)."""
    out = []
    for line in (text or "").splitlines():
        m = _SESSION_LINE.match(line.strip())
        if m:
            out.append(
                {
                    "name": m["name"],
                    "endpoint": m["endpoint"],
                    "hardware": m["hw"].strip(),
                    "shape": m["shape"].strip(),
                    "family": session_family(m["hw"], m["shape"]),
                }
            )
    return out


def _fetch_server_sessions(account, run = subprocess.run):
    """(ok, sessions) from the account's own `colab sessions`, which lists every session of that Google
    account whoever started it. ok False = unknown (no login for this unix user, CLI error)."""
    if not account_ready(account):
        return False, []
    home, auth = _account_auth(account)
    cmd = [ncr.colab_cli() or "colab"] + (["--auth", auth] if auth != "adc" else []) + ["sessions"]
    env = dict(os.environ, **({"HOME": home} if home else {}))
    try:
        r = run(cmd, env = env, capture_output = True, text = True, timeout = 60, stdin = subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False, []
    text = (r.stdout or "") + (r.stderr or "")
    sess = parse_sessions(text)
    if r.returncode != 0 or not (sess or "no active session" in text.lower()):
        return False, []
    return True, sess


def worker_endpoint(worker):
    """The runtime endpoint of one of this user's pool workers, from its named-session state file."""
    if worker.get("endpoint"):
        return worker["endpoint"]
    home, _ = _account_auth(worker["account"])
    state_home = Path(home) / "nbrun" if home else ncr.colab_state_home()
    state = state_home / ("session-%s.json" % ncr._safe_filename(worker["name"]))
    try:
        data = json.loads(state.read_text() or "{}")
    except (OSError, ValueError):
        return None
    for v in data.values() if isinstance(data, dict) else []:
        if isinstance(v, dict) and v.get("endpoint"):
            return v["endpoint"]
    return None


def server_sessions(
    account,
    max_age = SERVER_TTL_S,
    _fetch = None,
):
    """Host-wide cached view of the account's live sessions: {t, ok, sessions, pool}. `pool` maps
    endpoint -> {worker, user} for pool workers (registered by their owner), so every user can tell
    pool VMs from external ones (e.g. a workspace calling notebook_cloud_run.py directly). The cache
    file's flock doubles as the refresh lock: parallel callers wait for one fetch, then reuse it."""
    own = {}
    with contextlib.suppress(Exception):
        with workers() as ws:
            for name, w in ws["workers"].items():
                if w.get("account") == account and w.get("state") not in ("PROVISIONING",):
                    ep = worker_endpoint(dict(w, name = name))
                    if ep:
                        w["endpoint"] = ep
                        own[ep] = name
    now = _now()
    with _json_locked(shared_dir() / "cloud" / ("server_%s.json" % account)) as c:
        if now - c.get("t", 0) >= max_age:
            ok, sess = (_fetch or _fetch_server_sessions)(account)
            c.update(t = _now(), ok = ok, sessions = sess)
        listed = (
            {x.get("endpoint") for x in c.get("sessions") or [] if isinstance(x, dict)}
            if c.get("ok")
            else set()
        )
        # an entry lives while its TTL runs OR the server still lists its VM: a lost record whose
        # entry aged out would otherwise turn a billing pool VM into "unmanaged" for good
        journal = _pool_journal(account)
        pool = {ep: v for ep, v in journal.items() if ep in listed}  # a wiped / corrupt cache
        pool.update(
            {
                ep: v
                for ep, v in c.get("pool", {}).items()
                if now - v.get("t", 0) < POOL_RUNTIME_TTL_S or ep in listed
            }
        )
        new = {}
        for ep, name in own.items():
            pool[ep] = {"worker": name, "user": getpass.getuser(), "t": round(now, 1)}
            if (journal.get(ep) or {}).get("worker") != name:
                new[ep] = pool[ep]
        c["pool"] = pool
        _pool_journal_write(account, journal, new, keep = set(pool))
        return {
            "t": c["t"],
            "ok": c.get("ok", False),
            "sessions": list(c.get("sessions", [])),
            "pool": dict(pool),
        }


def _pool_journal_path(account):
    return shared_dir() / "cloud" / ("pool_%s.jsonl" % account)


def _pool_journal(account):
    """endpoint -> {worker, user, t}: every endpoint the pool registered for `account`, append-only
    (one JSON object per line, a torn or bad line is skipped), so the registry survives a corrupt or
    deleted server_<account>.json. Read and written under that file's lock."""
    out = {}
    with contextlib.suppress(OSError):
        for line in _pool_journal_path(account).read_text().splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                v = json.loads(line)
                if str(v.get("worker", "")).startswith(POOL_PREFIX) and v.get("ep"):
                    out[v["ep"]] = {
                        "worker": v["worker"],
                        "user": v.get("user"),
                        "t": v.get("t", 0),
                    }
    return out


def _pool_journal_write(account, journal, new, keep):
    """Append `new`; compact to the endpoints still in the registry (`keep`) once dropped lines pile up."""
    path = _pool_journal_path(account)
    with contextlib.suppress(OSError):
        if len(journal) > 2 * max(len(keep), 8):
            rows = {ep: v for ep, v in journal.items() if ep in keep}
            rows.update(new)
            tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
            tmp.write_text("".join(json.dumps(dict(v, ep = ep)) + "\n" for ep, v in rows.items()))
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o666)
            tmp.replace(path)
        elif new:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o666)
            with os.fdopen(fd, "a") as fh:
                fh.write("".join(json.dumps(dict(v, ep = ep)) + "\n" for ep, v in new.items()))


def server_counts(accounts = None, _fetch = None):
    """{account: {"total": n, family: n, ...}} from the server, only for accounts whose view is known."""
    out = {}
    for acct in accounts or COLAB_ACCOUNTS:
        with contextlib.suppress(Exception):
            v = server_sessions(acct, _fetch = _fetch)
            if v["ok"]:
                cnt = {"total": len(v["sessions"])}
                for x in v["sessions"]:
                    cnt[x["family"]] = cnt.get(x["family"], 0) + 1
                out[acct] = cnt
    return out


def colab_has_slot(
    st,
    account,
    tier,
    tiers,
    server = None,
):
    """Room for one more session. `server` (server_counts) covers sessions the ledger never saw,
    e.g. notebook_cloud_run.py run directly by another workspace: the larger count wins."""
    fam = tiers[tier]["family"]
    srv = (server or {}).get(account, {})
    if max(slots_used(st, account, fam), srv.get(fam, 0)) >= account_slots(st, account, tier):
        return False
    cap = account_cap(st, account)
    return cap is None or max(account_used(st, account), srv.get("total", 0)) < cap


def take_slot(
    st,
    backend,
    tier,
    tiers,
    account = None,
    token_fp = None,
    hold_s = 3600,
    sid = None,
    jobs = (),
):
    """Book one host-wide slot. Callers hold the ledger lock and re-check room under it, so racing
    dispatches never double-book. `jobs`: the job ids the slot serves (slot_jobs)."""
    sid = sid or uuid.uuid4().hex[:12]
    me = owner()
    st["slots"][sid] = {
        "backend": backend,
        "account": account,
        "token_fp": token_fp,
        "tier": tier,
        "family": tiers[tier]["family"],
        "user": getpass.getuser(),
        "pid": os.getpid(),
        "t": round(_now(), 1),
        "expires_at": _now() + hold_s,
        "jobs": list(jobs),
        **{k: me[k] for k in ("owner_pid", "owner_start", "owner_boot", "owner_pidns")},
    }
    return sid


def set_slot_owner(
    sid,
    who,
    job = None,
):
    """The slot follows its worker's current owner (a warm claim), so a rebuilt record can tell a
    live job from an idle VM; `job` replaces the previous job's id (slot_jobs: booked once)."""
    with ledger() as st:
        if sid in st["slots"]:
            st["slots"][sid].update(
                {k: who.get(k) for k in ("owner_pid", "owner_start", "owner_boot", "owner_pidns")}
            )
            if job:
                st["slots"][sid]["jobs"] = [job]


def slot_jobs(st = None):
    """Job ids holding a slot now (a job counts once its dispatch has booked its machine)."""

    def ids(state):
        return {j for v in state["slots"].values() for j in v.get("jobs") or []}

    if st is not None:
        return ids(st)
    with ledger() as st_:
        return ids(st_)


def free_capacity(
    cand,
    pending = None,
    st = None,
    server = None,
    warm = None,
    env = None,
):
    """Free slots right now on the machine a select_tier candidate names: Kaggle kernels left on its
    token (within the cross-token KAGGLE_KERNELS_TOTAL), or Colab sessions left for (account, tier
    family) plus IDLE warm workers of it. pending: {(backend, tier, account or token_env): n} the
    caller picked but has not booked yet; they are subtracted (Kaggle's also from the total)."""
    tiers = load_tiers()
    pending = pending or {}
    key = (cand.get("backend"), cand.get("tier"), cand.get("account") or cand.get("token_env"))

    def calc(state):
        if cand["backend"] == "kaggle":
            tok = (
                (os.environ if env is None else env).get(cand.get("token_env") or "") or ""
            ).strip()
            if not tok or kaggle_benched(tok, state):
                return 0
            mine = pending.get(key, 0)
            allk = sum(n for k, n in pending.items() if k[0] == "kaggle")
            return max(
                0,
                min(
                    KAGGLE_KERNELS_PER_TOKEN - kaggle_kernels_live(state, tok) - mine,
                    KAGGLE_KERNELS_TOTAL - kaggle_kernels_total(state, env) - allk,
                ),
            )
        if cand["backend"] != "colab":
            return 0
        acct, tier = cand["account"], cand["tier"]
        if colab_benched(state, acct, tier, tiers):
            return 0
        fam = tiers[tier]["family"]
        srv = (server or {}).get(acct, {})
        n = account_slots(state, acct, tier) - max(slots_used(state, acct, fam), srv.get(fam, 0))
        cap = account_cap(state, acct)
        if cap is not None:
            n = min(n, cap - max(account_used(state, acct), srv.get("total", 0)))
        idle = sum(
            1
            for w in (warm if warm is not None else idle_workers())
            if w.get("tier") == tier and w.get("account") == acct
        )
        return max(0, max(0, n) + idle - pending.get(key, 0))

    if st is not None:
        return calc(st)
    if warm is None and cand.get("backend") == "colab":
        warm = idle_workers()  # before the ledger lock: workers -> ledger is the lock order
    with ledger() as st_:
        return calc(st_)


def extend_slot(sid, hold_s):
    with ledger() as st:
        if sid in st["slots"]:
            st["slots"][sid]["expires_at"] = _now() + hold_s


def free_slot(sid):
    with ledger() as st:
        st["slots"].pop(sid, None)


def kaggle_tokens(env = None):
    env = os.environ if env is None else env
    return [(n, env[n].strip()) for n in ncr.kaggle_token_env_names(env)]


def _ncr_hours_after(fp, t):
    entries = ncr.KaggleUsageLedger()._load().get(fp, [])
    return (
        sum(e[1] for e in entries if e[0] > t and e[1] not in ncr._KAGGLE_LEGACY_MARKS_S) / 3600.0
    )


def kaggle_used_hours(
    st,
    token,
    now = None,
):
    """Hours used this week, for display only (selection never gates on it: the tracked number was
    wrong both ways, 93.9 vs 15.8 h real and 2.6 vs 21.4 h real). With a `kaggle-sync` baseline under
    SYNC_MAX_AGE_S: the website number plus what the tools recorded after it; else the host ledger or
    this user's ncr ledger, whichever saw more."""
    now = now or _now()
    live = ncr.kaggle_quota(token)
    if live:
        return float(live["used_h"])
    fp = ncr._token_fingerprint(token)
    sync = st.get("sync", {}).get("kaggle:" + fp)
    if sync and now - sync["t"] < SYNC_MAX_AGE_S:
        shared_after = sum(e[1] for e in st["kaggle"].get(fp, []) if e[0] > sync["t"])
        return sync["used_h"] + max(shared_after, _ncr_hours_after(fp, sync["t"]))
    shared = sum(e[1] for e in st["kaggle"].get(fp, []))
    local = ncr.KaggleUsageLedger().used_hours(token)
    return max(shared, local)


def kaggle_sync(
    env_name,
    used_h = None,
    left_h = None,
    env = None,
):
    """Set the week's baseline from the Kaggle website (Settings > quota); neither provider has a
    quota API. Returns the stored record."""
    env = os.environ if env is None else env
    tok = (env.get(env_name) or "").strip()
    if not tok:
        raise SystemExit("%s is not set" % env_name)
    if used_h is None:
        used_h = ncr.kaggle_weekly_hours(env_name) - float(left_h)
    rec = {"used_h": round(float(used_h), 2), "t": round(_now(), 1), "env": env_name}
    with ledger() as st:
        st["sync"]["kaggle:" + ncr._token_fingerprint(tok)] = rec
    return rec


def _ago(t):
    d = max(0, _now() - t)
    return (
        "%.0f min" % (d / 60)
        if d < 5400
        else ("%.1f h" % (d / 3600) if d < 172800 else "%.0f d" % (d / 86400))
    )


def kaggle_benched(
    token,
    st = None,
    now = None,
):
    """Benched after a Kaggle refusal: host-wide (mirrored by dispatch_kaggle) or in this user's ncr
    ledger (a standalone notebook_cloud_run run)."""
    now = now or _now()
    if st is not None:
        b = st.get("kaggle_bench", {}).get(ncr._token_fingerprint(token))
        if b and b.get("until", 0) > now:
            return True
    return ncr.KaggleUsageLedger().saturated(token)


_RNG = random.Random()


def kaggle_draw(tokens, rng = None):
    """[(env_name, token)] reordered by weighted random sampling without replacement, weight = weekly
    allowance (60 : 45 : 30): the first is tried first, the other is the retry."""
    rng = rng or _RNG
    left, out = list(tokens), []
    while left:
        pick = rng.choices(left, weights = [ncr.kaggle_weekly_hours(n) for n, _ in left])[0]
        out.append(pick)
        left.remove(pick)
    return out


def kaggle_kernels_total(st, env = None):
    """Kernels running host-wide: every pool Kaggle slot in the ledger, whichever token booked it (another
    unix user may hold a token this environment lacks, or an older value of one), plus this user's
    direct notebook_cloud_run kernels on the tokens it has."""
    fps = {ncr._token_fingerprint(tok) for _n, tok in kaggle_tokens(env)}
    pool = sum(1 for v in st["slots"].values() if v.get("backend") == "kaggle")
    mine = sum(
        1 for v in st["slots"].values() if v.get("backend") == "kaggle" and v.get("token_fp") in fps
    )
    return pool + sum(kaggle_kernels_live(st, tok) for _n, tok in kaggle_tokens(env)) - mine


def kaggle_room(
    st,
    token,
    env = None,
):
    """Kernels the pool may start now on `token`: Kaggle's 2 per token, and KAGGLE_KERNELS_TOTAL
    ($CLOUD_POOL_KAGGLE_KERNELS, 6) across all tokens. 0 when benched."""
    if kaggle_benched(token, st):
        return 0
    return max(
        0,
        min(
            KAGGLE_KERNELS_PER_TOKEN - kaggle_kernels_live(st, token),
            KAGGLE_KERNELS_TOTAL - kaggle_kernels_total(st, env),
        ),
    )


def kaggle_kernels_live(st, token):
    """Kernels running on `token`: pool slots, plus kernels this user's notebook_cloud_run pushed
    directly (its kernel index; live runner pid, no pool slot) which the ledger never saw. Those
    used to let a third kernel be pushed into Kaggle's 2-per-account cap."""
    fp = ncr._token_fingerprint(token)
    pool = sum(
        1 for v in st["slots"].values() if v.get("backend") == "kaggle" and v.get("token_fp") == fp
    )
    direct = 0
    with contextlib.suppress(Exception):
        direct = sum(
            1
            for r in ncr.load_kaggle_index().values()
            if r.get("token_fp") == fp and not r.get("pool_slot") and ncr.kaggle_record_live(r)
        )
    return pool + direct


KAGGLE_QUOTA_MARGIN = 1.2


def kaggle_quota_fits(quota, predicted_s):
    """True unless the live quota says the token cannot cover this run (predicted x margin + setup).
    Unknown quota (None) never blocks: Kaggle's refusal stays the backstop."""
    if not quota:
        return True
    need_h = (predicted_s * KAGGLE_QUOTA_MARGIN + COLD_SETUP_S) / 3600.0
    return quota.get("remaining_h", 0.0) >= max(ncr.KAGGLE_MIN_REMAINING_H, need_h)


# ---------------------------------------------------------------------------------------------
# Roofline runtime prediction + selection
# ---------------------------------------------------------------------------------------------


def _flops(gpu, dtype):
    g = GPUS[gpu]
    if dtype in ("fp8", "nvfp4") and dtype in g:
        return g[dtype]
    if dtype == "fp32":
        return g["fp16"] / 4
    return g["fp16"]


def predict_s(
    est_s,
    tier,
    dtype = "bf16",
    job_class = "train",
    ref = "B200",
    corrections = None,
    gb = None,
):
    """Runtime on `tier` from `est_s` measured (or guessed) on `ref`:
    est_s * (o + (1 - o) * (a * F_ref / F_tier + (1 - a) * BW_ref / BW_tier)) * learned correction,
    o = FIXED_SHARE; jobs under SMALL_GB on a B200 reference use SMALL_REF instead."""
    tiers = load_tiers()
    gpu = tiers[tier]["gpu"] if tier in tiers else tier
    a = COMPUTE_SHARE.get(job_class, 0.6)
    # the reference ran the same dtype if it could; B200 runs everything
    if gb is not None and gb < SMALL_GB and ref == "B200":
        ref = SMALL_REF
    ratio = (
        a * _flops(ref, dtype) / _flops(gpu, dtype) + (1 - a) * GPUS[ref]["bw"] / GPUS[gpu]["bw"]
    )
    o = FIXED_SHARE.get(job_class, 0.3)
    ratio = max(o, o + (1 - o) * ratio)
    corr = ((corrections or {}).get(gpu) or {}).get(job_class, 1.0)
    return est_s * ratio * corr


MIN_LEARN_S = 60  # shorter runs are start-up noise: a 4 s job taught T4-HM a 0.25x speed-up


def _gpu_of(tier):
    return load_tiers().get(tier, {}).get("gpu", tier)


def record_runtime(tier, job_class, predicted, actual):
    """Learn a per-(GPU, class) correction (EWMA of actual/predicted), bounded to [0.25, 4]. Keyed by
    GPU, not tier: T4 and T4-HM are the same card and must not drift apart."""
    if not predicted or not actual or predicted <= 0 or actual < MIN_LEARN_S:
        return
    with ledger() as st:
        c = st["corrections"].setdefault(_gpu_of(tier), {})
        old = c.get(job_class, 1.0)
        c[job_class] = round(max(0.25, min(4.0, 0.7 * old + 0.3 * old * actual / predicted)), 4)


def _norm_dtype(d):
    d = (d or "bf16").lower().replace("float", "fp").replace("bfloat16", "bf16")
    return {
        "fp16": "fp16",
        "half": "fp16",
        "bf16": "bf16",
        "fp8": "fp8",
        "e4m3": "fp8",
        "nvfp4": "nvfp4",
        "fp4": "nvfp4",
        "fp32": "fp32",
        "4bit": "fp16",
        "int4": "fp16",
    }.get(d, d)


def select_tier(
    gb,
    dtype = "bf16",
    ram_gb = 0.0,
    est_s = 600,
    job_class = "train",
    perf = False,
    multi_gpu = False,
    ref = "B200",
    backends = ("kaggle", "colab"),
    env = None,
    _st = None,
    _warm = None,
    rng = None,
    _server = None,
    disk_gb = 0.0,
):
    """Ranked candidates, cheapest first. Each: {backend, tier, account, token_env, predicted_s,
    expected_cu, reason}. Empty list = nothing remote fits (the caller keeps waiting locally)."""
    dtype = _norm_dtype(dtype)
    tiers = load_tiers()
    ctx = contextlib.nullcontext(_st) if _st is not None else ledger()
    warm = _warm if _warm is not None else idle_workers()
    if _server is None:
        _server = server_counts() if ("colab" in backends and _st is None) else {}
    # account_ready reads workers.json; asking for it while holding ledger.json was the ABBA
    # deadlock against claim_worker / release_worker (workers -> ledger). Resolve it first.
    ready = {acct: account_ready(acct) for acct in COLAB_ACCOUNTS} if "colab" in backends else {}
    # live Kaggle quota (cached 10 min per user) before the lock too: it is a CLI call
    live_quota = (
        {tok: ncr.kaggle_quota(tok) for _, tok in kaggle_tokens(env)}
        if "kaggle" in backends
        else {}
    )
    out = []
    with ctx as st:
        corr = st.get("corrections", {})
        for name, t in tiers.items():
            if t["backend"] not in backends or dtype not in t["dtypes"]:
                continue
            vram = t["vram_gb"] * (t.get("gpus", 1) if multi_gpu else 1)
            if gb > vram or ram_gb > t["ram_gb"]:
                continue
            # scratch free as last measured on this tier (record_disk); unknown = allowed, and the
            # remote disk gate answers DISK_SHORT before installing anything
            seen = (st.get("disk_seen") or {}).get(name, {}).get("free")
            if disk_gb and seen is not None and seen < disk_gb:
                continue
            if multi_gpu and t.get("gpus", 1) < 2:
                continue
            if t["backend"] == "amd_ci":
                # no roofline for gfx1151 yet; queue position, not cost, is what matters here
                live = sum(1 for x in st.get("amd_live", {}).values() if x.get("tier") == name)
                if live >= (t.get("perf_slots", 1) if perf else t["slots"]):
                    continue
                out.append(
                    {
                        "backend": "amd_ci",
                        "tier": name,
                        "account": None,
                        "token_env": None,
                        "predicted_s": None,
                        "expected_cu": 0.0,
                        "sort": (2, live),
                        "reason": "%s: %d of %d slots busy%s; dispatch via amd_ci/scaffold.py"
                        % (name, live, t["slots"], ", timing takes the box alone" if perf else ""),
                    }
                )
                continue
            pred = predict_s(est_s, name, dtype, job_class, ref, corr, gb = gb)
            if pred > t.get("max_s", MAX_RUNTIME_S[t["backend"]]):
                continue  # the platform would kill it first (Kaggle 12 h session, Colab 24 h VM)
            if t["backend"] == "kaggle":
                # no hours gate: Kaggle's refusal benches a token (kaggle_benched); live kernels are
                # the one real local limit (2 per token)
                ok = [
                    (n, tok)
                    for n, tok in kaggle_tokens(env)
                    if kaggle_room(st, tok, env) > 0
                    and kaggle_quota_fits(live_quota.get(tok), pred)
                ]
                total = sum(ncr.kaggle_weekly_hours(n) for n, _ in ok)
                for rank, (env_name, tok) in enumerate(kaggle_draw(ok, rng)):
                    out.append(
                        {
                            "backend": "kaggle",
                            "tier": name,
                            "account": None,
                            "token_env": env_name,
                            "predicted_s": round(pred),
                            "expected_cu": 0.0,
                            "sort": (0, rank),
                            "reason": "%s drawn %s (weight %g/%g), free quota"
                            % (
                                env_name,
                                "first" if rank == 0 else "as the retry",
                                ncr.kaggle_weekly_hours(env_name),
                                total,
                            ),
                        }
                    )
                continue
            rate = (
                t["cu_per_h"]
                if t["cu_per_h"] is not None
                else (tiers["G4"]["cu_per_h"] or 9.0) * 1.5
            )
            for acct, a in COLAB_ACCOUNTS.items():
                if a["tiers"] is not None and name not in a["tiers"]:
                    continue
                if not ready.get(acct) or colab_benched(st, acct, name, tiers):
                    continue
                is_warm = any(w["tier"] == name and w["account"] == acct for w in warm)
                if not is_warm and not colab_has_slot(st, acct, name, tiers, _server):
                    continue
                wall = pred + (WARM_SETUP_S if is_warm else COLD_SETUP_S)
                cu = rate * wall / 3600
                pref = bool(a.get("prefer"))
                # a warm idle worker of a fitting tier first, ahead of Kaggle too: setup is the slow part
                order = (
                    -1 if is_warm else 1,
                    round(cu, 3)
                    - (0.001 if pref else 0)
                    + (1000 if t.get("scarce") and name == "H100" else 0),
                    0 if pref else 1,
                )
                out.append(
                    {
                        "backend": "colab",
                        "tier": name,
                        "account": acct,
                        "token_env": None,
                        "predicted_s": round(pred),
                        "expected_cu": round(cu, 3),
                        "sort": order,
                        "warm": is_warm,
                        "reason": "%s%s %.2f CU/h x %.0f min (%s), %s"
                        % (
                            "warm " if is_warm else "",
                            name,
                            rate,
                            wall / 60,
                            "warm worker" if is_warm else "cold start",
                            acct,
                        ),
                    }
                )
    out.sort(key = lambda c: c["sort"])
    if perf:
        for c in out:
            c["reason"] += "; timing will be on %s, not %s" % (c["tier"], ref)
    for c in out:
        c.pop("sort", None)
    # one Kaggle line per token is enough; drop Colab duplicates of the same tier on a costlier account
    return out


# ---------------------------------------------------------------------------------------------
# The job wrapper that runs on the remote kernel (one cell)
# ---------------------------------------------------------------------------------------------

JOB_WRAPPER = r"""
import base64, glob, hashlib, io, json, os, shutil, signal, subprocess, sys, tarfile, threading, time
JOB = json.loads(base64.b64decode("__SB_JOB__").decode())
ROLLBACK = JOB.get("rollback", True)

def _unseal():
    # Secrets travel AES-GCM sealed; the key comes from ~/.sb/k (written by a separate one-line
    # Colab exec, then removed here) or, on Kaggle, from the job itself (Kaggle only ever gets the
    # shared read-only token, so there the seal only keeps it out of plain sight).
    sealed = JOB.get("sealed")
    if not sealed:
        return {}
    kp = os.path.expanduser("~/.sb/k")
    key = None
    if os.path.exists(kp):
        with open(kp) as fh:
            key = base64.b64decode(fh.read().strip())
        os.remove(kp)
    elif JOB.get("k"):
        key = base64.b64decode(JOB["k"])
    if key is None:
        print("SB_SEAL no key on this machine: the job runs without its secrets", flush=True)
        return {}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "cryptography"], capture_output=True)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(sealed)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))

SECRETS = _unseal()
UPLOAD_TOKEN = SECRETS.pop("__upload_token__", None)   # never in any job environment
_SECRET_VALUES = [v for v in list(SECRETS.values()) + [UPLOAD_TOKEN or ""] if isinstance(v, str) and len(v) >= 8]
_TOKEN_RE = __import__("re").compile(r"(hf_[A-Za-z0-9]{20,}|KGAT_[A-Za-z0-9_]{8,})")

def mask(text):
    for v in _SECRET_VALUES:
        text = text.replace(v, "***")
    return _TOKEN_RE.sub("***", text)
HEARTBEAT_S = 300
SB_HOME = os.path.expanduser("~/.sb")            # switchboard cache: uv binary, baseline, pythons
LEAN = ["tensorflow", "tensorflow-probability", "tensorflow-datasets", "tensorflow-hub", "tensorflow-text",
        "tf-keras", "keras", "jax", "jaxlib", "flax", "optax", "orbax-checkpoint", "chex"]

def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)

def disk_free_gb(p="/"):
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize / 2 ** 30

def gpu_used_mib():
    # Only this job's card: a Kaggle T4x2 kernel runs two jobs, one per GPU.
    vis = os.environ.get("CUDA_VISIBLE_DEVICES", "").replace(" ", "")
    sel = ["-i", vis] if vis and all(x.isdigit() for x in vis.split(",")) else []
    try:
        r = run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"] + sel)
    except OSError:
        return -1
    try:
        return sum(int(x) for x in r.stdout.split())
    except ValueError:
        return -1

def freeze_hash():
    r = run([sys.executable, "-m", "pip", "freeze", "--all"])
    return hashlib.sha256(r.stdout.encode()).hexdigest()[:16]

def scratch_root():
    # A big extra local disk (the A100 High-RAM "local-scratch") keeps venvs and models off the boot disk.
    if os.environ.get("SB_SCRATCH"):
        return os.environ["SB_SCRATCH"]
    best = None
    with open("/proc/mounts") as fh:
        for line in fh:
            dev, mnt = line.split()[:2]
            if not dev.startswith("/dev/") or mnt in ("/", "/content") or mnt.startswith(
                    ("/proc", "/sys", "/dev", "/boot", "/etc", "/usr", "/kaggle", "/opt")):
                continue
            try:
                free = disk_free_gb(mnt)
            except OSError:
                continue
            if free > 200 and os.access(mnt, os.W_OK) and (best is None or free > best[1]):
                best = (mnt, free)
    if best:
        return best[0]
    return "/content" if os.path.isdir("/content") else "/tmp"

os.makedirs(SB_HOME, exist_ok=True)
BASE = scratch_root()
os.makedirs(BASE, exist_ok=True)
CACHE = os.path.join(BASE, "sb_cache")
JOBDIR = os.path.join(BASE, "sbjobs", JOB["id"])
BASELINE = os.path.join(SB_HOME, "baseline.json")
print("SB_SCRATCH=%s free=%.0fGB" % (BASE, disk_free_gb(BASE)), flush=True)
# Disk gate before anything is installed: the shared caches (uv wheels, HF models kept for reuse)
# go only when the job would not fit otherwise, uv first since models are the expensive re-download.
NEED_GB = float(JOB.get("disk_gb") or 0)
FREE0 = disk_free_gb(BASE)
for sub_ in ("studio", "uv", "hf"):      # built Studio homes (remote_studio) go first: cheapest to rebuild
    if NEED_GB and FREE0 < NEED_GB and os.path.isdir(os.path.join(CACHE, sub_)):
        shutil.rmtree(os.path.join(CACHE, sub_), ignore_errors=True)
        print("SB_DISK evicted cache %s for a %.0f GB job" % (sub_, NEED_GB), flush=True)
        FREE0 = disk_free_gb(BASE)
DISK_SHORT = bool(NEED_GB and FREE0 < NEED_GB)
if DISK_SHORT:
    print("SB_DISK_SHORT free=%.1fGB need=%.1fGB on %s" % (FREE0, NEED_GB, BASE), flush=True)

if JOB.get("preclean"):
    # A worker reclaimed from a dead owner or adopted as an orphan: whatever its last job left behind
    # goes before this one starts. Kill every process in the jobs tree, drop the tree (caches stay),
    # verify GPU memory + system packages against the baseline; with no baseline (an adopted VM this
    # wrapper never saw) the GPU must be idle and the baseline below is recorded fresh.
    jobs_root = os.path.join(BASE, "sbjobs").encode()
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    if jobs_root in fh.read():
                        os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    shutil.rmtree(jobs_root.decode(), ignore_errors=True)
    base0 = json.load(open(BASELINE)) if os.path.exists(BASELINE) else {"gpu_mib": 0}
    deadline = time.time() + 30
    while gpu_used_mib() > base0["gpu_mib"] + 512 and time.time() < deadline:
        time.sleep(2)
    bad = []
    if gpu_used_mib() > base0["gpu_mib"] + 512:
        bad.append("gpu %d MiB > baseline %d + 512" % (gpu_used_mib(), base0["gpu_mib"]))
    if base0.get("freeze") and freeze_hash() != base0["freeze"]:
        bad.append("system site-packages changed")
    print("SB_CLEAN=" + ("OK" if not bad else "FAIL:" + "; ".join(bad)), flush=True)
    if bad:
        raise RuntimeError("reclaimed worker failed its clean: " + "; ".join(bad))

if ROLLBACK and not os.path.exists(BASELINE):
    if disk_free_gb("/") < 40:
        # Only under disk pressure: jobs run in their own venvs and never import Colab's stack.
        run([sys.executable, "-m", "pip", "uninstall", "-y"] + LEAN)
        print("SB_LEAN uninstalled unused preinstalled packages", flush=True)
    with open(BASELINE, "w") as fh:
        json.dump({"freeze": freeze_hash(), "gpu_mib": gpu_used_mib(), "t": time.time()}, fh)

uv = shutil.which("uv") or os.path.join(SB_HOME, "bin", "uv")
if not os.path.exists(uv):
    r = run("curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=%s sh"
            % os.path.join(SB_HOME, "bin"))
    if not os.path.exists(uv):   # no curl / blocked: a --target install leaves site-packages untouched
        run([sys.executable, "-m", "pip", "install", "-q", "--target", os.path.join(SB_HOME, "uvpkg"), "uv"])
        uv = os.path.join(SB_HOME, "uvpkg", "bin", "uv")

env = dict(os.environ)
# the platform's PYTHONPATH (Kaggle's sitecustomize dir, Colab's /env/python) leaks its preinstalled
# packages into the job venv and breaks it ("Error in sitecustomize: No module named 'wrapt'")
env.pop("PYTHONPATH", None)
env.update(JOB.get("env") or {})
env.update(SECRETS)
env.update(UV_CACHE_DIR=os.path.join(CACHE, "uv"), UV_PYTHON_INSTALL_DIR=os.path.join(SB_HOME, "python"),
           PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1", TMPDIR=os.path.join(JOBDIR, "tmp"))
if JOB.get("shared_models"):
    env["HF_HUB_CACHE"] = os.path.join(CACHE, "hf")
    env["HF_HOME"] = os.path.join(JOBDIR, "hf")
else:
    env["HF_HOME"] = os.path.join(JOBDIR, "hf")
for d in (JOBDIR, env["TMPDIR"], os.path.join(JOBDIR, "work"), CACHE):
    os.makedirs(d, exist_ok=True)

t_setup = time.time()
venv = os.path.join(JOBDIR, "venv")
py = os.path.join(venv, "bin", "python")
if not DISK_SHORT:
    r = run([uv, "venv", "--seed", "--python", JOB.get("python") or sys.executable, venv], env=env)
    print(r.stdout[-2000:] + r.stderr[-2000:], flush=True)
env["SB_UV"] = uv           # jobs that install packages themselves (jobs/ab_remote.py)
env["SB_CACHE"] = CACHE     # kept across jobs on a reused worker (clones, models)

def cuda_index():
    try:
        text = run(["nvidia-smi"]).stdout
    except OSError:
        text = ""
    m = __import__("re").search(r"CUDA Version:\s*(\d+)\.(\d+)", text)
    drv = (int(m.group(1)), int(m.group(2))) if m else (12, 1)
    for tag in ("cu130", "cu129", "cu128", "cu126", "cu124", "cu121", "cu118"):
        need = (int(tag[2:4]), int(tag[4:]))
        if drv >= need:
            return "https://download.pytorch.org/whl/" + tag
    return "https://download.pytorch.org/whl/cu118"

torch_spec = JOB.get("torch") or "auto"
if torch_spec != "none" and not DISK_SHORT:
    spec = ["torch"] if torch_spec == "auto" else torch_spec.split()
    if not any(x.split("=")[0].split("<")[0].split(">")[0] == "numpy" for x in spec):
        spec.append("numpy")        # torch warns and disables tensor.numpy() without it
    # The torch index alone (it mirrors torch's deps): an extra index would outrank it in uv and hand
    # back PyPI's CUDA build, which an older driver cannot load.
    tenv = {k: v for k, v in env.items() if k not in ("UV_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_INDEX", "PIP_EXTRA_INDEX_URL")}
    r = run([uv, "pip", "install", "--python", py] + spec + ["--index-url", cuda_index()], env=tenv)
    print("SB_TORCH rc=%d %s" % (r.returncode, (r.stdout + r.stderr)[-1500:]), flush=True)
reqs = list(JOB.get("requirements") or [])
if JOB.get("notebook_b64"):
    reqs += ["ipykernel", "nbclient", "nbformat"]
if reqs and not DISK_SHORT:
    r = run([uv, "pip", "install", "--python", py] + reqs, env=env)
    print("SB_REQS rc=%d %s" % (r.returncode, (r.stdout + r.stderr)[-3000:]), flush=True)
setup_s = time.time() - t_setup

work = os.path.join(JOBDIR, "work")
for rel, b64 in (JOB.get("files") or {}).items():
    dest = os.path.join(work, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(base64.b64decode(b64))
env["PATH"] = os.path.join(venv, "bin") + os.pathsep + env.get("PATH", "")
env["VIRTUAL_ENV"] = venv
env["JUPYTER_PATH"] = os.path.join(venv, "share", "jupyter")
if JOB.get("notebook_b64"):
    nb = os.path.join(work, JOB["name"])
    with open(nb, "wb") as fh:
        fh.write(base64.b64decode(JOB["notebook_b64"]))
    runner = ("import nbformat,sys\nfrom nbclient import NotebookClient\n"
              "nb=nbformat.read(sys.argv[1],as_version=4)\n"
              "c=NotebookClient(nb,timeout=%d,kernel_name='python3',resources={'metadata':{'path':'.'}})\n"
              "try:\n    c.execute()\nfinally:\n    nbformat.write(nb,sys.argv[1].replace('.ipynb','.executed.ipynb'))\n"
              % int(JOB.get("timeout_s", 7200)))
    cmd = [py, "-c", runner, nb]
else:
    cmd = [py, "-u", os.path.join(work, JOB["name"])] + list(JOB.get("argv") or [])
if DISK_SHORT:
    cmd = [sys.executable, "-c", "import sys; print('SB disk short: %.1f GB free, %.1f GB needed'); sys.exit(98)"
           % (FREE0, NEED_GB)]
elif not os.path.exists(py):
    # the venv never came up (bad python / uv): the job's failure, reported with a result so it is
    # not read as INFRA and retried on a fresh paid VM
    cmd = [sys.executable, "-c", "import sys; print('SB setup failed: no venv python at %s'); sys.exit(97)" % py]

base_mib = gpu_used_mib()
peak = [0]
done = threading.Event()
def sample():
    # 0.5 s first, backing off to 5 s: a short job allocates and exits before a flat 5 s tick
    step = 0.5
    while not done.wait(step):
        u = gpu_used_mib()
        if u >= 0:
            peak[0] = max(peak[0], u - max(base_mib, 0))
        step = min(5.0, step * 1.5)
threading.Thread(target=sample, daemon=True).start()

t0 = time.time()
proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, start_new_session=True)
def _kill():
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
timer = threading.Timer(int(JOB.get("timeout_s", 7200)), _kill)
timer.start()
tail = []
def pump():
    n = 0
    for line in proc.stdout:
        n += 1
        tail.append(line)
        del tail[:-400]
        if n <= 400 or "Error" in line or "SB_" in line:
            print(mask(line), end="", flush=True)
reader = threading.Thread(target=pump, daemon=True)
reader.start()
def heartbeat():
    # pump() goes quiet after 400 lines; the local idle watchdog (ncr --idle-timeout) reads silence
    # as a stalled kernel, so a healthy long job says so every few minutes.
    while not done.wait(HEARTBEAT_S):
        print("SB_HB %ds" % int(time.time() - t0), flush=True)
threading.Thread(target=heartbeat, daemon=True).start()
rc = proc.wait()
timer.cancel()
# a grandchild that left the process group can hold the pipe open forever; don't wait on it
reader.join(30)
done.set()
secs = time.time() - t0

# Job-written text artifacts are masked like the cell output (the job's own secrets, hf_ / KGAT_
# tokens) before they leave the VM by any channel; binaries (PNG, safetensors, ...) go as they are.
TEXT_EXT = (".log", ".txt", ".json", ".jsonl", ".md", ".out", ".ipynb", ".csv", ".yaml", ".yml", ".html",
            ".py", ".sh", ".err")

def _add_file(tar, fp, arc):
    if fp.lower().endswith(TEXT_EXT) and os.path.isfile(fp) and not os.path.islink(fp):
        try:
            with open(fp, "rb") as fh:
                masked = mask(fh.read().decode("utf-8")).encode("utf-8")
        except (OSError, UnicodeDecodeError):
            masked = None
        if masked is not None:
            ti = tar.gettarinfo(fp, arcname=arc)
            ti.size = len(masked)
            tar.addfile(ti, io.BytesIO(masked))
            return
    tar.add(fp, arcname=arc, recursive=False)

def add_masked(tar, p, base):
    if os.path.isdir(p) and not os.path.islink(p):
        tar.add(p, arcname=os.path.relpath(p, base), recursive=False)
        for r, ds, fs in os.walk(p):
            for d in ds:
                q = os.path.join(r, d)
                tar.add(q, arcname=os.path.relpath(q, base), recursive=False)
            for f in fs:
                q = os.path.join(r, f)
                _add_file(tar, q, os.path.relpath(q, base))
    else:
        _add_file(tar, p, os.path.relpath(p, base))

art = io.BytesIO()
with tarfile.open(fileobj=art, mode="w:gz") as tar:
    pats = list(JOB.get("artifacts") or []) + ["*.executed.ipynb"]
    for pat in pats:
        for p in glob.glob(os.path.join(work, pat), recursive=True):
            add_masked(tar, p, work)
data = art.getvalue()
UP = JOB.get("upload") or {}
if os.path.isdir("/kaggle/working") and len(data) > 64 * 1024:
    # Kaggle hands back all of /kaggle/working as files: no size cap worth a base64 stream
    name = "sbart_%s.tar.gz" % JOB["id"]
    with open(os.path.join("/kaggle/working", name), "wb") as fh:
        fh.write(data)
    print("SB_ART_FILE=%s bytes=%d" % (name, len(data)), flush=True)
elif len(data) <= JOB.get("artifact_max", 5 * 1024 * 1024):
    b64 = base64.b64encode(data).decode()
    for i in range(0, len(b64), 100000):
        print("SB_ART:" + b64[i:i + 100000], flush=True)
elif UP.get("repo") and UPLOAD_TOKEN:
    # too big for the notebook: the private artifact dataset, read back (and expired) by the host
    try:
        try:
            from huggingface_hub import HfApi
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], capture_output=True)
            from huggingface_hub import HfApi
        path = "%s/%s.tar.gz" % (UP["prefix"].rstrip("/"), JOB["id"])
        HfApi(token=UPLOAD_TOKEN).upload_file(path_or_fileobj=io.BytesIO(data), path_in_repo=path,
                                              repo_id=UP["repo"], repo_type="dataset")
        print("SB_ART_HF=%s:%s bytes=%d" % (UP["repo"], path, len(data)), flush=True)
    except Exception as exc:
        print("SB_ARTIFACT_SKIPPED bytes=%d upload failed: %s" % (len(data), mask(str(exc))[:300]), flush=True)
else:
    print("SB_ARTIFACT_SKIPPED bytes=%d" % len(data), flush=True)
UPLOAD_TOKEN = None
print("SB_RESULT=" + json.dumps({"rc": rc, "secs": round(secs, 1), "setup_secs": round(setup_s, 1),
      "peak_gpu_mib": peak[0], "scratch": BASE, "disk_free_gb": round(FREE0, 1), "disk_need_gb": NEED_GB,
      "disk_short": DISK_SHORT, "tail": mask("".join(tail[-40:]))[-4000:]}), flush=True)

if ROLLBACK:
    problems = []
    with __import__("contextlib").suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    for pid in os.listdir("/proc"):          # anything that detached from the group but lives in the job dir
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    if JOBDIR.encode() in fh.read():
                        os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    shutil.rmtree(JOBDIR, ignore_errors=True)
    base = json.load(open(BASELINE))
    deadline = time.time() + 30
    while gpu_used_mib() > base["gpu_mib"] + 512 and time.time() < deadline:
        time.sleep(2)
    if gpu_used_mib() > base["gpu_mib"] + 512:
        problems.append("gpu %d MiB > baseline %d + 512" % (gpu_used_mib(), base["gpu_mib"]))
    if freeze_hash() != base["freeze"]:
        problems.append("system site-packages changed")
    if disk_free_gb(BASE) < 20:
        shutil.rmtree(os.path.join(CACHE, "studio"), ignore_errors=True)   # Studio homes before models
        hub = os.path.join(CACHE, "hf")
        for d in sorted(glob.glob(os.path.join(hub, "*")), key=os.path.getmtime):
            shutil.rmtree(d, ignore_errors=True)
            if disk_free_gb(BASE) >= 20:
                break
        if disk_free_gb(BASE) < 20:
            shutil.rmtree(os.path.join(CACHE, "uv"), ignore_errors=True)
        if disk_free_gb(BASE) < 20:
            problems.append("disk %.1f GB free after evicting caches" % disk_free_gb(BASE))
    print("SB_ROLLBACK=" + ("OK" if not problems else "FAIL:" + "; ".join(problems)), flush=True)
if rc != 0:
    raise RuntimeError("job exited %d" % rc)
"""


SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|_KEY$", re.I)


def seal(values, key):
    """AES-GCM over a JSON dict -> base64(nonce + ciphertext)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return base64.b64encode(
        nonce + AESGCM(key).encrypt(nonce, json.dumps(values).encode(), None)
    ).decode()


def unseal(blob, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))


def split_secrets(env):
    """(plain env, secret env): secret = a name like *TOKEN*, *SECRET*, *PASSWORD*, *_KEY."""
    plain = {k: v for k, v in env.items() if not SECRET_KEY_RE.search(k)}
    return plain, {k: v for k, v in env.items() if SECRET_KEY_RE.search(k)}


def key_notebook(key, dest):
    """The one-line Colab cell that leaves the job's key at ~/.sb/k (0600) for the job cell, which
    removes it. Its own source and executed copy are deleted locally once the run ends."""
    code = (
        "import os\np = os.path.expanduser('~/.sb'); os.makedirs(p, exist_ok=True)\n"
        "fd = os.open(os.path.join(p, 'k'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "os.write(fd, %r); os.close(fd); print('SB_KEY ok')\n" % base64.b64encode(key)
    )
    nb = {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "sbkey",
                "metadata": {},
                "outputs": [],
                "source": code.splitlines(True),
            }
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    dest.write_text(json.dumps(nb))
    with contextlib.suppress(OSError):
        os.chmod(dest, 0o600)
    return dest


def build_job_notebook(
    job,
    rollback,
    dest,
    key = None,
    embed_key = False,
):
    """One-cell notebook that runs `job` in a fresh venv on the remote kernel. Every secret in its
    environment (and job["_upload_token"]) is sealed with `key` (made here when None and needed);
    embed_key puts the key in the job too (Kaggle, which gets no write token). Returns the job id;
    the key used is left in job["_seal_key"]."""
    plain, secret = split_secrets(job.get("_remote_env") or {})
    if job.get("_upload_token"):
        secret["__upload_token__"] = job["_upload_token"]
    spec = {
        "id": job.get("id") or uuid.uuid4().hex[:10],
        "rollback": rollback,
        "torch": job.get("torch", "auto"),
        "requirements": job.get("requirements") or [],
        "env": plain,
        "argv": job.get("argv") or [],
        "artifacts": job.get("artifacts") or [],
        "timeout_s": int(job.get("timeout_s") or 4 * 3600),
        "python": job.get("python"),
        "shared_models": bool(job.get("shared_models")),
        "artifact_max": int(job.get("artifact_max") or ARTIFACT_MAX_BYTES),
        "files": {},
        "upload": job.get("_upload") or {},
        "preclean": bool(job.get("_preclean")),
    }
    if secret:
        key = key or os.urandom(32)
        spec["sealed"] = seal(secret, key)
        if embed_key:
            spec["k"] = base64.b64encode(key).decode()
        job["_seal_key"] = key
    if job.get("notebook"):
        p = Path(job["notebook"])
        spec["name"] = p.name
        spec["notebook_b64"] = base64.b64encode(p.read_bytes()).decode()
    else:
        p = Path(job["script"])
        spec["name"] = p.name
        spec["files"][p.name] = base64.b64encode(p.read_bytes()).decode()
    for extra in job.get("files") or []:
        q = Path(extra)
        spec["files"][q.name] = base64.b64encode(q.read_bytes()).decode()
    blob = base64.b64encode(json.dumps(spec).encode()).decode()
    src = JOB_WRAPPER.replace("__SB_JOB__", blob)
    nb = {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "sbjob",
                "metadata": {},
                "outputs": [],
                "source": src.splitlines(True),
            }
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    dest.write_text(json.dumps(nb))
    return spec["id"]


def _remote_env(job, env = None):
    """Env for the remote job. HF_TOKEN: the job's own, else the shared read-only one. The caller's
    own (possibly write-scoped) HF_TOKEN leaves the host only with pass_hf_token."""
    env = os.environ if env is None else env
    out = dict(job.get("env") or {})
    if "HF_TOKEN" not in out:
        if job.get("pass_hf_token") and env.get("HF_TOKEN"):
            out["HF_TOKEN"] = env["HF_TOKEN"]
        else:
            tok = hf_read_token()
            if tok:
                out["HF_TOKEN"] = tok
    return out


def parse_markers(text):
    res, art, rollback, skipped, art_file, art_hf, clean = None, [], None, None, None, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SB_RESULT="):
            with contextlib.suppress(ValueError):
                res = json.loads(line[len("SB_RESULT=") :])
        elif line.startswith("SB_ART:"):
            art.append(line[len("SB_ART:") :])
        elif line.startswith("SB_ROLLBACK="):
            rollback = line[len("SB_ROLLBACK=") :]
        elif line.startswith("SB_ARTIFACT_SKIPPED"):
            skipped = line
        elif line.startswith("SB_CLEAN="):
            clean = line[len("SB_CLEAN=") :]
        elif line.startswith("SB_ART_FILE="):
            art_file = line[len("SB_ART_FILE=") :].split()[0]
        elif line.startswith("SB_ART_HF="):
            art_hf = line[len("SB_ART_HF=") :].split()[0]
    return {
        "result": res,
        "artifact_b64": "".join(art),
        "rollback": rollback,
        "artifact_skipped": skipped,
        "artifact_file": art_file,
        "artifact_hf": art_hf,
        "clean": clean,
    }


_TOKEN_TEXT = re.compile(r"(hf_[A-Za-z0-9]{20,}|KGAT_[A-Za-z0-9_]{8,})")


def mask_tokens(text):
    """hf_ / KGAT_ tokens out of anything shown or stored (the remote cell masks its own too)."""
    return _TOKEN_TEXT.sub("***", text or "")


def fetch_artifacts(got, outdir, dest):
    """The job's artifact tarball, wherever the remote put it: the SB_ART stream, a file in the
    Kaggle output (SB_ART_FILE), or the private HF dataset (SB_ART_HF). -> extracted paths."""
    if got.get("artifact_b64"):
        return _unpack(got["artifact_b64"], dest)
    if got.get("artifact_file"):
        for f in Path(outdir).rglob(got["artifact_file"]):
            return _unpack(base64.b64encode(f.read_bytes()).decode(), dest)
        return []
    if got.get("artifact_hf"):
        repo, _, path = got["artifact_hf"].partition(":")
        from huggingface_hub import hf_hub_download

        f = hf_hub_download(
            repo,
            path,
            repo_type = "dataset",
            token = artifact_token(),
            local_dir = str(Path(outdir) / "hf_artifacts"),
        )
        return _unpack(base64.b64encode(Path(f).read_bytes()).decode(), dest)
    return []


# ---------------------------------------------------------------------------------------------
# Private HF artifact dataset (Colab results too big for the notebook), 3-day expiry
# ---------------------------------------------------------------------------------------------

ARTIFACT_REPO = (
    os.environ.get("STUDIO_REGRESS_ARTIFACT_REPO") or "danielhanchen/switchboard-artifacts"
)
ARTIFACT_DAYS = float(os.environ.get("STUDIO_REGRESS_ARTIFACT_DAYS") or 3)
EXPIRE_EVERY_S = 1800  # host-wide: at most one expiry pass per half hour, whoever runs it
_STAMP_RE = re.compile(r"^runs/(\d{8}T\d{4})-[^/]+/")


def artifact_token():
    """The upload / read token for ARTIFACT_REPO: $STUDIO_REGRESS_ARTIFACT_TOKEN, else HF_TOKEN."""
    return (
        os.environ.get("STUDIO_REGRESS_ARTIFACT_TOKEN") or os.environ.get("HF_TOKEN") or ""
    ).strip() or None


def artifact_prefix(run_id, now = None):
    return "runs/%s-%s" % (time.strftime("%Y%m%dT%H%M", time.gmtime(now or _now())), run_id)


_REPO_READY = {}  # repo -> when it was last confirmed private
REPO_CHECK_S = 600


def ensure_artifact_repo(api = None):
    """Create the PRIVATE dataset (exist_ok) and confirm it IS private: create_repo ignores
    private=True for a repo that already exists, so a public one (a STUDIO_REGRESS_ARTIFACT_REPO
    override, a visibility flip) would publish every log and screenshot. False when there is no
    token or the repo is not private (no upload: results stay inline or are dropped). A verified
    answer is reused for REPO_CHECK_S."""
    tok = artifact_token()
    if not tok:
        return False
    if _now() - _REPO_READY.get(ARTIFACT_REPO, -REPO_CHECK_S) < REPO_CHECK_S:
        return True
    if api is None:
        from huggingface_hub import HfApi
        api = HfApi(token = tok)
    api.create_repo(ARTIFACT_REPO, repo_type = "dataset", private = True, exist_ok = True)
    if getattr(api.repo_info(ARTIFACT_REPO, repo_type = "dataset"), "private", None) is not True:
        print(
            "[cloud_pool] WARNING: artifact dataset %s is not private; not uploading to it"
            % ARTIFACT_REPO,
            file = sys.stderr,
            flush = True,
        )
        return False
    _REPO_READY[ARTIFACT_REPO] = _now()
    return True


def expired_paths(
    paths,
    days = None,
    now = None,
):
    """Files under runs/<UTC yyyymmddTHHMM>-<id>/ older than `days`."""
    import calendar

    days = ARTIFACT_DAYS if days is None else days
    cut = (now or _now()) - days * 86400
    out = []
    for p in paths:
        m = _STAMP_RE.match(p)
        if m and calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M")) < cut:
            out.append(p)
    return out


def uploads_may_run():
    """A Colab pool slot is live host-wide: its job may be uploading to ARTIFACT_REPO right now, and
    super_squash_history under an upload in flight can lose it (Kaggle never uploads)."""
    with contextlib.suppress(Exception):
        with ledger() as st:
            return any(v.get("backend") == "colab" for v in st["slots"].values())
    return True


def expire_artifacts(
    days = None,
    api = None,
    now = None,
    force = False,
    log = None,
):
    """Delete expired runs/* from ARTIFACT_REPO in one commit, then squash history so the LFS
    blobs stop counting against storage. Single-flight and throttled host-wide (shared lock +
    stamp); never raises. -> number of files deleted, or None when skipped."""
    log = log or (lambda m: print("[cloud_pool] " + m, file = sys.stderr))
    if not artifact_token():
        return None
    d = shared_dir()
    try:
        _mkdir(d, 0o777)
        lock = open(d / "artifact_expire.lock", "a")
    except OSError:
        return None
    import fcntl

    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None  # another run is expiring right now
        stamp = d / "artifact_expire.json"
        prev = {}
        with contextlib.suppress(OSError, ValueError):
            prev = json.loads(stamp.read_text())
        if not force and (now or _now()) - prev.get("t", 0) < EXPIRE_EVERY_S:
            return None
        try:
            if api is None:
                from huggingface_hub import HfApi
                api = HfApi(token = artifact_token())
            if not api.repo_exists(ARTIFACT_REPO, repo_type = "dataset"):
                return 0
            from huggingface_hub import CommitOperationDelete

            old = expired_paths(api.list_repo_files(ARTIFACT_REPO, repo_type = "dataset"), days, now)
            if old:
                api.create_commit(
                    ARTIFACT_REPO,
                    repo_type = "dataset",
                    operations = [CommitOperationDelete(path_in_repo = p) for p in old],
                    commit_message = "switchboard: expire %d artifact file(s) older than %g days"
                    % (len(old), ARTIFACT_DAYS if days is None else days),
                )
            squash = bool(old) or bool(prev.get("squash_pending"))
            busy = uploads_may_run()
            if squash and not busy:
                api.super_squash_history(ARTIFACT_REPO, repo_type = "dataset")
            if old or squash:
                log(
                    "artifact repo: expired %d file(s), %s"
                    % (
                        len(old),
                        "history squashed"
                        if squash and not busy
                        else "squash deferred (a Colab job may be uploading)"
                        if squash
                        else "nothing to squash",
                    )
                )
            with contextlib.suppress(OSError):
                stamp.write_text(
                    json.dumps(
                        {
                            "t": now or _now(),
                            "deleted": len(old),
                            "squash_pending": bool(squash and busy),
                        }
                    )
                )
                os.chmod(stamp, 0o666)
            return len(old)
        except Exception as e:  # noqa: BLE001 - housekeeping never breaks a run
            log("artifact repo expiry skipped: %s" % mask_tokens(str(e))[:200])
            return None
    finally:
        lock.close()


def _notebook_text(path):
    try:
        nb = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return ""
    parts = []
    for cell in nb.get("cells", []):
        for o in cell.get("outputs", []):
            t = o.get("text")
            if t is None and "data" in o:
                t = o["data"].get("text/plain")
            if isinstance(t, list):
                t = "".join(t)
            if t:
                parts.append(t)
    return "\n".join(parts)


def collect(outdir):
    """Markers from notebook_cloud_run's outputs in `outdir` -> {name: parsed}."""
    outdir = Path(outdir)
    rep = {}
    with contextlib.suppress(OSError, ValueError):
        rep = json.loads((outdir / "report.json").read_text())
    found = {}
    for r in rep.get("results", []):
        text = _notebook_text(r.get("executed_notebook") or "")
        if not text and r.get("log"):
            with contextlib.suppress(OSError):
                text = Path(r["log"]).read_text()
        found[r["name"]] = dict(
            parse_markers(text), status = r.get("status"), summary = r.get("summary")
        )
    return found


def _unpack(b64, dest):
    if not b64:
        return []
    dest.mkdir(parents = True, exist_ok = True)
    with tarfile.open(fileobj = io.BytesIO(base64.b64decode(b64)), mode = "r:gz") as tar:
        tar.extractall(dest, filter = "data") if sys.version_info >= (3, 12) else tar.extractall(dest)
        return [str(dest / m.name) for m in tar.getmembers()]


# ---------------------------------------------------------------------------------------------
# notebook_cloud_run + colab CLI calls (monkeypatched in tests)
# ---------------------------------------------------------------------------------------------


def secure_account_home(account):
    """0700 on the account HOME and its colab-cli dirs, 0600 on token.json (the CLI writes it 0664,
    and it is a Google OAuth refresh token)."""
    home = account_home(account)
    for d in (home.parent, home, home / ".config", home / ".config" / "colab-cli", home / "nbrun"):
        if d.is_dir():
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
    tok = home / ".config" / "colab-cli" / "token.json"
    if tok.exists():
        with contextlib.suppress(OSError):
            os.chmod(tok, 0o600)
    return home


def _account_auth(account):
    """(home or None, auth provider). An account dir that holds a colab token is driven with oauth2
    under that HOME; daniel without one uses the real HOME's existing login (ADC)."""
    home = secure_account_home(account)
    if (home / ".config" / "colab-cli" / "token.json").exists():
        return str(home), "oauth2"
    if account == "daniel":
        return None, "adc"
    return str(home), "oauth2"


def _account_env(account):
    home, _ = _account_auth(account)
    env = dict(os.environ)
    if home:
        env["NBRUN_COLAB_HOME"] = home
        env["NOTEBOOK_CLOUD_RUN_HOME"] = str(Path(home) / "nbrun")
    else:
        env.pop("NBRUN_COLAB_HOME", None)
    return env


class Gate:
    """Lets a caller (gpu_queue) abandon a remote attempt that has not started running yet: `cancel`
    is honoured only until `started` is set (the job is executing remotely; it then always wins, so
    one job never runs twice)."""

    def __init__(self):
        self.cancel = threading.Event()
        self.started = threading.Event()

    def cancelled(self):
        return self.cancel.is_set() and not self.started.is_set()


CANCELLED_RC = 130


def _gate_kw(gate):
    return {"gate": gate} if gate is not None else {}


EXEC_MARKER = "=> "  # ncr logs "=> <notebook> on Colab <tier>" when execution begins


NCR_STOP_GRACE_S = 360  # ncr's teardown: kill `colab exec` (<= 35 s) + `colab stop` (<= 300 s)


def _stop_ncr(p, grace = NCR_STOP_GRACE_S):
    p.terminate()
    try:
        p.wait(timeout = grace)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def run_ncr(
    argv,
    env,
    log_path,
    timeout,
    gate = None,
    watch = None,
):
    """notebook_cloud_run, output appended to log_path; 3 on timeout. With a gate: `started` is set
    at the exec marker, and a cancel before it terminates ncr (CANCELLED_RC). With a watch
    (ProvisionWatch): on_exec() at the exec marker; before it, check(new log text) returning a
    reason terminates ncr (PROVISION_RC)."""
    cmd = [sys.executable, "-u", str(HERE / "notebook_cloud_run.py")] + argv
    with open(log_path, "a") as fh:
        fh.write("$ %s\n" % " ".join(shlex.quote(a) for a in cmd))
        fh.flush()
        mark = fh.tell()
        p = subprocess.Popen(cmd, env = env, stdout = fh, stderr = subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return p.wait(timeout = 2)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                # SIGTERM first: notebook_cloud_run's handler deletes a still-running Kaggle kernel, or
                # kills its `colab exec` child and stops the VM. A bare SIGKILL skipped that and left it billing.
                _stop_ncr(p)
                return 3
            pending_gate = gate is not None and not gate.started.is_set()
            pending_watch = watch is not None and not watch.started
            if not (pending_gate or pending_watch):
                continue
            text = ""
            with contextlib.suppress(OSError):
                with open(log_path, "rb") as rf:
                    rf.seek(mark)
                    text = rf.read().decode("utf-8", "replace")
            if any(ln.startswith(EXEC_MARKER) for ln in text.splitlines()):
                if gate is not None:
                    gate.started.set()
                if pending_watch:
                    watch.started = True
                    with contextlib.suppress(Exception):
                        watch.on_exec()
                continue
            if pending_gate and gate.cancel.is_set():
                _stop_ncr(p)
                return CANCELLED_RC
            if pending_watch and watch.check(text):
                _stop_ncr(p)
                return PROVISION_RC


def colab_stop(worker):
    """Stop a worker's VM (its named-session state file lives under the account HOME). True when the
    VM is gone (stopped, or the CLI no longer knows it); False keeps the record for sweep to retry."""
    home, auth = _account_auth(worker["account"])
    with contextlib.suppress(Exception):
        attach_state(worker)  # a worker another unix user started: without state, "not found" lies
    state = _state_path(worker["account"], worker["name"])
    cli = ncr.colab_cli() or "colab"
    cmd = (
        [cli, "--config", str(state)]
        + (["--auth", auth] if auth != "adc" else [])
        + ["stop", "-s", worker["name"]]
    )
    env = dict(os.environ, **({"HOME": home} if home else {}))
    try:
        r = subprocess.run(cmd, env = env, capture_output = True, text = True, timeout = 300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 or "not found" in text.lower() or "no active session" in text.lower()


class ProvisionWatch:
    """A fresh worker's way to `colab exec`, watched from run_ncr. At the exec marker the record goes
    PROVISIONING -> BUSY with its endpoint (the VM exists; sweep can attribute its session). Before
    it: a credits refusal in the log, the provisioning deadline, or sweep having reclaimed the record
    stops ncr (its SIGTERM handler stops any VM it made) with `kind` / `reason` set."""

    def __init__(
        self,
        name,
        deadline_s,
        clock = None,
    ):
        self.name, self.deadline_s = name, deadline_s
        self.clock = clock or _now
        self.t0 = self.clock()
        self.started = False
        self.kind = self.reason = None
        self._next_rec = self.t0 + 30

    def on_exec(self):
        with workers() as ws:
            w = ws["workers"].get(self.name)
            if w and w["state"] == "PROVISIONING" and w.get("owner_pid") == os.getpid():
                w.update(state = "BUSY", started_at = round(self.clock(), 1))
                with contextlib.suppress(Exception):
                    w["endpoint"] = w.get("endpoint") or worker_endpoint(dict(w, name = self.name))

    def check(self, text):
        now = self.clock()
        if "colab said:" in text.lower() and classify_colab_refusal(text) == "credits":
            self.kind, self.reason = "credits", "Colab refused (credits) while allocating"
        elif now - self.t0 > self.deadline_s:
            self.kind, self.reason = (
                "stuck",
                "no `colab exec` within %d s of the claim" % self.deadline_s,
            )
        elif now >= self._next_rec:
            self._next_rec = now + 30
            with workers() as ws:
                w = ws["workers"].get(self.name)
            if not w or w.get("state") != "PROVISIONING" or w.get("owner_pid") != os.getpid():
                self.kind, self.reason = "reclaimed", "sweep reclaimed %s as stuck" % self.name
        return self.reason


def provision_deadline(tier, tiers = None):
    """Seconds a fresh worker of `tier` may take to reach `colab exec`: the pool deadline, or the
    tier's allocation wait plus kernel probe and setup when that is longer."""
    t = (tiers or load_tiers()).get(tier, {})
    return max(POOL_PROVISION_DEADLINE_S, int(t.get("alloc_wait", DEFAULT_ALLOC_WAIT)) + 300)


# ---------------------------------------------------------------------------------------------
# Colab workers (per unix user)
# ---------------------------------------------------------------------------------------------


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _proc_start(pid):
    """Start time (clock ticks since boot) of `pid`, from /proc (readable for other users' pids)."""
    try:
        return int(Path("/proc/%d/stat" % int(pid)).read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_ns():
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def owner():
    """This process as a worker owner: pid + start time + boot id (a recycled pid is not the owner),
    plus its pid namespace (a pid from another namespace means nothing here)."""
    return {
        "owner_pid": os.getpid(),
        "owner_start": _proc_start(os.getpid()),
        "owner_boot": _boot_id(),
        "owner_pidns": _pid_ns(),
        "owner_user": getpass.getuser(),
        "claimed_at": _now(),
    }


def owner_alive(w):
    """The worker's owner process still runs: same pid, same start time, same boot (any unix user).
    An owner in another pid namespace (a sandboxed session) cannot be checked by pid: it counts as
    alive until a Colab VM's lifetime has passed since its claim, so its running job is never
    reclaimed and cleaned out from under it."""
    if w.get("owner_pidns") and _pid_ns() and w["owner_pidns"] != _pid_ns():
        return _now() - float(w.get("claimed_at") or 0) < POOL_RUNTIME_TTL_S
    pid = w.get("owner_pid")
    if not _pid_alive(pid):
        return False
    if w.get("owner_boot") and _boot_id() and w["owner_boot"] != _boot_id():
        return False
    start = _proc_start(pid)
    return not (w.get("owner_start") and start is not None and start != w["owner_start"])


def _pid_alive(pid):
    if not pid:
        return False  # no owner (e.g. a STOPPING record whose stop failed): sweep retries it
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def idle_workers():
    with contextlib.suppress(Exception):
        with workers() as ws:
            return [dict(w, name = n) for n, w in ws["workers"].items() if w.get("state") == "IDLE"]
    return []


def claim_worker(
    tier,
    account,
    est_hold_s,
    server = None,
    name = None,
    job = None,
):
    """An IDLE worker of this tier/account (-> BUSY, gen+1), else a new PROVISIONING record with a
    host-wide slot. Returns the worker dict (with name) or None if no slot. `name` (a pinned worker):
    that worker when IDLE, None while it is busy, and the normal path once it has expired."""
    tiers = load_tiers()
    server = server_counts([account]) if server is None else server
    with workers() as ws:
        pinned = ws["workers"].get(name) if name else None
        if pinned is not None and pinned["tier"] == tier and pinned["account"] == account:
            if pinned["state"] != "IDLE":
                return None
            return _hand_out(ws, name, pinned, est_hold_s, job)
        # an IDLE worker of ANY unix user / workspace: the VM belongs to the Google account, and
        # attach_state lets this user's credentials drive it by endpoint
        for name, w in ws["workers"].items():
            if w["tier"] == tier and w["account"] == account and w["state"] == "IDLE":
                return _hand_out(ws, name, w, est_hold_s, job)
        with ledger() as st:
            if not colab_has_slot(st, account, tier, tiers, server):
                return None
            name = "sb-%s-%s-%s" % (tier.lower(), account, uuid.uuid4().hex[:6])
            sid = take_slot(
                st,
                "colab",
                tier,
                tiers,
                account = account,
                hold_s = est_hold_s + 3600,
                sid = name,
                jobs = [job] if job else (),
            )
        w = {
            "tier": tier,
            "account": account,
            "state": "PROVISIONING",
            "gen": 1,
            **owner(),
            "created": _now(),
            "slot": sid,
            "jobs": 0,
        }
        ws["workers"][name] = w
        return dict(w, name = name, warm = False)


def _hand_out(
    ws,
    name,
    w,
    est_hold_s,
    job = None,
):
    """Under the workers lock: claim an IDLE worker for this process. A worker reclaimed from a dead
    owner or adopted from an orphaned session (needs_clean) goes out as CLEANING: the dispatch runs
    the clean / verify cell first and quarantines it on failure."""
    clean = bool(w.get("needs_clean"))
    w.update(state = "CLEANING" if clean else "BUSY", gen = w.get("gen", 0) + 1, **owner())
    if w.get("slot"):
        extend_slot(w["slot"], est_hold_s + 3600)
        set_slot_owner(w["slot"], w, job)
    with contextlib.suppress(Exception):
        attach_state(dict(w, name = name))
    return dict(w, name = name, warm = True, clean = clean)


def _state_path(account, name):
    home, _ = _account_auth(account)
    state_home = Path(home) / "nbrun" if home else ncr.colab_state_home()
    return state_home / ("session-%s.json" % ncr._safe_filename(name))


def attach_state(w):
    """Make `w`'s VM drivable by name from THIS unix user's credentials: write the colab CLI's session
    state (name + endpoint, no token: with token_expires_at unset the CLI's get_session refreshes
    token and url from the account's own list_assignments). Kept when the file already knows it."""
    if not w.get("endpoint"):
        return False
    path = _state_path(w["account"], w["name"])
    try:
        cur = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        cur = {}
    if isinstance(cur, dict) and (cur.get(w["name"]) or {}).get("endpoint") == w["endpoint"]:
        return True
    tiers = load_tiers()
    t = tiers.get(w["tier"], {})
    hm = w["tier"].endswith("-HM")
    entry = {
        "name": w["name"],
        "token": "",
        "url": "",
        "endpoint": w["endpoint"],
        "token_expires_at": None,
        "variant": "GPU",
        "accelerator": (t.get("gpu") or w["tier"]).split("-")[0].replace("RTXPRO6000", "G4"),
        "machine_shape": "HIGH_RAM" if hm else "STANDARD",
    }
    path.parent.mkdir(parents = True, exist_ok = True)
    tmp = path.with_suffix(".tmp%d" % os.getpid())
    tmp.write_text(json.dumps({w["name"]: entry}, indent = 2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def release_worker(
    name,
    ok,
    spawn_expiry = True,
    needs_clean = False,
):
    """After a job: IDLE (and schedule expiry) when the rollback verified, else QUARANTINED + stopped.
    needs_clean: back to IDLE still owing its clean step (a reclaimed worker whose job was cancelled)."""
    tiers = load_tiers()
    stop = None
    with workers() as ws:
        w = ws["workers"].get(name)
        if not w:
            return
        w["jobs"] = w.get("jobs", 0) + 1
        if not w.get("endpoint"):  # recorded so another unix user can attach (attach_state)
            with contextlib.suppress(Exception):
                w["endpoint"] = worker_endpoint(dict(w, name = name))
        if ok:
            w.update(state = "IDLE", idle_since = _now(), owner_pid = None, needs_clean = bool(needs_clean))
            idle_s = tiers[w["tier"]].get("idle_s", 420)
            if w.get("slot"):
                extend_slot(w["slot"], idle_s + SLOT_GRACE_S)
            gen = w["gen"]
        else:
            w["state"] = "QUARANTINED"
            stop = dict(w, name = name)
    if stop:
        _stop_and_forget(stop)
    elif spawn_expiry:
        spawn_expire(name, gen, idle_s)


STOP_ATTEMPTS = 5
STOP_STALE_S = 900  # a stop takes minutes: a STOPPING record older than this is retried
# An idle worker's slot outlives its idle window by this much: sweep stops a worker whose expiry
# process died at idle_s + 120, so the slot must still be held then.
SLOT_GRACE_S = 180


def _stop_and_forget(w, bill = True):
    """Stop the VM, then drop the record, bill up to now and free the slot. A failed stop keeps the
    record as STOPPING with no owner so the next sweep retries (a VM we cannot stop still bills);
    after STOP_ATTEMPTS it is dropped with a warning naming the manual command."""
    ok = colab_stop(w) is not False
    with workers() as ws:
        cur = ws["workers"].get(w["name"])
        if cur and not ok:
            cur["stop_fails"] = cur.get("stop_fails", 0) + 1
            if cur["stop_fails"] < STOP_ATTEMPTS:
                cur.update(state = "STOPPING", owner_pid = None, owner_pidns = None, owner_start = None)
                cur = None
            else:
                print(
                    "cloud_pool: giving up stopping %s after %d tries; stop it by hand: colab stop -s %s"
                    % (w["name"], cur["stop_fails"], w["name"]),
                    file = sys.stderr,
                )
        if cur is not None:
            ws["workers"].pop(w["name"], None)
    if cur is not None and w.get("slot"):
        free_slot(w["slot"])
    return ok


def spawn_expire(name, gen, after_s):
    cmd = [
        sys.executable,
        str(HERE / "cloud_pool.py"),
        "colab-expire",
        "--worker",
        name,
        "--gen",
        str(gen),
        "--after",
        str(int(after_s)),
    ]
    with contextlib.suppress(OSError):
        subprocess.Popen(
            cmd,
            stdin = subprocess.DEVNULL,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.DEVNULL,
            start_new_session = True,
            env = dict(os.environ),
        )


def note_demand(tier):
    with contextlib.suppress(Exception):
        with ledger() as st:
            st.setdefault("demand", {})[tier] = _now()


EXPIRE_POLL_S = 30


def _still_idle(name, gen):
    with workers() as ws:
        w = ws["workers"].get(name)
        return dict(w) if w and w.get("state") == "IDLE" and w.get("gen") == gen else None


def _wait_idle(name, gen, secs, sleep):
    """Sleep `secs` in EXPIRE_POLL_S steps; None as soon as the worker is claimed or gone, so a
    stale-generation expiry process exits instead of sleeping out its whole window."""
    left = float(secs)
    while left > 0:
        step = min(EXPIRE_POLL_S, left)
        sleep(step)
        left -= step
        if not _still_idle(name, gen):
            return None
    return _still_idle(name, gen)


def colab_expire(
    name,
    gen,
    after_s,
    sleep = time.sleep,
):
    """Stop worker `name` if it is still IDLE at generation `gen` after `after_s`. A compatible job
    that asked for this tier within the window buys one more window (after_s / 2)."""
    for attempt in range(2):
        w = _wait_idle(name, gen, after_s if attempt == 0 else after_s / 2, sleep)
        if not w:
            with workers() as ws:
                return "kept" if name in ws["workers"] else "gone"
        with ledger() as st:
            asked = st.get("demand", {}).get(w["tier"], 0)
        if attempt == 0 and _now() - asked < after_s:
            if w.get("slot"):
                extend_slot(w["slot"], after_s / 2 + SLOT_GRACE_S)
            continue
        break
    with workers() as ws:
        w = ws["workers"].get(name)
        if not w or w.get("state") != "IDLE" or w.get("gen") != gen:
            return "kept"
        # STOPPING under the same lock as the check: a claim_worker in the window before the stop
        # would otherwise hand this dying VM to a job
        w.update(state = "STOPPING", **owner())
    _stop_and_forget(dict(w, name = name))
    return "stopped"


PROVISION_SWEEP_GRACE_S = (
    300  # sweep acts this long after the deadline: a live owner gives up first
)


def stuck_provisioning(
    records,
    listings,
    tiers,
    now = None,
):
    """Names of PROVISIONING workers to reclaim (pure). A worker qualifies past its provisioning
    deadline plus PROVISION_SWEEP_GRACE_S when the server's listing for its account is known and its
    endpoint (if any) is not in it. An owner on older code keeps a running fresh worker PROVISIONING
    for the whole job, so per (account, family) only as many are reclaimed as the listing cannot
    account for: listed sessions not attributed to a record by endpoint may be theirs (the newest
    candidates are reclaimed first; a VM that came up is more likely an older claim's)."""
    now = _now() if now is None else now
    out = []
    groups = {}
    for name, w in records.items():
        if w.get("state") != "PROVISIONING" or w.get("tier") not in tiers:
            continue
        lst = listings.get(w.get("account"))
        if not lst or not lst.get("ok"):
            continue
        if (
            now - float(w.get("created") or now)
            <= provision_deadline(w["tier"], tiers) + PROVISION_SWEEP_GRACE_S
        ):
            continue
        eps = {x.get("endpoint") for x in lst.get("sessions") or []}
        if w.get("endpoint") and w["endpoint"] in eps:
            continue
        groups.setdefault((w["account"], tiers[w["tier"]]["family"]), []).append(name)
    for (acct, fam), names in groups.items():
        listed = {
            x.get("endpoint")
            for x in listings[acct].get("sessions") or []
            if x.get("family") == fam
        }
        known = {
            w.get("endpoint")
            for w in records.values()
            if w.get("account") == acct and w.get("endpoint")
        }
        spare = len(listed - known)
        names.sort(key = lambda n: -float(records[n].get("created") or 0))
        out += names[: max(0, len(names) - spare)]
    return out


def sweep(_fetch = None):
    """Stop workers whose idle window passed (their expiry process died), and those whose owner died
    mid-job; reclaim fresh workers stuck PROVISIONING (stuck_provisioning: slot freed, family
    benched). Returns the names stopped or reclaimed."""
    tiers = load_tiers()
    victims, ledger_ext, rebuilt, reclaimed, stuck = [], [], [], [], []
    with workers() as ws:
        prov = {
            w.get("account") for w in ws["workers"].values() if w.get("state") == "PROVISIONING"
        }
    listings = {}
    for acct in sorted(a for a in prov if a):
        with contextlib.suppress(Exception):
            listings[acct] = server_sessions(
                acct, _fetch = _fetch
            )  # server < workers: before the lock
    with workers() as ws:
        for name in stuck_provisioning(ws["workers"], listings, tiers):
            stuck.append(dict(ws["workers"].pop(name), name = name))
        for name, w in ws["workers"].items():
            idle_s = tiers.get(w["tier"], {}).get("idle_s", 420)
            if w["state"] == "IDLE" and _now() - w.get("idle_since", _now()) > idle_s + 120:
                victims.append(dict(w, name = name))
            elif w["state"] in ("BUSY", "CLEANING") and w.get("owner_pid") and not owner_alive(w):
                # owner died mid-job: the VM is fine, its workspace is not. Reclaim it: IDLE with
                # needs_clean, so the next claimer gets it as CLEANING (clean + verify first)
                w.update(
                    state = "IDLE",
                    idle_since = _now(),
                    owner_pid = None,
                    needs_clean = True,
                    reclaimed = w.get("reclaimed", 0) + 1,
                )
                if w.get("slot"):
                    ledger_ext.append((w["slot"], idle_s + SLOT_GRACE_S))
                reclaimed.append((name, w.get("gen", 1), idle_s))
            elif w["state"] == "PROVISIONING" and w.get("owner_pid") and not owner_alive(w):
                victims.append(dict(w, name = name))
            elif w["state"] == "QUARANTINED":
                victims.append(dict(w, name = name))
            elif w["state"] == "REBUILT":
                rebuilt.append(name)
            elif w["state"] == "STOPPING" and (
                not owner_alive(w) or _now() - float(w.get("claimed_at") or 0) > STOP_STALE_S
            ):
                victims.append(dict(w, name = name))
        for v in victims:
            ws["workers"][v["name"]].update(state = "STOPPING", **owner())
        for sid, hold in ledger_ext:  # workers -> ledger: the lock order everywhere
            extend_slot(sid, hold)
        if rebuilt:
            with ledger() as st:
                for name in rebuilt:
                    w = ws["workers"][name]
                    if name in st["slots"]:
                        continue  # its job may still run: wait for its owner or the slot
                    idle_s = tiers.get(w["tier"], {}).get("idle_s", 300)
                    if w["tier"] in tiers:
                        take_slot(
                            st,
                            "colab",
                            w["tier"],
                            tiers,
                            account = w["account"],
                            sid = name,
                            hold_s = idle_s + SLOT_GRACE_S,
                        )
                    w.update(state = "IDLE", idle_since = _now(), needs_clean = True)
    for w in victims:
        _stop_and_forget(w)
    for name, gen, idle_s in reclaimed:
        spawn_expire(name, gen, idle_s)
    for w in stuck:
        if w.get("slot"):
            free_slot(w["slot"])
        with contextlib.suppress(Exception):
            bench_colab(
                w["account"],
                w["tier"],
                "stuck",
                "provisioning never produced a session (%s)" % w["name"],
            )
        print(
            "[cloud_pool] reclaimed %s: PROVISIONING %.0f min with no session on the server; %s %s benched"
            " %d min"
            % (
                w["name"],
                (_now() - float(w.get("created") or _now())) / 60,
                w["account"],
                w["tier"],
                PROVISION_BENCH_S // 60,
            ),
            file = sys.stderr,
            flush = True,
        )
    return (
        [w["name"] for w in victims]
        + [w["name"] for w in stuck]
        + forget_vanished(_fetch = _fetch)
        + adopt_orphans(_fetch = _fetch)
    )


POOL_PREFIX = "sb-"


def adopt_orphans(_fetch = None):
    """Colab sessions the pool made but lost (a crashed run, a wiped workers.json): listed by the
    server, missing from workers.json, and named with POOL_PREFIX (or registered by endpoint in the
    host-wide pool map). They become IDLE workers with needs_clean (cleaned and verified, a fresh
    baseline if none, before any job) and the usual idle expiry. Any other session (someone's
    interactive notebook) is never adopted or stopped; status lists it as unmanaged."""
    tiers = load_tiers()
    adopted = []
    for acct in COLAB_ACCOUNTS:
        with contextlib.suppress(Exception):
            v = server_sessions(acct, _fetch = _fetch)
            if not v["ok"]:
                continue
            pool = v.get("pool") or {}
            with workers() as ws:
                known_ep = {w.get("endpoint") for w in ws["workers"].values() if w.get("endpoint")}
                for x in v["sessions"]:
                    name = x.get("name") or ""
                    reg = (pool.get(x["endpoint"]) or {}).get("worker") or ""
                    if not name.startswith(POOL_PREFIX):
                        name = reg if reg.startswith(POOL_PREFIX) else ""
                    if not name or x["endpoint"] in known_ep or name in ws["workers"]:
                        continue
                    tier = x.get("family")
                    if tier not in tiers or tiers[tier].get("backend") != "colab":
                        continue
                    with ledger() as st:
                        sid = take_slot(
                            st,
                            "colab",
                            tier,
                            tiers,
                            account = acct,
                            sid = name,
                            hold_s = tiers[tier].get("idle_s", 300) + SLOT_GRACE_S,
                        )
                    ws["workers"][name] = {
                        "tier": tier,
                        "account": acct,
                        "state": "IDLE",
                        "gen": 1,
                        "owner_pid": None,
                        "created": _now(),
                        "idle_since": _now(),
                        "slot": sid,
                        "jobs": 0,
                        "endpoint": x["endpoint"],
                        "needs_clean": True,
                        "adopted": True,
                    }
                    adopted.append((name, tiers[tier].get("idle_s", 300)))
    for name, idle_s in adopted:
        spawn_expire(name, 1, idle_s)
    return [n for n, _ in adopted]


def forget_vanished(_fetch = None):
    """Drop this user's IDLE pool workers whose VM the server no longer lists (Colab reclaimed it):
    nothing to stop, just free the record and its slot. Only a listing newer than the worker's
    idle_since counts, and only for workers with a known endpoint; external sessions are never
    touched here or anywhere else."""
    with workers() as ws:
        accts = {w.get("account") for w in ws["workers"].values() if w.get("state") == "IDLE"}
    gone = []
    for acct in sorted(a for a in accts if a):
        v = server_sessions(acct, _fetch = _fetch)
        if not v["ok"]:
            continue
        live = {x["endpoint"] for x in v["sessions"]}
        with workers() as ws:
            for name, w in list(ws["workers"].items()):
                if (
                    w.get("account") == acct
                    and w.get("state") == "IDLE"
                    and w.get("endpoint")
                    and w["endpoint"] not in live
                    and v["t"] > w.get("idle_since", _now())
                ):
                    ws["workers"].pop(name)
                    gone.append(dict(w, name = name))
    for w in gone:
        if w.get("slot"):
            free_slot(w["slot"])
    return [w["name"] for w in gone]


# ---------------------------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------------------------


def record_disk(tier, res):
    """Remember the scratch space a run saw on `tier` (select_tier skips tiers too small for a job)."""
    if res and res.get("disk_free_gb") is not None:
        with ledger() as st:
            st.setdefault("disk_seen", {})[tier] = {
                "free": float(res["disk_free_gb"]),
                "t": round(_now()),
            }


def _disk_short(res, base):
    """A DISK_SHORT result as an INFRA dict (the job never ran: not a test failure), else None."""
    if not (res and res.get("disk_short")):
        return None
    return dict(
        base,
        status = "INFRA",
        result = res,
        reason = "%s: %.1f GB scratch free, job needs %.1f GB"
        % (base.get("tier"), res.get("disk_free_gb") or 0, res.get("disk_need_gb") or 0),
    )


def _job_defaults(job):
    job = dict(job)
    job.setdefault("id", uuid.uuid4().hex[:10])
    job.setdefault("gb", 8.0)
    job.setdefault("dtype", "bf16")
    job.setdefault("est_s", 600)
    job.setdefault("class", "train")
    job.setdefault("ram_gb", 0.0)
    return job


def _outdir(job):
    d = Path(
        job.get("outdir")
        or (user_dir() / "runs" / ("%s-%s" % (time.strftime("%Y%m%d_%H%M%S"), job["id"])))
    )
    d.mkdir(parents = True, exist_ok = True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)  # the job notebook carries HF_TOKEN in its spec
    return d


def _pin_match(c, pin):
    if c["backend"] != pin.get("backend") or c["tier"] != pin.get("tier"):
        return False
    if c["backend"] == "colab":
        return c["account"] == pin.get("account")
    return c.get("token_env") == pin.get("token_env")


def dispatch(
    job,
    backend = "auto",
    dry_run = False,
    env = None,
    account = None,
    gate = None,
    pin = None,
    prefer = None,
):
    """Run one job remotely. Returns {status, backend, tier, account, rc, outdir, result, artifacts,
    rollback, predicted_s, reason}. status: PASS | FAIL | INFRA | NO_REMOTE_FIT | CANCELLED (the
    gate was cancelled before the job started running remotely).

    pin: only that backend / tier / account (PIN_UNAVAILABLE otherwise). prefer: the machine the
    caller picked moments ago is tried first, the other fitting ones after it (a racing caller may
    have taken its last slot)."""
    job = _job_defaults(job)
    with contextlib.suppress(Exception):
        sweep()
    backends = ("kaggle", "colab") if backend == "auto" else (backend,)
    cands = select_tier(
        job["gb"],
        job["dtype"],
        job["ram_gb"],
        job["est_s"],
        job["class"],
        perf = bool(job.get("perf")),
        multi_gpu = bool(job.get("multi_gpu")),
        backends = backends,
        env = env,
        disk_gb = float(job.get("disk_gb") or 0),
    )
    if account:
        cands = [c for c in cands if c["backend"] != "colab" or c["account"] == account]
        if backend == "auto":
            cands = [c for c in cands if c["backend"] == "colab"]  # --account means Colab
    if not cands:
        return {
            "status": "NO_REMOTE_FIT",
            "candidates": [],
            "reason": "no remote tier has the VRAM / dtype / RAM / disk / quota for %.1f GB %s"
            % (job["gb"], job["dtype"]),
        }
    if pin:
        # one machine per caller (studio_regress offload): only the pinned backend / tier / account
        cands = [c for c in cands if _pin_match(c, pin)]
        if not cands:
            return {
                "status": "PIN_UNAVAILABLE",
                "candidates": [],
                "reason": "the pinned %s %s is not available now (slot, quota or bench)"
                % (pin.get("backend"), pin.get("tier")),
            }
    if prefer:
        cands = [c for c in cands if _pin_match(c, prefer)] + [
            c for c in cands if not _pin_match(c, prefer)
        ]
    if dry_run:
        return {"status": "DRY_RUN", "candidates": cands}
    last, attempts = None, 0
    for c in cands:
        # a candidate whose slot a racing dispatch just took is refused under the lock at once
        # (no_slot): try the next; only real launches count toward the 4-attempt limit
        if attempts >= 4:
            break
        if gate is not None and gate.cancelled():
            return {"status": "CANCELLED", "reason": "a local GPU freed first"}
        if c["backend"] == "amd_ci":
            # AMD runs go through the amd_ci scaffold (branch + workflow + base/head differential),
            # which needs the PR context this generic job lacks; hand it back instead of guessing.
            return {
                "status": "MANUAL",
                "backend": "amd_ci",
                "tier": c["tier"],
                "candidates": cands,
                "reason": c["reason"]
                + "; run `python amd_ci/scaffold.py --pr N%s`"
                % (" --windows" if c["tier"] == "amd-windows" else ""),
            }
        if c["backend"] == "kaggle":
            res = dispatch_kaggle([job], c, env = env, **_gate_kw(gate))[0]
        else:
            want = pin or prefer or {}
            res = dispatch_colab(
                job, c, worker = want.get("worker") if _pin_match(c, want) else None, **_gate_kw(gate)
            )
        if res["status"] != "INFRA":
            return res
        last = res
        if not res.get("no_slot"):
            attempts += 1
        if c["backend"] == "colab":
            note_demand(c["tier"])
    return last


def dispatch_colab(
    job,
    cand,
    _retry = True,
    gate = None,
    worker = None,
):
    tiers = load_tiers()
    tier, account = cand["tier"], cand["account"]
    hold = int(cand["predicted_s"] + COLD_SETUP_S + 600)
    w = claim_worker(tier, account, hold, name = worker, job = job.get("id"))
    if not w:
        return {
            "status": "INFRA",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "no_slot": True,
            "reason": ("pinned worker %s is busy" % worker)
            if worker
            else "no free %s slot for %s" % (tier, account),
        }
    out = _outdir(job)
    nb = out / ("sbjob_%s.ipynb" % job["id"])
    job = dict(job, _remote_env = _remote_env(job), _preclean = bool(w.get("clean")))
    if job.get("artifact_upload") and artifact_token():
        # results too big for the notebook output go to the private dataset (Colab only)
        with contextlib.suppress(Exception):
            if ensure_artifact_repo():
                job["_upload"] = {
                    "repo": ARTIFACT_REPO,
                    "prefix": job.get("artifact_prefix") or artifact_prefix(job["id"]),
                }
                job["_upload_token"] = artifact_token()
    build_job_notebook(job, True, nb)
    notebooks = [str(nb)]
    knb = None
    if job.get("_seal_key"):
        knb = key_notebook(job["_seal_key"], out / ("sbkey_%s.ipynb" % job["id"]))
        notebooks = [str(knb), str(nb)]  # one VM (--session): the key cell first, the job after
    wall = int(job.get("timeout_s") or 4 * 3600) + 1800
    _, auth = _account_auth(account)
    argv = [
        "--backend",
        "colab",
        "--gpu",
        tier,
        "--session",
        w["name"],
        "--colab-auth",
        auth,
        "--no-smoke-patch",
        "--per-cell-timeout",
        str(wall),
        "--wall-timeout",
        str(wall),
        "--alloc-wait",
        str(tiers[tier].get("alloc_wait", DEFAULT_ALLOC_WAIT)),
        "--connect-retries",
        "0",
        "--idle-timeout",
        str(COLAB_IDLE_TIMEOUT_S),
        "--non-interactive",
        "--outdir",
        str(out),
    ] + notebooks
    t0 = _now()
    log = out / "cloud_pool.log"
    mark = log.stat().st_size if log.exists() else 0
    watch = None if w.get("warm") else ProvisionWatch(w["name"], provision_deadline(tier, tiers))
    try:
        rc = run_ncr(
            argv,
            _account_env(account),
            log,
            wall + 1200,
            **_gate_kw(gate),
            **({"watch": watch} if watch else {}),
        )
    finally:
        if knb is not None:  # the key leaves this host only once: drop every local copy of it
            for f in out.rglob("sbkey_%s*" % job["id"]):
                with contextlib.suppress(OSError):
                    f.unlink()
    if rc == CANCELLED_RC:
        # abandoned before the job ran: a fresh VM (maybe mid-allocation) is stopped, a warm one kept
        if w.get("warm"):  # a reclaimed worker never ran its clean step: it still owes it
            release_worker(w["name"], True, needs_clean = w.get("clean"))
        else:
            _stop_and_forget(dict(w))
        return {
            "status": "CANCELLED",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "outdir": str(out),
            "reason": "a local GPU freed first",
        }
    if rc == PROVISION_RC and watch is not None and watch.kind:
        # never reached `colab exec`: drop the record (if still ours) and its slot, bench the family
        # unless sweep already did, and let dispatch move on (INFRA: the job never ran)
        with workers() as ws:
            cur = ws["workers"].get(w["name"])
            if cur and cur.get("owner_pid") == os.getpid() and cur["state"] == "PROVISIONING":
                ws["workers"].pop(w["name"])
        if w.get("slot"):
            free_slot(w["slot"])
        if watch.kind != "reclaimed":
            bench_colab(account, tier, watch.kind, watch.reason)
        return {
            "status": "INFRA",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "rc": rc,
            "provision": watch.kind,
            "outdir": str(out),
            "reason": "%s %s: %s; benched %s"
            % (account, tier, watch.reason, "24 h" if watch.kind == "credits" else "30 min"),
        }
    if rc == 2 and not w.get("warm"):
        # CREDENTIAL ERROR (ncr exit 2) before any VM existed: nothing to stop or bill; keep the
        # account out of selection for AUTH_BAD_TTL_S
        mark_auth_bad(account)
        with workers() as ws:
            ws["workers"].pop(w["name"], None)
        if w.get("slot"):
            free_slot(w["slot"])
        return {
            "status": "INFRA",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "rc": rc,
            "outdir": str(out),
            "reason": "%s: Colab credentials rejected; run `cloud_pool.py colab-login "
            "--account %s`" % (account, account),
        }
    found = collect(out)
    got = found.get("sbjob_%s" % job["id"]) or next(iter(found.values()), {})
    if w.get("clean") and got.get("clean") != "OK":
        # a reclaimed / adopted VM that would not come clean (or never answered): quarantine it
        # (stop + forget) and run the job on another worker; the job itself never started
        _stop_and_forget(dict(w))
        if _retry:
            return dispatch_colab(job, cand, _retry = False, gate = gate)
        return {
            "status": "INFRA",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "rc": rc,
            "outdir": str(out),
            "reason": "reclaimed worker %s failed its clean (%s); stopped"
            % (w["name"], got.get("clean") or "no answer"),
        }
    rollback = got.get("rollback") or ""
    res = got.get("result")
    record_disk(tier, res)
    ok_worker = rc in (0, 1) and rollback == "OK"
    release_worker(w["name"], ok_worker)
    short = _disk_short(
        res,
        {
            "backend": "colab",
            "tier": tier,
            "account": account,
            "worker": w["name"],
            "rc": rc,
            "outdir": str(out),
        },
    )
    if short:
        return short
    if rc == 3 or (res is None and rc != 0):
        # infra: VM lost / capacity / never ran. A refusal (no credits, too many sessions) benches
        # this account's tier family so selection moves on; else one retry on a fresh worker.
        text = ""
        with contextlib.suppress(OSError):
            with open(log, "rb") as fh:
                fh.seek(mark)
                text = fh.read().decode("utf-8", "replace")
        kind = classify_colab_refusal(text) if not w.get("warm") else None
        if kind:
            bench_colab(account, tier, kind, "Colab refused (%s)" % kind)
            return {
                "status": "INFRA",
                "backend": "colab",
                "tier": tier,
                "account": account,
                "rc": rc,
                "refusal": kind,
                "outdir": str(out),
                "reason": "%s %s refused: %s; benched %s"
                % (account, tier, kind, "24 h" if kind == "credits" else "10 min"),
            }
        if _retry and w.get("warm"):
            return dispatch_colab(
                job, cand, _retry = False, gate = gate
            )  # a fresh worker, not the lost pin
        return {
            "status": "INFRA",
            "backend": "colab",
            "tier": tier,
            "account": account,
            "rc": rc,
            "outdir": str(out),
            "reason": "notebook_cloud_run exit %s; see %s" % (rc, out / "cloud_pool.log"),
        }
    if res and res.get("secs"):
        record_runtime(tier, job["class"], cand["predicted_s"], res["secs"])
    try:
        arts = fetch_artifacts(got, out, out / "artifacts")
        art_err = None
    except Exception as exc:  # noqa: BLE001 - tar / zlib / HF download
        # a run cut off mid-transfer leaves a truncated SB_ART stream; the verdict still stands
        arts, art_err = [], "artifacts unreadable: %s: %s" % (type(exc).__name__, exc)
    return {
        "status": "PASS" if rc == 0 and res and res.get("rc") == 0 else "FAIL",
        "backend": "colab",
        "artifact_error": art_err,
        "artifact_skipped": got.get("artifact_skipped"),
        "tier": tier,
        "account": account,
        "worker": w["name"],
        "warm": w.get("warm"),
        "rc": rc,
        "outdir": str(out),
        "result": res,
        "rollback": rollback,
        "artifacts": arts,
        "wall_s": round(_now() - t0, 1),
        "predicted_s": cand["predicted_s"],
        "timing_on": tier if job.get("perf") else None,
        "reason": cand["reason"],
    }


KAGGLE_QUEUE_TIMEOUT_S = 1800


def pair_for_kaggle(jobs):
    """Pack jobs two per T4x2 kernel (one per GPU). Perf, multi-GPU and whole-kernel jobs (both T4s,
    e.g. a parallel-arms A/B: base on GPU0, head on GPU1) get a kernel alone."""

    def alone(j):
        return j.get("perf") or j.get("multi_gpu") or j.get("whole_kernel")

    solo = [j for j in jobs if alone(j)]
    rest = sorted([j for j in jobs if not alone(j)], key = lambda j: -j["est_s"])
    return [[j] for j in solo] + [
        rest[i : i + KAGGLE_JOBS_PER_KERNEL] for i in range(0, len(rest), KAGGLE_JOBS_PER_KERNEL)
    ]


def dispatch_kaggle(
    jobs,
    cand,
    env = None,
    gate = None,
):
    env = os.environ if env is None else env
    jobs = [_job_defaults(j) for j in jobs]
    token = (env.get(cand["token_env"]) or "").strip()
    fp = ncr._token_fingerprint(token)
    tiers = load_tiers()
    results = []
    for batch in pair_for_kaggle(jobs):
        est = (
            max(
                predict_s(j["est_s"], cand["tier"], j["dtype"], j["class"], gb = j.get("gb"))
                for j in batch
            )
            + COLD_SETUP_S
        )
        rid = uuid.uuid4().hex[:10]
        with ledger() as st:
            # re-checked under the lock: a racing dispatch may have taken the token's last kernel slot
            if kaggle_room(st, token, env) <= 0:
                results += [
                    {
                        "status": "INFRA",
                        "backend": "kaggle",
                        "no_slot": True,
                        "reason": "%s is benched or full (%d per token, %d across tokens)"
                        % (cand["token_env"], KAGGLE_KERNELS_PER_TOKEN, KAGGLE_KERNELS_TOTAL),
                    }
                    for _ in batch
                ]
                continue
            take_slot(
                st,
                "kaggle",
                cand["tier"],
                tiers,
                token_fp = fp,
                hold_s = est * 2 + 3600,
                sid = "kg-" + rid,
                jobs = [j["id"] for j in batch],
            )
        out = _outdir(batch[0] if len(batch) == 1 else dict(batch[0], id = "pair-" + rid))
        nbs = []
        for j in batch:
            nb = out / ("sbjob_%s.ipynb" % j["id"])
            # Kaggle gets the shared read-only token only (never an upload token), key embedded
            build_job_notebook(dict(j, _remote_env = _remote_env(j, env)), False, nb, embed_key = True)
            nbs.append(str(nb))
        wall = max(int(j.get("timeout_s") or 4 * 3600) for j in batch) + 1800
        par = min(len(batch), 2)
        argv = [
            "--backend",
            "kaggle",
            "--gpu",
            "T4x2",
            "--no-smoke-patch",
            "--per-cell-timeout",
            str(wall),
            "--wall-timeout",
            str(wall),
            "--non-interactive",
            "--kaggle-queue-timeout",
            str(KAGGLE_QUEUE_TIMEOUT_S),
            "--pack",
            str(len(batch)),
            "--parallel-gpus",
            str(par),
            "--outdir",
            str(out),
        ] + nbs
        # The token goes by environment (never argv, which `ps` shows), as the only Kaggle token the
        # child can see, so notebook_cloud_run's own chooser cannot pick another account.
        kenv = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_API_TOKEN")}
        kenv["KAGGLE_API_TOKEN"] = token
        kenv["NBRUN_POOL_SLOT"] = "kg-" + rid  # its kernel record is then not double-counted
        t0 = _now()
        if gate is not None:
            if gate.cancelled():
                with ledger() as st:
                    st["slots"].pop("kg-" + rid, None)
                results += [
                    {
                        "status": "CANCELLED",
                        "backend": "kaggle",
                        "reason": "a local GPU freed first",
                    }
                    for _ in batch
                ]
                continue
            gate.started.set()  # a pushed kernel runs (and bills) whatever happens here
        try:
            rc = run_ncr(argv, kenv, out / "cloud_pool.log", wall + 1800 + KAGGLE_QUEUE_TIMEOUT_S)
        finally:
            used_h = (_now() - t0) / 3600
            # a refusal notebook_cloud_run recorded in this user's ledger becomes a host-wide bench
            until = ncr.KaggleUsageLedger().saturated_until(token)
            with ledger() as st:
                st["kaggle"].setdefault(fp, []).append(
                    [round(_now(), 1), round(used_h, 3), "settled", rid]
                )
                st["slots"].pop("kg-" + rid, None)
                if until > _now():
                    st["kaggle_bench"][fp] = {
                        "until": round(until, 1),
                        "why": "Kaggle refused (%s)" % cand["token_env"],
                    }
        found = collect(out)
        for j in batch:
            got = found.get("sbjob_%s" % j["id"], {})
            res = got.get("result")
            record_disk(cand["tier"], res)
            short = _disk_short(
                res,
                {
                    "backend": "kaggle",
                    "tier": cand["tier"],
                    "token_env": cand["token_env"],
                    "rc": rc,
                    "outdir": str(out),
                },
            )
            if short:
                results.append(short)
                continue
            try:
                arts = fetch_artifacts(got, out, out / "artifacts" / j["id"])
            except Exception:  # noqa: BLE001 - a broken tarball leaves the verdict standing
                arts = []
            if res and res.get("secs"):
                record_runtime(
                    cand["tier"],
                    j["class"],
                    predict_s(j["est_s"], cand["tier"], j["dtype"], j["class"], gb = j.get("gb")),
                    res["secs"],
                )
            status = (
                "INFRA"
                if res is None and rc in (2, 3)
                else ("PASS" if res and res.get("rc") == 0 else "FAIL")
            )
            results.append(
                {
                    "status": status,
                    "backend": "kaggle",
                    "tier": cand["tier"],
                    "token_env": cand["token_env"],
                    "rc": rc,
                    "outdir": str(out),
                    "result": res,
                    "artifacts": arts,
                    "wall_s": round(_now() - t0, 1),
                    "paired_with": [x["id"] for x in batch if x is not j],
                    "timing_on": "Kaggle T4" if j.get("perf") else None,
                    "reason": cand["reason"],
                }
            )
    return results


# ---------------------------------------------------------------------------------------------
# Auth + probes
# ---------------------------------------------------------------------------------------------


def colab_login_command(account):
    home, _ = _account_auth(account)
    home = shlex.quote(home or str(account_home(account)))
    cli = ncr.colab_cli() or "colab"
    # umask 077 so the CLI writes token.json 0600 (it would be 0664); cloud_pool re-tightens on use
    return (
        "mkdir -p %s && chmod 700 %s && (umask 077 && HOME=%s %s --auth oauth2 sessions) && "
        "chmod 600 %s/.config/colab-cli/token.json" % (home, home, home, cli, home)
    )


def colab_missing(fallback = None):
    """Colab accounts with no login for this unix user (file checks only, no network): no token in
    the account HOME, and for daniel none of the real HOME's either (the fallback _account_auth
    uses: the CLI's own ~/.config/colab-cli/token.json, or Application Default Credentials)."""
    if fallback is None:
        fallback = [Path.home() / ".config" / "colab-cli" / "token.json", ncr.adc_path()]
    out = []
    for acct in sorted(COLAB_ACCOUNTS):
        if (account_home(acct) / ".config" / "colab-cli" / "token.json").exists():
            continue
        if acct == "daniel" and any(Path(f).exists() for f in fallback):
            continue
        out.append(acct)
    return out


def colab_login_run(
    accounts,
    run = subprocess.call,
    verify = None,
):
    """Log in each account interactively (URL, paste the code), then check every account for real
    (`colab sessions`, the call sessions make). -> exit code."""
    ncr.record_tools()  # where this shell finds colab / kaggle, for sessions whose PATH lacks them
    if not ncr.colab_cli():
        print(
            "cloud_pool: the colab CLI is not installed: uv tool install 'google-colab-cli>=0.7.4'",
            file = sys.stderr,
        )
        return 2
    rc = 0
    for acct in accounts:
        print(
            "\n=== Colab account %s (sign in as %s) ===" % (acct, COLAB_ACCOUNTS[acct]["email"]),
            flush = True,
        )
        if run(["bash", "-c", colab_login_command(acct)]) != 0:
            print("cloud_pool: login for %s did not finish" % acct, file = sys.stderr)
            rc = 2
    res = (verify or check_auth)()
    bad = []
    for k, (ok, d) in sorted(res.items()):
        if k.startswith("colab:") or not ok:
            print("%-26s %s  %s" % (k, "OK  " if ok else "FAIL", d))
        if k.startswith("colab:") and not ok:
            bad.append(k[6:])
    print(
        "Colab: %s (CLI %s)"
        % ("every account works" if not bad else "not working: " + ", ".join(bad), ncr.colab_cli())
    )
    return rc or (2 if bad else 0)


def check_auth(env = None):
    """{name: (ok, detail)} for both Colab accounts and every Kaggle token, for this unix user."""
    env = os.environ if env is None else env
    out = {}
    saved = os.environ.get("NBRUN_COLAB_HOME")
    try:
        for acct in COLAB_ACCOUNTS:
            home, auth = _account_auth(acct)
            if home:
                os.environ["NBRUN_COLAB_HOME"] = home
            else:
                os.environ.pop("NBRUN_COLAB_HOME", None)
            if home and not (Path(home) / ".config" / "colab-cli" / "token.json").exists():
                out["colab:" + acct] = (False, "not logged in; run: " + colab_login_command(acct))
                continue
            st = ncr.check_colab_auth(provider = auth)
            out["colab:" + acct] = (
                st.ok,
                "%s (%s)" % ("ok" if st.ok else st.reason, home or "real HOME, %s" % auth),
            )
    finally:
        if saved is None:
            os.environ.pop("NBRUN_COLAB_HOME", None)
        else:
            os.environ["NBRUN_COLAB_HOME"] = saved
    for name, tok in kaggle_tokens(env):
        ok, detail, user = ncr.check_kaggle_auth(ncr.KaggleCredentials(token = tok, source = name))
        out["kaggle:" + name] = (ok, ("ok as %s" % user) if ok else detail[:200])
    for name in ("KAGGLE_API_TOKEN", "KAGGLE_API_TOKEN_2", "KAGGLE_API_TOKEN_3"):
        out.setdefault("kaggle:" + name, (False, "%s is not set" % name))
    return out


def probe_concurrency(
    account,
    tier = "T4",
    limit = 4,
    alloc = None,
    stop = None,
):
    """Allocate `tier` sessions on `account` until one is refused (or `limit`), stop them all, and
    record the count as the account's slot limit."""
    alloc = alloc or _probe_alloc
    stop = stop or colab_stop
    made = []
    try:
        for i in range(limit):
            name = "sb-probe-%s-%d-%s" % (account, i, uuid.uuid4().hex[:4])
            if not alloc(account, tier, name):
                break
            made.append(name)
    finally:
        for name in made:
            stop({"name": name, "account": account, "tier": tier})
    with ledger() as st:
        st["accounts"][account] = {
            "slots": max(1, len(made)),
            "probed_at": round(_now()),
            "tier": tier,
        }
    return len(made)


def _probe_alloc(account, tier, name):
    home, auth = _account_auth(account)
    state_home = Path(home) / "nbrun" if home else ncr.colab_state_home()
    state_home.mkdir(parents = True, exist_ok = True)
    cmd = (
        [ncr.colab_cli() or "colab", "--config", str(state_home / ("session-%s.json" % name))]
        + (["--auth", auth] if auth != "adc" else [])
        + ["new", "-s", name, "--gpu", tier.split("-")[0]]
    )
    env = dict(os.environ, **({"HOME": home} if home else {}))
    r = subprocess.run(cmd, env = env, capture_output = True, text = True, timeout = 900)
    text = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and ncr.classify_platform_error(text) is None


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def status(_fetch = None):
    lines = []
    with ledger() as st:
        for acct in COLAB_ACCOUNTS:
            benched = [
                "%s until %s"
                % (k.split(":", 1)[1], time.strftime("%H:%M", time.localtime(v["until"])))
                for k, v in st["colab_bench"].items()
                if k.startswith(acct + ":")
            ]
            lines.append(
                "colab %-8s %d live slot(s)%s"
                % (
                    acct,
                    account_used(st, acct),
                    ", benched: " + ", ".join(benched) if benched else "",
                )
            )
        for name, tok in kaggle_tokens():
            sy = st["sync"].get("kaggle:" + ncr._token_fingerprint(tok))
            fresh = sy and _now() - sy["t"] < SYNC_MAX_AGE_S
            live = ncr.kaggle_quota(tok)
            if live:
                src = ", live (kaggle quota), %.1f h left, refreshes %s UTC" % (
                    live["remaining_h"],
                    time.strftime("%a %H:%M", time.gmtime(live["refresh_at"]))
                    if live.get("refresh_at")
                    else "?",
                )
                total = live["total_h"]
            else:
                total = ncr.kaggle_weekly_hours(name)
                src = (
                    ", synced %s ago%s"
                    % (
                        _ago(sy["t"]),
                        " (STALE: re-run kaggle-sync)" if _now() - sy["t"] > 24 * 3600 else "",
                    )
                    if fresh
                    else ", not synced (this host's runs only)"
                )
            lines.append(
                "kaggle %-18s %.1f / %gh this week, %d kernel(s) running%s%s"
                % (
                    name,
                    kaggle_used_hours(st, tok),
                    total,
                    kaggle_kernels_live(st, tok),
                    src,
                    ", benched (Kaggle refusal)" if kaggle_benched(tok, st) else "",
                )
            )
        for sid, s in sorted(st["slots"].items()):
            lines.append(
                "  slot %-28s %-7s %-8s %-8s user=%s expires in %.0f min"
                % (
                    sid,
                    s["backend"],
                    s["tier"],
                    s.get("account") or "",
                    s.get("user"),
                    (s["expires_at"] - _now()) / 60,
                )
            )
    with workers() as ws:
        for name, w in sorted(ws["workers"].items()):
            prov = ""
            if w["state"] == "PROVISIONING" and w.get("created"):
                age = _now() - float(w["created"])
                prov = (
                    " provisioning %.0f min%s"
                    % (age / 60, " STUCK" if age > provision_deadline(w["tier"]) else "")
                    if w.get("tier") in load_tiers()
                    else ""
                )
            lines.append(
                "  worker %-28s %-11s gen=%d jobs=%d%s"
                % (name, w["state"], w.get("gen", 0), w.get("jobs", 0), prov)
            )
    for acct in COLAB_ACCOUNTS:
        with contextlib.suppress(Exception):
            v = server_sessions(acct, _fetch = _fetch)
            if not v["ok"]:
                lines.append(
                    "colab %-8s server: unknown (no login for this user or CLI error)" % acct
                )
                continue
            lines.append(
                "colab %-8s server: %d live session(s), fetched %s ago"
                % (acct, len(v["sessions"]), _ago(v["t"]))
            )
            for x in v["sessions"]:
                own = v["pool"].get(x["endpoint"])
                lines.append(
                    "  session %-40s %-8s %s"
                    % (
                        x["endpoint"],
                        x["family"],
                        "pool (%s, %s)" % (own["worker"], own["user"])
                        if own
                        else "unmanaged (not the pool's: never adopted or stopped)",
                    )
                )
    return "\n".join(lines)


def _job_from_args(a):
    job = {
        "gb": a.gb,
        "dtype": a.dtype,
        "est_s": a.est_min * 60,
        "class": a.job_class,
        "ram_gb": a.ram_gb,
        "perf": a.perf,
        "multi_gpu": a.multi_gpu,
        "argv": a.argv,
        "requirements": a.req or [],
        "torch": a.torch,
        "artifacts": a.artifact or [],
        "files": a.file or [],
        "pass_hf_token": a.pass_hf_token,
        "shared_models": a.shared_models,
        "env": ncr.parse_env_pairs(a.env or []),
    }
    if a.timeout_min:
        job["timeout_s"] = int(a.timeout_min * 60)
    if a.script:
        job["script"] = a.script
    if a.notebook:
        job["notebook"] = a.notebook
    return job


def main(argv = None):
    p = argparse.ArgumentParser(
        description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest = "cmd", required = True)

    def job_args(sp):
        sp.add_argument(
            "--gb", type = float, default = 8.0, help = "VRAM reservation (single device), GB"
        )
        sp.add_argument("--dtype", default = "bf16", help = "fp16|bf16|fp8|nvfp4|fp32")
        sp.add_argument("--ram-gb", type = float, default = 0.0)
        sp.add_argument(
            "--est-min", type = float, default = 10.0, help = "runtime estimate on a B200, minutes"
        )
        sp.add_argument("--class", dest = "job_class", default = "train", choices = sorted(COMPUTE_SHARE))
        sp.add_argument("--perf", action = "store_true", help = "timing job: runs alone on one card")
        sp.add_argument("--multi-gpu", action = "store_true")
        sp.add_argument("--backend", default = "auto", choices = ("auto", "colab", "kaggle", "amd_ci"))
        sp.add_argument("--account", choices = sorted(COLAB_ACCOUNTS), help = "pin the Colab account")

    s = sub.add_parser("select", help = "rank remote tiers for a job")
    job_args(s)
    s.add_argument("--json", action = "store_true")
    r = sub.add_parser("run", help = "run a script or notebook remotely")
    job_args(r)
    g = r.add_mutually_exclusive_group(required = True)
    g.add_argument("--script")
    g.add_argument("--notebook")
    r.add_argument("--req", action = "append", help = "pip requirement for the job venv (repeatable)")
    r.add_argument(
        "--torch", default = "auto", help = "auto | none | a pip spec ('torch==2.8.0 torchvision')"
    )
    r.add_argument(
        "--artifact", action = "append", help = "glob under the job dir to bring back (<=5 MB total)"
    )
    r.add_argument("--file", action = "append", help = "extra local file shipped next to the script")
    r.add_argument("--env", action = "append", metavar = "KEY=VALUE")
    r.add_argument("--timeout-min", type = float, default = None)
    r.add_argument(
        "--pass-hf-token",
        action = "store_true",
        help = "send your own HF_TOKEN (default: the shared read-only one)",
    )
    r.add_argument(
        "--shared-models",
        action = "store_true",
        help = "keep HF models in the worker's switchboard cache",
    )
    r.add_argument("--dry-run", action = "store_true")
    r.add_argument("--outdir")
    r.add_argument("argv", nargs = "*")
    sub.add_parser("status")
    sub.add_parser("sweep")
    sub.add_parser("check-auth")
    lg = sub.add_parser(
        "colab-login", help = "print the login command for an account (--run: log in now)"
    )
    lg.add_argument(
        "--account",
        choices = sorted(COLAB_ACCOUNTS),
        help = "default with --run: every account not logged in",
    )
    lg.add_argument(
        "--run", action = "store_true", help = "log in interactively (paste the code Google shows)"
    )
    cm = sub.add_parser(
        "colab-missing", help = "Colab accounts this unix user has not logged in (no network)"
    )
    cm.add_argument(
        "--record-tools",
        action = "store_true",
        help = "also record where this shell finds colab / kaggle",
    )
    ex = sub.add_parser("colab-expire")
    ex.add_argument("--worker", required = True)
    ex.add_argument("--gen", type = int, required = True)
    ex.add_argument("--after", type = float, required = True)
    ks = sub.add_parser("kaggle-sync", help = "set this week's Kaggle GPU hours from the website")
    ks.add_argument("--token-env", required = True, help = "KAGGLE_API_TOKEN, KAGGLE_API_TOKEN_2, ...")
    kg = ks.add_mutually_exclusive_group(required = True)
    kg.add_argument("--used-hours", type = float)
    kg.add_argument("--left-hours", type = float, help = "the site's 'available' number")
    pc = sub.add_parser("probe-concurrency")
    pc.add_argument("--account", required = True, choices = sorted(COLAB_ACCOUNTS))
    pc.add_argument("--tier", default = "T4")
    pc.add_argument("--limit", type = int, default = 4)
    a = p.parse_args(argv)

    if a.cmd == "select":
        backends = ("kaggle", "colab") if a.backend == "auto" else (a.backend,)
        c = select_tier(
            a.gb,
            a.dtype,
            a.ram_gb,
            a.est_min * 60,
            a.job_class,
            a.perf,
            a.multi_gpu,
            backends = backends,
        )
        if a.account:
            c = [x for x in c if x["backend"] != "colab" or x["account"] == a.account]
        if a.json:
            print(json.dumps(c, indent = 1))
        else:
            for x in c:
                eta = (
                    "~%4.0f min" % (x["predicted_s"] / 60)
                    if x["predicted_s"] is not None
                    else "   ? min"
                )
                print(
                    "%-7s %-12s %-8s %s  %6.2f CU  %s"
                    % (
                        x["backend"],
                        x["tier"],
                        x["account"] or "",
                        eta,
                        x["expected_cu"],
                        x["reason"],
                    )
                )
            if not c:
                print("NO_REMOTE_FIT: no tier has the VRAM / dtype / quota for this job")
        return 0 if c else 4
    if a.cmd == "run":
        job = _job_from_args(a)
        if a.outdir:
            job["outdir"] = a.outdir
        res = dispatch(job, backend = a.backend, dry_run = a.dry_run, account = a.account)
        print(json.dumps(res, indent = 1, default = str))
        return {
            "PASS": 0,
            "DRY_RUN": 0,
            "FAIL": 1,
            "INFRA": 3,
            "NO_REMOTE_FIT": 4,
            "MANUAL": 5,
        }.get(res.get("status"), 3)
    if a.cmd == "status":
        print(status())
        return 0
    if a.cmd == "sweep":
        print("\n".join(sweep()) or "nothing to stop")
        return 0
    if a.cmd == "check-auth":
        res = check_auth()
        for k, (ok, d) in res.items():
            print("%-26s %s  %s" % (k, "OK  " if ok else "FAIL", d))
        return 0 if all(ok for ok, _ in res.values()) else 2
    if a.cmd == "colab-login":
        if a.run:
            return colab_login_run([a.account] if a.account else colab_missing())
        if not a.account:
            p.error("colab-login needs --account (or --run)")
        print(colab_login_command(a.account))
        return 0
    if a.cmd == "colab-missing":
        if a.record_tools:
            ncr.record_tools()
        print(" ".join(colab_missing()))
        return 0
    if a.cmd == "colab-expire":
        print(colab_expire(a.worker, a.gen, a.after))
        return 0
    if a.cmd == "kaggle-sync":
        rec = kaggle_sync(a.token_env, a.used_hours, a.left_hours)
        print(
            "%s: %.1f h used this week as of now (tool-recorded runs after this add on)"
            % (a.token_env, rec["used_h"])
        )
        return 0
    if a.cmd == "probe-concurrency":
        print(
            "%s: %d concurrent %s session(s)"
            % (a.account, probe_concurrency(a.account, a.tier, a.limit), a.tier)
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
