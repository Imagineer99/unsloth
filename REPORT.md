# PR #10648 investigation

**Verdict: real performance improvement; hold merge against the requested update semantics.**

PR: https://github.com/unslothai/unsloth/pull/10648
Reviewed head: b2d65068d7cbc9c5e3a5acf70c2c3600a97eadf7 (live head rechecked).
Pre-PR comparison base: 191b69c12b4434b5247f1fd7a455b4a760b169ae.
Fresh main checked for merge compatibility: d7bc72c1f965bfe14c6b6cf4a18df10eb0bf2f66.
Work: <review-root>/
No tracked production changes, commits, pushes, PR creation, or external comments.

## Before and after

Before: repeat Studio updates repeated release resolution and runtime validation even when nothing changed. Node ran both node -v and npm --version.
After: llama and whisper can answer from existing metadata plus release checks; uncertain cases return to selection. Node skips node -v on a matching record, but STILL RUNS npm --version. The pasted summary is outdated on that point.
The independent Node A/B simulation confirmed both probes on the base, only the npm probe on the second head check, and rejection of a failed npm probe on both revisions. These were instrumented probes, not measured startup timings.
The author's claimed 13–63-second macOS savings and approximately five-second Windows savings were not independently benchmarked here.

## Findings

### P2: latest lookup can suppress a newer published release

studio/install_llama_prebuilt.py:7747–7772, _expected_release_tag_without_plan.
The user explicitly requires newest published compatible release.
With two ordinary published releases, release-1 older and release-2 newer, and GitHub Latest pointing to release-1:
- Full publication ordering selects release-2.
- New marker lookup returns release-1.
- An already-current marker can therefore bypass the full selector, particularly a regression from the macOS path that disables the download-host shortcut.
- This need not be a one-update delay. GitHub permits make_latest=false or changing the designated latest release.
Independent test test_latest_pointer_must_not_hide_newer_publication fails with the exact old/new discrepancy. Routing the same case through the existing API publication-order path passes.
This is a resolver simulation, not an actual download/install of both release artifacts.

Recommended implementation direction: share the selector's newest-compatible-release semantics; do not use the designated Latest pointer alone as proof that an install is current. Preserve compatibility filtering and fall back to full selection when it cannot be established. The API escape path proves this specific discrepancy can be resolved, not that a complete implementation has been validated.

### P3: new Node test is not portable to Windows

tests/studio/install/test_install_node_prebuilt_logic.py:1236.
test_a_refreshed_marker_keeps_its_owner_and_group monkeypatches os.chown with default raising=True. Native Windows Python has no os.chown, so the test fails before exercising the writer.
Production catches AttributeError around os.chown, so this is a test defect, not evidence that Node installation breaks on Windows.
Fix direction: make the ownership test POSIX-only or inject the optional function with raising=False and explicitly test the missing-function path.

## Executed tests

| Check | Result |
|---|---|
| Four focused installer/core suites, WSL Linux Python 3.14.6 | 670 passed, 71 skipped |
| Ten additional selection/compatibility/setup suites, PR head | 621 passed, 10 skipped |
| Identical additional suites, pre-PR base | 621 passed, 10 skipped |
| Independent host-profile matrix | 12 passed |
| Independent newest-publication assertion | 1 failed, reproduced finding |
| Same publication scenario with API path | passed |
| Instrumented Node before/after probe | passed |
| Native Windows Python 3.12.11, Node suite | 100 passed, 3 skipped, 1 failed (test portability above) |

The 1,291 passing PR repository tests cover different focused/additional suites; reruns and base results are not added to that total.
Initial collection failed because the isolated invocation omitted shared test helpers. The first broader run lacked uvicorn/structlog and showed a transient combined Node-path assertion. After installing those test dependencies, the entire broader batch passed on both base and head. No product fix was needed for those failures.
WSL skips include missing PowerShell and an unavailable torch-dependent ROCm suite. Three static skip warnings are justified POSIX execute-bit/mode tests.
Windows ran natively against fixtures on the WSL UNC filesystem; this is not an NTFS-only certification.
uv environments and fixtures were kept under the review temp directory.

## Compatibility and confidence limits

The requested Windows/Linux/WSL/Mac × NVIDIA/AMD/CPU matrix was exercised only for host-profile serialization and hardware-change sensitivity. The broader repository tests simulate many backend routing, driver, architecture, pin, payload, old-marker, and fallback conditions. They do not establish actual GPU execution across all 12 combinations. A Mac/NVIDIA profile test does not imply a supported CUDA runtime on macOS.
Old marker and kept-install suites passed; missing llama evidence takes the full path and metadata backfill enables subsequent reuse. This supports tested old-install compatibility, not every historical version or future marker schema.
No new GPU backend is introduced. Existing runtime pathways remain, but update freshness is observably changed.
No frontend files changed. No Firefox, Safari, Chrome, or Edge sessions were launched. Browser regressions were not demonstrated; browser certification is unproven. Existing PR UI CI passes are supplementary evidence, not independently executed browser coverage.
No real GPU inference, native macOS run, full Studio install/update benchmark, or 99% coverage claim is made.
A uv venv isolates Python dependencies; it is not an OS security sandbox.

## Existing CI and mechanical checks

Two red CI jobs were inspected:
- Windows compiler detector: positive control compiled a type but the detector recorded no compiler process. Harness failure before the intended assertion. https://github.com/unslothai/unsloth/actions/runs/34450786957/job/102785812772
- Core latest HF/TRL: unsloth_zoo @ main tests reported upstream source-pattern drift and missing SFTTrainer._tp_size. Logs implicate external/latest dependency integration, not these installer edits; no separate base rerun of this CI job was performed. https://github.com/unslothai/unsloth/actions/runs/34450787096/job/102785812511

Pre-execution static scan: no static findings. Net production diff inspected.
Fresh-base merge-tree check: clean, no accidental reverts identified.
No new workflows or dependency manifests changed.

## Authoritative references

GitHub release API documents make_latest=true/false/legacy; Latest is not guaranteed to equal newest published:
https://docs.github.com/en/rest/releases/releases?apiVersion=latest
Python documents cross-platform replacement and platform-specific OS operations:
https://docs.python.org/3/library/os.html

## Published evidence files

See README.md for the published, sanitized subset. Raw metadata, CI logs, XML containing local paths, environments and installation fixtures are not published.

Next stage should fix the release-order mismatch and portable test, preserve these repros, and run native macOS plus real supported GPU/Studio integration checks. Investigation-only scope means no production fixes were applied.
