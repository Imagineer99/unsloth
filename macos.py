# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""macOS portability evidence for the exact helper, not full pipeline execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

from prepare import BASE, HEAD, ROOT, WORK

if __name__ == '__main__':
    result = subprocess.run([sys.executable, str(ROOT / 'normalization_probe.py'), str(WORK)],
                            check=True, capture_output=True, text=True, timeout=120)
    facts = json.loads(result.stdout)
    assert facts['function_cases_passed'] == 3840
    assert facts['independent_ce_oracles_passed'] == 72
    assert facts['cases_with_intended_change'] > 0
    summary = (f'macOS normalization-helper check\n\nBase: `{BASE}`\n\nHead: `{HEAD}`\n\n'
               f'```json\n{json.dumps(facts, indent=2)}\n```\n\n'
               'Scope: exact functions extracted by AST, native CPU tensors, simulated trainer/version '
               'adapters, independent cross-entropy oracle. This does not certify full SFTTrainer, '
               'Gloo pipeline, MLX, or GPU execution on macOS.\n')
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(summary)
