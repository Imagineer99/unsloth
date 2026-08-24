#!/usr/bin/env python3
"""Disposable real-GGUF A/B probe for upstream PR #9149."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
import tempfile
import types
import urllib.request
from pathlib import Path


MODEL_URL = (
    "https://huggingface.co/ChristianAzinn/bge-small-en-v1.5-gguf/resolve/main/"
    "bge-small-en-v1.5.Q2_K.gguf"
)
SOURCE_KEY = b"bert.pooling_type"
HIDDEN_KEY = b"bert.pooling_typX"


def load_classifier(repo: Path):
    loggers = types.ModuleType("loggers")
    loggers.get_logger = lambda name: logging.getLogger(name)
    sys.modules["loggers"] = loggers

    utils = types.ModuleType("utils")
    utils.__path__ = []
    sys.modules["utils"] = utils
    models = types.ModuleType("utils.models")
    models.__path__ = []
    sys.modules["utils.models"] = models
    model_config = types.ModuleType("utils.models.model_config")
    model_config.colocated_split_shards = lambda path: ([Path(path)], True)
    sys.modules["utils.models.model_config"] = model_config

    source = repo / "studio/backend/utils/models/gguf_metadata.py"
    spec = importlib.util.spec_from_file_location("pr9149_gguf_metadata", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix = "pr9149-ab-") as tmp:
        original = Path(tmp) / "bge-small-en-v1.5.Q2_K.gguf"
        no_pooling = Path(tmp) / "neutral-model.gguf"
        print(f"AB_DOWNLOAD={MODEL_URL}")
        urllib.request.urlretrieve(MODEL_URL, original)
        data = original.read_bytes()
        if data.count(SOURCE_KEY) != 1 or data.count(HIDDEN_KEY) != 0:
            raise AssertionError("source GGUF did not contain exactly one pooling_type key")
        changed = data.replace(SOURCE_KEY, HIDDEN_KEY)
        if len(changed) != len(data) or sum(a != b for a, b in zip(data, changed)) != 1:
            raise AssertionError("fixture mutation was not exactly one byte")
        no_pooling.write_bytes(changed)

        classifier = load_classifier(repo)
        architecture = classifier.read_gguf_architecture(str(no_pooling))
        pooling_type = classifier._parse_gguf_arch_uints(
            str(no_pooling), frozenset({"pooling_type"})
        ).get("pooling_type")
        classifier_head = classifier._gguf_has_classifier_head(str(no_pooling))
        classifier.read_gguf_general_metadata = lambda _: {}
        verdict = classifier.is_gguf_embedding_model(
            str(no_pooling), "neutral/model.gguf", architecture = architecture
        )

        print(f"AB_SOURCE_SHA256={hashlib.sha256(data).hexdigest()}")
        print(f"AB_ARCHITECTURE={architecture}")
        print(f"AB_POOLING_TYPE={pooling_type}")
        print(f"AB_CLASSIFIER_HEAD={classifier_head}")
        print(f"AB_EMBEDDING_MODE={verdict}")
        if architecture != "bert" or pooling_type is not None or classifier_head is not False:
            raise AssertionError("fixture does not exercise generic BERT without pooling or cls.*")
        if verdict is not False:
            raise AssertionError("AB_REPRO: generic BERT without pooling was unsafely auto-enabled")
        print("AB_PASS: ambiguous generic BERT stayed fail closed")


if __name__ == "__main__":
    main()
