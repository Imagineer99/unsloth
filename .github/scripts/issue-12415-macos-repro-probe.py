#!/usr/bin/env python3
"""Run #12415 request shapes through WebKit on macOS, with real cached GGUF/routes.

Does not claim to exercise the packaged Tauri application or Safari itself.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / ".github" / "scripts" / "issue-12415-macos-helper.py"
TOKEN = "hf_issue12415_synthetic_runner_credential"


def environment(artifacts):
    # Keep model blobs outside uploaded artifacts.
    home = artifacts.parent.parent / "state"
    hf_home = home / "hf"
    env = dict(os.environ, HF_HOME=str(hf_home), HF_HUB_CACHE=str(hf_home / "hub"),
               HF_TOKEN_PATH=str(hf_home / "token"), HF_XET_CACHE=str(hf_home / "xet"),
               UNSLOTH_STUDIO_HOME=str(home / "studio"),
               UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE="1", UNSLOTH_ALLOW_CPU="1",
               UNSLOTH_IS_PRESENT="1", UNSLOTH_MIRROR_FALLBACK="0",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_XET="1")
    for name in ("HF_TOKEN", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_ENDPOINT",
                 "HF_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN",
                 "HUGGINGFACEHUB_API_TOKEN", "HF_OIDC_RESOURCE"):
        env.pop(name, None)
    return env


def run_helper(python, mode, metadata, env, *extra):
    subprocess.run([str(python), str(HELPER), mode, "--metadata", str(metadata), *extra],
                   env=env, check=True, timeout=600)


async def probe(base, artifacts, metadata, attempts):
    data = json.loads(metadata.read_text())
    observations = []
    def events():
        return [json.loads(line) for line in attempts.read_text().splitlines()] if attempts.exists() else []
    async with async_playwright() as browsers:
        browser_type = getattr(browsers, os.environ.get("STUDIO_BROWSER", "webkit"))
        browser = await browser_type.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(base, wait_until="domcontentloaded")
            online = await page.evaluate("navigator.onLine")
            assert online is True, "This experiment requires the browser to report online"
            async def state(blocked, incomplete=False):
                async with httpx.AsyncClient(base_url=base) as client:
                    response = await client.post("/repro/state", params={"blocked": str(blocked).lower(),
                                                   "incomplete": str(incomplete).lower()})
                    response.raise_for_status()
            async def query(label, prefix, params, token=None):
                count = len(events())
                started = time.monotonic()
                result = await page.evaluate("""async ({prefix, params, token}) => {
                    const headers = token ? {'X-Unsloth-HF-Token': token} : {};
                    const r = await fetch(prefix + '/gguf-variants?' + new URLSearchParams(params), {headers});
                    return {status: r.status, body: await r.json(), navigator_online: navigator.onLine};
                }""", {"prefix": prefix, "params": params, "token": token})
                observation = {"label": label, "prefix": prefix, "query": params, **result,
                    "elapsed_seconds": round(time.monotonic()-started, 3),
                    "remote_events": events()[count:]}
                observations.append(observation)
                (artifacts / "observations.json").write_text(json.dumps(observations, indent=2)+"\n")
                assert result["navigator_online"] is True
                return observation
            # A real successful remote listing distinguishes setup failure from offline behavior.
            await state(False)
            positive = await query("online-remote-control", "/api/hub", {"repo_id": data["repo"]})
            assert positive["status"] == 200 and positive["body"]["variants"], positive
            assert any(e["kind"] == "http" for e in positive["remote_events"]), positive
            print("PASS online remote discovery control", flush=True)
            for prefix in ("/api/hub", "/api/models"):
                await state(True)
                baseline = await query("on-device-default-options", prefix, {
                    "repo_id": data["repo"], "local_path": data["snapshot"]})
                assert baseline["status"] == 200, baseline
                assert any(v["downloaded"] for v in baseline["body"]["variants"]), baseline
                assert any(e["kind"] == "http" and e["blocked"] and "/api/models/" in e["path"]
                           for e in baseline["remote_events"]), "Metadata probe was not reproduced"
                print(f"REPRO {prefix}: default On Device options queried HF before cache fallback", flush=True)
                await state(True)
                preferred = await query("on-device-cache-preferred", prefix, {
                    "repo_id": data["repo"], "local_path": data["snapshot"], "prefer_local_cache": "true"}, TOKEN)
                assert preferred["status"] == 200, preferred
                assert any(v["downloaded"] for v in preferred["body"]["variants"]), preferred
                assert any(e["kind"] == "http" and e["path"].endswith("/auth-check") and e["blocked"]
                           for e in preferred["remote_events"]), "Auth probe was not reproduced"
                print(f"REPRO {prefix}: cache preference still reached HF auth-check", flush=True)
                await state(True)
                offline = await query("explicit-local-only-control", prefix, {
                    "repo_id": data["repo"], "local_path": data["snapshot"],
                    "prefer_local_cache": "true", "offline": "true"}, TOKEN)
                assert offline["status"] == 200, offline
                assert any(v["downloaded"] for v in offline["body"]["variants"]), offline
                assert offline["body"]["dependencies_resolved"] is True, offline
                assert offline["remote_events"] == [], offline
                print(f"PASS {prefix}: explicit offline discovery resolved cache without remote calls", flush=True)
            await state(True, incomplete=True)
            incomplete = await query("missing-companion-control", "/api/hub", {
                "repo_id": data["repo"], "prefer_local_cache": "true", "offline": "true"}, TOKEN)
            assert incomplete["status"] == 200, incomplete
            assert not incomplete["body"]["dependencies_resolved"], incomplete
            assert all(not v["downloaded"] or v["partial"] for v in incomplete["body"]["variants"]), incomplete
            assert incomplete["remote_events"] == [], incomplete
            print("PASS incomplete companion remains non-loadable", flush=True)
            await state(True)
            stranger = await query("other-credential-control", "/api/hub", {
                "repo_id": data["repo"], "prefer_local_cache": "true", "offline": "true"}, "hf_other_runner_identity")
            assert stranger["status"] == 404, stranger
            assert stranger["remote_events"] == [], stranger
            print("PASS another credential cannot read the host cache offline", flush=True)
            await page.screenshot(path=str(artifacts / "webkit-request-harness.png"))
        finally:
            await context.close()
            await browser.close()


async def main():
    artifacts = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "evidence/macos-runner-local-check")).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    install_home = Path(os.environ.get("UNSLOTH_STUDIO_HOME", Path.home()/".unsloth"/"studio"))
    python = Path(os.environ.get("ISSUE_12415_BACKEND_PYTHON", install_home/"unsloth_studio"/"bin"/"python"))
    assert python.is_file(), f"Missing installed backend Python: {python}"
    (artifacts / "platform.json").write_text(json.dumps({"system": platform.system(),
        "machine": platform.machine(), "version": platform.platform(), "browser": os.environ.get("STUDIO_BROWSER", "webkit"),
        "harness_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "release_tag": os.environ.get("ISSUE_12415_RELEASE_TAG"),
        "native_desktop_app": False, "scope": "real Studio API routes via browser request harness"}, indent=2)+"\n")
    env = environment(artifacts)
    # State isolation changes STUDIO_HOME; keep the installer's actual native runtime.
    runtime_roots = [install_home / "llama.cpp", install_home.parent / "llama.cpp",
                     Path.home() / ".unsloth" / "llama.cpp"]
    if os.environ.get("UNSLOTH_LLAMA_CPP_PATH"):
        runtime_roots.insert(0, Path(os.environ["UNSLOTH_LLAMA_CPP_PATH"]))
    for runtime in runtime_roots:
        if any((runtime / name).is_file() for name in ("llama-server", "build/bin/llama-server")):
            env["UNSLOTH_LLAMA_CPP_PATH"] = str(runtime)
            break
    else:
        raise RuntimeError("Installed llama.cpp runtime was not found")
    metadata = artifacts / "model.json"
    if os.environ.get("ISSUE_12415_INSTALLED_BACKEND") == "1":
        run_helper(python, "provenance", metadata, env, "--output", str(artifacts/"backend-provenance.json"))
    run_helper(python, "prepare", metadata, env)
    # The same synthetic host identity is held for the blocked auth-check and offline control.
    env["HF_TOKEN"] = TOKEN
    attempts = artifacts / "remote-attempts.jsonl"
    if attempts.exists():
        attempts.unlink()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (artifacts / "route-host.log").open("w") as log:
        server = subprocess.Popen([str(python), str(HELPER), "serve", "--metadata", str(metadata),
                  "--attempts", str(attempts), "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url=base, timeout=1) as client:
                for _ in range(120):
                    assert server.poll() is None, "Route host exited; inspect route-host.log"
                    try:
                        if (await client.get("/")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.5)
                else:
                    raise TimeoutError("Route host startup exceeded 60 seconds")
            await probe(base, artifacts, metadata, attempts)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    run_helper(python, "local", metadata, env, "--output", str(artifacts/"local-inference.json"))
    print(f"PASS {platform.system()} reproduction checks complete; permanent loading failure was not observed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
