# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Disposable paired proof for PR 10312. No weights, server, or GPU required."""

from __future__ import annotations
import argparse
import ast
import hashlib
import functools
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

BASE = "5ae462df3802de9c483731cdb3ba9a2741a1d7f0"
HEAD = "9c1a25dbd3b7c3dc727fe0d3a35a476961f2729a"
MODEL = "unsloth/gemma-4-E2B-unsloth-bnb-4bit"
REVISION = "2b0731cf4f4b33eff1c902d30b6c6642ec3ee8f6"
TOKENIZER_SHA256 = "cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f"


def tree(path):
    return ast.parse(path.read_text(encoding = "utf-8"))


def definitions(path, names, namespace):
    nodes = [n for n in tree(path).body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names), (path, names)
    exec(compile(ast.Module(body = nodes, type_ignores = []), str(path), "exec"), namespace)


def module(name, namespace = None):
    value = types.ModuleType(name)
    value.__dict__.update(namespace or {})
    value.__package__ = name.rpartition(".")[0]
    sys.modules[name] = value
    return value


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_boundary(repo, processor, config):
    """Execute the actual new FastBaseModel post-load statement if present."""
    path = repo / "unsloth/models/vision.py"
    calls = [
        n
        for n in ast.walk(tree(path))
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_apply_post_load_tokenizer_fixes"
    ]
    if not calls:
        assert not any(
            isinstance(n, ast.FunctionDef) and n.name == "_apply_post_load_tokenizer_fixes"
            for n in tree(repo / "unsloth/tokenizer_utils.py").body
        )
        return processor
    assert len(calls) == 1
    utilities = repo / "unsloth/tokenizer_utils.py"
    names = {
        "_tokenizer_objects",
        "_tokenizer_stored",
        "_normalize_type_name",
        "_is_gemma4_config",
        "_is_gemma4_tokenizer",
        "_chat_template_emits_bos",
        "_is_gemma4_instruct_tokenizer",
        "_needs_gemma4_base_bos",
        "_has_add_bos_token_setter",
        "_update_generic_fast_post_processor",
        "_enable_add_bos_token",
        "_fix_gemma4_base_bos_token",
        "_apply_post_load_tokenizer_fixes",
    }
    names |= {
        n.name
        for n in tree(utilities).body
        if isinstance(n, ast.FunctionDef)
        and n.name
        in {
            "_tokenizer_auto_adds_bos",
            "_strip_bos_from_chat_template_text",
            "_dedupe_bos_chat_template",
        }
    }
    scope = {"_GEMMA4_INSTRUCT_EOS": "<turn|>", "re": re}
    definitions(utilities, names, scope)
    module("unsloth.tokenizer_utils", scope)
    scope.update(
        tokenizer = processor,
        auto_config = config,
        fix_tokenizer = True,
        model = types.SimpleNamespace(config = config),
    )
    exec(compile(ast.Module(body = calls, type_ignores = []), str(path), "exec"), scope)
    return scope["tokenizer"]


def attach_template(repo, processor):
    """Real get_chat_template + real Zoo pad patch; skip package/GPU startup only."""
    import torch
    from transformers import ProcessorMixin
    from transformers.utils import logging

    zoo = Path(importlib.metadata.distribution("unsloth-zoo").locate_file("unsloth_zoo"))
    pad_spec = importlib.util.spec_from_file_location("_bos_proof_pad", zoo / "pad_token.py")
    pad = importlib.util.module_from_spec(pad_spec)
    pad_spec.loader.exec_module(pad)
    zoo_scope = {"fix_pad_token": pad.fix_pad_token, "torch": torch, "functools": functools}
    definitions(zoo / "tokenizer_utils.py", {"_maybe_inference_mode", "patch_tokenizer"}, zoo_scope)
    module("unsloth").__path__ = [str(repo / "unsloth")]
    module("unsloth.models").__path__ = [str(repo / "unsloth/models")]
    utils = module("unsloth.models._utils", {"_patch_tokenizer": zoo_scope["patch_tokenizer"]})
    definitions(repo / "unsloth/models/_utils.py", {"patch_tokenizer"}, utils.__dict__)
    path = repo / "unsloth/chat_templates.py"
    template = next(
        ast.literal_eval(n.value)
        for n in tree(path).body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "gemma4_template" for t in n.targets)
    )
    chat = module(
        "unsloth.chat_templates",
        {
            "ProcessorMixin": ProcessorMixin,
            "logger": logging.get_logger("bos-proof"),
            "re": re,
            "CHAT_TEMPLATES": {"gemma-4": (template, "<turn|>", False, None)},
            "DEFAULT_SYSTEM_MESSAGE": {"gemma-4": None},
        },
    )
    definitions(
        path,
        {"get_chat_template", "_change_system_message", "_escape_jinja_literal"},
        chat.__dict__,
    )
    # patch_saving=False skips export-hook installation, not tokenizer/template behavior.
    return chat.get_chat_template(processor, chat_template = "gemma-4", patch_saving = False)


