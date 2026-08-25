#!/usr/bin/env python3
"""Real FastMCP A/B probe for PR 9636. Writes a machine-readable report."""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import subprocess
import sys
import types
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.utilities.types import File, Image
from mcp import types as mcp_types


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def load_backend(repo: Path):
    logger_module = types.ModuleType("loggers")
    logger_module.get_logger = logging.getLogger
    sys.modules["loggers"] = logger_module
    path = repo / "studio" / "backend" / "core" / "inference" / "mcp_client.py"
    spec = importlib.util.spec_from_file_location("pr9636_mcp_client", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def embedded(mime="image/png", uri="file:///generated.png"):
    kwargs = {"uri": uri, "blob": PNG_B64}
    if mime is not None:
        kwargs["mimeType"] = mime
    return mcp_types.EmbeddedResource(
        type="resource", resource=mcp_types.BlobResourceContents(**kwargs)
    )


def build_server():
    server = FastMCP("pr9636-ab")

    @server.tool
    def plain_text():
        return "hello"

    @server.tool
    def direct_image():
        return Image(data=PNG_BYTES, format="png")

    @server.tool
    def file_png():
        return File(data=PNG_BYTES, format="png")

    @server.tool
    def file_svg():
        return File(data=b"<svg xmlns='http://www.w3.org/2000/svg'/>", format="svg")

    @server.tool
    def raw_embedded():
        return embedded()

    @server.tool
    def raw_application_png():
        return embedded("application/png")

    @server.tool
    def mixed():
        return ["caption", File(data=PNG_BYTES, format="png"), Image(data=PNG_BYTES, format="png")]

    return server


def summarize(flat: str, marker: str):
    token = "\n" + marker
    if token not in flat:
        return {"text": flat, "images": []}
    text, raw = flat.rsplit(token, 1)
    return {"text": text, "images": json.loads(raw)}


async def collect(backend):
    output = {}
    async with Client(build_server()) as client:
        for name in (
            "plain_text",
            "direct_image",
            "file_png",
            "file_svg",
            "raw_embedded",
            "raw_application_png",
            "mixed",
        ):
            result = await client.call_tool(name, {})
            output[name] = summarize(backend._flatten_result(result), backend.MCP_IMAGES_SENTINEL)

    for name, uri in (
        ("missing_mime_query", "https://example.test/image.png?download=1"),
        ("missing_mime_fragment", "https://example.test/image.png#preview"),
    ):
        block = types.SimpleNamespace(
            type="resource",
            resource=types.SimpleNamespace(uri=uri, mimeType=None, blob=PNG_B64),
        )
        result = types.SimpleNamespace(content=[block], structured_content=None, is_error=False)
        output[name] = summarize(backend._flatten_result(result), backend.MCP_IMAGES_SENTINEL)
    return output


def image_count(cases, name):
    return len(cases[name]["images"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    backend = load_backend(repo)
    cases = asyncio.run(collect(backend))
    branch = os.environ.get("GITHUB_REF_NAME", subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=repo, text=True
    ).strip())

    direct_ok = (
        cases["plain_text"]["text"] == "hello"
        and image_count(cases, "direct_image") == 1
    )
    core_ok = all(
        image_count(cases, name) == expected
        for name, expected in {
            "file_png": 1,
            "file_svg": 1,
            "raw_embedded": 1,
            "raw_application_png": 1,
            "mixed": 2,
        }.items()
    )
    uri_ok = all(image_count(cases, name) == 1 for name in (
        "missing_mime_query", "missing_mime_fragment"
    ))

    failures = []
    if not direct_ok:
        failures.append("legacy direct Text/Image controls changed")
    if branch.endswith("-base"):
        if core_ok:
            failures.append("negative base unexpectedly preserved embedded resources")
    else:
        if not core_ok:
            failures.append("PR core behavior did not preserve embedded resources")
    if branch.endswith("-uri-fixed") and not uri_ok:
        failures.append("URI-fixed branch did not handle query/fragment MIME inference")

    report = {
        "branch": branch,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "fastmcp": importlib.metadata.version("fastmcp"),
        "mcp": importlib.metadata.version("mcp"),
        "direct_controls_ok": direct_ok,
        "embedded_core_ok": core_ok,
        "uri_query_fragment_ok": uri_ok,
        "failures": failures,
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "cases"}, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
