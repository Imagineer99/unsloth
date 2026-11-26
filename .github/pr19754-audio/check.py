"""Run the production mixer against a private PulseAudio monitor. Log measurements only."""

import array
import contextlib
import functools
import http.server
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import struct
import subprocess
import threading
import time
import wave

from playwright.sync_api import sync_playwright


SITE = Path("audio-site")
FREQUENCIES = (220, 330, 440, 550)
WEIGHTS = [
    [complex(math.cos(2 * math.pi * f * i / 8000), math.sin(2 * math.pi * f * i / 8000))
     for i in range(800)]
    for f in FREQUENCIES
]


def prepare_site():
    shutil.copyfile(Path(__file__).with_name("observe.js"), SITE / "observe.js")
    for rev in ("base", "head"):
        (SITE / f"{rev}.html").write_text(
            '<!doctype html><meta charset="utf-8"><div id="root"></div>'
            f'<script src="observe.js"></script><script src="{rev}.js"></script>'
        )
    for index, frequency in enumerate(FREQUENCIES):
        second = b"".join(struct.pack("<h", round(1800 * math.sin(2 * math.pi * frequency * t / 16000)))
                          for t in range(16000))
        with wave.open(str(SITE / f"{index}.wav"), "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", ""))
            for _ in range(180):
                out.writeframesraw(second)


def measure_output():
    # Read three seconds from the isolated monitor, analyze two after the initial 250 ms.
    with subprocess.Popen(
        ["parec", "--device=pr19754.monitor", "--raw", "--format=s16le",
         "--rate=48000", "--channels=1", "--latency-msec=20"],
        stdout=subprocess.PIPE,
    ) as recorder:
        try:
            data = recorder.stdout.read(3 * 48000 * 2)
        finally:
            recorder.terminate()
    if len(data) != 288000:
        raise RuntimeError(f"Short PCM capture: {len(data)} bytes")
    samples = array.array("h", data[24000:216000])[::6]
    rms = [math.sqrt(sum(v*v for v in samples[i:i+160]) / 160) for i in range(0, len(samples), 160)]
    peaks = []
    for weight in WEIGHTS:
        blocks = (samples[i:i+800] for i in range(0, len(samples), 800))
        peaks.append(round(statistics.median(2 * abs(sum(v*w for v, w in zip(block, weight))) / 800 for block in blocks)))
    return {"near_silent_20ms": sum(x < 5 for x in rms), "total_windows": len(rms), "tone_amplitudes": peaks}


def exercise(browser, url, rev, mode, trial):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(f"{url}/{rev}.html?{mode}")
        page.wait_for_function("stems().length === 4 && stems().every(e => e.readyState >= 3)")
        page.get_by_role("button", name="Play stems", exact=True).click()
        if mode == "solo":
            page.wait_for_function("stems().every(e => !e.paused && e.currentTime > .3)")
            page.get_by_role("button", name="Solo bass", exact=True).click()
        time.sleep(1)
        page.evaluate("seeks.length = 0")
        output = measure_output()
        state = page.evaluate("""() => ({
            starts, resumes, contexts: contexts.length,
            advancing: stems().filter(e => !e.paused && e.currentTime > 0).length,
            playingUI: !!document.querySelector('[aria-label="Pause stems"]'),
            bass_seeks: seeks.filter(s => s.element === stems()[1]).length
        })""")
        result = dict(revision=rev, sha=os.environ[rev.upper()], mode=mode, trial=trial,
                      errors=errors, **state, **output)
        print(json.dumps(result), flush=True)
        return result
    finally:
        page.close()


def verdict(rows):
    failures = []
    for row in rows:
        tones = row["tone_amplitudes"]
        silent = row["near_silent_20ms"]
        if row["mode"] == "stalled" and row["revision"] == "base":
            ok = row["contexts"] == row["resumes"] == 1 and row["starts"] == row["advancing"] == 0 and silent == 100
        elif row["mode"] == "stalled":
            ok = row["contexts"] == row["resumes"] == 0 and row["starts"] == row["advancing"] == 4 and min(tones) > 100
        else:
            ok = (tones[1] > 100 and max(tones[0], *tones[2:]) < .2 * tones[1] and silent == 0
                  and row["advancing"] == 4)
        if not (ok and row["playingUI"] and not row["errors"]):
            failures.append(f"{row['revision']}/{row['mode']}/trial-{row['trial']}")
    if failures:
        raise SystemExit("Failed audio assertions: " + ", ".join(failures))
    print("Expected startup contrast observed; no solo dropout in the sampled trials.")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    prepare_site()
    handler = functools.partial(QuietHandler, directory=str(SITE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server, sync_playwright() as pw:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        with contextlib.closing(pw.firefox.launch(headless=True)) as browser:
            print("Firefox", browser.version, "Pulse latency (ms)", os.environ["PULSE_LATENCY_MSEC"])
            if browser.version != "142.0.1":
                raise RuntimeError("Unexpected Firefox build")
            rows = []
            for mode, trials in (("stalled", 1), ("solo", 3)):
                for trial in range(1, trials + 1):
                    for rev in ("base", "head"):
                        rows.append(exercise(browser, url, rev, mode, trial))
        server.shutdown()
    verdict(rows)


if __name__ == "__main__":
    main()
