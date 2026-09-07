# SPDX-License-Identifier: AGPL-3.0-only
"""Verify PR 10264's public cache gate against a TCP listener that never replies."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path


def load_helper(path: Path):
    spec = importlib.util.spec_from_file_location("pr10264_latest_hf_tokens", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blackhole_endpoint() -> str:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def accept_and_hold() -> None:
        connection, _ = server.accept()
        try:
            time.sleep(30)
        finally:
            connection.close()
            server.close()

    threading.Thread(target=accept_and_hold, name="test-blackhole", daemon=True).start()
    host, port = server.getsockname()
    return f"http://{host}:{port}"


def main() -> int:
    import huggingface_hub

    source = Path(sys.argv[1])
    helper = load_helper(source)
    original_api = huggingface_hub.HfApi
    endpoint = blackhole_endpoint()
    huggingface_hub.HfApi = lambda: original_api(endpoint=endpoint)
    helper._hub_offline = lambda: False
    helper.reset_repo_access_cache()

    before = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    allowed = helper.cache_reads_authorized("hf_test", repo_id="org/private")
    elapsed = time.monotonic() - started
    extra_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.ident not in before and thread.name != "test-blackhole"
    ]
    result = {
        "huggingface_hub": huggingface_hub.__version__,
        "allowed": allowed,
        "elapsed_seconds": round(elapsed, 3),
        "extra_probe_threads": extra_threads,
    }
    print(json.dumps(result, sort_keys=True))
    passed = not allowed and 9.0 <= elapsed <= 13.0 and not extra_threads
    print(f"HTTP_TIMEOUT_FIX={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
