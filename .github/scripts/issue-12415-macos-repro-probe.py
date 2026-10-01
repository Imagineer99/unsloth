#!/usr/bin/env python3
"""Run #12415 request shapes through WebKit on macOS, with real cached GGUF/routes.

Does not claim to exercise the packaged Tauri application or Safari itself.
"""

from __future__ import annotations
import asyncio
import hashlib
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
    env = dict(
        os.environ,
        HF_HOME = str(hf_home),
        HF_HUB_CACHE = str(hf_home / "hub"),
        HF_TOKEN_PATH = str(hf_home / "token"),
        HF_XET_CACHE = str(hf_home / "xet"),
        UNSLOTH_STUDIO_HOME = str(home / "studio"),
        UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE = "1",
        UNSLOTH_ALLOW_CPU = "1",
        UNSLOTH_IS_PRESENT = "1",
        UNSLOTH_MIRROR_FALLBACK = "0",
        HF_HUB_DISABLE_IMPLICIT_TOKEN = "1",
        HF_HUB_DISABLE_XET = "1",
    )
    for name in (
        "HF_TOKEN",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_ENDPOINT",
        "HF_HUB_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "HUGGINGFACEHUB_API_TOKEN",
        "HF_OIDC_RESOURCE",
    ):
        env.pop(name, None)
    return env


def run_helper(python, mode, metadata, env, *extra):
    subprocess.run(
        [str(python), str(HELPER), mode, "--metadata", str(metadata), *extra],
        env = env,
        check = True,
        timeout = 600,
    )


