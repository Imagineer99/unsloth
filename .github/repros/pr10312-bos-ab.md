<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# PR 10312: reproducible BOS regression

This staging example compares upstream baseline `5ae462df3802de9c483731cdb3ba9a2741a1d7f0` with PR head `9c1a25dbd3b7c3dc727fe0d3a35a476961f2729a`, refreshed on 2026-09-07. This head includes the new fix_tokenizer opt-out and generic fast tokenizer handling.

[Run the paired workflow](https://github.com/Imagineer99/unsloth/actions/workflows/pr10312-bos-ab.yml). It runs on Linux, Windows, and macOS with Python 3.12, Transformers 5.5.0, tokenizers 0.22.2, torch 2.11.0, torchvision 0.26.0 and current unsloth-zoo 2026.9.1. Both sides run in the same isolated uv environment on each runner.

**Green CI means the regression is proven, not that the PR is safe.** The controller requires baseline success and PR failure specifically on `exported_chat_has_one_bos`. Installation failures, missing evidence and unrelated exceptions fail CI. Each artifact includes independent baseline/PR logs and JSON, immutable source revisions, tokenizer checksum and package versions.

## Reproduction

1. Load the real Gemma 4 E2B base processor.
2. Execute the source revision's actual post-load tokenizer fix when present.
3. Apply its real `get_chat_template(processor, chat_template="gemma-4")`.
4. Save and reload the real processor; execute the post-load fix again.
5. Render a user message and encode with the actual Studio `generate_stream` tokenizer expression.

Expected results for the same `Hello` message:

| Boundary | Baseline | PR |
|---|---|---|
| Raw completion | `[9259]` | `[2, 9259]` (intended fix) |
| Exported chat encoded by Studio | `[2, 105, 2364, 107, 9259, ...]` | `[2, 2, 105, 2364, 107, ...]` (duplicate BOS) |
| Simulated encoding guard | One BOS | One BOS |

The harness downloads metadata only from `unsloth/gemma-4-E2B-unsloth-bnb-4bit` at revision `2b0731cf4f4b33eff1c902d30b6c6642ec3ee8f6`. It checks the tokenizer SHA-256. No model weights or GPU are needed.

The workflow contains the exact environment installation commands. After installing them in `./temp/pr10312/venv`, run its Python executable with:

```sh
python .github/scripts/pr10312_bos_ab.py paired --repo . --output ./temp/pr10312/evidence
```

Use the virtual environment Python, not an unrelated global Python. All generated worktrees, environments, caches and evidence belong under `./temp/`. Optional `--snapshot PATH` reuses the pinned metadata snapshot offline.

## Scope

This is a real tokenizer/processor boundary test using unchanged functions extracted from the pinned source AST to avoid GPU-dependent package initialization. It calls real Zoo padding/tokenizer helpers, real serialization and the actual Studio encoding expression. `patch_saving=False` skips installation of unrelated export hooks. It does not launch the full Studio server, browser, GPU model or test all hardware configurations. The candidate encoding guard is a simulation only; no product fix is included in this staging branch. The paired assertions also require unchanged Studio/template source hashes between both revisions.
