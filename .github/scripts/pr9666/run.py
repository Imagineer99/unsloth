# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Run on a native Mac only. Failures to build, launch or capture are not A/B proof."""
import json
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from prepare import MANIFEST, prepare
from analyze import analyze

def run(args, cwd, log, env=None, timeout=2400):
    with open(log,'a') as stream:
        subprocess.run(args,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=timeout)

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

def wait_http(url, process, expected):
    for _ in range(300):
        if process.poll() is not None: raise RuntimeError('Vite exited before becoming ready')
        try:
            with urllib.request.urlopen(url,timeout=1) as response:
                if response.status==200 and json.load(response)==expected:return
        except OSError: pass
        time.sleep(0.1)
    raise RuntimeError('Loopback frontend did not become ready')

def side_run(checkout, side, work, artifacts, run_id):
    root, out = work/side, artifacts/side
    subprocess.run(['git','fetch','--depth=1','--no-tags','https://github.com/unslothai/unsloth.git',MANIFEST[side]],cwd=checkout,check=True)
    subprocess.run(['git','worktree','add','--detach',str(root),MANIFEST[side]],cwd=checkout,check=True)
    out.mkdir(parents=True)
    # Cache/install logs never include a native browser profile in uploaded artifacts.
    run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],root/'studio/frontend',out/'install.log')
    # Compile with a placeholder URL, then select a port immediately before launch.
    # The native probe installs the runtime URL into Tauri's context before build.
    override = prepare(root,side,0,out,run_id)
    env = dict(os.environ, TAURI_CONFIG=json.dumps(override), PR9666_OUTPUT=str(out),
               CARGO_TARGET_DIR=str(work/'cargo-target'), CARGO_PROFILE_DEV_DEBUG='0', CARGO_INCREMENTAL='0')
    run(['cargo','build','--locked','--no-default-features','--bin','pr9666_probe'],root/'studio/src-tauri',out/'build.log',env)
    binary = work/f'pr9666-probe-{side}'
    shutil.copy2(work/'cargo-target/debug/pr9666_probe',binary)
    port = free_port()
    env['PR9666_PROFILE'] = json.loads((out/'provenance.json').read_text())['profile_uuid']
    env['PR9666_URL'] = f'http://127.0.0.1:{port}/pr9666-native.html'
    provenance = json.loads((out/'provenance.json').read_text())
    provenance.update(port=port,runtime_url=env['PR9666_URL'])
    (out/'provenance.json').write_text(json.dumps(provenance,indent=2))
    identity = {k:provenance[k] for k in ['side','sha','profile_uuid']}
    with open(out/'vite.log','w') as frontend_log, open(out/'native.log','w') as native_log:
        server = subprocess.Popen(['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=root/'studio/frontend',stdout=frontend_log,stderr=subprocess.STDOUT)
        native = None
        try:
            wait_http(f'http://127.0.0.1:{port}/pr9666-identity.json',server,identity)
            native = subprocess.Popen([str(binary)],cwd=root/'studio/src-tauri',env=env,stdout=native_log,stderr=subprocess.STDOUT)
            code = native.wait(timeout=180)
            if code != 0: raise RuntimeError(f'{side} native process exited {code}')
            if not (out/'result.json').exists():raise RuntimeError(f'{side} produced no result')
        finally:
            for process in [native,server]:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.kill();process.wait()

def main():
    checkout=pathlib.Path.cwd().resolve()
    work=checkout/'temp/pr9666-native-work'
    artifacts=checkout/'temp/pr9666-native-artifacts'
    artifacts.mkdir(parents=True,exist_ok=False)
    try:
        if platform.system()!='Darwin' or int(platform.mac_ver()[0].split('.')[0])<14:
            raise RuntimeError('Requires native macOS14+; browser emulation is not accepted')
        request=urllib.request.Request('https://api.github.com/repos/unslothai/unsloth/pulls/9666',headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json'})
        with urllib.request.urlopen(request,timeout=30) as response:
            live=json.load(response)
        if live['head']['sha']!=MANIFEST['head']:raise RuntimeError('Live PR head changed; approval pins must be refreshed')
        work.mkdir(parents=True,exist_ok=False)
        run_id=os.environ.get('GITHUB_RUN_ID',str(time.time_ns()))+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')
        for side in ['base','head']:side_run(checkout,side,work,artifacts,run_id)
        result=analyze(artifacts)
        print(json.dumps({'status':result['status'],'findings':result['findings']},indent=2))
        return 1 if result['findings'] else 0
    except Exception as error:
        (artifacts/'incomplete.json').write_text(json.dumps({'status':'incomplete_not_regression_proof','error':str(error)},indent=2))
        print(f'INCOMPLETE: {error}',file=sys.stderr)
        return 2

if __name__=='__main__':sys.exit(main())
