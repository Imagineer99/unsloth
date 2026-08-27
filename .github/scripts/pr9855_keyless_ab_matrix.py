# SPDX-License-Identifier: AGPL-3.0-only
"""External A/B auth matrix for PR 9855; never copied into the product worktree."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from types import ModuleType
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required = True)
    parser.add_argument("--expected", choices = ("blocked", "allowed"), required = True)
    parser.add_argument("--commit")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    backend = root / "studio" / "backend"
    sys.path.insert(0, str(backend))

    run_root = Path(__file__).resolve().parent / "runs"
    run_root.mkdir(exist_ok = True)
    with tempfile.TemporaryDirectory(prefix = "auth-matrix-", dir = run_root) as temp_name:
        home = Path(temp_name)
        os.environ["UNSLOTH_STUDIO_HOME"] = str(home)

        from fastapi import Depends, FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from starlette.requests import Request

        from auth import storage
        import auth.authentication as authentication
        from auth.authentication import create_access_token, get_current_subject, security
        from utils import host_policy
        from utils.keyless_api_access import _reset_scope_cache, set_keyless_api_access

        storage.DB_PATH = home / "auth.db"
        storage._BOOTSTRAP_PW_PATH = home / ".bootstrap_password"
        storage._bootstrap_password = None
        storage._api_key_pbkdf2_salt_cache = None
        storage._reset_api_key_hash_cache()
        _reset_scope_cache()

        storage.create_initial_user(
            username = storage.DEFAULT_ADMIN_USERNAME,
            password = "human-password-123",
            jwt_secret = secrets.token_urlsafe(64),
            must_change_password = True,
        )

        lan_access = ModuleType("lan_access")
        lan_access.lan_listener_status = lambda: {
            "running": True,
            "port": 8888,
            "addresses": ["192.168.1.24"],
        }
        sys.modules["lan_access"] = lan_access
        host_policy._lan_connector_active = True
        host_policy._remote_connector_active = False

        counts = {
            "reported_positive_cases": 0,
            "common_invariants": 0,
        }

        def app_state(**overrides):
            state = SimpleNamespace(
                bind_host = "127.0.0.1",
                secure = False,
                remote_access_is_colab = False,
                lan_access_is_colab = False,
                lan_access_secure_launch = False,
                cloudflare_url = None,
            )
            for name, value in overrides.items():
                setattr(state, name, value)
            return state

        def request_for(
            *,
            path = "/v1/chat/completions",
            method = "POST",
            root_path = "",
            headers = None,
            state = None,
            server = ("127.0.0.1", 8000),
            client = ("127.0.0.1", 50000),
        ):
            return Request(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "root_path": root_path,
                    "query_string": b"",
                    "scheme": "http",
                    "server": server,
                    "client": client,
                    "headers": [
                        (name.lower().encode(), value.encode())
                        for name, value in (headers or {}).items()
                    ],
                    "app": SimpleNamespace(state = state or app_state()),
                }
            )

        def bearer_request(token, **kwargs):
            return request_for(headers = {"Authorization": f"Bearer {token}"}, **kwargs)

        def subject_of(request):
            credentials = asyncio.run(security(request))
            return asyncio.run(get_current_subject(credentials))

        def outcome(request):
            try:
                return ("ok", subject_of(request))
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

        expected_reported = (
            ("ok", storage.DEFAULT_ADMIN_USERNAME)
            if args.expected == "allowed"
            else (403, "Password change required")
        )

        inference_routes = [
            ("POST", "/v1/chat/completions"),
            ("POST", "/v1/chat/count_tokens"),
            ("POST", "/v1/completions"),
            ("POST", "/v1/embeddings"),
            ("POST", "/v1/messages"),
            ("POST", "/v1/messages/count_tokens"),
            ("POST", "/v1/responses"),
            ("GET", "/v1/models"),
            ("GET", "/v1/models/unsloth/model"),
        ]
        transports = [
            {},
            {
                "server": ("::1", 8000),
                "client": ("::1", 50000),
                "state": app_state(bind_host = "::1"),
            },
            {
                "server": ("192.168.1.24", 8888),
                "client": ("192.168.1.90", 54321),
            },
        ]
        dummy_bearers = (None, "not-needed", "lm-studio", "ollama")

        set_keyless_api_access("inference")
        for method, path in inference_routes:
            for transport in transports:
                for token in dummy_bearers:
                    request = (
                        request_for(method = method, path = path, **transport)
                        if token is None
                        else bearer_request(token, method = method, path = path, **transport)
                    )
                    assert outcome(request) == expected_reported, (method, path, transport, token)
                    counts["reported_positive_cases"] += 1

        for path, root_path in (
            ("/v1/models/", ""),
            ("/studio/v1/models", "/studio"),
            ("/studio/v1/models/unsloth/model/", "/studio"),
        ):
            for token in dummy_bearers:
                request = (
                    request_for(method = "GET", path = path, root_path = root_path)
                    if token is None
                    else bearer_request(token, method = "GET", path = path, root_path = root_path)
                )
                assert outcome(request) == expected_reported
                counts["reported_positive_cases"] += 1

        set_keyless_api_access("full")
        for method, path in (
            ("POST", "/api/train/start"),
            ("DELETE", "/api/training/history/123"),
            ("GET", "/api/settings"),
        ):
            for transport in transports[:2]:
                for token in dummy_bearers:
                    request = (
                        request_for(method = method, path = path, **transport)
                        if token is None
                        else bearer_request(token, method = method, path = path, **transport)
                    )
                    assert outcome(request) == expected_reported
                    counts["reported_positive_cases"] += 1

        # Exercise the real FastAPI dependency and HTTP response shape used by curl/SDKs.
        set_keyless_api_access("inference")
        app = FastAPI()
        for name, value in vars(app_state()).items():
            setattr(app.state, name, value)

        @app.get("/v1/models")
        async def models(current_subject: str = Depends(get_current_subject)):
            return {"subject": current_subject}

        with TestClient(
            app,
            base_url = "http://127.0.0.1:8000",
            client = ("127.0.0.1", 50000),
        ) as http_client:
            for token in dummy_bearers:
                headers = {} if token is None else {"Authorization": f"Bearer {token}"}
                response = http_client.get("/v1/models", headers = headers)
                if args.expected == "allowed":
                    assert response.status_code == 200, response.text
                    assert response.json() == {"subject": storage.DEFAULT_ADMIN_USERNAME}
                else:
                    assert response.status_code == 403, response.text
                    assert response.json() == {"detail": "Password change required"}
                counts["reported_positive_cases"] += 1

        # Invariants that must be identical before and after the PR.
        set_keyless_api_access("off")
        result = outcome(request_for())
        assert result[0] in (401, 403), result
        counts["common_invariants"] += 1

        web_jwt = create_access_token(storage.DEFAULT_ADMIN_USERNAME)
        for scope in ("off", "inference", "full"):
            set_keyless_api_access(scope)
            assert outcome(bearer_request(web_jwt)) == (403, "Password change required")
            counts["common_invariants"] += 1

        desktop_jwt = create_access_token(storage.DEFAULT_ADMIN_USERNAME, desktop = True)
        assert outcome(bearer_request(desktop_jwt)) == ("ok", storage.DEFAULT_ADMIN_USERNAME)
        counts["common_invariants"] += 1

        api_key, row = storage.create_api_key(
            storage.DEFAULT_ADMIN_USERNAME, "matrix", expires_at = None
        )
        assert outcome(bearer_request(api_key, path = "/v1/load")) == (
            "ok",
            storage.DEFAULT_ADMIN_USERNAME,
        )
        counts["common_invariants"] += 1

        expired_jwt = create_access_token(
            storage.DEFAULT_ADMIN_USERNAME,
            expires_delta = authentication.timedelta(seconds = -60),
        )
        assert outcome(bearer_request(expired_jwt))[0] == 401
        counts["common_invariants"] += 1

        storage.revoke_api_key(storage.DEFAULT_ADMIN_USERNAME, row["id"])
        assert outcome(bearer_request(api_key))[0] == 401
        counts["common_invariants"] += 1

        set_keyless_api_access("inference")
        for method, path in (
            ("POST", "/v1/load"),
            ("GET", "/v1/chat/completions"),
            ("POST", "/v1/models"),
            ("POST", "/v1/images/generations"),
            ("GET", "/v1x/models"),
        ):
            for token in (None, "not-needed"):
                request = (
                    request_for(method = method, path = path)
                    if token is None
                    else bearer_request(token, method = method, path = path)
                )
                assert outcome(request)[0] in (401, 403)
                counts["common_invariants"] += 1

        for header in (
            "Bearer arbitrary",
            "Keyless anything",
            "Basic abc",
            "Bearer",
            "",
        ):
            assert outcome(request_for(headers = {"Authorization": header}))[0] in (401, 403)
            counts["common_invariants"] += 1

        duplicate_scope = request_for().scope
        duplicate_scope["headers"] = [
            (b"authorization", b"Bearer not-needed"),
            (b"authorization", b"Bearer not-needed"),
        ]
        assert outcome(Request(duplicate_scope))[0] == 403
        counts["common_invariants"] += 1

        blocked_requests = [
            request_for(headers = {"Origin": "https://evil.example"}),
            request_for(headers = {"Sec-Fetch-Site": "cross-site"}),
            request_for(state = app_state(secure = True)),
            request_for(state = app_state(remote_access_is_colab = True)),
            request_for(state = app_state(cloudflare_url = "https://x.trycloudflare.com")),
            request_for(
                server = ("203.0.113.10", 8000),
                client = ("203.0.113.11", 50000),
                state = app_state(bind_host = "0.0.0.0"),
            ),
        ]
        for request in blocked_requests:
            assert outcome(request)[0] in (401, 403)
            counts["common_invariants"] += 1

        set_keyless_api_access("full")
        lan_request = request_for(
            server = ("192.168.1.24", 8888),
            client = ("192.168.1.90", 54321),
        )
        assert outcome(lan_request)[0] in (401, 403)
        counts["common_invariants"] += 1

        real_get_user = authentication.get_user_and_secret
        authentication.get_user_and_secret = lambda _username: None
        try:
            assert outcome(request_for())[0] == 401
        finally:
            authentication.get_user_and_secret = real_get_user
        counts["common_invariants"] += 1

        # Existing/older installs have no outstanding setup gate (migration default 0).
        connection = storage.get_connection()
        try:
            connection.execute(
                "UPDATE auth_user SET must_change_password = 0 WHERE username = ?",
                (storage.DEFAULT_ADMIN_USERNAME,),
            )
            connection.commit()
        finally:
            connection.close()
        set_keyless_api_access("inference")
        assert outcome(request_for()) == ("ok", storage.DEFAULT_ADMIN_USERNAME)
        assert outcome(bearer_request(web_jwt)) == ("ok", storage.DEFAULT_ADMIN_USERNAME)
        counts["common_invariants"] += 2

        print(
            json.dumps(
                {
                    "commit": args.commit
                    or subprocess.check_output(
                        ["git", "-C", str(root), "rev-parse", "HEAD"], text = True
                    ).strip(),
                    "expected_reported_behavior": args.expected,
                    **counts,
                    "total_cases": sum(counts.values()),
                    "status": "PASS",
                },
                sort_keys = True,
            )
        )


if __name__ == "__main__":
    main()
