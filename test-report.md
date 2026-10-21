**Unsloth attachment validation — `1107b7ad447c0867d71da54a4fcc5b5442c47d19`**

The original project landing page rejected dropped files with “Dropped files need a single chat. Open one, then drop it there.” A fresh Chromium 156.0.8078.4 replay confirms the earlier audit: merge base has zero cards and no native registration/read; the final head has one card, one registration/read, and no rejection. Both scenes have no selected model or existing chat, and no page/native errors. [Original project-drop comparison](project-drop-before-after.png).

A separate regression found during development is also covered: hold a picker read, save the original chat through **Chat with files → Add files**, select **New chat**, release the read, then reopen the saved chat. Visible attachment counts were **merge base 1 → intermediate implementation 0 → final implementation 1**. The [saved-chat comparison](saved-chat-regression-before-after.png) shows the intermediate-to-final repair; it does **not** show a pre-existing merge-base bug. The unsaved-chat control still leaves zero attachments in the new chat.

| Final verification | Result |
| --- | --- |
| Full frontend suite, Node 24.14.0 in WSL2 | 12,953 passed, 0 failed, 1 Windows-only skip |
| Native Windows Node | 100 passed: 59 committed regression tests, 40 compatibility simulations, 1 Windows-only test |
| WSL2 browsers: Chromium, Firefox, WebKit | 60 scenarios passed |
| Native Windows Chrome and Edge | 22 scenarios passed |
| Typecheck, including tests | Passed |
| Production build | Passed; Vite chunk-size advisory only |

The Windows-only `bun.cmd` safety test skipped in WSL2 passed separately on Windows. The 59 committed regression tests include **19 adapter tests**, 32 native-consumer ownership tests, and 8 project-drop tests; these overlap the full-suite count. Browser scenarios cover delayed reads, project changes, Compare, saved-chat handoff/remount, and unsaved cancellation. Compatibility simulations cover drive/UNC/POSIX paths, Unicode/spaces, and unavailable older bridge capabilities.

All ten changed source/test file hashes from the verified run match the committed head; hashes and structured counts are in [validation.json](validation.json). The merge base is `37f53745d15141de71711772c8bb301b6467c968`. Upstream main was `664fe02d8c59509450a2ddecda9eb2f660aa06f4`, 115 commits beyond that base. A clean merge-tree was reviewed; **the merged tree was not tested**.

To reproduce the committed checks with dependencies installed, use Node 24.14.0 from `studio/frontend`:

```sh
node --experimental-strip-types --test "tests/**/*.test.ts"
node scripts/typecheck.mjs --tests
npm run build
node --experimental-strip-types --test tests/project-native-drop.test.ts tests/native-drop-consumer-ownership.test.ts tests/draft-attachment-adapter.test.ts
```

The 40 compatibility simulations and browser fixtures are supplementary audit harnesses, not committed test commands. Effective final results above exclude initial harness failures caused by an unsupported expectation that New chat clears typed text. Only that expectation was removed; attachment, navigation, and error assertions remained. Original failed attempts were retained. A Windows log-printing encoding error occurred after a passing test had already saved its result.

Browser runs used isolated profiles and deterministic HTTP/Tauri fixtures. They do not establish physical OS drag-and-drop, packaged-app, native macOS Safari, real inference/GPU, or old-binary compatibility. WebKit is not Safari; WSL2 is not an independent native Linux host. Windows browser versions were observed from installed executables at final verification, not recorded per run. Test-owned browsers and servers were stopped.

Adapter contract scope: resource cleanup is per operation, but cancellation notification is keyed by File. Overlapping adds that reuse the identical File cannot independently query completion status. Native readers create fresh File instances; no natural UI reproducer was found.
