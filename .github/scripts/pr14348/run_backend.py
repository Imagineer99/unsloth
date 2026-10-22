"""Run pinned backend A/B and focused compatibility tests.

All subprocess state, pytest temporary data, bytecode caches and logs stay under
--output. Public reports exclude stdout/stderr, token files and application homes.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = '588e1c4ffefc7ee74a8474da2d4791ea2c44313b'
HEAD = 'bf573a6117540e6187cf8427e3971e97bd74636f'
TESTS = [
    'test_mcp_access_setting.py', 'test_mcp_access_route.py', 'test_mcp_gate.py',
    'test_mcp_server.py', 'test_mcp_prompts.py', 'test_mcp_upgrade_compat.py',
    'test_mcp_tool_contract.py',
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--head', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--phase', choices=('compatibility',), default='compatibility')
    parser.add_argument('--skip-suite', action='store_true', help='For package development only; hosted workflow does not use this.')
    args = parser.parse_args()
    source = {'base': args.base.resolve(), 'head': args.head.resolve()}
    for label, expected in (('base', BASE), ('head', HEAD)):
        actual = subprocess.check_output(['git', '-C', str(source[label]), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != expected:
            raise SystemExit(f'{label} SHA mismatch: expected {expected}, got {actual}')
        subprocess.run(['git', '-C', str(source[label]), 'diff', '--quiet', 'HEAD', '--'], check=True)
    output = args.output.resolve()
    private = output / 'private'
    reports = output / 'reports'
    state = output / 'state'
    for directory in (private, reports, state, output / 'tmp'):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1',
        'UNSLOTH_STUDIO_HOME': str(state / 'initial-home'),
        'UNSLOTH_STUDIO_DISABLE_DEVICE_PROBE': '1',
        'UNSLOTH_ALLOW_CPU': '1', 'UNSLOTH_IS_PRESENT': '1',
        'UNSLOTH_NVIDIA_LIBRARY_PROBE': '0', 'UNSLOTH_VULKAN_DEVICE_PROBE': '0',
        'UNSLOTH_DISABLE_SANDBOX_WARMUP': '1',
        'TEMP': str(output / 'tmp'), 'TMP': str(output / 'tmp'), 'TMPDIR': str(output / 'tmp'),
    })
    for key in ('UNSLOTH_STUDIO_ENABLE_MCP', 'UNSLOTH_STUDIO_MCP_TOKEN'):
        env.pop(key, None)
    scripts = Path(__file__).resolve().parent
    execution = {'phase': args.phase, 'base_sha': BASE, 'head_sha': HEAD, 'platform': platform.system(), 'python': platform.python_version(), 'results': []}
    versions = sorted([{'name': d.metadata['Name'], 'version': d.version} for d in importlib.metadata.distributions()], key=lambda d: d['name'].lower())
    (reports / 'environment.json').write_text(json.dumps({'python': platform.python_version(), 'platform': platform.platform(), 'packages': versions}, indent=2) + '\n', encoding='utf-8')

    def execute(name: str, command: list[str], cwd: Path) -> int:
        with (private / f'{name}.log').open('w', encoding='utf-8') as log:
            process = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        execution['results'].append({'name': name, 'exit_code': process.returncode})
        print(f"{'PASS' if process.returncode == 0 else 'FAIL'} {name} (exit {process.returncode})", flush=True)
        return process.returncode

    def probe(label: str, mode: str) -> int:
        name = f'{label}-{mode}'
        return execute(name, [sys.executable, str(scripts / 'backend_probe.py'),
            '--source', str(source[label]), '--revision', label, '--mode', mode,
            '--state-dir', str(state / name), '--report', str(reports / f'{name}.json')], scripts)

    failed = False
    for label, mode in (('base', 'feature'), ('head', 'feature'), ('head', 'compatibility')):
        failed = probe(label, mode) != 0 or failed
    if not args.skip_suite:
        junit = private / 'focused-suite.xml'
        command = [sys.executable, '-m', 'pytest', '-q', '--tb=short', '--timeout=120',
            f'--basetemp={state / "pytest-temp"}', '-o', f'cache_dir={state / "pytest-cache"}',
            f'--junitxml={junit}', *[f'tests/{name}' for name in TESTS]]
        code = execute('head-focused-suite', command, source['head'] / 'studio' / 'backend')
        failed = code != 0 or failed
        summary = {'status': 'PASS' if code == 0 else 'FAIL', 'exit_code': code, 'files': TESTS}
        if junit.exists():
            xml = ET.parse(junit).getroot()
            suites = [xml] if xml.tag == 'testsuite' else list(xml.iter('testsuite'))
            summary['counts'] = {key: sum(int(suite.attrib.get(key, 0)) for suite in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
            summary['failed_tests'] = [case.attrib.get('name') for case in xml.iter('testcase') if case.find('failure') is not None or case.find('error') is not None]
        (reports / 'head-focused-suite.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    execution['status'] = 'FAIL' if failed else 'PASS'
    (reports / f'{args.phase}-summary.json').write_text(json.dumps(execution, indent=2) + '\n', encoding='utf-8')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