def studio_encode(repo, tokenizer, prompt):
    path = repo / "studio/backend/core/inference/inference.py"
    method = next(
        n
        for n in ast.walk(tree(path))
        if isinstance(n, ast.FunctionDef) and n.name == "generate_stream"
    )
    nodes = [
        n
        for n in ast.walk(method)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "inputs" for t in n.targets)
    ]
    assert len(nodes) == 1
    scope = dict(tokenizer = tokenizer, prompt = prompt, model = types.SimpleNamespace(device = "cpu"))
    exec(compile(ast.Module(body = nodes, type_ignores = []), str(path), "exec"), scope)
    return scope["inputs"]["input_ids"][0].tolist()


def probe(args):
    from transformers import AutoConfig, AutoProcessor

    repo, output = Path(args.repo).resolve(), Path(args.output).resolve()
    output.mkdir(parents = True, exist_ok = True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    snapshot = Path(args.snapshot).resolve()
    assert source_hash(snapshot / "tokenizer.json") == TOKENIZER_SHA256
    config = AutoConfig.from_pretrained(snapshot, local_files_only = True)
    processor = AutoProcessor.from_pretrained(snapshot, local_files_only = True)
    processor = load_boundary(repo, processor, config)
    raw_ids = processor.tokenizer("Hello")["input_ids"]
    processor = attach_template(repo, processor)
    messages = [{"role": "user", "content": "Hello"}]
    rendered = processor.apply_chat_template(messages, tokenize = False, add_generation_prompt = True)
    direct_ids = studio_encode(repo, processor.tokenizer, rendered)
    processor.save_pretrained(output / "export")
    reloaded = AutoProcessor.from_pretrained(output / "export", local_files_only = True)
    reloaded = load_boundary(repo, reloaded, config)
    prompt = reloaded.apply_chat_template(messages, tokenize = False, add_generation_prompt = True)
    assert prompt == rendered
    ids = studio_encode(repo, reloaded.tokenizer, prompt)
    tok = reloaded.tokenizer
    candidate = tok(
        prompt, add_special_tokens = tok.bos_token is None or not prompt.startswith(tok.bos_token)
    )["input_ids"]
    assert candidate[0] == tok.bos_token_id and candidate[1] != tok.bos_token_id
    passes = ids[0] == tok.bos_token_id and ids[1] != tok.bos_token_id
    result = {
        "label": args.label,
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd = repo, text = True
        ).strip(),
        "model": MODEL,
        "revision": REVISION,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "versions": {
            n: importlib.metadata.version(n)
            for n in ["transformers", "tokenizers", "torch", "torchvision", "unsloth-zoo"]
        },
        "source_hashes": {
            p: source_hash(repo / p)
            for p in [
                "unsloth/tokenizer_utils.py",
                "unsloth/models/vision.py",
                "unsloth/chat_templates.py",
                "studio/backend/core/inference/inference.py",
            ]
        },
        "zoo_pad_source_hash": source_hash(
            Path(
                importlib.metadata.distribution("unsloth-zoo").locate_file(
                    "unsloth_zoo/pad_token.py"
                )
            )
        ),
        "prompt": prompt,
        "raw_ids": raw_ids,
        "direct_ids": direct_ids,
        "exported_studio_ids": ids,
        "candidate_boundary_ids": candidate,
        "exported_chat_has_one_bos": passes,
        "failed_assertion": None if passes else "exported_chat_has_one_bos",
    }
    (output / "result.json").write_text(json.dumps(result, indent = 2), encoding = "utf-8")
    print(json.dumps(result, indent = 2), flush = True)
    print(
        ("PASS" if passes else "REGRESSION_ASSERTION_FAILED") + " exported_chat_has_one_bos",
        flush = True,
    )
    return 0 if passes else 1


