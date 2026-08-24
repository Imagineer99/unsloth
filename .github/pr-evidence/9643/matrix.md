# Qwen sampling-default Playwright matrix

Expectation: Only complete legacy built-in Default snapshots for Qwen3.5/3.6/3.8 migrate to minP=0 and presencePenalty=1.5; fresh, generic-Qwen3, explicit, customized, and unrelated-model behavior remains correct.

BEFORE `785a68dc4d` · AFTER `15432c65d5` · 16 Playwright captures · zero browser errors/warnings

| Scenario | Backend status | Initial saved state | BEFORE UI | AFTER UI | Targeted migration PUT | Result |
|---|---|---|---|---|---|---|
| Qwen3.8 · fresh settings | T 0.7 · P 0.8 · min 0 · presence 1.5 | none | T 0.6 · P 0.95 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0 · presence 1.5 | no | PASS |
| Qwen3.8 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | PASS |
| Qwen3.6 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | PASS |
| Qwen3.5 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | PASS |
| Generic Qwen3 · legacy-like | T 0.6 · P 0.95 · min 0 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0.01 · presence Off | no | PASS |
| Qwen3.8 · explicit presence=0 | T 0.7 · P 0.8 · min 0 · presence 1.5 | T — · P — · min — · presence 0 | T 0.6 · P 0.95 · min 0 · presence Off | T 0.6 · P 0.95 · min 0 · presence Off | no | PASS |
| Qwen3.8 · customized snapshot | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.65 · P 0.95 · min 0.01 · presence 0 | T 0.65 · P 0.95 · min 0.01 · presence Off | T 0.65 · P 0.95 · min 0.01 · presence Off | no | PASS |
| Llama 3.2 · fresh settings | T 1.5 · P 0.95 · min 0.1 · presence 0 | none | T 1.5 · P 0.95 · min 0.1 · presence Off | T 1.5 · P 0.95 · min 0.1 · presence Off | no | PASS |

Notes:

- Backend status is read from the same mock server that served each photographed UI.
- `Targeted migration PUT` means the exact per-model and global patch `{minP: 0, presencePenalty: 1.5}`.
- Other PUTs are normal global settings persistence and are preserved in `backend-matrix.json`.
- Limitation: Model weights were mocked because the behavior under test is settings hydration. Backend status values came from the repository resolver outputs; no token generation was tested.
