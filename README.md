# PR #10649: independent dependency-pass evidence

These are **local investigation results for one historical PR head**, not GitHub Actions results or a merge approval.

- [Upstream PR #10649](https://github.com/unslothai/unsloth/pull/10649)
- Tested head: [`2944f723f0ee9ef20732c1fabda8515c72cf1929`](https://github.com/unslothai/unsloth/commit/2944f723f0ee9ef20732c1fabda8515c72cf1929)
- Tested merge base: `191b69c12b4434b5247f1fd7a455b4a760b169ae`
- Investigation date: 2026-09-10.
- **The upstream PR advanced after testing. These results do not certify subsequent changes.**

## What the evidence establishes

The optimization addresses real redundant installer calls. In an eight-step sequence using tiny local wheels and the actual installer functions, base invoked installation eight times; head invoked it four times. Both repaired a deliberately removed transitive dependency. Head skipped the four settled steps and reran on changed constraints or requirement bytes.

The head probe seeds per-step evidence to isolate the gate. The base probe invokes the base install function unconditionally, matching the base requirements-step scheduling. This is not a complete Studio install, timing benchmark, ROCm wheel download measurement, or end-to-end upgrade test.

| Run | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Full installer test directory, WSL Ubuntu / Python 3.14.6 | 3,678 | 23 | 524 |
| Those exact 23 failures rerun on base | 0 | 23 | 0 |
| Eight changed test files, WSL / Python 3.11.15 | 882 | 13 | 16 |
| Core skip/manifest suites, native Windows / Python 3.12.14 | 228 | 1 | 0 |
| Independent decision and manifest probes, Python 3.11 | 112 | 3 | 0 |

Counts overlap and must not be added as unique coverage. Full and focused runs used 20-second per-test timeouts. All 23 full-suite failures also reproduced on base: six ROCm parity timeouts, twelve ROCm/progress cases, two missing-uvicorn cases, and three mixed-card diagnostic cases. The additional focused malformed-metadata failure also occurred on base and was unstable across isolated/suite runs. The Windows failure was a symlink-creation privilege error (`WinError 1314`). These failures do not establish a new runtime regression introduced by this PR.

The 112 independent passes comprise 108 mocked evidence decisions (four OS labels × three accelerator labels × nine evidence states) and four actual old/new manifest writer-reader combinations. Both manifest readers accept the tested additive format and reject a missing live completion marker.

## Three failing independent assertions: scope matters

1. **Triton missing payload, normal and forced pass (two assertions):** real metadata/RECORD files were present while the recorded package payload was absent; an unchanged remote commit was simulated. The current Triton helper skipped installation, including with `UNSLOTH_STUDIO_FULL_DEPS=1`. Its fallback is intentional in the PR's own tests. This contradicts an unconditional “every step reruns” interpretation, but does **not** prove that base pip/uv would repair that same damaged VCS installation. An installer invocation alone is not proof of a reinstall.
2. **Interruption from a live manifest (one assertion):** the real installer was interrupted at the bootstrap handoff after parking the live manifest. A subsequent planner accepted parked evidence when deep verification was stubbed to succeed. This isolates the lifecycle; it does not demonstrate a broken environment being reported complete. Ordinary completion verification still rejects the missing live marker.

These are contract/repair concerns, **not confirmed new user-visible regressions**. The probes intentionally retain their failing assertions so the observed behavior is reviewable.

## Files and provenance

- [results.json](results.json): per-test outcomes extracted from original JUnit files, plus SHA-256 hashes of those original files. Tracebacks, host paths, and raw logs are omitted. This is a derived local result record, not a third-party attestation.
- [offline-head.json](offline-head.json) and [offline-base.json](offline-base.json): eight local-wheel scenario outcomes on each version.
- [test_independent.py](test_independent.py): decision matrix, manifest compatibility, and contract probes.
- [offline_step_probe.py](offline_step_probe.py): real local-wheel install/import/repair sequence.

Published probes differ from the executed copies only by an SPDX header and configurable checkout-root paths. The fixture logic and assertions are unchanged.

## Reproduce on Linux/WSL

Work in a disposable directory containing these files. Check out the exact source revisions as sibling `head/` and `base/` directories:

```bash
git clone --filter=blob:none --no-checkout https://github.com/unslothai/unsloth.git head
git -C head fetch origin 2944f723f0ee9ef20732c1fabda8515c72cf1929 191b69c12b4434b5247f1fd7a455b4a760b169ae
git -C head checkout --detach 2944f723f0ee9ef20732c1fabda8515c72cf1929
git -C head worktree add --detach ../base 191b69c12b4434b5247f1fd7a455b4a760b169ae
mkdir -p temp
export TMPDIR="$PWD/temp"
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python pytest==9.1.1 packaging==26.3 pip==26.2.1
"$PWD/.venv/bin/python" -m pytest test_independent.py -q --basetemp=temp/independent
# Expected for the tested head: 112 passed, 3 failed.
```

Run each wheel sequence in its own **fresh** environment:

```bash
uv venv --python 3.11 .probe-head
uv pip install --python .probe-head/bin/python packaging==26.3 pip==26.2.1
"$PWD/.probe-head/bin/python" offline_step_probe.py head
uv venv --python 3.11 .probe-base
uv pip install --python .probe-base/bin/python packaging==26.3 pip==26.2.1
"$PWD/.probe-base/bin/python" offline_step_probe.py base
```

Package indexes are disabled inside the wheel probe; its fixture wheels are generated locally. Initial environment provisioning requires package access. To use existing checkouts, set `PR10649_CHECKOUT_ROOT` to the directory containing `head/` and `base/`. The wheel probe writes its fixture artifacts there.

For the broad run, the minimal environment included pytest 9.1.1, pytest-timeout 2.4.0, packaging 26.3, pip 26.2.1, PyYAML 6.0.3, typer 0.27.2, and structlog 26.1.0. From `head/`, run `python -m pytest tests/studio/install -v --timeout=20`, with a repository-local `--basetemp` and `--junitxml` destination. Timing-dependent and host-dependent outcomes can differ.

## Unexecuted coverage

No actual AMD GPU computation, native macOS, full Studio old-release upgrade/downgrade, training/inference, or Firefox/Safari/Chrome/Edge integration was run. OS/accelerator labels in the matrix are mocks, not support claims; macOS/NVIDIA, for example, is not certified by a passing label simulation. The physical host exposed an NVIDIA GPU through WSL, but this investigation did not exercise GPU kernels.

The evidence supports the ordinary skip/invalidation behavior. It cannot establish 99% coverage or guarantee that merging cannot break another pathway.
