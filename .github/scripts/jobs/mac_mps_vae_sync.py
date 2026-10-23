"""Apple Silicon probe for unsloth#14940: Metal driver memory across tiled / framewise VAE calls.

Arms per case, one fresh process each: `none` (no hook), `base` (merge-base install_decoder_sync,
fetched at BASE_SHA), `head` (the checkout's install_vae_sync). Random-init diffusers VAEs at their
default (= Wan 2.1 / Qwen-Image) configs, bf16, sizes shrunk for a 7 GB runner.
Exit 0 measured, 3 skipped (no MPS), 1 crash.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

BASE_SHA = os.environ.get("PROBE_BASE_SHA", "d436a92f2c3a94c3d666e0c837af02fdb91c3e67")
MOD = "studio/backend/core/inference/diffusion_device.py"
CASES = ("wan_decode", "wan_encode", "qwen_decode", "qwen_encode")
ARMS = ("none", "base", "head")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def child(case, arm, base_path):
    import torch

    gib = 1024**3
    torch.manual_seed(0)
    dev = torch.device("mps")
    if case.startswith("wan"):
        from diffusers import AutoencoderKLWan as cls
    else:
        from diffusers import AutoencoderKLQwenImage as cls
    vae = cls().to(dev, torch.bfloat16).eval()
    if case.startswith("qwen"):
        vae.enable_tiling(
            tile_sample_min_height = 128,
            tile_sample_min_width = 128,
            tile_sample_stride_height = 96,
            tile_sample_stride_width = 96,
        )
    pipe = types.SimpleNamespace(vae = vae)
    target = types.SimpleNamespace(device = "mps")
    installed = None
    if arm == "base":
        installed = _load(base_path, "dd_base").install_decoder_sync(pipe, target)
    elif arm == "head":
        installed = _load(os.path.abspath(MOD), "dd_head").install_vae_sync(pipe, target)
    stats = {"calls": 0, "max_driver": 0}

    def rec(_m, _a, _o):
        stats["calls"] += 1
        stats["max_driver"] = max(stats["max_driver"], torch.mps.driver_allocated_memory())

    vae.decoder.register_forward_hook(rec)
    vae.encoder.register_forward_hook(rec)
    torch.mps.synchronize()
    torch.mps.empty_cache()
    start = torch.mps.driver_allocated_memory()
    t0 = time.perf_counter()
    with torch.no_grad():
        if case == "wan_decode":
            z = torch.randn(1, 16, 3, 24, 24, device = dev, dtype = torch.bfloat16)
            out = vae.decode(z).sample
        elif case == "wan_encode":
            x = torch.randn(1, 3, 9, 192, 192, device = dev, dtype = torch.bfloat16)
            out = vae.encode(x).latent_dist.mode()
        elif case == "qwen_decode":
            z = torch.randn(1, 16, 1, 48, 48, device = dev, dtype = torch.bfloat16)
            out = vae.decode(z).sample
        else:
            x = torch.randn(1, 3, 1, 384, 384, device = dev, dtype = torch.bfloat16)
            out = vae.encode(x).latent_dist.mode()
        torch.mps.synchronize()
    wall = time.perf_counter() - t0
    end = torch.mps.driver_allocated_memory()
    torch.mps.empty_cache()
    held = torch.mps.driver_allocated_memory()
    return {
        "case": case,
        "arm": arm,
        "installed": installed,
        "calls": stats["calls"],
        "out_shape": list(out.shape),
        "out_finite": bool(torch.isfinite(out.float()).all()),
        "peak_over_start_gib": round((stats["max_driver"] - start) / gib, 3),
        "end_over_start_gib": round((end - start) / gib, 3),
        "held_after_empty_cache_gib": round((held - start) / gib, 3),
        "wall_s": round(wall, 3),
        "recommended_max_gib": round(torch.mps.recommended_max_memory() / gib, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default = "mac_mps_vae_sync.json")
    ap.add_argument("--case")
    ap.add_argument("--arm")
    ap.add_argument("--base-path")
    ap.add_argument("--tiny", action = "store_true")
    a = ap.parse_args()
    if a.case:
        print("PROBE_ROW " + json.dumps(child(a.case, a.arm, a.base_path)), flush = True)
        return 0
    import torch

    res = {
        "job": "mac_mps_vae_sync",
        "torch": torch.__version__,
        "base_sha": BASE_SHA,
        "mps": torch.backends.mps.is_available(),
        "rows": [],
        "errors": [],
    }
    if not res["mps"]:
        res["skipped"] = "torch.backends.mps.is_available() is False on this runner"
        json.dump(res, open(a.out, "w"), indent = 1)
        print("JOB_RESULT " + json.dumps(res))
        return 3
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "diffusers>=0.35"], check = True)
    import diffusers

    res["diffusers"] = diffusers.__version__
    base_path = os.path.join(tempfile.mkdtemp(), "diffusion_device_base.py")
    url = f"https://raw.githubusercontent.com/unslothai/unsloth/{BASE_SHA}/{MOD}"
    with urllib.request.urlopen(url, timeout = 60) as r, open(base_path, "wb") as f:
        f.write(r.read())
    for case in CASES:
        for arm in ARMS:
            p = subprocess.run(
                [sys.executable, __file__, "--case", case, "--arm", arm, "--base-path", base_path],
                capture_output = True,
                text = True,
                timeout = 900,
            )
            row = next(
                (
                    json.loads(ln[10:])
                    for ln in p.stdout.splitlines()
                    if ln.startswith("PROBE_ROW ")
                ),
                None,
            )
            if row is None:
                res["errors"].append(
                    {
                        "case": case,
                        "arm": arm,
                        "rc": p.returncode,
                        "tail": (p.stdout + p.stderr)[-1500:],
                    }
                )
            else:
                res["rows"].append(row)
                print(json.dumps(row), flush = True)
            json.dump(res, open(a.out, "w"), indent = 1)
    res["passed"] = not res["errors"]
    json.dump(res, open(a.out, "w"), indent = 1)
    print("JOB_RESULT " + json.dumps(res))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