def paired(args):
    from huggingface_hub import snapshot_download

    root, output = Path(args.repo).resolve(), Path(args.output).resolve()
    output.mkdir(parents = True, exist_ok = True)
    snapshot = args.snapshot or snapshot_download(
        MODEL,
        revision = REVISION,
        cache_dir = output / "hf-cache",
        allow_patterns = [
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "processor_config.json",
        ],
    )
    values = {}
    for label, sha in [("baseline", BASE), ("pr", HEAD)]:
        checkout = output / ("source-" + label)
        if checkout.exists():
            assert (
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd = checkout, text = True
                ).strip()
                == sha
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), sha], cwd = root, check = True
            )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "probe",
            "--repo",
            str(checkout),
            "--output",
            str(output / label),
            "--label",
            label,
            "--snapshot",
            str(snapshot),
        ]
        result_file = output / label / "result.json"
        result_file.unlink(missing_ok = True)  # Never accept evidence from an earlier run.
        with (output / (label + ".log")).open("w", encoding = "utf-8") as log:
            completed = subprocess.run(command, stdout = log, stderr = subprocess.STDOUT)
        result_file = output / label / "result.json"
        if not result_file.exists():
            raise RuntimeError(
                f"{label}: setup/execution failed, NOT regression proof; see {label}.log"
            )
        values[label] = json.loads(result_file.read_text(encoding = "utf-8"))
        values[label]["returncode"] = completed.returncode
        print(
            f"{label}: rc={completed.returncode}, raw={values[label]['raw_ids']}, exported={values[label]['exported_studio_ids'][:5]}",
            flush = True,
        )
    a, b = values["baseline"], values["pr"]
    assert a["source_sha"] == BASE and b["source_sha"] == HEAD
    assert a["returncode"] == 0 and a["exported_chat_has_one_bos"]
    assert b["returncode"] == 1 and b["failed_assertion"] == "exported_chat_has_one_bos"
    assert a["raw_ids"] == [9259] and b["raw_ids"] == [2, 9259]
    assert b["exported_studio_ids"] == [2] + a["exported_studio_ids"]
    assert a["candidate_boundary_ids"] == b["candidate_boundary_ids"] == a["exported_studio_ids"]
    assert a["versions"] == b["versions"]
    assert (
        a["source_hashes"]["studio/backend/core/inference/inference.py"]
        == b["source_hashes"]["studio/backend/core/inference/inference.py"]
    )
    assert (
        a["source_hashes"]["unsloth/chat_templates.py"]
        == b["source_hashes"]["unsloth/chat_templates.py"]
    )
    values["verdict"] = "PROVEN: baseline passes; PR fails only the single-BOS assertion"
    (output / "paired-result.json").write_text(json.dumps(values, indent = 2), encoding = "utf-8")
    summary = f"### PR 10312 paired proof\n\nBaseline `{BASE[:12]}`: **PASS**, one BOS.\n\nPR `{HEAD[:12]}`: **expected assertion failure**, two BOS.\n\nCandidate encoding simulation: one BOS; no product fix applied.\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding = "utf-8") as handle:
            handle.write(summary)
    print(values["verdict"], flush = True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument("mode", choices = ["probe", "paired"])
    parser.add_argument("--repo", required = True)
    parser.add_argument("--output", required = True)
    parser.add_argument("--snapshot")
    parser.add_argument("--label")
    parsed = parser.parse_args()
    raise SystemExit(probe(parsed) if parsed.mode == "probe" else paired(parsed))
