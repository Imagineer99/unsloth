"""Focused contract for restoring a prompt after a no-model send failure."""

import os
from pathlib import Path


REPO = Path(
    os.environ.get("PR7368_REPO_ROOT", Path(__file__).resolve().parents[2])
)
ADAPTER = REPO / "studio/frontend/src/features/chat/api/chat-adapter.ts"
THREAD = REPO / "studio/frontend/src/components/assistant-ui/thread.tsx"
STORE = REPO / "studio/frontend/src/features/chat/stores/chat-runtime-store.ts"


def test_no_model_failure_requests_restore_before_throwing():
    source = ADAPTER.read_text(encoding = "utf-8")

    helper = source.index("function latestUserMessageText")
    no_model_guard = source.index(
        "if (!useChatRuntimeStore.getState().params.checkpoint)"
    )
    failure = source.index("if (!loaded)", no_model_guard)
    rejected = source.index('throw new Error("Load a model first.")', failure)
    branch = source[failure:rejected]

    assert helper < no_model_guard
    assert "const promptText = latestUserMessageText(messages);" in branch
    assert "requestComposerRestore(promptText)" in branch


def test_runtime_store_exposes_one_shot_restore_state():
    source = STORE.read_text(encoding = "utf-8")

    assert "composerRestoreText: string | null;" in source
    assert "composerRestoreText: null," in source
    assert (
        "requestComposerRestore: (composerRestoreText) => set({ composerRestoreText })"
        in source
    )
    assert "clearComposerRestore: () => set({ composerRestoreText: null })" in source


def test_composer_consumes_restore_without_overwriting_new_input():
    source = THREAD.read_text(encoding = "utf-8")
    subscription = source.index(
        "const composerRestoreText = useChatRuntimeStore("
    )
    effect_end = source.index(
        "}, [composerRestoreText, aui]);",
        subscription,
    )
    effect = source[subscription:effect_end]

    clear = effect.index("clearComposerRestore();")
    empty_guard = effect.index(
        "if (composer.getState().text.trim().length === 0)"
    )
    restore = effect.index("composer.setText(composerRestoreText);")

    assert clear < empty_guard < restore
