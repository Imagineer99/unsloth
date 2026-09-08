// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
import assert from 'node:assert/strict';
import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const head = 'b4a60aeefd46e7573d35b10e2a3264589ef1d293';
const subject = resolve(process.argv[2]);
const output = resolve(process.argv[3]);
mkdirSync(output, { recursive: true });
const scripts = dirname(fileURLToPath(import.meta.url));
const git = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: subject, encoding: 'utf8' });
assert.equal(git.status, 0, git.stderr);
assert.equal(git.stdout.trim(), head, 'Wrong PR revision');
const relative = 'studio/frontend/src/features/settings/lib/example-model.ts';
const original = readFileSync(join(subject, relative), 'utf8');
const before = '...servability(option, pinned, keylessOnly, autoSwitch)';
const after = '...servability(option, pinned ?? null, keylessOnly, autoSwitch)';
assert.equal(original.split(before).length - 1, 1, 'Correction must match one call');
const fixed = join(output, 'fixed');
for (const path of [relative,
  'studio/frontend/src/features/settings/lib/example-model-id.ts',
  'studio/frontend/src/features/settings/components/agent-command.ts',
  'studio/frontend/tests/example-model.test.ts',
  'studio/frontend/tests/helpers/kit.ts',
  'studio/frontend/tests/bundler-resolver.mjs']) {
  mkdirSync(dirname(join(fixed, path)), { recursive: true });
  cpSync(join(subject, path), join(fixed, path));
}
writeFileSync(join(fixed, relative), original.replace(before, after));
const expected = [];
for (const kind of ['nonGGUF', 'standaloneGGUF', 'withheld']) {
  for (const keylessOnly of [false, true]) for (const autoSwitch of [false, true]) {
    if (kind === 'withheld' || keylessOnly || !autoSwitch)
      expected.push(`${kind}/${keylessOnly}/${autoSwitch}`);
  }
}
const report = { head, platform: process.platform, node: process.version, runs: [] };
for (const variant of ['A', 'B', 'A', 'B']) {
  const run = spawnSync(process.execPath,
    ['--experimental-strip-types', join(scripts, 'probe.mjs'), variant === 'A' ? subject : fixed],
    { encoding: 'utf8', timeout: 30000 });
  assert.equal(run.error, undefined, String(run.error));
  assert.equal(run.signal, null, 'Probe terminated');
  const result = JSON.parse(run.stdout); // Setup/import failures cannot count as reproduction.
  assert.equal(result.total, 80);
  assert.equal(run.status, variant === 'A' ? 1 : 0, run.stderr);
  if (variant === 'A') {
    assert.equal(result.passed, 70);
    assert.deepEqual(result.failures.map(f => `${f.kind}/${f.keylessOnly}/${f.autoSwitch}`).sort(), expected.slice().sort());
    for (const failure of result.failures) {
      assert.equal(failure.loaded, true);
      assert.equal(failure.selected, true);
      assert.equal(failure.expected, true);
      if (failure.kind === 'withheld') {
        assert.match(failure.error, /^TypeError:.*toLowerCase/);
      } else {
        assert.equal(failure.error, undefined);
        assert.equal(failure.result.model, 'org/model');
        assert.equal(failure.result.servable, false);
        assert.equal(failure.result.blockedBy, failure.keylessOnly ? 'keyless' : 'autoSwitchOff');
      }
    }
  } else {
    assert.equal(result.passed, 80);
    assert.deepEqual(result.failures, []);
  }
  report.runs.push({ variant, ...result });
  writeFileSync(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(`PASS ${variant}: ${result.passed}/80; exact expected behavior confirmed`);
}
const tests = spawnSync(process.execPath,
  ['--experimental-strip-types', '--test', join(fixed, 'studio/frontend/tests/example-model.test.ts')],
  { encoding: 'utf8', timeout: 30000 });
writeFileSync(join(output, 'existing-tests.log'), tests.stdout + tests.stderr);
assert.equal(tests.status, 0, tests.stdout + tests.stderr);
assert.equal(readFileSync(join(subject, relative), 'utf8'), original, 'A source must remain unchanged');
console.log('PASS existing resolver tests on B; A source unchanged');
if (process.env.GITHUB_STEP_SUMMARY) {
  writeFileSync(process.env.GITHUB_STEP_SUMMARY,
    `## PR #10313 deterministic A/B\n\nHead: ${head}\n\nOS: ${process.platform}; Node: ${process.version}\n\nA: exact 4 crashes + 6 false warnings reproduced twice.\n\nB: 80/80 pass twice; existing resolver suite passes.\n\nOnly correction: pinned ?? null at the servability call.\n`);
}