async def probe(base, artifacts, metadata, attempts):
    data = json.loads(metadata.read_text())
    observations = []

    def events():
        return (
            [json.loads(line) for line in attempts.read_text().splitlines()]
            if attempts.exists()
            else []
        )

    async with async_playwright() as browsers:
        browser_type = getattr(browsers, os.environ.get("STUDIO_BROWSER", "webkit"))
        browser = await browser_type.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(base, wait_until = "domcontentloaded")
            await page.add_script_tag(path = os.environ["ISSUE_12415_FRONTEND_BUNDLE"])
            online = await page.evaluate("navigator.onLine")
            assert online is True, "This experiment requires the browser to report online"

            async def state(blocked, incomplete = False):
                async with httpx.AsyncClient(base_url = base) as client:
                    response = await client.post(
                        "/repro/state",
                        params = {
                            "blocked": str(blocked).lower(),
                            "incomplete": str(incomplete).lower(),
                        },
                    )
                    response.raise_for_status()

            async def query(
                label,
                prefix,
                params,
                token = None,
            ):
                count = len(events())
                started = time.monotonic()
                result = await page.evaluate(
                    """async ({prefix, params, token}) => {
                    const headers = token ? {'X-Unsloth-HF-Token': token} : {};
                    const query = window.issue12415.ggufVariantsQuery(params.repo_id, {
                        localOnly: params.offline === 'true',
                        preferLocalCache: params.prefer_local_cache === 'true',
                        localPath: params.local_path,
                    }, false);
                    const r = await fetch(prefix + '/gguf-variants?' + query, {headers});
                    const body = await r.json();
                    return {status: r.status, body, navigator_online: navigator.onLine,
                        query: Object.fromEntries(query),
                        sole_quant: window.issue12415.verifiedSoleHubVariant(body.variants || [],
                            body.resolved_locally === true, body.dependencies_resolved === true)};
                }""",
                    {"prefix": prefix, "params": params, "token": token},
                )
                observation = {
                    "label": label,
                    "prefix": prefix,
                    "query": params,
                    **result,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "remote_events": events()[count:],
                }
                observations.append(observation)
                (artifacts / "observations.json").write_text(
                    json.dumps(observations, indent = 2) + "\n"
                )
                assert result["navigator_online"] is True
                return observation

            # A real successful remote listing distinguishes setup failure from offline behavior.
            await state(False)
            positive = await query("online-remote-control", "/api/hub", {"repo_id": data["repo"]})
            assert positive["status"] == 200 and positive["body"]["variants"], positive
            assert any(e["kind"] == "http" for e in positive["remote_events"]), positive
            print("PASS online remote discovery control", flush = True)
            for prefix in ("/api/hub", "/api/models"):
                await state(True)
                offline = await query(
                    "fixed-on-device-discovery",
                    prefix,
                    {
                        "repo_id": data["repo"],
                        "local_path": data["snapshot"],
                        "prefer_local_cache": "true",
                        "offline": "true",
                    },
                    TOKEN,
                )
                assert offline["status"] == 200, offline
                assert any(v["downloaded"] for v in offline["body"]["variants"]), offline
                assert offline["body"]["dependencies_resolved"] is True, offline
                assert offline["sole_quant"] is not None, offline
                assert offline["remote_events"] == [], offline
                print(
                    f"PASS {prefix}: actual frontend query resolved sole cached quant with zero remote attempts",
                    flush = True,
                )

            # Hold optional discovery in the browser, and observe the cached phase before release.
            await state(True)
            count = len(events())
            release_remote = asyncio.Event()
            remote_started = asyncio.Event()

            async def hold_remote(route):
                if "offline=true" not in route.request.url:
                    remote_started.set()
                    await release_remote.wait()
                await route.continue_()

            await page.route("**/api/models/gguf-variants?**", hold_remote)
            await page.evaluate(
                """({repo, snapshot, token}) => {
                window.cachedPhase = null;
                window.finalPhase = null;
                window.pickerFailure = null;
                window.pickerPending = window.issue12415.loadPickerGgufVariants(async localOnly => {
                    const query = window.issue12415.ggufVariantsQuery(repo, {localOnly, localPath: snapshot}, false);
                    const response = await fetch('/api/models/gguf-variants?' + query, {
                        headers: {'X-Unsloth-HF-Token': token}});
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                }, {onDevice: true, showAllQuantizations: true, canDiscoverRemote: () => true},
                cached => {window.cachedPhase = cached;}).then(result => {window.finalPhase = result;})
                    .catch(error => {window.pickerFailure = String(error);});
            }""",
                {"repo": data["repo"], "snapshot": data["snapshot"], "token": TOKEN},
            )
            await asyncio.wait_for(remote_started.wait(), timeout = 15)
            cached = await page.evaluate("window.cachedPhase")
            assert cached and cached["dependencies_resolved"] is True, cached
            assert any(v["downloaded"] and not v["partial"] for v in cached["variants"]), cached
            assert await page.evaluate("window.finalPhase === null")
            assert events()[count:] == [], "Cached phase contacted Hub before optional discovery"
            print(
                "PASS Show all cached quant usable while optional remote request is stalled",
                flush = True,
            )
            release_remote.set()
            await page.wait_for_function(
                "window.finalPhase !== null || window.pickerFailure !== null", timeout = 45000
            )
            assert await page.evaluate("window.pickerFailure") is None
            final = await page.evaluate("window.finalPhase")
            cached_quant = next(v for v in cached["variants"] if v["downloaded"])
            final_quant = next(v for v in final["variants"] if v["quant"] == cached_quant["quant"])
            assert (
                final_quant["filename"],
                final_quant.get("cache_path"),
                final_quant["downloaded"],
            ) == (cached_quant["filename"], cached_quant.get("cache_path"), True)
            optional_events = events()[count:]
            assert any(
                e["blocked"] for e in optional_events
            ), "Optional remote failure was not exercised"
            observations.append(
                {
                    "label": "fixed-show-all-stalled-remote",
                    "prefix": "/api/models",
                    "status": 200,
                    "navigator_online": True,
                    "body": final,
                    "cached_phase": cached,
                    "remote_events_before_cached": [],
                    "remote_events": optional_events,
                }
            )
            (artifacts / "observations.json").write_text(json.dumps(observations, indent = 2) + "\n")
            await page.unroute("**/api/models/gguf-variants?**", hold_remote)
            print(
                "PASS Show all retains cached load path after optional remote transport fails",
                flush = True,
            )
            await state(True, incomplete = True)
            incomplete = await query(
                "missing-companion-control",
                "/api/hub",
                {"repo_id": data["repo"], "prefer_local_cache": "true", "offline": "true"},
                TOKEN,
            )
            assert incomplete["status"] == 200, incomplete
            assert not incomplete["body"]["dependencies_resolved"], incomplete
            assert incomplete["sole_quant"] is None, incomplete
            assert all(
                not v["downloaded"] or v["partial"] for v in incomplete["body"]["variants"]
            ), incomplete
            assert incomplete["remote_events"] == [], incomplete
            print("PASS incomplete companion remains non-loadable", flush = True)
            await state(True)
            stranger = await query(
                "other-credential-control",
                "/api/hub",
                {
                    "repo_id": data["repo"],
                    "local_path": data["snapshot"],
                    "prefer_local_cache": "true",
                    "offline": "true",
                },
                "hf_other_runner_identity",
            )
            assert stranger["status"] == 404, stranger
            assert stranger["remote_events"] == [], stranger
            print("PASS another credential cannot read the host cache offline", flush = True)
            await page.screenshot(path = str(artifacts / "webkit-request-harness.png"))
        finally:
            await context.close()
            await browser.close()


