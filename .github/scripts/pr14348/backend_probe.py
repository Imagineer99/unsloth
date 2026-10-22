"""Pinned MCP feature/compatibility probe; adapted from the review's feature_probe.py.

Uses the real auth, SQLite settings router, MCP gate and server in process.
No model runtime or external service is started. Reports contain no credentials.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--revision', choices=('base', 'head'), required=True)
    parser.add_argument('--mode', choices=('feature', 'compatibility'), default='feature')
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    home = state / 'home'
    home.mkdir()
    os.environ['UNSLOTH_STUDIO_HOME'] = str(home)
    for key in ('UNSLOTH_STUDIO_ENABLE_MCP', 'UNSLOTH_STUDIO_MCP_TOKEN'):
        os.environ.pop(key, None)
    for key, value in {
        'UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE': '1',
        'UNSLOTH_NVIDIA_LIBRARY_PROBE': '0',
        'UNSLOTH_VULKAN_DEVICE_PROBE': '0',
        'UNSLOTH_DISABLE_SANDBOX_WARMUP': '1',
        'UNSLOTH_ALLOW_CPU': '1',
        'UNSLOTH_IS_PRESENT': '1',
    }.items():
        os.environ[key] = value
    sys.path.insert(0, str(args.source.resolve() / 'studio' / 'backend'))
    report = {
        'revision': args.revision,
        'mode': args.mode,
        'platform': platform.system(),
        'python': platform.python_version(),
        'versions': {name: importlib.metadata.version(name) for name in ('fastmcp', 'mcp', 'fastapi', 'pydantic')},
        'checks': [],
        'status': 'ERROR',
    }

    def check(name: str, condition: bool, details=None) -> None:
        entry = {'name': name, 'status': 'PASS' if condition else 'FAIL'}
        if details is not None:
            entry['details'] = details
        report['checks'].append(entry)
        print(f"{entry['status']} {name}", flush=True)
        if not condition:
            raise AssertionError(name)

    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from auth import storage
        from auth.authentication import create_access_token
        from mcp_server import create_studio_mcp
        from routes import settings
        from studio_mcp.gate import StudioMcpGate
        from tests.mcp_harness import LOCAL, MCP_HEADERS, seed_owner

        storage.DB_PATH = home / 'auth.db'
        storage._BOOTSTRAP_PW_PATH = home / '.bootstrap_password'
        seed_owner()
        raw_key, _ = storage.create_api_key(storage.DEFAULT_ADMIN_USERNAME, name='temporary-validation')
        ui = {'Authorization': f'Bearer {create_access_token(subject=storage.DEFAULT_ADMIN_USERNAME)}'}
        agent = {**MCP_HEADERS, 'Authorization': f'Bearer {raw_key}'}
        mcp_app = create_studio_mcp().http_app(path='/', stateless_http=True)
        app = FastAPI(lifespan=mcp_app.lifespan)
        app.include_router(settings.router, prefix='/api/settings')
        app.mount('/mcp', StudioMcpGate(mcp_app))
        for key, value in {
            'server_port': 8888, 'cloudflare_url': None, 'bind_host': '127.0.0.1',
            'secure': False, 'remote_access_is_colab': False,
            'lan_access_is_colab': False, 'lan_access_secure_launch': False,
        }.items():
            setattr(app.state, key, value)
        url = '/api/settings/mcp-access'
        with TestClient(app, **LOCAL) as http:
            def put(body, headers=None):
                return http.put(url, json=body, headers=ui if headers is None else headers)

            def get():
                response = http.get(url, headers=ui)
                if response.status_code != 200:
                    raise RuntimeError('owner settings read failed')
                return response.json()

            def rpc(method, params=None):
                response = http.post('/mcp/', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, headers=agent)
                if response.status_code != 200:
                    raise RuntimeError('MCP transport did not return HTTP 200')
                if 'application/json' in response.headers.get('content-type', ''):
                    return response.json()
                messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                if not messages:
                    raise RuntimeError('MCP transport returned no JSON-RPC message')
                return messages[-1]

            def listed():
                return {tool['name'] for tool in rpc('tools/list')['result']['tools']}

            def off():
                return {tool['name'] for tool in get()['tools'] if not tool['enabled']}

            check('legacy enable request succeeds', put({'enabled': True}).status_code == 200)
            initial = get()
            names = listed()
            check('initial catalog contains 23 tools including core expected names', len(names) == 23 and {'chat', 'cancel', 'studio_status', 'list_models'} <= names)

            if args.mode == 'feature':
                changed = put({'disabled_tools': ['cancel']})
                after = listed()
                if args.revision == 'base':
                    check('baseline GET lacks per-tool settings', 'tools' not in initial)
                    check('baseline rejects new tool-only request', changed.status_code == 422)
                    check('baseline leaves cancel available', 'cancel' in after)
                else:
                    check('head GET includes per-tool settings', 'tools' in initial and len(initial['tools']) == 23)
                    check('head accepts tool-only request', changed.status_code == 200)
                    check('head omits disabled cancel from discovery', 'cancel' not in after)
                    denied = rpc('tools/call', {'name': 'cancel', 'arguments': {'kind': 'chat', 'id': 'validation'}})['result']
                    check('head refuses direct disabled calls', denied.get('isError') is True and '[tool_disabled] cancel ' in denied['content'][0]['text'])
                check('legacy disable request succeeds', put({'enabled': False}).status_code == 200)
                blocked = http.post('/mcp/', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers=agent)
                check('disabled access gate remains HTTP 404', blocked.status_code == 404)
                check('legacy re-enable request succeeds', put({'enabled': True}).status_code == 200)
                if args.revision == 'head':
                    check('legacy access write preserves disabled tools', 'cancel' not in listed())
                    check('tool can be re-enabled', put({'disabled_tools': []}).status_code == 200 and listed() == names)

            elif args.mode == 'compatibility':
                check('head tool controls exist', args.revision == 'head' and 'tools' in initial)
                check('owner disables cancel', put({'disabled_tools': ['cancel']}).status_code == 200)
                for method in ('GET', 'PUT'):
                    response = http.request(method, url, json={'disabled_tools': []}, headers=agent)
                    check(f'API key cannot {method} installation settings', response.status_code == 403)
                check('denied API-key writes preserve choices', off() == {'cancel'})
                before = get()
                response = put({'enabled': False, 'disabled_tools': ['cancel', 'unknown_validation_tool']})
                check('unknown tool rejected atomically', response.status_code == 422 and get() == before)
                os.environ['UNSLOTH_STUDIO_ENABLE_MCP'] = '1'
                check('environment override is exposed', get()['forced_by_env'] is True)
                check('environment override permits tool-only save', put({'disabled_tools': ['chat', 'cancel']}).status_code == 200 and off() == {'chat', 'cancel'})
                before = get()
                check('environment override rejects access changes atomically', put({'enabled': False, 'disabled_tools': []}).status_code == 409 and get() == before)
                os.environ.pop('UNSLOTH_STUDIO_ENABLE_MCP')
                check('reset tools', put({'disabled_tools': []}).status_code == 200)
                initial_prompts = {p['name'] for p in rpc('prompts/list')['result']['prompts']}
                check('dependent prompts initially present', {'make_short_film', 'build_dataset_and_train'} <= initial_prompts)
                for dependency in ('studio_status', 'list_models'):
                    check(f'disable prompt dependency {dependency}', put({'disabled_tools': [dependency]}).status_code == 200)
                    available = {p['name'] for p in rpc('prompts/list')['result']['prompts']}
                    check(f'prompts hidden with {dependency} disabled', not {'make_short_film', 'build_dataset_and_train'} & available)
                from utils import mcp_access
                mcp_access._reset_cache()
                check('persisted choices survive cache reset', off() == {'list_models'})
                from unittest.mock import patch
                with patch.object(mcp_access, '_read_from_db', side_effect=RuntimeError('validation read failure')):
                    mcp_access._reset_cache()
                    check('storage failure sends unavailable tool list', get()['tools'] is None)
                mcp_access._reset_cache()
                check('read recovery restores saved choice', off() == {'list_models'})

        report['status'] = 'PASS'
    except AssertionError as exc:
        report['status'] = 'FAIL'
        report['failure'] = str(exc)
    except Exception as exc:
        report['status'] = 'ERROR'
        report['error_type'] = type(exc).__name__
        print(f'ERROR setup or transport: {type(exc).__name__}', flush=True)
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
