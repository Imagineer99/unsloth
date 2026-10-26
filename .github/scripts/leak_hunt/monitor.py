"""Process-tree sampler + deterministic leak verdicts for leak_hunt.

Attribution per OS:
  linux:  the harness is PR_SET_CHILD_SUBREAPER, so every orphan re-parents to it and stays a
          descendant of the harness; candidates = harness descendants.
  darwin: descendants of the roots, plus every process already tracked (pid + create_time) after it
          re-parents to launchd, plus a sweep of ppid==1 same-uid processes created after start whose
          environ carries LEAK_HUNT_TAG=<run id>.
  win32:  members of a Job object the roots were assigned to, plus descendants of the roots.
Spawn sites come from the accel spawn log (spawns-*.jsonl), joined by pid + time.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import psutil

DEFAULTS = {
    "orphan_s": 10.0,
    "pileup_n": 3,
    "accumulate_n": 2,
    "accumulate_span_s": 10.0,
    "spin_frac": 0.8,
    "spin_s": 10.0,
    "zombie_s": 10.0,
}


# ------------------------------------------------------------------ OS attribution helpers


def make_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno = True)
        PR_SET_CHILD_SUBREAPER = 36
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except Exception:
        return False


class WinJob:
    """A Job object the roots are assigned to; members enumerated with QueryInformationJobObject."""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error = True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        self.k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        self.k32.OpenProcess.restype = ctypes.c_void_p
        h = self.k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(
                self.k32.AssignProcessToJobObject(ctypes.c_void_p(self.handle), ctypes.c_void_p(h))
            )
        finally:
            self.k32.CloseHandle(ctypes.c_void_p(h))

    def members(self) -> set[int]:
        if not self.handle:
            return set()
        n = 4096

        class LIST(ctypes.Structure):
            _fields_ = [
                ("assigned", ctypes.c_ulong),
                ("listed", ctypes.c_ulong),
                ("ids", ctypes.c_size_t * n),
            ]

        buf = LIST()
        ok = self.k32.QueryInformationJobObject(
            ctypes.c_void_p(self.handle), 3, ctypes.byref(buf), ctypes.sizeof(buf), None
        )
        if not ok:
            return set()
        return {int(buf.ids[i]) for i in range(buf.listed)}


# ------------------------------------------------------------------ tracking


@dataclass
class Track:
    pid: int
    ctime: float
    cmdline: str
    name: str
    first_seen: float
    last_seen: float
    orig_ppid: int
    parent_key: tuple | None
    parent_chain: list
    is_server: bool = False
    is_root: bool = False
    root_label: str = ""
    site: str = ""
    spawn: dict | None = None
    exited_at: float | None = None
    orphaned_at: float | None = None
    zombie_since: float | None = None
    zombie_max_s: float = 0.0
    ppids: list = field(default_factory = list)  # [(t, ppid)] on change
    cpu: list = field(default_factory = list)  # [(t, user+system)]
    alive_after_stop: bool = False
    cwd: str = ""

    def key(self):
        return (self.pid, round(self.ctime, 2))

    def to_json(self):
        d = {k: v for k, v in self.__dict__.items() if k != "cpu"}
        d["cpu_samples"] = len(self.cpu)
        d["cpu_total_s"] = round(self.cpu[-1][1], 2) if self.cpu else 0.0
        return d


def _proc_key(p: psutil.Process):
    return (p.pid, round(p.create_time(), 2))


class Monitor:
    def __init__(
        self,
        run_tag: str,
        spawn_dir: str,
        hz: float = 4.0,
        markers: tuple = (),
    ):
        self.run_tag, self.spawn_dir, self.period = run_tag, spawn_dir, 1.0 / hz
        # cmdline / cwd substrings (the run's Studio home) for orphans whose env was scrubbed
        self.markers = tuple(m for m in markers if m)
        self.me = os.getpid()
        self.uid = os.getuid() if hasattr(os, "getuid") else None
        self.start = time.time()
        self.roots: dict[int, str] = {}
        self.server_pids: set[int] = set()
        self.tracks: dict[tuple, Track] = {}
        self.site_live: list = []  # sample times
        self.spawns: dict[int, list] = {}
        self._offsets: dict[str, int] = {}
        self.subreaper = make_subreaper()
        self.job = WinJob() if sys.platform == "win32" else None
        self._stop = threading.Event()
        self._thread = None
        self.stop_time: float | None = None
        self.server_exit_time: float | None = None
        self.server_stats: dict = {}
        self.errors: list = []

    # roots ------------------------------------------------------------
    def add_root(self, pid: int, label: str) -> None:
        self.roots[pid] = label
        if self.job is not None and not self.job.assign(pid):
            self.errors.append(f"job assign failed for {pid}")

    def mark_server(self, pid: int) -> None:
        self.server_pids.add(pid)

    # spawn log --------------------------------------------------------
    def _read_spawns(self) -> None:
        for path in glob.glob(os.path.join(self.spawn_dir, "spawns-*.jsonl")):
            off = self._offsets.get(path, 0)
            try:
                with open(path, encoding = "utf-8") as fh:
                    fh.seek(off)
                    chunk = fh.read()
                    # only whole lines
                    cut = chunk.rfind("\n") + 1
                    self._offsets[path] = off + len(chunk[:cut].encode("utf-8"))
            except OSError:
                continue
            for line in chunk[:cut].splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec.get("pid"), int):
                    self.spawns.setdefault(rec["pid"], []).append(rec)

    def _spawn_for(self, pid: int, ctime: float):
        recs = self.spawns.get(pid)
        if not recs:
            return None
        best = min(recs, key = lambda r: abs(float(r.get("t", 0)) - ctime))
        return best if abs(float(best.get("t", 0)) - ctime) < 30 else None

    # candidate discovery ---------------------------------------------
    def _candidates(self) -> dict:
        found = {}

        def add(p):
            try:
                if p.pid == self.me:
                    return
                found[_proc_key(p)] = p
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        if self.subreaper:
            try:
                for p in psutil.Process(self.me).children(recursive = True):
                    add(p)
            except psutil.Error:
                pass
        for root in list(self.roots):
            try:
                rp = psutil.Process(root)
                add(rp)
                for p in rp.children(recursive = True):
                    add(p)
            except psutil.Error:
                pass
        if self.job is not None:
            for pid in self.job.members():
                try:
                    add(psutil.Process(pid))
                except psutil.Error:
                    pass
        # previously tracked processes keep being tracked after re-parenting (macOS: launchd)
        for key, tr in list(self.tracks.items()):
            if tr.exited_at is None and key not in found:
                try:
                    p = psutil.Process(tr.pid)
                    if _proc_key(p) == key:
                        add(p)
                except psutil.Error:
                    pass
        if sys.platform == "darwin":
            self._tag_sweep(add)
        return found

    def _tag_sweep(self, add) -> None:
        marker = f"LEAK_HUNT_TAG={self.run_tag}"
        for p in psutil.process_iter(["pid", "ppid", "uids", "create_time"]):
            try:
                info = p.info
                if info["ppid"] != 1 or info["create_time"] < self.start - 1:
                    continue
                if self.uid is not None and info["uids"] and info["uids"].real != self.uid:
                    continue
                env = p.environ()
                if env.get("LEAK_HUNT_TAG") == self.run_tag or marker in env:
                    add(p)
                    continue
                if self.markers:
                    text = " ".join(p.cmdline())
                    try:
                        text += " " + p.cwd()
                    except psutil.Error:
                        pass
                    if any(m in text for m in self.markers):
                        add(p)
            except (psutil.Error, KeyError):
                continue

    # sampling ---------------------------------------------------------
    def _parent_chain(self, p: psutil.Process) -> list:
        out = []
        try:
            for a in p.parents()[:6]:
                try:
                    out.append(f"{a.pid}:{a.name()}")
                except psutil.Error:
                    out.append(str(a.pid))
        except psutil.Error:
            pass
        return out

    def tick(self) -> None:
        now = time.time()
        self._read_spawns()
        found = self._candidates()
        for key, p in found.items():
            try:
                with p.oneshot():
                    ppid = p.ppid()
                    status = p.status()
                    ct = p.cpu_times()
                    cpu = float(ct.user + ct.system)
                    tr = self.tracks.get(key)
                    if tr is None:
                        try:
                            cmd = " ".join(p.cmdline())[:400]
                        except psutil.Error:
                            cmd = ""
                        try:
                            name = p.name()
                        except psutil.Error:
                            name = "?"
                        parent_key = None
                        try:
                            parent = psutil.Process(ppid)
                            parent_key = _proc_key(parent)
                        except psutil.Error:
                            pass
                        tr = Track(
                            pid = p.pid,
                            ctime = p.create_time(),
                            cmdline = cmd,
                            name = name,
                            first_seen = now,
                            last_seen = now,
                            orig_ppid = ppid,
                            parent_key = parent_key,
                            parent_chain = self._parent_chain(p),
                            is_server = p.pid in self.server_pids,
                            is_root = p.pid in self.roots,
                            root_label = self.roots.get(p.pid, ""),
                        )
                        try:
                            tr.cwd = p.cwd()
                        except psutil.Error:
                            pass
                        tr.ppids.append((now, ppid))
                        if p.pid not in self.roots and ppid in (1, self.me):
                            tr.orphaned_at = now  # orphaned before we first saw it
                            tr.parent_key = None
                        self.tracks[key] = tr
                    tr.last_seen = now
                    if tr.ppids[-1][1] != ppid:
                        tr.ppids.append((now, ppid))
                    tr.cpu.append((now, cpu))
                    if p.pid in self.server_pids:
                        tr.is_server = True
                    if status == psutil.STATUS_ZOMBIE:
                        # dead: never alive for SURVIVOR / PILEUP / ORPHAN
                        if tr.exited_at is None:
                            tr.exited_at = now
                        if ppid == self.me and p.pid not in self.roots:
                            # adopted by us as subreaper: ours to reap, not a Studio fault
                            try:
                                os.waitpid(p.pid, os.WNOHANG)
                            except (ChildProcessError, OSError):
                                pass
                            tr.zombie_since = None
                        else:
                            tr.zombie_since = tr.zombie_since or now
                            tr.zombie_max_s = max(tr.zombie_max_s, now - tr.zombie_since)
                    else:
                        tr.zombie_since = None
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for key, tr in self.tracks.items():
            if key not in found and tr.exited_at is None:
                tr.exited_at = now
            if tr.exited_at is None and tr.orphaned_at is None and tr.parent_key is not None:
                if tr.parent_key not in found:
                    alive = False
                    try:
                        pp = psutil.Process(tr.parent_key[0])
                        alive = (
                            _proc_key(pp) == tr.parent_key and pp.status() != psutil.STATUS_ZOMBIE
                        )
                    except psutil.Error:
                        alive = False
                    if not alive and tr.parent_key[0] != self.me:
                        tr.orphaned_at = now
            if tr.spawn is None:
                rec = self._spawn_for(tr.pid, tr.ctime)
                if rec is not None:
                    tr.spawn = {
                        k: rec.get(k)
                        for k in (
                            "kind",
                            "site",
                            "frames",
                            "argv",
                            "start_new_session",
                            "process_group",
                            "creationflags",
                            "ppid",
                        )
                    }
        self.site_live.append(now)

    def _is_alive(self, tr: Track) -> bool:
        try:
            p = psutil.Process(tr.pid)
            return _proc_key(p) == tr.key() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def site_of(self, tr: Track) -> str:
        if tr.spawn and tr.spawn.get("site") and tr.spawn["site"] != "unknown":
            return tr.spawn["site"]
        parent = self.tracks.get(tr.parent_key) if tr.parent_key else None
        if parent is not None and parent is not tr:
            psite = parent.site or (parent.spawn or {}).get("site") or parent.name
            return f"child-of[{psite}]:{tr.name}"
        if tr.cwd and any(tr.cwd.startswith(m) for m in self.markers):
            return f"tool-workdir:{tr.name}"
        return f"unattributed:{tr.name}"

    def snapshot_server(self, label: str, pid: int) -> None:
        try:
            p = psutil.Process(pid)
            info = {"threads": p.num_threads()}
            try:
                info["fds"] = p.num_fds() if hasattr(p, "num_fds") else p.num_handles()
            except psutil.Error:
                info["fds"] = None
            self.server_stats[label] = info
        except psutil.Error as exc:
            self.server_stats[label] = {"error": repr(exc)}

    def run_background(self) -> None:
        def loop():
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    self.tick()
                except Exception as exc:  # keep sampling
                    self.errors.append(repr(exc))
                self._stop.wait(max(0.0, self.period - (time.time() - t0)))

        self._thread = threading.Thread(target = loop, name = "leak-hunt-monitor", daemon = True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(10)

    def finalize(self) -> list[dict]:
        for tr in self.tracks.values():
            tr.site = self.site_of(tr)
        return [tr.to_json() | {"_cpu": tr.cpu} for tr in self.tracks.values()]


# ------------------------------------------------------------------ verdicts (pure)


def _finding(kind, tr, detail, **extra):
    return {
        "kind": kind,
        "pid": tr["pid"],
        "cmdline": tr["cmdline"],
        "site": tr.get("site"),
        "spawn_frames": (tr.get("spawn") or {}).get("frames"),
        "parent_chain": tr.get("parent_chain"),
        "first_seen": tr["first_seen"],
        "last_seen": tr["last_seen"],
        "detail": detail,
        **extra,
    }


def evaluate(
    tracks: list[dict],
    site_live: list,
    *,
    stop_time: float,
    soak_start: float,
    allow: list[str] | None = None,
    thresholds: dict | None = None,
) -> list[dict]:
    """tracks: Monitor.finalize() rows (need pid, cmdline, site, is_server, is_root, first_seen,
    last_seen, exited_at, orphaned_at, zombie_max_s, alive_after_stop, _cpu). Deterministic."""
    th = {**DEFAULTS, **(thresholds or {})}
    allow_re = [re.compile(a) for a in (allow or [])]
    out = []

    def allowed(tr):
        return any(r.search(tr.get("cmdline") or "") for r in allow_re)

    for tr in tracks:
        if allowed(tr):
            continue
        end = tr["exited_at"] if tr.get("exited_at") is not None else tr["last_seen"]
        oa = tr.get("orphaned_at")
        # cleaned up before the soak ended (e.g. killed by the tool's own timeout) is not a leak
        alive_at_stop = tr.get("exited_at") is None or tr["exited_at"] >= stop_time
        if (
            oa is not None
            and oa < stop_time
            and alive_at_stop
            and not tr.get("is_root")
            and end - oa > th["orphan_s"]
        ):
            out.append(_finding("ORPHAN", tr, f"alive {end - oa:.1f}s after its parent exited"))
        if tr.get("alive_after_stop"):
            out.append(
                _finding("SURVIVOR", tr, "alive after Studio exited and the post-stop window")
            )
        if tr.get("zombie_max_s", 0) > th["zombie_s"]:
            out.append(_finding("ZOMBIE", tr, f"unreaped zombie for {tr['zombie_max_s']:.1f}s"))
        if not tr.get("is_server") and not tr.get("is_root"):
            cpu = tr.get("_cpu") or []
            k = 0
            for i in range(len(cpu)):
                while k + 1 < i and cpu[i][0] - cpu[k + 1][0] >= th["spin_s"]:
                    k += 1
                dt = cpu[i][0] - cpu[k][0]
                if dt >= th["spin_s"] and (cpu[i][1] - cpu[k][1]) / dt >= th["spin_frac"]:
                    frac = (cpu[i][1] - cpu[k][1]) / dt
                    out.append(
                        _finding("SPINNER", tr, f"{frac * 100:.0f}% of a core over {dt:.1f}s")
                    )
                    break
    # PILEUP: concurrent live count per (final) site at each sample time
    peak: dict = {}
    eligible = [
        tr for tr in tracks if not tr.get("is_server") and not tr.get("is_root") and not allowed(tr)
    ]
    for t in site_live:
        counts: dict = {}
        for tr in eligible:
            if tr["first_seen"] <= t and (tr.get("exited_at") is None or tr["exited_at"] > t):
                counts[tr.get("site")] = counts.get(tr.get("site"), 0) + 1
        for site, n in counts.items():
            if n > peak.get(site, (0, 0))[0]:
                peak[site] = (n, t)
    by_site: dict = {}
    for tr in tracks:
        if not tr.get("is_server") and not tr.get("is_root") and not allowed(tr):
            by_site.setdefault(tr.get("site"), []).append(tr)
    for site, (n, t) in sorted(peak.items(), key = lambda kv: str(kv[0])):
        members = by_site.get(site, [])
        if not members:
            continue
        if n >= th["pileup_n"]:
            out.append(
                {
                    "kind": "PILEUP",
                    "site": site,
                    "peak_live": n,
                    "at": t,
                    "pids": [m["pid"] for m in members][:20],
                    "cmdline": members[0]["cmdline"],
                    "detail": f"{n} live at once from one site",
                }
            )
            continue
        # accumulation: processes from one site still alive at stop, created over a span
        alive_at_stop = [
            m
            for m in members
            if (m.get("exited_at") is None or m["exited_at"] >= stop_time)
            and m["first_seen"] >= soak_start
        ]
        if len(alive_at_stop) >= th["accumulate_n"]:
            span = max(m["first_seen"] for m in alive_at_stop) - min(
                m["first_seen"] for m in alive_at_stop
            )
            if span >= th["accumulate_span_s"]:
                out.append(
                    {
                        "kind": "PILEUP",
                        "site": site,
                        "peak_live": len(alive_at_stop),
                        "at": stop_time,
                        "pids": [m["pid"] for m in alive_at_stop][:20],
                        "cmdline": alive_at_stop[0]["cmdline"],
                        "detail": f"{len(alive_at_stop)} accumulated over {span:.0f}s, still alive at stop",
                    }
                )
    return out
