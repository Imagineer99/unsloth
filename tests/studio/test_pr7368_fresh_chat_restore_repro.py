"""Reproduce the fresh-chat null-to-saved draft-key restore loss."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
THREAD = REPO / "studio/frontend/src/components/assistant-ui/thread.tsx"
RUNTIME_PROVIDER = (
    REPO / "studio/frontend/src/features/chat/runtime-provider.tsx"
)


def test_fresh_chat_restore_is_dropped_after_first_thread_save():
    thread = THREAD.read_text(encoding = "utf-8")
    runtime_provider = RUNTIME_PROVIDER.read_text(encoding = "utf-8")

    assert (
        "const referenceThreadId = threadId ?? activeThreadId ?? null;"
        in thread
    )
    assert (
        "const draftKey = draftThreadId ? composerDraftKey(draftThreadId) : null;"
        in thread
    )

    submit_start = thread.index("const handleSubmit = useCallback(")
    submit_end = thread.index("const stopQueue = useCallback(", submit_start)
    submit = thread[submit_start:submit_end]
    assert "pendingText ? { draftKey, text: pendingText } : null" in submit

    append_start = runtime_provider.index(
        "append({ parentId, message }: ExportedMessageRepositoryItem)"
    )
    append_end = runtime_provider.index(
        "const thread = await getStoredChatThread(remoteId)",
        append_start,
    )
    first_save = runtime_provider[append_start:append_end]
    assert "const { remoteId } = await initializeThread;" in first_save
    assert "store.setActiveThreadId(remoteId);" in first_save

    effect_start = thread.index(
        "const composerRestore = useChatRuntimeStore("
    )
    effect_end = thread.index(
        "}, [composerRestore, draftKey, aui]);",
        effect_start,
    )
    effect = thread[effect_start:effect_end]

    mismatch = effect.index("composerRestore.draftKey !== draftKey")
    nullable_save = effect.index("if (composerRestore.draftKey)")
    early_return = effect.index("return;", nullable_save)
    restore = effect.index("composer.setText(composerRestore.text)")
    assert mismatch < nullable_save < early_return < restore

    def consume_restore(
        restore_key: str | None,
        mounted_key: str | None,
    ) -> str:
        if restore_key != mounted_key:
            return "saved" if restore_key else "dropped"
        return "restored"

    assert consume_restore(None, "chat:first-save") == "dropped"
