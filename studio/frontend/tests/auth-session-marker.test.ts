// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import test from "node:test";

import {
  installLocalStorageFake,
  registerBundlerResolver,
} from "./helpers/kit.ts";

registerBundlerResolver();
const { store, storage } = installLocalStorageFake();
Object.assign(globalThis.window as object, {
  dispatchEvent: () => true,
});

const {
  AUTH_REFRESH_TOKEN_KEY,
  AUTH_SESSION_MARK_KEY,
  AUTH_TOKEN_KEY,
  storeAuthTokens,
} = await import("../src/features/auth/session.ts");

test("session-marker quota failure does not reject otherwise valid credentials", () => {
  store.clear();
  const setItem = storage.setItem;
  storage.setItem = (key: string, value: string) => {
    if (key === AUTH_SESSION_MARK_KEY) {
      throw new DOMException("quota full", "QuotaExceededError");
    }
    setItem(key, value);
  };

  assert.doesNotThrow(
    () => storeAuthTokens("access-token", "refresh-token"),
    "cache metadata must not turn a successful login into an error",
  );
  assert.equal(store.get(AUTH_TOKEN_KEY), "access-token");
  assert.equal(store.get(AUTH_REFRESH_TOKEN_KEY), "refresh-token");
  assert.equal(store.has(AUTH_SESSION_MARK_KEY), false);
});
