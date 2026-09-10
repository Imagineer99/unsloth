# PR 10649: pinned native A/B evidence

Recommendation: targeted A/B CI was warranted; screenshots were not needed for this installer-only net diff. The optimization addresses real repeated work, but this validation does not support an unconditional safe-to-merge claim.

- BEFORE: `191b69c12b4434b5247f1fd7a455b4a760b169ae` (merge base).
- AFTER: `b675a9d70a9514bbdcb95b98fe41242313c0707b`.
- Harness: `8d1fbf2` on this branch.
- [Completed validation run](https://github.com/Imagineer99/unsloth/actions/runs/34470147480).
- [Earlier run](https://github.com/Imagineer99/unsloth/actions/runs/34470004660) exposed a Windows harness source-decoding error. The completed validation explicitly reads UTF-8; the source pins and expected outcomes did not change.

| Native runner | Architecture | BEFORE passing | AFTER passing |
|---|---|---:|---:|
| Ubuntu | x86_64 | 42/42 | 48/51 |
| Windows | AMD64 | 38/38 | 44/47 |
| macOS 15 | arm64 | 38/38 | 44/47 |

There were no harness exceptions in the completed validation. The three failed AFTER assertions are repeated on each runner; they are three distinct cases, not nine distinct bugs. Baseline assertions explicitly expect redundant dispatch, rather than treating any baseline error as reproduction evidence.

## Observed before and after

The real requirements step invokes the installer on every baseline pass. AFTER skips settled passes with valid evidence. Removing a real fixture dependency breaks its parent's import; both revisions restore that dependency and its import. Constraint and requirements changes trigger work; subsequent head passes settle back to skipping. The actual old/new manifest readers accept the opposite writer's compatible fixture, legacy evidence forces work, and interrupted passes remain incomplete.

The platform controls passed: Linux CPU/CUDA/ROCm family switching, Windows ROCm companion and index checks, and macOS MLX evidence invalidation. Plugin build artifacts no longer invalidate the fingerprint, while source edits do.

## Outstanding finding: warm Triton evidence bypasses payload validation

In `studio/install_python_stack.py`, `_triton_kernels_step` returns from `_skip_step(... extra_check=_ref_current)` before reaching `_payload_recorded_intact`. With unchanged requirements, warm step evidence, and matching git provenance, each of these fixtures dispatches zero installer calls on AFTER, versus one on BEFORE:

- a recorded internal module has been deleted;
- a recorded internal module has been truncated;
- the distribution's RECORD has been removed.

The corresponding no-evidence cases and the forced-pass control dispatch on AFTER. This isolates the inconsistent early skip. The manifest's deep payload scan covers the core distribution and its companion, not every optional Triton payload, so it is not a general substitute for this step's payload predicate.

The test proves bypassed payload validation and skipped repair dispatch. It does **not** prove baseline pip would successfully reinstall or recover every corrupted git distribution: the install call is recorded, not executed, in this fixture. Before claiming repair guarantees, apply the intact-payload condition consistently and verify actual repair/reinstallation semantics.

## Scope and limitations

`results/` preserves sanitized JSON and logs downloaded from the completed run. The native jobs use separate venvs and caches, real local wheel installation, and actual extracted installer step code. They are not full Studio installations. All tests ran under workspace temporary directories.

The Triton decision fixture deliberately selects its supported-path branch on every host to compare pure decision logic; it does not claim native Triton support on Windows or macOS. Git remote answers and MLX import subprocess results are controlled. Windows installer calls are captured, while its actual PowerShell guard and metadata probe execute.

No GPU kernels, browser sessions, complete Studio launch/update, or full OS-by-hardware Cartesian product were tested. No percentage of all possible cases is claimed. These pins describe the tested revisions only; later PR changes require delta review. No upstream product code, fork PR, or GitHub comment was published by this validation.
