# SPDX-License-Identifier: AGPL-3.0-only
"""Baseline / PR / candidate proof using real processors and Studio dispatch."""
from __future__ import annotations
import argparse
import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import types
from typing import Optional, Generator
import pr10312_bos_ab as proof


def studio(repo):
    path = repo / "studio/backend/core/inference/inference.py"
    nodes = [n for n in ast.walk(proof.tree(path)) if isinstance(n, ast.FunctionDef)
             and n.name in {"_generate_chat_response_inner", "generate_stream"}]
    assert len(nodes) == 2
    helper_path = repo / "studio/backend/core/inference/chat_template_helpers.py"
    spec = importlib.util.spec_from_file_location("core.inference.chat_template_helpers", helper_path)
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    # The registry is intentionally empty: test a user-exported tokenizer's template.
    proof.module("utils.datasets", {"MODEL_TO_TEMPLATE_MAPPER": {}, "get_tokenizer_chat_template": None})
    scope = {"Optional": Optional, "Generator": Generator, "logger": logging.getLogger("fix-proof"),
             "trailing_assistant_text": lambda messages: messages[-1]["content"] if messages[-1]["role"] == "assistant" else ""}
    dispatch = next(n for n in nodes if n.name == "_generate_chat_response_inner")
    exec(compile(ast.Module(body=[dispatch], type_ignores=[]), str(path), "exec"), scope)
    stream = next(n for n in nodes if n.name == "generate_stream")
    assignments = [n for n in ast.walk(stream) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "inputs" for t in n.targets)]
    assert len(assignments) == 1
    defaults = {a.arg: ast.literal_eval(d) for a, d in zip(stream.args.args[-len(stream.args.defaults):], stream.args.defaults)}
    def encode(tokenizer, prompt, **kwargs):
        ns = {**defaults, **kwargs, "tokenizer": tokenizer, "prompt": prompt,
              "model": types.SimpleNamespace(device="cpu")}
        exec(compile(ast.Module(body=assignments, type_ignores=[]), str(path), "exec"), ns)
        return ns["inputs"]["input_ids"][0].tolist()
    def chat(processor, messages, fail=False):
        tok = getattr(processor, "tokenizer", processor)
        def render(tokenizer, messages, **kwargs):
            if fail:
                raise ValueError("deliberate template failure")
            continued = kwargs.get("continue_final_message", False)
            return tokenizer.apply_chat_template(messages, tokenize=False,
                add_generation_prompt=not continued, continue_final_message=continued)
        def capture(prompt, *args, **kwargs):
            yield {"prompt": prompt, "ids": encode(tok, prompt, **kwargs),
                   "specials": kwargs.get("add_special_tokens", defaults.get("add_special_tokens", True))}
        backend = types.SimpleNamespace(active_model_name="local-export",
            models={"local-export": {"tokenizer": processor}}, _normalize_top_k=lambda x:x,
            _apply_chat_template_for_generation=render, generate_stream=capture,
            format_chat_prompt=lambda *a, **k: "Hello")
        result = list(scope["_generate_chat_response_inner"](backend, messages,
                      continue_final_message=messages[-1]["role"] == "assistant"))
        assert len(result) == 1
        return result[0]
    return encode, chat


