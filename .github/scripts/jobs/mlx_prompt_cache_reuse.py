"""Studio MLX prompt-cache reuse across chat turns, base vs head, on Apple Silicon.

Drives Studio's own MLXInferenceBackend through one long greedy chat whose KV outgrows a small
prompt-cache budget (UNSLOTH_MLX_PROMPT_CACHE_BYTES, sized from the model config so the first two
turns fit and later ones do not), then: regenerate the last turn, one turn of another chat, the
long chat again, and a two-row batch (long chat + short chat). Every turn records the cached
prefix (usage.prompt_tokens_details.cached_tokens), prompt tokens, prefill ms, peak / active MLX
memory and the reply text.

Arms: head = the checkout's studio/backend; base = the same tree with only
core/inference/mlx_inference.py swapped for --base-file, else `git show <--base-ref>:...` after a
shallow fetch (default ref `upstream-main`, the staging base). Each arm runs in a fresh process.

    python jobs/mlx_prompt_cache_reuse.py --tiny       # SmolLM-135M
    python jobs/mlx_prompt_cache_reuse.py              # Qwen3-0.6B-4bit

Exit 3 off Apple Silicon.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

DEFAULT_MODEL = "mlx-community/Qwen3-0.6B-4bit"
TINY_MODEL = "mlx-community/SmolLM-135M-Instruct-4bit"
REL = "core/inference/mlx_inference.py"
FIT_TOKENS = 700  # budget in tokens of KV: turns 1-2 fit, turn 3 on does not

PARAGRAPH = (
    "Here is part {i} of my notes on the history of the printing press, the spread of "
    "literacy, paper mills, movable type, censorship, the economics of book production and "
    "how ideas travelled between cities. "
)


def _backend_dir():
    for cand in (Path.cwd(), *Path.cwd().parents):
        if (cand / "studio" / "backend" / REL).is_file():
            return cand / "studio" / "backend"
    raise SystemExit("run from an unsloth checkout (studio/backend not found)")


def _user(i, reps = 6):
    return "".join(PARAGRAPH.format(i = f"{i}.{r}") for r in range(reps)) + f"Summarise part {i}."


def _kv_bytes_per_token(model_dir):
    cfg = json.loads((Path(model_dir) / "config.json").read_text(encoding = "utf-8"))
    cfg = cfg.get("text_config", cfg)
    layers = cfg["num_hidden_layers"]
    heads = cfg["num_attention_heads"]
    kv = cfg.get("num_key_value_heads", heads)
    head_dim = cfg.get("head_dim") or cfg["hidden_size"] // heads
    return layers * 2 * kv * head_dim * 2  # K and V, fp16/bf16


def child(backend_dir, model, out_path):
    out_path = Path(out_path).resolve()
    sys.path.insert(0, str(backend_dir))
    os.chdir(backend_dir)
    import mlx.core as mx
    from core.inference.mlx_inference import MLXInferenceBackend
    from utils.models.model_config import ModelConfig

    backend = MLXInferenceBackend()
    assert backend.load_model(ModelConfig.from_identifier(model), max_seq_length = 8192)
    greedy = dict(temperature = 0.0, top_p = 1.0, top_k = 0, min_p = 0.0, max_new_tokens = 24)
    rows = []

    def record(phase, chat, stats, text, t0):
        usage = (stats or {}).get("usage", {})
        timings = (stats or {}).get("timings", {})
        rows.append(
            {
                "phase": phase,
                "chat": chat,
                "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                "prefill_n": timings.get("prompt_n"),
                "prompt_ms": round(timings.get("prompt_ms") or 0.0, 1),
                "wall_ms": round((time.perf_counter() - t0) * 1e3, 1),
                "peak_gb": round(mx.get_peak_memory() / 1e9, 4),
                "active_after_gb": round(mx.get_active_memory() / 1e9, 4),
                "text": text,
            }
        )
        print(json.dumps(rows[-1]), flush = True)

    def turn(phase, chat, messages):
        mx.reset_peak_memory()
        t0 = time.perf_counter()
        text = ""
        for text in backend.generate_chat_response(messages, system_prompt = "", **greedy):
            pass
        record(phase, chat, backend.last_generation_stats, text, t0)
        return text

    long_chat = []
    for i in range(1, 7):
        long_chat.append({"role": "user", "content": _user(i)})
        long_chat.append({"role": "assistant", "content": turn(f"turn{i}", "long", long_chat)})
    # Regenerate: same history, reply dropped.
    turn("regenerate", "long", long_chat[:-1])
    other = [{"role": "user", "content": "What is two plus two? Answer briefly."}]
    turn("other_chat", "other", other)
    long_chat.append({"role": "user", "content": _user(7)})
    long_chat.append({"role": "assistant", "content": turn("turn7_after_other", "long", long_chat)})
    long_chat.append({"role": "user", "content": _user(8)})
    long_chat.append({"role": "assistant", "content": turn("turn8", "long", long_chat)})

    long_chat.append({"role": "user", "content": _user(9)})
    requests = [
        dict(messages = list(long_chat), system_prompt = "", **greedy),
        dict(
            messages = other
            + [
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "And three plus three?"},
            ],
            system_prompt = "",
            **greedy,
        ),
    ]
    reason = backend.batch_unavailable_reason(requests)
    batch = {"unavailable": reason}
    if reason is None:
        mx.reset_peak_memory()
        t0 = time.perf_counter()
        texts = {}
        for item in backend.generate_chat_batch(requests):
            idx, snap = item[0], item[1]
            if snap is not None:
                texts[idx] = snap
        for idx, stats in enumerate(backend.last_batch_generation_stats or []):
            record(
                f"batch_row{idx}", "long" if idx == 0 else "other", stats, texts.get(idx, ""), t0
            )
    out_path.write_text(
        json.dumps(
            {
                "rows": rows,
                "batch": batch,
                "budget": os.environ.get("UNSLOTH_MLX_PROMPT_CACHE_BYTES"),
            }
        ),
        encoding = "utf-8",
    )


def _base_source(backend_dir, a):
    if a.base_file:
        return Path(a.base_file).read_text(encoding = "utf-8")
    repo = backend_dir.parent.parent
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "--depth", "1", "origin", a.base_ref], check = True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"FETCH_HEAD:studio/backend/{REL}"],
        check = True,
        capture_output = True,
        text = True,
    ).stdout


def main():
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        child(Path(sys.argv[i + 1]), sys.argv[i + 2], sys.argv[i + 3])
        return
    p = C.base_parser("mlx_prompt_cache_reuse", DEFAULT_MODEL, TINY_MODEL, default_steps = 0)
    p.add_argument("--base-ref", default = "upstream-main")
    p.add_argument("--base-file", default = None)
    a = C.resolve_args(p)
    C.reject_cfg(a)
    with C.JobRecorder(a, backend_hint = "mlx") as rec:
        if not (sys.platform == "darwin" and platform.machine() == "arm64"):
            rec.skip("needs Apple Silicon")
        from huggingface_hub import snapshot_download

        model_dir = snapshot_download(a.model)
        per_token = _kv_bytes_per_token(model_dir)
        budget = per_token * FIT_TOKENS
        head_dir = _backend_dir()
        scratch = Path(tempfile.mkdtemp(prefix = "mlx_pc_", dir = Path(a.out).resolve().parent))
        base_dir = scratch / "base_backend"
        shutil.copytree(
            head_dir,
            base_dir,
            ignore = shutil.ignore_patterns("__pycache__", "tests", "node_modules"),
        )
        base_src = _base_source(head_dir, a)
        rec.check(
            "arms_differ",
            base_src != (head_dir / REL).read_text(encoding = "utf-8"),
            "base and head mlx_inference.py are byte-identical"
            if base_src == (head_dir / REL).read_text(encoding = "utf-8")
            else "",
        )
        (base_dir / REL).write_text(base_src, encoding = "utf-8")
        env = dict(
            os.environ,
            UNSLOTH_MLX_PROMPT_CACHE_BYTES = str(budget),
            UNSLOTH_STUDIO_HOME = str(scratch / "home"),
        )
        rec.summary(kv_bytes_per_token = per_token, budget_bytes = budget, budget_tokens = FIT_TOKENS)
        arms = {}
        for arm, bdir in (("base", base_dir), ("head", head_dir)):
            out = scratch / f"{arm}.json"
            try:
                rc = subprocess.run(
                    [
                        sys.executable,
                        os.path.abspath(__file__),
                        "--child",
                        str(bdir),
                        a.model,
                        str(out),
                    ],
                    env = env,
                    timeout = 1800,
                ).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            if rc != 0 or not out.is_file():
                rec.check(f"child_ok[{arm}]", False, f"exit {rc}")
                continue
            arms[arm] = json.loads(out.read_text(encoding = "utf-8"))
            for row in arms[arm]["rows"]:
                rec.step(arm = arm, **row)
            rec.summary(**{f"{arm}.batch": arms[arm]["batch"]})
        if len(arms) != 2:
            return
        by = {arm: {r["phase"]: r for r in d["rows"]} for arm, d in arms.items()}

        def over_budget(phase):
            return (by["base"][phase]["prompt_tokens"] or 0) > FIT_TOKENS

        cont = [f"turn{i}" for i in range(2, 7)] + ["turn8", "regenerate"]
        over = [ph for ph in cont if over_budget(ph)]
        rec.check("some_turns_over_budget", len(over) >= 3, over)

        def reuse(arm, ph):
            r = by[arm][ph]
            return (r["cached_tokens"] or 0) / max(r["prompt_tokens"] or 1, 1)

        table = {
            ph: {
                arm: (
                    by[arm][ph]["cached_tokens"],
                    by[arm][ph]["prompt_tokens"],
                    by[arm][ph]["prompt_ms"],
                )
                for arm in by
            }
            for ph in by["head"]
        }
        rec.summary(per_phase_cached_prompt_ms = table)
        rec.check(
            "base_misses_over_budget",
            all(reuse("base", ph) < 0.5 for ph in over),
            {ph: round(reuse("base", ph), 3) for ph in over},
        )
        # Head serves the whole previous turn (prompt + reply); only this turn's new text is left.
        prev = {f"turn{i}": f"turn{i - 1}" for i in range(2, 7)}
        prev.update(turn8 = "turn7_after_other")
        want = {
            ph: (
                by["head"][prev[ph]]["prompt_tokens"]
                if ph in prev
                else by["head"][ph]["prompt_tokens"] - 1
            )
            for ph in over
        }
        rec.check(
            "head_reuses_over_budget",
            all((by["head"][ph]["cached_tokens"] or 0) >= want[ph] for ph in over),
            {ph: (by["head"][ph]["cached_tokens"], want[ph]) for ph in over},
        )
        # Another chat releases the held slot; head behaves as base there.
        rec.check(
            "other_chat_released",
            reuse("head", "turn7_after_other") < 0.5,
            round(reuse("head", "turn7_after_other"), 3),
        )
        if "batch_row0" in by["head"]:
            rec.check(
                "batch_long_row_reuses",
                reuse("head", "batch_row0") > 0.8,
                round(reuse("head", "batch_row0"), 3),
            )
        same = [ph for ph in by["head"] if by["head"][ph]["text"] == by["base"][ph]["text"]]
        rec.summary(
            greedy_text_exact_match = f"{len(same)}/{len(by['head'])}",
            greedy_text_diverged = [ph for ph in by["head"] if ph not in same],
        )
        rec.summary(peak_gb = {arm: max(r["peak_gb"] for r in d["rows"]) for arm, d in arms.items()})
        print(
            "\n| phase | base cached/prompt | base prefill ms | head cached/prompt | head prefill ms |"
        )
        print("|---|---|---|---|---|")
        for ph, v in table.items():
            b, h = v.get("base"), v["head"]
            print(f"| {ph} | {b[0]}/{b[1]} | {b[2]} | {h[0]}/{h[1]} | {h[2]} |", flush = True)
        shutil.rmtree(scratch, ignore_errors = True)


if __name__ == "__main__":
    main()
