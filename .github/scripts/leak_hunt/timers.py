#!/usr/bin/env python3
"""Enumerate the time-valued constants and loop sleeps the leak harness may compress.

Output schema (consumed by the accel import hook, keep it stable):
  {"constants": [{"module", "file", "line", "name", "value", "unit": "s"|"ms", "scope": "module"|<Class>,
                  "spawn_related"}],
   "loop_sleeps": [{"module", "file", "line", "function", "call", "arg", "value", "unit", "in_loop",
                    "spawn_related"}],
   "timeouts": [same fields as constants]}
`module` is the dotted import name (studio/backend on sys.path: core.inference.x; unsloth.x; unsloth_zoo.x).
"""

from __future__ import annotations

import argparse
import ast
import json
import operator
import re
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import static_audit as sa  # noqa: E402

TIMER_NAME = re.compile(
    r"TTL|INTERVAL|PERIOD|POLL|REFRESH|GRACE|BACKOFF|STALE|EVERY|HEARTBEAT|DELAY|_SECONDS|_SECS|_S$|_MS$",
    re.I,
)
EXCLUDE_NAME = re.compile(
    r"BYTES|SIZE|MAX_ENTRIES|COUNT|LIMIT|LEN|TOKEN|LINK|PROOF|JWT|AUTH|SESSION|PASSWORD|COOKIE|TIMEOUT",
    re.I,
)
TIMEOUT_NAME = re.compile(r"TIMEOUT", re.I)
MS_NAME = re.compile(r"(^|_)MS($|_)|MILLIS", re.I)
SLEEP_CALLS = {"time.sleep", "asyncio.sleep"}
WAIT_ATTRS = {"wait"}

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def const_eval(node, env):
    """Numeric value of a constant expression (literals, + - * / // ** %, names already in env), else None."""
    if isinstance(node, ast.Constant):
        v = node.value
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = const_eval(node.operand, env)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = const_eval(node.left, env), const_eval(node.right, env)
        if a is None or b is None:
            return None
        try:
            return _OPS[type(node.op)](a, b)
        except (ArithmeticError, ValueError, OverflowError):
            return None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("float", "int")
        and len(node.args) == 1
    ):
        return const_eval(node.args[0], env)
    return None


def _assignments(body):
    """(target name, value node, stmt) for simple Name assignments, descending into if/try blocks."""
    for node in body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            yield node.targets[0].id, node.value, node
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            yield node.target.id, node.value, node
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ("body", "orelse", "finalbody"):
                yield from _assignments(getattr(node, sub, []) or [])
            for h in getattr(node, "handlers", []) or []:
                yield from _assignments(h.body)


def reach_spawn(ix: sa.Index):
    """(functions that can reach a spawn call, modules holding a direct spawn site)."""
    au = sa.Auditor.__new__(sa.Auditor)
    au.ix = ix
    spawning, spawn_modules = set(), set()
    for mod in ix.modules.values():
        for fn in mod.funcs.values():
            for node in ix._own_nodes(fn):
                if isinstance(node, ast.Call) and au.spawn_kind(mod, node):
                    spawning.add(fn.qual)
                    spawn_modules.add(mod.name)
                    break
    rev = {}
    for q, outs in ix.edges().items():
        for t in outs:
            rev.setdefault(t, set()).add(q)
    seen = set(spawning)
    dq = deque(spawning)
    while dq:
        q = dq.popleft()
        for p in rev.get(q, ()):
            if p not in seen:
                seen.add(p)
                dq.append(p)
    return seen, spawn_modules


def _referencing_funcs(ix, mod, name, scope):
    out = []
    for fn in mod.funcs.values():
        for n in ix._own_nodes(fn):
            if (
                scope == "module"
                and isinstance(n, ast.Name)
                and n.id == name
                and isinstance(n.ctx, ast.Load)
            ):
                out.append(fn.qual)
                break
            if scope != "module" and isinstance(n, ast.Attribute) and n.attr == name:
                out.append(fn.qual)
                break
    return out


