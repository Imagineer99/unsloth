# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Materialize pinned product revisions with identical head-side regression tests."""
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

BASE = '10c52ed463591a8db375cb4b1d45c3361a1fa055'
HEAD = '4977671e07361881b6eb32355a39459ed610ba7c'
ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'work'


def git(*args):
    return subprocess.check_output(['git', '-C', str(WORK / 'upstream'), *args], text=True).strip()


if __name__ == '__main__':
    WORK.mkdir(exist_ok=True)
    subprocess.run(['git', 'init', '-b', 'fix/pr17291-pinned-input', str(WORK / 'upstream')], check=True)
    remote = os.environ.get('REPRO_LOCAL_GIT', 'https://github.com/unslothai/unsloth.git')
    git('fetch', '--depth=2', remote, HEAD)
    assert git('rev-parse', 'FETCH_HEAD') == HEAD
    assert git('rev-parse', f'{HEAD}^') == BASE
    assert git('diff', '--name-only', BASE, HEAD, '--', 'unsloth') == 'unsloth/models/pp_env.py'
    for label, sha in [('before', BASE), ('after', HEAD)]:
        target = WORK / label
        target.mkdir(exist_ok=True)
        archive = WORK / f'{label}.tar'
        git('archive', '--format=tar', f'--output={archive}', sha, 'unsloth', 'tests', 'pyproject.toml')
        with tarfile.open(archive) as source:
            source.extractall(target, filter='data')
        filename = 'base_pp_env.py' if label == 'before' else 'head_pp_env.py'
        shutil.copy2(target / 'unsloth/models/pp_env.py', WORK / filename)
    # Only test code comes from the head on the before side. Production is exact BASE.
    for path in ['tests/test_parallel_cp_compose_gloo.py', 'tests/test_pp_env_eval.py']:
        shutil.copy2(WORK / 'after' / path, WORK / 'before' / path)
    print(f'PINNED_BASE={BASE}\nPINNED_HEAD={HEAD}', flush=True)
