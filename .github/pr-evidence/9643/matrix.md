# Qwen sampling-default Playwright matrix

Expectation: Complete legacy built-in Default snapshots migrate to the active reasoning table without overwriting explicit, newer-tab, previous-model, generic-Qwen3, customized, or unrelated-model settings.

BEFORE `785a68dc4d` · AFTER `338fb86f17` · 28 Playwright captures · zero browser errors/warnings

| Scenario | Backend status | Initial saved state | BEFORE UI | AFTER UI | Targeted per-model migration PUT | Guard proof | Result |
|---|---|---|---|---|---|---|---|
| Qwen3.8 · fresh settings | T 0.7 · P 0.8 · min 0 · presence 1.5 | none | T 0.6 · P 0.95 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0 · presence 1.5 | no | — | PASS |
| Qwen3.8 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | — | PASS |
| Qwen3.8 · legacy · thinking off | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.7 · P 0.8 · min 0 · presence 1.5 | yes | — | PASS |
| Qwen3.6 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | — | PASS |
| Qwen3.6 9B · default thinking off | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.7 · P 0.8 · min 0 · presence 1.5 | yes | — | PASS |
| Qwen3.5 · legacy Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | — | PASS |
| Qwen3.8 · global-only legacy | T 0.7 · P 0.8 · min 0 · presence 1.5 | partial: min 0.01 · presence 0 | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.7 · P 0.8 · min 0 · presence 1.5 | no | global state durable at T 0.7 · P 0.8 · min 0 · presence 1.5 | PASS |
| Qwen3.8 · previous-model global | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | previous global min 0.01 · presence 0 remained unchanged | PASS |
| Qwen3.8 · custom → Default | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | yes | selecting built-in Default retried and persisted migration | PASS |
| Qwen3.8 · newer tab edit | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0 · presence 1.5 | no | confirming GET kept server presence 0.4; no migration PUT | PASS |
| Generic Qwen3 · legacy-like | T 0.6 · P 0.95 · min 0 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence 0 | T 0.6 · P 0.95 · min 0.01 · presence Off | T 0.6 · P 0.95 · min 0.01 · presence Off | no | — | PASS |
| Qwen3.8 · explicit presence=0 | T 0.7 · P 0.8 · min 0 · presence 1.5 | T — · P — · min — · presence 0 | T 0.6 · P 0.95 · min 0 · presence Off | T 0.6 · P 0.95 · min 0 · presence Off | no | — | PASS |
| Qwen3.8 · customized snapshot | T 0.7 · P 0.8 · min 0 · presence 1.5 | T 0.65 · P 0.95 · min 0.01 · presence 0 | T 0.65 · P 0.95 · min 0.01 · presence Off | T 0.65 · P 0.95 · min 0.01 · presence Off | no | — | PASS |
| Llama 3.2 · fresh settings | T 1.5 · P 0.95 · min 0.1 · presence 0 | none | T 1.5 · P 0.95 · min 0.1 · presence Off | T 1.5 · P 0.95 · min 0.1 · presence Off | no | — | PASS |

Notes:

- Backend status is read from the same mock server that served each photographed UI.
- `Targeted per-model migration PUT` means a fingerprint-guarded per-model patch containing `minP=0` and `presencePenalty=1.5`.
- The global-only migration payload is byte-for-byte indistinguishable from normal runtime persistence in browser traffic; the durable browser outcome is shown here and the focused unit test isolates that migration branch.
- In the newer-tab race, the current tab has already changed its local view before the confirming GET. The proof is the server retaining the newer `presencePenalty=0.4` and receiving no migration PUT.
- Other PUTs are normal global settings persistence and are preserved in `backend-matrix.json`.
- Limitation: Model weights were mocked because the behavior under test is settings hydration. Backend status values came from the repository resolver outputs; no token generation was tested.
