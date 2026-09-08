# SPDX-License-Identifier: AGPL-3.0-only
"""Real child, loopback HTTP, log marker and streamed bytes; accelerated timeout."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from loopback_server import LoopbackHTTPServer
import tempfile

import pytest
import typer
import unsloth_cli.commands.start as s

ROOT=Path(__file__).resolve().parent

@pytest.mark.parametrize('model,mode', [('org/model','live'),('org/model-GGUF','live'),('org/model','stall')])
def test_real_process_stream(monkeypatch,tmp_path,model,mode):
    # Keep loopback test traffic independent of the runner's ambient proxy settings.
    monkeypatch.setenv('no_proxy','*')
    monkeypatch.setenv('NO_PROXY','*')
    release=threading.Event()
    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length',str(65536*60))
            self.end_headers()
            try:
                for index in range(60):
                    if index==5 and not release.wait(30):
                        raise TimeoutError('Probe setup did not release the transfer')
                    self.wfile.write(b'x'*65536)
                    self.wfile.flush()
                    time.sleep(.04)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        def log_message(self,*args):pass
    upstream=LoopbackHTTPServer(('127.0.0.1',0),Source)
    thread=threading.Thread(target=upstream.serve_forever,daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    cache=tmp_path/'payload.incomplete'
    real_popen=subprocess.Popen
    children=[]
    shutdowns=[]
    timed_start=[]
    def spawn(command,**kwargs):
        child=real_popen([sys.executable,str(ROOT/'child_server.py'),str(port),
            f'http://127.0.0.1:{upstream.server_port}/payload',str(cache),mode],**kwargs)
        children.append(child)
        # Popen returns before production starts its deadline. Hold it here until
        # the child has received bytes and the real HTTP health endpoint responds.
        limit=time.monotonic()+25
        while time.monotonic()<limit:
            assert child.poll() is None, 'Test child exited during setup'
            if cache.exists() and cache.stat().st_size>=5*65536:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1) as response:
                        assert json.load(response)['status']=='healthy'
                    break
                except OSError:
                    pass
            time.sleep(.02)
        else:
            log=Path(tempfile.gettempdir())/f'unsloth-start-server-{os.getpid()}.log'
            print(log.read_text(errors='replace').replace('sk-unsloth-test','[test-key]'))
            raise AssertionError('Test child/transfer never became ready for timing')
        timed_start.append(time.monotonic())
        release.set()
        return child
    def shutdown(child):
        shutdowns.append(cache.stat().st_size)
        child.terminate()
        child.wait(timeout=5)
    monkeypatch.setattr(s.subprocess,'Popen',spawn)
    monkeypatch.setattr(s,'_shutdown_server',shutdown)
    monkeypatch.setattr(s,'_auto_served_server',None)
    # Same 900-second production budget; wall time accelerated 900x.
    monkeypatch.setattr(s,'time',SimpleNamespace(monotonic=lambda:time.monotonic()*900,
        sleep=lambda seconds:time.sleep(.01)))
    begin=time.monotonic()
    try:
        if mode=='stall':
            with pytest.raises(typer.Exit):
                s._start_studio_server(f'http://127.0.0.1:{port}',model,s.LoadOptions())
            assert shutdowns==[5*65536]
            assert time.monotonic()-timed_start[0] < 5, 'Noisy stall exceeded its bounded timeout'
        else:
            try:
                child=s._start_studio_server(f'http://127.0.0.1:{port}',model,s.LoadOptions())
            except typer.Exit:
                # Reject setup failures: the real transfer must have started and been stopped.
                assert os.environ['PROBE_ARM'] == 'base'
                assert len(shutdowns) == 1
                assert 0 < shutdowns[0] < 65536*60
                assert children[0].poll() is not None
                assert time.monotonic()-timed_start[0] >= 1.0
                print(json.dumps({'arm':'base','model':model,'outcome':'active-download-killed',
                    'bytes_at_shutdown':shutdowns[0]}))
                pytest.xfail('Confirmed baseline kills an active file transfer')
            assert os.environ['PROBE_ARM'] == 'head', 'Baseline unexpectedly completed'
            assert child.poll() is None
            assert cache.stat().st_size==65536*60
            assert shutdowns==[]
        print(json.dumps({'mode':mode,'model':model,'seconds':time.monotonic()-begin,
            'bytes':cache.stat().st_size,'shutdowns':shutdowns}))
    finally:
        release.set()
        for child in children:
            if child.poll() is None: child.terminate()
            child.wait(timeout=5)
        s._auto_served_server=None
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
