# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic A/B proof for PR 10264's lost Hub probe timeout."""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


BEFORE_REF = "4e3be385d8f25d2151da80cd9d4859745f8aae43"
HELPER_PATH = Path("studio/backend/hub/utils/hf_tokens.py")


def _load_helper(path: Path, variant: str):
    spec = importlib.util.spec_from_file_location(f"pr10264_hf_tokens_{variant}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blackhole_endpoint() -> str:
    """Accept TCP connections without ever sending an HTTP response."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def hold(connection: socket.socket) -> None:
        time.sleep(60)
        connection.close()

    def accept_connections() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=hold, args=(connection,), daemon=True).start()

    threading.Thread(target=accept_connections, daemon=True).start()
    host, port = server.getsockname()
    return f"http://{host}:{port}"


def _worker(variant: str, source: Path) -> int:
    import huggingface_hub

    endpoint = _blackhole_endpoint()
    helper = _load_helper(source, variant)
    original_api = huggingface_hub.HfApi

    if variant == "before":
        # 4e3be385 imports HfApi inside the probe and calls
        # repo_info(..., timeout=10). Keep that exact implementation.
        huggingface_hub.HfApi = lambda token=None: original_api(
            endpoint=endpoint,
            token=token,
        )
    else:
        # 7a1626e0 imports the package-level auth_check bound method. Redirect
        # only its endpoint; do not otherwise alter the implementation.
        huggingface_hub.auth_check = original_api(endpoint=endpoint).auth_check

    print(json.dumps({"event": "started", "variant": variant}), flush=True)
    started = time.monotonic()
    allowed = helper._probe_repo_access("org/repo", "hf_pr10264_test_token", "model")
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "event": "returned",
                "variant": variant,
                "allowed": allowed,
                "elapsed_seconds": round(elapsed, 3),
            }
        ),
        flush=True,
    )
    return 0


def _extract_before(repo: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{BEFORE_REF}:{HELPER_PATH.as_posix()}"],
        check=True,
        capture_output=True,
    )
    destination.write_bytes(result.stdout)


def _run_variant(
    variant: str,
    source: Path,
    *,
    deadline: float,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        variant,
        "--source",
        str(source),
    ]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=deadline)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        timed_out = True
    return {
        "variant": variant,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_code": process.returncode,
        "worker_output": output.strip().splitlines(),
    }


def _controller(repo: Path, deadline: float, before_source_arg: Path | None) -> int:
    import huggingface_hub

    with tempfile.TemporaryDirectory(prefix="pr10264-timeout-ab-") as raw_temp:
        if before_source_arg is None:
            before_source = Path(raw_temp) / "hf_tokens_before.py"
            _extract_before(repo, before_source)
        else:
            before_source = before_source_arg.resolve()
        after_source = repo / HELPER_PATH
        observations = [
            _run_variant("before", before_source, deadline=deadline),
            _run_variant("after", after_source, deadline=deadline),
        ]

    for observation in observations:
        print(f"A_B_OBSERVATION={json.dumps(observation, sort_keys=True)}", flush=True)

    before, after = observations
    passed = (
        not before["timed_out"]
        and before["exit_code"] == 0
        and before["elapsed_seconds"] >= 9
        and after["timed_out"]
    )
    print(f"HF_HUB_VERSION={huggingface_hub.__version__}", flush=True)
    print(f"BEFORE_BOUNDED={str(not before['timed_out']).lower()}", flush=True)
    print(f"AFTER_EXCEEDED_{deadline:g}S={str(after['timed_out']).lower()}", flush=True)
    print(f"A_B_PROOF={'PASS' if passed else 'FAIL'}", flush=True)
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--before-source", type=Path)
    parser.add_argument("--deadline", type=float, default=13.0)
    parser.add_argument("--worker", choices=("before", "after"))
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.source is None:
            parser.error("--worker requires --source")
        return _worker(args.worker, args.source)
    return _controller(args.repo.resolve(), args.deadline, args.before_source)


if __name__ == "__main__":
    raise SystemExit(main())
