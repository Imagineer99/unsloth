"""Launch both pinned real apps and run the platform's narrow MCP UI probes.

Linux: Chromium, Firefox, WebKit. Windows: installed Chrome and Edge.
macOS: native Safari through safaridriver. Every process is stopped in finally.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
import httpx
import psutil

BASE = '588e1c4ffefc7ee74a8474da2d4791ea2c44313b'
HEAD = 'bf573a6117540e6187cf8427e3971e97bd74636f'


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        _, alive = psutil.wait_procs(children + [parent], timeout=10)
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', choices=('feature', 'concurrency'), default='feature')
    parser.add_argument('--browser-python', type=Path, help='Optional local capture interpreter; hosted workflow uses the same isolated runtime for both.')
    parser.add_argument('--only', choices=('chromium', 'firefox', 'webkit', 'chrome', 'msedge', 'safari'), help='Local package verification only; hosted workflow uses full platform selection.')
    args = parser.parse_args()
    browser_python = str(args.browser_python.absolute()) if args.browser_python else sys.executable
    root = args.root.resolve()
    scripts = Path(__file__).resolve().parent
    output = root / 'run' / ('browser' if args.phase == 'feature' else 'concurrency')
    private, reports, state = (output / name for name in ('private', 'reports', 'state'))
    for directory in (private, reports, state, root / 'tmp'):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        private.chmod(0o700)
    processes = []
    config = {}
    results = []
    failed = False
    stage = 'initialization'
    try:
        sides = [('before', 'base', BASE), ('after', 'head', HEAD)] if args.phase == 'feature' else [('after', 'head', HEAD)]
        for label, folder, sha in sides:
            stage = f'source-check:{folder}'
            source = root / folder
            actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            if actual != sha:
                raise RuntimeError(f'{folder} SHA mismatch')
            subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'], check=True)
            home = state / f'home-{folder}'
            home.mkdir(exist_ok=False)
            (home / 'share').mkdir()
            identity = secrets.token_hex(32)
            (home / 'share' / 'studio_install_id').write_text(identity, encoding='utf-8')
            (home / '.uidiff_sha').write_text(sha + '\n', encoding='utf-8')
            password = secrets.token_urlsafe(32)
            env = os.environ.copy()
            for key in ('UNSLOTH_STUDIO_ENABLE_MCP', 'UNSLOTH_STUDIO_MCP_TOKEN', 'UNSLOTH_STUDIO_PASSWORD'):
                env.pop(key, None)
            env.update({
                'UNSLOTH_STUDIO_HOME': str(home), 'UNSLOTH_HOME': str(home),
                'HF_HOME': str(home / 'cache' / 'huggingface'),
                'HF_HUB_CACHE': str(home / 'cache' / 'huggingface' / 'hub'),
                'HF_XET_CACHE': str(home / 'cache' / 'huggingface' / 'xet'),
                'XDG_CACHE_HOME': str(home / 'cache'),
                'TMPDIR': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'TEMP': str(root / 'tmp'),
                'UNSLOTH_STUDIO_PASSWORD': password,
                'UNSLOTH_STUDIO_DISABLE_TORCH_WARM': '1', 'UNSLOTH_DISABLE_SANDBOX_WARMUP': '1',
                'UNSLOTH_DISABLE_UPDATE_CHECK': '1', 'UNSLOTH_STUDIO_DISABLE_PUBLIC_CHECK': '1',
                'UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE': '1', 'UNSLOTH_NVIDIA_LIBRARY_PROBE': '0',
                'UNSLOTH_VULKAN_DEVICE_PROBE': '0', 'UNSLOTH_DISABLE_MLX_AUTOREPAIR': '1',
                'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            })
            stage = f'launch:{folder}'
            port = free_port()
            with (private / f'{folder}-server.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, str(source / 'studio' / 'backend' / 'run.py'),
                    '--host', '127.0.0.1', '--port', str(port), '--frontend', str(source / 'studio' / 'frontend' / 'dist'),
                    '--no-cloudflare', '--silent'], cwd=source / 'studio' / 'backend', env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            processes.append(process)
            url = f'http://127.0.0.1:{port}'
            with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
                stage = f'health-and-identity:{folder}'
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'{folder} app exited during startup')
                    try:
                        health = client.get('/api/health').json()
                        if health.get('status') == 'healthy' and health.get('studio_root_id') == identity:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f'{folder} app health/identity timed out')
                stage = f'owner-login:{folder}'
                login = client.post('/api/auth/login', json={'username': 'unsloth', 'password': password})
                login.raise_for_status()
                auth = login.json()
                stage = f'create-disposable-api-key:{folder}'
                key = client.post('/api/auth/api-keys', json={'name': 'disposable MCP validation'}, headers={'Authorization': 'Bearer ' + auth['access_token']})
                key.raise_for_status()
                config[label] = {'url': url, 'home': str(home), 'worktree': str(source), 'sha': sha,
                    'pid': process.pid, 'studio_root_id': identity, 'health': health,
                    'access_token': auth['access_token'], 'refresh_token': auth.get('refresh_token'), 'api_key': key.json()['key']}
            print(f'PASS {folder} real app health and isolated identity', flush=True)
        for label, side in config.items():
            record = private / f'{label}-launch.json'
            record.write_text(json.dumps({key: side[key] for key in ('pid', 'sha', 'url', 'home', 'worktree')}, indent=2), encoding='utf-8')
            if os.name != 'nt':
                record.chmod(0o600)
            side['launch_record_path'] = str(record)
        config_path = private / 'apps.json'
        config_path.write_text(json.dumps(config, indent=2), encoding='utf-8')
        if os.name != 'nt':
            config_path.chmod(0o600)
        targets = {'Linux': ['chromium', 'firefox', 'webkit'], 'Windows': ['chrome', 'msedge'], 'Darwin': ['safari']}[platform.system()]
        if args.phase == 'concurrency':
            if platform.system() != 'Linux':
                raise RuntimeError('concurrency phase is intentionally scoped to Linux Chromium')
            targets = ['chromium']
        elif args.only:
            targets = [args.only]
        for browser in targets:
            stage = f'browser-probe:{browser}'
            target = private / 'captures' / browser
            target.mkdir(parents=True, exist_ok=True)
            if args.phase == 'concurrency':
                command = [browser_python, str(scripts / 'browser' / 'concurrent_save.py'), '--config', str(config_path), '--out', str(target), '--engine', 'chromium']
            elif browser == 'safari':
                command = [browser_python, str(scripts / 'browser' / 'safari_mcp.py'), '--config', str(config_path), '--out', str(target)]
            else:
                command = [browser_python, str(scripts / 'browser' / 'capture_mcp.py'), '--config', str(config_path), '--out', str(target),
                    '--engine', 'chromium' if browser in ('chrome', 'msedge') else browser]
                if browser in ('chrome', 'msedge'):
                    command.extend(['--channel', browser])
            browser_env = os.environ.copy()
            browser_env.update({'TMPDIR': str(root / 'tmp'), 'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'PYTHONDONTWRITEBYTECODE': '1'})
            with (private / f'{browser}.log').open('w', encoding='utf-8') as log:
                run = subprocess.run(command, cwd=scripts, env=browser_env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
            if args.phase == 'concurrency':
                stage = 'verify-public-concurrency-report'
                diagnostic = target / 'concurrent-save-chromium.json'
                if diagnostic.is_file():
                    serialized = diagnostic.read_text(encoding='utf-8')
                    json.loads(serialized)
                    known_secrets = [side.get(key) for side in config.values() for key in ('access_token', 'refresh_token', 'api_key')]
                    if any(secret and secret in serialized for secret in known_secrets):
                        raise RuntimeError('credential present in concurrency diagnostic')
                    shutil.copy2(diagnostic, reports / diagnostic.name)
            elif run.returncode == 0:
                stage = f'verify-public-artifacts:{browser}'
                meta_path = target / 'meta.json'
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                serialized = json.dumps(meta)
                known_secrets = [side.get(key) for side in config.values() for key in ('access_token', 'refresh_token', 'api_key')]
                if any(secret and secret in serialized for secret in known_secrets):
                    raise RuntimeError('credential present in public capture metadata')
                required = ('before.png', 'after.png', 'meta.json')
                if any(not (target / name).is_file() for name in required):
                    raise RuntimeError('successful driver omitted required evidence')
                public = reports / browser
                public.mkdir(parents=True, exist_ok=True)
                for name in (*required, 'after-overview.png', 'comparison.png'):
                    artifact = target / name
                    if artifact.is_file():
                        shutil.copy2(artifact, public / name)
            result = {'browser': browser, 'status': 'PASS' if run.returncode == 0 else 'FAIL', 'exit_code': run.returncode}
            if run.returncode != 0 and args.phase == 'feature':
                # Public failures expose only controlled diagnostic fields, never raw
                # exception messages, browser logs, failed images or partial metadata.
                meta_path = target / 'meta.json'
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    diagnostic = {key: meta[key] for key in ('stage', 'error_type', 'sides_completed', 'traceback_frames') if key in meta}
                    serialized = json.dumps(diagnostic)
                    known_secrets = [side.get(key) for side in config.values() for key in ('access_token', 'refresh_token', 'api_key')]
                    if any(secret and secret in serialized for secret in known_secrets):
                        raise RuntimeError('credential present in browser failure diagnostic')
                    result['diagnostic'] = diagnostic
                    print('Browser failure diagnostic: ' + serialized, flush=True)
            results.append(result)
            failed = failed or run.returncode != 0
            print(f"{result['status']} real MCP UI on {browser}", flush=True)
    except Exception as exc:
        failed = True
        results.append({'status': 'ERROR', 'error_type': type(exc).__name__, 'stage': stage})
        print(f'ERROR browser setup or orchestration: {type(exc).__name__}', flush=True)
    finally:
        for process in reversed(processes):
            stop(process)
        summary = {'status': 'FAIL' if failed else 'PASS', 'phase': args.phase, 'platform': platform.system(), 'base_sha': BASE, 'head_sha': HEAD, 'results': results}
        (reports / 'browser-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
