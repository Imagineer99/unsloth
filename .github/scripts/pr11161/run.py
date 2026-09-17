# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned A/B behavioral probe and optional real Studio frontend scene."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx

REFS = {'before': '38d4a4869ad3173ba7cad99682f5c8726e72f14d',
        'after': 'c7ed763b79d163eb295b65e2b343b856821920e7'}
parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--side', choices=REFS, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--ui', action='store_true')
args = parser.parse_args()
actual_sha = subprocess.check_output(['git', '-C', str(args.repo.resolve()), 'rev-parse', 'HEAD'], text=True).strip()
assert actual_sha == REFS[args.side], f'Wrong checkout: {actual_sha}'
if args.ui:
    stamp = args.repo / 'studio/frontend/dist/.uidiff_sha'
    assert stamp.read_text().strip() == REFS[args.side], 'Frontend must be built and stamped for this exact SHA'
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=True)
facts = {'side': args.side, 'expected_sha': REFS[args.side], 'cases': {},
         'limitations': ['Synthetic upstream and authentication; real production passthrough/monitor handlers.',
                         'Model selection, GPU inference and full Studio startup are outside this harness.']}


def until(fn, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Condition did not become true before deadline')


class Stream:
    def __init__(self, case, subject='alice'):
        self.case, self.subject = case, subject
        self.headers, self.text, self.error = {}, '', None
        self.done = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.drain, daemon=True)
        self.thread.start()

    def drain(self):
        try:
            with httpx.stream('POST', url + '/v1/chat/completions', headers={'x-fixture-subject': self.subject},
                              json={'model': 'default', 'stream': True,
                                    'messages': [{'role': 'user', 'content': self.case}],
                                    'tools': [{'type': 'function', 'function': {'name': 'lookup',
                                               'parameters': {'type': 'object', 'properties': {}}}}]},
                              timeout=30, trust_env=False) as response:
                response.raise_for_status()
                self.headers = dict(response.headers)
                for chunk in response.iter_text():
                    self.text += chunk
        except Exception as exc:
            self.error = repr(exc)
        finally:
            self.elapsed = time.monotonic() - self.started
            self.done.set()

    def join(self):
        assert self.done.wait(15), f'{self.case}: stream leaked'
        assert self.error is None, self.error
        return self.text


def get(path, subject='alice'):
    response = httpx.get(url + path, headers={'x-fixture-subject': subject}, timeout=10, trust_env=False)
    response.raise_for_status()
    return response.json()


def advance(case):
    response = httpx.post(url + '/__advance/' + case, timeout=5, trust_env=False)
    response.raise_for_status()


def row(case, subject='alice'):
    entry_id = get('/__state')[case]['monitor_id']
    return get('/api/inference/monitor/' + entry_id, subject)


def probe():
    expected = args.side == 'after'
    # Hold an ordinary stream during prefill; fetch actual monitor endpoint while it runs.
    stream = Stream('concurrent-alice')
    until(lambda: get('/__state').get(stream.case, {}).get('phase') == 'prefill-ready')
    entry = until(lambda: (r if (r := row(stream.case)).get('prompt_progress') else False)) if expected else row(stream.case)
    header = stream.headers.get('x-unsloth-monitor-id')
    assert (header == entry['id']) if expected else (header is None)
    assert entry.get('running_phase') == ('prompt_processing' if expected else None)
    assert entry.get('prompt_progress', {}).get('percent') == 60 if expected else 'prompt_progress' not in entry
    # A second active user must not see Alice, even with her exact returned ID.
    second = Stream('concurrent-bob', 'bob')
    until(lambda: get('/__state').get(second.case, {}).get('monitor_id'))
    bob_id = get('/__state')[second.case]['monitor_id']
    assert bob_id != entry['id']
    assert get('/api/inference/monitor', 'bob')['entries'][0]['id'] == bob_id
    for requester, other in [('bob', entry['id']), ('alice', bob_id)]:
        response = httpx.get(url + '/api/inference/monitor/' + other,
                             headers={'x-fixture-subject': requester}, trust_env=False)
        assert response.status_code == 404
    facts['cases']['prefill_and_isolation'] = dict(header=header, row=entry, distinct_request_ids=True, cross_subject_status=404)
    advance(stream.case)
    assert 'Fixture answer.' in stream.join()
    until(lambda: get('/__state').get(second.case, {}).get('phase') == 'prefill-ready')
    advance(second.case)
    assert 'Fixture answer.' in second.join()
    for case in ['legacy', 'tool', 'slow', 'frozen', 'stall']:
        stream = Stream(case)
        body = stream.join()
        entry = row(case)
        error = '"error"' in body
        if case in ('legacy', 'tool') or (case == 'slow' and expected):
            assert not error, body
            assert ('tool_calls' if case == 'tool' else 'Fixture answer.') in body
            assert '[DONE]' in body
            assert entry['status'] == 'completed', entry
            assert entry.get('running_phase') == ('token_generation' if expected else None), entry
        else:
            assert error, body
            assert stream.elapsed < 8, stream.elapsed
            assert entry['status'] == 'error', entry
        if case == 'stall':
            assert 'Fixture answer.' in body
        facts['cases'][case] = dict(elapsed=round(stream.elapsed, 3), error=error, row=entry,
                                  body=body, requested_progress=get('/__state')[case]['upstream_body'].get('return_progress', False))
    until(lambda: get('/api/inference/monitor')['active_requests'] == 0)
    until(lambda: get('/api/inference/monitor', 'bob')['active_requests'] == 0)
    until(lambda: all(record.get('closed') for record in get('/__state').values()))
    facts['active_requests_after_tests'] = 0
    facts['upstream_streams_closed_after_tests'] = True


