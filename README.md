# PR #12450 UI evidence

Real Colab A100 GPU generations captured through browser Playwright. Matching crops and labels were added outside the screenshot content; screenshot pixels were not edited or resized.

Both captures use Qwen-Image 2.1 (Fast FP8), prompt , seed 43, 1024 x 1024, 40 steps, guidance 1, batch 1, and runs 1. Both show a completed 256 x 256 thumbnail while the full-resolution original is loading. No network mocks or simulated delays.

BEFORE: original unmodified Studio at 5c239a233d000ccb6a0213ecefa92f9cd78bbf58, with spinner over the preview.
AFTER: same Colab Studio plus the locally tested image-loading fix, with Loading image… in the compact toolbar. Gallery counts differ because the captures were taken during separate generations.

The original subsequently loaded at 1024 x 1024, loading feedback cleared, and Download became enabled.

- before-after-detail.png: matched crop around the preview and loading feedback.
- before-after-context.png: wider matched crop showing model and generation settings.

These captures show the real Colab application in the browser, not native Windows window frames.
