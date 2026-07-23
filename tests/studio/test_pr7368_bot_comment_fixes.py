"""Contracts for the three PR 7368 composer-restore review fixes."""

import os
from pathlib import Path


REPO = Path(
    os.environ.get("PR7368_REPO_ROOT", Path(__file__).resolve().parents[2])
)
ADAPTER = REPO / "studio/frontend/src/features/chat/api/chat-adapter.ts"
THREAD = REPO / "studio/frontend/src/components/assistant-ui/thread.tsx"
STORE = REPO / "studio/frontend/src/features/chat/stores/chat-runtime-store.ts"


def test_restore_is_scoped_to_the_source_draft():
    store = STORE.read_text(encoding = "utf-8")
    thread = THREAD.read_text(encoding = "utf-8")

    assert (
        "pendingComposerRestore: { draftKey: string | null; text: string } | null;"
        in store
    )
    assert (
        "composerRestore: { draftKey: string | null; text: string } | null;"
        in store
    )

    effect_start = thread.index(
        "const composerRestore = useChatRuntimeStore("
    )
    effect_end = thread.index(
        "}, [composerRestore, draftKey, aui]);",
        effect_start,
    )
    effect = thread[effect_start:effect_end]

    mismatch = effect.index("composerRestore.draftKey !== draftKey")
    persist = effect.index(
        "writeComposerDraft(composerRestore.draftKey, composerRestore.text)"
    )
    restore = effect.index("composer.setText(composerRestore.text)")
    assert mismatch < persist < restore


def test_only_a_direct_composer_send_can_arm_a_restore():
    adapter = ADAPTER.read_text(encoding = "utf-8")
    thread = THREAD.read_text(encoding = "utf-8")

    submit_start = thread.index("const handleSubmit = useCallback(")
    submit_end = thread.index("const stopQueue = useCallback(", submit_start)
    submit = thread[submit_start:submit_end]

    assert "setPendingComposerRestore(" in submit
    assert "pendingText ? { draftKey, text: pendingText } : null" in submit

    no_model = adapter.index("if (!loaded)")
    rejected = adapter.index('throw new Error("Load a model first.")', no_model)
    failure_branch = adapter[no_model:rejected]
    assert "promoteComposerRestore()" in failure_branch
    assert "latestUserMessageText" not in adapter
    assert "requestComposerRestore" not in adapter


def test_image_edit_restore_keeps_the_original_user_text():
    thread = THREAD.read_text(encoding = "utf-8")
    adapter = ADAPTER.read_text(encoding = "utf-8")

    overlay_start = thread.index("if (overlay) {")
    pending_end = thread.index("const stopQueue = useCallback(", overlay_start)
    submit_tail = thread[overlay_start:pending_end]

    rewrite = submit_tail.index(
        "Use the selected generated image as the reference and apply this edit:"
    )
    capture = submit_tail.index("const pendingText = composerText.trim();")
    arm = submit_tail.index(
        "pendingText ? { draftKey, text: pendingText } : null"
    )

    assert rewrite < capture < arm
    assert "latestUserMessageText" not in adapter
