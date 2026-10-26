#!/usr/bin/env python3
"""Deterministic AST inventory of every process spawn site, classified for leak risk.

Pure stdlib. Each root is `PATH[::IMPORT_BASE]`: module names are relative to IMPORT_BASE
(default: PATH's parent), so `studio/backend` maps to `core.inference.x` and `unsloth/` to
`unsloth.x`. Risk tiers:
  HIGH   = periodic_reachable AND (timeout_kills_only_leader OR not reaped)
  MEDIUM = timeout_kills_only_leader OR escapes without a known reaper
  LOW    = everything else
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

WS = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
OUT_DIR = WS / "outputs" / "leak_hunt" / "static"

# canonical callee -> kind family
SPAWN_CALLS = {
    "subprocess.Popen": "popen",
    "subprocess.run": "run",
    "subprocess.call": "run",
    "subprocess.check_call": "run",
    "subprocess.check_output": "run",
    "subprocess.getoutput": "run",
    "subprocess.getstatusoutput": "run",
    "asyncio.create_subprocess_exec": "async_popen",
    "asyncio.create_subprocess_shell": "async_popen",
    "os.fork": "fork",
    "os.forkpty": "fork",
    "pty.fork": "fork",
    "os.posix_spawn": "fork",
    "os.posix_spawnp": "fork",
    "os.system": "run",
    "os.popen": "os_popen",
    "pty.spawn": "run",
    "multiprocessing.Process": "mp_process",
    "multiprocessing.Pool": "mp_pool",
    "multiprocessing.pool.Pool": "mp_pool",
    "concurrent.futures.ProcessPoolExecutor": "mp_pool",
}
SPAWN_PREFIXES = {"os.spawn": "run"}  # os.spawnl/spawnv/... (P_WAIT or P_NOWAIT, conservative)
# get_context(...).Process / ctx.Process
MP_ATTRS = {"Process": "mp_process", "Pool": "mp_pool"}
NEW_GROUP_FLAGS = ("CREATE_NEW_PROCESS_GROUP", "CREATE_NEW_CONSOLE", "DETACHED_PROCESS")
GROUP_KILL_NAMES = ("killpg", "TerminateJobObject", "AssignProcessToJobObject")
GROUP_KILL_HELPER = re.compile(
    r"kill_(process_)?tree|kill_?group|terminate_tree|killpg|kill_children|tree_kill", re.I
)
PERIODIC_CALLS = {
    "threading.Thread": "thread",
    "threading.Timer": "timer",
    "asyncio.create_task": "task",
    "asyncio.ensure_future": "task",
}
PERIODIC_ATTRS = {
    "create_task": "task",
    "ensure_future": "task",
    "call_later": "call_later",
    "call_at": "call_later",
    "add_job": "scheduler",
}
SLEEP_ATTRS = {"sleep", "wait", "wait_for"}
# Studio's process_lifetime framework: pdeathsig / job object / shutdown terminate_all registry.
LIFETIME_HELPERS = {
    "spawn_on_lifetime_thread",
    "child_popen_kwargs",
    "adopt_pid",
    "terminate_pid",
    "terminate_descendants",
    "compose_preexec",
}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Call):
        inner = _dotted(node.func)
        if inner is not None:
            parts.append(inner + "()")
            return ".".join(reversed(parts))
    return None


def _src(
    lines,
    node,
    limit = 140,
):
    try:
        text = lines[node.lineno - 1].strip()
    except (IndexError, AttributeError):
        return ""
    return f"L{node.lineno}: {text[:limit]}"


@dataclass
class Func:
    qual: str  # module.Class.func or module.outer.inner
    module: str
    cls: str | None
    node: ast.AST
    file: str
    nested: dict = field(default_factory = dict)  # local name -> qual
    calls: list = field(default_factory = list)  # (node, [quals])
    roots: list = field(default_factory = list)  # (kind, evidence, [target quals])
    has_sleep_loop: str | None = None


@dataclass
class Module:
    name: str
    file: str
    rel: str
    root: str
    tree: ast.Module
    lines: list
    aliases: dict = field(default_factory = dict)  # local name -> dotted target
    funcs: dict = field(default_factory = dict)  # qual -> Func
    classes: set = field(default_factory = set)


def _is_test_path(rel: Path) -> bool:
    parts = rel.parts
    if any(p in ("tests", "test", "__pycache__") for p in parts[:-1]):
        return True
    name = parts[-1]
    return name.startswith("test_") or name == "conftest.py" or name.endswith("_test.py")


def _module_name(path: Path, base: Path) -> str:
    rel = path.relative_to(base).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module: str, is_pkg: bool, level: int, target: str | None) -> str:
    pkg = module.split(".") if is_pkg else module.split(".")[:-1]
    if level > 1:
        pkg = pkg[: len(pkg) - (level - 1)]
    base = ".".join(pkg)
    if target:
        return f"{base}.{target}" if base else target
    return base


class Index:
    def __init__(self):
        self.modules: dict[str, Module] = {}
        self.funcs: dict[str, Func] = {}
        self.by_last: dict[tuple[str, str], list[str]] = {}  # (module, last name) -> quals

    # ---------- loading ----------
    def load_root(self, root: Path, base: Path, label: str, overlays: dict):
        for path in sorted(root.rglob("*.py")):
            rel_root = path.relative_to(root)
            if _is_test_path(rel_root):
                continue
            rel = str(path.relative_to(base))
            src = overlays.get(rel)
            if src is None:
                try:
                    src = path.read_text(encoding = "utf-8", errors = "replace")
                except OSError:
                    continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            name = _module_name(path, base)
            mod = Module(name, str(path), rel, label, tree, src.splitlines())
            self.modules[name] = mod
            self._collect(mod, path.name == "__init__.py")

    def _collect(self, mod: Module, is_pkg: bool):
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.aliases[a.asname] = a.name
                    else:
                        mod.aliases.setdefault(a.name.split(".")[0], a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                base = (
                    _resolve_relative(mod.name, is_pkg, node.level, node.module)
                    if node.level
                    else (node.module or "")
                )
                for a in node.names:
                    if a.name == "*":
                        continue
                    mod.aliases[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
            elif isinstance(node, ast.ClassDef):
                mod.classes.add(node.name)

        def visit(body, prefix, cls, parent):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{prefix}.{node.name}"
                    fn = Func(qual, mod.name, cls, node, mod.rel)
                    mod.funcs[qual] = fn
                    self.funcs[qual] = fn
                    self.by_last.setdefault((mod.name, node.name), []).append(qual)
                    if parent is not None:
                        parent.nested[node.name] = qual
                    visit(node.body, qual, cls, fn)
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}.{node.name}", node.name, None)
                elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                    for sub in ("body", "orelse", "finalbody", "handlers"):
                        seq = getattr(node, sub, None) or []
                        inner = []
                        for s in seq:
                            inner.extend(s.body if isinstance(s, ast.ExceptHandler) else [s])
                        visit(inner, prefix, cls, parent)

        visit(mod.tree.body, mod.name, None, None)

    # ---------- resolution ----------
    def canonical(self, mod: Module, node) -> str | None:
        """Dotted name with the leading alias resolved (sp.Popen -> subprocess.Popen)."""
        d = _dotted(node)
        if d is None:
            return None
        head, _, rest = d.partition(".")
        if head in mod.aliases:
            target = mod.aliases[head]
            return f"{target}.{rest}" if rest else target
        return d

    def resolve(self, mod: Module, fn: Func | None, node) -> list[str]:
        """Function quals a callee/reference expression may name (approximate, by name)."""
        if isinstance(node, ast.Lambda):
            out = []
            for sub in ast.walk(node.body):
                if isinstance(sub, ast.Call):
                    out.extend(self.resolve(mod, fn, sub.func))
            return out
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "partial"
        ):
            return self.resolve(mod, fn, node.args[0]) if node.args else []
        if isinstance(node, ast.Name):
            name = node.id
            f = fn
            while f is not None:
                if name in f.nested:
                    return [f.nested[name]]
                parent_qual = f.qual.rsplit(".", 1)[0]
                f = self.funcs.get(parent_qual)
            q = f"{mod.name}.{name}"
            if q in self.funcs:
                return [q]
            if name in mod.aliases and mod.aliases[name] in self.funcs:
                return [mod.aliases[name]]
            return []
        if isinstance(node, ast.Attribute):
            base = node.value
            if (
                isinstance(base, ast.Name)
                and base.id in ("self", "cls")
                and fn is not None
                and fn.cls
            ):
                prefix = fn.qual
                while prefix and not prefix.endswith("." + fn.cls):
                    prefix = prefix.rsplit(".", 1)[0] if "." in prefix else ""
                q = f"{prefix}.{node.attr}"
                return [q] if q in self.funcs else []
            canon = self.canonical(mod, node)
            if canon and canon in self.funcs:
                return [canon]
            if isinstance(base, ast.Name) and base.id in mod.classes:
                q = f"{mod.name}.{base.id}.{node.attr}"
                return [q] if q in self.funcs else []
            if isinstance(base, ast.Name) and base.id in mod.aliases:
                return []  # an imported module/name whose attribute is not in the scanned roots
            # obj.method() on an unknown object: a same-module method of that name, if unambiguous
            cands = [q for q in self.by_last.get((mod.name, node.attr), []) if self.funcs[q].cls]
            return list(cands) if 0 < len(cands) <= 2 else []
        return []

    # ---------- per-function facts ----------
    def analyse(self):
        for mod in self.modules.values():
            for fn in mod.funcs.values():
                self._analyse_func(mod, fn)

    def _own_nodes(self, fn: Func):
        """Nodes of fn's body, not descending into nested defs/classes."""
        stack = list(fn.node.body)
        while stack:
            node = stack.pop()
            yield node
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                stack.append(child)

    def _analyse_func(self, mod: Module, fn: Func):
        for node in self._own_nodes(fn):
            if isinstance(node, ast.Call):
                fn.calls.append((node, self.resolve(mod, fn, node.func)))
                canon = self.canonical(mod, node.func) or ""
                kind = PERIODIC_CALLS.get(canon)
                attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
                if (
                    kind is None
                    and canon.endswith(("Thread", "Timer"))
                    and canon.split(".")[-1] in ("Thread", "Timer")
                ):
                    kind = canon.split(".")[-1].lower()
                if kind is None and attr in PERIODIC_ATTRS:
                    kind = PERIODIC_ATTRS[attr]
                if kind:
                    targets = []
                    if kind == "thread":
                        for kw in node.keywords:
                            if kw.arg == "target":
                                targets = self.resolve(mod, fn, kw.value)
                    elif kind == "timer":
                        if len(node.args) >= 2:
                            targets = self.resolve(mod, fn, node.args[1])
                        for kw in node.keywords:
                            if kw.arg == "function":
                                targets = self.resolve(mod, fn, kw.value)
                    elif kind == "call_later":
                        if len(node.args) >= 2:
                            targets = self.resolve(mod, fn, node.args[1])
                    else:  # task / scheduler: first positional
                        if node.args:
                            a = node.args[0]
                            targets = self.resolve(
                                mod, fn, a.func if isinstance(a, ast.Call) else a
                            )
                    if targets:
                        fn.roots.append((kind, f"{mod.rel}:{node.lineno}", targets))
            if isinstance(node, ast.While) and fn.has_sleep_loop is None:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        c = self.canonical(mod, sub.func) or ""
                        last = c.split(".")[-1]
                        if last in SLEEP_ATTRS and ("." in c or c in ("sleep",)):
                            fn.has_sleep_loop = f"{mod.rel}:{node.lineno}"
                            break

    # ---------- graph ----------
    def edges(self):
        g = {}
        for q, fn in self.funcs.items():
            outs = set()
            for _node, targets in fn.calls:
                outs.update(targets)
            for _kind, _ev, targets in fn.roots:
                outs.update(targets)
            g[q] = outs
        return g

    def periodic_chains(self):
        """qual -> chain (list) from the nearest periodic root."""
        g = self.edges()
        chains = {}
        dq = deque()
        for q, fn in sorted(self.funcs.items()):
            for kind, ev, targets in fn.roots:
                for t in targets:
                    if t not in chains:
                        chains[t] = [f"{kind}@{ev} (from {q})", t]
                        dq.append(t)
            if fn.has_sleep_loop and q not in chains:
                chains[q] = [f"sleep-loop@{fn.has_sleep_loop}", q]
                dq.append(q)
        while dq:
            q = dq.popleft()
            for t in sorted(g.get(q, ())):
                if t not in chains:
                    chains[t] = chains[q] + [t]
                    dq.append(t)
        return chains


