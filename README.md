# Dataset PyArrow registration evidence

The composite was captured by a local Chromium/Playwright probe against two isolated Studio processes and homes.

- BEFORE: base `6abc0c71e1`; valid ChatML upload reaches Dataset Preview and returns the exact duplicate `Array2DExtensionType` error.
- AFTER: the same upload and browser assertions with the production patch applied; `/api/hub/datasets/check-format` returns 200, `Ready for training` is visible, and both fixture rows render.
- Viewport: 1280 x 900 per side.
- Frontend/backend: 127.0.0.1:5173 and 127.0.0.1:8888.
- Final PR verification: the identical regression test fails with the exact ArrowKeyError on merge base `0998656891` and passes on head `afaf2b3f3a`.

The screenshots predate a three-commit rebase onto the current merge base. Those intervening commits do not touch the warm-up cleanup or dataset preview path; the current-base negative control and head test provide commit-level verification.
