# SPDX-License-Identifier: AGPL-3.0-only
"""Local-only evidence host: real Studio monitor/passthrough, synthetic model/auth.

This deliberately does not exercise model discovery, GPU inference, or real login.
No progress fields, response headers, or UI labels are synthesized by this host.
"""
import argparse
import asyncio
import contextvars
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--home', type=Path, required=True)
parser.add_argument('--ready', type=Path, required=True)
args = parser.parse_args()
repo = args.repo.resolve()
home = args.home.resolve()
home.mkdir(parents=True, exist_ok=True)
for key, value in {
    'UNSLOTH_STUDIO_HOME': home, 'HF_HOME': home / 'hf',
    'HF_HUB_CACHE': home / 'hf/hub', 'HF_XET_CACHE': home / 'hf/xet',
    'XDG_CACHE_HOME': home / 'cache', 'TMPDIR': home / 'tmp',
    'TEMP': home / 'tmp', 'TMP': home / 'tmp',
}.items():
    Path(value).mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(value)
os.environ.update(UNSLOTH_ALLOW_CPU='1', UNSLOTH_IS_PRESENT='1',
                  UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE='1', UNSLOTH_NVIDIA_LIBRARY_PROBE='0')
sys.path.insert(0, str(repo / 'studio/backend'))
import loggers  # Settle the actual package before route imports.
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from core.inference.api_monitor import ApiMonitor
from models.inference import ChatCompletionRequest
from routes import inference as inf

sock = socket.socket()
sock.bind(('127.0.0.1', 0))
port = sock.getsockname()[1]
sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
dist = repo / 'studio/frontend/dist'
identity = dict(sha=sha, pid=os.getpid(), port=port, home=str(home), repo=str(repo),
                bundle_sha256=hashlib.sha256((dist / 'index.html').read_bytes()).hexdigest() if dist.exists() else None,
                bundle_commit=(dist / '.uidiff_sha').read_text().strip() if (dist / '.uidiff_sha').exists() else None)
app = FastAPI()
monitor = ApiMonitor(max_entries=100)
inf.api_monitor = monitor
inf._monitor_active_model = lambda: 'fixture/local-gguf'
inf._monitor_context_length = lambda: 4096
inf._monitor_queue_state = lambda: None
inf._direct_llama_is_busy = lambda: False
case_ctx = contextvars.ContextVar('case', default='scene')
inf._first_token_timeout_s = lambda: 2.0 if case_ctx.get() in ('slow', 'frozen', 'stall') else 120.0
inf._openai_compat_stream_stall_timeout = lambda: .4 if case_ctx.get() in ('slow', 'frozen', 'stall') else 120.0
state = {}


def frame(delta=None, **extra):
    return 'data: ' + json.dumps({'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk',
                                 'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': None}], **extra}) + '\n\n'


@app.get('/__identity')
def get_identity():
    return identity


@app.get('/__state')
def get_state():
    return {k: {field: value for field, value in v.items() if field != 'release'} for k, v in state.items()}


@app.post('/__advance/{case}')
async def advance(case: str):
    state[case]['release'].set()
    return {'released': case}


@app.post('/fake/v1/chat/completions')
async def fake_llama(request: Request):
    body = await request.json()
    case = body['messages'][-1]['content']
    record = state.setdefault(case, {})
    record.update(upstream_body=body, release=asyncio.Event(), phase='starting')

    async def generate():
        try:
            if case in ('slow', 'frozen'):
                for i in range(7):
                    if body.get('return_progress'):
                        yield frame({'role': 'assistant', 'content': None}, prompt_progress={
                            'processed': (i * 200 if case == 'slow' else 0), 'total': 2000, 'cache': 0, 'time_ms': i * 500})
                    await asyncio.sleep(.5)
            elif case != 'legacy':
                if body.get('return_progress'):
                    yield frame({'role': 'assistant', 'content': None}, prompt_progress={
                        'processed': 1200, 'total': 2000, 'cache': 0, 'time_ms': 15000})
            record['phase'] = 'prefill-ready'
            if case.startswith('scene') or case.startswith('concurrent'):
                await record['release'].wait()
            if case == 'tool':
                yield frame({'tool_calls': [{'index': 0, 'id': 'call_lookup', 'type': 'function',
                                             'function': {'name': 'lookup', 'arguments': '{}'}}]})
            else:
                yield frame({'content': 'Fixture answer.'})
            record['phase'] = 'generation'
            if case.startswith('scene'):
                record['release'].clear()
                await record['release'].wait()
            if case == 'stall':
                await asyncio.sleep(4)
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield 'data: [DONE]\n\n'
        finally:
            record['closed'] = True
    return StreamingResponse(generate(), media_type='text/event-stream')


@app.post('/v1/chat/completions')
async def completion(request: Request):
    raw = await request.json()
    payload = ChatCompletionRequest(**raw)
    case = raw['messages'][-1]['content']
    case_ctx.set(case)
    subject = request.headers.get('x-fixture-subject', 'alice')
    entry_id = monitor.start(endpoint='/v1/chat/completions', method='POST', model='fixture/local-gguf',
                             prompt=case, subject=subject)
    state[case] = dict(monitor_id=entry_id, subject=subject)
    backend = SimpleNamespace(base_url=f'http://127.0.0.1:{port}/fake', context_length=4096,
                              _request_reasoning_kwargs=lambda *_a, **_k: None)
    return await inf._openai_passthrough_stream(request, threading.Event(), backend, payload,
                                               'fixture/local-gguf', 'chatcmpl-' + case, monitor_id=entry_id)


@app.get('/api/inference/monitor')
async def monitor_list(request: Request):
    return await inf.get_api_monitor(current_subject=request.headers.get('x-fixture-subject', 'alice'))


@app.get('/api/inference/monitor/{entry_id}')
async def monitor_detail(entry_id: str, request: Request):
    return await inf.get_api_monitor_entry(entry_id, current_subject=request.headers.get('x-fixture-subject', 'alice'))


# Only unrelated shell services are fixtures. Monitor endpoints above use production code.
@app.api_route('/api/{path:path}', methods=['GET', 'POST'], include_in_schema=False)
async def shell_fixture(path: str):
    if path == 'auth/status':
        return dict(authenticated=True, requires_password_change=False, login_mode='single')
    if 'me' == path.split('/')[-1]:
        return dict(username='fixture', role='admin', is_admin=True, permissions=['*'])
    if 'status' in path:
        return dict(status='idle', is_loaded=False, active_model=None)
    if 'device' in path:
        return dict(device_type='cpu', devices=[], platform='linux')
    return {}


if dist.exists():
    app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/{path:path}')
    async def frontend(path: str):
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / 'index.html')

args.ready.parent.mkdir(parents=True, exist_ok=True)
args.ready.write_text(json.dumps(identity, indent=2))
uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[sock])
