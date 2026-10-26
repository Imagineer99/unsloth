"""leak_hunt runtime accelerator, loaded at interpreter start (sitecustomize or .pth).

Inert unless LEAK_HUNT_DIR is set. Then, in every Python process of the Studio venv:
  * spawn log: every Popen / os.fork / os.posix_spawn* / multiprocessing start appends one JSON
    line (pid, argv head, flags, first non-stdlib frames) to LEAK_HUNT_DIR/spawns-<writer pid>.jsonl.
  * timer compression (only the process that first imports a module named in LEAK_HUNT_TIMERS, i.e.
    the Studio server; LEAK_HUNT_COMPRESS=1 is then popped so its children do not inherit it):
    each listed constant / literal loop sleep is rewritten in the module AST before exec to
    min(value, LEAK_HUNT_COMPRESS_TO), so derived constants and default args follow. Rewritten
    modules are never written to the bytecode cache. Each rewrite -> compressed.jsonl.
Never raises into the host program.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time

_DIR = os.environ.get("LEAK_HUNT_DIR") or ""
_lock = threading.Lock()
_files: dict = {}
_STDLIB_PREFIXES: tuple = ()


def _stdlib_prefixes() -> tuple:
    import sysconfig

    out = set()
    for key in ("stdlib", "platstdlib"):
        try:
            p = sysconfig.get_paths().get(key)
        except Exception:
            p = None
        if p:
            out.add(os.path.realpath(p))
    return tuple(out)


def _write(stem: str, record: dict) -> None:
    try:
        pid = os.getpid()
        record.setdefault("t", time.time())
        record.setdefault("writer", pid)
        line = json.dumps(record, default = str) + "\n"
        with _lock:
            key = (stem, pid)
            fh = _files.get(key)
            if fh is None:
                fh = open(
                    os.path.join(_DIR, f"{stem}-{pid}.jsonl"), "a", buffering = 1, encoding = "utf-8"
                )
                _files[key] = fh
            fh.write(line)
    except Exception:
        pass


def _frames(skip: int = 2, limit: int = 6) -> list:
    out = []
    try:
        f = sys._getframe(skip)
    except Exception:
        return out
    here = os.path.realpath(__file__)
    while f is not None and len(out) < limit:
        fn = f.f_code.co_filename
        try:
            real = os.path.realpath(fn)
        except Exception:
            real = fn
        if real != here and not real.startswith(_STDLIB_PREFIXES) and not fn.startswith("<frozen"):
            out.append(f"{fn}:{f.f_lineno}:{f.f_code.co_name}")
        f = f.f_back
    return out


def _site(frames: list) -> str:
    return frames[0].rsplit(":", 1)[0] if frames else "unknown"


def _log_spawn(
    kind: str,
    pid,
    argv = None,
    **extra,
) -> None:
    fr = _frames(3)
    rec = {"kind": kind, "pid": pid, "ppid": os.getpid(), "site": _site(fr), "frames": fr}
    if argv is not None:
        try:
            if isinstance(argv, (str, bytes)):
                argv = [argv]
            rec["argv"] = [str(a) for a in list(argv)[:6]]
        except Exception:
            rec["argv"] = [repr(argv)[:200]]
    rec.update(extra)
    _write("spawns", rec)


def _install_spawn_log() -> None:
    import subprocess

    orig_init = subprocess.Popen.__init__
    if getattr(orig_init, "_leak_hunt", False):
        return

    def __init__(self, args, *a, **kw):
        orig_init(self, args, *a, **kw)
        try:
            _log_spawn(
                "popen",
                getattr(self, "pid", None),
                args,
                cwd = str(kw.get("cwd")) if kw.get("cwd") is not None else None,
                start_new_session = bool(kw.get("start_new_session")),
                process_group = kw.get("process_group"),
                creationflags = kw.get("creationflags", 0),
            )
        except Exception:
            pass

    __init__._leak_hunt = True
    subprocess.Popen.__init__ = __init__

    if hasattr(os, "fork"):
        orig_fork = os.fork

        def fork():
            pid = orig_fork()
            if pid:
                _log_spawn("fork", pid, None)
            return pid

        os.fork = fork

    for name in ("posix_spawn", "posix_spawnp"):
        orig = getattr(os, name, None)
        if orig is None:
            continue

        def make(orig, name):
            def wrapper(path, argv, *a, **kw):
                pid = orig(path, argv, *a, **kw)
                _log_spawn(name, pid, argv)
                return pid

            return wrapper

        setattr(os, name, make(orig, name))

    try:
        import multiprocessing.process as mpp

        orig_start = mpp.BaseProcess.start

        def start(self):
            orig_start(self)
            try:
                _log_spawn("multiprocessing", self.pid, [getattr(self, "name", "")])
            except Exception:
                pass

        mpp.BaseProcess.start = start
    except Exception:
        pass


# ---------------------------------------------------------------- timer compression

_targets: dict = {}  # module -> {"names": {(scope, name): unit}, "sleeps": {line}}
_compress_to = 1.0
_active = False


def _load_targets(path: str) -> None:
    with open(path, encoding = "utf-8") as fh:
        data = json.load(fh)
    for c in data.get("constants", []):
        mod = c.get("module")
        if not mod or not c.get("name"):
            continue
        t = _targets.setdefault(mod, {"names": {}, "sleeps": set()})
        t["names"][(c.get("scope") or "module", c["name"])] = c.get("unit") or "s"
    for s in data.get("loop_sleeps", []):
        mod = s.get("module")
        if mod and s.get("line"):
            _targets.setdefault(mod, {"names": {}, "sleeps": set()})["sleeps"].add(int(s["line"]))


def _target_for(fullname: str):
    t = _targets.get(fullname)
    if t is not None:
        return t
    for mod, t in _targets.items():
        if fullname.endswith("." + mod) or mod.endswith("." + fullname):
            return t
    return None


def _num(node):
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return node.value
    try:
        v = eval(compile(ast.Expression(node), "<leak_hunt>", "eval"), {"__builtins__": {}})
    except Exception:
        return None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cap(value, unit: str):
    cap = _compress_to * (1000.0 if unit == "ms" else 1.0)
    if value <= cap:
        return None
    return max(1, int(round(cap))) if isinstance(value, int) else float(cap)


class _Rewriter(ast.NodeTransformer):
    def __init__(self, module: str, target: dict):
        self.module, self.target, self.scope, self.records = module, target, ["module"], []

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node):
        self.scope.append("<function>")
        self.generic_visit(node)
        self.scope.pop()
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def _assign(self, node, targets):
        scope = self.scope[-1]
        for tg in targets:
            if isinstance(tg, ast.Name):
                unit = self.target["names"].get((scope, tg.id))
                if unit is None:
                    continue
                old = _num(node.value)
                if old is None:
                    continue
                new = _cap(old, unit)
                if new is None:
                    continue
                node.value = ast.copy_location(ast.Constant(new), node.value)
                self.records.append(
                    {
                        "module": self.module,
                        "name": tg.id,
                        "scope": scope,
                        "line": node.lineno,
                        "old": old,
                        "new": new,
                    }
                )
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        return self._assign(node, node.targets)

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        return self._assign(node, [node.target]) if node.value is not None else node

    def visit_Call(self, node):
        self.generic_visit(node)
        if node.lineno in self.target["sleeps"] and node.args:
            old = _num(node.args[0])
            if old is not None:
                new = _cap(old, "s")
                if new is not None:
                    node.args[0] = ast.copy_location(ast.Constant(new), node.args[0])
                    self.records.append(
                        {
                            "module": self.module,
                            "name": "<sleep>",
                            "scope": self.scope[-1],
                            "line": node.lineno,
                            "old": old,
                            "new": new,
                        }
                    )
        return node


class _Finder:
    """Meta-path finder: wraps the normal spec of a target module with a rewriting loader."""

    def find_spec(
        self,
        fullname,
        path = None,
        target = None,
    ):
        if not _active or _target_for(fullname) is None:
            return None
        from importlib.machinery import PathFinder, SourceFileLoader

        spec = PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, SourceFileLoader):
            return None
        spec.loader = _RewritingLoader(fullname, spec.origin)
        return spec


def _make_loader_cls():
    from importlib.machinery import SourceFileLoader
    class RewritingLoader(SourceFileLoader):
        def get_code(self, fullname):
            source = self.get_data(self.path)
            tree = ast.parse(source, self.path)
            rw = _Rewriter(fullname, _target_for(fullname))
            tree = ast.fix_missing_locations(rw.visit(tree))
            for rec in rw.records:
                _write("compressed", rec)
            _write(
                "compressed", {"module": fullname, "name": "<loaded>", "rewrites": len(rw.records)}
            )
            return compile(tree, self.path, "exec", dont_inherit = True)

    return RewritingLoader


_RewritingLoader = None


def _install_compression() -> None:
    global _compress_to, _active, _RewritingLoader
    timers = os.environ.get("LEAK_HUNT_TIMERS")
    if not timers:
        return
    _load_targets(timers)
    _compress_to = float(os.environ.get("LEAK_HUNT_COMPRESS_TO", "1.0"))
    _RewritingLoader = _make_loader_cls()
    _active = True
    sys.meta_path.insert(0, _ActivatingFinder())


class _ActivatingFinder(_Finder):
    """Claims the server on its first target import: pops LEAK_HUNT_COMPRESS, records limits."""

    claimed = False

    def find_spec(
        self,
        fullname,
        path = None,
        target = None,
    ):
        if _target_for(fullname) is None:
            return None
        if not _ActivatingFinder.claimed:
            if os.environ.get("LEAK_HUNT_COMPRESS") != "1":
                return None
            _ActivatingFinder.claimed = True
            os.environ.pop("LEAK_HUNT_COMPRESS", None)
            _record_server()
        return super().find_spec(fullname, path, target)


def _record_server() -> None:
    rec = {"kind": "server", "pid": os.getpid(), "argv": sys.argv[:6], "targets": len(_targets)}
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        inf = resource.RLIM_INFINITY
        rec["nofile"] = ["unlimited" if soft == inf else soft, "unlimited" if hard == inf else hard]
    except Exception:
        rec["nofile"] = None
    _write("server", rec)


def install() -> None:
    global _STDLIB_PREFIXES
    if not _DIR or not os.path.isdir(_DIR):
        return
    try:
        _STDLIB_PREFIXES = _stdlib_prefixes()
        _install_spawn_log()
        _install_compression()
        _write(
            "procs",
            {
                "kind": "start",
                "pid": os.getpid(),
                "argv": sys.argv[:6] if hasattr(sys, "argv") else [],
            },
        )
    except Exception as exc:  # never break the host interpreter
        _write("errors", {"error": repr(exc)})


install()
