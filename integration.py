# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""A green job requires head passes AND base fails on the intended numerical mismatch."""
import importlib.metadata
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from prepare import BASE, HEAD, ROOT, WORK

REGRESSION = 'tests/test_parallel_cp_compose_gloo.py::test_cpu_pipeline_eval_matches_one_process'
HEAD_TESTS = ['tests/test_pp_env_eval.py', 'tests/test_pp_env_cpu_chunked_ce.py', REGRESSION,
              'tests/test_pp_env_gloo.py::test_pp_env_trainer_matches_one_process[pp2]']


def run(side, tests):
    report = WORK / f'{side}.xml'
    command = [sys.executable, '-m', 'pytest', '-q', '-rs', *tests,
               f'--junitxml={report}', f'--basetemp={WORK / "tmp" / ("pytest-" + side)}']
    result = subprocess.run(command, cwd=WORK / side, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=600)
    (WORK / f'{side}.log').write_text(result.stdout)
    print(f'{side}: exit={result.returncode}\n' + '\n'.join(result.stdout.splitlines()[-8:]), flush=True)
    if not report.exists():
        raise AssertionError(f'{side}: pytest did not produce a report\n{result.stdout[-12000:]}')
    return result, ET.parse(report).getroot().findall('.//testcase')


def summarize(text):
    print(text, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(text + '\n')


if __name__ == '__main__':
    (WORK / 'tmp').mkdir(exist_ok=True)
    os.environ.update(UNSLOTH_FORCE_CPU='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
                      TMPDIR=str(WORK / 'tmp'), HF_HOME=str(WORK / 'hf-cache'))
    versions = {name: importlib.metadata.version(name) for name in
                ['torch', 'trl', 'transformers', 'unsloth-zoo', 'peft', 'datasets', 'accelerate']}
    summarize(f'CPU pipeline A/B\n\nBase: `{BASE}`\n\nHead: `{HEAD}`\n\n{platform.platform()}\n\n{versions}')
    result, cases = run('after', HEAD_TESTS)
    if result.returncode or len(cases) != 14 or any(c.find(tag) is not None for c in cases for tag in ['failure', 'error', 'skipped']):
        raise AssertionError(f'HEAD must pass all 14 tests without skips\n{result.stdout[-20000:]}')
    # Freeze Hub inputs once the head has populated the isolated cache.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    result, cases = run('before', [REGRESSION])
    assert result.returncode == 1 and len(cases) == 3, result.stdout[-20000:]
    values = []
    for case in cases:
        failure = case.find('failure')
        assert failure is not None and case.find('error') is None and case.find('skipped') is None, ET.tostring(case)
        text = failure.text or ''
        assert 'comparison failed' in text, text
        match = re.search(r'Obtained:\s*([0-9.eE+-]+).*?Expected:\s*([0-9.eE+-]+)', text, re.S)
        assert match, text
        old, reference = map(float, match.groups())
        assert math.isfinite(old) and math.isfinite(reference) and 0 < old < reference
        assert (reference - old) / reference > 0.01, (old, reference)
        values.append((case.attrib['name'], old, reference))
    summary = ['\nHead: **14 passed, zero skips**. Base: **3 intended numerical failures**.',
               '\n| Case | Base PP=2 | Single-process reference | Head PP=2 |',
               '|---|---:|---:|---|']
    summary += [f'| {name} | {old:.6f} | {reference:.6f} | Matches at rtol=1e-5 |' for name, old, reference in values]
    summary += ['\nBoth stages and identical evaluation rows are asserted by the regression.',
                '\nThe job succeeds only when both the positive and negative assertions hold.']
    summarize('\n'.join(summary))
    refs = WORK / 'hf-cache' / 'hub'
    for ref in sorted(refs.glob('models--*/refs/main')):
        print(f'MODEL_CONFIG_REVISION {ref.parent.parent.name} {ref.read_text().strip()}', flush=True)
