# Disposable PR 10388 macOS validation

Do not merge this standalone validation branch. It deliberately contains only a harness and one branch-scoped workflow; it does not modify fork main or run unrelated repository workflows.

BEFORE: 919922c416a58e777d8f1afc4c8142e6070ae951
AFTER: 0a2be3584cfdb4357af3e1864cbe49574fb65b8b

The same five PR regression cases, two existing forwarding/ownership controls, and one real spawned-process queue test run against each source tree on macos-15 / Python 3.12. BEFORE must have exactly six assertion failures and two passes. AFTER must have eight passes. Setup errors, skips, timeouts, unexpected failures, and unexpectedly passing negative cases reject the proof.

The regression file is copied verbatim from the pinned PR head. Only the named eight cases run. No GPU, model download, browser, Studio launch, PR creation, artifact upload, or GitHub comment is involved. Results are printed in the Actions log.

The workflow uses immutable action SHAs, read-only contents permission, and no persisted checkout credentials.
