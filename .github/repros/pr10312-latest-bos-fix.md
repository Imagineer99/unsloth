<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# PR 10312 fix against the latest author head

The fix preserves BOS in runtime Gemma chat templates and disables automatic special-token insertion only after successful Studio chat rendering. Raw completion and manual fallback retain automatic BOS. The author's dictionary-template save helper remains unchanged.

This branch starts at author head `91f975068fbbb93b9b08135185d7ad4abb0bdb29`. The four-way comparison uses the same pinned model snapshot, dependencies and assertions on each runner:

| Source | Studio rendered chat | apply_chat_template(tokenize=True) | Raw completion |
|---|---|---|---|
| Main 5ae462df3 | One BOS | One BOS | Missing BOS |
| Original PR 9c1a25dbd | Duplicate BOS | One BOS | One BOS |
| Latest PR 91f975068 | One BOS | Missing BOS | One BOS |
| Fixed latest head | One BOS | One BOS | One BOS |

[Workflow](https://github.com/Imagineer99/unsloth/actions/workflows/pr10312-bos-latest-fix-ab.yml) runs on Linux, Windows and macOS. Green requires main/fixed success and both expected PR failures. Installation errors, unrelated failures and stale results cannot count as evidence. Logs and per-revision JSON include actual token IDs, source hashes and dependency versions.

Each revision includes 20 real Gemma chat scenarios and 12 real Rust tokenizer controls covering BOS/EOS policy, save/reload, Unicode, empty content, template whitespace and continuation. Explicit direct chat tokenization catches the latest regression. Permanent regression tests in `tests/python/test_gemma4_base_bos_token.py` exercise real tokenizers and serialization; the runtime-preservation cases fail against the latest author head and pass with the fix, while the dictionary export-helper control passes both.

The model metadata is `unsloth/gemma-4-E2B-unsloth-bnb-4bit` at revision `2b0731cf4f4b33eff1c902d30b6c6642ec3ee8f6`, with tokenizer SHA-256 `cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f`. The workflow installs Python 3.12, Transformers 5.5.0, tokenizers 0.22.2, torch 2.11.0 and Zoo 2026.9.1 in an isolated uv environment. No model weights are downloaded.

Run the workflow's environment setup, then its virtual-environment Python:

```sh
python .github/scripts/pr10312_bos_fix_ab.py paired --repo . --output ./temp/pr10312-evidence
```

All generated files stay below `./temp/`. Optional `--snapshot` reuses the pinned metadata offline. Source extraction executes actual tokenizer helpers, Studio chat dispatch, native-template fallback and encoding expressions while isolating package/GPU initialization and model generation. This is not full Studio, browser, GGUF, MLX or multimodal generation certification.

The fix follows [Transformers guidance](https://huggingface.co/docs/transformers/v4.43.0/chat_templating): a template owns chat special tokens, so later tokenization must not insert them again.
