"""Verify the fresh-chat draft-key transition fix in PR 7368."""

import os
from pathlib import Path


REPO = Path(
    os.environ.get("PR7368_REPO_ROOT", Path(__file__).resolve().parents[2])
)
THREAD = REPO / "studio/frontend/src/components/assistant-ui/thread.tsx"
STORE = REPO / "studio/frontend/src/features/chat/stores/chat-runtime-store.ts"
DRAFTS = REPO / "studio/frontend/src/features/chat/utils/composer-draft.ts"


def test_fresh_chat_restore_survives_first_thread_id_assignment():
    thread = THREAD.read_text(encoding = "utf-8")
    store = STORE.read_text(encoding = "utf-8")
    drafts = DRAFTS.read_text(encoding = "utf-8")

    assert 'const NEW_CHAT_DRAFT_ID = "__new__";' in drafts
    assert "threadId ?? NEW_CHAT_DRAFT_ID" in drafts
    assert (
        "pendingComposerRestore: { draftKey: string; text: string } | null;"
        in store
    )

    submit_start = thread.index("const handleSubmit = useCallback(")
    submit_end = thread.index("const stopQueue = useCallback(", submit_start)
    submit = thread[submit_start:submit_end]
    assert "const pendingDraftKey = draftKey ?? composerDraftKey(null);" in submit
    assert (
        "lastSubmitDraftKeyRef.current = pendingText ? pendingDraftKey : null;"
        in submit
    )
    assert (
        "pendingText ? { draftKey: pendingDraftKey, text: pendingText } : null"
        in submit
    )

    effect_start = thread.index(
        "const composerRestore = useChatRuntimeStore("
    )
    effect_end = thread.index(
        "}, [composerRestore, draftKey, aui]);",
        effect_start,
    )
    effect = thread[effect_start:effect_end]
    assert "composerRestore.draftKey !== draftKey" in effect
    assert (
        "composerRestore.draftKey !== lastSubmitDraftKeyRef.current"
        in effect
    )
    assert "lastSubmitDraftKeyRef.current = null;" in effect
    assert "composer.setText(composerRestore.text);" in effect

    new_chat_key = "chat-draft:__new__"
    saved_thread_key = "chat-draft:first-save"
    last_submit_key = new_chat_key
    should_persist_elsewhere = (
        new_chat_key != saved_thread_key
        and new_chat_key != last_submit_key
    )
    assert not should_persist_elsewhere
