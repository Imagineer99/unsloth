"""Exercise one real, cold speculative MLX chat on the given revision."""
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

side = sys.argv[1]
work = Path(sys.argv[2]).resolve()
repo = work / side
backend = repo / "studio/backend"
home = work / f"{side}-home"
home.mkdir()
marker = home / "speculative-prefill"
sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
assert sha == os.environ[side.upper()]
password = secrets.token_urlsafe(32)
env = dict(os.environ, UNSLOTH_HOME=str(home), UNSLOTH_STUDIO_HOME=str(home/"studio"),
           UNSLOTH_STUDIO_PASSWORD=password, UNSLOTH_IS_PRESENT="1", PR19738_PREFILL=str(marker),
           PYTHONPATH=os.pathsep.join(map(str,[Path(__file__).parent/"observe",backend,repo])))
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
url = f"http://127.0.0.1:{port}"
log = (home/"server.log").open("w")
server = subprocess.Popen([sys.executable, str(backend/"run.py"), "--host","127.0.0.1","--port",str(port),
                           "--frontend",str(repo/"studio/frontend/dist")],
                          env=env, cwd=backend, stdout=log, stderr=log, start_new_session=True)
print(f"{side} {sha} pid={server.pid} port={port}", flush=True)
try:
    with httpx.Client(base_url=url,timeout=300,trust_env=False) as api:
        for _ in range(240):
            if server.poll() is not None:
                raise RuntimeError(f"server exited {server.returncode}")
            try:
                if api.get("/healthz",timeout=2).is_success: break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else: raise TimeoutError("backend health")
        login=api.post("/api/auth/login",json={"username":"unsloth","password":password})
        login.raise_for_status()
        auth=login.json()
        headers={"Authorization":"Bearer "+auth["access_token"]}
        model_path=str(work/"Qwen3.5-0.8B-bf16")
        loaded=api.post("/api/inference/load",headers=headers,json={
            "model_path":model_path,"is_lora":False,"max_seq_length":12288,
            "speculative_type":"ngram","n_parallel":2,"n_ubatch":512})
        loaded.raise_for_status()
        info=loaded.json()
        print("Load", {k:info.get(k) for k in ("status","is_mlx","spec_drafter_kind","spec_fallback_reason","parallel_slots")},flush=True)
        assert info.get("status") in ("loaded","already_loaded") and info.get("spec_drafter_kind")=="ngram", "n-gram load unverified"

    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(viewport={"width":1280,"height":840},locale="en-US")
        seed={"unsloth_auth_token":auth["access_token"],"unsloth_refresh_token":auth.get("refresh_token",""),
              "unsloth_chat_preferences":json.dumps({"state":{"showGenerationDetails":True},"version":1})}
        page.add_init_script("Object.entries("+json.dumps(seed)+").forEach(([k,v])=>localStorage.setItem(k,v))")
        page.add_init_script(path=Path(__file__).with_name("stream.js"))
        page.goto(url+"/chat",wait_until="domcontentloaded")
        composer=page.locator('textarea[aria-label="Message input"]')
        composer.wait_for(timeout=60000)
        prompt="Read the entries, then give a detailed account of what they have in common.\n"+"\n".join(
            f"Entry {i}: cobalt, silver, copper and iron." for i in range(610))
        composer.fill(prompt)
        composer.press("Enter")
        start=Path(str(marker)+".start")
        end=Path(str(marker)+".end")
        deadline=time.monotonic()+300
        shot=False
        decode_visible=False
        while time.monotonic()<deadline:
            state=page.evaluate("window.pr19738")
            if not shot and start.exists() and not end.exists() and not state["text"]:
                pattern=r"^Working\.{3}$" if side=="before" else r"Processing prompt [1-9]\d?%"
                label=page.get_by_text(re.compile(pattern)).last
                if label.count() and label.is_visible():
                    box=label.bounding_box()
                    if box:
                        clip={"x":max(0,box["x"]-8),"y":max(0,box["y"]-10),"width":min(720,1280-max(0,box["x"]-8)),"height":min(62,840-max(0,box["y"]-10))}
                        visible=label.inner_text()
                        page.screenshot(path=work/(side+".png"),clip=clip)
                        shot=not end.exists() and not page.evaluate("Boolean(window.pr19738.text)")
            if side=="after" and not state["finish"] and state["decode"]:
                decode_visible |= page.get_by_text(re.compile(r"\d[\d,]* tokens?\s*·.*tok/s")).last.is_visible()
            if state["finish"]: break
            page.wait_for_timeout(35)
        assert state["finish"] and state["text"] and shot and start.exists() and end.exists(), (
            "incomplete comparison",state["finish"],len(state["text"]),shot,start.exists(),end.exists())
        if side=="after":
            assert state["prefillBeforeText"] and state["decode"] and decode_visible, "missing live phase"
        else:
            assert not state["frames"], "base emitted progress"
        facts=dict(sha=sha,port=port,pid=server.pid,model_revision=os.environ["MODEL_SHA"],status_text=visible,
                   reply_sha256=hashlib.sha256(state["text"].encode()).hexdigest(),finish=state["finish"],
                   frames=state["frames"],decode_visible=decode_visible,speculative_admission=True)
        (work/(side+".json")).write_text(json.dumps(facts))
        print(side,"facts", {k:v for k,v in facts.items() if k!="frames"}, "frames",len(facts["frames"]),flush=True)
finally:
    if server.poll() is None:
        os.killpg(server.pid,signal.SIGTERM)
        try: server.wait(timeout=15)
        except subprocess.TimeoutExpired: os.killpg(server.pid,signal.SIGKILL)
    log.close()
    if sys.exc_info()[0] is not None:
        for line in (home/"server.log").read_text(errors="replace").splitlines()[-120:]:
            if re.search(r"ModuleNotFoundError|ImportError|RuntimeError|ValueError|TypeError|AttributeError",line) and not re.search(r"pass(word)?|token|secret|api.key",line,re.I):
                print(line[:400])
