Disposable CPU evaluation A/B validation for unslothai/unsloth#17291.

Base: 10c52ed463591a8db375cb4b1d45c3361a1fa055
Head: 4977671e07361881b6eb32355a39459ed610ba7c

Ubuntu: real SFTTrainer evaluate, Llama and Qwen3-MoE aux off/on, one process
versus two Gloo pipeline ranks. Head must pass all 14 focused tests without
skips. Base retains head regression tests but exact base production code;
all three eval regressions must fail specifically on understated loss.
Import errors, skipped paths, or arbitrary red tests do not satisfy A/B proof.

macOS: exact normalization functions with native CPU tensors, 3,840 function
cases and 72 independent CE oracle cases. This is helper-level portability
evidence, not full macOS pipeline or MLX support.

The workflow uses contents:read, no persisted checkout credentials, no repository
secrets, and no artifact uploads or GitHub comments. Results go to job logs and
job summaries. No PR or fork-main update is needed. Do not merge this branch.
