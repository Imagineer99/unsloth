# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Review-only property probes of the exact before/after function with real torch tensors."""
import ast
import itertools
import json
import pathlib
import platform
import sys
import types

import torch
from packaging.version import Version

root = pathlib.Path(sys.argv[1])

def load(name):
    tree = ast.parse((root / name).read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_eval_items')
    scope = {'torch': torch}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), name, 'exec'), scope)
    return scope['_eval_items']

before, after = load('base_pp_env.py'), load('head_pp_env.py')
trl = types.ModuleType('trl')
sys.modules['trl'] = trl
checks = changed = 0
for version, marked, average, getter_kind, batch, length, seed in itertools.product(
    ['0.18.2', '1.14.0', '1.15.0', '1.15.0.post1', '1.16.0'],
    [False, True], [False, True], ['normal', 'none', 'raises', 'scalar'],
    [1, 2, 5], [1, 2, 7, 32], range(4),
):
    trl.__version__ = version
    g = torch.Generator().manual_seed(seed)
    labels = torch.randint(0, 13, (batch, length), generator=g)
    labels[torch.rand((batch, length), generator=g) < seed / 3] = -100
    original = labels.clone()
    def compute_loss(self):
        raise AssertionError('must not execute trainer loss')
    if marked:
        compute_loss._unsloth_cpu = True
    def getter(self, batches, device):
        assert self.args.average_tokens_across_devices is False
        if getter_kind == 'raises':
            raise RuntimeError('probe fallback')
        count = (batches[0]['labels'] != -100).sum()
        return int(count) if getter_kind == 'scalar' else count
    cls = type('ProbeTrainer', (), {'compute_loss': compute_loss})
    if getter_kind != 'none':
        cls._get_num_items_in_batch = getter
    trainer = cls()
    trainer.args = types.SimpleNamespace(device=torch.device('cpu'), average_tokens_across_devices=average)
    expected = int((labels[:, 1:] != -100).sum()) if marked and Version(version) >= Version('1.15.0') or getter_kind in ['none', 'raises'] else int((labels != -100).sum())
    a = after(trainer, {'labels': labels}, labels)
    b = before(trainer, {'labels': labels}, labels)
    assert a == expected, (version, marked, getter_kind, labels, a, expected)
    assert trainer.args.average_tokens_across_devices is average
    assert torch.equal(original, labels)
    if not (marked and Version(version) >= Version('1.15.0')):
        assert a == b
    changed += a != b
    checks += 1

# Causal CE oracle: each eligible target belongs to its preceding logit.
numeric_checks = 0
for batch, length, seed in itertools.product([1, 2, 5], [2, 7, 32], range(8)):
    g = torch.Generator().manual_seed(seed)
    logits = torch.randn(batch, length, 13, generator=g, dtype=torch.float64)
    labels = torch.randint(0, 13, (batch, length), generator=g)
    labels[torch.rand((batch, length), generator=g) < seed / 8] = -100
    targets = labels[:, 1:]
    count = int((targets != -100).sum())
    total = torch.nn.functional.cross_entropy(logits[:, :-1].reshape(-1, 13), targets.reshape(-1), reduction='sum')
    corrected = total / max(count, 1)
    terms = [-torch.log_softmax(logits[b, t-1], -1)[labels[b, t]] for b in range(batch) for t in range(1, length) if labels[b, t] != -100]
    oracle = torch.stack(terms).mean() if terms else torch.tensor(0.0)
    torch.testing.assert_close(corrected, oracle.to(torch.float64), rtol=1e-12, atol=1e-12)
    numeric_checks += 1
print(json.dumps({'platform': platform.platform(), 'python': platform.python_version(), 'torch': torch.__version__, 'function_cases_passed': checks, 'cases_with_intended_change': changed, 'independent_ce_oracles_passed': numeric_checks}, indent=2))
