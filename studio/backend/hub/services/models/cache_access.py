# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Per-request authorization for the installation-wide model cache."""

from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException


def caller_may_read_cached_model(repo_id: Any, hf_token: Any = None) -> bool:
    """Whether this caller may learn that a cached model repository exists."""
    if not isinstance(repo_id, str) or not repo_id.strip():
        return False
    from routes.inference import _reject_private_hub_repo_without_an_account_token

    try:
        _reject_private_hub_repo_without_an_account_token(
            repo_id.strip(),
            None,
            shared_cache_answers_offline = False,
        )
    except HTTPException:
        # A nonempty token is not proof by itself: otherwise any account with
        # any Hub credential could enumerate every private repo in the shared
        # cache. Confirm this token can actually see this repository.
        if not isinstance(hf_token, str) or not hf_token.strip():
            return False
        try:
            from huggingface_hub import HfApi

            HfApi(token = hf_token.strip()).model_info(
                repo_id.strip(),
                files_metadata = False,
            )
        except Exception:  # noqa: BLE001 - inaccessible and unavailable both withhold.
            return False
    return True


def visible_cached_model_rows(rows: Iterable[dict], hf_token: Any = None) -> list[dict]:
    """Filter a shared scan after it completes, never in its cross-request cache."""
    return [
        row
        for row in rows
        if caller_may_read_cached_model(row.get("repo_id"), hf_token)
    ]