async def main():
    artifacts = Path(
        os.environ.get("STUDIO_ARTIFACT_DIR", "evidence/macos-runner-local-check")
    ).resolve()
    artifacts.mkdir(parents = True, exist_ok = True)
    install_home = Path(os.environ.get("UNSLOTH_STUDIO_HOME", Path.home() / ".unsloth" / "studio"))
    python = Path(
        os.environ.get(
            "ISSUE_12415_BACKEND_PYTHON", install_home / "unsloth_studio" / "bin" / "python"
        )
    )
    assert python.is_file(), f"Missing installed backend Python: {python}"
    (artifacts / "platform.json").write_text(
        json.dumps(
            {
                "system": platform.system(),
                "machine": platform.machine(),
                "version": platform.platform(),
                "browser": os.environ.get("STUDIO_BROWSER", "webkit"),
                "harness_sha": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd = ROOT, text = True
                ).strip(),
                "release_tag": os.environ.get("ISSUE_12415_RELEASE_TAG"),
                "native_desktop_app": False,
                "scope": "real Studio API routes via browser request harness",
            },
            indent = 2,
        )
        + "\n"
    )
    env = environment(artifacts)
    source_hashes = {}
    for relative in (
        "studio/frontend/src/features/chat/api/gguf-variants-request.ts",
        "studio/frontend/src/features/hub/inventory/api.ts",
        "studio/frontend/src/features/model-picker/components/model-selector/pickers.tsx",
        "studio/frontend/src/features/model-picker/components/model-selector/gguf-discovery.ts",
        "studio/frontend/src/features/model-picker/components/model-selector/sole-quant-cache.ts",
    ):
        committed = subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd = ROOT)
        assert (ROOT / relative).read_bytes() == committed, relative
        source_hashes[relative] = hashlib.sha256(committed).hexdigest()
    (artifacts / "frontend-provenance.json").write_text(json.dumps(source_hashes, indent = 2) + "\n")
    # State isolation changes STUDIO_HOME; keep the installer's actual native runtime.
    runtime_roots = [
        install_home / "llama.cpp",
        install_home.parent / "llama.cpp",
        Path.home() / ".unsloth" / "llama.cpp",
    ]
    if os.environ.get("UNSLOTH_LLAMA_CPP_PATH"):
        runtime_roots.insert(0, Path(os.environ["UNSLOTH_LLAMA_CPP_PATH"]))
    for runtime in runtime_roots:
        if any((runtime / name).is_file() for name in ("llama-server", "build/bin/llama-server")):
            env["UNSLOTH_LLAMA_CPP_PATH"] = str(runtime)
            break
    else:
        raise RuntimeError("Installed llama.cpp runtime was not found")
    metadata = artifacts / "model.json"
    run_helper(
        python, "provenance", metadata, env, "--output", str(artifacts / "backend-provenance.json")
    )
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
        server = subprocess.Popen(
            [
                str(python),
                str(HELPER),
                "serve",
                "--metadata",
                str(metadata),
                "--attempts",
                str(attempts),
                "--port",
                str(port),
            ],
            env = env,
            stdout = log,
            stderr = subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url = base, timeout = 1) as client:
                for _ in range(120):
                    assert server.poll() is None, "Route host exited; inspect route-host.log"
                    try:
                        if (await client.get("/")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.5)
                else:
                    raise TimeoutError("Route host startup exceeded 60 seconds")
            await probe(base, artifacts, metadata, attempts)
        finally:
            server.terminate()
            try:
                server.wait(timeout = 10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout = 5)
    run_helper(python, "local", metadata, env, "--output", str(artifacts / "local-inference.json"))
    print(
        f"PASS {platform.system()} fix verification complete; native Tauri shell not exercised",
        flush = True,
    )


if __name__ == "__main__":
    asyncio.run(main())