# ---------- spawn classification ----------


def _kw(node, name):
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _truthy(v):
    return not (isinstance(v, ast.Constant) and v.value in (False, None, 0))


class Auditor:
    def __init__(self, index: Index):
        self.ix = index
        self.chains = index.periodic_chains()
        self._closure_cache = {}

    def spawn_kind(self, mod, call) -> tuple[str, str] | None:
        canon = self.ix.canonical(mod, call.func)
        if canon is None:
            return None
        if canon in SPAWN_CALLS:
            return canon, SPAWN_CALLS[canon]
        for pre, kind in SPAWN_PREFIXES.items():
            if canon.startswith(pre) and canon != "os.spawn":
                return canon, kind
        last = canon.split(".")[-1]
        if last in MP_ATTRS and (
            "get_context()" in canon
            or canon.startswith("multiprocessing")
            or canon.split(".")[0] in ("ctx", "mp_ctx", "_ctx", "context")
        ):
            return canon, MP_ATTRS[last]
        if canon.endswith("ProcessPoolExecutor"):
            return canon, "mp_pool"
        return None

    def closure(
        self,
        qual,
        depth = 3,
    ):
        """qual plus functions it calls, up to depth."""
        key = (qual, depth)
        if key in self._closure_cache:
            return self._closure_cache[key]
        seen = {qual}
        frontier = [qual]
        for _ in range(depth):
            nxt = []
            for q in frontier:
                fn = self.ix.funcs.get(q)
                if fn is None:
                    continue
                for _n, targets in fn.calls:
                    for t in targets:
                        if t not in seen:
                            seen.add(t)
                            nxt.append(t)
            frontier = nxt
        self._closure_cache[key] = seen
        return seen

    def _group_kill(self, qual):
        for q in sorted(self.closure(qual, 3)):
            fn = self.ix.funcs.get(q)
            if fn is None:
                continue
            mod = self.ix.modules[fn.module]
            for node in self.ix._own_nodes(fn):
                if isinstance(node, ast.Call):
                    c = self.ix.canonical(mod, node.func) or ""
                    last = c.split(".")[-1]
                    if last in GROUP_KILL_NAMES or GROUP_KILL_HELPER.search(last):
                        return True, f"{fn.file}:{node.lineno} {last} (in {q})"
                    if last == "children" and "psutil" in ast.unparse(node)[:200] + c:
                        return True, f"{fn.file}:{node.lineno} psutil children (in {q})"
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value == "/T"
                ):
                    return True, f"{fn.file}:{node.lineno} taskkill /T (in {q})"
        return False, ""

    def _preexec_facts(self, mod, fn, value):
        """(new_group evidence, rlimit_cpu evidence) from a preexec_fn expression."""
        quals = self.ix.resolve(mod, fn, value)
        ng, rl = "", ""
        for q in quals:
            for qq in sorted(self.closure(q, 3)):
                f2 = self.ix.funcs.get(qq)
                if f2 is None:
                    continue
                m2 = self.ix.modules[f2.module]
                for node in self.ix._own_nodes(f2):
                    if isinstance(node, ast.Call):
                        c = self.ix.canonical(m2, node.func) or ""
                        if c.split(".")[-1] in ("setsid", "setpgrp", "setpgid") and not ng:
                            ng = f"{f2.file}:{node.lineno} {c} (preexec {qq})"
                    if isinstance(node, ast.Attribute) and node.attr == "RLIMIT_CPU" and not rl:
                        rl = f"{f2.file}:{node.lineno} RLIMIT_CPU (preexec {qq})"
        if isinstance(value, ast.Attribute) and value.attr in ("setsid", "setpgrp"):
            ng = ng or f"{mod.rel}:{value.lineno} preexec_fn={ast.unparse(value)}"
        return ng, rl

    def _splat_facts(self, mod, fn, call):
        """Keys a **splat of a helper call may carry (child_popen_kwargs() etc.)."""
        keys = {}
        for kw in call.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Call):
                for q in self.ix.resolve(mod, fn, kw.value.func):
                    f2 = self.ix.funcs.get(q)
                    if f2 is None:
                        continue
                    for node in ast.walk(f2.node):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str):
                            keys.setdefault(node.value, f"{f2.file}:{node.lineno} (via **{q})")
                        if isinstance(node, ast.keyword) and node.arg:
                            keys.setdefault(node.arg, f"{f2.file}:{node.value.lineno} (via **{q})")
                        if isinstance(node, ast.Name) and node.id in NEW_GROUP_FLAGS:
                            keys.setdefault(node.id, f"{f2.file}:{node.lineno} (via **{q})")
                        if isinstance(node, ast.Attribute) and node.attr in NEW_GROUP_FLAGS:
                            keys.setdefault(node.attr, f"{f2.file}:{node.lineno} (via **{q})")
        return keys

    def _bound_name(self, fn, call):
        """How the spawn result is held: ('var', name) | ('attr', name) | ('return',) | ('with', name) | ('lambda',) | None."""
        parents = {}
        for node in ast.walk(fn.node):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        cur = call
        via_lambda = False
        while True:
            par = parents.get(id(cur))
            if par is None:
                return ("lambda",) if via_lambda else None
            if isinstance(par, ast.Lambda):
                # spawn_on_lifetime_thread(lambda: Popen(...)): follow the call the lambda is handed to
                via_lambda = True
                cur = par
                continue
            if via_lambda and isinstance(par, ast.Call) and cur in par.args:
                via_lambda = False
                cur = par
                continue
            if isinstance(par, ast.Await):
                cur = par
                continue
            if isinstance(par, (ast.Assign, ast.AnnAssign)):
                tgts = par.targets if isinstance(par, ast.Assign) else [par.target]
                for t in tgts:
                    if isinstance(t, ast.Name):
                        return ("var", t.id)
                    if isinstance(t, ast.Attribute):
                        return ("attr", t.attr)
                    if isinstance(t, ast.Subscript):
                        return ("attr", ast.unparse(t.value).split(".")[-1])
                return None
            if isinstance(par, ast.withitem):
                v = par.optional_vars
                return ("with", v.id if isinstance(v, ast.Name) else "")
            if isinstance(par, ast.Return):
                return ("return",)
            if isinstance(par, ast.Call):
                fname = (
                    par.func.attr
                    if isinstance(par.func, ast.Attribute)
                    else (par.func.id if isinstance(par.func, ast.Name) else "")
                )
                if fname in ("append", "add", "setdefault", "put"):
                    return ("attr", ast.unparse(par.func.value).split(".")[-1])
                return ("arg", fname)
            if isinstance(par, (ast.stmt,)):
                return ("discard",)
            cur = par

    def _method_on(
        self,
        fn,
        var,
        methods,
        attr = False,
    ):
        """Evidence of var.method(...) in fn (var a local or self.<attr>)."""
        for node in ast.walk(fn.node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ):
                base = node.func.value
                if (not attr and isinstance(base, ast.Name) and base.id == var) or (
                    attr and isinstance(base, ast.Attribute) and base.attr == var
                ):
                    return node
        return None

    def _passes_var_to_reaper(self, mod, fn, var, methods):
        """var handed to a helper whose body waits/kills on its parameter (e.g. _end_probe_group(proc))."""
        for node in ast.walk(fn.node):
            if not isinstance(node, ast.Call):
                continue
            if not any(isinstance(a, ast.Name) and a.id == var for a in node.args):
                continue
            for q in self.ix.resolve(mod, fn, node.func):
                f2 = self.ix.funcs.get(q)
                if f2 is None:
                    continue
                for sub in ast.walk(f2.node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in methods
                    ):
                        return f"{f2.file}:{sub.lineno} {sub.func.attr} (helper {q})"
        return ""

    def _attr_reaper_in_module(self, mod, attr, methods):
        for fn in mod.funcs.values():
            n = self._method_on(fn, attr, methods, attr = True)
            if n is not None:
                return f"{mod.rel}:{n.lineno} .{attr}.{n.func.attr}() (in {fn.qual})"
        # `proc, self._proc = self._proc, None; proc.wait()`: a function reading the attribute and waiting.
        for fn in mod.funcs.values():
            reads = any(
                isinstance(n, ast.Attribute) and n.attr == attr and isinstance(n.ctx, ast.Load)
                for n in ast.walk(fn.node)
            )
            if not reads:
                continue
            for n in ast.walk(fn.node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in methods
                ):
                    return (
                        f"{mod.rel}:{n.lineno} reads .{attr}, then .{n.func.attr}() (in {fn.qual})"
                    )
        return ""

    def classify(
        self,
        mod,
        fn,
        call,
        canon,
        kind,
        wrapper = None,
        inherited = None,
    ):
        site = {
            "root": mod.root,
            "file": mod.rel,
            "line": call.lineno,
            "module": mod.name,
            "function": fn.qual if fn else f"{mod.name}.<module>",
            "class": fn.cls if fn else None,
            "call": canon,
            "kind": kind,
            "wrapper": wrapper,
            "kwargs": sorted(k.arg for k in call.keywords if k.arg),
            "has_splat": any(k.arg is None for k in call.keywords),
            "source": _src(mod.lines, call),
            "returns_handle": False,
        }
        flags = {}
        splat = self._splat_facts(mod, fn, call) if fn else {}
        # has_timeout
        t = _kw(call, "timeout")
        ev = ""
        if t is not None and _truthy(t):
            ev = _src(mod.lines, call)
        bound = self._bound_name(fn, call) if fn else None
        if not ev and fn and bound and bound[0] in ("var", "with"):
            for node in ast.walk(fn.node):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("communicate", "wait")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == bound[1]
                    and (_kw(node, "timeout") is not None or node.args)
                ):
                    ev = _src(mod.lines, node)
                    break
                if (
                    isinstance(node, ast.Call)
                    and (self.ix.canonical(mod, node.func) or "").endswith("wait_for")
                    and node.args
                    and bound[1] in ast.unparse(node.args[0])
                ):
                    ev = _src(mod.lines, node)
                    break
        if inherited and inherited.get("has_timeout", {}).get("value") and not ev:
            ev = "via wrapper: " + inherited["has_timeout"]["evidence"]
        flags["has_timeout"] = {"value": bool(ev), "evidence": ev}
        # new_group
        ng = ""
        v = _kw(call, "start_new_session")
        if v is not None and _truthy(v):
            ng = _src(mod.lines, call) + " start_new_session"
        if not ng and _kw(call, "process_group") is not None:
            ng = _src(mod.lines, call) + " process_group"
        cf = _kw(call, "creationflags")
        if not ng and cf is not None and any(f in ast.unparse(cf) for f in NEW_GROUP_FLAGS):
            ng = _src(mod.lines, call) + " creationflags"
        rl = ""
        pre = _kw(call, "preexec_fn")
        if pre is not None and fn:
            png, prl = self._preexec_facts(mod, fn, pre)
            ng = ng or png
            rl = prl
        for key in ("start_new_session", "process_group", *NEW_GROUP_FLAGS):
            if not ng and key in splat:
                ng = f"{key} {splat[key]}"
        if inherited:
            ng = ng or inherited.get("new_group", {}).get("evidence", "")
            rl = rl or inherited.get("rlimit_cpu", {}).get("evidence", "")
        flags["new_group"] = {"value": bool(ng), "evidence": ng}
        flags["rlimit_cpu"] = {"value": bool(rl), "evidence": rl}
        # group kill
        gk, gev = self._group_kill(fn.qual) if fn else (False, "")
        if not gk and inherited and inherited.get("group_kill_in_scope", {}).get("value"):
            gk, gev = True, "via wrapper: " + inherited["group_kill_in_scope"]["evidence"]
        flags["group_kill_in_scope"] = {"value": gk, "evidence": gev}
        # reaped / escapes
        reaped, rev, escapes = False, "", False
        wait_methods = {
            "popen": ("wait", "communicate"),
            "async_popen": ("wait", "communicate"),
            "mp_process": ("join",),
            "mp_pool": ("join", "shutdown", "terminate", "close"),
            "os_popen": ("close", "read"),
        }.get(kind, ())
        if kind == "run":
            reaped, rev = True, "blocking call"
        elif kind == "fork":
            for q in sorted(self.closure(fn.qual, 2)) if fn else []:
                f2 = self.ix.funcs.get(q)
                if f2 is None:
                    continue
                m2 = self.ix.modules[f2.module]
                hit = next(
                    (
                        n
                        for n in ast.walk(f2.node)
                        if isinstance(n, ast.Call)
                        and (self.ix.canonical(m2, n.func) or "").startswith("os.wait")
                    ),
                    None,
                )
                if hit is not None:
                    reaped, rev = True, f"{f2.file}:{hit.lineno} os.wait* (in {q})"
                    break
        elif bound is None or bound[0] == "discard":
            rev = "result discarded"
        elif bound[0] == "with":
            reaped, rev = True, "context manager"
        elif bound[0] == "var":
            n = self._method_on(fn, bound[1], wait_methods)
            if n is not None:
                reaped, rev = True, _src(mod.lines, n)
            else:
                helper = self._passes_var_to_reaper(mod, fn, bound[1], wait_methods)
                if helper:
                    reaped, rev = True, helper
                else:
                    # stored or returned later?
                    for node in ast.walk(fn.node):
                        if (
                            isinstance(node, ast.Return)
                            and node.value is not None
                            and any(
                                isinstance(x, ast.Name) and x.id == bound[1]
                                for x in ast.walk(node.value)
                            )
                        ):
                            escapes = True
                            site["returns_handle"] = True
                        if (
                            isinstance(node, ast.Assign)
                            and any(
                                isinstance(x, ast.Name) and x.id == bound[1]
                                for x in ast.walk(node.value)
                            )
                            and any(
                                isinstance(t, (ast.Attribute, ast.Subscript)) for t in node.targets
                            )
                        ):
                            escapes = True
                            tgt = node.targets[0]
                            name = (
                                tgt.attr
                                if isinstance(tgt, ast.Attribute)
                                else ast.unparse(tgt.value).split(".")[-1]
                            )
                            r = self._attr_reaper_in_module(mod, name, wait_methods)
                            if r:
                                reaped, rev = True, r
                    if escapes and not reaped:
                        rev = "stored/returned, no reaper found"
                    elif not escapes:
                        rev = f"no {'/'.join(wait_methods)} on {bound[1]}"
        elif bound[0] == "attr":
            escapes = True
            r = self._attr_reaper_in_module(mod, bound[1], wait_methods)
            reaped, rev = (True, r) if r else (False, f"stored on .{bound[1]}, no reaper found")
        elif bound[0] in ("return", "lambda", "arg"):
            escapes = True
            site["returns_handle"] = bound[0] == "return"
            rev = f"escapes ({bound[0]}{':' + bound[1] if len(bound) > 1 else ''})"
        if inherited and not reaped:
            # wrapper call site: the caller holds the handle
            pass
        lt = ""
        if fn:
            for node in self.ix._own_nodes(fn):
                if isinstance(node, ast.Call):
                    last = (self.ix.canonical(mod, node.func) or "").split(".")[-1]
                    if last.lstrip("_") in LIFETIME_HELPERS:
                        lt = f"{mod.rel}:{node.lineno} {last}"
                        break
        if not lt and inherited:
            lt = inherited.get("lifetime_managed", {}).get("evidence", "")
        flags["lifetime_managed"] = {"value": bool(lt), "evidence": lt}
        flags["reaped"] = {"value": reaped, "evidence": rev}
        flags["escapes"] = {"value": escapes, "evidence": rev if escapes else ""}
        flags["timeout_kills_only_leader"] = {
            "value": flags["has_timeout"]["value"] and not gk,
            "evidence": "timeout with no group kill reachable"
            if flags["has_timeout"]["value"] and not gk
            else "",
        }
        chain = self.chains.get(fn.qual) if fn else None
        flags["periodic_reachable"] = {
            "value": chain is not None,
            "evidence": " -> ".join(chain or []),
        }
        site["flags"] = flags
        site["chain"] = chain or []
        tkol = flags["timeout_kills_only_leader"]["value"]
        # A lifetime-managed long-lived child (server) is expected to outlive the call, and a wrapper that
        # returns its handle is judged at its call sites: MEDIUM at most, not HIGH.
        site["wrapper_definition"] = bool(site["returns_handle"])
        if chain and (tkol or (not reaped and not lt and not site["wrapper_definition"])):
            risk = "HIGH"
        elif tkol or (escapes and not reaped):
            risk = "MEDIUM"
        else:
            risk = "LOW"
        site["risk"] = risk
        return site

    def run(self):
        sites = []
        wrappers = {}  # qual -> site (functions returning a spawned handle)
        for mod in self.ix.modules.values():
            fn_of = {}
            for fn in mod.funcs.values():
                for node in self.ix._own_nodes(fn):
                    fn_of[id(node)] = fn
                # lambdas inside fn belong to fn too
                for node in ast.walk(fn.node):
                    fn_of.setdefault(id(node), fn)
            for node in ast.walk(mod.tree):
                if not isinstance(node, ast.Call):
                    continue
                sk = self.spawn_kind(mod, node)
                if sk is None:
                    continue
                fn = fn_of.get(id(node))
                site = self.classify(mod, fn, node, sk[0], sk[1])
                sites.append(site)
                if (
                    fn
                    and site["returns_handle"]
                    and not site["flags"]["reaped"]["value"]
                    and sk[1] in ("popen", "async_popen", "mp_process")
                ):
                    wrappers.setdefault(fn.qual, site)
        # one level of wrapper call sites
        for mod in self.ix.modules.values():
            for fn in mod.funcs.values():
                for node, targets in fn.calls:
                    for t in targets:
                        if t in wrappers and t != fn.qual:
                            w = wrappers[t]
                            site = self.classify(
                                mod,
                                fn,
                                node,
                                f"wrapper:{t}",
                                w["kind"],
                                wrapper = t,
                                inherited = w["flags"],
                            )
                            sites.append(site)
        sites.sort(
            key = lambda s: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[s["risk"]], s["file"], s["line"])
        )
        return sites


