# Sidebar fade · glass only

The footer fade is hidden only for glass themes. Standard themes retain the fade and switching back restores it.

Base: `24d65c19ddaf0da82c4cc48b9bb67ec69f48b97a`

Head: `967716923bbfa0e7b523e2f1eb0c27938d5a1655`

![Before and after](comparison.png)

Validation: `{"states": 40, "standardThemeMaxPixelDifference": 0}`

Component harness only: native calls are stubbed, chat rows are synthetic, the footer JSX and scroll-fade function are extracted from the pinned source. The modal backdrop uses the real app's viewport attributes and opacity. No native WebView content or backend behavior is exercised. Images are fixed crops with labels; no scaling or pixel retouching. Full screenshots, measured facts and environment details are in this folder and `meta.json`.