def probe(args):
    from transformers import AutoProcessor, AutoConfig, PreTrainedTokenizerFast
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    repo, output = Path(args.repo).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = Path(args.snapshot)
    assert proof.source_hash(snapshot / "tokenizer.json") == proof.TOKENIZER_SHA256
    config = AutoConfig.from_pretrained(snapshot, local_files_only=True)
    processor = proof.load_boundary(repo, AutoProcessor.from_pretrained(snapshot, local_files_only=True), config)
    encode, chat = studio(repo)
    raw = encode(processor.tokenizer, "Hello")
    processor = proof.attach_template(repo, processor)
    messages = [{"role":"user", "content":"Hello"}]
    direct = chat(processor, messages)
    processor.save_pretrained(output / "export")
    processor = proof.load_boundary(repo, AutoProcessor.from_pretrained(output / "export", local_files_only=True), config)
    exported = chat(processor, messages)
    assert direct["ids"] == exported["ids"]
    cases = []
    templates = [processor.tokenizer.chat_template, " \n" + processor.tokenizer.chat_template]
    for template_index, template in enumerate(templates):
        processor.tokenizer.chat_template = template
        for text in ["Hello", "", "你好 🦥", "<bos> inside text", "two\nlines"]:
            for continuation in [False, True]:
                msgs = [{"role":"user", "content":text}]
                if continuation:
                    msgs += [{"role":"assistant", "content":"Partial answer"}]
                result = chat(processor, msgs)
                expected = processor.tokenizer(result["prompt"], add_special_tokens=False)["input_ids"]
                cases.append({"template":template_index, "text":text, "continuation":continuation,
                              "matches_template_tokens":result["ids"] == expected})
    fallback = chat(processor, messages, fail=True)
    assert fallback["ids"] == raw and fallback["specials"] is True
    # Real Rust tokenizers with optional BOS/EOS post-processing exercise other policies.
    control_count = 0
    for bos in [False, True]:
        for eos in [False, True]:
            backend = Tokenizer(models.WordLevel({"[UNK]":0,"<s>":1,"</s>":2,"Hello":3}, unk_token="[UNK]"))
            backend.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
            single = ("<s> " if bos else "") + "$A" + (" </s>" if eos else "")
            backend.post_processor = processors.TemplateProcessing(single=single, special_tokens=[("<s>",1),("</s>",2)])
            tok = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]",
                                         bos_token="<s>" if bos else None, eos_token="</s>" if eos else None)
            for template in ["Hello", ("<s> " if bos else "") + "Hello" + (" </s>" if eos else ""), " \nHello"]:
                tok.chat_template = template
                assert encode(tok,"Hello") == tok("Hello")["input_ids"]
                value = chat(tok, messages)
                if args.label == "fixed":
                    assert value["ids"] == tok(value["prompt"], add_special_tokens=False)["input_ids"]
                control_count += 1
    success = all(c["matches_template_tokens"] for c in cases)
    result = {"label": args.label, "raw_ids":raw, "direct":direct, "exported":exported,
              "cases":cases, "control_count":control_count, "fallback":fallback,
              "source_sha":subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip(),
              "inference_sha256":proof.source_hash(repo / "studio/backend/core/inference/inference.py"),
              "versions":{n:proof.importlib.metadata.version(n) for n in ["transformers","tokenizers","torch","unsloth-zoo"]},
              "failed_assertion":None if success else "rendered_chat_matches_template_tokens"}
    (output / "result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))
    return 0 if success else 1


def paired(args):
    from huggingface_hub import snapshot_download
    root, output = Path(args.repo).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = args.snapshot or snapshot_download(proof.MODEL, revision=proof.REVISION, cache_dir=output / "hf-cache",
        allow_patterns=["config.json","tokenizer.json","tokenizer_config.json","processor_config.json"])
    results = {}
    for label, sha in [("baseline",proof.BASE),("pr",proof.HEAD),("fixed",None)]:
        repo = root if sha is None else output / ("source-" + label)
        if sha and not repo.exists():
            subprocess.run(["git","worktree","add","--detach",str(repo),sha],cwd=root,check=True)
        if sha:
            assert subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip() == sha
        target = output / label
        target.mkdir(exist_ok=True)
        (target / "result.json").unlink(missing_ok=True)
        with (output / (label + ".log")).open("w",encoding="utf-8") as log:
            proc = subprocess.run([sys.executable,__file__,"probe","--repo",str(repo),"--output",str(target),
                "--snapshot",str(snapshot),"--label",label],stdout=log,stderr=subprocess.STDOUT)
        assert (target / "result.json").exists(), f"{label}: setup failure; inspect log"
        r = json.loads((target / "result.json").read_text(encoding="utf-8"))
        r["returncode"] = proc.returncode
        results[label] = r
        assert proc.returncode == (1 if label == "pr" else 0), r
        assert r["raw_ids"] == ([9259] if label == "baseline" else [2,9259])
        print(label, "exit", proc.returncode, "chat",r["exported"]["ids"][:5],"raw",r["raw_ids"],flush=True)
    assert results["pr"]["failed_assertion"] == "rendered_chat_matches_template_tokens"
    assert results["fixed"]["exported"]["ids"] == results["baseline"]["exported"]["ids"]
    assert results["pr"]["exported"]["ids"] == [2] + results["fixed"]["exported"]["ids"]
    assert results["baseline"]["versions"] == results["pr"]["versions"] == results["fixed"]["versions"]
    (output / "paired-result.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    summary = "Baseline PASS, PR expected duplicate-BOS FAIL, candidate PASS. Raw completion BOS preserved; 20 real Gemma chat cases and 12 tokenizer policy controls per revision."
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a",encoding="utf-8") as f:f.write(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["probe","paired"])
    parser.add_argument("--repo",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--label")
    args = parser.parse_args()
    raise SystemExit(probe(args) if args.mode == "probe" else paired(args))
