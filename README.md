# PR9666 native macOS A/B validation

Prepared locally; not committed, pushed, run on macOS, or uploaded.

## Proposed execution

- Fork: `Imagineer99/unsloth` (authenticated identity and repository verified).
- Disposable branch: `fix/pr9666-native-mac-ab-20260908`.
- One branch-scoped GitHub Actions job on `macos-15` (Apple Silicon), capped at 75 minutes.
- No fork PR is needed: pushing this branch triggers the workflow.
- The branch contains only this validation package. It has independent history to avoid triggering the repository's unrelated workflows.
- Read-only repository permissions, SHA-pinned actions, no stored checkout credentials, no secrets, no GPU/model downloads, no installers or signing.
- The workflow retains generated screenshots, JSON measurements and build/test logs as the `pr9666-native-mac-ab` Actions artifact for 7 days. No profiles, binaries, caches or provider data are uploaded.
- Public PR comments, image hosting and upstream changes require separate review and approval after the artifacts exist.

## Pinned comparison and expected outcomes

BEFORE: `5c44c8c96b7fad0777cb6ea1240e5427f81e6fe4`.
AFTER: `70775b3bdf147d10e941306c17c25d51d98a4bca`.

Scene: `native_mac_interface_scale_and_toolbar`.

BEFORE has no Interface scale control. AFTER offers 50–200%, changes actual WKWebView.pageZoom, preserves 125% across reload, and resets to 100%. The production Navbar must remain clear of the actual native window-button frames in a 900×600-point window at 100/125/50/200%. Existing browser simulation predicts a 125% overlap, but native measurements determine the outcome.

This mounts the actual AppearanceTab, Navbar, SidebarProvider and SidebarInset components in a test-only Tauri binary built with each side's original locked dependencies and capabilities. The Mac chrome styles and appearance effect are extracted verbatim from the corresponding production provider. It exercises real Tauri zoom IPC and WKWebView, but bypasses the full Studio backend/setup lifecycle. It is not a complete Studio session or installer test.

Each side has its own detached checkout, native process, source-verified loopback frontend and WKWebView data-store UUID. The native store is managed by macOS on the disposable runner. All harness, build and report directories are inside the job workspace. Free ports are selected immediately before launch, and an identity endpoint must match the side, SHA and profile before the native window starts.

## Files

- `workflow.yml`: the proposed Actions file; installed as `.github/workflows/pr9666-native-mac.yml`.
- `manifest.json`: frozen SHAs, scene and original expectation.
- `prepare.py`: creates the controlled scene and test-only binary in disposable checkouts; no product fixes or dependency changes.
- `scene.tsx`: input, capture, boundary, reload and reset driver.
- `probe.rs`, `native.m`: native Tauri commands, WKWebView zoom, AppKit frame measurements and own-window screenshot capture.
- `run.py`: source fetch, isolated build/start/stop, provenance and result classification.
- `analyze.py`: compares measured native frames, validates identities and creates labelled composites without resizing source pixels.
- `test_analyze.py`: 15 local geometry and verdict tests.

## Result interpretation

- **Pass:** both sides complete; feature assertions pass; all measured head toolbar positions clear real native window buttons.
- **Regression:** baseline clears the buttons, but a completed head scene overlaps them. JSON records the affected controls and overlap area in square native points. The job exits 1 while retaining evidence.
- **Incomplete:** failure to build, launch, capture, establish source/profile identity, or reconcile DOM/native coordinates. Baseline failure also cannot establish a PR regression. Such failures exit 2 and must not be presented as negative A/B proof.

Expected artifacts include `appearance-before-after.png`, a tight `toolbar-before-after.png`, original window captures and per-capture JSON, `meta.json`, each side's provenance and result, and logs. Manual image inspection is explicitly pending; successful execution does not authorize posting the images.

Native close/minimize/zoom buttons are measured, not clicked. The probe can establish frame overlap; it cannot establish that a specific physical user click closes the app.

## Local validation completed

- Both generated scenes typechecked against the pinned sources and built successfully with Vite.
- Full scene automation completed in Chromium with an explicitly mocked native adapter, including head 50/125/200%, reload 125% and reset 100%. These records are harness checks, not native Mac evidence.
- All 15 geometry/verdict tests passed, including Retina point conversion, missing captures, wrong SHAs, shared profiles and rejecting a failing baseline as PR proof.
- Python syntax compilation, Rust parsing/formatting and workflow YAML/permissions checks passed.
- An independent static review found no actionable native adapter or coordinate-handling blocker.

Native macOS compilation, real window launch, screenshot permissions and measured geometry remain unrun. They require the proposed approved job. macOS 14+ is required for isolated WKWebView data-store identifiers; the job uses macOS 15.

The production PR worktree is unchanged. Local test-only modifications are confined to new disposable checkouts. No commits, remote branches, PRs, comments or uploads have been made.
