# PR #10648: investigation evidence

**New: [native Linux/Windows/macOS Actions A/B results](CI-RESULTS.md).**

Review target: [unslothai/unsloth#10648](https://github.com/unslothai/unsloth/pull/10648), commit `b2d65068d7cbc9c5e3a5acf70c2c3600a97eadf7`.
Comparison base: `191b69c12b4434b5247f1fd7a455b4a760b169ae`.

**Verdict: a real optimization, with a reproduced update-freshness mismatch and a Windows test-portability failure.** No production fix is included.

Read [the investigation report](REPORT.md). Evidence was collected locally, not by a fork Actions run. Logs are sanitized; local paths are replaced with placeholders. Test counts are not a statistical coverage guarantee.

| Evidence | Result |
|---|---|
| [Focused installer/core suites](focused.log) | 670 passed, 71 skipped |
| [Additional head compatibility suites](extended-final.log) | 621 passed, 10 skipped |
| [Identical base compatibility suites](base-extended.log) | 621 passed, 10 skipped |
| [Independent publication/profile probes](independent.log) | Publication-order assertion fails; 12 profile cases pass |
| [Native Windows Node suite](windows-node.log) | 100 passed, 3 skipped; ownership test fails because os.chown is absent |
| [Node base/head comparison](ab.json) | Second head check saves node -v; npm failure rejected on both |
| [API-path experiment](policy.log) | Same publication scenario passes; no production edit |

## Reproduce the independent probes

Use a fresh local clone of this evidence branch. Run these commands from its root on Linux/WSL; all checkout and test files remain below this directory. Python 3.14.6 and pytest 9.1.1 were used for the recorded Linux run.

```bash
uv venv --python 3.14.6 .venv
uv pip install --python .venv/bin/python pytest==9.1.1
git clone --no-checkout https://github.com/unslothai/unsloth.git head
git -C head checkout --detach b2d65068d7cbc9c5e3a5acf70c2c3600a97eadf7
git -C head worktree add --detach ../base 191b69c12b4434b5247f1fd7a455b4a760b169ae
.venv/bin/python -m pytest test_independent.py -q --basetemp=./temp/independent
# Expected: one failure showing release-1 versus release-2, and 12 passes.
.venv/bin/python probe_ab.py
.venv/bin/python probe_policy.py
```

The publication probe compares the real full-ordering function with the new lookup using mocked release responses. It does not install release binaries. The API experiment changes only an in-process lookup switch, not production files. The 12 host-profile cases do not exercise actual GPU kernels or establish support for every hypothetical OS/GPU combination.

To reproduce the focused suite, from this directory:

```bash
cd head
PYTHONPATH=tests/_shared ../.venv/bin/python -m pytest \
  tests/studio/install/test_install_llama_prebuilt_logic.py \
  tests/studio/install/test_install_node_prebuilt_logic.py \
  tests/studio/install/test_install_whisper_prebuilt_logic.py \
  tests/studio/install/test_prebuilt_core.py \
  --basetemp=../temp/focused -q
```

Native Windows reproduction uses Python 3.12.11 and pytest 9.1.1. From a Windows environment, run the Node test file at the pinned head with `--confcutdir=head/tests/studio/install` and a basetemp under this directory. The recorded run used a WSL UNC filesystem; it is not an NTFS-only certification.

## Scope

No native macOS run, full Studio install/update benchmark, real GPU inference matrix, or browser certification is claimed. No workflows, credentials, environments, downloaded binaries, or private artifacts are included in this branch.
