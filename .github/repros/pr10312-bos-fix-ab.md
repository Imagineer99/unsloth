<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# PR 10312 candidate fix A/B

The candidate changes Studio's text chat path to pass `add_special_tokens=False` after successful template rendering. Direct raw completion callers keep the default `True`, and template-error fallback retains the previous behavior. It leaves the PR's tokenizer fix enabled.

This follows [Transformers chat-template guidance](https://huggingface.co/docs/transformers/v4.43.0/chat_templating). An explicit rendering flag avoids guessing from a BOS prefix and handles leading whitespace or templates without BOS.

The same environment and pinned real Gemma 4 E2B processor compare main `5ae462df3802de9c483731cdb3ba9a2741a1d7f0`, PR `9c1a25dbd3b7c3dc727fe0d3a35a476961f2729a`, and the candidate checkout. See the [original reproduction](pr10312-bos-ab.md) for dependency and model pins.

| Case | Main | PR | Candidate |
|---|---|---|---|
| Rendered chat after save/reload | One BOS | Two BOS | One BOS |
| Raw completion | Missing BOS | One BOS | One BOS |

The controller requires main PASS, PR assertion FAIL, and candidate PASS. Setup failures cannot count as negative proof. Every revision exercises 20 real Gemma chat cases: two templates (including leading whitespace), five message contents (including empty, Unicode and literal BOS), and two continuation modes. Twelve additional real Rust tokenizer controls cover optional BOS/EOS post-processing and templates with/without special tokens. Manual template-error fallback preserves raw encoding.

Run the virtual environment Python from the original example:

```sh
python .github/scripts/pr10312_bos_fix_ab.py paired --repo . --output ./temp/pr10312-fix-evidence
```

[Cross-platform workflow](https://github.com/Imagineer99/unsloth/actions/workflows/pr10312-bos-fix-ab.yml) runs on Linux, Windows and macOS. Its artifacts include logs and independent per-revision results, source revisions/hashes, package versions and exact token IDs.

Scope: real processor/template serialization, actual Studio chat dispatch method, native-template fallback helper and tokenization expression. Package/GPU initialization, model generation, the template registry and manual fallback formatter are isolated. This is boundary verification, not a full Studio server/browser or GPU test. The API change appends an optional argument so existing positional calls retain their meaning. No change to MLX, GGUF, image or audio generation.
