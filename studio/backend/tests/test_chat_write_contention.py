# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""A temporarily slow writer must not abort concurrent chat persistence."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from storage import chat_generation_runs_db as runs
from storage import studio_db as db


@pytest.fixture(autouse = True)
def isolated_chat_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UNSLOTH_STUDIO_HOME", str(tmp_path))
    monkeypatch.setattr(db, "_schema_ready", False)
    monkeypatch.setattr(runs, "_schema_ready", False)
    yield
    db.close_wal_keeper()


def _seed(name, *, create_run = True):
    db.upsert_chat_thread({"id": name, "modelType": "gguf", "createdAt": 1})
    user = {
        "id": f"user-{name}",
        "threadId": name,
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
        "createdAt": 1,
    }
    db.upsert_chat_message(user)
    request = {
        "run_id": f"run-{name}",
        "owner_subject": "test",
        "thread_id": name,
        "user_message_id": user["id"],
        "assistant_message_id": f"assistant-{name}",
        "request_payload": {},
    }
    if create_run:
        runs.create_run(**request)
    return user, request


def test_chat_writes_survive_a_lock_longer_than_five_seconds():
    # These operations are called from synchronous routes or asyncio.to_thread.
    # Separate threads/runs prevent cancellation and completion racing each other.
    db.open_wal_keeper()
    try:
        _, create_request = _seed("create", create_run = False)
        tokens = {}
        for name in ("start", "append", "cancel", "finish"):
            _seed(name)
            tokens[name] = runs.get_worker_token(f"run-{name}")
            if name != "start":
                runs.mark_running(f"run-{name}", tokens[name])
        single, _ = _seed("single", create_run = False)
        batch, _ = _seed("batch", create_run = False)
        operations = {
            "create": lambda: runs.create_run(**create_request),
            "start": lambda: runs.mark_running("run-start", tokens["start"]),
            "append": lambda: runs.append_events(
                "run-append", tokens["append"], [("chunk", {"text": "preserved"})]
            ),
            "cancel": lambda: runs.request_cancel("run-cancel"),
            "finish": lambda: runs.finish_run(
                "run-finish", worker_token = tokens["finish"], status = "completed"
            ),
            "single": lambda: db.upsert_chat_message(single),
            "batch": lambda: db.sync_chat_messages("batch", [batch]),
        }
        entered = {name: threading.Event() for name in operations}

        def invoke(name, operation):
            entered[name].set()
            return operation()

        holder = db.get_connection()
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE chat_threads SET title='slow writer' WHERE id='append'")
            with ThreadPoolExecutor(max_workers = len(operations)) as executor:
                futures = {
                    name: executor.submit(invoke, name, operation)
                    for name, operation in operations.items()
                }
                try:
                    assert all(event.wait(10) for event in entered.values())
                    # WAL readers still work while all seven writers are waiting.
                    assert runs.get_run("run-append")["status"] == "running"
                    assert db.list_chat_messages("single")[0]["id"] == single["id"]
                    # Keep the production five-second default intact. This is a real
                    # SQLite lock, not a mocked OperationalError or shortened timeout.
                    time.sleep(6)
                finally:
                    holder.commit()
                failures = {}
                for name, future in futures.items():
                    try:
                        future.result(timeout = 35)
                    except Exception as exc:
                        failures[name] = repr(exc)
                assert not failures, failures
        finally:
            holder.close()

        assert runs.get_run("run-create")["status"] == "queued"
        assert runs.get_run("run-start")["status"] == "running"
        assert runs.get_run("run-cancel")["status"] == "cancelling"
        assert runs.get_run("run-finish")["status"] == "completed"
        chunks = [event for event in runs.list_events("run-append") if event["type"] == "chunk"]
        assert [event["payload"] for event in chunks] == [{"text": "preserved"}]
        assert db.get_chat_message("single", single["id"])["content"] == single["content"]
        assert db.get_chat_message("batch", batch["id"])["content"] == batch["content"]
    finally:
        db.close_wal_keeper()


def test_invalid_message_still_raises_integrity_error():
    # A contention policy must not turn unrelated DB errors into retries or success.
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_chat_message(
            {"id": "orphan", "threadId": "missing", "role": "user", "createdAt": 1}
        )


@pytest.mark.parametrize("operation", ["single", "batch", "append"])
def test_writer_wait_is_bounded(operation, monkeypatch):
    user, _ = _seed("bounded", create_run = operation == "append")
    token = runs.get_worker_token("run-bounded") if operation == "append" else None
    # Exercise the same deadline with a short budget rather than holding a test
    # transaction for thirty seconds. A permanent lock must still surface.
    monkeypatch.setattr(db, "_CONTENDED_BUSY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runs, "_CONTENDED_BUSY_TIMEOUT_SECONDS", 0.05)
    operations = {
        "single": lambda: db.upsert_chat_message(user),
        "batch": lambda: db.sync_chat_messages("bounded", [user]),
        "append": lambda: runs.append_events("run-bounded", token, [("chunk", {"text": "hi"})]),
    }
    holder = db.get_connection()
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match = "locked"):
            operations[operation]()
        assert time.monotonic() - started < 2
    finally:
        holder.rollback()
        holder.close()
