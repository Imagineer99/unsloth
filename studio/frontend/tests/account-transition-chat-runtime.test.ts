// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import test from "node:test";

import {
  installLocalStorageFake,
  registerStoreStubResolver,
} from "./helpers/kit.ts";

registerStoreStubResolver();
const { fireWindowEvent } = installLocalStorageFake();

const { ACCOUNT_CHANGED_EVENT } = await import(
  "../src/lib/account-transition.ts"
);
const { useChatRuntimeStore } = await import(
  "../src/features/chat/stores/chat-runtime-store.ts"
);

test("an account change clears pending chat media from the hydrated runtime", () => {
  const runtime = useChatRuntimeStore.getState();
  runtime.setPendingAudio("YWxpY2VzIHJlY29yZGluZw==", "alice.wav");
  runtime.setPendingImageEditReference({
    threadId: "alice-private-thread",
    openaiImageGenerationCallId: "alice-private-image",
  });

  fireWindowEvent(ACCOUNT_CHANGED_EVENT, new Event(ACCOUNT_CHANGED_EVENT));

  assert.equal(useChatRuntimeStore.getState().pendingAudioBase64, null);
  assert.equal(useChatRuntimeStore.getState().pendingAudioName, null);
  assert.equal(useChatRuntimeStore.getState().pendingImageEditReference, null);
});
