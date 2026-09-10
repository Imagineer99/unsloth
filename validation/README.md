# Targeted Actions validation

This workflow compares three immutable source revisions on Ubuntu, Windows and macOS:
- Base: 191b69c12b4434b5247f1fd7a455b4a760b169ae
- Reviewed head: b2d65068d7cbc9c5e3a5acf70c2c3600a97eadf7
- Advanced head observed when dispatching: f269003eab795a4c887ef1cab318f595d9e67f6d

The evidence-only PR targets feat/pr10648-review-evidence. No application code is changed and no unrelated upstream workflows run.

Expected outcomes:
1. Real Node/npm probe: all revisions pass. Base repeats both commands; both heads skip node -v on reuse. Deleting npm/lib/cli.js must make all revisions reject the install.
2. Publication-order requirement: base passes; both heads are expected to fail because the marker lookup returns release-1 while full publication ordering selects release-2. Responses are mocked and both releases are ordinary compatible candidates; this is resolver evidence, not an actual release download or install.
3. Original Node installer suite: base passes on all platforms. Both heads are expected to fail the new os.chown ownership test on Windows; POSIX cases should pass.

Failures remain red, rather than being reported as successful validations. Both probe and native-suite steps run and produce separate JUnit artifacts even when one fails. The workflow's final gate preserves the combined failure verdict.

Node 24 is installed by the official setup-node action; the exact Node version is printed with each probe result. Python is 3.12 and pytest is pinned to 9.1.1. All action versions are pinned by SHA, permissions are contents:read, and checkout credentials are not retained.

No GPU execution, WSL setup, Studio launch, or browser certification is claimed. No production fix is included.
