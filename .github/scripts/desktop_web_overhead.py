"""Windows release-mode UI experiment, not a model throughput benchmark.

Measures private committed bytes (additive) and CPU seconds for owned process
trees. Working sets are diagnostic only: summing them double-counts shared pages.
Both renderers have CDP enabled and are driven by the same external controller.
Never publish a performance conclusion from an incomplete run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import statistics
import subprocess
import time
import urllib.request

import psutil
from playwright.sync_api import sync_playwright

OUT = Path('benchmark-results')
OUT.mkdir(exist_ok=True)
CLI = Path.home() / '.unsloth/studio/unsloth_studio/Scripts/unsloth.exe'
APP = Path('studio/src-tauri/target/release/unsloth-studio.exe').resolve()
EDGE = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
BENCH_PASSWORD = secrets.token_hex(24)


def auth_post(path, payload, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request('http://127.0.0.1:8888/api/auth/' + path,
                                     data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class Tree:
    def __init__(self):
        self.known = {}
        self.cpu = {}

    def add(self, pid):
        p = psutil.Process(pid)
        self.known[(p.pid, p.create_time())] = p

    def live(self):
        for p in list(self.known.values()):
            try:
                if not p.is_running():
                    continue
                for c in p.children(recursive=True):
                    self.known[(c.pid, c.create_time())] = c
            except psutil.NoSuchProcess:
                pass
        return [p for p in self.known.values() if p.is_running()]

    def sample(self):
        private = working = 0
        processes = []
        for p in self.live():
            try:
                mem = p.memory_info()
                cpu = p.cpu_times()
                self.cpu[(p.pid, p.create_time())] = cpu.user + cpu.system
                private += mem.private
                working += mem.rss
                processes.append({'pid': p.pid, 'name': p.name(), 'private_bytes': mem.private})
            except psutil.NoSuchProcess:
                continue
        return {'time': time.monotonic(), 'private_bytes': private,
                'working_set_bytes_nonadditive': working,
                'cpu_seconds': sum(self.cpu.values()), 'processes': processes}

    def stop(self):
        live = self.live()
        for p in reversed(live):
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(live, timeout=10)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass


def spawn(command, tree, label, env=None):
    # No terminal window; the actual benchmark UI must remain headed.
    with (OUT / f'{label}.log').open('wb') as log:
        p = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                             env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    tree.add(p.pid)
    return p


def wait_url(url, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.load(response)
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f'Endpoint did not become ready: {url}')


def attach(pw, port):
    wait_url(f'http://127.0.0.1:{port}/json/version')
    return pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')


def chat_ready(page):
    page.wait_for_url('**/chat**', timeout=180000)
    page.locator('textarea').first.wait_for(state='visible', timeout=180000)
    page.bring_to_front()


def backend_ready(tree):
    # These trials own a clean runner and request port 8888. A fallback to a
    # different server/port indicates isolation failure, not a valid sample.
    wait_url('http://127.0.0.1:8888/api/health', seconds=10)
    if not any(p.name().lower().startswith('python') for p in tree.live()):
        raise RuntimeError('No Python backend in the measured process tree')


def measure(page, tree, seconds, typing=False):
    rows = [tree.sample()]
    start = time.monotonic()
    i = 0
    while time.monotonic() - start < seconds:
        if typing:
            page.locator('textarea').first.fill('Benchmark draft ' + ('hello ' * (1 + i % 20)))
        page.wait_for_timeout(1000)
        rows.append(tree.sample())
        i += 1
    if typing:
        page.locator('textarea').first.fill('')
    elapsed = rows[-1]['time'] - rows[0]['time']
    return {'median_private_mib': statistics.median(r['private_bytes'] for r in rows) / 2**20,
            'peak_private_mib': max(r['private_bytes'] for r in rows) / 2**20,
            'cpu_seconds_per_second': (rows[-1]['cpu_seconds'] - rows[0]['cpu_seconds']) / elapsed,
            'samples': rows}


def trial(pw, mode, index, seconds, tokens):
    label = f'{index}-{mode}'
    tree = Tree()
    browser = None
    page = None
    baseline = None
    try:
        print(f'START {label}', flush=True)
        start = time.monotonic()
        if mode == 'desktop':
            env = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='--remote-debugging-port=9222')
            spawn([str(APP)], tree, label, env)
            browser = attach(pw, 9222)
            deadline = time.monotonic() + 60
            while not browser.contexts[0].pages:
                if time.monotonic() > deadline:
                    raise RuntimeError('Desktop has no WebView page')
                time.sleep(.5)
            page = browser.contexts[0].pages[0]
            chat_ready(page)
            tokens = page.evaluate('''() => ({
                unsloth_auth_token: localStorage.getItem('unsloth_auth_token'),
                unsloth_refresh_token: localStorage.getItem('unsloth_refresh_token')
            })''')
            if not tokens['unsloth_auth_token']:
                raise RuntimeError('Desktop did not authenticate')
            if index == 'warmup':
                # Use the supported desktop account-setup endpoint, then normal
                # password login for browser trials. Desktop tokens alone do not
                # satisfy the browser's first-password routing check.
                fresh = auth_post('desktop-initial-password',
                                  {'new_password': BENCH_PASSWORD}, tokens['unsloth_auth_token'])
                tokens = {'unsloth_auth_token': fresh['access_token'],
                          'unsloth_refresh_token': fresh['refresh_token']}
                page.evaluate('(tokens) => {for (const [k,v] of Object.entries(tokens)) localStorage.setItem(k,v)}', tokens)
        else:
            profile = (OUT / f'edge-profile-{label}').resolve()
            spawn([str(EDGE), '--remote-debugging-port=9223', f'--user-data-dir={profile}',
                   '--no-first-run', '--no-default-browser-check', '--window-size=1280,900',
                   'about:blank'], tree, label + '-browser')
            browser = attach(pw, 9223)
            context = browser.contexts[0]
            page = context.pages[0]
            if mode == 'web-existing':
                page.wait_for_timeout(10000)
                baseline = measure(page, tree, 10)
                start = time.monotonic()
            spawn([str(CLI), 'studio', '-H', '127.0.0.1', '-p', '8888'], tree, label + '-backend')
            wait_url('http://127.0.0.1:8888/api/health')
            fresh = auth_post('login', {'username': 'unsloth', 'password': BENCH_PASSWORD})
            if fresh.get('must_change_password'):
                raise RuntimeError('Benchmark account setup did not complete')
            tokens = {'unsloth_auth_token': fresh['access_token'],
                      'unsloth_refresh_token': fresh['refresh_token']}
            context.add_init_script('const tokens = ' + json.dumps(tokens) +
                                    '; for (const [k,v] of Object.entries(tokens)) localStorage.setItem(k,v);')
            if mode == 'web-existing':
                page = context.new_page()
            page.goto('http://127.0.0.1:8888/chat', wait_until='domcontentloaded')
            chat_ready(page)
        ready_seconds = time.monotonic() - start
        session = page.context.new_cdp_session(page)
        session.send('Emulation.setDeviceMetricsOverride', {
            'width': 1280, 'height': 800, 'deviceScaleFactor': 1, 'mobile': False})
        page.wait_for_timeout(30000)
        backend_ready(tree)
        print(f'READY {label}; collecting idle and composer samples', flush=True)
        page.screenshot(path=str(OUT / f'{label}.png'))
        result = {'mode': mode, 'round': index, 'ready_seconds': ready_seconds,
                  'url': page.url, 'viewport': page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})'),
                  'user_agent': page.evaluate('navigator.userAgent'),
                  'idle': measure(page, tree, seconds),
                  'typing': measure(page, tree, seconds, typing=True)}
        backend_ready(tree)
        chat_ready(page)
        if baseline:
            result['blank_browser_baseline'] = baseline
            result['idle_incremental_private_mib'] = result['idle']['median_private_mib'] - baseline['median_private_mib']
        (OUT / f'{label}.json').write_text(json.dumps(result, indent=2))
        print(f'PASS {label}', flush=True)
        return result, tokens
    except Exception:
        if page:
            try:
                page.screenshot(path=str(OUT / f'{label}-failed.png'))
                (OUT / f'{label}-failed.txt').write_text(page.locator('body').inner_text())
            except Exception:
                pass
        raise
    finally:
        # Include all discovered descendants before disconnecting the controller.
        tree.live()
        if browser:
            try:
                browser.close()
            finally:
                tree.stop()
        else:
            tree.stop()
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    for path in (APP, CLI, EDGE):
        if not path.is_file():
            raise RuntimeError(f'Missing required executable: {path}')
    metadata = {'sha': os.environ.get('GITHUB_SHA'), 'platform': platform.platform(),
                'cpu_count': psutil.cpu_count(), 'ram_bytes': psutil.virtual_memory().total,
                'app_sha256': hashlib.sha256(APP.read_bytes()).hexdigest(),
                'scope': 'No model loaded; no-torch backend; release-mode unsigned desktop; headed Edge; CDP enabled on both'}
    (OUT / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    results = []
    with sync_playwright() as pw:
        # First launch setup/auth and caches are excluded from reported trials.
        _, tokens = trial(pw, 'desktop', 'warmup', 2, {})
        orders = [('desktop', 'web-fresh', 'web-existing'),
                  ('web-existing', 'web-fresh', 'desktop'),
                  ('web-fresh', 'desktop', 'web-existing')]
        for index in range(args.rounds):
            for mode in orders[index % len(orders)]:
                result, tokens = trial(pw, mode, index, args.seconds, tokens)
                results.append(result)
    lines = ['# Windows UI overhead experiment', '',
             metadata['scope'], '',
             'Private MiB includes the backend and every tracked app/browser descendant. CPU is cores consumed (CPU seconds / wall second).', '',
             '| Mode | Idle private MiB median [min, max] | Idle CPU cores median | Typing CPU cores median |',
             '|---|---:|---:|---:|']
    for mode in ('desktop', 'web-fresh', 'web-existing'):
        group = [r for r in results if r['mode'] == mode]
        mem = [r['idle']['median_private_mib'] for r in group]
        idle = statistics.median(r['idle']['cpu_seconds_per_second'] for r in group)
        typing = statistics.median(r['typing']['cpu_seconds_per_second'] for r in group)
        lines.append(f'| {mode} | {statistics.median(mem):.1f} [{min(mem):.1f}, {max(mem):.1f}] | {idle:.4f} | {typing:.4f} |')
    lines += ['', 'Hosted-runner exploratory evidence only. No claim about model performance, GPUs, other browsers/OSes, or a universal winner.',
              'Startup observations include automation attachment and are diagnostic, not precise startup benchmarks.',
              'Existing-browser incremental memory and raw process samples are in the per-trial JSON files. Blank-browser baseline does not represent a typical multi-tab session.']
    report = '\n'.join(lines)
    (OUT / 'summary.md').write_text(report)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(report)
    print(report)


if __name__ == '__main__':
    main()
