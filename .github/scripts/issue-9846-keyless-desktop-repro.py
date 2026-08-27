# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Reproduce issue #9846 against the real Studio authentication dependencies."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "studio" / "backend"))


def _resolve_subject(credentials: HTTPAuthorizationCredentials) -> str:
    from auth.authentication import get_current_subject

    return asyncio.run(get_current_subject(credentials))


def _loopback_request() -> Request:
    state = SimpleNamespace(
        bind_host = "127.0.0.1",
        secure = False,
        remote_access_is_colab = False,
        lan_access_is_colab = False,
        lan_access_secure_launch = False,
        cloudflare_url = None,
    )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "root_path": "",
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
            "headers": [(b"host", b"127.0.0.1:8000")],
            "app": SimpleNamespace(state = state),
        }
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix = "unsloth-9846-") as studio_home:
        os.environ["UNSLOTH_STUDIO_HOME"] = studio_home

        # Imports follow UNSLOTH_STUDIO_HOME because the storage paths are resolved
        # when these modules load, exactly as they are during Desktop startup.
        from auth import storage
        from auth.authentication import create_access_token, security
        from utils.keyless_api_access import set_keyless_api_access

        storage.create_initial_user(
            username = storage.DEFAULT_ADMIN_USERNAME,
            password = "desktop-bootstrap-password",
            jwt_secret = secrets.token_urlsafe(64),
            must_change_password = True,
        )
        assert storage.requires_password_change(storage.DEFAULT_ADMIN_USERNAME)

        # Desktop deliberately leaves the web bootstrap-password flag set, but its
        # session token carries a marker that bypasses the web-only password gate.
        desktop_token = create_access_token(storage.DEFAULT_ADMIN_USERNAME, desktop = True)
        assert _resolve_subject(
            HTTPAuthorizationCredentials(scheme = "Bearer", credentials = desktop_token)
        ) == storage.DEFAULT_ADMIN_USERNAME
        print("PASS Desktop session is admitted while the bootstrap-password flag remains set")

        api_key, _ = storage.create_api_key(
            username = storage.DEFAULT_ADMIN_USERNAME,
            name = "issue-9846-control",
            expires_at = None,
        )
        assert _resolve_subject(
            HTTPAuthorizationCredentials(scheme = "Bearer", credentials = api_key)
        ) == storage.DEFAULT_ADMIN_USERNAME
        print("PASS bearer API key control is admitted")

        set_keyless_api_access("inference", tools = False)
        request = _loopback_request()
        keyless_credentials = asyncio.run(security(request))
        assert keyless_credentials.scheme == "Keyless"
        assert request.state.keyless_api_admitted is True

        try:
            _resolve_subject(keyless_credentials)
        except HTTPException as exc:
            assert exc.status_code == 403, exc
            assert exc.detail == "Password change required", exc
            print(
                "REPRODUCED issue #9846: keyless Desktop request returns "
                "403 Password change required"
            )
            return

        raise AssertionError(
            "NOT REPRODUCED: keyless Desktop request was admitted despite the "
            "bootstrap-password flag"
        )


if __name__ == "__main__":
    main()
