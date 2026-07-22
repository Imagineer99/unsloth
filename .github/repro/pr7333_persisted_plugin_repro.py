#!/usr/bin/env python3
"""Reproduce PR 7333's persisted Claude plugin upgrade regression."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unsloth_cli.commands.start import write_claude_subagent_plugin


MODEL_ID = "unsloth/test-model-GGUF:Q4_K_M"
PLUGIN_NAME = "unsloth-local-agent"


def _seed_legacy_layout(root: Path) -> Path:
    """Create the stable plugin layout emitted before PR 7333."""
    plugin = root / PLUGIN_NAME
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": PLUGIN_NAME, "version": "1.0.0"}),
        encoding="utf-8",
    )
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unsloth": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": ["-m", "unsloth_cli.claude_subagent_mcp"],
                        "env": {
                            "UNSLOTH_CLAUDE_SUBAGENT_BASE_URL": "http://127.0.0.1:9",
                            "UNSLOTH_CLAUDE_SUBAGENT_API_KEY": "ci-placeholder",
                            "UNSLOTH_CLAUDE_SUBAGENT_MODEL": MODEL_ID,
                            "UNSLOTH_CLAUDE_SUBAGENT_BYPASS_PERMISSIONS": "0",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    skill = plugin / "skills" / "local-agent" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\ndescription: Delegate to the legacy local agent.\n---\n",
        encoding="utf-8",
    )
    return plugin


def _claude_binary() -> Path:
    override = os.environ.get("CLAUDE_CODE_BIN")
    if override:
        binary = Path(override)
        if binary.is_file():
            return binary
        raise AssertionError(f"CLAUDE_CODE_BIN does not exist: {binary}")
    npm_root = subprocess.check_output(["npm", "root", "--global"], text=True).strip()
    binary = Path(npm_root) / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if not binary.is_file():
        raise AssertionError(f"Claude Code binary not found at {binary}")
    return binary


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _probe_claude(plugin: Path, debug_log: Path) -> str:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.update(
        {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            "ANTHROPIC_AUTH_TOKEN": "ci-placeholder",
            "ANTHROPIC_MODEL": MODEL_ID,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        [
            str(_claude_binary()),
            "--model",
            MODEL_ID,
            "--plugin-dir",
            str(plugin),
            "--print",
            "--no-session-persistence",
            "--debug-file",
            str(debug_log),
            "Reply with OK",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        **kwargs,
    )
    try:
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            if debug_log.exists():
                text = debug_log.read_text(encoding="utf-8", errors="replace")
                if "unsloth_cli.claude_subagent_mcp" in text and "Connection failed" in text:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.25)
        return text
    finally:
        _stop_process(process)


def fresh_control() -> None:
    with tempfile.TemporaryDirectory(prefix="pr7333-fresh-") as raw:
        root = Path(raw)
        plugin = write_claude_subagent_plugin(root, {"id": MODEL_ID})
        assert (plugin / "agents" / "local-subagent.md").is_file()
        assert not (plugin / ".mcp.json").exists()
        assert not (plugin / "skills" / "local-agent" / "SKILL.md").exists()
    print("PASS fresh-control: native agent only")


def upgrade_repro() -> None:
    with tempfile.TemporaryDirectory(prefix="pr7333-upgrade-") as raw:
        root = Path(raw)
        plugin = _seed_legacy_layout(root)
        write_claude_subagent_plugin(root, {"id": MODEL_ID})

        retained = [
            relative
            for relative in (Path(".mcp.json"), Path("skills/local-agent/SKILL.md"))
            if (plugin / relative).exists()
        ]
        assert retained, "Bug did not reproduce: legacy plugin files were migrated"

        debug_log = root / "claude-debug.log"
        debug = _probe_claude(plugin, debug_log)
        assert "Loaded 1 agents from plugin unsloth-local-agent" in debug
        assert "unsloth_cli.claude_subagent_mcp" in debug
        assert "Connection failed" in debug
        print("REPRODUCED retained files:", ", ".join(map(str, retained)))
        print("REPRODUCED Claude loaded the native agent and started the stale MCP server")
        raise AssertionError(
            "PR 7333 upgrade regression reproduced: stale .mcp.json starts the deleted bridge"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("fresh", "upgrade"))
    args = parser.parse_args()
    fresh_control() if args.mode == "fresh" else upgrade_repro()


if __name__ == "__main__":
    main()
