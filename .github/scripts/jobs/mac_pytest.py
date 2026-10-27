"""Apple Silicon: run named pytest files strictly (a skip-only run fails), e.g. MLX tests a PR does not touch.

python jobs/mac_pytest.py --files tests/python/test_mlx_public_trainer_api.py tests/test_mlx_trl_lazy_all.py
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402

p = c.base_parser("mac_pytest", "n/a", "n/a", default_steps = 0)
p.add_argument("--files", nargs = "+", required = True)
a = c.resolve_args(p)
c.reject_cfg(a)

with c.JobRecorder(a) as rec:
    rec.set_backend("pytest")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *a.files],
        capture_output = True,
        text = True,
    )
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-8000:], flush = True)
    counts = {
        k: int(v)
        for v, k in re.findall(
            r"(\d+) (passed|failed|skipped|error|errors)",
            out.splitlines()[-1] if out.strip() else "",
        )
    }
    rec.summary(**counts)
    rec.check("pytest_exit_zero", r.returncode == 0, f"exit {r.returncode}")
    rec.check("something_passed", counts.get("passed", 0) > 0, counts)
