"""Prepare two isolated Python environments and the two pinned frontend builds."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def interpreter(env: Path) -> Path:
    return env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('backend', 'browser'), required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    for label, expected in (
        ('base', '588e1c4ffefc7ee74a8474da2d4791ea2c44313b'),
        ('head', 'bf573a6117540e6187cf8427e3971e97bd74636f'),
    ):
        checkout = root / label
        actual = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != expected:
            raise SystemExit(f'{label} SHA mismatch before dependency installation')
        subprocess.run(['git', '-C', str(checkout), 'diff', '--quiet', 'HEAD', '--'], check=True)
    scripts = Path(__file__).resolve().parent
    private = root / 'run' / 'private'
    for directory in (private, root / 'tmp', root / 'cache' / 'pip', root / 'cache' / 'npm'):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({'PIP_CACHE_DIR': str(root / 'cache' / 'pip'), 'npm_config_cache': str(root / 'cache' / 'npm'),
                'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'TMPDIR': str(root / 'tmp'),
                'PYTHONDONTWRITEBYTECODE': '1'})

    def run(name, command, cwd=root):
        print(f'Preparing {name}', flush=True)
        with (private / f'{name}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([str(x) for x in command], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        if result.returncode:
            raise SystemExit(f'{name} failed (exit {result.returncode}); see private setup log on runner')

    venv = root / f'env-{args.mode}'
    run(f'{args.mode}-venv', [sys.executable, '-m', 'venv', venv])
    python = interpreter(venv)
    requirements = scripts / ('requirements.txt' if args.mode == 'backend' else 'requirements-runtime.txt')
    install = [python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input']
    if args.mode == 'browser':
        install.extend(['-r', root / 'head' / 'studio' / 'backend' / 'requirements' / 'studio.txt'])
    install.extend(['-r', requirements])
    run(f'{args.mode}-dependencies', install)
    if args.mode == 'browser':
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not npm:
            raise SystemExit('npm is required to build the pinned frontend sources')
        for label in ('base', 'head'):
            frontend = root / label / 'studio' / 'frontend'
            # No dependency lifecycle scripts; Vite uses the platform esbuild binary
            # supplied as an optional dependency, as in the existing compatibility CI.
            run(f'{label}-npm-ci', [npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=frontend)
            run(f'{label}-frontend-build', [npm, 'run', 'build'], cwd=frontend)
    summary = root / 'run' / 'reports'
    summary.mkdir(parents=True, exist_ok=True)
    (summary / f'setup-{args.mode}.json').write_text(json.dumps({'status': 'PASS', 'mode': args.mode, 'python': sys.version.split()[0]}, indent=2) + '\n', encoding='utf-8')
    print(f'PASS {args.mode} environment prepared', flush=True)


if __name__ == '__main__':
    main()
