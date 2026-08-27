# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP-level proof that keyless-full still cannot mutate credentials."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import secrets
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required = True)
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    sys.path.insert(0, str(root / "studio" / "backend"))

    run_root = Path(__file__).resolve().parent / "runs"
    run_root.mkdir(exist_ok = True)
    with tempfile.TemporaryDirectory(prefix = "route-guards-", dir = run_root) as temp_name:
        home = Path(temp_name)
        os.environ["UNSLOTH_STUDIO_HOME"] = str(home)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from auth import storage
        from auth.authentication import create_access_token
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

        route_path = root / "studio" / "backend" / "routes" / "auth.py"
        spec = importlib.util.spec_from_file_location("_pr9855_auth_route", route_path)
        auth_route = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(auth_route)

        app = FastAPI()
        app.state.bind_host = "127.0.0.1"
        app.state.secure = False
        app.state.remote_access_is_colab = False
        app.state.lan_access_is_colab = False
        app.state.lan_access_secure_launch = False
        app.state.cloudflare_url = None
        app.include_router(auth_route.router, prefix = "/api/auth")

        set_keyless_api_access("full")
        guarded = (
            ("post", "/api/auth/logout", None, "Signing out"),
            (
                "post",
                "/api/auth/change-password",
                {
                    "current_password": "human-password-123",
                    "new_password": "new-password-123",
                },
                "Changing passwords",
            ),
            ("post", "/api/auth/api-keys", {"name": "blocked"}, "Managing API keys"),
            ("get", "/api/auth/api-keys", None, "Managing API keys"),
            ("delete", "/api/auth/api-keys/1", None, "Managing API keys"),
        )

        with TestClient(
            app,
            base_url = "http://127.0.0.1:8000",
            client = ("127.0.0.1", 50000),
        ) as client:
            for token in (None, "not-needed", "lm-studio", "ollama"):
                headers = {} if token is None else {"Authorization": f"Bearer {token}"}
                for method, path, body, label in guarded:
                    kwargs = {"headers": headers}
                    if body is not None:
                        kwargs["json"] = body
                    response = getattr(client, method)(path, **kwargs)
                    assert response.status_code == 403, (method, path, token, response.text)
                    assert response.json()["detail"].startswith(
                        f"{label} can only be done"
                    ), response.text

                response = client.post(
                    "/api/auth/desktop-initial-password",
                    headers = headers,
                    json = {"new_password": "new-password-123"},
                )
                assert response.status_code == 403, response.text
                assert response.json() == {
                    "detail": "This action requires the Unsloth desktop app."
                }

            # The browser setup gate remains; keyless-full cannot downgrade this JWT.
            browser_jwt = create_access_token(storage.DEFAULT_ADMIN_USERNAME)
            response = client.get(
                "/api/auth/api-keys",
                headers = {"Authorization": f"Bearer {browser_jwt}"},
            )
            assert response.status_code == 403, response.text
            assert response.json() == {"detail": "Password change required"}

            # Existing API-key and Desktop pathways retain their prior authority.
            api_key, _row = storage.create_api_key(storage.DEFAULT_ADMIN_USERNAME, "existing")
            response = client.get(
                "/api/auth/api-keys",
                headers = {"Authorization": f"Bearer {api_key}"},
            )
            assert response.status_code == 200, response.text

            desktop_jwt = create_access_token(storage.DEFAULT_ADMIN_USERNAME, desktop = True)
            response = client.post(
                "/api/auth/api-keys",
                headers = {"Authorization": f"Bearer {desktop_jwt}"},
                json = {"name": "desktop"},
            )
            assert response.status_code == 200, response.text

        assert storage.requires_password_change(storage.DEFAULT_ADMIN_USERNAME) is True
        assert sorted(
            row["name"] for row in storage.list_api_keys(storage.DEFAULT_ADMIN_USERNAME)
        ) == ["desktop", "existing"]
        print("PASS 24 keyless credential-mutation denials + browser/API-key/Desktop invariants")


if __name__ == "__main__":
    main()
