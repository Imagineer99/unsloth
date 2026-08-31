// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { chromium, firefox, webkit } from "playwright";

const ROOT = path.resolve(import.meta.dirname, "../..");
const ADAPTER_PATH = path.join(
  ROOT,
  "studio/frontend/src/features/chat/api/chat-adapter.ts",
);
const HELPER_PATH = path.join(
  ROOT,
  "studio/frontend/src/features/chat/tool-call-arguments.ts",
);
const WIRE =
  '{"arguments":{"id":9007199254740993},"arguments_text":"{\\"id\\":9007199254740993}"}';
const EXACT = '{"id":9007199254740993}';
const ROUNDED = '{"id":9007199254740992}';

const adapter = readFileSync(ADAPTER_PATH, "utf8");
const exactTextWired =
  /toolCallArgumentsText\(\s*toolEvent\.arguments_text/.test(adapter) &&
  existsSync(HELPER_PATH);

const parsed = JSON.parse(WIRE);
let nodeCardText;
if (exactTextWired) {
  const helper = await import(pathToFileURL(HELPER_PATH).href);
  nodeCardText = helper.toolCallArgumentsText(
    parsed.arguments_text,
    parsed.arguments,
  );
} else {
  nodeCardText = JSON.stringify(parsed.arguments ?? {});
}

console.log(
  `ADAPTER mode=${exactTextWired ? "exact-text" : "legacy-stringify"} node_card=${nodeCardText}`,
);

function launcher(name) {
  if (name === "chromium") return [chromium, {}];
  if (name === "firefox") return [firefox, {}];
  if (name === "webkit") return [webkit, {}];
  if (name === "chrome") return [chromium, { channel: "chrome" }];
  if (name === "msedge") return [chromium, { channel: "msedge" }];
  throw new Error(`unsupported browser: ${name}`);
}

let failed = false;
for (const name of process.argv.slice(2)) {
  let browser;
  try {
    const [engine, options] = launcher(name);
    browser = await engine.launch(options);
    const page = await browser.newPage();
    const cardText = await page.evaluate(
      ({ wire, useExactText }) => {
        const event = JSON.parse(wire);
        return useExactText &&
          typeof event.arguments_text === "string" &&
          event.arguments_text.length > 0
          ? event.arguments_text
          : JSON.stringify(event.arguments ?? {});
      },
      { wire: WIRE, useExactText: exactTextWired },
    );
    assert.equal(JSON.stringify(parsed.arguments), ROUNDED);
    assert.equal(cardText, nodeCardText);
    assert.equal(cardText, EXACT);
    console.log(`BROWSER ${name} PASS card=${cardText}`);
  } catch (error) {
    failed = true;
    console.error(`BROWSER ${name} FAIL`, error);
  } finally {
    await browser?.close();
  }
}

process.exitCode = failed ? 1 : 0;
