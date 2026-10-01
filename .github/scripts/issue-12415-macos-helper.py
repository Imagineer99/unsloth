#!/usr/bin/env python3
"""Disposable #12415 runner helper: real routes, isolated cache, blocked remote sockets.

The route host replaces Studio login with a test owner. It is not the Desktop app.
Only network transport is blocked; discovery and cache authorization remain real.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if os.environ.get("ISSUE_12415_INSTALLED_BACKEND") == "1":
    distribution = importlib.metadata.distribution("unsloth")
    BACKEND = Path(distribution.locate_file("studio/backend")).resolve()
    assert BACKEND.is_dir(), "Installed Studio backend is missing"
    assert not BACKEND.is_relative_to(
        (ROOT / "studio/backend").resolve()
    ), "Release probe must not import checkout backend"
else:
    BACKEND = ROOT / "studio" / "backend"
sys.path.insert(0, str(BACKEND))
TOKEN = "hf_issue12415_synthetic_runner_credential"


def provenance(output: Path):
    from hub.services.models import gguf_variants
    from utils._studio_release_build import STUDIO_RELEASE_VERSION

    version = importlib.metadata.version("unsloth")
    expected = os.environ.get("ISSUE_12415_BACKEND_VERSION")
    if expected:
        assert version == expected, (version, expected)
        assert STUDIO_RELEASE_VERSION == os.environ["ISSUE_12415_RELEASE_TAG"]
    module = Path(gguf_variants.__file__).resolve()
    assert module.is_relative_to(BACKEND.resolve()), module
    modules = {}
    if os.environ.get("ISSUE_12415_VERIFY_FIX") == "1":
        from hub.services.models import account_access
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd = ROOT, text = True).strip()
        for source in (gguf_variants, account_access):
            path = Path(source.__file__).resolve()
            assert path.is_relative_to((ROOT / "studio/backend").resolve()), path
            relative = path.relative_to(ROOT).as_posix()
            committed = subprocess.check_output(["git", "show", f"{head}:{relative}"], cwd = ROOT)
            assert path.read_bytes() == committed, f"Module differs from checkout: {relative}"
            modules[relative] = hashlib.sha256(committed).hexdigest()
    output.write_text(
        json.dumps(
            {
                "unsloth_version": version,
                "studio_release_version": STUDIO_RELEASE_VERSION,
                "backend_root": str(BACKEND),
                "gguf_variants_path": str(module),
                "gguf_variants_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "checkout_module_hashes": modules,
            },
            indent = 2,
        )
        + "\n"
    )
    print(f"PASS backend provenance: unsloth=={version}, {STUDIO_RELEASE_VERSION}", flush = True)


def prepare(output: Path):
    from huggingface_hub import HfApi, hf_hub_download
    from hub.utils import download_manifest
    from utils.hf_cache_settings import get_hf_cache_paths

    repo = "unsloth/Qwen3-0.6B-GGUF"
    info = HfApi(token = False).model_info(repo, files_metadata = True)
    candidates = sorted(
        (
            s.rfilename
            for s in info.siblings
            if s.rfilename.lower().endswith(".gguf")
            and ("q4_k_m" in s.rfilename.lower() or "ud-q4_k_xl" in s.rfilename.lower())
            and "-of-" not in s.rfilename
            and "mmproj" not in s.rfilename.lower()
        ),
        key = lambda name: ("q4_k_m" not in name.lower(), name),
    )
    if not candidates:
        raise RuntimeError("No one-file Q4 quant found in the public small-model repo")
    filename = candidates[0]
    cache = get_hf_cache_paths().hub_cache
    path = Path(hf_hub_download(repo, filename, revision = info.sha, token = False, cache_dir = cache))
    refs = path.parent.parent.parent / "refs"
    refs.mkdir(exist_ok = True)
    (refs / "main").write_text(info.sha)
    from hub.utils.gguf import extract_quant_label

    quant = extract_quant_label(filename)
    assert quant and path.open("rb").read(4) == b"GGUF"
    assert download_manifest.write_manifest(
        "model",
        repo,
        quant,
        [download_manifest.ExpectedFile(filename, path.stat().st_size)],
        hub_cache = cache,
    )
    data = {
        "repo": repo,
        "revision": info.sha,
        "filename": filename,
        "quant": quant,
        "path": str(path),
        "snapshot": str(path.parent),
        "bytes": path.stat().st_size,
    }
    output.write_text(json.dumps(data, indent = 2) + "\n")
    print(
        f"PASS downloaded complete real GGUF: {repo} {filename} ({data['bytes']} bytes)", flush = True
    )


def serve(metadata: Path, attempts: Path, port: int):
    from fastapi import FastAPI, Header
    import httpx
    import uvicorn
    from auth.authentication import get_current_subject, authenticated_via_api_key
    from hub.dependencies import get_request_hf_token
    from hub.routes import inventory
    from routes import models
    from hub.utils import hf_tokens, inventory_scan, download_manifest
    from utils.hf_cache_settings import get_hf_cache_paths

    data = json.loads(metadata.read_text())
    state = {"blocked": False}

    def record(
        kind,
        host,
        path = "",
    ):
        with attempts.open("a") as stream:
            stream.write(
                json.dumps({"kind": kind, "host": host, "path": path, "blocked": state["blocked"]})
                + "\n"
            )

    def loopback(host):
        if str(host).lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
        except ValueError:
            return False

    real_lookup = socket.getaddrinfo
    real_connect = socket.socket.connect

    def lookup(host, port, *args, **kwargs):
        if state["blocked"] and not loopback(host):
            record("dns-blocked", str(host))
            raise socket.gaierror(socket.EAI_AGAIN, "issue12415 simulated offline transport")
        return real_lookup(host, port, *args, **kwargs)

    def connect(sock, address):
        if state["blocked"] and isinstance(address, tuple) and not loopback(address[0]):
            record("connect-blocked", str(address[0]))
            raise OSError("issue12415 simulated offline transport")
        return real_connect(sock, address)

    socket.getaddrinfo = lookup
    socket.socket.connect = connect
    real_send = httpx.Client.send

    def send(client, request, *args, **kwargs):
        url = urlsplit(str(request.url))
        if not loopback(url.hostname):
            # Omit query strings, headers, tokens and bodies from evidence.
            record("http", url.hostname, url.path)
        return real_send(client, request, *args, **kwargs)

    httpx.Client.send = send
    # Older installed hub clients use requests; runner installs use httpx.
    try:
        import requests
    except ImportError:
        pass
    else:
        real_requests_send = requests.Session.send

        def requests_send(session, request, *args, **kwargs):
            url = urlsplit(str(request.url))
            if not loopback(url.hostname):
                record("http", url.hostname, url.path)
            return real_requests_send(session, request, *args, **kwargs)

        requests.Session.send = requests_send

    app = FastAPI()
    app.include_router(inventory.router, prefix = "/api/hub")
    app.include_router(models.router, prefix = "/api/models")
    app.dependency_overrides[get_current_subject] = lambda: "runner-owner"
    app.dependency_overrides[authenticated_via_api_key] = lambda: False

    def token_header(hf_token: str | None = Header(None, alias = "X-Unsloth-HF-Token")):
        return hf_token

    app.dependency_overrides[get_request_hf_token] = token_header

    @app.get("/")
    def index():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<title>Issue 12415 macOS request probe</title><h1>Offline GGUF discovery probe</h1>"
        )

    @app.post("/repro/state")
    def set_state(blocked: bool, incomplete: bool = False):
        state["blocked"] = blocked
        hf_tokens.reset_repo_access_cache()
        inventory_scan.invalidate_hf_cache_scans()
        files = [download_manifest.ExpectedFile(data["filename"], data["bytes"])]
        if incomplete:
            files.append(download_manifest.ExpectedFile("mmproj-F16.gguf", 128))
        assert download_manifest.write_manifest(
            "model", data["repo"], data["quant"], files, hub_cache = get_hf_cache_paths().hub_cache
        )
        return state

    uvicorn.run(app, host = "127.0.0.1", port = port, log_level = "warning")


def local_inference(metadata: Path, output: Path):
    data = json.loads(metadata.read_text())
    from core.inference.llama_cpp import LlamaCppBackend

    binary = LlamaCppBackend._find_llama_server_binary()
    if not binary:
        raise RuntimeError("Installed llama-server binary was not found")
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    command = [
        str(binary),
        "-m",
        data["path"],
        "-ngl",
        "0",
        "-c",
        "512",
        "-t",
        "3",
        "--no-warmup",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    env = dict(os.environ, HF_HUB_OFFLINE = "1", TRANSFORMERS_OFFLINE = "1")
    started = time.monotonic()
    import httpx

    with output.with_suffix(".log").open("w") as log:
        process = subprocess.Popen(command, env = env, stdout = log, stderr = subprocess.STDOUT)
        try:
            with httpx.Client(base_url = f"http://127.0.0.1:{port}", timeout = 2) as client:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError(f"llama-server exited {process.returncode}")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    raise TimeoutError("Local model startup exceeded 60 seconds")
                response = client.post(
                    "/completion",
                    json = {"prompt": "The capital of France is", "n_predict": 8, "temperature": 0},
                    timeout = 60,
                )
                response.raise_for_status()
                result = response.json()
                assert result.get("tokens_predicted", 0) > 0
                result.update(
                    {
                        "binary": str(binary),
                        "cpu_only": True,
                        "hf_offline_flags": True,
                        "host_network_disabled": False,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
                output.write_text(json.dumps(result, indent = 2) + "\n")
                print("PASS local cached-file CPU inference produced tokens", flush = True)
        finally:
            process.terminate()
            try:
                process.wait(timeout = 10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout = 5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices = ["provenance", "prepare", "serve", "local"])
    parser.add_argument("--metadata", type = Path, required = True)
    parser.add_argument("--attempts", type = Path)
    parser.add_argument("--port", type = int)
    parser.add_argument("--output", type = Path)
    args = parser.parse_args()
    if args.mode == "provenance":
        provenance(args.output)
    elif args.mode == "prepare":
        prepare(args.metadata)
    elif args.mode == "serve":
        serve(args.metadata, args.attempts, args.port)
    else:
        local_inference(args.metadata, args.output)
