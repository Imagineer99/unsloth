#!/usr/bin/env python3
"""Deterministic Windows A/B probe for issue #9934's SQLite write policy."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile


BATCHES = 64


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix = "unsloth-9934-"))
    os.environ["UNSLOTH_STUDIO_HOME"] = str(root)

    from storage import chat_generation_runs_db as runs
    from storage import studio_db
    from utils.paths import studio_db_path

    studio_db.upsert_chat_thread(
        {"id": "thread", "title": "Chat", "modelType": "base", "createdAt": 1}
    )
    studio_db.upsert_chat_message(
        {
            "id": "user",
            "threadId": "thread",
            "role": "user",
            "content": [{"type": "text", "text": "Hello"}],
            "createdAt": 2,
        }
    )
    runs.create_run(
        run_id = "run",
        owner_subject = "owner",
        thread_id = "thread",
        user_message_id = "user",
        assistant_message_id = "assistant",
        request_payload = {"model": "local", "stream": True},
    )
    worker_token = runs.get_worker_token("run")
    assert worker_token is not None

    db_path = studio_db_path()
    wal_path = Path(f"{db_path}-wal")
    fixed = hasattr(studio_db, "open_wal_keeper")
    keeper = studio_db.open_wal_keeper() if fixed else None
    before = digest(db_path)
    previous = before
    main_db_rewrites = 0
    wal_missing_after_batch = 0
    statements: list[str] = []
    real_get_connection = runs.get_connection

    def traced_connection():
        conn = real_get_connection()
        conn.set_trace_callback(statements.append)
        return conn

    runs.get_connection = traced_connection
    try:
        for index in range(BATCHES):
            runs.append_events(
                "run",
                worker_token,
                [("chunk", {"choices": [{"delta": {"content": str(index)}}]})],
            )
            current = digest(db_path)
            main_db_rewrites += current != previous
            previous = current
            wal_missing_after_batch += not wal_path.exists()
    finally:
        runs.get_connection = real_get_connection

    normalized = [statement.upper().replace(" ", "") for statement in statements]
    normal_before_begin = False
    if "PRAGMASYNCHRONOUS=NORMAL" in normalized and "BEGINIMMEDIATE" in normalized:
        normal_before_begin = normalized.index("PRAGMASYNCHRONOUS=NORMAL") < normalized.index(
            "BEGINIMMEDIATE"
        )

    runs.finish_run(
        "run",
        worker_token = worker_token,
        status = "completed",
        finish_reason = "stop",
    )
    events = runs.list_events("run")
    if keeper is not None:
        keeper.close()

    check = studio_db.get_connection()
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        ordinary_sync = check.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        check.close()

    print(
        "RESULT",
        f"policy={'fixed' if fixed else 'baseline'}",
        f"batches={BATCHES}",
        f"main_db_rewrites={main_db_rewrites}",
        f"wal_missing_after_batch={wal_missing_after_batch}",
        f"normal_before_begin={normal_before_begin}",
        f"ordinary_sync={ordinary_sync}",
        f"events={len(events)}",
        f"integrity={integrity}",
    )

    assert fixed, "REPRO: production has no lifespan WAL keeper"
    assert main_db_rewrites == 0, "stream batches checkpointed back into studio.db"
    assert wal_missing_after_batch == 0, "WAL was checkpointed/deleted between stream batches"
    assert normal_before_begin, "recoverable stream commits still use FULL synchronization"
    assert ordinary_sync == 2, "the scoped policy weakened ordinary/final transactions"
    assert len(events) == BATCHES + 2
    assert integrity == "ok"
    assert not wal_path.exists(), "the lifespan keeper did not checkpoint cleanly on close"
    print("PASS issue-9934 durable stream writes are scoped and checkpointed once")


if __name__ == "__main__":
    main()
