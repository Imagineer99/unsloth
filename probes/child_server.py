# SPDX-License-Identifier: AGPL-3.0-only
"""Disposable HTTP/download child used only by the review integration probes."""
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

port, source, cache, mode = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
cache.write_bytes(b'')
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/health':
            print('request_completed path=/api/health', flush=True)
            body={'status':'healthy'}
        elif self.headers.get('Authorization') != 'Bearer sk-unsloth-test':
            self.send_error(401)
            return
        elif 'gguf-variants' in self.path:
            body={'default_variant':'Q4_K_M','variants':[]}
        else:
            body={'downloaded_bytes':cache.stat().st_size,'expected_bytes':65536*60}
        raw=json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self,*args):pass

server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
print('UNSLOTH_START_API_KEY: sk-unsloth-test',flush=True)
with urllib.request.urlopen(source,timeout=5) as response, cache.open('wb') as out:
    for i in range(60):
        chunk=response.read(65536)
        out.write(chunk)
        out.flush()
        if mode=='stall' and i==4:
            while True: time.sleep(1)
print('Model loaded: org/model',flush=True)
while True: time.sleep(1)