def build_index(roots, overlays = None):
    ix = Index()
    for path, base, label in roots:
        ix.load_root(Path(path), Path(base), label, overlays or {})
    ix.analyse()
    return ix


def default_roots():
    main = WS / "temp" / "leak_hunt_main"
    zoo = WS / "temp" / "leak_hunt_zoo"
    return [
        (main / "unsloth", main, "unsloth"),
        (main / "studio" / "backend", main / "studio" / "backend", "studio_backend"),
        (zoo / "unsloth_zoo", zoo, "unsloth_zoo"),
    ]


def parse_roots(specs):
    out = []
    for spec in specs:
        path, _, base = spec.partition("::")
        p = Path(path).resolve()
        out.append((p, Path(base).resolve() if base else p.parent, p.name))
    return out


def render_md(sites, summary):
    lines = [
        "# Spawn-site leak audit",
        "",
        "## Counts",
        "",
        "| root | HIGH | MEDIUM | LOW | total |",
        "|---|---|---|---|---|",
    ]
    for root, c in sorted(summary["per_root"].items()):
        lines.append(
            f"| {root} | {c.get('HIGH', 0)} | {c.get('MEDIUM', 0)} | {c.get('LOW', 0)} | {sum(c.values())} |"
        )
    t = summary["per_tier"]
    lines.append(
        f"| **all** | {t.get('HIGH', 0)} | {t.get('MEDIUM', 0)} | {t.get('LOW', 0)} | {sum(t.values())} |"
    )
    lines += [
        "",
        "## Sites",
        "",
        "| risk | site | function | call | timeout | group | gkill | reaped | periodic chain |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    yn = lambda f: "Y" if f["value"] else "-"
    for s in sites:
        f = s["flags"]
        chain = " -> ".join(c.split(".")[-1] if i else c for i, c in enumerate(s["chain"]))
        lines.append(
            f"| {s['risk']} | {s['file']}:{s['line']} | {s['function'].split('.', 0)[0]} | {s['call']} | "
            f"{yn(f['has_timeout'])} | {yn(f['new_group'])} | {yn(f['group_kill_in_scope'])} | "
            f"{yn(f['reaped'])}{' (esc)' if f['escapes']['value'] else ''} | {chain[:160]} |"
        )
    return "\n".join(lines) + "\n"


def main(argv = None):
    ap = argparse.ArgumentParser(
        description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--roots",
        nargs = "*",
        help = "PATH[::IMPORT_BASE] (default: main unsloth, studio/backend, unsloth_zoo)",
    )
    ap.add_argument(
        "--overlay",
        action = "append",
        default = [],
        metavar = "REL=FILE",
        help = "replace the content of a base-relative file (e.g. a PR version)",
    )
    ap.add_argument("--out-dir", default = str(OUT_DIR))
    ap.add_argument("--tag", default = "", help = "suffix for output names")
    ap.add_argument("--quiet", action = "store_true")
    a = ap.parse_args(argv)
    roots = parse_roots(a.roots) if a.roots else default_roots()
    overlays = {}
    for ov in a.overlay:
        rel, _, f = ov.partition("=")
        overlays[rel] = Path(f).read_text(encoding = "utf-8")
    ix = build_index(roots, overlays)
    sites = Auditor(ix).run()
    per_root, per_tier = {}, {}
    for s in sites:
        per_root.setdefault(s["root"], {}).setdefault(s["risk"], 0)
        per_root[s["root"]][s["risk"]] += 1
        per_tier[s["risk"]] = per_tier.get(s["risk"], 0) + 1
    summary = {
        "roots": [str(r[0]) for r in roots],
        "modules": len(ix.modules),
        "functions": len(ix.funcs),
        "per_root": per_root,
        "per_tier": per_tier,
    }
    out = Path(a.out_dir)
    out.mkdir(parents = True, exist_ok = True)
    sfx = f"_{a.tag}" if a.tag else ""
    (out / f"spawn_sites{sfx}.json").write_text(
        json.dumps({"summary": summary, "sites": sites}, indent = 1)
    )
    (out / f"spawn_sites{sfx}.md").write_text(render_md(sites, summary))
    if not a.quiet:
        print(json.dumps(summary, indent = 1))
        print(f"wrote {out / f'spawn_sites{sfx}.json'} and .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
