# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Focused regression proof for the PR 9102 keyless tool-policy race."""

import asyncio
import secrets
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from auth import authentication, storage
from auth.authentication import get_current_credential, security
from state.tool_policy import (
    get_tool_policy_default,
    reset_tool_policy,
    set_tool_policy_default,
)
from utils.keyless_api_access import (
    KeylessToolPolicyMiddleware,
    _reset_scope_cache,
    set_keyless_api_access,
)


@pytest.fixture(autouse = True)
def isolated_auth_and_tool_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(storage, "_BOOTSTRAP_PW_PATH", tmp_path / ".bootstrap_password")
    monkeypatch.setattr(storage, "_bootstrap_password", None)
    monkeypatch.setattr(storage, "_api_key_pbkdf2_salt_cache", None)
    storage._reset_api_key_hash_cache()
    _reset_scope_cache()
    reset_tool_policy()
    yield
    storage._reset_api_key_hash_cache()
    _reset_scope_cache()
    reset_tool_policy()


def test_revoked_key_fallback_cannot_keep_server_tools_enabled(monkeypatch):
    storage.create_initial_user(
        username = storage.DEFAULT_ADMIN_USERNAME,
        password = "human-password-123",
        jwt_secret = secrets.token_urlsafe(64),
    )
    set_keyless_api_access("inference", tools = False)
    set_tool_policy_default(True)
    raw_key, row = storage.create_api_key(
        username = storage.DEFAULT_ADMIN_USERNAME,
        name = "race-repro",
        expires_at = None,
    )

    real_validation = authentication.bearer_is_valid_api_key
    revoked = False

    def validate_then_revoke(token):
        nonlocal revoked
        valid = real_validation(token)
        if valid and not revoked:
            storage.revoke_api_key(storage.DEFAULT_ADMIN_USERNAME, row["id"])
            revoked = True
        return valid

    monkeypatch.setattr(authentication, "bearer_is_valid_api_key", validate_then_revoke)
    observed = {}
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "headers": [(b"authorization", f"Bearer {raw_key}".encode())],
        "app": SimpleNamespace(state = SimpleNamespace(bind_host = "127.0.0.1")),
    }

    async def downstream(asgi_scope, receive, send):
        credentials = await security(Request(asgi_scope, receive))
        observed["subject"], _generation = await get_current_credential(credentials)
        observed["tools_policy"] = get_tool_policy_default()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(KeylessToolPolicyMiddleware(downstream)(scope, receive, send))

    print(
        f"fallback_subject={observed['subject']} "
        f"tools_policy={observed['tools_policy']}"
    )
    assert revoked is True
    assert observed["subject"] == storage.DEFAULT_ADMIN_USERNAME
    assert observed["tools_policy"] is False, (
        "REPRODUCED: the revoked key fell back to keyless admin after the middleware "
        "left server tools enabled"
    )