def scene():
    from playwright.sync_api import sync_playwright, expect
    stream = Stream('scene')
    until(lambda: get('/__state').get('scene', {}).get('phase') == 'prefill-ready')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1050}, device_scale_factor=1,
                                          locale='en-GB', color_scheme='light')
            context.add_init_script("localStorage.setItem('unsloth_auth_token','fixture-only'); localStorage.setItem('unsloth_auth_refresh_token','fixture-only');")
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.goto(url + '/api-monitor', wait_until='domcontentloaded')
            try:
                scene_text = page.get_by_text('scene', exact=True)
                expect(scene_text).to_be_visible(timeout=20000)
                scene_text.click()
                # RequestDetail's direct wrapper, not an arbitrary ancestor of the page.
                detail = page.get_by_role('heading', name='POST /v1/chat/completions', exact=True).locator('..').locator('..')
                expected_label = 'Prompt processing · 60%' if args.side == 'after' else 'running'
                expect(detail.locator('header').get_by_text(expected_label, exact=True)).to_be_visible(timeout=10000)
                prefill = row('scene')
                detail.screenshot(path=str(out / 'prefill.png'), animations='disabled')
                facts['scene'] = {'prefill': prefill, 'prefill_label': expected_label, 'page_errors': errors}
                advance('scene')
                until(lambda: get('/__state')['scene'].get('phase') == 'generation')
                generated_label = 'Token generation' if args.side == 'after' else 'running'
                expect(detail.locator('header').get_by_text(generated_label, exact=True)).to_be_visible(timeout=10000)
                expect(detail.get_by_text('Fixture answer.', exact=True)).to_be_visible(timeout=10000)
                detail.screenshot(path=str(out / 'generation.png'), animations='disabled')
                facts['scene'].update(generation=row('scene'), generation_label=generated_label,
                                      dom_prefill_asserted=True, dom_generation_asserted=True)
                assert not errors, errors
            except Exception:
                page.screenshot(path=str(out / 'scene-failure.png'), full_page=True)
                (out / 'scene-failure.txt').write_text(page.locator('body').inner_text() + '\nERRORS: ' + repr(errors))
                raise
            finally:
                advance('scene')
                stream.join()
                context.close()
        finally:
            browser.close()


log = (out / 'server.log').open('w')
ready = out / 'ready.json'
if ready.exists():
    ready.unlink()
process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('server.py')), '--repo', str(args.repo.resolve()),
                            '--home', str(out / 'studio-home'), '--ready', str(ready)], stdout=log, stderr=subprocess.STDOUT)
try:
    until(lambda: ready.exists() or (process.poll() is not None and (_ for _ in ()).throw(RuntimeError('Server setup failed; see server.log'))), 30)
    identity = json.loads(ready.read_text())
    assert identity['sha'] == REFS[args.side], identity
    assert identity['pid'] == process.pid
    url = 'http://127.0.0.1:' + str(identity['port'])
    def healthy():
        try:
            return get('/__identity') == identity
        except httpx.ConnectError:
            return False
    until(healthy)
    facts['identity'] = identity
    probe()
    if args.ui:
        scene()
    facts['passed'] = True
    print(json.dumps({'side': args.side, 'passed': True, 'cases': list(facts['cases']), 'ui': args.ui}), flush=True)
finally:
    facts.setdefault('passed', False)
    (out / 'facts.json').write_text(json.dumps(facts, indent=2), encoding='utf-8')
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    log.close()
