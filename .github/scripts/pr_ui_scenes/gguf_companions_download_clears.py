"""Scene: download a GGUF with its required files from the Hub card, then read its tag in place.

For PR 12557's follow-up fix. The card plans companions once per mount; before the fix a
download of the GGUF plus its companions from the SAME card left the plan's companion bytes
cached, so the quant read "Partial" with everything on disk until the card remounted. The
fix re-plans on inventory changes.

Each side starts from an empty cache (both sides share one cache dir, so the scene purges
the two repos first), clicks Download with "Include required files", waits for every file
to land, then reads the selected quant's tag WITHOUT leaving the page.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(WORKSPACE))

from pr_ui_scenes._common import Session  # noqa: E402
from pr_ui_scenes.gguf_companions_pending import TAG_RE, _tag_text  # noqa: E402
from pr_ui_scenes.gguf_picker_rows import _select_repo  # noqa: E402
from studio_test_kit.auth import seed_init_script  # noqa: E402
from studio_test_kit.ui import open_chat  # noqa: E402

DEFAULT_REPO = "unsloth/FLUX.2-klein-4B-GGUF"
DEFAULT_BASE = "unsloth/FLUX.2-klein-4B"  # what the download plan names, not the upstream repo
DEFAULT_FILE = "flux-2-klein-4b-Q2_K.gguf"
DEFAULT_QUANT = "Q2_K"


def _repo_dir(hub: Path, repo: str) -> Path:
    return hub / ("models--" + repo.replace("/", "--"))


def _snapshot_files(hub: Path, repo: str) -> int:
    snaps = _repo_dir(hub, repo) / "snapshots"
    return (
        sum(1 for f in snaps.rglob("*") if f.is_file() or f.is_symlink()) if snaps.exists() else 0
    )


def _hub_bytes(hub: Path) -> int:
    return sum(f.stat().st_size for f in hub.rglob("*") if f.is_file())


def _settled(hub: Path, repos: tuple[str, ...]) -> bool:
    return all(_snapshot_files(hub, r) for r in repos) and not any(hub.rglob("*.incomplete"))


async def drive(
    session: Session,
    out_dir: Path,
    label: str,
    repo: str = DEFAULT_REPO,
    base_repo: str = DEFAULT_BASE,
    filename: str = DEFAULT_FILE,
    quant: str = DEFAULT_QUANT,
    cache_hub: str = "",
    timeout_s: int = 1800,
    **_: object,
) -> tuple[list[Path], dict]:
    if not cache_hub:
        raise RuntimeError("cache_hub is required: the scene purges and watches it")
    hub = Path(cache_hub)
    for r in (repo, base_repo):
        d = _repo_dir(hub, r)
        if d.exists():
            shutil.rmtree(d)
    if (hub / "blobs").exists():
        shutil.rmtree(hub / "blobs")
    facts: dict = {"purged": True}
    shots: list[Path] = []
    out_dir.mkdir(parents = True, exist_ok = True)
    init = seed_init_script(
        type(
            "A", (), {"access_token": session.access_token, "refresh_token": session.refresh_token}
        )(),
        [],
    )
    async with open_chat(
        session.base_url, init_scripts = [init], viewport = (1500, 1000), headless = True
    ) as sp:
        page = sp.page
        await page.goto(f"{session.base_url}/hub", wait_until = "domcontentloaded")
        await _select_repo(page, repo)
        trigger = (
            page.locator("button.hub-menu-trigger")
            .filter(has_text = re.compile(r"Q\d|BF16|F16|Select quantization"))
            .first
        )
        await trigger.wait_for(state = "visible", timeout = 60_000)
        await trigger.click()
        rows = page.locator("[role='button'][aria-pressed]")
        await rows.first.wait_for(state = "visible", timeout = 30_000)
        await rows.filter(has = page.get_by_text(quant, exact = True)).first.click()
        await page.wait_for_timeout(1_000)
        if await rows.first.is_visible():
            await page.keyboard.press("Escape")
        facts["tags_before_download"] = await _tag_text(trigger)

        await page.get_by_role("button", name = re.compile(r"^Download$")).first.click()
        dialog = page.get_by_role("alertdialog").filter(has_text = "Download model").first
        await dialog.wait_for(state = "visible", timeout = 60_000)
        box = dialog.get_by_role("checkbox").first
        await page.wait_for_function(
            "d => !d.textContent.includes('Checking required files')",
            arg = await dialog.element_handle(),
            timeout = 120_000,
        )
        if (await box.get_attribute("data-state")) != "checked":
            await box.click()
        facts["dialog_text"] = (await dialog.inner_text()).replace("\n", " ")[:400]
        await dialog.get_by_role("button", name = "Download", exact = True).click()

        t0 = time.monotonic()
        last, stable = -1, 0
        while time.monotonic() - t0 < timeout_s:
            size = _hub_bytes(hub)
            stable = stable + 1 if size == last else 0
            last = size
            if stable >= 4 and _settled(hub, (repo, base_repo)):
                break
            await page.wait_for_timeout(5_000)
        else:
            raise RuntimeError(f"[{label}] downloads did not finish in {timeout_s}s")
        facts["download_s"] = round(time.monotonic() - t0)
        facts["snapshot_files"] = {r: _snapshot_files(hub, r) for r in (repo, base_repo)}
        facts["hub_gb"] = round(last / 1e9, 2)
        # Poll-loop completion, debounced inventory bump, variant refetch and re-plan.
        tags: list[str] = []
        for _ in range(24):
            await page.wait_for_timeout(5_000)
            tags = await _tag_text(trigger)
            if "On device" in tags:
                break
        facts["tags_after_download"] = tags
        facts["trigger_text_after"] = (await trigger.inner_text()).replace("\n", " ")
        facts["same_page"] = "/hub" in page.url
        tbox = await trigger.bounding_box()
        shot = out_dir / f"{label.lower()}_after_download.png"
        if tbox:
            await page.screenshot(
                path = str(shot),
                clip = {
                    "x": max(0, tbox["x"] - 200),
                    "y": max(0, tbox["y"] - 110),
                    "width": tbox["width"] + 400,
                    "height": tbox["height"] + 140,
                },
            )
        else:
            await sp.screenshot(shot, full_page = False)
        shots.append(shot)
        _ = TAG_RE
    return shots, facts
