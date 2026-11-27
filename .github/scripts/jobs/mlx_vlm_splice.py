"""Studio MLX VLM prompt counts on a splicing model (FastVLM) vs a processor-expanding one, base vs head.

Random-weight tiny copies built from the real configs (no weights pulled). Per arm, one fresh process
loads the model through Studio's MLXInferenceBackend, counts an image chat, generates, and records
the reported prompt usage against the widest get_input_embeddings width actually fed.
Exit 0 all checks pass, 1 failed check / crash.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FASTVLM = "mlx-community/FastVLM-0.5B-bf16"
QWENVL = "trl-internal-testing/tiny-Qwen2_5_VLForConditionalGeneration"
STUDIO_DEPS = [
    "fastapi",
    "structlog",
    "pyjwt",
    "timm",
    "torchvision",
    "pydantic",
    "httpx",
    "uvicorn",
]


def make_tiny_fastvlm(src, dst):
    import mlx.core as mx
    from mlx.utils import tree_flatten
    from mlx_vlm.utils import get_model_and_args

    dst.mkdir(parents = True, exist_ok = True)
    for f in src.iterdir():
        if f.is_file() and f.name not in ("model.safetensors.index.json", "config.json"):
            shutil.copy(f, dst / f.name)
    cfg = json.loads((src / "config.json").read_text(encoding = "utf-8"))
    cfg["num_hidden_layers"] = cfg["max_window_layers"] = 2
    cfg["layer_types"] = cfg["layer_types"][:2]
    (dst / "config.json").write_text(json.dumps(cfg), encoding = "utf-8")
    mod, _ = get_model_and_args(cfg)
    mc = mod.ModelConfig.from_dict(cfg)
    for key in ("vision_config", "text_config"):
        sub = getattr(mod, key.title().replace("_", ""), None)
        if sub is not None and isinstance(getattr(mc, key, None), dict):
            setattr(mc, key, sub.from_dict(getattr(mc, key)))
    model = mod.Model(mc)
    weights = {k: v.astype(mx.bfloat16) for k, v in tree_flatten(model.parameters())}
    mx.save_safetensors(str(dst / "model.safetensors"), weights, metadata = {"format": "mlx"})


def run_arm(tree, model_dir):
    sys.path.insert(0, str(Path(tree) / "studio" / "backend"))
    from PIL import Image

    import core.inference.mlx_inference as mi

    assert Path(mi.__file__).resolve().is_relative_to(Path(tree).resolve()), mi.__file__
    from utils.models.model_config import ModelConfig

    cfg = ModelConfig.from_identifier(model_dir)
    cfg.is_vision = True
    backend = mi.MLXInferenceBackend()
    assert backend.load_model(cfg, trust_remote_code = True)
    fed = []
    embed = backend._model.get_input_embeddings

    def spy(*args, **kwargs):
        out = embed(*args, **kwargs)
        ids = kwargs.get("input_ids", args[0] if args else None)
        fed.append((int(ids.shape[-1]), int(out.inputs_embeds.shape[1])))
        return out

    backend._model.get_input_embeddings = spy
    image = Image.new("RGB", (320, 240), "red")
    msgs = [{"role": "user", "content": "What colour is this?"}]
    count = backend.count_chat_tokens(msgs, images = [image])
    fed.clear()
    for _ in backend.generate_chat_response(
        messages = msgs, image = image, max_new_tokens = 4, temperature = 0.0
    ):
        pass
    usage = (backend.last_generation_stats or {}).get("usage", {}).get("prompt_tokens")
    import mlx.core as mx

    print(
        "ARM "
        + json.dumps(
            {
                "count_chat_tokens": count,
                "usage_prompt_tokens": usage,
                "fed_columns": max(fed)[1] if fed else None,
                "splices": getattr(backend, "_media_splices", None),
                "device": str(mx.default_device()),
            }
        ),
        flush = True,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default = "6e25b5c4e867455be17ea5e0abdd77f931ebdc31")
    ap.add_argument("--out", default = "mlx_vlm_splice.json")
    ap.add_argument("--arm", nargs = 2, metavar = ("TREE", "MODEL"))
    ap.add_argument("--no-install", action = "store_true")
    a = ap.parse_args()
    if a.arm:
        return run_arm(*a.arm)

    result = {
        "job": "mlx_vlm_splice",
        "model": [FASTVLM, QWENVL],
        "backend": "unsloth",
        "device": platform.platform(),
        "config": {"base": a.base},
        "steps": [],
        "summary": {},
        "checks": {},
        "passed": False,
        "error": None,
        "skipped": False,
        "finished": False,
    }

    def flush():
        Path(a.out).write_text(json.dumps(result, indent = 1), encoding = "utf-8")

    try:
        if not a.no_install:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    *STUDIO_DEPS,
                    "-r",
                    "studio/backend/requirements/studio.txt",
                ],
                check = True,
            )
        head = Path.cwd().resolve()
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output = True, text = True)
        result["revisions"] = {"head": rev.stdout.strip(), "base": a.base}
        work = Path(tempfile.mkdtemp(prefix = "splice_"))
        subprocess.run(
            ["git", "fetch", "-q", "--depth=1", "https://github.com/unslothai/unsloth", a.base],
            check = True,
        )
        base = work / "base"
        base.mkdir()
        archive = subprocess.run(
            ["git", "archive", a.base, "studio/backend"], check = True, capture_output = True
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(base)], input = archive, check = True)
        subprocess.run(
            [
                "hf",
                "download",
                FASTVLM,
                "--exclude",
                "*.safetensors",
                "--local-dir",
                str(work / "fastvlm_cfg"),
            ],
            check = True,
        )
        subprocess.run(["hf", "download", QWENVL, "--local-dir", str(work / "qwenvl")], check = True)
        make_tiny_fastvlm(work / "fastvlm_cfg", work / "fastvlm")
        arms = {}
        for model_key, model_dir in (("fastvlm", work / "fastvlm"), ("qwenvl", work / "qwenvl")):
            for side, tree in (("base", base), ("head", head)):
                t0 = time.time()
                p = subprocess.run(
                    [sys.executable, __file__, "--arm", str(tree), str(model_dir)],
                    capture_output = True,
                    text = True,
                    cwd = work,
                    timeout = 1800,
                )
                line = next((ln for ln in p.stdout.splitlines() if ln.startswith("ARM ")), None)
                arms[f"{model_key}_{side}"] = (
                    json.loads(line[4:])
                    if line
                    else {"rc": p.returncode, "tail": (p.stdout + p.stderr)[-3000:]}
                )
                arms[f"{model_key}_{side}"]["seconds"] = round(time.time() - t0, 1)
                print(model_key, side, json.dumps(arms[f"{model_key}_{side}"])[:400], flush = True)
        result["summary"] = arms
        fb, fh = arms["fastvlm_base"], arms["fastvlm_head"]
        qb, qh = arms["qwenvl_base"], arms["qwenvl_head"]

        def check(name, ok, detail):
            result["checks"][name] = {"ok": bool(ok), "detail": detail}

        check("metal_device", "gpu" in str(fh.get("device", "")), fh.get("device"))
        check(
            "base_defect_reproduces",
            fb.get("usage_prompt_tokens") is not None
            and fb.get("usage_prompt_tokens") < (fb.get("fed_columns") or 0),
            fb,
        )
        check("head_usage_equals_fed", fh.get("usage_prompt_tokens") == fh.get("fed_columns"), fh)
        check("head_count_equals_fed", fh.get("count_chat_tokens") == fh.get("fed_columns"), fh)
        check("head_detects_splice", fh.get("splices") is True, fh.get("splices"))
        check(
            "control_unchanged",
            qb.get("count_chat_tokens") == qh.get("count_chat_tokens")
            and qb.get("usage_prompt_tokens") == qh.get("usage_prompt_tokens")
            and qh.get("splices") is False,
            {"base": qb, "head": qh},
        )
        result["passed"] = all(c["ok"] for c in result["checks"].values())
    except Exception as exc:
        result["error"] = repr(exc)
    result["finished"] = True
    flush()
    print(
        "JOB_RESULT "
        + json.dumps(
            {
                "passed": result["passed"],
                "checks": {k: v["ok"] for k, v in result["checks"].items()},
                "error": result["error"],
            }
        ),
        flush = True,
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
