# SPDX-License-Identifier: AGPL-3.0-only
"""Real-pull A/B probe for upstream PR 10222.

The base branch must fail because Studio hides the pulled model. The head branch
must list exactly one row, preserve conservative rejection of load-bearing or
unknown layers, and materialize the exact pulled model blob as a GGUF.
"""

import json
import os
from pathlib import Path

from hub.services.models.ollama import (
    _unsupported_ollama_layer_media_types,
    is_ollama_manifest_ref,
    materialize_ollama_model_ref,
    scan_ollama_dir,
)


MODELS_DIR = Path(os.environ["OLLAMA_MODELS"]).expanduser().resolve()
EXPECTED_TAG = os.environ.get("PROBE_MODEL", "qwen2.5:0.5b")
REJECTED_MEDIA_TYPES = (
    "application/vnd.ollama.image.adapter",
    "application/vnd.ollama.image.future-runtime",
    "application/vnd.ollama.image.tensor",
    "application/vnd.ollama.image.json",
)


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}", flush = True)


def fail(message: str, *, result: str = "UNEXPECTED") -> None:
    print(f"FAIL: {message}")
    print(f"REPRO-RESULT={result}")
    raise SystemExit(1)


banner("1. Inspect the manifest written by the real ollama pull")
print(f"OLLAMA_MODELS={MODELS_DIR}")
manifests = sorted(
    path for path in (MODELS_DIR / "manifests").rglob("*") if path.is_file()
)
if len(manifests) != 1:
    fail(f"expected one manifest, found {len(manifests)}")

manifest_path = manifests[0]
manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
layers = manifest.get("layers", [])
print(f"manifest={manifest_path.relative_to(MODELS_DIR)}")
for layer in layers:
    print(f"{layer.get('mediaType', '<missing>'):<52} {layer.get('size', 0):>13,} bytes")

model_layers = [
    layer
    for layer in layers
    if layer.get("mediaType") == "application/vnd.ollama.image.model"
]
if len(model_layers) != 1:
    fail(f"expected one primary model layer, found {len(model_layers)}")

unsupported = _unsupported_ollama_layer_media_types(layers)
print(f"unsupported_media_types={list(unsupported)}")

banner("2. Scan Studio inventory over that exact store")
rows = scan_ollama_dir(MODELS_DIR)
print(f"scan_ollama_dir() returned {len(rows)} row(s)")
for row in rows:
    print(f"display_name={row.display_name!r}")
    print(f"source={row.source} format={row.model_format} runtime={row.runtime}")
    print(f"size_bytes={row.size_bytes:,}")

if not rows:
    fail(
        f"{EXPECTED_TAG} is on disk but Studio lists zero Ollama rows; withheld over {list(unsupported)}",
        result="DEFECT-REPRODUCED",
    )
if len(rows) != 1:
    fail(f"expected exactly one isolated-store row, found {len(rows)}")

row = rows[0]
if not row.display_name.startswith(EXPECTED_TAG):
    fail(f"unexpected display name: {row.display_name!r}")
if row.source != "ollama" or row.model_format != "gguf" or row.runtime != "llama_cpp":
    fail(f"unexpected row classification: {row!r}")
if unsupported:
    fail(f"head still rejects pulled manifest metadata: {list(unsupported)}")

banner("3. Preserve conservative rejection of non-admitted layers")
for media_type in REJECTED_MEDIA_TYPES:
    rejected = _unsupported_ollama_layer_media_types(
        [{"mediaType": "application/vnd.ollama.image.model"}, {"mediaType": media_type}]
    )
    print(f"{media_type}: {list(rejected)}")
    if rejected != (media_type,):
        fail(f"{media_type} was unexpectedly admitted")

banner("4. Resolve the inventory row through Studio's real load path")
ref = row.load_id or row.id
if not is_ollama_manifest_ref(ref):
    fail(f"load id is not an ollama-manifest ref: {ref[:100]}")

resolved = Path(materialize_ollama_model_ref(ref))
digest = model_layers[0].get("digest", "")
if not digest.startswith("sha256:"):
    fail(f"unexpected primary model digest: {digest!r}")
blob = MODELS_DIR / "blobs" / digest.replace(":", "-")
print(f"resolved={resolved}")
print(f"source_blob={blob}")
print(f"gguf_size={resolved.stat().st_size:,}")

if resolved.suffix != ".gguf" or not resolved.is_file():
    fail("materialized path is not an existing .gguf")
if not blob.is_file():
    fail("primary model blob is missing")
if not resolved.samefile(blob):
    fail("materialized GGUF does not reference the pulled primary model blob")

print(f"PASS: {EXPECTED_TAG} is listed and resolves to the exact pulled GGUF blob.")
print("REPRO-RESULT=FIXED")
