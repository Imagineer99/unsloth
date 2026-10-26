"""Install / remove the leak_hunt accelerator for a Studio venv.

Two modes, same module:
  pythonpath (default): prepend leak_hunt/accel to PYTHONPATH; sitecustomize.py imports
      leak_hunt_accel. Touches nothing on disk, so it is safe on a shared venv.
  pth: copy leak_hunt_accel.py + a one-line .pth into the venv's site-packages (a private CI
      venv), removed by uninstall_pth. Survives code that scrubs PYTHONPATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ACCEL_DIR = Path(__file__).resolve().parent
PTH_NAME = "zz_leak_hunt_accel.pth"
MODULE = "leak_hunt_accel.py"


def site_packages(python: str) -> Path:
    out = subprocess.run(
        [python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output = True,
        text = True,
        check = True,
        timeout = 60,
    ).stdout.strip()
    return Path(out)


def install_pth(python: str) -> list[Path]:
    sp = site_packages(python)
    mod, pth = sp / MODULE, sp / PTH_NAME
    shutil.copy2(ACCEL_DIR / MODULE, mod)
    pth.write_text("import leak_hunt_accel\n", encoding = "utf-8")
    return [pth, mod]


def uninstall_pth(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def pythonpath_env(env: dict) -> dict:
    cur = env.get("PYTHONPATH", "")
    env = dict(env)
    env["PYTHONPATH"] = str(ACCEL_DIR) + (os.pathsep + cur if cur else "")
    return env
