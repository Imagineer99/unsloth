# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Check identical tests against pinned source trees; reject unexpected failures."""
import argparse
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PINS = {'before': '919922c416a58e777d8f1afc4c8142e6070ae951', 'after': '0a2be3584cfdb4357af3e1864cbe49574fb65b8b'}
FAILURES = {
    *(f'test_direct_reader_discards_responses_from_released_requests[{kind}]' for kind in ('token','gen_done','gen_error','audio_done')),
    'test_direct_reader_drain_waits_for_its_own_terminal_response',
    'test_real_spawn_process_queue',
}
CONTROLS = {'test_rerouting_a_foreign_response_moves_worker_ownership', 'test_rerouting_a_foreign_gen_done_retires_that_request'}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', required=True)
    parser.add_argument('--after', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    harness = Path(__file__).resolve().parent
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    print(platform.platform(), sys.version, flush=True)
    for side, sha in PINS.items():
        source = Path(getattr(args, side)).resolve()
        actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        assert actual == sha, (side, actual, sha)
        env = os.environ.copy(); env['PYTHONPATH'] = str(source/'studio/backend')
        env['REVIEW_BACKEND'] = str(source/'studio/backend')
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        xml = output/f'{side}.xml'
        selectors = [str(harness/'test_regression.py')+'::'+name for name in (
            'test_direct_reader_discards_responses_from_released_requests',
            'test_direct_reader_drain_waits_for_its_own_terminal_response',
            'test_rerouting_a_foreign_response_moves_worker_ownership',
            'test_rerouting_a_foreign_gen_done_retires_that_request')]
        selectors.append(str(harness/'test_spawn.py')+'::test_real_spawn_process_queue')
        command = [sys.executable,'-m','pytest','-p','pytest_timeout','--noconftest','-q','--timeout=20',f'--junitxml={xml}',f'--basetemp={output/side}',*selectors]
        run = subprocess.run(command,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
        (output/f'{side}.log').write_text(run.stdout,encoding='utf-8')
        print(run.stdout, flush=True)
        assert run.returncode == (1 if side == 'before' else 0), (side, run.returncode)
        cases = ET.parse(xml).findall('.//testcase')
        assert len(cases) == 8
        assert {c.attrib['name'] for c in cases} == FAILURES | CONTROLS
        assert all(c.find('error') is None and c.find('skipped') is None for c in cases)
        failed = {c.attrib['name'] for c in cases if c.find('failure') is not None}
        assert failed == (FAILURES if side == 'before' else set()), (side,failed)
        for case in cases:
            failure = case.find('failure')
            if failure is not None:
                assert 'AssertionError' in ''.join(failure.itertext()), case.attrib['name']
                assert 'Timeout' not in ''.join(failure.itertext()), case.attrib['name']
        print(f'VERIFIED {side} {sha}: {len(cases)-len(failed)} passed; {len(failed)} expected assertion failures; zero errors/skips',flush=True)
    print('A/B PROOF PASSED',flush=True)

if __name__ == '__main__':
    main()
