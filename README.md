# PR #10649 targeted A/B validation

BEFORE is merge base `191b69c12b4434b5247f1fd7a455b4a760b169ae`.
AFTER is `b675a9d70a9514bbdcb95b98fe41242313c0707b`, inspected after the approved plan's head advanced with MLX imported-payload checks and plugin build-artifact exclusions. The plugin checks verify ignored build outputs and source-edit invalidation.

Three native runner jobs compare both revisions in separate venvs and package caches. Each executes unchanged source snippets for the requirements and Triton steps; the harness does not invent base/head scheduling. Real local wheels exercise install, import failure after removing a transitive dependency, repair, constraint changes, and settled skips. Minimal synthetic `unsloth`/`unsloth-zoo` fixture wheels permit the real manifest verifier to run. These are not full Studio installations.

Both sides must satisfy their explicit expected behavior. The redundant-install invariant records false on base and true on head; an unrelated setup failure is never accepted as negative proof. Compatibility controls require both sides to work. Newly guarded shell family changes intentionally differ between base and head.

Triton tests cover warm and absent evidence with real metadata/payload fixtures, while git remote answers and install invocation recording are controlled. MLX tests use real metadata/payload fixtures but intercept the import subprocess; no MLX computation is claimed. Windows tests execute the source ROCm guard and Python companion probe, intercepting only the actual installer command. Linux shell tests execute the extracted fast-path block.

The jobs publish sanitized JSON, logs, source commit identities, Python versions, and architecture. No GPU kernels, end-to-end Studio update, screenshots, or browsers are exercised. A red head assertion remains red; the workflow does not relabel product failures as success.

Local execution with existing pinned checkouts:

```bash
uv run --no-project --python 3.13 --with uv==0.12.1 python run.py --base /absolute/base --head /absolute/head --work ./temp/ab
```

Use a fresh `--work` directory. `--all-python-platforms` additionally exercises MLX helper logic on a non-macOS host for harness verification; that does not claim native macOS execution.