def collect(ix: sa.Index):
    reach, spawn_modules = reach_spawn(ix)
    constants, timeouts, sleeps = [], [], []
    for mod in sorted(ix.modules.values(), key = lambda m: m.name):
        env = {}
        scopes = [("module", mod.tree.body)]
        for node in mod.tree.body:
            if isinstance(node, ast.ClassDef):
                scopes.append((node.name, node.body))
        for scope, body in scopes:
            local_env = dict(env)
            for name, value, stmt in _assignments(body):
                v = const_eval(value, local_env)
                if v is None:
                    continue
                local_env[name] = v
                if scope == "module":
                    env[name] = v
                is_timeout = bool(TIMEOUT_NAME.search(name))
                if not is_timeout and (not TIMER_NAME.search(name) or EXCLUDE_NAME.search(name)):
                    continue
                refs = _referencing_funcs(ix, mod, name, scope)
                row = {
                    "module": mod.name,
                    "file": mod.rel,
                    "line": stmt.lineno,
                    "name": name,
                    "value": v,
                    "unit": "ms" if MS_NAME.search(name) else "s",
                    "scope": scope,
                    "spawn_related": mod.name in spawn_modules or any(r in reach for r in refs),
                }
                (timeouts if is_timeout else constants).append(row)
        for fn in mod.funcs.values():
            loops = [
                n for n in ix._own_nodes(fn) if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
            ]
            in_loop_ids = {id(x) for lp in loops for x in ast.walk(lp)}
            for n in ix._own_nodes(fn):
                if not isinstance(n, ast.Call):
                    continue
                canon = ix.canonical(mod, n.func) or ""
                last = canon.split(".")[-1]
                arg = None
                if canon in SLEEP_CALLS or (
                    last == "sleep" and canon.split(".")[0] in ("time", "asyncio")
                ):
                    arg = n.args[0] if n.args else None
                elif last in WAIT_ATTRS and isinstance(n.func, ast.Attribute):
                    arg = n.args[0] if n.args else sa._kw(n, "timeout")
                elif last == "Timer" and (canon.startswith("threading") or canon == "Timer"):
                    arg = n.args[0] if n.args else sa._kw(n, "interval")
                    if arg is None:
                        continue
                    sleeps.append(
                        _sleep_row(mod, fn, n, canon, arg, env, id(n) in in_loop_ids, reach)
                    )
                    continue
                else:
                    continue
                if arg is None or id(n) not in in_loop_ids:
                    continue
                sleeps.append(_sleep_row(mod, fn, n, canon, arg, env, True, reach))
    key = lambda r: (r["module"], r["line"])
    return {
        "constants": sorted(constants, key = key),
        "loop_sleeps": sorted(sleeps, key = key),
        "timeouts": sorted(timeouts, key = key),
    }


def _sleep_row(mod, fn, n, canon, arg, env, in_loop, reach):
    return {
        "module": mod.name,
        "file": mod.rel,
        "line": n.lineno,
        "function": fn.qual,
        "call": canon,
        "arg": ast.unparse(arg),
        "value": const_eval(arg, env),
        "unit": "s",
        "in_loop": in_loop,
        "spawn_related": fn.qual in reach,
    }


def main(argv = None):
    ap = argparse.ArgumentParser(
        description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--roots", nargs = "*", help = "PATH[::IMPORT_BASE] (default: static_audit defaults)"
    )
    ap.add_argument("--out", default = str(sa.OUT_DIR / "timers.json"))
    a = ap.parse_args(argv)
    roots = sa.parse_roots(a.roots) if a.roots else sa.default_roots()
    ix = sa.build_index(roots)
    data = collect(ix)
    out = Path(a.out)
    out.parent.mkdir(parents = True, exist_ok = True)
    out.write_text(json.dumps(data, indent = 1))
    n_sr = sum(r["spawn_related"] for r in data["constants"])
    print(
        f"constants={len(data['constants'])} (spawn_related={n_sr}) loop_sleeps={len(data['loop_sleeps'])} "
        f"timeouts={len(data['timeouts'])} -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
