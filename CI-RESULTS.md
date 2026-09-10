# Native GitHub Actions A/B results

**Result: base green on all three operating systems; both tested PR heads reproduce the freshness mismatch and the Windows test-portability failure.**

[Completed run 34469651933](https://github.com/Imagineer99/unsloth/actions/runs/34469651933) · [Validation PR #83](https://github.com/Imagineer99/unsloth/pull/83) · [Machine-readable counts](ci-results.json)

Workflow commit: `5b13fa7` (source revisions are independently pinned).

- Base: `191b69c12b4434b5247f1fd7a455b4a760b169ae`
- Reviewed head: `b2d65068d7cbc9c5e3a5acf70c2c3600a97eadf7`
- Advanced head at dispatch: `f269003eab795a4c887ef1cab318f595d9e67f6d`

| Runner | Custom probes | Original Node suite |
|---|---|---|
| [base / ubuntu-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305225) | 2 pass, 0 fail | 84 pass, 0 fail, 0 skip |
| [base / windows-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305373) | 2 pass, 0 fail | 83 pass, 0 fail, 1 skip |
| [base / macos-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305504) | 2 pass, 0 fail | 84 pass, 0 fail, 0 skip |
| [reviewed-head / ubuntu-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305441) | 1 pass, 1 fail | 104 pass, 0 fail, 0 skip |
| [reviewed-head / windows-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305152) | 1 pass, 1 fail | 100 pass, 1 fail, 3 skip |
| [reviewed-head / macos-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305261) | 1 pass, 1 fail | 104 pass, 0 fail, 0 skip |
| [current-head / ubuntu-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305077) | 1 pass, 1 fail | 104 pass, 0 fail, 0 skip |
| [current-head / windows-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305287) | 1 pass, 1 fail | 100 pass, 1 fail, 3 skip |
| [current-head / macos-latest](https://github.com/Imagineer99/unsloth/actions/runs/34469651933/job/102846305231) | 1 pass, 1 fail | 104 pass, 0 fail, 0 skip |

## Interpretation

The real Node/npm probe passes in all nine cells. Base runs both version commands again on reuse. Both heads omit node -v while retaining npm validation. Deleting the copied npm/lib/cli.js makes every revision reject the damaged install. These are actual subprocesses, not mocked Node version responses.

The other custom probe checks the user-required newest-published-compatible semantics with synthetic release responses for a macOS host. Base uses publication ordering and returns release-2. Both heads' new marker lookup returns release-1 when GitHub Latest points there. This is the sole custom-probe failure in all six head cells. Running that resolver case on Windows/Linux does not claim the corresponding default OS selection route is identical to macOS; the mocked host is explicitly macOS in every cell. No release assets or model inference are involved.

The base Node suite passes everywhere. Both head suites pass on Linux/macOS. On Windows, their only failure is test_a_refreshed_marker_keeps_its_owner_and_group trying to monkeypatch nonexistent os.chown. The writer itself catches AttributeError; this remains a test-portability issue, not a demonstrated Windows installer failure.

Recommendation: retain the optimization, align the marker lookup with full compatible-release selection before merge, and make the ownership test portable. No production fix is included and no fixed-head validation is claimed.

## Evidence quality and scope

The first run, 34469473255, is excluded from A/B proof: the harness omitted the parent directory required by pytest basetemp. Commit 5b13fa7 creates it before testing; the corrected run has no fixture errors and all three base jobs pass. No product files changed to repair the harness.

Nine JUnit artifacts in the completed run retain separate custom-probe and original-suite results for 14 days. Job links and this summary provide stable references after artifact expiry. Counts include parameterized tests and must not be interpreted as a statistical coverage percentage.

This adds native Windows, macOS and Linux filesystem/process evidence. It does not certify GPUs, WSL, browsers, complete Studio updates, or every historical install. No GitHub review comment was posted.
