"""Staging-only: run the setup.sh GPU-summary shell suite under this runner's own bash."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="studio/setup.sh is not run on Windows")
def test_setup_gpu_summary_suite_passes_including_the_sigpipe_case():
    env = dict(os.environ)
    if not shutil.which("timeout") and shutil.which("gtimeout"):
        # macOS: expose only GNU timeout (brew coreutils), keep the BSD head/sort/wc/awk.
        shim = ROOT / ".timeout_shim"
        shim.mkdir(exist_ok=True)
        link = shim / "timeout"
        if not link.exists():
            link.symlink_to(shutil.which("gtimeout"))
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    r = subprocess.run(["bash", str(ROOT / "tests/sh/test_setup_gpu_summary_probe_sources.sh")],
                       capture_output=True, text=True, timeout=900, cwd=str(ROOT), env=env)
    out = r.stdout + r.stderr
    print(out[-4000:])
    assert r.returncode == 0, out[-4000:]
    assert "FAIL:" not in out and "Results:" in out, out[-4000:]
