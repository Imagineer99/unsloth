#!/usr/bin/env python3
"""Collect immutable Qwen3.8-Flash-Next multimodal recognition evidence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from huggingface_hub import model_info

from utils.models.model_config import (
    is_vision_model,
    list_gguf_variants,
    list_local_gguf_variants,
)


MODEL_REPO = "Qwen/Qwen3.8-Flash-Next"
GGUF_REPO = "unsloth/Qwen3.8-Flash-Next-GGUF"
PRE_VISION_REVISION = "8bdc66664944"
ADD_VISION_REVISION = "178b998806b7"


def gguf_shape(revision: str) -> dict[str, object]:
    info = model_info(GGUF_REPO, revision=revision, files_metadata=True)
    names = [sibling.rfilename for sibling in info.siblings]
    main_ggufs = [
        name
        for name in names
        if name.lower().endswith(".gguf") and "mmproj" not in name.lower()
    ]
    projectors = [
        name
        for name in names
        if name.lower().endswith(".gguf") and "mmproj" in name.lower()
    ]
    return {
        "requested_revision": revision,
        "resolved_revision": info.sha,
        "main_gguf_count": len(main_ggufs),
        "projectors": projectors,
    }


def main() -> None:
    pre = gguf_shape(PRE_VISION_REVISION)
    post = gguf_shape(ADD_VISION_REVISION)

    assert pre["main_gguf_count"] == 22, pre
    assert pre["projectors"] == [], pre
    assert post["main_gguf_count"] == 22, post
    assert post["projectors"] == ["mmproj-BF16.gguf", "mmproj-F16.gguf"], post
    print(
        "PASS immutable HF history: pre-vision revision has 22 model GGUFs and no mmproj"
    )
    print(
        "PASS immutable HF history: Add vision revision keeps 22 model GGUFs and adds "
        "mmproj-BF16.gguf + mmproj-F16.gguf"
    )

    with tempfile.TemporaryDirectory(prefix="qwen38-flash-next-") as temp_dir:
        local = Path(temp_dir)
        (local / "Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf").touch()
        variants_before, vision_before = list_local_gguf_variants(str(local))
        assert len(variants_before) == 1
        assert vision_before is False
        print(
            "PASS Studio repro: a pre-projector local download is classified "
            "has_vision=False"
        )

        (local / "mmproj-F16.gguf").touch()
        variants_after, vision_after = list_local_gguf_variants(str(local))
        assert len(variants_after) == 1
        assert vision_after is True
        print(
            "PASS Studio A/B: the identical local download plus mmproj is classified "
            "has_vision=True"
        )

    model = model_info(MODEL_REPO)
    safetensors_vision = is_vision_model(MODEL_REPO)
    current_variants, current_gguf_vision = list_gguf_variants(GGUF_REPO)
    assert model.pipeline_tag == "image-text-to-text", model.pipeline_tag
    assert safetensors_vision is True
    assert current_gguf_vision is True
    assert current_variants
    print("PASS current HF metadata: pipeline_tag=image-text-to-text")
    print("PASS current Studio safetensors detection: is_vision_model=True")
    print("PASS current Studio GGUF detection: has_vision=True")

    evidence = {
        "model": MODEL_REPO,
        "gguf_repo": GGUF_REPO,
        "pre_vision": pre,
        "add_vision": post,
        "studio_local_ab": {
            "without_mmproj": False,
            "with_mmproj": True,
        },
        "current": {
            "pipeline_tag": model.pipeline_tag,
            "safetensors_is_vision": safetensors_vision,
            "gguf_has_vision": current_gguf_vision,
            "gguf_variant_count": len(current_variants),
        },
    }
    output = Path(os.environ.get("QWEN_REPRO_OUTPUT", "qwen38-evidence.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"PASS wrote evidence artifact: {output}")


if __name__ == "__main__":
    main()
