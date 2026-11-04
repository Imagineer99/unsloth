// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
const css = read("../src/index.css").replace(/\/\*[\s\S]*?\*\//g, "");
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ selectors: m[1].split(",").map((s) => s.trim()), body: m[2] }));
const FRAME = '[data-slot="sidebar-container"][data-windows-glass-frame]::before';

test("only a pinned sidebar in the Windows desktop app carries the glass frame", () => {
	const sidebar = read("../src/components/app-sidebar.tsx");
	assert.match(sidebar, /data-windows-glass-frame=\{\(pinned && usesWindowsTitlebar\(\)\) \|\| undefined\}/);
	const titlebar = read("../src/components/tauri/window-titlebar.tsx");
	const [, body] = /export function usesWindowsTitlebar\(\): boolean \{\s*return ([^;]+);/.exec(titlebar) ?? [];
	assert.match(body ?? "", /^isTauri && /);
	const re = new RegExp(/\/(.+)\/\.test\(getClientPlatform\(\)\)/.exec(body ?? "")?.[1] ?? "^$");
	for (const p of ["windows", "win32", "mozilla/5.0 (windows nt 10.0; win64; x64)"]) assert.ok(re.test(p), p);
	for (const p of ["macos", "macintel", "darwin", "linux x86_64", "x11", ""]) assert.ok(!re.test(p), p);
});

test("the frame paints one glass surface over the sidebar and caption", () => {
	const frame = rules.find((r) => r.selectors.some((s) => s.endsWith(FRAME) && s.includes(":not([data-glass-plain])")));
	assert.ok(frame);
	assert.match(frame.body, /pointer-events:\s*none/);
	assert.match(frame.body, /var\(--sidebar-width\)\s+100%/);
	assert.match(frame.body, /100%\s+var\(--studio-custom-titlebar-height\)/);
	const tinted = rules.find((r) => r.selectors.includes('html[data-glass][data-chat-wallpaper-workspace-ready] [data-windows-glass-frame]::before'));
	assert.ok(tinted?.selectors.includes('html[data-glass][data-chat-wallpaper-workspace-ready] [data-slot="sidebar-inner"]'));
	const inner = rules.find((r) => r.selectors.some((s) => s.endsWith('[data-windows-glass-frame] > [data-slot="sidebar-inner"]')));
	assert.ok(inner && /background:\s*transparent/.test(inner.body) && /backdrop-filter:\s*none/.test(inner.body));
});
