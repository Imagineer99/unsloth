#!/usr/bin/env python3
"""Pinned A/B probe for Unsloth PR #9640.

Loads the real ``tools.py`` from the checked-out ref with only its unrelated
MCP/logging imports stubbed.  No model, network, GPU, or subprocess is used.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _McpDb:
    @staticmethod
    def get_server(_server_id):
        return None

    @staticmethod
    def list_servers():
        return []


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def load_tools(repo: Path):
    backend = repo / "studio" / "backend"
    module_path = backend / "core" / "inference" / "tools.py"
    if not module_path.is_file():
        raise AssertionError(f"missing tools.py at {module_path}")

    core = _module("core")
    core.__path__ = [str(backend / "core")]
    inference = _module("core.inference")
    inference.__path__ = [str(backend / "core" / "inference")]
    _module("core.inference.context_window", _RESULT_NOTICE_RESERVE=0)

    def _false(*_args, **_kwargs):
        return False

    def _none(*_args, **_kwargs):
        return None

    _module(
        "core.inference.mcp_client",
        MCP_TOOL_PREFIX="mcp__",
        TOOL_CACHE_INVALIDATING_FIELDS=frozenset(),
        cache_tools=_none,
        call_tool_sync=_none,
        get_cached_tools=_none,
        in_failure_cooloff=_false,
        is_stdio=_false,
        list_tools_async=_none,
        parse_server_headers=lambda *_args, **_kwargs: {},
        probe_timeout=lambda: 1,
        record_probe_failure=_none,
        stdio_mcp_enabled=lambda: True,
    )
    _module("storage", mcp_servers_db=_McpDb())
    _module("loggers", get_logger=lambda *_args, **_kwargs: _Logger())

    sys.modules.pop("core.inference.tools", None)
    spec = importlib.util.spec_from_file_location("core.inference.tools", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not build tools.py import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def run(mode: str, repo: Path) -> dict:
    tools = load_tools(repo)
    facts: dict[str, object] = {"mode": mode, "ref": repo.name}

    sentinel = ValueError("guard sentinel")
    propagated = None
    try:
        with tools._session_in_flight("plain"):
            raise sentinel
    except ValueError as exc:
        propagated = exc
    if mode == "before":
        assert propagated is None, "negative control no longer reproduces suppression"
    else:
        assert propagated is sentinel, "guard did not preserve exception identity"
    facts["guard_exception"] = "swallowed" if propagated is None else "propagated"
    assert not tools._active_sessions

    tool_impl = {
        "python": "_python_exec",
        "terminal": "_bash_exec",
        "edit_file": "_edit_file",
    }
    observed: dict[str, str] = {}
    tools._fit_result_to_room = lambda value, *_args, **_kwargs: value
    for tool_name, impl_name in tool_impl.items():
        call_error = AttributeError(f"{tool_name} malformed argument")

        def _explode(*_args, _error=call_error, **_kwargs):
            raise _error

        setattr(tools, impl_name, _explode)
        try:
            result = tools.execute_tool(tool_name, {}, session_id=f"ab-{tool_name}")
        except AttributeError as exc:
            assert exc is call_error
            observed[tool_name] = f"raised:{exc}"
        else:
            observed[tool_name] = result
        if mode == "before":
            assert observed[tool_name] == f"Unknown tool: {tool_name}", observed
        else:
            assert observed[tool_name] == f"raised:{call_error}", observed
        assert not tools._active_sessions
    facts["malformed_tools"] = observed

    tools._python_exec = lambda *_args, **_kwargs: "python-ok"
    assert tools.execute_tool("python", {"code": "print(1)"}, session_id="ok") == "python-ok"
    assert tools.execute_tool("no_such_tool", {}) == "Unknown tool: no_such_tool"
    facts["controls"] = {"success": "python-ok", "unknown": "Unknown tool: no_such_tool"}

    removed: list[tuple[str, bool]] = []
    tools._remove_session_sandbox_locked = lambda session_id, files: removed.append((session_id, files))
    tools._thread_exists = lambda *_args, **_kwargs: False
    key = tools._session_key("CleanupCase")
    tools._pending_removals[key] = {"CleanupCase": True}
    cleanup_error = RuntimeError("body failed")
    caught = None
    try:
        with tools._session_in_flight("cleanupcase"):
            raise cleanup_error
    except RuntimeError as exc:
        caught = exc
    assert removed == [("CleanupCase", True)]
    assert not tools._active_sessions
    assert not tools._pending_removals
    assert not tools._removing_sessions
    # The old early return ran only when there was no pending deletion.  A
    # queued cleanup already preserved the body exception, and must stay so.
    assert caught is cleanup_error
    facts["queued_cleanup"] = {"ran_once": True, "exception": "propagated"}

    cleanup_boom = OSError("cleanup failed")
    tools._remove_session_sandbox_locked = lambda *_args, **_kwargs: (_ for _ in ()).throw(cleanup_boom)
    tools._pending_removals[key] = {"CleanupCase": True}

    def _cleanup_failure():
        with tools._session_in_flight("cleanupcase"):
            pass

    assert raises(OSError, _cleanup_failure) is cleanup_boom
    assert not tools._active_sessions
    assert not tools._pending_removals
    assert not tools._removing_sessions
    facts["cleanup_failure_releases_state"] = True

    entered = threading.Event()
    release = threading.Event()
    waiter_entered = threading.Event()

    def _slow_remove(*_args, **_kwargs):
        entered.set()
        assert release.wait(10), "release timeout"

    tools._remove_session_sandbox_locked = _slow_remove
    tools._pending_removals[key] = {"CleanupCase": True}

    def _owner():
        with tools._session_in_flight("CleanupCase"):
            pass

    def _waiter():
        with tools._session_in_flight("cleanupcase"):
            waiter_entered.set()

    owner = threading.Thread(target=_owner, daemon=True)
    owner.start()
    assert entered.wait(10), "cleanup never began"
    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()
    assert not waiter_entered.wait(0.25), "waiter entered during removal"
    release.set()
    assert waiter_entered.wait(10), "waiter was not notified"
    owner.join(10)
    waiter.join(10)
    assert not owner.is_alive() and not waiter.is_alive()
    facts["waiter_notification"] = True

    print(json.dumps(facts, indent=2, sort_keys=True))
    return facts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("before", "after"), required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    run(args.mode, args.repo.resolve())
