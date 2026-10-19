# Issue 14120 targeted validation

Prepared locally; no workflow has run on GitHub.

- Before: `2fee36ffa352bfb36f331d92bb81e7ee0c408acb`
- Fix: `f30f104da391f58762a93014063e276466437966`
- Product branch: `fix/metal-alongside-admission`
- Disposable validation branch: `fix/issue-14120-metal-repro`
- Runner: `macos-15-xlarge`, one job, maximum 30 minutes.
- Eligible organization repository required. GitHub larger runners are unavailable to the personal `Imagineer99/unsloth` fork. Destination remains to be agreed.

`run_ab.py` checks full commit IDs and runs identical tests from the fixed tree on both implementations. Baseline must fail exactly seven named assertions, including the real-loader-to-eviction route test; the four controls pass. The fixed tree must pass all eleven. Collection errors, skipped tests, unrelated assertions and timeouts do not count as a reproduced bug.

`metal_probe.py` uses the real 270M Gemma GGUF header and fitter. It injects a budget smaller than the weights and intercepts server launch on both sides, so the negative case never allocates the oversized model. The control really loads the small model through Studio's backend, verifies positive Metal offload in the runtime log, and generates text.

GitHub exposes virtualized hardware. The probe records the device and Studio's virtualization detection. When virtualized, both sides explicitly set `UNSLOTH_ALLOW_PARAVIRTUAL_METAL=1` to exercise Metal rather than Studio's normal VM CPU fallback. This is accelerated VM evidence, not a reproduction of the physical M4 Max panic or the exact four-model workload. Failure to expose usable Metal or to offload positively is a setup/capability failure, never a passing GPU test.

Runtime release: `unslothai/llama.cpp` tag `b11719-mix-e614faa`, installed by the checked-in installer. Model: `unsloth/gemma-3-270m-it-GGUF`, revision `c90975dbd40c0c7b275fefaae758c3415c906238`, file `gemma-3-270m-it-Q4_K_M.gguf`. Runtime installer compatibility and the Mac-only probe remain unverified until the approved run. The model digest, device info, commit IDs and test results are recorded. Dependencies are locked for Python 3.12 / macOS 15 arm64.

The workflow triggers only on a push to the disposable branch or manual dispatch. Obtain approval for the destination, push, paid run and automatic test-artifact upload before publishing the branch. No fork PR or issue comment is necessary to execute this test. Do not merge this disposable workflow into the product PR.

Local validation: full focused suite 418 passed. Identical A/B harness: baseline seven expected failures plus four passes, fixed tree eleven passes. No model has been loaded locally for this task.
