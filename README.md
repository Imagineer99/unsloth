# Unsloth glass UI comparisons

Three independent PR comparisons against the same main commit. Each folder contains a labelled comparison, all full screenshots, facts, patch, and validation logs. `manifest.json` pins the exact base and heads; `scene-plan.json` records the expected effect before capture.

The comparisons use production Vite builds of real Sidebar, WindowTitlebar and BrowserPanel components. The sidebar footer overlay JSX and its syncFade function are extracted from each commit's AppSidebar. Chat rows and profile information are synthetic. Native calls are stubbed and the Settings viewport backdrop is reproduced using the app's attributes and opacity. This checks rendering and layering; it does not exercise actual native WebView content or backend behavior. No real conversation data is included.

## Reproduce

Use Node 24, the repository frontend dependencies, Playwright and Chromium. For each of `base`, `browser`, `titlebar`, `sidebar`, check out its manifest SHA in a clean Unsloth checkout. Place `scene-<name>.tsx`, renamed to `scene.tsx`, alongside this folder's `index.html` and `vite.config.ts` under `studio/frontend/work/glass-pr-evidence/`. From `studio/frontend`, run:

```
node node_modules/vite/bin/vite.js build --config work/glass-pr-evidence/vite.config.ts --outDir /absolute/path/to/evidence/build-<name>
```

Add `provenance.json` to that build directory with `sha` matching the manifest and `build` matching its name. Keep each build separate. Install Playwright next to `capture.cjs`, then run `node capture.cjs`. Optionally set `CHROME_EXECUTABLE` to a local Chrome binary. The driver uses fresh isolated browser contexts and separate ephemeral local servers, asserts commit stamps, checks state, and writes screenshots plus facts. Windows Chrome was used for the published captures; font rendering can vary on other hosts.

The labelled images are fixed crops pasted without scaling or retouching. Measurements are calculated from the same full screenshots. Standard-theme browser and sidebar captures are pixel-identical across sides. The titlebar sample compares blank regions above/below the native strip, averaging the sidebar samples to avoid treating intentional grain as a seam.
