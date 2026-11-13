"""Compare one real slot reload with a fresh destination process using the same prompt."""
import argparse
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import gguf
import httpx
import numpy as np

from core.inference.llama_cpp import LlamaCppBackend
from core.inference import llama_keepwarm


def fixture(source, path, threshold, reordered):
    reader = gguf.GGUFReader(source)
    writer = gguf.GGUFWriter(path, "llama")
    key = "general.architecture"
    architecture = writer.kv_data[0].pop(key)
    if not reordered:
        writer.kv_data[0][key] = architecture
    writer.add_uint32("llama.rope.scaling.original_context_length", threshold)
    if reordered:
        writer.kv_data[0][key] = architecture
    writer.add_string("llama.rope.scaling.type", "longrope")
    writer.add_float32("llama.rope.scaling.factor", 2.0)
    for name, field in reader.fields.items():
        if name.startswith("GGUF.") or name == key:
            continue
        subtype = field.types[-1] if len(field.types) > 1 else None
        writer.add_key_value(name, field.contents(), field.types[0], subtype)
    for tensor in reader.tensors:
        writer.add_tensor(tensor.name, tensor.data, raw_dtype=tensor.tensor_type)
    for name, factor in (("long", 8.0), ("short", 1.0)):
        writer.add_tensor(f"rope_factors_{name}.weight", np.full(4, factor, dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


@contextmanager
def server(binary, model, context, directory, label):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = directory / f"{label}.log"
    args = [str(binary), "-m", str(model), "-c", str(context), "-np", "1", "-ngl", "0",
            "-t", "3", "--host", "127.0.0.1", "--port", str(port), "--slot-save-path",
            str(directory), "--no-warmup", "-lv", "4"]
    with log_path.open("w") as log, httpx.Client(trust_env=False, timeout=30) as client:
        child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(200):
                if child.poll() is not None:
                    raise RuntimeError(log_path.read_text()[-2000:])
                try:
                    if client.get(url + "/health").is_success:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("llama-server startup timed out")
            backend = LlamaCppBackend(manages_processes=False)
            backend._read_gguf_metadata(str(model))
            backend._process = child
            backend._port = port
            backend._healthy = True
            backend._requested_n_ctx = backend._effective_context_length = context
            backend._slot_save_binary = (str(binary), binary.stat().st_mtime_ns)
            backend._slot_save_dir = str(directory)
            backend._effective_parallel_slots = 1
            backend._stdout_lines = log_path.read_text().splitlines()
            backend._reconcile_effective_ctx_with_server(context)
            yield backend, client
        finally:
            child.terminate()
            child.wait(timeout=15)


def generate(backend, client, prompt):
    body = client.post(backend.base_url + "/completion", json={
        "prompt": prompt, "id_slot": 0, "cache_prompt": True, "temperature": 0,
        "n_predict": 1, "n_probs": 10, "post_sampling_probs": False,
    }).raise_for_status().json()
    return body["completion_probabilities"][0]["id"], body["timings"]["cache_n"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision", choices=["base", "head"])
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    root = Path(os.environ["UNSLOTH_STUDIO_HOME"])
    binary = Path(os.environ["LLAMA_SERVER_PATH"]).resolve()
    failures = []
    for case, old, new, threshold in [
        ("compatible", 1024, 2048, 2048),
        ("capped", 2048, 4096, 2048),
        ("reordered", 1024, 2048, 1024),
    ]:
        directory = root / case
        directory.mkdir(parents=True, exist_ok=True)
        model = directory / "longrope.gguf"
        fixture(args.model, model, threshold, case == "reordered")
        prompt = "The kite flies above the trees. " * 40
        with server(binary, model, old, directory, "old") as (backend, client):
            generate(backend, client, prompt)
            generate(backend, client, prompt + " Then")
            manifest = backend.save_slots_for_resume()
            assert manifest and manifest["slots"], f"{case}: populated slot was not saved"
            llama_keepwarm.stash_unload_kv(manifest)
        with server(binary, model, new, directory, "reload") as (backend, client):
            llama_keepwarm.note_model_loaded(backend)
            token, cached = generate(backend, client, prompt + " Then the")
        with server(binary, model, new, directory, "fresh") as (backend, client):
            cold, cold_cached = generate(backend, client, prompt + " Then the")
        expected_reuse = case == "compatible" and args.revision != "base"
        print(f"{case}: cached={cached} token={token} fresh={cold}", flush=True)
        if cold_cached or token != cold or (cached > 0) != expected_reuse:
            failures.append(case)
    if failures:
        raise SystemExit("KV regression: " + ", ".join(failures))


if __name__ == "__main__":
    main()
