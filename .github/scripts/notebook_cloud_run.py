#!/usr/bin/env python3
"""
notebook_cloud_run.py -- run Unsloth notebooks on a rented Colab or Kaggle GPU.

One command, no prior setup. Pick a backend, pick a GPU, name one or more
notebooks, get a real pass/fail for each:

    python notebook_cloud_run.py --backend kaggle --check-auth
    python notebook_cloud_run.py --backend colab  --gpu T4   "Llama3.1_(8B)-Alpaca.ipynb"
    python notebook_cloud_run.py --backend kaggle --gpu T4x2 ./my_notebook.ipynb

A notebook argument is a local path, a bare name from the unslothai/notebooks
repo, or any URL (Colab and GitHub blob links are rewritten to raw links).

Five things this script refuses to get wrong, each of them learned from a
regression sweep that reported the wrong answer or burned real money:

  1. The exit code is a lie. `colab exec` exits 0 even when cells raise -- its
     loop records the error output and moves on. Kaggle marks a kernel
     "complete" the moment the notebook stops, error cells and all. So the
     verdict always comes from PARSING the executed notebook, never from a
     return code. See `parse_executed_notebook`.
  2. `colab exec --timeout` is PER CELL, not per notebook. Twenty cells at
     900s each is a five-hour VM. Every remote call here carries an
     independent wall-clock deadline on top of the per-cell one.
  3. A leaked session bills until the 24h cap. Teardown runs from a `finally`,
     from an `atexit` hook, and from SIGINT/SIGTERM handlers, so neither a
     crash nor Ctrl-C can strand a billable VM.
  4. Never pass `colab --env`: the CLI prepends its environment prelude to
     every cell, which corrupts any cell whose first line has to stay
     `%%capture` or `%%bash`. Environment setup goes into an injected first
     cell instead (see `build_bootstrap_cell`).
  5. Shortening a run can fake its own success. Capping `max_steps` at 5 while
     the notebook logs every 50 steps produces a run with no loss at all,
     which reads as "never trained". The smoke patch lowers `logging_steps`
     to match, and strips `%%capture` so a failed install is visible.

Exit codes:
    0   every notebook passed
    1   at least one notebook failed (a cell raised, or it never finished)
    2   credential / authentication problem
    3   infrastructure problem (CLI missing, GPU tier unavailable, no result)
    130 interrupted

Python 3.9+, standard library only. Runs on Linux, macOS, Windows and WSL
(the Colab CLI itself is Linux/macOS only; the Kaggle backend works
everywhere).
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import getpass
import gzip
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote as url_quote
from urllib.parse import unquote as url_unquote
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

NOTEBOOKS_REPO = "unslothai/notebooks"
NOTEBOOKS_RAW_BASE = "https://raw.githubusercontent.com/unslothai/notebooks/refs/heads/main"
# A bare name is looked up in these repo directories, in order.
NOTEBOOKS_DIRS = ("nb", "kaggle", "original_template")

# `colab new --gpu` values, per the CLI's own help. An UNRECOGNISED value is
# not rejected by the CLI -- it silently falls back to A100 and then fails at
# allocation, which reads like a capacity problem instead of a typo. So the
# tier is validated here, locally, before a round trip is spent on it.
COLAB_GPUS = ("T4", "L4", "G4", "A100", "H100", "CPU")

# `--gpu A100` is the 40GB part. Colab's 80GB A100 is not a separate --gpu
# value at all: it is the SAME accelerator on a high-RAM VM, selected by
# `shape=hm` on the assign request (`colab new --gpu A100 --high-mem`).
#
# Measured on live sessions, which is the only reason this is stated as fact:
#
#   colab new --gpu A100              NVIDIA A100-SXM4-40GB   40960 MiB   83.5GB RAM
#   colab new --gpu A100 --high-mem   NVIDIA A100-SXM4-80GB   81920 MiB  167.1GB RAM
#
# So high-RAM is not merely a RAM knob, and a caller who asks for 80GB and
# silently receives the 40GB card gets an out-of-memory failure that reads
# exactly like a broken notebook. `--high-mem` first shipped on PyPI in
# google-colab-cli 0.7.2 (0.6.0 has no way to send the shape), so the flag is
# probed on the installed binary and a CLI without it is REFUSED with an
# upgrade hint rather than quietly handed the standard VM.
COLAB_GPU_ALIASES = {
    "A100-80GB": "A100",
    "A10080GB": "A100",
    "A100HM": "A100",
    "A100-HM": "A100",
}
# High-RAM is not an A100 story: every tier has one, and a caller who wants the
# 25GB T4 VM rather than the 12.7GB one reaches for the same spelling. Without
# these the request falls through to "unknown Colab GPU tier", which reads like
# a typo when it is really a capability limit.
for _tier in ("T4", "L4", "G4", "A100", "H100"):
    for _suffix in ("HIGHRAM", "HIGH-RAM", "HM", "-HM", "HIGHMEM"):
        COLAB_GPU_ALIASES.setdefault(f"{_tier}-{_suffix.lstrip('-')}", _tier)
del _tier, _suffix
COLAB_HIGHMEM_ONLY = frozenset(COLAB_GPU_ALIASES)


def wants_high_mem(gpu: str) -> bool:
    """True when the tier spelling asks for the high-RAM shape."""
    key = re.sub(r"[-_ ]", "", (gpu or "")).upper()
    return key in {re.sub(r"[-_ ]", "", a).upper() for a in COLAB_HIGHMEM_ONLY}


# L4 and the TPUs have a single shape, so --high-mem is accepted and ignored
# there. Saying so up front beats a caller concluding the flag silently failed.
COLAB_SINGLE_SHAPE = frozenset({"L4"})

# Setting machine_shape in the uploaded notebook's own metadata does NOT work,
# which is worth stating because it is the obvious thing to try and it fails
# silently. Measured on a live session, metadata.colab = {"machine_shape": "hm",
# "gpuType": "T4"}:
#
#   requested hm via notebook metadata   Tesla T4   14.56 GiB VRAM   12.67 GB RAM
#   a plain --gpu T4                     Tesla T4   14.56 GiB VRAM   12.67 GB RAM
#
# Identical, because `colab new` allocates the VM before any notebook metadata
# is read. The shape has to be on the assign request itself.

# Kaggle `machine_shape` values. The allowed names live in a server-side enum
# that kagglesdk does not ship, so they are spelled out here. Kaggle hands out
# two T4s per GPU kernel, hence the T4x2 spelling.
KAGGLE_GPUS = {
    "T4X2": ("T4x2", "NvidiaTeslaT4"),
    "T4": ("T4x2", "NvidiaTeslaT4"),
    "P100": ("P100", "NvidiaTeslaP100"),
    "TPU": ("TPU", "Tpu1VmV38"),
    "CPU": ("CPU", ""),
}

# Substrings that mean "the platform had no room right now", as opposed to
# "your credentials are wrong" or "the notebook is broken". Kept separate so
# those three cases can never print the same message.
CAPACITY_MARKERS = (
    "toomanyassignments",
    "precondition failed",
    "no quota for",
    "no accelerator quota",
    "resource exhausted",
    "maximum batch gpu session count",
    "session count of 2 reached",
    "quota exceeded",
    # Two runs started together on one account: Colab refuses the second as "too many sessions".
    # That frees as soon as the other finishes, so --alloc-wait retries it like capacity (it failed
    # straight to NO_RESULT; cloud_pool already treats it as busy).
    "too many active sessions",
    "too many sessions",
)

AUTH_MARKERS = (
    "401",
    "403",
    "unauthenticated",
    "unauthorized",
    "permission denied",
    "invalid credentials",
    "could not automatically determine credentials",
    "reauthentication",
)

GCLOUD_LOGIN_COMMAND = (
    "gcloud auth application-default login \\\n"
    "  --scopes=openid,\\\n"
    "https://www.googleapis.com/auth/cloud-platform,\\\n"
    "https://www.googleapis.com/auth/userinfo.email,\\\n"
    "https://www.googleapis.com/auth/colaboratory"
)

# The CLI's own provider, and the only one that works with no gcloud on the box:
# it prints a URL, you approve in any browser, you paste the code back. Same
# remote flow gcloud itself uses, so it suits headless hosts and containers.
# Token lands in ~/.config/colab-cli/token.json, NOT in the gcloud ADC file.
OAUTH2_LOGIN_COMMAND = "colab --auth oauth2 sessions"
OAUTH2_TOKEN_RELPATH = ("colab-cli", "token.json")

DEFAULT_MAX_STEPS = 5
DEFAULT_GRPO_MAX_STEPS = 3  # a GRPO step costs num_generations rollouts
DEFAULT_PER_CELL_TIMEOUT = 900  # seconds, one cell
DEFAULT_WALL_TIMEOUT = 3600  # seconds, the whole notebook
DEFAULT_CONNECT_RETRIES = 2  # colab: fresh VMs to try after a lost kernel connection
DEFAULT_ALLOC_WAIT = 900  # colab: seconds to keep asking when a VM request is refused for capacity
ALLOC_POLL_SECONDS = 60
KERNEL_PROBE_TIMEOUT = 300  # seconds for a one-line cell to answer on a new VM
# The CLI raises this when its kernel websocket drops, then can sit on the dead socket until the
# wall timeout; seeing it means nothing more will come back from this VM.
COLAB_CONNECTION_LOST = ("Connection was lost",)
# `colab exec` prints this before every notebook cell. MEASURED (google-colab-cli 0.6.0 with
# jupyter_kernel_client 0.8.0): `--timeout` does NOT bound a cell. execute_interactive clamps the
# remaining time to 0 and keeps spinning on an empty iopub queue without ever raising, so a cell that
# goes quiet (or whose output stream stalls) runs until something kills the CLI. The per-cell bound
# is therefore enforced locally, from this marker.
COLAB_CELL_MARKER = re.compile(r"\[colab\] Executing cell (\d+)/(\d+)")
CELL_TIMEOUT_GRACE = 120  # seconds on top of --per-cell-timeout before the local kill
DEFAULT_IDLE_TIMEOUT = 1800  # seconds with no output line at all before the run is killed
KILL_GRACE_SECONDS = 20  # SIGINT first, so `colab exec` still saves the partial notebook

# Environment the injected bootstrap cell sets on the remote machine. An
# unattended kernel must never block on an interactive API-key prompt.
BOOTSTRAP_ENV = {
    "WANDB_DISABLED": "true",
    "WANDB_MODE": "disabled",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DISABLE_MLFLOW_INTEGRATION": "true",
    "TOKENIZERS_PARALLELISM": "false",
}

BOOTSTRAP_MARKER = "notebook_cloud_run bootstrap ok"
GPU_COUNT_RE = re.compile(r"notebook_cloud_run gpus=(\d+)")

# Verdicts. PASS is the only one that means "this notebook works".
PASS = "PASS"
CELL_ERROR = "CELL_ERROR"
OUT_OF_MEMORY = "OUT_OF_MEMORY"
INCOMPLETE = "INCOMPLETE"
NOT_RUN = "NOT_RUN"
NO_RESULT = "NO_RESULT"
TIMEOUT = "TIMEOUT"
CONNECTION_LOST = "CONNECTION_LOST"
# Every cell ran, but a logged training loss was NaN or inf: the model it produced is garbage.
NON_FINITE_LOSS = "NON_FINITE_LOSS"
# A GPU was requested but the kernel had none: Kaggle quietly runs an account without phone
# verification or GPU quota on CPU, and the first GPU cell then reads like a notebook bug.
NO_GPU = "NO_GPU"

# NO_RESULT, CONNECTION_LOST and NO_GPU mean "we never learned anything about this notebook", which
# is an infrastructure outcome rather than a verdict on the code.
INFRA_STATUSES = (NO_RESULT, CONNECTION_LOST, NO_GPU)

# stderr text that looks alarming but is not a failure. Checked before the
# traceback patterns so a warning that happens to contain the word "Error"
# cannot fail an otherwise clean run.
BENIGN_STREAM = re.compile(
    r"^\s*(?:"
    r"WARNING[: ]"
    r"|\w*(?:Warning|Deprecation)\b"
    r"|.*\b(?:UserWarning|FutureWarning|DeprecationWarning|RuntimeWarning)\b"
    r"|\d+%\|"  # tqdm progress bars
    r"|Downloading\b|Fetching\b|Loading\b|Map:|Unsloth[: ]"
    r")",
    re.IGNORECASE,
)

# A failure printed to a stream instead of raised through the kernel. This is
# how a `!command` shell escape, a subprocess, or a background thread fails:
# the cell itself "succeeds", the notebook records no error output, and a
# verdict built only on error outputs calls the run a pass.
#
# Note what is deliberately ABSENT: pip's
#   "ERROR: pip's dependency resolver does not currently take into account ..."
# is routine noise about packages already in the base image, and it is
# normally followed by "Successfully installed". Matching it fails healthy
# notebooks, and worse, it masks the real cause when there is one.
STREAM_FAILURE = re.compile(
    r"^\s*(?:"
    r"Traceback \(most recent call last\)"
    r"|ERROR: (?:Could not find a version that satisfies the requirement"
    r"|No matching distribution found for"
    r"|Failed building wheel for"
    r"|Cannot install)"
    r"|CUDA out of memory"
    r"|torch\.(?:cuda\.)?OutOfMemoryError"
    r"|No space left on device"
    r"|The kernel appears to have died"
    r")",
    re.MULTILINE,
)

# Out of memory is a hardware-size problem, not a broken notebook, and the
# remedy is a bigger card rather than a code fix -- so it gets its own status
# and its own message. Matched against the WHOLE traceback, not just the
# exception name: a library that catches an OOM and re-raises something else
# pushes the real cause thousands of characters up the stack.
OOM_PATTERN = re.compile(
    r"OutOfMemoryError"
    r"|CUDA out of memory"
    r"|CUBLAS_STATUS_ALLOC_FAILED"
    r"|HIP out of memory"
    r"|DefaultCPUAllocator: can't allocate memory"
    r"|Cannot copy out of meta tensor"
    r"|cannot be called on meta tensors"
    r"|enough GPU RAM to fit the quantized model",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_QUIET = False


def _safe_print(msg: str, stream = None) -> None:
    """print() that never raises. When the launching shell (or the `| tee` it
    pipes into) dies, every write raises BrokenPipeError; raised from teardown,
    that skipped `colab stop` and left a VM billing until the 24h cap."""
    try:
        print(msg, file = stream or sys.stdout, flush = True)
    except (OSError, ValueError):
        pass


def log(msg: str = "") -> None:
    if not _QUIET:
        _safe_print(msg)


def warn(msg: str) -> None:
    _safe_print("WARNING: %s" % msg, sys.stderr)


class CredentialError(Exception):
    """A credential is missing, malformed or rejected. Exit code 2."""


class InfraError(Exception):
    """The platform could not give us a usable machine. Exit code 3."""


class ConnectionLostError(InfraError):
    """The VM was allocated but its kernel connection never came up or dropped."""


class UsageError(Exception):
    """The invocation is wrong and no amount of waiting will fix it. Exit 2.

    Kept apart from InfraError on purpose. Exit 3 means "the platform had
    nothing for us right now", and the retry loop this script is built to sit
    inside retries on 3 and treats anything else as an answer. A mistyped
    --gpu is not a capacity problem, so returning 3 for one turns a typo into
    a loop that waits forever: measured at two jobs x 16 attempts x a 420s
    gap, about 3h45m spent rediscovering a spelling error, with every job
    queued behind them blocked for the duration.
    """


def _safe_filename(name: str) -> str:
    """A session name is user input and becomes a path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")
    return cleaned or "unnamed"


def _reported_hardware(status_text: str) -> Optional[str]:
    """The accelerator out of a `colab status` / `colab sessions` line.

    The CLI's own format is
      `[name] endpoint | Hardware: X | Shape: Y | Variant: Z`
    and X is the accelerator enum name (T4, L4, G4, A100, H100) or CPU,
    which is the same vocabulary as `Plan.remote_accelerator`, so the two
    compare directly with no mapping table to drift.
    """
    found = re.search(r"Hardware:\s*([A-Za-z0-9-]+)", status_text or "")
    return found.group(1).upper() if found else None


def _reported_shape(status_text: str) -> Optional[str]:
    """The machine shape out of a `colab status` line, upper-cased.

    `Shape: Standard` or `Shape: High-RAM` in google-colab-cli 0.7+; absent
    from 0.6.0's status line, which has no shape field at all.
    """
    found = re.search(r"Shape:\s*([A-Za-z-]+)", status_text or "")
    return found.group(1).upper() if found else None


def colab_state_home() -> Path:
    """Where a NAMED session's state lives, so it survives the run."""
    override = os.environ.get("NOTEBOOK_CLOUD_RUN_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "notebook-cloud-run"


COLAB_OWNER_FILE = "colab-owner.json"
COLAB_RUN_STATE = "colab-session-state.json"
COLAB_RUN_PREFIX = "unsloth-nbrun-"


def colab_runs_registry() -> Path:
    """One line per run dir that allocated an unnamed Colab VM (read by --reclaim-orphans)."""
    return colab_state_home() / "colab-runs.txt"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


_sleep = time.sleep  # patched by tests


def classify_platform_error(text: str) -> Optional[str]:
    """Return "capacity", "auth" or None for a platform error message."""
    low = (text or "").lower()
    for marker in CAPACITY_MARKERS:
        if marker in low:
            return "capacity"
    for marker in AUTH_MARKERS:
        if marker in low:
            return "auth"
    return None


def classify_kaggle_refusal(text: str) -> str:
    """ "quota" when Kaggle says the weekly GPU quota is gone, else "busy" (2-kernel cap,
    TooManyAssignments, precondition failed, rate limit): a short bench."""
    low = (text or "").lower()
    return "quota" if any(m in low for m in QUOTA_REFUSAL_MARKERS) else "busy"


def slugify(text: str) -> str:
    """Kaggle's title -> slug rule, closely enough to assert on it.

    Kaggle derives the kernel address from the TITLE, not from the `id` field,
    and the CLI only warns when the two disagree -- it still exits 0, and then
    every later status/output call 403s against an address you never saw.
    """
    out = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", out)[:50].strip("-")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# --------------------------------------------------------------------------
# Notebook resolution: local path | bare name | URL
# --------------------------------------------------------------------------


@dataclass
class NotebookSource:
    """Where a notebook argument points, resolved without touching the network."""

    spec: str
    kind: str  # "local" | "url"
    location: str  # path or primary URL
    name: str  # file name, used for reports and slugs
    fallbacks: List[str] = field(default_factory = list)

    def describe(self) -> str:
        if self.kind == "local":
            return "local file %s" % self.location
        return self.location


def colab_url_to_raw(url: str) -> str:
    """colab.research.google.com/github/<o>/<r>/... -> raw.githubusercontent.com."""
    marker = "colab.research.google.com/github/"
    idx = url.find(marker)
    if idx == -1:
        return url
    rest = url[idx + len(marker) :].split("#", 1)[0].split("?", 1)[0]
    rest = rest.replace("/blob/", "/", 1)
    return "https://raw.githubusercontent.com/" + rest


def github_blob_to_raw(url: str) -> str:
    """github.com/<o>/<r>/blob/<ref>/<path> -> raw.githubusercontent.com."""
    marker = "github.com/"
    idx = url.find(marker)
    if idx == -1 or "raw.githubusercontent.com" in url:
        return url
    rest = url[idx + len(marker) :].split("#", 1)[0].split("?", 1)[0]
    if "/blob/" in rest:
        rest = rest.replace("/blob/", "/", 1)
    elif "/raw/" in rest:
        rest = rest.replace("/raw/", "/", 1)
    else:
        return url
    return "https://raw.githubusercontent.com/" + rest


def normalise_url(url: str) -> str:
    if "colab.research.google.com/github/" in url:
        return colab_url_to_raw(url)
    if "github.com/" in url and "raw.githubusercontent.com" not in url:
        return github_blob_to_raw(url)
    return url


def resolve_notebook(spec: str) -> NotebookSource:
    """Turn one command-line notebook argument into a NotebookSource.

    Offline and side-effect free, so `--dry-run` and the tests can call it.
    """
    spec = (spec or "").strip()
    if not spec:
        raise InfraError("empty notebook argument")

    if spec.startswith(("http://", "https://")):
        url = normalise_url(spec)
        # Take the name from the URL PATH only, and reduce it to a bare file
        # name: a query string is not part of the file name (and is invalid in
        # one on Windows), and a percent-encoded %2F would otherwise decode
        # into a path that escapes the staging directory.
        name = Path(url_unquote(urlsplit(url).path)).name or "notebook.ipynb"
        return NotebookSource(spec = spec, kind = "url", location = url, name = name)

    path = Path(spec).expanduser()
    if path.exists():
        return NotebookSource(spec = spec, kind = "local", location = str(path.resolve()), name = path.name)

    # A bare name (or a repo-relative path) in unslothai/notebooks.
    name = spec if spec.lower().endswith(".ipynb") else spec + ".ipynb"
    if "/" in name:
        return NotebookSource(
            spec = spec,
            kind = "url",
            location = "%s/%s" % (NOTEBOOKS_RAW_BASE, name),
            name = name.rsplit("/", 1)[-1],
        )
    candidates = ["%s/%s/%s" % (NOTEBOOKS_RAW_BASE, d, name) for d in NOTEBOOKS_DIRS]
    return NotebookSource(
        spec = spec, kind = "url", location = candidates[0], name = name, fallbacks = candidates[1:]
    )


def fetch_notebook(
    source: NotebookSource,
    dest_dir: Path,
    timeout: int = 60,
) -> Path:
    """Materialise a NotebookSource as a local .ipynb. Touches the network."""
    dest_dir.mkdir(parents = True, exist_ok = True)
    dest = dest_dir / source.name

    if source.kind == "local":
        shutil.copyfile(source.location, dest)
        return dest

    errors = []
    for url in [source.location] + source.fallbacks:
        # Unsloth notebook names contain parentheses and spaces; percent-encode
        # everything that is not already a URL delimiter.
        quoted = url_quote(url, safe = ":/?#[]@!$&'*+,;=%~")
        try:
            with urllib.request.urlopen(quoted, timeout = timeout) as resp:
                dest.write_bytes(resp.read())
            if url != source.location:
                log("  resolved %s -> %s" % (source.spec, url))
            return dest
        except urllib.error.HTTPError as exc:
            errors.append("%s -> HTTP %s" % (url, exc.code))
        except Exception as exc:  # noqa: BLE001
            errors.append("%s -> %s: %s" % (url, type(exc).__name__, exc))

    raise InfraError(
        "could not download notebook %r.\nTried:\n  %s\n"
        "If this is a name from %s, check the spelling -- names are "
        "case-sensitive and include the parentheses."
        % (source.spec, "\n  ".join(errors), NOTEBOOKS_REPO)
    )


def read_notebook(path: Path) -> dict:
    """Load a notebook, insisting it actually parses.

    Existence is not enough: a download killed mid-write leaves a file of
    plausible size, and treating it as complete moves the failure to the
    verdict stage, where it looks like the notebook's fault.
    """
    try:
        # utf-8-sig: a notebook saved by a Windows editor carries a BOM, which
        # decodes fine but breaks json.load -- and would then be reported as a
        # truncated download, blaming the network for the user's editor.
        with path.open("r", encoding = "utf-8-sig") as fh:
            nb = json.load(fh)
    except (OSError, ValueError) as exc:
        size = path.stat().st_size if path.is_file() else 0
        raise InfraError(
            "%s could not be read as a notebook (%s: %s); if it came off the "
            "network the download was probably truncated at %d bytes"
            % (path, type(exc).__name__, exc, size)
        )
    if not isinstance(nb, dict) or "cells" not in nb:
        raise InfraError("%s does not look like a Jupyter notebook" % path)
    return nb


# --------------------------------------------------------------------------
# Smoke patching: make a run cheap without making it meaningless
# --------------------------------------------------------------------------

# Anchored on a delimiter rather than only on line start, so an inline
# `SFTConfig(max_steps = 60, ...)` is capped instead of falling through to the
# fallback, which would then splice in a duplicate keyword argument and make
# the cell a SyntaxError. A commented-out `# max_steps = 60,` still does not
# match, which is what we want -- 17 repo notebooks carry one.
_MAX_STEPS_RE = re.compile(r"(?m)(?P<lead>(?:^|[(,])[ \t]*max_steps\s*=\s*)(?P<val>\d+)")
_LOGGING_STEPS_RE = re.compile(r"(?m)(?P<lead>(?:^|[(,])[ \t]*logging_steps\s*=\s*)(?P<val>\d+)")
# Indentation is required: a module-level `num_train_epochs = 3` that is later
# passed into the config by name is not the config, and inserting the cap
# there both does nothing and suppresses the fallback that would have worked.
_EPOCHS_RE = re.compile(r"(?m)^(?P<indent>[ \t]+)num_train_epochs\s*=\s*[\d.]+")
_REPORT_TO_RE = re.compile(r"""(?P<lead>report_to\s*=\s*)(?P<q>["'])(?P<val>[^"']*)(?P=q)""")
_CAPTURE_RE = re.compile(r"(?m)^[ \t]*%%capture.*$")
_GRPO_RE = re.compile(r"\bGRPO(?:Trainer|Config)\b")
_TRAINER_CONFIG_RE = re.compile(
    r"\b(?:SFTConfig|GRPOConfig|DPOConfig|ORPOConfig|KTOConfig|TrainingArguments|"
    r"UnslothTrainingArguments)\s*\("
)


def _match_is_code(text: str, position: int) -> bool:
    """True if `position` is not inside a `#` comment or a string on its line.

    Deliberately simple -- it exists to stop the one rule that splices text
    from firing on `# see TrainingArguments(...) in the docs`, not to be a
    Python tokenizer.
    """
    line_start = text.rfind("\n", 0, position) + 1
    quote = ""
    escaped = False
    for char in text[line_start:position]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return False
    return not quote


def _cell_source(cell: dict) -> str:
    src = cell.get("source", "")
    if isinstance(src, list):
        return "".join(src)
    return src or ""


def _set_cell_source(cell: dict, text: str) -> None:
    cell["source"] = text.splitlines(keepends = True)


def build_bootstrap_cell(extra_env: Optional[Dict[str, str]] = None) -> dict:
    """The first cell we inject. Never routed through `colab --env`."""
    env = dict(BOOTSTRAP_ENV)
    env.update(extra_env or {})
    lines = [
        "# Injected by notebook_cloud_run.py -- keeps an unattended kernel from",
        "# blocking on an interactive API-key prompt, and silences telemetry.",
        "import os",
    ]
    for key in sorted(env):
        lines.append("os.environ[%r] = %r" % (key, str(env[key])))
    lines.append("print(%r, flush=True)" % BOOTSTRAP_MARKER)
    lines += [
        "import subprocess as _p",
        "try: _g = _p.run(['nvidia-smi', '-L'], capture_output=True, text=True, timeout=60).stdout.count('GPU ')",
        "except Exception: _g = 0",
        "print('notebook_cloud_run gpus=%d' % _g, flush=True)",
    ]
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [ln + "\n" for ln in lines],
    }


def ensure_cell_ids(nb: dict) -> dict:
    """Give every cell an `id`, in place.

    nbformat 4.5 made `id` mandatory and currently warns on every notebook
    that lacks one; the warning is documented to become a hard error. Both
    backends validate the notebook before running it, so a notebook we
    assembled ourselves must not be the one that trips over that.
    """
    nb.setdefault("nbformat", 4)
    if nb.get("nbformat") == 4 and nb.get("nbformat_minor", 0) < 5:
        nb["nbformat_minor"] = 5
    for cell in nb.get("cells", []):
        if not cell.get("id"):
            cell["id"] = uuid.uuid4().hex[:8]
    return nb


def smoke_patch_notebook(
    nb: dict,
    max_steps: int = DEFAULT_MAX_STEPS,
    grpo_max_steps: int = DEFAULT_GRPO_MAX_STEPS,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[dict, List[str]]:
    """Shorten training and make failures visible, on a copy of the notebook.

    Deliberately conservative. It only rewrites values it can see, and it
    never RAISES a step count that is already lower than the target, so
    `--max-steps 100` cannot make a 5-step notebook slower. Model ids and
    dataset ids are never touched -- that is how a removed model gets
    detected instead of silently substituted.

    Returns (patched_notebook, human-readable list of what changed).
    """
    nb = json.loads(json.dumps(nb))  # deep copy, no dependencies
    changes: List[str] = []
    saw_max_steps = False
    trainer_cells: List[int] = []

    for index, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        src = _cell_source(cell)
        if not src.strip():
            continue
        original = src

        # 1. Strip %%capture. A pip install that fails with its stdout
        #    captured is invisible until a much later ImportError, and the
        #    stream scanner is the only thing that catches that class at all.
        if _CAPTURE_RE.search(src):
            src = _CAPTURE_RE.sub("", src, count = 1).lstrip("\n")
            changes.append("cell %d: removed %%%%capture so install errors are visible" % index)

        # A GRPO step costs roughly num_generations rollouts, so it gets a
        # tighter budget. The choice is per cell, because a GRPO notebook's
        # SFT pre-warmup trainer should still get the full budget.
        cell_steps = grpo_max_steps if _GRPO_RE.search(src) else max_steps

        # 2. Cap max_steps.
        def _steps(match: "re.Match[str]") -> str:
            current = int(match.group("val"))
            if current <= cell_steps:
                return match.group(0)
            changes.append("cell %d: max_steps %d -> %d" % (index, current, cell_steps))
            return "%s%d" % (match.group("lead"), cell_steps)

        if _MAX_STEPS_RE.search(src):
            saw_max_steps = True
            src = _MAX_STEPS_RE.sub(_steps, src)
        elif _TRAINER_CONFIG_RE.search(src):
            trainer_cells.append(index)
            # 3. A config block with epochs but no max_steps trains for a full
            #    pass over the dataset. Add a step cap in front of it rather
            #    than replacing it, so the original intent stays readable.
            if _EPOCHS_RE.search(src):
                src = _EPOCHS_RE.sub(
                    lambda m: "%smax_steps = %d,\n%s" % (m.group("indent"), cell_steps, m.group(0)),
                    src,
                    count = 1,
                )
                saw_max_steps = True
                changes.append(
                    "cell %d: inserted max_steps = %d before "
                    "num_train_epochs" % (index, cell_steps)
                )

        # 4. Lower logging_steps to match. Capping a run at 5 steps while the
        #    notebook logs every 50 produces a run with no loss at all, which
        #    reads exactly like a training regression that never happened.
        def _logging(match: "re.Match[str]") -> str:
            if int(match.group("val")) <= cell_steps:
                return match.group(0)
            changes.append(
                "cell %d: logging_steps %s -> 1 (the shortened run "
                "would otherwise log nothing)" % (index, match.group("val"))
            )
            return "%s1" % match.group("lead")

        src = _LOGGING_STEPS_RE.sub(_logging, src)

        # 5. Never report to an external tracker from an unattended kernel.
        #    The original quote character is preserved: emitting `"none"` into
        #    a match that lived inside a double-quoted string would terminate
        #    that string and make the cell a SyntaxError.
        def _report(match: "re.Match[str]") -> str:
            if match.group("val") == "none":
                return match.group(0)
            changes.append("cell %d: report_to %r -> 'none'" % (index, match.group("val")))
            quote = match.group("q")
            return "%s%snone%s" % (match.group("lead"), quote, quote)

        src = _REPORT_TO_RE.sub(_report, src)

        if src != original:
            _set_cell_source(cell, src)

    # Fallback: a trainer config with neither max_steps nor num_train_epochs
    # still needs a cap, or the "smoke" run trains on the whole dataset.
    #
    # This is the only rule that SPLICES text rather than replacing a value,
    # so it is the only one that can produce invalid Python. It therefore
    # refuses to fire on a match inside a comment or a string literal, and
    # refuses to fire at all if the cell mentions max_steps anywhere -- a
    # second `max_steps=` in the same call is `SyntaxError: keyword argument
    # repeated`, which nothing would catch until the cell ran on a rented GPU.
    if not saw_max_steps and trainer_cells:
        index = trainer_cells[0]
        cell = nb["cells"][index]
        src = _cell_source(cell)
        match = next(
            (m for m in _TRAINER_CONFIG_RE.finditer(src) if _match_is_code(src, m.start())), None
        )
        if match is not None and not re.search(r"\bmax_steps\s*=", src):
            steps = grpo_max_steps if _GRPO_RE.search(src) else max_steps
            _set_cell_source(
                cell, "%s\n    max_steps = %d,%s" % (src[: match.end()], steps, src[match.end() :])
            )
            changes.append("cell %d: added max_steps = %d to the trainer config" % (index, steps))

    nb.setdefault("cells", []).insert(0, build_bootstrap_cell(extra_env))
    changes.insert(
        0,
        "cell 0: injected bootstrap cell (%d environment variables)"
        % len(dict(BOOTSTRAP_ENV, **(extra_env or {}))),
    )
    return ensure_cell_ids(nb), changes


# --------------------------------------------------------------------------
# The verdict: parse the executed notebook, never the exit code
# --------------------------------------------------------------------------


@dataclass
class CellFailure:
    index: int
    ename: str
    evalue: str
    detail: str = ""
    is_oom: bool = False

    def one_line(self) -> str:
        head = "code cell %d: %s" % (self.index, self.ename)
        if self.evalue:
            head += ": " + self.evalue.strip().splitlines()[0][:200]
        return head


@dataclass
class Verdict:
    status: str
    code_cells: int = 0
    executed_cells: int = 0
    failures: List[CellFailure] = field(default_factory = list)
    reason: str = ""
    training_losses: List[float] = field(default_factory = list)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def summary(self) -> str:
        if self.reason:
            return self.reason
        if self.status == PASS:
            trained = (
                ", %d training steps logged" % len(self.training_losses)
                if self.training_losses
                else ""
            )
            return "%d/%d code cells executed cleanly%s" % (
                self.executed_cells,
                self.code_cells,
                trained,
            )
        if self.status == OUT_OF_MEMORY and self.failures:
            return (
                "ran out of memory -- %s. Retry on a larger tier "
                "(--gpu A100 on Colab)" % self.failures[0].one_line()
            )
        if self.failures:
            return self.failures[0].one_line()
        return self.status


def _cell_was_executed(cell: dict) -> bool:
    if cell.get("execution_count") is not None:
        return True
    return bool(cell.get("outputs"))


def _output_text(output: dict, key: str = "text") -> str:
    text = output.get(key, "")
    if isinstance(text, list):
        text = "".join(text)
    return text or ""


def stream_failure(text: str) -> Optional[str]:
    """Return the offending line if a stream carries a real failure.

    A cell can "succeed" while the work inside it fails: a `!command` shell
    escape, a subprocess, or a pip install of a package that does not exist
    all print their failure to a stream and leave execution running. Without
    this scan those runs are indistinguishable from a clean pass.
    """
    if not text:
        return None
    # pip colours every `ERROR:` line red, and \x1b is not \s, so `^\s*ERROR:`
    # never anchors against a raw stream. Skipping this makes the entire
    # dependency half of STREAM_FAILURE dead code on both platforms.
    text = strip_ansi(text)
    for match in STREAM_FAILURE.finditer(text):
        # `^\s*` can begin the match on the PRECEDING blank line, which would
        # slice out an empty string; the caller tests the result for truth, so
        # that silently drops a real failure. Anchor on the keyword instead.
        matched = match.group(0)
        keyword = match.start() + (len(matched) - len(matched.lstrip()))
        line_start = text.rfind("\n", 0, keyword) + 1
        line_end = text.find("\n", keyword)
        line = text[line_start : line_end if line_end != -1 else len(text)]
        if BENIGN_STREAM.match(line):
            continue
        if line.strip():
            return line.strip()
    return None


# TRL renders its Step / Training Loss table as text/html only -- there is no
# text/plain twin. Skipping the mimetype makes a run that trained perfectly
# well look like it never trained. The gap between the two <td>s is matched
# with [^<]* rather than \s* because we read the RAW notebook JSON, where that
# gap is `</td>\n",\n       "      <td>`: quotes, commas and indentation. It
# still cannot cross a </tr><tr>, so it cannot pair a step with another row.
# nan / inf must match too: a diverged run logs them, and skipping those rows is how a notebook
# that trained to NaN used to read as PASS with "2 training steps logged".
_LOSS_VALUE = r"([-+]?(?:[0-9]*\.[0-9]+(?:e[-+]?[0-9]+)?|nan|inf(?:inity)?)(?![A-Za-z0-9_]))"
_LOSS_TABLE_RE = re.compile(
    r"<td[^>]*>\s*(\d+)\s*</td>[^<]*<td[^>]*>\s*" + _LOSS_VALUE + r"\s*</td>", re.I
)
# transformers 5 logs values quoted: {'loss': '1.423'}.
_LOSS_JSON_RE = re.compile(r"['\"]loss['\"]:\s*['\"]?" + _LOSS_VALUE, re.I)


def extract_training_losses(text: str, limit: Optional[int] = 50) -> List[float]:
    losses = [float(v) for _, v in _LOSS_TABLE_RE.findall(text)]
    if not losses:
        losses = [float(v) for v in _LOSS_JSON_RE.findall(text)]
    return losses if limit is None else losses[:limit]


def parse_executed_notebook(
    nb: dict,
    context: str = "",
    expect_gpu: bool = False,
) -> Verdict:
    """Decide pass/fail from an executed notebook's cell outputs.

    THIS is the verdict. `colab exec` exits 0 with a raised ValueError sitting
    in the output notebook, and a Kaggle kernel reports "complete" for a
    notebook whose first cell died. Nothing else in this file is allowed to
    declare a run successful.
    """
    code = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]

    # Our own injected bootstrap cell is not the user's code: counting it
    # shifts every reported cell index by one against the notebook they have.
    # Its marker is also the only proof that anything ran in THIS session --
    # most notebooks ship with the author's saved outputs, and an artifact
    # that was never executed is otherwise indistinguishable from a clean run.
    bootstrap = [c for c in code if BOOTSTRAP_MARKER in _cell_source(c)]
    bootstrap_ids = {id(c) for c in bootstrap}
    bootstrap_ran = any(
        BOOTSTRAP_MARKER in _output_text(output)
        for cell in bootstrap
        for output in (cell.get("outputs") or [])
        if output.get("output_type") == "stream"
    )

    cells = [c for c in code if id(c) not in bootstrap_ids and _cell_source(c).strip()]
    code_cells = len(cells)
    gpus = [
        int(m)
        for cell in bootstrap
        for output in (cell.get("outputs") or [])
        if output.get("output_type") == "stream"
        for m in GPU_COUNT_RE.findall(_output_text(output))
    ]
    if expect_gpu and gpus and max(gpus) == 0:
        return Verdict(
            NO_GPU,
            code_cells,
            0,
            [],
            "a GPU was requested but the kernel ran without one: the platform gave this account "
            "a CPU machine (on Kaggle: no phone verification or no GPU quota), so nothing here "
            "says anything about the notebook%s" % (" (%s)" % context if context else ""),
        )
    failures: List[CellFailure] = []
    rich_text: List[str] = []
    text_by_cell: List[Tuple[int, str]] = []

    # Cells run in order, so a cell that printed nothing (a def-only cell, where Colab also
    # records no execution_count) still ran if any later cell did. Counting only cells with
    # evidence reports a finished notebook as INCOMPLETE.
    ran = [index for index, cell in enumerate(cells) if _cell_was_executed(cell)]
    executed = ran[-1] + 1 if ran else 0
    for index, cell in enumerate(cells):
        for output in cell.get("outputs", []) or []:
            otype = output.get("output_type")
            if otype == "error":
                traceback = output.get("traceback") or []
                if isinstance(traceback, list):
                    traceback = "\n".join(traceback)
                traceback = strip_ansi(str(traceback))
                ename = output.get("ename") or "Error"
                evalue = output.get("evalue") or ""
                failures.append(
                    CellFailure(
                        index = index,
                        ename = ename,
                        evalue = evalue,
                        detail = traceback[-2000:],
                        # Scan the whole traceback, not just ename/evalue: a
                        # library that swallows an OOM and re-raises something
                        # else pushes the real cause off the end of the summary.
                        is_oom = bool(OOM_PATTERN.search("%s %s %s" % (ename, evalue, traceback))),
                    )
                )
            elif otype == "stream":
                text = strip_ansi(_output_text(output))
                hit = stream_failure(text)
                if hit:
                    failures.append(
                        CellFailure(
                            index = index,
                            ename = "StreamFailure",
                            evalue = hit,
                            detail = "printed to %s; the cell itself did not raise"
                            % (output.get("name") or "stdout"),
                            # Scan the whole stream, as with a traceback: the OOM
                            # is often several lines below the matched header.
                            is_oom = bool(OOM_PATTERN.search(text)),
                        )
                    )
                rich_text.append(text)
                text_by_cell.append((index, text))
            elif otype in ("execute_result", "display_data"):
                data = output.get("data", {}) or {}
                for mime in ("text/html", "text/plain"):
                    if mime in data:
                        rich_text.append(_output_text(data, mime))
                        text_by_cell.append((index, _output_text(data, mime)))

    all_losses = extract_training_losses("\n".join(rich_text), limit = None)
    losses = all_losses[:50]
    cell_losses = [
        (index, extract_training_losses(text, limit = None)) for index, text in text_by_cell
    ]
    non_finite_cell = next(
        (index for index, found in cell_losses if not all(math.isfinite(x) for x in found)), None
    )

    if code_cells == 0:
        return Verdict(NOT_RUN, 0, 0, [], "the notebook has no runnable code cells")
    if bootstrap and not bootstrap_ran:
        return Verdict(
            NOT_RUN,
            code_cells,
            0,
            [],
            "the injected bootstrap cell never printed its marker, so nothing "
            "ran in this session; any outputs in this notebook are the ones it "
            "shipped with%s" % (" (%s)" % context if context else ""),
        )
    if executed == 0:
        return Verdict(
            NOT_RUN,
            code_cells,
            0,
            failures,
            "no cell produced any output; the kernel never started%s"
            % (" (%s)" % context if context else ""),
            losses,
        )
    # A NaN loss in an earlier cell than the first error is the root cause: transformers 5
    # samples from the NaN logits and dies with a device-side assert two cells later.
    if non_finite_cell is not None and (
        not failures or non_finite_cell <= min(f.index for f in failures)
    ):
        logged = [x for index, found in cell_losses if index == non_finite_cell for x in found]
        bad = [i for i, x in enumerate(logged) if not math.isfinite(x)]
        finite = [x for x in logged[: bad[0]] if math.isfinite(x)]
        return Verdict(
            NON_FINITE_LOSS,
            code_cells,
            executed,
            failures,
            "training loss went non-finite: %d of %d logged steps are nan/inf, the first at "
            "logged step %d%s; the trained model is unusable%s"
            % (
                len(bad),
                len(logged),
                bad[0] + 1,
                " (last finite loss %.4g)" % finite[-1] if finite else "",
                " (%s)" % context if context else "",
            ),
            losses,
        )
    if failures:
        # Report the earliest failing cell first: later ones are usually
        # consequences of it. The STATUS has to come from that root cause too,
        # or an import error with a downstream OOM tells the user to rent a
        # bigger GPU for what is really a version pin.
        failures.sort(key = lambda f: (f.index, 0 if f.ename != "StreamFailure" else 1))
        status = OUT_OF_MEMORY if failures[0].is_oom else CELL_ERROR
        return Verdict(status, code_cells, executed, failures, "", losses)
    if executed < code_cells:
        return Verdict(
            INCOMPLETE,
            code_cells,
            executed,
            failures,
            "stopped after %d of %d code cells with no error recorded; the "
            "kernel died or the run was cut short%s"
            % (executed, code_cells, " (%s)" % context if context else ""),
            losses,
        )
    return Verdict(PASS, code_cells, executed, failures, "", losses)


def verdict_from_file(
    path: Path,
    context: str = "",
    expect_gpu: bool = False,
) -> Verdict:
    return parse_executed_notebook(read_notebook(path), context, expect_gpu)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


@dataclass
class KaggleCredentials:
    """A Kaggle credential in any of the three shapes the CLI accepts.

    * `token`      -- a modern bearer token (KGAT_...), the current default
    * `username` + `key` -- the legacy API key pair from kaggle.json
    * neither      -- "delegated": the CLI already has a credential of its own
                      (`kaggle auth login`, or ~/.kaggle/access_token), and we
                      simply let it use it rather than re-implementing its
                      lookup badly.
    """

    username: Optional[str] = None
    key: Optional[str] = None
    token: Optional[str] = None
    source: str = ""

    @property
    def mode(self) -> str:
        if self.token:
            return "token"
        if self.username and self.key:
            return "api-key"
        return "delegated"

    def env(self) -> Dict[str, str]:
        if self.mode == "token":
            return {"KAGGLE_API_TOKEN": self.token or ""}
        if self.mode == "api-key":
            return {"KAGGLE_USERNAME": self.username or "", "KAGGLE_KEY": self.key or ""}
        return {}

    def redacted(self) -> str:
        """Never print a whole credential, not even into a local log."""
        secret = self.token or self.key or ""
        who = self.username or "(username resolved from Kaggle)"
        if self.mode == "delegated":
            return "%s using the kaggle CLI's own credential (%s)" % (who, self.source)
        return "%s (%s %s..., from %s)" % (
            who,
            "token" if self.mode == "token" else "key",
            secret[:4],
            self.source,
        )


# ---- Numbered Kaggle tokens and the 70% switch --------------------------
#
# Several Kaggle accounts are usually available as KAGGLE_API_TOKEN,
# KAGGLE_API_TOKEN_2, KAGGLE_API_TOKEN_3 ... The _2 account is the preferred
# one, so it leads; the unnumbered token is the fallback; anything else
# follows in numeric order.
#
# The choice is a weighted random draw (choose_kaggle_token). The weight is the
# live remaining quota from `kaggle quota --format json` (CLI >= 2.2.4; cached,
# a token under the floor is skipped), falling back to the weekly allowance
# when that read fails. It does NOT gate on the local ledger, which was wrong both ways in practice
# (93.9 h shown vs 15.8 h real from stacked marks; 2.6 h vs 21.4 h real from
# runs on other machines and the website). Kaggle's own refusal is the signal:
# a quota refusal benches the token for _KAGGLE_QUOTA_HOLD_S, a busy /
# concurrency / rate-limit refusal for _KAGGLE_BUSY_HOLD_S, and a benched
# token drops out of the draw. The ledger records wall hours for display.
KAGGLE_PREFERRED_TOKEN_ENV = "KAGGLE_API_TOKEN_2"
KAGGLE_FALLBACK_TOKEN_ENV = "KAGGLE_API_TOKEN"
# Kaggle's published free allowance is 30 GPU hours per week per account; ours differ.
KAGGLE_WEEKLY_GPU_HOURS = 30.0
# Billed on notebook wall time, not GPU time, so an idle kernel still drains the allowance.
KAGGLE_WEEKLY_GPU_HOURS_BY_ENV = {
    "KAGGLE_API_TOKEN": 60.0,
    "KAGGLE_API_TOKEN_1": 60.0,
    "KAGGLE_API_TOKEN_2": 45.0,
    "KAGGLE_API_TOKEN_3": 60.0,
}


def kaggle_weekly_hours(env_name: Optional[str]) -> float:
    return KAGGLE_WEEKLY_GPU_HOURS_BY_ENV.get(env_name or "", KAGGLE_WEEKLY_GPU_HOURS)


_KAGGLE_LEDGER_WINDOW_S = 7 * 24 * 3600
# Bench lengths after a refusal. Marks used to be recorded as a whole week's allowance of usage,
# so they stacked and a brief capacity blip benched the account for 7 days.
_KAGGLE_QUOTA_HOLD_S = 24 * 3600  # weekly quota gone (Kaggle's reset time is not exposed)
_KAGGLE_BUSY_HOLD_S = 10 * 60  # 2-kernel cap, TooManyAssignments, rate limit
QUOTA_REFUSAL_MARKERS = (
    "no quota for",
    "no accelerator quota",
    "quota exceeded",
    "exceeded your",
    "weekly",
    "out of quota",
    "quota has been",
)
# The exact values old mark_saturated() wrote as one usage entry (the largest allowance: 30 h,
# then 60 h). Read as marks, never as usage.
_KAGGLE_LEGACY_MARKS_S = (30 * 3600.0, 60 * 3600.0)
_SAT_PREFIX = "sat:"


def kaggle_token_env_names(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Names of the KAGGLE_API_TOKEN* variables that are actually set, in
    preference order: _2 first, then the unnumbered one, then _3, _4, ...

    Only names with a non-empty value are returned, so an exported-but-empty
    variable cannot shadow a working one.
    """
    env = os.environ if env is None else env
    numbered = []
    for name, value in env.items():
        if not (value or "").strip():
            continue
        m = re.fullmatch(r"KAGGLE_API_TOKEN_(\d+)", name)
        if m and name != KAGGLE_PREFERRED_TOKEN_ENV:
            numbered.append((int(m.group(1)), name))
    ordered = []
    for name in (KAGGLE_PREFERRED_TOKEN_ENV, KAGGLE_FALLBACK_TOKEN_ENV):
        if (env.get(name) or "").strip():
            ordered.append(name)
    ordered.extend(name for _, name in sorted(numbered))
    return ordered


def _token_fingerprint(token: str) -> str:
    """Stable per-token id for the ledger. Never stores the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class KaggleUsageLedger:
    """Rolling 7-day record of GPU seconds this tool spent per Kaggle token.

    One small JSON file, keyed by token fingerprint. Entries older than the
    window are dropped on every load, so the file cannot grow without bound
    and a week-old burst stops counting against the account by itself.

    Every operation is best-effort: a missing, unreadable or corrupt ledger
    means "no recorded usage", never a crash. Losing the ledger costs one
    suboptimal account choice, which is not worth failing a run over.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        now: Optional[float] = None,
    ) -> None:
        self.path = path or (colab_state_home() / "kaggle_usage.json")
        self._now = now if now is not None else time.time()

    def _load(self) -> Dict[str, List[List[float]]]:
        try:
            raw = json.loads(self.path.read_text(encoding = "utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        cutoff = self._now - _KAGGLE_LEDGER_WINDOW_S
        out: Dict[str, List[List[float]]] = {}
        for fp, entries in raw.items():
            if not isinstance(entries, list):
                continue
            kept = [
                e
                for e in entries
                if isinstance(e, list)
                and len(e) == 2
                and isinstance(e[0], (int, float))
                and e[0] >= cutoff
            ]
            if kept:
                out[str(fp)] = kept
        return out

    def _save(self, data: Dict[str, List[List[float]]]) -> None:
        try:
            self.path.parent.mkdir(parents = True, exist_ok = True)
            tmp = self.path.with_suffix(".tmp%d" % os.getpid())
            tmp.write_text(json.dumps(data), encoding = "utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def record(self, token: str, gpu_seconds: float) -> None:
        """Add GPU seconds spent on `token` (saturation is separate: mark_saturated)."""
        if not token or gpu_seconds <= 0:
            return
        # Load-modify-save under a lock: two runs finishing together must not
        # drop each other's entry (parallel packed kernels, several workspaces).
        with self._locked():
            data = self._load()
            data.setdefault(_token_fingerprint(token), []).append(
                [round(self._now, 1), round(float(gpu_seconds), 1)]
            )
            self._save(data)

    @contextlib.contextmanager
    def _locked(self):
        try:
            import fcntl
            self.path.parent.mkdir(parents = True, exist_ok = True)
            fh = open(self.path.with_suffix(".lock"), "a+")
        except (ImportError, OSError):  # Windows, or an unwritable dir: best effort as before
            yield
            return
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            fh.close()

    def mark_saturated(
        self,
        token: str,
        hold_s: float = _KAGGLE_QUOTA_HOLD_S,
    ) -> None:
        """Bench `token` for `hold_s` (kept under its own key, never summed as usage)."""
        if not token:
            return
        with self._locked():
            data = self._load()
            data.setdefault(_SAT_PREFIX + _token_fingerprint(token), []).append(
                [round(self._now, 1), round(float(hold_s), 1)]
            )
            self._save(data)

    def saturated_until(self, token: str) -> float:
        """Epoch the token's bench ends (0 = not benched)."""
        data = self._load()
        fp = _token_fingerprint(token)
        marks = list(data.get(_SAT_PREFIX + fp, []))
        # legacy marks: a whole allowance recorded as one usage entry
        marks += [
            [e[0], _KAGGLE_QUOTA_HOLD_S] for e in data.get(fp, []) if e[1] in _KAGGLE_LEGACY_MARKS_S
        ]
        return max([t + hold for t, hold in marks] + [0.0])

    def saturated(self, token: str) -> bool:
        return self.saturated_until(token) > self._now

    def used_hours(self, token: str) -> float:
        """Real GPU hours this week (saturation marks, legacy ones included, are not usage)."""
        entries = self._load().get(_token_fingerprint(token), [])
        return sum(e[1] for e in entries if e[1] not in _KAGGLE_LEGACY_MARKS_S) / 3600.0


def choose_kaggle_token(
    env: Optional[Mapping[str, str]] = None,
    ledger: Optional[KaggleUsageLedger] = None,
    rng: Optional[random.Random] = None,
    quota = None,
) -> Optional[Tuple[str, str, str]]:
    """Pick a Kaggle token from the numbered environment variables.

    Returns `(env_name, token, reason)`, or None when none are set (the caller
    then falls through to the rest of the credential chain unchanged).

    Weighted random draw over the tokens that are not benched, weight = weekly
    allowance (60 : 45 : 60), so load spreads in proportion to what each
    account can take. If every token is benched, the one whose bench ends
    first is used anyway: this run was asked for on Kaggle, and Kaggle's own
    refusal is the real stop.
    """
    names = kaggle_token_env_names(env)
    if not names:
        return None
    env = os.environ if env is None else env
    ledger = ledger or KaggleUsageLedger()
    rng = rng or random.Random()
    toks = [(n, (env.get(n) or "").strip()) for n in names]
    free = [(n, t) for n, t in toks if not ledger.saturated(t)]
    # `quota` (token -> kaggle_quota dict or None): a token whose live remaining hours are
    # under KAGGLE_MIN_REMAINING_H is benched until Kaggle's refreshAt, and the draw weighs
    # by remaining hours instead of the nominal allowance. Unknown quota = old behaviour.
    live = {t: quota(t) for _, t in free} if quota else {}
    for n, t in list(free):
        q = live.get(t)
        if q and q["remaining_h"] < KAGGLE_MIN_REMAINING_H:
            ledger.mark_saturated(
                t,
                max(600.0, (q.get("refresh_at") or 0) - time.time())
                if q.get("refresh_at")
                else _KAGGLE_QUOTA_HOLD_S,
            )
            free.remove((n, t))
    if not free:
        n, t = min(toks, key = lambda x: ledger.saturated_until(x[1]))
        return (n, t, "every Kaggle token is benched after a refusal; %s's bench ends first" % n)
    weights = [
        float(live[t]["remaining_h"]) if live.get(t) else kaggle_weekly_hours(n) for n, t in free
    ]
    n, t = rng.choices(free, weights = weights)[0]
    if live.get(t):
        return (
            n,
            t,
            "%s drawn with weight %.1f/%.1f (live remaining GPU hours)"
            % (n, live[t]["remaining_h"], sum(weights)),
        )
    return (
        n,
        t,
        "%s drawn with weight %g/%g (weekly allowance)" % (n, kaggle_weekly_hours(n), sum(weights)),
    )


# ---- Live quota (`kaggle quota`, CLI >= 2.x) -------------------------------
#
# The CLI now has a first-party quota reading (probed 2026-10-02 with CLI 2.2.4:
# `kaggle quota --format json` -> [{"resource": "GPU", "used": "50.87h",
# "remaining": "9.13h", "total": "60.00h", "refreshAt": "2026-10-03T00:00:00"}]).
# It replaces the hand-typed `kaggle-sync` baseline when it answers; when it
# does not (old CLI, network), behaviour falls back to the allowance draw.
KAGGLE_QUOTA_TTL_S = 600
KAGGLE_MIN_REMAINING_H = 0.5  # below this a token is skipped (a run would die mid-way)


def _hours(text) -> Optional[float]:
    m = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*h?\s*$", str(text or ""))
    return float(m.group(1)) if m else None


def parse_kaggle_quota(text: str) -> Optional[Dict[str, object]]:
    """The GPU row of `kaggle quota --format json` -> {used_h, remaining_h, total_h, refresh_at}."""
    try:
        rows = json.loads(text[text.index("[") :])
    except (ValueError, AttributeError):
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("resource", "")).upper() == "GPU":
            used, left, total = (_hours(row.get(k)) for k in ("used", "remaining", "total"))
            if used is None or left is None or total is None:
                return None
            refresh = None
            with contextlib.suppress(ValueError, TypeError):
                import calendar
                refresh = float(
                    calendar.timegm(
                        time.strptime(str(row.get("refreshAt"))[:19], "%Y-%m-%dT%H:%M:%S")
                    )
                )
            return {"used_h": used, "remaining_h": left, "total_h": total, "refresh_at": refresh}
    return None


def kaggle_live_quota_enabled() -> bool:
    """Off under pytest (no network from unit tests) unless NBRUN_KAGGLE_QUOTA=1 asks for it;
    NBRUN_KAGGLE_QUOTA=0 turns it off anywhere."""
    flag = os.environ.get("NBRUN_KAGGLE_QUOTA")
    if flag is not None:
        return flag.strip() not in ("0", "", "false", "no")
    return "PYTEST_CURRENT_TEST" not in os.environ


def kaggle_quota(
    token: str,
    timeout: int = 60,
    max_age: float = KAGGLE_QUOTA_TTL_S,
    runner = None,
) -> Optional[Dict[str, object]]:
    """Live weekly GPU quota for `token` (cached per user for `max_age` s, failures too, so a
    broken CLI costs one call per TTL). None = unknown."""
    if not token or not kaggle_live_quota_enabled():
        return None
    cache_path = colab_state_home() / "kaggle_quota.json"
    fp = _token_fingerprint(token)
    now = time.time()
    with contextlib.suppress(OSError, ValueError):
        cached = json.loads(cache_path.read_text(encoding = "utf-8")).get(fp)
        if cached and now - cached.get("t", 0) < max_age:
            return cached.get("quota")
    cli = kaggle_cli()
    quota = None
    if cli:
        env = subprocess_env({"KAGGLE_API_TOKEN": token})
        for k in ("KAGGLE_USERNAME", "KAGGLE_KEY"):
            env.pop(k, None)
        res = (runner or run_capture)([cli, "quota", "--format", "json"], timeout = timeout, env = env)
        if res.returncode == 0:
            quota = parse_kaggle_quota(res.output)
    with contextlib.suppress(OSError, ValueError):
        cache_path.parent.mkdir(parents = True, exist_ok = True)
        try:
            data = json.loads(cache_path.read_text(encoding = "utf-8"))
        except (OSError, ValueError):
            data = {}
        data[fp] = {"t": round(now, 1), "quota": quota}
        tmp = cache_path.with_name(cache_path.name + ".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(data), encoding = "utf-8")
        tmp.replace(cache_path)
    return quota


# ---- Kernel records ----------------------------------------------------------
#
# Every pushed kernel is written to <workdir>/kaggle_kernels.json (the run dir)
# and appended to the per-user index <state home>/kaggle_kernels.jsonl, with
# the owning token's env NAME (never the token), the pid and host of the
# runner, and its state. That is what lets a killed runner's kernel be found,
# collected or deleted later (`--kaggle-sweep`), and what keeps the sweep away
# from kernels this workspace never launched.
KAGGLE_RECORD_NAME = "kaggle_kernels.json"
KAGGLE_TERMINAL = ("COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED", "CANCEL_REQUESTED")


def kaggle_kernel_index_path() -> Path:
    return colab_state_home() / "kaggle_kernels.jsonl"


def record_kaggle_kernel(workdir: Path, kernel_id: str, **fields) -> Dict[str, object]:
    """Merge `fields` into the kernel's record (run dir + per-user index). Best effort."""
    import socket

    path = Path(workdir) / KAGGLE_RECORD_NAME
    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError):
        data = {}
    rec = dict(
        data.get(kernel_id)
        or {
            "kernel_id": kernel_id,
            "workdir": str(workdir),
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }
    )
    rec.update(fields)
    rec["updated"] = round(time.time(), 1)
    data[kernel_id] = rec
    with contextlib.suppress(OSError):
        tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(data, indent = 1), encoding = "utf-8")
        tmp.replace(path)
    with contextlib.suppress(OSError):
        idx = kaggle_kernel_index_path()
        idx.parent.mkdir(parents = True, exist_ok = True)
        with open(idx, "a", encoding = "utf-8") as fh:  # one short line: an atomic append
            fh.write(json.dumps(rec) + "\n")
    return rec


def load_kaggle_index() -> Dict[str, Dict[str, object]]:
    """Latest record per kernel id from the per-user index."""
    out: Dict[str, Dict[str, object]] = {}
    with contextlib.suppress(OSError):
        for line in kaggle_kernel_index_path().read_text(encoding = "utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("kernel_id"):
                    out[rec["kernel_id"]] = dict(out.get(rec["kernel_id"], {}), **rec)
    return out


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def kaggle_record_live(rec: Mapping[str, object]) -> bool:
    """A kernel some live runner on this host is still driving (pushed, not yet terminal)."""
    import socket
    return (
        rec.get("state") in ("pushed", "QUEUED", "RUNNING")
        and rec.get("host") == socket.gethostname()
        and _pid_alive(rec.get("pid"))
    )


@dataclass
class KagglePaths:
    """Every place the Kaggle CLI keeps a credential. Injectable for tests."""

    config: Path  # kaggle.json (legacy username + key)
    tokens: List[Path]  # access_token files (bearer token)
    oauth: Path  # credentials.json written by `kaggle auth login`

    @classmethod
    def discover(cls) -> "KagglePaths":
        directory = kaggle_config_dir()
        legacy = Path.home() / ".kaggle"
        return cls(
            config = directory / "kaggle.json",
            tokens = [
                legacy / "access_token",
                legacy / "access_token.txt",
                directory / "access_token",
                directory / "access_token.txt",
            ],
            oauth = legacy / "credentials.json",
        )


def kaggle_config_dir() -> Path:
    """Mirror the Kaggle CLI's own config-directory rule, on every platform.

    KAGGLE_CONFIG_DIR wins; otherwise ~/.kaggle if it already exists (the
    historical location); otherwise, on Linux only, the XDG directory.
    Getting this wrong means reading a credential the CLI will not use.
    """
    override = os.environ.get("KAGGLE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    legacy = Path.home() / ".kaggle"
    if legacy.exists() or not sys.platform.startswith("linux"):
        return legacy
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "kaggle"


def kaggle_config_path() -> Path:
    """Where the Kaggle CLI looks for kaggle.json."""
    return kaggle_config_dir() / "kaggle.json"


def read_kaggle_json(path: Path) -> Optional[Tuple[str, str]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialError(
            "%s exists but could not be read as JSON (%s).\n"
            "Delete it and re-run, or fix it by hand. It must look like:\n"
            '  {"username": "your-kaggle-username", "key": "your-api-key"}' % (path, exc)
        )
    if not isinstance(data, dict):
        raise CredentialError("%s must contain a JSON object" % path)
    username = str(data.get("username") or "").strip()
    key = str(data.get("key") or "").strip()
    if not username or not key:
        raise CredentialError('%s is missing the "username" and/or "key" field.' % path)
    return username, key


def write_kaggle_json(creds: KaggleCredentials, path: Path) -> bool:
    """Persist credentials privately. Only ever called on the user's own box.

    The file is CREATED at 0600 rather than written and then chmod'd: a plain
    write lands at the umask (0644 on a default box) and leaves the key
    readable by every other local user until the chmod runs.

    Returns True if the file is genuinely restricted, False on platforms
    without POSIX mode bits -- so the caller can avoid claiming otherwise.
    """
    if not creds.username or not creds.key:
        raise CredentialError("only a username + key pair can be written to kaggle.json")
    path.parent.mkdir(parents = True, exist_ok = True)
    payload = json.dumps({"username": creds.username, "key": creds.key}) + "\n"
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding = "utf-8") as handle:
        handle.write(payload)
    if os.name == "nt":
        # Windows ignores the mode; os.chmod there only toggles read-only, so
        # reporting "mode 600" would be a false assurance.
        return False
    try:
        os.chmod(path, 0o600)
        return True
    except OSError:
        warn("could not restrict %s; tighten it yourself if this machine is shared." % path)
        return False


def read_access_token(path: Path) -> Optional[str]:
    """Read a bearer token from a file, or None.

    `is_file()` is inside the `try`: a token longer than NAME_MAX (255 on
    Linux and macOS) makes it raise OSError rather than return False, and
    KAGGLE_API_TOKEN legitimately holds either a token or a path to one.
    """
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding = "utf-8").strip() or None
    except (OSError, ValueError):
        return None


def resolve_kaggle_credentials(
    username: Optional[str] = None,
    key: Optional[str] = None,
    token: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    paths: Optional[KagglePaths] = None,
    interactive: bool = True,
    prompt: Optional[object] = None,
    secret_prompt: Optional[object] = None,
    save: bool = True,
) -> KaggleCredentials:
    """Resolve a Kaggle credential, highest precedence first.

        1. --kaggle-token, or --kaggle-username + --kaggle-key
        2. KAGGLE_API_TOKEN in the environment (a token, or a path to one)
        3. KAGGLE_USERNAME + KAGGLE_KEY in the environment
        4. an access_token file next to the CLI's config
        5. kaggle.json ($KAGGLE_CONFIG_DIR, else ~/.kaggle, else the XDG dir)
        6. credentials.json from `kaggle auth login` -- delegated to the CLI
        7. an interactive prompt, which offers to write kaggle.json at 600

    Everything is injectable (`env`, `paths`, `prompt`, `secret_prompt`) so
    the whole chain is testable offline.
    """
    env = os.environ if env is None else env
    paths = KagglePaths.discover() if paths is None else paths

    if token:
        return KaggleCredentials(
            username = (username or "").strip() or None, token = token.strip(), source = "command line"
        )
    if username and key:
        return KaggleCredentials(username = username.strip(), key = key.strip(), source = "command line")
    if username or key:
        raise CredentialError(
            "--kaggle-username and --kaggle-key must be given together "
            "(or use --kaggle-token on its own)."
        )

    # KAGGLE_API_TOKEN* in the environment. Several accounts are usually set;
    # choose_kaggle_token draws one weighted by weekly allowance, skipping
    # tokens benched after a Kaggle refusal.
    chosen = choose_kaggle_token(env, quota = kaggle_quota if kaggle_live_quota_enabled() else None)
    if chosen:
        env_name, env_token, reason = chosen
        # The CLI accepts either the token itself or a path to a file holding
        # it, so accept both here too rather than sending a filename as a
        # bearer token and reporting the 401 as bad credentials.
        as_path = Path(env_token).expanduser()
        from_file = read_access_token(as_path)
        return KaggleCredentials(
            token = from_file or env_token,
            source = env_name + (" -> %s" % as_path if from_file else "") + "; " + reason,
        )

    env_user = (env.get("KAGGLE_USERNAME") or "").strip()
    env_key = (env.get("KAGGLE_KEY") or "").strip()
    if env_user and env_key:
        return KaggleCredentials(username = env_user, key = env_key, source = "environment")
    if env_user or env_key:
        raise CredentialError(
            "only one of KAGGLE_USERNAME / KAGGLE_KEY is set in the "
            "environment. Set both, or unset both and use %s." % paths.config
        )

    for token_path in paths.tokens:
        if read_access_token(token_path):
            # Do not copy the token around; the CLI reads this file itself.
            return KaggleCredentials(source = str(token_path))

    from_json = read_kaggle_json(paths.config)
    if from_json:
        return KaggleCredentials(username = from_json[0], key = from_json[1], source = str(paths.config))

    if paths.oauth.is_file():
        return KaggleCredentials(source = "%s (kaggle auth login)" % paths.oauth)

    if not interactive:
        raise CredentialError(
            "no Kaggle credentials found, and --non-interactive was given.\n"
            "Provide one of these:\n"
            "  kaggle auth login                     (browser sign-in, no token to manage)\n"
            "  export KAGGLE_API_TOKEN=KGAT_...      (Settings -> API -> Generate New Token)\n"
            "  export KAGGLE_USERNAME=... KAGGLE_KEY=...\n"
            "  --kaggle-token ... | --kaggle-username ... --kaggle-key ...\n"
            "  write %s" % paths.config
        )

    ask = prompt or input
    ask_secret = secret_prompt or getpass.getpass
    log("")
    log("No Kaggle credentials found.")
    log("The easiest fix is a browser sign-in:  kaggle auth login")
    log("Otherwise, take the username and key from")
    log("https://www.kaggle.com/settings -> API -> Create New Token")
    log("(that downloads a kaggle.json holding both values).")
    got_user = str(ask("Kaggle username: ")).strip()
    got_key = str(ask_secret("Kaggle API key (hidden): ")).strip()
    if not got_user or not got_key:
        raise CredentialError("both a username and an API key are required.")

    creds = KaggleCredentials(username = got_user, key = got_key, source = "interactive prompt")
    if save:
        # Ask. Writing somebody's API key to disk without a word is not a
        # decision this script gets to make for them.
        answer = (
            str(ask("Save these to %s so you are not asked again? [y/N] " % paths.config))
            .strip()
            .lower()
        )
        if answer in ("y", "yes"):
            try:
                restricted = write_kaggle_json(creds, paths.config)
                log("Saved %s%s." % (paths.config, " (mode 600)" if restricted else ""))
            except OSError as exc:
                warn("could not save %s: %s" % (paths.config, exc))
        else:
            log("Not saved; these credentials are used for this run only.")
    return creds


@dataclass
class ColabAuthStatus:
    ok: bool
    reason: str = ""
    detail: str = ""
    adc_path: Optional[str] = None
    # "ok" | "missing_cli" | "missing_kernel_client" | "no_credentials" |
    # "rejected". A missing CLI is an infrastructure problem with a completely
    # different fix from a missing or under-scoped credential, so the two must
    # never print the same advice. missing_kernel_client is a third: everything
    # authenticates and allocates, and only `colab exec` fails.
    kind: str = "ok"

    def instructions(self) -> str:
        if self.kind == "missing_cli":
            return (
                "Install the Colab CLI:\n\n"
                "  uv tool install google-colab-cli     (or: pip install "
                "google-colab-cli)\n\n"
                "It supports Linux and macOS only. On Windows, use "
                "--backend kaggle, or run this from WSL."
            )
        if self.kind == "missing_kernel_client":
            return (
                "The Colab CLI needs Google's fork of jupyter-kernel-client, not\n"
                "the PyPI package of the same name -- see the git pin in its\n"
                "pyproject.toml. With the PyPI one installed, auth and allocation\n"
                "both succeed and `colab exec` then dies with\n"
                "  AttributeError: module 'jupyter_kernel_client' has no attribute\n"
                "  'KernelClient' (or 'JupyterSubprotocol')\n"
                "which looks like a broken notebook rather than a broken install.\n"
                "Automatic repair failed; install it into the CLI's own environment:\n\n"
                "  uv pip install --python %s \\\n"
                "    'jupyter-kernel-client @ git+https://github.com/"
                "googlecolab/jupyter-kernel-client.git'" % colab_cli_python()
            )
        return (
            "Colab needs credentials, by either route.\n\n"
            "1. No gcloud on this machine (headless hosts, containers, CI). Use\n"
            "   the CLI's own remote flow: it prints a URL, you approve in any\n"
            "   browser, you paste the code back. Pass --colab-auth oauth2 to\n"
            "   this script afterwards so every call uses the same provider:\n\n"
            "     %s\n\n"
            "2. Application Default Credentials, all four scopes. Opens a\n"
            "   browser, so this script deliberately does not drive it:\n\n"
            "%s\n\n"
            "Why all four: without userinfo.email the session backend 401s;\n"
            "without colaboratory the keep-alive RPC 403s; openid and\n"
            "cloud-platform are mandated by gcloud itself.\n"
            "Then confirm with:  colab sessions" % (OAUTH2_LOGIN_COMMAND, GCLOUD_LOGIN_COMMAND)
        )


def oauth2_token_path() -> Path:
    """Where the CLI's own oauth2 provider caches its token."""
    base = os.environ.get("XDG_CONFIG_HOME")
    account = colab_account_home()
    root = (
        Path(account) / ".config"
        if account
        else Path(base).expanduser()
        if base
        else Path.home() / ".config"
    )
    return root.joinpath(*OAUTH2_TOKEN_RELPATH)


KERNEL_CLIENT_FORK = "git+https://github.com/googlecolab/jupyter-kernel-client.git"
# colab_cli/runtime.py uses both at module top level: jupyter_kernel_client.KernelClient and
# .JupyterSubprotocol. The PyPI package lacks KernelClient (1.0.2) or JupyterSubprotocol (older).
_KERNEL_CLIENT_PROBE = (
    "import jupyter_kernel_client as j, sys; "
    "sys.exit(0 if hasattr(j, 'KernelClient') and hasattr(j, 'JupyterSubprotocol') else 3)"
)


def colab_cli() -> Optional[str]:
    """The `colab` binary to drive: $COLAB_CLI if set, else `colab` on PATH, else the uv tool bin dir.

    The override lets a run use a CLI other than the one on PATH (a newer
    release in a venv, a git checkout) without touching the PATH itself.
    """
    override = os.environ.get("COLAB_CLI")
    if override:
        return shutil.which(os.path.expanduser(override))
    return tool_path("colab")


def tools_record() -> Path:
    """Per-user {tool: absolute path} written by launcher.sh / cloud_pool.py record-tools."""
    return (
        Path(os.environ.get("CLOUD_POOL_USER_DIR") or Path.home() / ".config" / "switchboard")
        / "tools.json"
    )


def tool_path(name: str) -> Optional[str]:
    """`name` on PATH, else the uv tool bin dir, else the path recorded from the user's login shell.
    A session's PATH can lack what the user's shell has (~/.local/bin, a venv): sessions reported
    "the Colab CLI isn't installed" while the shell ran it fine."""
    found = shutil.which(name)
    if found:
        return found
    cands = [
        os.path.join(d, name)
        for d in (os.environ.get("UV_TOOL_BIN_DIR"), os.path.expanduser("~/.local/bin"))
        if d
    ]
    with contextlib.suppress(OSError, ValueError, AttributeError):
        rec = json.loads(tools_record().read_text(encoding = "utf-8")).get(name)
        if rec:
            cands.append(rec)
    return next((c for c in cands if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def record_tools(names = ("colab", "kaggle")) -> dict:
    """Write where this process finds each tool (only found ones; keeps others' old entries)."""
    path = tools_record()
    try:
        cur = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError):
        cur = {}
    for n in names:
        found = shutil.which(n)
        if found:
            cur[n] = os.path.abspath(found)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps(cur, indent = 1, sort_keys = True), encoding = "utf-8")
    return cur


def kaggle_cli() -> Optional[str]:
    return tool_path("kaggle")


# The CLI version to upgrade to for `colab new --high-mem`. The flag first
# shipped in 0.7.2 (0.6.0 has none, 0.7.3 was yanked); 0.7.4 is the release
# this was verified against on live A100 sessions.
COLAB_CLI_HIGH_MEM_MIN = "0.7.4"


def colab_cli_version(cli: Optional[str]) -> Optional[str]:
    """`colab version` of the given binary, or None when it cannot be read."""
    if not cli:
        return None
    probe = run_capture([cli, "version"], timeout = 60)
    found = re.search(r"Version:\s*(\S+)", probe.output or "")
    return found.group(1) if found else None


def colab_cli_python():
    """The interpreter the `colab` CLI runs under (its shebang: uv tool / pipx env), else ours."""
    cli = colab_cli()
    try:
        with open(cli, "rb") as f:
            first = f.readline().decode(errors = "replace").strip()
        py = first[2:].split()[0] if first.startswith("#!") else ""
        if py and os.path.basename(py).startswith("python") and os.access(py, os.X_OK):
            return py
    except (TypeError, OSError):
        pass
    return sys.executable


def kernel_client_ok() -> bool:
    """True if the jupyter-kernel-client in the CLI's OWN environment is the fork `colab exec` needs.

    Checked separately from auth because the failure lands much later, after a
    VM has been paid for and allocated, and reads like a notebook error. Probed
    in the CLI's interpreter, not ours: a uv tool / pipx install has its own env.
    """
    try:
        return (
            subprocess.run(
                [colab_cli_python(), "-c", _KERNEL_CLIENT_PROBE],  # noqa: S603
                capture_output = True,
                timeout = 60,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def repair_kernel_client() -> bool:
    """Install the fork into the CLI's own environment (uv, else pip); True if the probe then passes."""
    py = colab_cli_python()
    cmds = []
    if shutil.which("uv"):
        cmds.append(
            [
                "uv",
                "pip",
                "install",
                "-q",
                "--python",
                py,
                f"jupyter-kernel-client @ {KERNEL_CLIENT_FORK}",
            ]
        )
    cmds.append([py, "-m", "pip", "install", "-q", f"jupyter-kernel-client @ {KERNEL_CLIENT_FORK}"])
    for cmd in cmds:
        try:
            if (
                subprocess.run(cmd, capture_output = True, timeout = 600).returncode == 0
                and kernel_client_ok()
            ):  # noqa: S603
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def adc_path() -> Path:
    """Application Default Credentials location, on every platform."""
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return Path(explicit).expanduser()
    config_dir = os.environ.get("CLOUDSDK_CONFIG")
    if config_dir:
        return Path(config_dir).expanduser() / "application_default_credentials.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "gcloud" / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def check_colab_auth(timeout: int = 120, provider: str = "adc") -> ColabAuthStatus:
    """Detect whether the Colab CLI is installed and actually authenticated.

    Authentication is checked behaviourally. `colab sessions` is read-only,
    costs nothing, and is the only thing that proves the token carries the
    scopes the backend wants -- a credential file can exist and still be
    missing the `colaboratory` scope. The ADC path is stat'd purely so that
    "you never logged in" and "you logged in without the right scope" are two
    different messages.
    """
    cli = colab_cli()
    adc = oauth2_token_path() if provider == "oauth2" else adc_path()
    if not cli:
        return ColabAuthStatus(
            False,
            "the `colab` CLI is not on PATH (or $COLAB_CLI is not an executable)",
            "",
            str(adc) if adc.exists() else None,
            kind = "missing_cli",
        )
    try:
        proc = subprocess.run(  # noqa: S603
            [cli] + (["--auth", provider] if provider != "adc" else []) + ["sessions"],
            capture_output = True,
            text = True,
            timeout = timeout,
            env = _default_env([cli]),
        )
    except subprocess.TimeoutExpired:
        return ColabAuthStatus(
            False, "`colab sessions` timed out after %ds" % timeout, kind = "rejected"
        )
    except OSError as exc:
        return ColabAuthStatus(
            False, "could not run `colab sessions`: %s" % exc, kind = "missing_cli"
        )

    output = strip_ansi(((proc.stdout or "") + (proc.stderr or "")).strip())
    if proc.returncode == 0:
        if not kernel_client_ok() and not repair_kernel_client():
            return ColabAuthStatus(
                False,
                "the installed jupyter-kernel-client is not the fork the "
                "Colab CLI pins, so `colab exec` will fail after a VM is "
                "allocated",
                output[:400],
                str(adc) if adc.exists() else None,
                kind = "missing_kernel_client",
            )
        return ColabAuthStatus(True, "", output[:400], str(adc) if adc.exists() else None)
    if not adc.exists():
        return ColabAuthStatus(
            False,
            "no Application Default Credentials at %s" % adc,
            output[:800],
            None,
            kind = "no_credentials",
        )
    return ColabAuthStatus(
        False,
        "credentials exist at %s but `colab sessions` was rejected, which is "
        "almost always a missing scope" % adc,
        output[:800],
        str(adc),
        kind = "rejected",
    )


_KAGGLE_USERNAME_RE = re.compile(r"^\s*[-*]?\s*username\s*:\s*(\S+)\s*$", re.MULTILINE)


def check_kaggle_auth(
    creds: KaggleCredentials, timeout: int = 120
) -> Tuple[bool, str, Optional[str]]:
    """Validate a Kaggle credential and discover the account it belongs to.

    Two cheap calls, because neither one alone is enough:

    * `kaggle config view` reports the username, which we need for the kernel
      id, but it does NOT prove a legacy username/key pair is valid -- that
      path just checks the two values are present.
    * `kaggle kernels list --mine` is a real authenticated request, but a
      brand-new account with no kernels returns nothing to read a name out of.

    Returns (ok, detail, username).
    """
    cli = kaggle_cli()
    if not cli:
        return (
            False,
            (
                "the `kaggle` CLI is not installed for this user. Install it with:  "
                "uv tool install kaggle  (launcher.sh does it on launch)"
            ),
            None,
        )

    env = subprocess_env(creds.env())

    def _run(args: List[str]) -> Tuple[int, str]:
        try:
            proc = subprocess.run(  # noqa: S603
                [cli] + args, capture_output = True, text = True, timeout = timeout, env = env
            )
        except subprocess.TimeoutExpired:
            return -1, "`kaggle %s` timed out after %ds" % (" ".join(args), timeout)
        except OSError as exc:
            return -1, "could not run the kaggle CLI: %s" % exc
        return proc.returncode, strip_ansi(((proc.stdout or "") + (proc.stderr or "")).strip())

    _, config_view = _run(["config", "view"])
    match = _KAGGLE_USERNAME_RE.search(config_view or "")
    username = match.group(1) if match and match.group(1) != "None" else None
    username = username or creds.username

    code, listing = _run(["kernels", "list", "--mine", "--page-size", "1"])
    if code == 0:
        if not username:
            return (
                False,
                (
                    "Kaggle accepted the credential but never reported a "
                    "username, so there is no account to file the kernel under. "
                    "Pass --kaggle-username explicitly."
                ),
                None,
            )
        return True, (listing or config_view)[:400], username

    if classify_platform_error(listing) == "auth":
        return (
            False,
            (
                "Kaggle rejected the credential from %s%s.\n"
                "Re-create it at https://www.kaggle.com/settings -> API, or run "
                "`kaggle auth login`.\nKaggle said: %s"
                % (
                    creds.source or "the kaggle CLI's own configuration",
                    " for user %r" % username if username else "",
                    listing[:400],
                )
            ),
            username,
        )
    return False, "the kaggle CLI failed: %s" % listing[:400], username


def colab_account_home() -> Optional[str]:
    """$NBRUN_COLAB_HOME: a directory used as HOME for every `colab` CLI call.

    The CLI keeps its token, settings and sessions under ~ (colab_cli/auth.py:
    expanduser("~/.config/colab-cli/token.json")), so pointing HOME at one
    directory per Google account is how cloud_pool.py drives two accounts from
    one unix user. Only the colab CLI sees it; kaggle and everything else keep
    the real HOME."""
    home = (os.environ.get("NBRUN_COLAB_HOME") or "").strip()
    return os.path.expanduser(home) if home else None


def _is_colab_cmd(cmd: Sequence[str]) -> bool:
    if not cmd:
        return False
    cli = colab_cli()
    first = str(cmd[0])
    return (
        bool(cli and os.path.realpath(first) == os.path.realpath(cli))
        or os.path.basename(first) == "colab"
    )


def _default_env(cmd: Sequence[str]) -> Dict[str, str]:
    home = colab_account_home()
    if home and _is_colab_cmd(cmd):
        return subprocess_env({"HOME": home})
    return subprocess_env()


def subprocess_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(extra or {})
    # Keep child CLIs from buffering or colouring output we are going to parse.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("NO_COLOR", "1")
    return env


# --------------------------------------------------------------------------
# Planning (what --dry-run prints, and what the backends consume)
# --------------------------------------------------------------------------


@dataclass
class PlannedNotebook:
    source: NotebookSource
    output_name: str


@dataclass
class Plan:
    backend: str
    gpu: str
    remote_accelerator: str
    notebooks: List[PlannedNotebook]
    max_steps: int
    grpo_max_steps: int
    per_cell_timeout: int
    wall_timeout: int
    workdir: str
    extra_env: Dict[str, str] = field(default_factory = dict)
    smoke_patch: bool = True
    keep_session: bool = False
    high_mem: bool = False  # colab: machine_shape=hm
    reuse_session: bool = False  # colab: one VM for every notebook
    session_name: Optional[str] = None  # colab: attach to this VM by name
    pack: int = 1  # kaggle: notebooks per kernel
    parallel_gpus: int = 1  # kaggle: packed notebooks run at once, one GPU each
    connect_retries: int = DEFAULT_CONNECT_RETRIES  # colab: fresh VMs after a lost connection
    alloc_wait: int = DEFAULT_ALLOC_WAIT  # colab: seconds to retry a capacity refusal
    idle_timeout: int = (
        DEFAULT_IDLE_TIMEOUT  # colab: seconds with no output before the run is killed
    )

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "gpu": self.gpu,
            "remote_accelerator": self.remote_accelerator,
            "high_mem": self.high_mem,
            "notebooks": [
                {
                    "spec": n.source.spec,
                    "kind": n.source.kind,
                    "location": n.source.location,
                    "fallbacks": n.source.fallbacks,
                    "name": n.source.name,
                    "output_name": n.output_name,
                }
                for n in self.notebooks
            ],
            "max_steps": self.max_steps,
            "grpo_max_steps": self.grpo_max_steps,
            "per_cell_timeout": self.per_cell_timeout,
            "wall_timeout": self.wall_timeout,
            "idle_timeout": self.idle_timeout,
            "connect_retries": self.connect_retries,
            "alloc_wait": self.alloc_wait,
            "workdir": self.workdir,
            # Keys only. --env is the documented way to pass something like
            # HF_TOKEN, and report.json is an ordinary file in a shared temp
            # directory.
            "extra_env_keys": sorted(self.extra_env),
            "smoke_patch": self.smoke_patch,
            "keep_session": self.keep_session,
            "reuse_session": self.reuse_session,
            "session_name": self.session_name,
            "pack": self.pack,
            "parallel_gpus": self.parallel_gpus,
        }


def normalise_gpu(backend: str, gpu: str) -> Tuple[str, str]:
    """Validate a GPU tier locally and map it to the remote identifier.

    Local validation is not optional on Colab: an unrecognised `--gpu` value
    is not rejected by the CLI, it silently falls back to A100 and then fails
    at allocation, which reads like a capacity problem instead of a typo.
    """
    want = (gpu or "").strip()
    if backend == "colab":
        for candidate in COLAB_GPUS:
            if candidate.lower() == want.lower():
                return candidate, candidate
        key = re.sub(r"[-_ ]", "", want).upper()
        if key in {re.sub(r"[-_ ]", "", a).upper() for a in COLAB_HIGHMEM_ONLY}:
            base = COLAB_GPU_ALIASES[
                next(a for a in COLAB_GPU_ALIASES if re.sub(r"[-_ ]", "", a).upper() == key)
            ]
            return base, base
        raise UsageError(
            "unknown Colab GPU tier %r. Valid tiers: %s\n"
            "Add -HighRAM to any of them (T4-HighRAM, A100-HM = the 80GB "
            "A100) for the high-RAM machine shape; needs google-colab-cli "
            ">= %s (`colab new --high-mem`).\n"
            "Availability depends on your Colab subscription; CPU always works."
            % (gpu, ", ".join(COLAB_GPUS), COLAB_CLI_HIGH_MEM_MIN)
        )
    if backend == "kaggle":
        key = re.sub(r"[-_ ]", "", want).upper()
        if key in KAGGLE_GPUS:
            return KAGGLE_GPUS[key]
        raise UsageError("unknown Kaggle accelerator %r. Valid tiers: T4x2, P100, TPU, CPU" % gpu)
    raise UsageError("unknown backend %r" % backend)


def parse_env_pairs(pairs: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise InfraError("--env expects KEY=VALUE, got %r" % pair)
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise InfraError("--env expects KEY=VALUE, got %r" % pair)
        out[key] = value
    return out


def build_plan(args: argparse.Namespace, workdir: Path) -> Plan:
    gpu, remote = normalise_gpu(args.backend, args.gpu or default_gpu(args.backend))
    if args.max_steps < 1:
        raise InfraError("--max-steps must be at least 1")
    if args.grpo_max_steps < 1:
        raise InfraError("--grpo-max-steps must be at least 1")
    if args.per_cell_timeout < 1 or args.wall_timeout < 1:
        raise InfraError("timeouts must be positive")

    planned: List[PlannedNotebook] = []
    seen = set()
    for spec in args.notebooks:
        source = resolve_notebook(spec)
        stem = Path(source.name).stem
        out = stem
        counter = 2
        while out in seen:
            out = "%s-%d" % (stem, counter)
            counter += 1
        seen.add(out)
        planned.append(PlannedNotebook(source = source, output_name = out))

    return Plan(
        backend = args.backend,
        gpu = gpu,
        remote_accelerator = remote,
        high_mem = (args.backend == "colab" and wants_high_mem(args.gpu or "")),
        notebooks = planned,
        max_steps = args.max_steps,
        grpo_max_steps = min(args.grpo_max_steps, args.max_steps),
        per_cell_timeout = args.per_cell_timeout,
        wall_timeout = args.wall_timeout,
        connect_retries = max(0, args.connect_retries),
        alloc_wait = max(0, args.alloc_wait),
        idle_timeout = max(0, getattr(args, "idle_timeout", DEFAULT_IDLE_TIMEOUT)),
        workdir = str(workdir),
        extra_env = parse_env_pairs(args.env or []),
        smoke_patch = not args.no_smoke_patch,
        session_name = (args.session or None) if args.backend == "colab" else None,
        # A named session is kept by definition: stopping it at the end would
        # leave nothing to attach to next time, which is the whole feature.
        keep_session = args.keep_session or bool(args.session),
        # Keeping a VM only makes sense if there is one VM to keep; otherwise
        # --keep-session on five notebooks would strand five billable VMs.
        reuse_session = (args.reuse_session or args.keep_session or bool(args.session)),
        pack = max(1, args.pack, getattr(args, "parallel_gpus", 1) or 1),
        # A parallel kernel runs `parallel_gpus` notebooks at once, so it needs at least that many packed.
        parallel_gpus = max(1, getattr(args, "parallel_gpus", 1) or 1),
    )


def render_plan(plan: Plan) -> str:
    lines = [
        "Plan (dry run: nothing was submitted, no machine was rented)",
        "  backend            %s" % plan.backend,
        "  GPU tier           %s  (sent to the platform as %r)"
        % (plan.gpu, plan.remote_accelerator),
        "  max_steps          %s"
        % (
            "SMOKE PATCH DISABLED -- the notebook runs exactly as written"
            if not plan.smoke_patch
            else "%d  (GRPO cells: %d)" % (plan.max_steps, plan.grpo_max_steps)
        ),
        "  per-cell timeout   %s" % human_duration(plan.per_cell_timeout),
        "  wall timeout       %s  (per notebook, enforced independently of the "
        "per-cell one)" % human_duration(plan.wall_timeout),
        "  idle timeout       %s"
        % (
            (human_duration(plan.idle_timeout) + "  (no output at all -> killed)")
            if plan.idle_timeout and plan.backend == "colab"
            else "off"
        ),
        "  work directory     %s" % plan.workdir,
        "  session            %s"
        % (
            "%r: attach if it is alive, else create it under that name" % plan.session_name
            if plan.session_name
            else "a fresh one, named at random"
        ),
        "  session teardown   %s"
        % (
            "KEPT (--session): the next run attaches to it, and it bills "
            "until you stop it yourself"
            if plan.session_name
            else "KEPT (--keep-session): it bills until you stop it yourself"
            if plan.keep_session
            else "always, from finally + atexit + signal handlers"
        ),
        "  isolation          %s"
        % (
            (
                "one VM shared by all %d notebooks (--reuse-session): each one "
                "inherits the previous one's installed packages" % len(plan.notebooks)
            )
            if plan.backend == "colab" and plan.reuse_session
            else "a fresh VM per notebook"
            if plan.backend == "colab"
            else (
                "%d notebook(s) per kernel, sharing one site-packages%s"
                % (
                    plan.pack,
                    ", %d at a time (one GPU each)" % plan.parallel_gpus
                    if plan.parallel_gpus > 1
                    else "",
                )
            )
            if plan.pack > 1
            else "one kernel per notebook"
        ),
    ]
    if plan.extra_env:
        lines.append("  extra environment  %s  (values hidden)" % ", ".join(sorted(plan.extra_env)))
    lines.append("  notebooks          %d" % len(plan.notebooks))
    for item in plan.notebooks:
        lines.append("    - %s" % item.source.spec)
        lines.append("        fetch    %s" % item.source.describe())
        for fallback in item.source.fallbacks:
            lines.append("        or       %s" % fallback)
        lines.append(
            "        result   %s%s%s.executed.ipynb" % (plan.workdir, os.sep, item.output_name)
        )
    lines.append("")
    lines.append("Would then run:")
    lines.append(_plan_actions(plan))
    return "\n".join(lines)


def _plan_actions(plan: Plan) -> str:
    count = len(plan.notebooks)
    if plan.backend == "colab":
        gpu_flag = "" if plan.remote_accelerator == "CPU" else " --gpu %s" % plan.remote_accelerator
        if plan.high_mem:
            gpu_flag += " --high-mem"
        scope = (
            ("once, for all %d notebooks, with a kernel restart between each" % count)
            if plan.reuse_session
            else ("%d time(s), once per notebook" % count)
        )
        return (
            "  colab new -s unsloth-nbrun-<id>%s        [%s]\n"
            "  colab exec -s unsloth-nbrun-<id> -f <patched>.ipynb --timeout %d\n"
            "  parse <patched>_output.ipynb for the verdict  "
            "(the exit code is recorded, never believed)\n"
            "  colab stop -s unsloth-nbrun-<id>  (always, even on Ctrl-C)"
            % (gpu_flag, scope, plan.per_cell_timeout)
        )
    accel = (
        "  (no accelerator)"
        if not plan.remote_accelerator
        else " --accelerator %s" % plan.remote_accelerator
    )
    kernels = (count + plan.pack - 1) // max(1, plan.pack)
    return (
        "  kaggle kernels push -p <package> -t %d%s        "
        "[%d kernel(s) for %d notebook(s)]\n"
        "  poll `kaggle kernels status` until COMPLETE / ERROR\n"
        "  kaggle kernels output -> download the executed notebooks\n"
        "  parse each executed notebook for the verdict"
        % (plan.wall_timeout, accel, kernels, count)
    )


# --------------------------------------------------------------------------
# Running a subprocess with a wall deadline and live output
# --------------------------------------------------------------------------


@dataclass
class CommandResult:
    returncode: int
    output: str
    timed_out: bool
    duration: float
    aborted_on: Optional[str] = None  # the abort pattern that ended the run early
    killed_by: Optional[str] = None  # "wall", "cell" or "idle": which local watchdog fired
    last_cell: Optional[str] = None  # "7/8": the cell running when it fired


# Every streaming child still running. A signal handler kills these BEFORE it stops the VM: a
# `colab exec` left running re-adds its session to the state file from its own `finally` after
# `colab stop` removed it, and keeps driving a kept (--session) VM with nobody reading its output.
_ACTIVE_PROCS: "set" = set()
_ACTIVE_LOCK = threading.Lock()


def kill_active_children() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        if proc.poll() is None:
            terminate_tree(proc, first_signal = getattr(signal, "SIGINT", None))


def run_streaming(
    cmd: List[str],
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    prefix: str = "  | ",
    quiet: bool = False,
    tail_lines: int = 500,
    abort_on: Sequence[str] = (),
    idle_timeout: int = 0,
    cell_timeout: int = 0,
    cell_marker: Optional["re.Pattern"] = None,
    poll_seconds: float = 1.0,
) -> CommandResult:
    """Run a command, echo its output live, and enforce a wall deadline.

    The deadline is the whole point of this helper. `colab exec --timeout`
    bounds a single CELL in theory and nothing in practice (see
    COLAB_CELL_MARKER); without an independent clock on the process, a
    twenty-cell notebook can hold a GPU for hours. Three watchdogs, all local:

      wall          `timeout` seconds for the whole command;
      cell          `cell_timeout` seconds since the last `cell_marker` line;
      idle          `idle_timeout` seconds since ANY output line (a stalled
                    websocket or output transport prints nothing at all).

    0 disables cell / idle. Whichever fires is in `killed_by`, and the child is
    killed (SIGINT first, so `colab exec` can still save what ran) on every
    exit path, exceptions and signals included.
    """
    started = time.time()
    tail: List[str] = []
    popen_kwargs = dict(
        stdout = subprocess.PIPE,
        stderr = subprocess.STDOUT,
        universal_newlines = True,
        bufsize = 1,
        env = env or _default_env(cmd),
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # so we can kill the tree

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603
    except OSError as exc:
        raise InfraError("could not run %s: %s" % (cmd[0], exc))
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)

    state: Dict[str, object] = {
        "killed_by": None,
        "aborted_on": None,
        "cell": None,
        "last_line": time.monotonic(),
        "cell_started": None,
    }
    begun = time.monotonic()
    finished = threading.Event()

    def _watch() -> None:
        while not finished.wait(poll_seconds):
            # Racing the normal exit: the watchdog can wake between the last
            # line of output and the child's exit, and flagging that as a
            # timeout turns a clean PASS into a TIMEOUT.
            if proc.poll() is not None or state["aborted_on"]:
                return
            now = time.monotonic()
            why = None
            if now - begun >= timeout:
                why = "wall"
            elif (
                cell_timeout
                and state["cell_started"] is not None
                and now - float(state["cell_started"]) >= cell_timeout
            ):  # type: ignore[arg-type]
                why = "cell"
            elif idle_timeout and now - float(state["last_line"]) >= idle_timeout:  # type: ignore[arg-type]
                why = "idle"
            if why:
                if proc.poll() is not None:
                    return
                state["killed_by"] = why
                terminate_tree(proc, first_signal = getattr(signal, "SIGINT", None))
                return

    watcher = threading.Thread(target = _watch, name = "run_streaming-watchdog", daemon = True)
    watcher.start()
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                state["last_line"] = time.monotonic()
                line = strip_ansi(line.rstrip("\n"))
                tail.append(line)
                if len(tail) > tail_lines:
                    del tail[0]
                if not quiet and not _QUIET:
                    _safe_print(prefix + line)
                if cell_marker is not None:
                    found = cell_marker.search(line)
                    if found:
                        state["cell"] = (
                            "%s/%s" % found.groups()[:2]
                            if len(found.groups()) >= 2
                            else found.group(0)
                        )
                        state["cell_started"] = time.monotonic()
                hit = next((p for p in abort_on if p in line), None)
                if hit and state["aborted_on"] is None:
                    state["aborted_on"] = hit
                    terminate_tree(proc)
        proc.wait()
    finally:
        finished.set()
        # Any way out of here -- exception, KeyboardInterrupt, SystemExit from
        # a signal handler -- must not leave the child running on its own.
        if proc.poll() is None:
            terminate_tree(proc, first_signal = getattr(signal, "SIGINT", None))
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS.discard(proc)

    killed_by = state["killed_by"] if state["aborted_on"] is None else None
    return CommandResult(
        returncode = proc.returncode if proc.returncode is not None else -1,
        output = "\n".join(tail),
        timed_out = killed_by is not None,
        duration = time.time() - started,
        aborted_on = state["aborted_on"],  # type: ignore[arg-type]
        killed_by = killed_by,  # type: ignore[arg-type]
        last_cell = state["cell"],  # type: ignore[arg-type]
    )


def run_capture(
    cmd: List[str],
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    stdin_text: Optional[str] = None,
) -> CommandResult:
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output = True,
            text = True,
            timeout = timeout,
            input = stdin_text,
            env = env or _default_env(cmd),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(-1, "timed out after %ds" % timeout, True, time.time() - started)
    except OSError as exc:
        raise InfraError("could not run %s: %s" % (cmd[0], exc))
    return CommandResult(
        proc.returncode,
        strip_ansi(((proc.stdout or "") + (proc.stderr or "")).strip()),
        False,
        time.time() - started,
    )


def _ignore_interrupts() -> Dict[int, object]:
    """Ignore SIGINT/SIGTERM, returning the handlers to restore afterwards."""
    previous: Dict[int, object] = {}
    # SIGHUP too: the launching shell dying sends it, and a handler that
    # unwinds an in-flight `colab stop` kills that stop half-way.
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    return previous


def _restore_interrupts(previous: Dict[int, object]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (ValueError, OSError, TypeError):
            pass


def terminate_tree(
    proc: "subprocess.Popen",
    first_signal: Optional[int] = None,
    grace: float = KILL_GRACE_SECONDS,
) -> None:
    """Kill a child and everything it spawned, on POSIX and on Windows.

    `first_signal` (SIGINT for `colab exec`) is sent first and given `grace`
    seconds: the CLI's `finally` then still writes the partially executed
    notebook, which says which cell hung. SIGTERM, then SIGKILL, follow."""
    try:
        if os.name != "nt" and first_signal is not None:
            try:
                os.killpg(os.getpgid(proc.pid), first_signal)
                proc.wait(timeout = grace)
                return
            except subprocess.TimeoutExpired:
                pass
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout = 15)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    name: str
    spec: str
    verdict: Verdict
    duration: float = 0.0
    executed_path: Optional[str] = None
    log_path: Optional[str] = None
    remote_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "spec": self.spec,
            "status": self.verdict.status,
            "summary": self.verdict.summary(),
            "code_cells": self.verdict.code_cells,
            "executed_cells": self.verdict.executed_cells,
            # json.dumps writes a bare NaN token, which strict parsers reject.
            "training_losses": [
                x if math.isfinite(x) else str(x) for x in self.verdict.training_losses
            ],
            "failures": [
                {
                    "code_cell": f.index,
                    "ename": f.ename,
                    "evalue": f.evalue,
                    "out_of_memory": f.is_oom,
                }
                for f in self.verdict.failures
            ],
            "duration_seconds": round(self.duration, 1),
            "executed_notebook": self.executed_path,
            "log": self.log_path,
            "remote": self.remote_ref,
        }


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def unrunnable(item: PlannedNotebook, exc: Exception) -> RunResult:
    """A result row for a notebook the platform never got to run.

    NO_RESULT, never a failure verdict: we learned nothing about this
    notebook, and saying otherwise would invent a regression out of a quota
    wall.
    """
    warn("%s could not be run: %s" % (item.source.spec, exc))
    return RunResult(
        item.output_name,
        item.source.spec,
        Verdict(
            NO_RESULT,
            0,
            0,
            [],
            "never ran -- %s: %s" % (type(exc).__name__, str(exc).strip().splitlines()[0][:200]),
        ),
    )


class Backend:
    name = "base"

    def preflight(self) -> None:
        raise NotImplementedError

    def run(self, plan: Plan, prepared: List[Tuple[PlannedNotebook, Path]]) -> List[RunResult]:
        raise NotImplementedError

    def release(self, reason: str) -> None:
        """Idempotent teardown. Called from finally, atexit and signal handlers."""


class ColabBackend(Backend):
    """Rent a Colab VM, execute notebooks on it, and always give it back."""

    name = "colab"

    def __init__(
        self,
        workdir: Path,
        keep_session: bool = False,
        auth_provider: str = "adc",
        session_name: Optional[str] = None,
    ) -> None:
        self.workdir = workdir
        self.keep_session = keep_session
        self.auth_provider = auth_provider
        self.session_name = session_name
        self.cli = colab_cli()
        self.session: Optional[str] = None
        self._released = False
        if session_name:
            # A named session has to outlive the run that made it, so its
            # state cannot live in the run's own directory. This is the
            # second half of reuse and it is easy to miss: a stable NAME with
            # a per-run --config still cannot attach, because the token and
            # endpoint the CLI needs were written somewhere the next run does
            # not look. Kept out of the CLI's default sessions.json so a
            # sweep cannot list or stop VMs a human started by hand.
            self.state_file = colab_state_home() / (
                "session-%s.json" % _safe_filename(session_name)
            )
            self.state_file.parent.mkdir(parents = True, exist_ok = True)
        else:
            # Isolate session state so a parallel run on the same box cannot see
            # or stop our VM, and we cannot stop theirs.
            self.state_file = workdir / "colab-session-state.json"

    def _base(self) -> List[str]:
        # `--config` and `--auth` are GLOBAL flags: both come before the subcommand.
        base = [self.cli or "colab", "--config", str(self.state_file)]
        if getattr(self, "auth_provider", "adc") != "adc":
            base += ["--auth", self.auth_provider]
        return base

    def preflight(self) -> None:
        status = check_colab_auth(provider = self.auth_provider)
        if status.ok:
            return
        message = "Colab is not usable: %s\n\n%s%s" % (
            status.reason,
            status.instructions(),
            ("\n\nThe CLI said:\n  %s" % status.detail) if status.detail else "",
        )
        # A missing tool is infrastructure (exit 3); a missing or under-scoped
        # credential is a credential problem (exit 2).
        if status.kind == "missing_cli":
            raise InfraError(message)
        raise CredentialError(message)

    def _supports_high_mem(self) -> bool:
        """Whether the installed CLI has --high-mem.

        Asked of the binary rather than assumed from a version, because the flag
        landed on main before it landed in a release: pypi 0.6.0 does not have
        it, 0.7.4 does, and a git checkout of 0.6.0 may. Getting this wrong is
        not a crash, it is a silently STANDARD VM, which is the failure this
        whole tier-naming exercise exists to prevent.
        """
        if getattr(self, "_high_mem_cached", None) is None:
            probe = run_capture(self._base() + ["new", "--help"], timeout = 60)
            self._high_mem_cached = "--high-mem" in (probe.output or "")
        return self._high_mem_cached

    def _attached(self, plan: Plan) -> Optional[str]:
        """The named session, if it is alive and usable. None to allocate.

        `colab status -s NAME` is the check rather than `colab sessions`,
        because sessions lists the account's server-side assignments and we
        need the one this state file can actually drive: an assignment with
        no local token shows up there as `[?]` and cannot be exec'd against.

        Deliberately conservative. Anything other than a clear healthy answer
        returns None and we allocate, because a wrong "yes" here sends every
        notebook at a dead VM and reports it as a notebook failure, while a
        wrong "no" costs one allocation. A session that IS alive but has the
        wrong hardware or shape also returns None, with the reason in
        self._mismatch, and _allocate refuses rather than allocating over it.
        """
        name = self.session_name
        if not name:
            return None
        probe = run_capture(self._base() + ["status", "-s", name], timeout = 120)
        text = probe.output or ""
        # MEASURED, and it is this script's own founding lesson turned on the
        # command it depends on: `colab status -s missing` prints
        #     [colab] Session 'unsloth-sweep' not found.
        # and EXITS 0, with the requested name inside the message. So a
        # returncode test passes, a substring test passes, and the only thing
        # that separates alive from dead is the shape of a real status line:
        #     [name] endpoint | Hardware: X | Shape: Y | Variant: Z
        # Attach only on that positive signal. Checked as an affirmative
        # rather than as the absence of "not found", because a message this
        # script does not know is then read as dead, which costs an
        # allocation, while the reverse sends every notebook at a VM that is
        # not there and reports it as the notebook's fault.
        if probe.returncode != 0:
            return None
        if not re.search(r"\[%s\]\s*\S" % re.escape(name), text):
            return None
        got = _reported_hardware(text)
        if not got:
            return None
        # The hardware is in the status line. Attaching to a T4 when the
        # caller asked for an A100 would silently measure the wrong machine,
        # which is the same class of bug as an unrecognised tier falling back
        # to A100, and a benchmark cannot detect it from the inside.
        want = (plan.remote_accelerator or "").upper()
        if want and got != want:
            self._mismatch = "Hardware: %s, not the %s that was asked for" % (got, want)
            return None
        # The shape matters as much as the accelerator: a Standard A100 is the
        # 40GB card and a High-RAM one the 80GB card, so an A100 match alone
        # would hand an 80GB request the 40GB VM. Accelerators with a single
        # shape (L4) are exempt; for the rest the reported shape must match,
        # and a status line without one (CLIs before 0.7) only matches a
        # standard request, since such a CLI cannot allocate high-RAM at all.
        if want and want not in COLAB_SINGLE_SHAPE:
            shape = _reported_shape(text)
            want_shape = "HIGH-RAM" if plan.high_mem else "STANDARD"
            if (shape or "STANDARD") != want_shape or (plan.high_mem and not shape):
                self._mismatch = "Shape: %s, not the %s shape that was asked for" % (
                    shape or "(not reported)",
                    "High-RAM" if plan.high_mem else "Standard",
                )
                return None
        log("  reusing session %s (%s), no allocation needed" % (name, plan.remote_accelerator))
        self.session = name
        self._released = False
        self._wipe(name)
        return name

    def _wipe(self, session: str) -> None:
        """Return a reused VM to a clean slate before handing it a notebook.

        MEASURED, and this is why it is not optional. A reused session was
        handed to a benchmark whose first cell asks for the card's free VRAM.
        The previous run had held ~22 GB in a torch allocation and had spawned
        llama-server as a detached subprocess. The new run read 18255 MiB free
        where the card has 40960, forced its "24 GiB" and "20 GiB" cells
        against a ceiling BELOW their own targets, and would have published
        three mislabelled rows. Nothing in the output says the machine was
        dirty; the numbers just quietly describe a different operating point.

        Two separate leaks, so two separate remedies:

        - the kernel holds Python objects (torch caching allocator, open
          models). `restart-kernel` frees those.
        - subprocesses started with Popen SURVIVE a kernel restart, because
          they were never the kernel's children in the accounting that matters.
          Those need killing by name.

        Order matters: kill the strays FIRST, because the kill runs THROUGH the
        kernel and a restart would otherwise drop the request. Best effort
        throughout: a wipe that fails is worth a warning, not a dead run.
        """
        killer = (
            "import subprocess\n"
            "for pat in ('llama-server', 'llama-bench', 'llama-batched-bench'):\n"
            "    subprocess.run(['pkill', '-9', '-f', pat])\n"
            "print('WIPE_KILLED')\n"
        )
        # Code on stdin, no `-f`: `colab exec` reads a pipe when no file is
        # given. A literal "-" is rejected as an extra argument (0.6.0, 0.7.4).
        res = run_capture(self._base() + ["exec", "-s", session], timeout = 180, stdin_text = killer)
        if res.returncode != 0:
            warn("could not sweep stray processes on %s: %s" % (session, (res.output or "")[:200]))
        res = run_capture(self._base() + ["restart-kernel", "-s", session], timeout = 300)
        if res.returncode != 0:
            warn(
                "could not restart the kernel on %s (%s); it keeps the "
                "previous run's memory and any measurement of free VRAM or "
                "RAM will describe that instead of a clean machine"
                % (session, (res.output or "")[:200])
            )
        else:
            log("  wiped session %s (strays killed, kernel restarted)" % session)

    def _record_owner(self, session: str) -> None:
        """Leave a note next to the session state saying which process owns it.

        `--reclaim-orphans` reads it: a session whose owner is gone (SIGKILL,
        OOM-killer, host reboot) never ran teardown, and the CLI's detached
        keep-alive pings it for up to 24h. Named sessions are kept on purpose
        and are never recorded here. Best effort: a run is never failed for it.
        """
        if self.session_name:
            return
        try:
            owner = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started": time.time(),
                "session": session,
                "state_file": str(self.state_file),
                "auth": self.auth_provider,
                "colab_home": colab_account_home(),
            }
            (self.workdir / COLAB_OWNER_FILE).write_text(
                json.dumps(owner, indent = 1), encoding = "utf-8"
            )
            registry = colab_runs_registry()
            registry.parent.mkdir(parents = True, exist_ok = True)
            with open(registry, "a", encoding = "utf-8") as handle:
                handle.write(str(self.workdir) + "\n")
        except OSError as exc:
            warn(
                "could not record the owner of %s (%s); --reclaim-orphans will "
                "not see it if this process dies" % (session, exc)
            )

    def _allocate(self, plan: Plan) -> str:
        self._mismatch = None
        attached = self._attached(plan)
        if attached:
            return attached
        if self._mismatch:
            # Allocating under the same name would overwrite the session's
            # entry in its state file: the old VM keeps running and billing,
            # but nothing local can exec on or `colab stop` it any more.
            raise InfraError(
                "session %s is alive but reports %s. Not reusing it, and not "
                "allocating over it either: a second `colab new -s %s` "
                "replaces its local state and leaves the running VM "
                "unreachable (still billed).\n"
                "Stop it first:\n    %s stop -s %s\n"
                "or pass a different --session name."
                % (
                    self.session_name,
                    self._mismatch,
                    self.session_name,
                    " ".join(shlex.quote(a) for a in self._base()),
                    self.session_name,
                )
            )
        session = self.session_name or ("unsloth-nbrun-%s" % uuid.uuid4().hex[:10])
        cmd = self._base() + ["new", "-s", session]
        if plan.remote_accelerator != "CPU":
            cmd += ["--gpu", plan.remote_accelerator]
        if plan.high_mem:
            if not self._supports_high_mem():
                version = colab_cli_version(self.cli)
                raise InfraError(
                    "google-colab-cli %s (%s) has no `colab new --high-mem`, "
                    "so the high-RAM shape (%s high-RAM%s) cannot be requested "
                    "and you would silently get the standard VM instead.\n"
                    "Upgrade the CLI (>= %s):\n"
                    "    uv tool upgrade google-colab-cli\n"
                    "    (or: pip install -U 'google-colab-cli>=%s')\n"
                    "or point COLAB_CLI at a newer `colab` binary, or drop the "
                    "high-RAM suffix and use --gpu %s."
                    % (
                        version or "(unknown version)",
                        self.cli or "colab",
                        plan.remote_accelerator,
                        " = the 80GB card" if plan.remote_accelerator == "A100" else "",
                        COLAB_CLI_HIGH_MEM_MIN,
                        COLAB_CLI_HIGH_MEM_MIN,
                        plan.remote_accelerator,
                    )
                )
            if plan.remote_accelerator in COLAB_SINGLE_SHAPE:
                log(
                    "  note: %s has a single machine shape, so --high-mem is "
                    "accepted and ignored" % plan.remote_accelerator
                )
            cmd += ["--high-mem"]
        log(
            "  allocating a %s%s VM (session %s)"
            % (plan.gpu, " high-RAM" if plan.high_mem else "", session)
        )
        # Record the name BEFORE the call returns: if `colab new` allocates and
        # then times out on us, the assignment exists and has to be released.
        self.session = session
        self._released = False
        self._record_owner(session)
        result = run_capture(cmd, timeout = 900)
        # A refusal for capacity is usually another run (or a browser tab) still holding the tier:
        # parallel runs on one account queue here instead of each ending as NO_RESULT.
        deadline = time.monotonic() + plan.alloc_wait
        while (
            result.returncode != 0
            and classify_platform_error(result.output) == "capacity"
            and time.monotonic() < deadline
        ):
            wait = max(1, min(ALLOC_POLL_SECONDS, int(deadline - time.monotonic())))
            log(
                "  Colab has no %s for this account right now; asking again in %ds "
                "(--alloc-wait %ds)" % (plan.gpu, wait, plan.alloc_wait)
            )
            _sleep(wait)
            result = run_capture(cmd, timeout = 900)
        if result.returncode != 0:
            kind = classify_platform_error(result.output)
            if kind == "capacity":
                raise InfraError(
                    "Colab would not give this account a %s right now.\n"
                    "This is a QUOTA / AVAILABILITY problem: not a broken "
                    "notebook, and not a bad credential.\n"
                    "Check what the account already holds FIRST:\n"
                    "    colab sessions\n"
                    "Assignments marked [?] were made outside this CLI, i.e. "
                    "browser tabs. They still count against the limit, and a\n"
                    "TooManyAssignments / 412 with no local sessions is usually "
                    "exactly that. Stop one and retry.\n"
                    "Otherwise: try a smaller tier (--gpu T4), wait, or review "
                    "your plan at https://colab.research.google.com/signup\n"
                    "Colab said: %s" % (plan.gpu, result.output[:400])
                )
            if kind == "auth":
                raise CredentialError(
                    "Colab rejected the credentials while allocating a VM.\n\n%s\n\n"
                    "Colab said: %s" % (ColabAuthStatus(False).instructions(), result.output[:400])
                )
            raise InfraError(
                "`colab new%s` failed: %s"
                % (
                    "" if plan.remote_accelerator == "CPU" else " --gpu " + plan.remote_accelerator,
                    result.output[:600],
                )
            )
        return session

    def run(self, plan: Plan, prepared: List[Tuple[PlannedNotebook, Path]]) -> List[RunResult]:
        results: List[RunResult] = []
        if plan.reuse_session:
            # One VM for everything. Fast, but every notebook after the first
            # inherits the previous one's installed packages.
            session = self._allocate(plan)
            try:
                for position, (item, patched) in enumerate(prepared):
                    if position:
                        # Kernel state also survives `colab exec` calls within
                        # a session, so at minimum reset the interpreter.
                        log("  restarting the kernel before the next notebook")
                        restart = run_capture(
                            self._base() + ["restart-kernel", "-s", session], timeout = 300
                        )
                        if restart.returncode != 0:
                            warn(
                                "could not restart the kernel (%s); the next "
                                "notebook inherits the previous one's state" % restart.output[:200]
                            )
                    results.append(self._run_one(plan, session, item, patched))
            finally:
                self.release("run finished")
            return results

        # Default: a fresh VM per notebook. Restarting the kernel resets the
        # interpreter but NOT the filesystem, so a `pip install` from notebook
        # 1 still shadows notebook 2's own pinned versions -- which fabricates
        # failures that look exactly like real regressions.
        for item, patched in prepared:
            for attempt in range(plan.connect_retries + 1):
                try:
                    session = self._allocate(plan)
                    try:
                        # A VM whose kernel never connects hangs `colab exec` until the wall timeout.
                        self._probe_kernel(session)
                        result = self._run_one(plan, session, item, patched)
                    finally:
                        self.release("notebook finished")
                except ConnectionLostError as exc:
                    result = RunResult(
                        item.output_name,
                        item.source.spec,
                        Verdict(CONNECTION_LOST, 0, 0, [], str(exc)),
                    )
                except (InfraError, CredentialError) as exc:
                    # Losing the VM on notebook 4 must not discard the verdicts
                    # for notebooks 1 to 3, which cost real GPU time to earn.
                    result = unrunnable(item, exc)
                if result.verdict.status != CONNECTION_LOST or attempt == plan.connect_retries:
                    break
                warn(
                    "%s: %s; retrying on a fresh VM (%d of %d)"
                    % (
                        item.source.spec,
                        result.verdict.reason or "connection lost",
                        attempt + 1,
                        plan.connect_retries,
                    )
                )
            results.append(result)
        return results

    def _probe_kernel(self, session: str) -> None:
        probe = self.workdir / "kernel-probe.py"
        probe.write_text('print("UNSLOTH_NBRUN_KERNEL_READY")\n', encoding = "utf-8")
        result = run_streaming(
            self._base() + ["exec", "-s", session, "-f", str(probe), "--timeout", "120"],
            timeout = KERNEL_PROBE_TIMEOUT,
            quiet = True,
            abort_on = COLAB_CONNECTION_LOST,
        )
        if "UNSLOTH_NBRUN_KERNEL_READY" not in result.output:
            raise ConnectionLostError(
                "the kernel on session %s did not answer a one-line probe within %s (%s)"
                % (
                    session,
                    human_duration(KERNEL_PROBE_TIMEOUT),
                    result.aborted_on
                    or ("timed out" if result.timed_out else "exit %d" % result.returncode),
                )
            )

    def _run_one(self, plan: Plan, session: str, item: PlannedNotebook, patched: Path) -> RunResult:
        started = time.time()
        log("")
        log("=> %s on Colab %s" % (item.source.spec, plan.gpu))
        cmd = self._base() + [
            "exec",
            "-s",
            session,
            "-f",
            str(patched),
            "--timeout",
            str(plan.per_cell_timeout),
        ]
        # `colab exec` writes <stem>_output.ipynb next to the input, and exits
        # 0 whether or not any cell raised. The exit code is context only.
        # A leftover from an earlier run in the same --outdir would otherwise
        # be read as THIS run's result when the CLI dies before writing one.
        produced = patched.with_name(patched.stem + "_output.ipynb")
        with contextlib.suppress(OSError):
            produced.unlink()
        result = run_streaming(
            cmd,
            timeout = plan.wall_timeout,
            abort_on = COLAB_CONNECTION_LOST,
            idle_timeout = getattr(plan, "idle_timeout", 0),
            cell_timeout = plan.per_cell_timeout + CELL_TIMEOUT_GRACE,
            cell_marker = COLAB_CELL_MARKER,
        )
        if result.timed_out:
            warn("%s: %s" % (item.source.spec, self._kill_reason(plan, result)))
            if self.keep_session:
                # Killing the local CLI does not stop the cell: the kernel keeps
                # running it on a VM that is kept (and billed). Without --session
                # / --keep-session the VM is stopped right after, which ends it.
                restart = run_capture(self._base() + ["restart-kernel", "-s", session], timeout = 300)
                if restart.returncode != 0:
                    warn(
                        "could not restart the kernel on kept session %s after the "
                        "kill (%s); the cell may still be running there"
                        % (session, (restart.output or "")[:200])
                    )

        log_path = self.workdir / ("%s.log" % item.output_name)
        log_path.write_text(result.output, encoding = "utf-8")
        final = self.workdir / ("%s.executed.ipynb" % item.output_name)
        remote = "colab session %s" % session

        if produced.exists():
            shutil.move(str(produced), str(final))
            try:
                verdict = verdict_from_file(final, "colab exec exited %d" % result.returncode)
            except InfraError as exc:
                verdict = Verdict(NO_RESULT, 0, 0, [], str(exc))
            # A PASS already proves every cell ran clean; a drop after that is teardown, and
            # turning it into CONNECTION_LOST would re-run a finished notebook on a fresh VM.
            if result.aborted_on and verdict.status in (INCOMPLETE, NOT_RUN):
                verdict = Verdict(
                    CONNECTION_LOST,
                    verdict.code_cells,
                    verdict.executed_cells,
                    verdict.failures,
                    "the kernel connection dropped (%r) after %d of %d code cells"
                    % (result.aborted_on, verdict.executed_cells, verdict.code_cells),
                    verdict.training_losses,
                )
            elif result.timed_out and verdict.status in (PASS, INCOMPLETE, NOT_RUN):
                # A killed run is never a PASS, even when the partial notebook
                # it saved looks clean.
                verdict = Verdict(
                    TIMEOUT,
                    verdict.code_cells,
                    verdict.executed_cells,
                    verdict.failures,
                    "%s after %d of %d code cells"
                    % (self._kill_reason(plan, result), verdict.executed_cells, verdict.code_cells),
                    verdict.training_losses,
                )
            return RunResult(
                item.output_name,
                item.source.spec,
                verdict,
                time.time() - started,
                str(final),
                str(log_path),
                remote,
            )

        if result.aborted_on:
            verdict = Verdict(
                CONNECTION_LOST,
                0,
                0,
                [],
                "the kernel connection dropped after %s (%r) and Colab wrote no output "
                "notebook. Infrastructure, not a verdict on the notebook. Tail in %s"
                % (human_duration(int(result.duration)), result.aborted_on, log_path),
            )
        elif result.timed_out:
            verdict = Verdict(
                TIMEOUT,
                0,
                0,
                [],
                "%s and Colab never wrote an output "
                "notebook. The tail of the run is in %s"
                % (self._kill_reason(plan, result), log_path),
            )
        else:
            verdict = Verdict(
                NO_RESULT,
                0,
                0,
                [],
                "Colab produced no output notebook (the CLI exited %d). That "
                "is an INFRASTRUCTURE failure, not a verdict on the notebook. "
                "Tail in %s" % (result.returncode, log_path),
            )
        return RunResult(
            item.output_name,
            item.source.spec,
            verdict,
            time.time() - started,
            None,
            str(log_path),
            remote,
        )

    @staticmethod
    def _kill_reason(plan: Plan, result: CommandResult) -> str:
        where = (" (in cell %s)" % result.last_cell) if result.last_cell else ""
        if result.killed_by == "cell":
            return (
                "per-cell timeout of %s hit%s; enforced locally because "
                "`colab exec --timeout` does not stop a quiet cell"
                % (human_duration(plan.per_cell_timeout), where)
            )
        if result.killed_by == "idle":
            return (
                "no output for %s%s (stalled kernel or output stream); "
                "killed by the idle watchdog"
                % (human_duration(getattr(plan, "idle_timeout", 0)), where)
            )
        return "wall timeout of %s hit%s" % (human_duration(plan.wall_timeout), where)

    def release(self, reason: str) -> None:
        if self._released or not self.session:
            return
        self._released = True
        # First the local CLI, then the VM: a `colab exec` still running would
        # re-add the session to the state file after `colab stop` removed it.
        try:
            kill_active_children()
        except Exception:  # noqa: BLE001
            pass
        if self.keep_session:
            log("")
            log(
                "NOT stopping session %s (%s). It bills until you run:"
                % (
                    self.session,
                    "--session, so the next run can attach to it"
                    if self.session_name
                    else "--keep-session",
                )
            )
            log("  colab --config %s stop -s %s" % (self.state_file, self.session))
            return
        log("")
        session = self.session
        log("releasing Colab session %s (%s)" % (session, reason))
        # Ignore interrupts for the duration of the stop. A second Ctrl-C here
        # unwinds the in-flight `colab stop`, kills its child, and leaves a VM
        # billing with nothing left to report it.
        previous = _ignore_interrupts()
        try:
            result = run_capture(self._base() + ["stop", "-s", session], timeout = 300)
            if result.returncode != 0 and "not found" not in result.output.lower():
                warn(
                    "`colab stop -s %s` failed: %s\nStop it by hand -- an "
                    "orphaned session bills until the 24h cap:\n  %s stop -s %s\n"
                    "(or later: python notebook_cloud_run.py --reclaim-orphans --yes)"
                    % (
                        session,
                        result.output[:300],
                        " ".join(shlex.quote(a) for a in self._base()),
                        session,
                    )
                )
            else:
                with contextlib.suppress(OSError):
                    (self.workdir / COLAB_OWNER_FILE).unlink()
        except Exception as exc:  # noqa: BLE001
            # Never propagate out of teardown: this runs from a `finally`, an
            # atexit hook and a signal handler.
            warn("could not stop Colab session %s: %s\nStop it by hand." % (session, exc))
        finally:
            self.session = None
            _restore_interrupts(previous)


# The Kaggle driver. Kaggle kernels hold exactly ONE source file -- sibling
# files in the push folder are silently not uploaded -- so the notebooks
# travel embedded in this driver as gzip + base64 rather than as attachments.
KAGGLE_DRIVER_TEMPLATE = """\
# Generated by notebook_cloud_run.py. Executes the embedded notebooks and
# writes each result into /kaggle/working, which is what
# `kaggle kernels output` hands back.
import base64
import gzip
import json
import os
import shutil
import subprocess
import sys
import time

PAYLOAD = "{payload}"
PER_CELL_TIMEOUT = {per_cell_timeout}
WORKING = "/kaggle/working"
# Notebooks run here and this whole directory is deleted afterwards.
# `kaggle kernels output` returns ALL of /kaggle/working, and a fine-tuning
# notebook leaves merged weights and GGUFs behind -- one unpruned sweep
# shipped back 371 MB and blocked for 18 minutes.
#
# Scratch deliberately does NOT live under /kaggle/working. A probe kernel
# measured that overlay at 19.5 GB TOTAL, while /tmp had 1026.8 GB free of
# 8062.4 -- a 16-bit merge plus a GGUF does not fit in the former and is
# comfortable in the latter. Running here was how Llama3_(8B)-Ollama exhausted
# the disk partway through an export, after the training had already been paid
# for. Only the executed notebooks and the summary are copied back, so the
# return payload stays small either way.
SCRATCH = os.environ.get("NBRUN_SCRATCH") or "/tmp/nbrun-scratch"

for _key, _value in {env!r}.items():
    os.environ[_key] = _value
# Every process the notebooks start inherits this tag (kernels, servers, setsid/nohup daemons);
# the reaper appended to this driver kills whatever still carries it after the last notebook.
NBRUN_TAG = "nbrun-%d-%d" % (os.getpid(), int(time.time() * 1000))
os.environ["NBRUN_TAG"] = NBRUN_TAG

try:
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError
except Exception:
    print("[nbrun] installing nbclient/nbformat", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "nbclient", "nbformat"], check=False)
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

payload = json.loads(gzip.decompress(base64.b64decode(PAYLOAD)).decode("utf-8"))
os.makedirs(SCRATCH, exist_ok=True)
summary = []
# --parallel-gpus N: run N packed notebooks at once, notebook k pinned to GPU
# k % N through its kernel's CUDA_VISIBLE_DEVICES (a T4x2 kernel is two 15 GB
# cards, and Kaggle bills the kernel's wall time, so idling one card is waste).
PARALLEL_GPUS = {parallel_gpus}


def run_entry(entry, gpu=None):
    name = entry["name"]
    out_path = os.path.join(WORKING, name + ".executed.ipynb")
    print("[nbrun] ===== %s =====%s" % (name, "" if gpu is None else " (GPU %d)" % gpu), flush=True)
    started = time.time()
    notebook = nbformat.reads(json.dumps(entry["notebook"]), as_version=4)
    path = SCRATCH if gpu is None else os.path.join(SCRATCH, name)
    os.makedirs(path, exist_ok=True)
    client = NotebookClient(
        notebook,
        timeout=PER_CELL_TIMEOUT,
        kernel_name="python3",
        # Stop at the first failing cell rather than burning the rest of the
        # budget on cells that cannot work. The partially executed notebook
        # still carries the error output, which is what the verdict reads.
        allow_errors=False,
        resources={{"metadata": {{"path": path}}}},
    )
    kw = {{}}
    if gpu is not None:
        kw["env"] = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu))
    error = ""
    try:
        client.execute(**kw)
    except CellExecutionError as exc:
        error = "cell raised: %s" % str(exc)[:400]
        print("[nbrun] %s: %s" % (name, error), flush=True)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, str(exc)[:400])
        print("[nbrun] %s: execution stopped: %s" % (name, error), flush=True)
    finally:
        with open(out_path, "w", encoding="utf-8") as handle:
            nbformat.write(notebook, handle)
    elapsed = time.time() - started
    print("[nbrun] %s finished in %.1fs -> %s" % (name, elapsed, out_path),
          flush=True)
    return {{"name": name, "seconds": round(elapsed, 1), "error": error}}


if PARALLEL_GPUS > 1:
    from concurrent.futures import ThreadPoolExecutor
    entries = payload["notebooks"]
    with ThreadPoolExecutor(max_workers=PARALLEL_GPUS) as pool:
        summary = list(pool.map(lambda ie: run_entry(ie[1], ie[0] % PARALLEL_GPUS),
                                enumerate(entries)))
else:
    for entry in payload["notebooks"]:
        summary.append(run_entry(entry))

shutil.rmtree(SCRATCH, ignore_errors=True)
with open(os.path.join(WORKING, "nbrun_summary.json"), "w",
          encoding="utf-8") as handle:
    json.dump({{"notebooks": summary}}, handle, indent=2)
print("[nbrun] all done", flush=True)
"""

# Appended to the driver (not .format()ed). A notebook that leaves a server or a setsid / nohup
# daemon behind keeps processes alive after the last cell; nbclient only shuts down the kernel
# itself. Kill everything that carries this run's NBRUN_TAG (never the driver or its ancestors,
# never Kaggle's own processes, which do not carry it), TERM then KILL, and say what it was.
KAGGLE_DRIVER_REAPER = """
def _nbrun_reap(tag, grace=10.0):
    import signal as _signal
    keep, pid = set(), os.getpid()
    while pid > 1 and pid not in keep:
        keep.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    needle = ("NBRUN_TAG=" + tag).encode()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in keep:
            continue
        try:
            with open("/proc/%s/environ" % name, "rb") as fh:
                if needle not in fh.read().split(b"\\x00"):
                    continue
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmd = fh.read().replace(b"\\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            continue
        found.append((int(name), cmd[:200]))
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        alive = []
        for pid, _cmd in found:
            try:
                os.kill(pid, sig)
                alive.append(pid)
            except Exception:
                pass
        end = time.time() + (grace if sig == _signal.SIGTERM else 2.0)
        while alive and time.time() < end:
            alive = [p for p in alive if os.path.exists("/proc/%d" % p)
                     and open("/proc/%d/stat" % p).read().rsplit(")", 1)[1].split()[0] != "Z"]
            time.sleep(0.2)
        if not alive:
            break
    return found


try:
    _left = _nbrun_reap(NBRUN_TAG)
except Exception as _exc:                          # never fail the run over the cleanup
    _left = []
    print("[nbrun] leftover-process reap failed: %s" % _exc, flush=True)
print("[nbrun] leftover processes reaped: %d" % len(_left), flush=True)
for _pid, _cmd in _left:
    print("[nbrun]   killed pid %d: %s" % (_pid, _cmd), flush=True)
try:
    _sp = os.path.join(WORKING, "nbrun_summary.json")
    _sm = json.load(open(_sp, encoding="utf-8"))
    _sm["leftover_processes"] = [{"pid": p, "cmd": c} for p, c in _left]
    json.dump(_sm, open(_sp, "w", encoding="utf-8"), indent=2)
except Exception:
    pass
"""


class KaggleBackend(Backend):
    """Package the notebooks into one Kaggle kernel, run it, fetch the results."""

    name = "kaggle"

    # Kaggle rejects oversized sources; stay well under it and say so early.
    MAX_PAYLOAD_BYTES = 800_000
    # A run of unreadable statuses usually means the kernel was filed under an
    # address we never saw. Give up rather than poll until the wall timeout.
    MAX_UNKNOWN_POLLS = 10

    # A kernel QUEUED longer than this is deleted and the run reported as INFRA: Kaggle keeps
    # a queued kernel waiting indefinitely, and the wall budget is for running, not queueing.
    DEFAULT_QUEUE_TIMEOUT = 3600

    def __init__(
        self,
        workdir: Path,
        creds: KaggleCredentials,
        poll_interval: int = 30,
        keep_kernel: bool = False,
        queue_timeout: Optional[int] = None,
    ) -> None:
        self.workdir = workdir
        self.creds = creds
        self.poll_interval = max(5, poll_interval)
        self.keep_kernel = keep_kernel
        self.queue_timeout = (
            self.DEFAULT_QUEUE_TIMEOUT if queue_timeout is None else max(60, int(queue_timeout))
        )
        self.cli = kaggle_cli()
        self.kernel_id: Optional[str] = None
        self._released = False
        # None (not pushed) | "pushed" | QUEUED | RUNNING | a KAGGLE_TERMINAL status
        # | "collected" (every executed notebook downloaded and parsed) | "deleted"
        self._state: Optional[str] = None

    def _token_env_name(self) -> Optional[str]:
        src = (self.creds.source or "").split(";")[0].split(" ")[0].strip()
        return src if src.startswith("KAGGLE_API_TOKEN") else None

    def _mark(self, state: str, **fields) -> None:
        self._state = state
        if self.kernel_id:
            with contextlib.suppress(Exception):
                record_kaggle_kernel(self.workdir, self.kernel_id, state = state, **fields)

    def delete_kernel(self, why: str) -> bool:
        """`kaggle kernels delete -y`: the only stop the CLI offers (it has no cancel), and it
        does stop a RUNNING kernel (probed 2026-10-02: a running CPU kernel deleted, status then
        denied). Interrupts are ignored meanwhile, so a second Ctrl-C cannot strand it."""
        if not self.kernel_id or self._state == "deleted":
            return True
        previous = _ignore_interrupts()
        try:
            res = run_capture(
                [self.cli or "kaggle", "kernels", "delete", "-y", self.kernel_id],
                timeout = 180,
                env = self._env(),
            )
            out = (res.output or "").lower()
            ok = "deleted successfully" in out or (res.returncode == 0 and "error" not in out)
            if ok:
                self._mark("deleted", deleted_why = why)
                log("  deleted Kaggle kernel %s (%s)" % (self.kernel_id, why))
            else:
                warn(
                    "could not delete Kaggle kernel %s (%s): %s\nDelete it by hand: "
                    "kaggle kernels delete -y %s"
                    % (self.kernel_id, why, res.output[:300], self.kernel_id)
                )
            return ok
        except Exception as exc:  # noqa: BLE001
            warn("could not delete Kaggle kernel %s: %s" % (self.kernel_id, exc))
            return False
        finally:
            _restore_interrupts(previous)

    def preflight(self) -> None:
        ok, detail, username = check_kaggle_auth(self.creds)
        if not ok:
            raise CredentialError(detail)
        # The kernel id has to name the account that owns it, and with a
        # bearer token or a delegated credential we only learn that from
        # Kaggle itself.
        self.creds.username = username or self.creds.username
        log("Kaggle credentials OK for %s" % self.creds.redacted())

    def _env(self) -> Dict[str, str]:
        return subprocess_env(self.creds.env())

    def build_kernel_package(
        self, plan: Plan, prepared: List[Tuple[PlannedNotebook, Path]], slug: str
    ) -> Path:
        # Kaggle derives the kernel address from the TITLE, not from `id`, and
        # the CLI only warns when the two disagree. Keep the title equal to the
        # slug and assert the round trip.
        title = slug
        if slugify(title) != slug:
            raise InfraError(
                "internal error: title %r slugifies to %r, expected %r"
                % (title, slugify(title), slug)
            )

        package = self.workdir / "kernels" / slug
        package.mkdir(parents = True, exist_ok = True)

        payload = {
            "notebooks": [
                {"name": item.output_name, "notebook": read_notebook(path)}
                for item, path in prepared
            ]
        }
        blob = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"), 6)).decode(
            "ascii"
        )
        if len(blob) > self.MAX_PAYLOAD_BYTES:
            raise InfraError(
                "these notebooks compress to %.1f MB, over the %.1f MB this "
                "script will embed in a single Kaggle kernel. Submit fewer "
                "notebooks per run." % (len(blob) / 1e6, self.MAX_PAYLOAD_BYTES / 1e6)
            )

        driver_code = (
            KAGGLE_DRIVER_TEMPLATE.format(
                payload = blob,
                per_cell_timeout = plan.per_cell_timeout,
                env = dict(BOOTSTRAP_ENV, **plan.extra_env),
                parallel_gpus = max(1, int(getattr(plan, "parallel_gpus", 1) or 1)),
            )
            + KAGGLE_DRIVER_REAPER
        )
        driver = {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "id": "nbruncell",
                    "metadata": {},
                    "outputs": [],
                    "source": [ln + "\n" for ln in driver_code.splitlines()],
                }
            ],
            "metadata": {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        (package / "runner.ipynb").write_text(json.dumps(driver, indent = 1), encoding = "utf-8")

        wants_gpu = plan.remote_accelerator not in ("", "Tpu1VmV38")
        metadata = {
            "id": "%s/%s" % (self.creds.username, slug),
            "title": title,
            "code_file": "runner.ipynb",
            "language": "python",
            "kernel_type": "notebook",
            # Never publish somebody's fine-tuning run by accident.
            "is_private": "true",
            "enable_gpu": "true" if wants_gpu else "false",
            "enable_tpu": "true" if plan.remote_accelerator == "Tpu1VmV38" else "false",
            "enable_internet": "true",
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }
        if plan.remote_accelerator:
            # Belt and braces: the accelerator also goes on the push command.
            metadata["machine_shape"] = plan.remote_accelerator
        (package / "kernel-metadata.json").write_text(
            json.dumps(metadata, indent = 2), encoding = "utf-8"
        )
        return package

    def run(self, plan: Plan, prepared: List[Tuple[PlannedNotebook, Path]]) -> List[RunResult]:
        # One kernel per notebook by default. Notebooks packed into a single
        # kernel share one site-packages, so notebook 1's `pip install` shadows
        # notebook 2's pins and fabricates failures that look exactly like real
        # regressions. --pack trades that isolation for fewer queue waits.
        pack = max(1, plan.pack)
        batches = [prepared[i : i + pack] for i in range(0, len(prepared), pack)]
        results: List[RunResult] = []
        for index, batch in enumerate(batches, 1):
            if len(batches) > 1:
                log("")
                log(
                    "kernel %d of %d (%s)"
                    % (index, len(batches), ", ".join(item.output_name for item, _ in batch))
                )
            try:
                results.extend(self._run_batch(plan, batch))
            except (InfraError, CredentialError) as exc:
                # As on Colab: a quota wall on batch 3 must not throw away the
                # verdicts batches 1 and 2 already paid for.
                results.extend(unrunnable(item, exc) for item, _ in batch)
        return results

    def _run_batch(self, plan: Plan, batch: List[Tuple[PlannedNotebook, Path]]) -> List[RunResult]:
        slug = "unsloth-nbrun-%s" % uuid.uuid4().hex[:10]
        self.kernel_id = "%s/%s" % (self.creds.username, slug)
        self._released = False
        self._state = None
        package = self.build_kernel_package(plan, batch, slug)
        started = time.time()
        try:
            # "pushing" before the call: a Ctrl-C / SIGTERM landing while `kernels push` is in
            # flight may leave a kernel Kaggle already accepted, so release() still deletes it.
            self._state = "pushing"
            try:
                self._push(plan, package)
            except Exception:
                self._state = None  # refused / failed push: nothing exists to delete
                raise
            self._mark(
                "pushed",
                slug = slug,
                token_env = self._token_env_name(),
                token_fp = _token_fingerprint(self.creds.token) if self.creds.token else None,
                pool_slot = os.environ.get("NBRUN_POOL_SLOT") or None,
                pushed_at = round(time.time(), 1),
                expected = len(batch),
            )
            state = self._poll(plan)
            self._mark(state)
            downloaded = self._fetch_output(slug, len(batch))
            got = len(self._parseable_outputs(downloaded))
            results = self._collect(plan, batch, downloaded, state, time.time() - started)
            if got >= len(batch):
                self._mark("collected", output_dir = str(downloaded))
                if not self.keep_kernel:
                    # Output is local and parsed: the kernel is only clutter on the account now.
                    self.delete_kernel("output downloaded and verified")
            else:
                warn(
                    "kept Kaggle kernel %s: only %d of %d executed notebooks came back. Retry the "
                    "download with:  kaggle kernels output %s -p %s\nthen delete it with:  "
                    "kaggle kernels delete -y %s   (or: notebook_cloud_run.py --kaggle-sweep --yes --force)"
                    % (self.kernel_id, got, len(batch), self.kernel_id, downloaded, self.kernel_id)
                )
            return results
        finally:
            # Record the wall clock of this kernel against the token that ran
            # it, for display (`--quota`, cloud_pool status); token choice does
            # not gate on it. In the `finally` so a failed or interrupted
            # kernel, which still consumed quota, is counted too.
            if self.creds.token and plan.remote_accelerator:
                KaggleUsageLedger().record(self.creds.token, time.time() - started)
            self.release("run finished")

    def _push(self, plan: Plan, package: Path) -> None:
        cmd = [
            self.cli or "kaggle",
            "kernels",
            "push",
            "-p",
            str(package),
            "-t",
            str(plan.wall_timeout),
        ]
        if plan.remote_accelerator:
            cmd += ["--accelerator", plan.remote_accelerator]
        log("  pushing kernel %s (%s)" % (self.kernel_id, plan.gpu))
        result = run_capture(cmd, timeout = 600, env = self._env())
        output = result.output

        # The CLI exits 0 on a failed push. Success is the banner, not the
        # return code -- the same lie as `colab exec`, in a different place.
        if "successfully pushed" not in output.lower():
            kind = classify_platform_error(output)
            if kind == "capacity":
                # Kaggle's own signal: bench this account (quota: a day; busy /
                # 2-kernel cap / rate limit: 10 min) so the next draw skips it.
                refusal = classify_kaggle_refusal(output)
                hold = _KAGGLE_QUOTA_HOLD_S if refusal == "quota" else _KAGGLE_BUSY_HOLD_S
                if self.creds.token:
                    KaggleUsageLedger().mark_saturated(self.creds.token, hold)
                raise InfraError(
                    "Kaggle refused the %s kernel for this account (%s).\n"
                    "Not a broken notebook. The concurrent-GPU-kernel cap is 2 "
                    "and the weekly GPU quota is finite; check with:  kaggle quota\n"
                    "This token is benched for %s, so the next draw uses another "
                    "KAGGLE_API_TOKEN* account.\nKaggle said: %s"
                    % (
                        plan.gpu,
                        "weekly quota" if refusal == "quota" else "busy / concurrency cap",
                        human_duration(hold),
                        output[:400],
                    )
                )
            if kind == "auth":
                raise CredentialError(
                    "Kaggle rejected the credentials while pushing the kernel "
                    "(source: %s).\nRe-create the token at "
                    "https://www.kaggle.com/settings -> API.\nKaggle said: %s"
                    % (self.creds.source, output[:400])
                )
            raise InfraError("`kaggle kernels push` failed: %s" % output[:600])

        if "does not resolve to the specified id" in output:
            raise InfraError(
                "Kaggle filed this kernel under an address other than %s, so "
                "its status and output could never be fetched. Kaggle said: %s"
                % (self.kernel_id, output[:300])
            )
        log("  pushed: https://www.kaggle.com/code/%s" % self.kernel_id)

    # The CLI prints: <kernel> has status "KernelWorkerStatus.COMPLETE"
    _STATUS_RE = re.compile(
        r"\b(QUEUED|RUNNING|COMPLETE|ERROR|CANCEL_REQUESTED|CANCEL_ACKNOWLEDGED|NEW_SCRIPT)\b"
    )

    def _poll(self, plan: Plan) -> str:
        # Queue time is not the notebook's fault, so it gets its own slack on
        # top of the wall timeout the kernel itself is bounded by.
        started = time.time()
        # Queue time gets its own budget (queue_timeout); the run budget (wall + 900 s slack)
        # starts when the kernel is first seen RUNNING, so a long queue cannot eat it and make
        # us walk away from a kernel that is legitimately still running.
        deadline = started + self.queue_timeout + plan.wall_timeout + 900
        running_since: Optional[float] = None
        last = ""
        unknowns = 0
        log(
            "  waiting for the kernel (queue budget %s, run budget %s)"
            % (human_duration(self.queue_timeout), human_duration(plan.wall_timeout + 900))
        )
        while time.time() < deadline:
            result = run_capture(
                [self.cli or "kaggle", "kernels", "status", self.kernel_id or ""],
                timeout = 180,
                env = self._env(),
            )
            match = self._STATUS_RE.search(result.output or "")
            status = match.group(1) if match else "UNKNOWN"
            if status != last:
                log("  status: %s  (%s elapsed)" % (status, human_duration(time.time() - started)))
                last = status
                if status in ("QUEUED", "RUNNING"):
                    self._mark(status)
            if status == "RUNNING" and running_since is None:
                running_since = time.time()
                deadline = running_since + plan.wall_timeout + 900
            if (
                status in ("QUEUED", "NEW_SCRIPT")
                and running_since is None
                and time.time() - started > self.queue_timeout
            ):
                kept = self.keep_kernel
                if not kept:
                    self.delete_kernel("still QUEUED after %s" % human_duration(self.queue_timeout))
                raise InfraError(
                    "the Kaggle kernel %s was still QUEUED after %s (Kaggle capacity, not the "
                    "notebook). %s"
                    % (
                        self.kernel_id,
                        human_duration(self.queue_timeout),
                        "Kept (--keep-kernel); it may still start and bill."
                        if kept
                        else "It was deleted so it cannot start unattended.",
                    )
                )
            if status in ("COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED"):
                if status != "COMPLETE":
                    # Not a verdict. The kernel may still have produced partly
                    # executed notebooks, and those are what we grade on.
                    warn("the kernel finished with status %s: %s" % (status, result.output[:300]))
                return status
            if status == "UNKNOWN":
                unknowns += 1
                if unknowns >= self.MAX_UNKNOWN_POLLS:
                    raise InfraError(
                        "could not read the status of %s %d times in a row. "
                        "The kernel is probably filed under a different "
                        "address. Last reply: %s" % (self.kernel_id, unknowns, result.output[:300])
                    )
            else:
                unknowns = 0
            time.sleep(self.poll_interval)
        # Kaggle enforces `push -t wall_timeout` itself, so this is a kernel that outlived its own
        # limit (or a status we cannot read): stop the billing rather than only the local wait.
        if self.keep_kernel:
            raise InfraError(
                "the Kaggle kernel %s was still running after %s. Kept (--keep-kernel): it is "
                "STILL GOING on Kaggle and consuming GPU quota; stop it with:  kaggle kernels "
                "delete -y %s"
                % (self.kernel_id, human_duration(plan.wall_timeout + 900), self.kernel_id)
            )
        deleted = self.delete_kernel("still running past the wall timeout")
        raise InfraError(
            "the Kaggle kernel %s was still running after %s; %s"
            % (
                self.kernel_id,
                human_duration(plan.wall_timeout + 900),
                "it was deleted, which stops it."
                if deleted
                else "deleting it FAILED, so it may still be billing: kaggle kernels delete -y %s"
                % self.kernel_id,
            )
        )

    def _fetch_output(
        self,
        slug: str,
        expected: int,
        attempts: int = 3,
    ) -> Path:
        dest = self.workdir / "kaggle-output" / slug
        dest.mkdir(parents = True, exist_ok = True)
        for attempt in range(1, attempts + 1):
            if len(self._parseable_outputs(dest)) >= expected:
                break
            result = run_capture(
                [
                    self.cli or "kaggle",
                    "kernels",
                    "output",
                    self.kernel_id or "",
                    "-p",
                    str(dest),
                    "--force",
                ],
                timeout = 1800,
                env = self._env(),
            )
            if result.returncode != 0 and attempt == attempts:
                warn("`kaggle kernels output` failed: %s" % result.output[:300])
            if len(self._parseable_outputs(dest)) >= expected:
                break
            if attempt < attempts:
                time.sleep(30 * attempt)
        logs = run_capture(
            [self.cli or "kaggle", "kernels", "logs", self.kernel_id or ""],
            timeout = 600,
            env = self._env(),
        )
        (dest / "kaggle-run.log").write_text(self._render_kaggle_log(logs.output), encoding = "utf-8")
        return dest

    @staticmethod
    def _render_kaggle_log(raw: str) -> str:
        """Kaggle logs are a JSON array of partial stream fragments.

        Each record's `data` is a fragment, often part of a line, so the
        records have to be concatenated before anything can be read out of
        them.
        """
        try:
            records = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(records, list):
            return raw
        return strip_ansi("".join(str(r.get("data", "")) for r in records if isinstance(r, dict)))

    @staticmethod
    def _parseable_outputs(directory: Path) -> List[Path]:
        """Executed notebooks that actually parse.

        Existence is not enough: a download killed mid-write leaves a file of
        plausible size, and treating it as complete moves the failure to the
        verdict, after the download budget has been spent.
        """
        good = []
        for path in sorted(directory.rglob("*.executed.ipynb")):
            try:
                json.loads(path.read_text(encoding = "utf-8"))
            except (OSError, ValueError):
                continue
            good.append(path)
        return good

    def _collect(
        self,
        plan: Plan,
        prepared: List[Tuple[PlannedNotebook, Path]],
        downloaded: Path,
        state: str,
        total: float,
    ) -> List[RunResult]:
        results: List[RunResult] = []
        per_notebook = total / max(1, len(prepared))
        kernel_log = downloaded / "kaggle-run.log"
        url = "https://www.kaggle.com/code/%s" % self.kernel_id
        for item, _patched in prepared:
            wanted = "%s.executed.ipynb" % item.output_name
            matches = [p for p in self._parseable_outputs(downloaded) if p.name == wanted]
            if matches:
                final = self.workdir / wanted
                shutil.copyfile(str(matches[0]), str(final))
                verdict = verdict_from_file(
                    final,
                    "kaggle kernel finished %s" % state,
                    expect_gpu = plan.remote_accelerator not in ("", "Tpu1VmV38"),
                )
                if verdict.status == NO_GPU:
                    KaggleUsageLedger().mark_saturated(self.creds.token)
                    log(
                        "  this Kaggle token got no GPU (quota); benched for a day, so the next draw uses another account"
                    )
                results.append(
                    RunResult(
                        item.output_name,
                        item.source.spec,
                        verdict,
                        per_notebook,
                        str(final),
                        str(kernel_log),
                        url,
                    )
                )
            else:
                results.append(
                    RunResult(
                        item.output_name,
                        item.source.spec,
                        Verdict(
                            NO_RESULT,
                            0,
                            0,
                            [],
                            "the Kaggle kernel (status %s) returned no readable "
                            "executed notebook for this entry. That is an "
                            "INFRASTRUCTURE failure, not a verdict on the "
                            "notebook. Kernel log: %s" % (state, kernel_log),
                        ),
                        per_notebook,
                        None,
                        str(kernel_log),
                        url,
                    )
                )
        return results

    def release(self, reason: str) -> None:
        """Hand the kernel back. Runs from `finally`, atexit and the signal handlers.

        * still QUEUED / RUNNING (Ctrl-C, SIGTERM, SIGHUP from a dying shell, an exception):
          nobody is left to collect it and Kaggle bills wall time, so it is deleted (which stops
          it), unless --keep-kernel; the record in the run dir names it either way.
        * finished but not collected (download failed): kept, with the commands to fetch it.
        * collected: already deleted by _run_batch unless --keep-kernel.
        """
        if self._released or not self.kernel_id or self._state is None:
            return
        self._released = True
        state = self._state
        if state == "deleted":
            return
        if state in ("pushing", "pushed", "QUEUED", "RUNNING"):
            if self.keep_kernel:
                warn(
                    "Kaggle kernel %s is %s and KEPT (--keep-kernel, %s): it bills until it ends. "
                    "Collect: kaggle kernels output %s -p %s ; stop: kaggle kernels delete -y %s"
                    % (
                        self.kernel_id,
                        state,
                        reason,
                        self.kernel_id,
                        self.workdir / "kaggle-output",
                        self.kernel_id,
                    )
                )
                self._mark(state, abandoned = reason)
                return
            self.delete_kernel("%s while %s" % (reason, state))
            return
        if state == "collected":
            return
        log("")
        log(
            "Kaggle kernel %s (%s, status %s) kept: its output was not collected. Fetch it with:  "
            "kaggle kernels output %s -p %s   then delete it with:  kaggle kernels delete -y %s"
            % (
                self.kernel_id,
                reason,
                state,
                self.kernel_id,
                self.workdir / "kaggle-output",
                self.kernel_id,
            )
        )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_report(results: List[RunResult], workdir: Path) -> None:
    # Deliberately `print`, not `log`: --quiet trims progress chatter, but a
    # run that reports nothing at all is worse than useless.
    def line(text: str = "") -> None:
        print(text, flush = True)

    line("")
    line("=" * 74)
    line("RESULTS")
    line("=" * 74)
    width = max([len(r.name) for r in results] + [8])
    for result in results:
        line(
            "%-12s %-*s %-8s %s"
            % (
                result.verdict.status,
                width,
                result.name,
                human_duration(result.duration),
                result.verdict.summary(),
            )
        )
        # summary() already names the first failure; show the next few only.
        for failure in result.verdict.failures[1:4]:
            line("%-12s %-*s %-8s   %s" % ("", width, "", "", failure.one_line()))
        if result.executed_path:
            line("%-12s %-*s %-8s   %s" % ("", width, "", "", result.executed_path))
    passed = sum(1 for r in results if r.verdict.passed)
    line("-" * 74)
    line("%d/%d passed. Artefacts in %s" % (passed, len(results), workdir))


def write_report(results: List[RunResult], plan: Plan, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "plan": plan.to_dict(),
                "results": [r.to_dict() for r in results],
                "passed": sum(1 for r in results if r.verdict.passed),
                "total": len(results),
            },
            indent = 2,
        ),
        encoding = "utf-8",
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

EPILOG = """\
quickstart
  1. python notebook_cloud_run.py --backend kaggle --check-auth
  2. python notebook_cloud_run.py --backend kaggle --gpu T4x2 --dry-run \\
         "Llama3.1_(8B)-Alpaca.ipynb"
  3. python notebook_cloud_run.py --backend kaggle --gpu T4x2 \\
         "Llama3.1_(8B)-Alpaca.ipynb"

notebook arguments
  a local path            ./my_notebook.ipynb
  a name from the repo    "Llama3.1_(8B)-Alpaca.ipynb"   (unslothai/notebooks)
  any URL                 a Colab or GitHub link is rewritten to its raw form

credentials
  Kaggle   --kaggle-token / --kaggle-username + --kaggle-key, else
           KAGGLE_API_TOKEN, else KAGGLE_USERNAME + KAGGLE_KEY, else an
           access_token file, else kaggle.json, else the credential
           `kaggle auth login` left behind, else an interactive prompt that
           offers to write kaggle.json at mode 600.
  Colab    Application Default Credentials. This script DETECTS them and
           prints the exact `gcloud auth application-default login` command
           for you to run; it never tries to drive a browser flow itself.

why the verdict is not the exit code
  `colab exec` exits 0 with a raised exception sitting in the output
  notebook, and Kaggle reports a kernel "complete" when its first cell died.
  Every verdict here comes from parsing the executed notebook.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog = "notebook_cloud_run.py",
        description = "Run Unsloth notebooks on a rented Colab or Kaggle GPU, "
        "and report a real pass/fail by parsing the executed "
        "notebook rather than trusting the CLI exit code.",
        epilog = EPILOG,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "notebooks",
        nargs = "*",
        help = "one or more notebooks: a local path, a name from unslothai/notebooks, or a URL",
    )
    parser.add_argument(
        "--backend",
        choices = ("colab", "kaggle"),
        default = "colab",
        help = "where to run (default: colab)",
    )
    parser.add_argument(
        "--gpu",
        default = None,
        help = "GPU tier. Colab: %s. Kaggle: T4x2, P100, TPU, CPU. "
        "(default: T4 on Colab, T4x2 on Kaggle)" % ", ".join(COLAB_GPUS),
    )
    parser.add_argument(
        "--max-steps",
        type = int,
        default = DEFAULT_MAX_STEPS,
        help = "cap every trainer at this many steps so a smoke run stays cheap "
        "(default: %d). Never raises a lower value already in the "
        "notebook, and lowers logging_steps to match." % DEFAULT_MAX_STEPS,
    )
    parser.add_argument(
        "--grpo-max-steps",
        type = int,
        default = DEFAULT_GRPO_MAX_STEPS,
        help = "step cap for GRPO cells specifically, since one GRPO step costs "
        "a whole batch of rollouts (default: %d)" % DEFAULT_GRPO_MAX_STEPS,
    )
    parser.add_argument(
        "--no-smoke-patch",
        action = "store_true",
        help = "run the notebook exactly as written: no step cap, no "
        "logging_steps change, no report_to override. Expect hours and "
        "real money.",
    )
    parser.add_argument(
        "--per-cell-timeout",
        type = int,
        default = DEFAULT_PER_CELL_TIMEOUT,
        help = "seconds any single cell may take (default: %d)" % DEFAULT_PER_CELL_TIMEOUT,
    )
    parser.add_argument(
        "--wall-timeout",
        type = int,
        default = DEFAULT_WALL_TIMEOUT,
        help = "seconds the whole notebook may take, enforced independently of "
        "--per-cell-timeout (default: %d)" % DEFAULT_WALL_TIMEOUT,
    )
    parser.add_argument(
        "--idle-timeout",
        type = int,
        default = DEFAULT_IDLE_TIMEOUT,
        help = "colab: kill the run when `colab exec` prints nothing for this many "
        "seconds (a stalled kernel websocket or output stream); 0 disables "
        "(default: %d)" % DEFAULT_IDLE_TIMEOUT,
    )
    parser.add_argument(
        "--reclaim-orphans",
        action = "store_true",
        help = "colab: list unsloth-nbrun-* sessions recorded in local run dirs "
        "whose owning process is gone; with --yes, stop them. Never touches "
        "named (--session) or browser sessions",
    )
    parser.add_argument(
        "--reclaim-root",
        action = "append",
        default = [],
        metavar = "DIR",
        help = "with --reclaim-orphans: also scan DIR recursively for run dirs "
        "(the run registry is always read)",
    )
    parser.add_argument(
        "--yes",
        action = "store_true",
        help = "with --reclaim-orphans / --kaggle-sweep: act (stop the Colab orphans / delete the kernels); "
        "without it both are dry runs",
    )
    parser.add_argument(
        "--connect-retries",
        type = int,
        default = DEFAULT_CONNECT_RETRIES,
        help = "colab: when a VM's kernel does not answer a one-line probe, or the "
        "connection drops mid-run, release it and retry the notebook on this "
        "many fresh VMs (default: %d)" % DEFAULT_CONNECT_RETRIES,
    )
    parser.add_argument(
        "--alloc-wait",
        type = int,
        default = DEFAULT_ALLOC_WAIT,
        help = "colab: when a VM request is refused for capacity (another run still "
        "holds the tier, TooManyAssignments / 412), keep asking every %ds for "
        "this many seconds before giving up; 0 fails at once (default: %d)"
        % (ALLOC_POLL_SECONDS, DEFAULT_ALLOC_WAIT),
    )
    parser.add_argument(
        "--env",
        action = "append",
        metavar = "KEY=VALUE",
        default = [],
        help = "extra environment variable for the remote kernel, repeatable "
        "(e.g. --env UNSLOTH_COMPILE_DISABLE=1)",
    )
    parser.add_argument(
        "--outdir",
        default = None,
        help = "where to write executed notebooks, logs and report.json "
        "(default: a timestamped directory under the system temp dir)",
    )
    parser.add_argument(
        "--dry-run",
        action = "store_true",
        help = "print exactly what would be submitted, then exit. Touches no "
        "remote machine and needs no credentials.",
    )
    parser.add_argument(
        "--check-auth",
        action = "store_true",
        help = "check credentials for the chosen backend, then exit",
    )
    parser.add_argument(
        "--list-gpus",
        action = "store_true",
        help = "list the GPU tiers each backend accepts, then exit",
    )
    parser.add_argument(
        "--quota",
        action = "store_true",
        help = "Kaggle GPU hours used this week per KAGGLE_API_TOKEN* (local ledger) and live "
        "Colab sessions, then exit",
    )
    parser.add_argument(
        "--colab-auth",
        choices = ("adc", "oauth2"),
        default = "adc",
        help = "Colab credential provider. adc reads gcloud Application Default "
        "Credentials. oauth2 is the CLI's own remote copy-paste flow, which "
        "needs no gcloud and so is the one that works on a headless host.",
    )
    parser.add_argument(
        "--session",
        default = None,
        metavar = "NAME",
        help = "Colab: attach to the named VM if it is still alive, otherwise "
        "create it under that name and leave it running. Skips the "
        "allocation queue entirely on every run after the first, which "
        "is the difference between starting work and waiting for "
        "capacity. Implies --reuse-session and --keep-session, so the "
        "VM bills until you run `colab stop -s NAME`, and every run "
        "inherits the last one's pip installs and filesystem. A live "
        "session whose hardware or shape (Standard / High-RAM) differs "
        "from --gpu is refused, not reused and not overwritten.",
    )
    parser.add_argument(
        "--keep-session",
        action = "store_true",
        help = "Colab: do not stop the VM when the run ends. It keeps billing "
        "until you run `colab stop`. Off by default for a reason, and it "
        "implies --reuse-session so only one VM can be stranded.",
    )
    parser.add_argument(
        "--reuse-session",
        action = "store_true",
        help = "Colab: run every notebook on one VM instead of a fresh one each "
        "time. Faster, but notebook 2 inherits notebook 1's pip installs, "
        "which can fabricate a failure that looks like a real regression.",
    )
    parser.add_argument(
        "--pack",
        type = int,
        default = 1,
        metavar = "N",
        help = "Kaggle: notebooks per kernel (default: 1). Packing trades "
        "queue waits for isolation -- packed notebooks share one "
        "site-packages, with the same hazard as --reuse-session.",
    )
    parser.add_argument(
        "--parallel-gpus",
        type = int,
        default = 1,
        metavar = "N",
        help = "Kaggle: run the notebooks packed into one kernel N at a time, notebook k on "
        "GPU k %% N via its kernel's CUDA_VISIBLE_DEVICES (T4x2: N=2, so both cards "
        "work and the wall-time-billed kernel ends sooner). Each notebook should "
        "isolate its own installs; they share site-packages. Default 1 = sequential.",
    )
    parser.add_argument(
        "--non-interactive",
        action = "store_true",
        help = "never prompt; fail with instructions if a credential is missing",
    )
    parser.add_argument(
        "--kaggle-username",
        default = None,
        help = "Kaggle username (with --kaggle-key, the legacy kaggle.json pair)",
    )
    parser.add_argument("--kaggle-key", default = None, help = "Kaggle legacy API key")
    parser.add_argument(
        "--kaggle-token",
        default = None,
        help = "Kaggle bearer token (KGAT_...) from Settings -> API -> Generate "
        "New Token; also read from KAGGLE_API_TOKEN",
    )
    parser.add_argument(
        "--poll-interval",
        type = int,
        default = 30,
        help = "Kaggle: seconds between status polls (default: 30)",
    )
    parser.add_argument(
        "--keep-kernel",
        action = "store_true",
        help = "Kaggle: never delete the kernel. By default it is deleted once its output is "
        "downloaded and parsed, and when the run is interrupted / overruns / sits QUEUED "
        "past --kaggle-queue-timeout (nobody would collect it, and Kaggle bills wall time)",
    )
    parser.add_argument(
        "--kaggle-queue-timeout",
        type = int,
        default = KaggleBackend.DEFAULT_QUEUE_TIMEOUT,
        metavar = "S",
        help = "Kaggle: delete a kernel still QUEUED after S seconds (default: %d)"
        % KaggleBackend.DEFAULT_QUEUE_TIMEOUT,
    )
    parser.add_argument(
        "--kaggle-sweep",
        action = "store_true",
        help = "Kaggle: list this workspace's unsloth-nbrun-* kernels (those recorded in run dirs "
        "under --sweep-root and in this user's index) and their status, then exit. DRY RUN "
        "unless --yes. Deletes finished kernels whose output was collected; --force also "
        "uncollected finished ones; --include-orphans also QUEUED/RUNNING ones whose runner "
        "on this host is dead. Never touches a kernel it has no local record of.",
    )
    parser.add_argument(
        "--sweep-root",
        action = "append",
        default = None,
        metavar = "DIR",
        help = "--kaggle-sweep: run-dir root to scan (repeatable; default: the "
        "directory holding this script)",
    )
    parser.add_argument(
        "--force",
        action = "store_true",
        help = "--kaggle-sweep: also delete finished kernels whose output was not collected",
    )
    parser.add_argument(
        "--include-orphans",
        action = "store_true",
        help = "--kaggle-sweep: also delete QUEUED/RUNNING kernels whose runner is dead",
    )
    parser.add_argument(
        "--kaggle-gc",
        action = "store_true",
        help = "Kaggle: on every KAGGLE_API_TOKEN* account, list this runner's kernels "
        "(unsloth-nbrun-*) not run for --gc-days and delete the finished ones. DRY RUN unless "
        "--yes. Launches start it automatically at most every 6 h (NBRUN_KAGGLE_GC=0 disables).",
    )
    parser.add_argument(
        "--gc-days",
        type = float,
        default = KAGGLE_GC_DAYS,
        metavar = "D",
        help = "--kaggle-gc: idle days before a finished kernel is deleted (default: %(default)g)",
    )
    parser.add_argument("--quiet", action = "store_true", help = "less chatter")
    return parser


def default_gpu(backend: str) -> str:
    return "T4" if backend == "colab" else "T4x2"


def make_workdir(outdir: Optional[str]) -> Path:
    if outdir:
        path = Path(outdir).expanduser()
        path.mkdir(parents = True, exist_ok = True)
        return path.resolve()
    base = Path(tempfile.mkdtemp(prefix = "unsloth-nbrun-%s-" % time.strftime("%Y%m%d-%H%M%S")))
    return base.resolve()


def prepare_notebooks(plan: Plan, workdir: Path) -> List[Tuple[PlannedNotebook, Path]]:
    staged = workdir / "input"
    prepared: List[Tuple[PlannedNotebook, Path]] = []
    for item in plan.notebooks:
        log("  fetching %s" % item.source.describe())
        local = fetch_notebook(item.source, staged)
        nb = read_notebook(local)
        if plan.smoke_patch:
            nb, changes = smoke_patch_notebook(
                nb, plan.max_steps, plan.grpo_max_steps, plan.extra_env
            )
            for change in changes[:6]:
                log("    %s" % change)
            if len(changes) > 6:
                log("    ... and %d more" % (len(changes) - 6))
        else:
            nb = json.loads(json.dumps(nb))
            nb.setdefault("cells", []).insert(0, build_bootstrap_cell(plan.extra_env))
            ensure_cell_ids(nb)
        patched = staged / ("%s.run.ipynb" % item.output_name)
        patched.write_text(json.dumps(nb, indent = 1), encoding = "utf-8")
        prepared.append((item, patched))
    return prepared


def install_teardown_handlers(backend: Backend) -> None:
    """Belt, braces, and a third belt. A leaked VM bills until the 24h cap."""
    atexit.register(backend.release, "interpreter exit")

    def _handler(signum, _frame):  # noqa: ANN001
        try:
            name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = str(signum)
        _safe_print("\nreceived %s -- releasing remote resources before exiting" % name, sys.stderr)
        try:
            backend.release("signal %s" % name)
        finally:
            sys.exit(130)

    for signame in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform has no such signal.
            pass


def _pid_alive(pid: object) -> bool:
    try:
        pid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _owner_is_running(owner: dict, alive = _pid_alive) -> bool:
    """The recorded owner is still this run (not a recycled pid)."""
    if not alive(owner.get("pid")):
        return False
    cmdline = Path("/proc/%s/cmdline" % owner.get("pid"))
    if cmdline.exists():
        with contextlib.suppress(OSError):
            return b"notebook_cloud_run" in cmdline.read_bytes()
    return True


def find_orphan_sessions(
    roots: Sequence[Path] = (),
    use_registry: bool = True,
    alive = _pid_alive,
) -> List[dict]:
    """Unnamed `unsloth-nbrun-*` sessions left in run dirs whose owner is gone.

    Only per-run state files (`<run dir>/colab-session-state.json`) are read:
    named --session state lives elsewhere and is kept on purpose, and the
    CLI's default sessions.json (browser / hand-made sessions) is never read.
    A run dir with no owner record (made before owners were recorded) counts
    only when the CLI's keep-alive process for it is dead too.
    """
    dirs: List[Path] = []
    if use_registry:
        with contextlib.suppress(OSError):
            for line in colab_runs_registry().read_text(encoding = "utf-8").splitlines():
                if line.strip():
                    dirs.append(Path(line.strip()))
    for root in roots:
        root = Path(root)
        if (root / COLAB_RUN_STATE).exists():
            dirs.append(root)
        with contextlib.suppress(OSError):
            dirs.extend(p.parent for p in root.rglob(COLAB_RUN_STATE))
    host = socket.gethostname()
    found: List[dict] = []
    seen = set()
    for d in dirs:
        state = d / COLAB_RUN_STATE
        try:
            key = state.resolve()
        except OSError:
            continue
        if key in seen or not state.exists():
            continue
        seen.add(key)
        try:
            sessions = json.loads(state.read_text(encoding = "utf-8") or "{}")
        except (OSError, ValueError):
            continue
        owner: dict = {}
        with contextlib.suppress(OSError, ValueError):
            owner = json.loads((d / COLAB_OWNER_FILE).read_text(encoding = "utf-8"))
        for name, entry in sessions.items() if isinstance(sessions, dict) else []:
            if not str(name).startswith(COLAB_RUN_PREFIX) or not isinstance(entry, dict):
                continue
            if owner:
                if owner.get("host") and owner.get("host") != host:
                    continue  # another machine's run: not ours to judge
                if _owner_is_running(owner, alive):
                    continue
                why = "owner pid %s is gone" % owner.get("pid")
            else:
                if alive(entry.get("keep_alive_pid")):
                    continue
                why = "no owner record and its keep-alive is gone"
            found.append(
                {
                    "session": name,
                    "state_file": str(state),
                    "run_dir": str(d),
                    "endpoint": entry.get("endpoint"),
                    "accelerator": entry.get("accelerator"),
                    "auth": owner.get("auth") or "adc",
                    "colab_home": owner.get("colab_home"),
                    "why": why,
                }
            )
    return found


def _server_endpoints(auth: str, colab_home: Optional[str]) -> Optional[set]:
    """Endpoints `colab sessions` lists for this account, or None when unknown."""
    cli = colab_cli()
    if not cli:
        return None
    cmd = [cli] + (["--auth", auth] if auth != "adc" else []) + ["sessions"]
    env = subprocess_env({"HOME": colab_home} if colab_home else None)
    res = run_capture(cmd, timeout = 60, env = env)
    text = res.output or ""
    if res.returncode != 0:
        return None
    ends = set(re.findall(r"^\[[^\]]*\]\s+(\S+)\s*\|", text, re.MULTILINE))
    if not ends and "no active session" not in text.lower():
        return None
    return ends


def cmd_reclaim_orphans(roots: Sequence[str], yes: bool) -> int:
    """List (and with --yes stop) orphaned unnamed sessions. Exit 0 clean, 1 orphans left."""
    orphans = find_orphan_sessions([Path(r).expanduser() for r in roots])
    if not orphans:
        print("no orphaned %s* sessions in the recorded run dirs" % COLAB_RUN_PREFIX)
        return 0
    server: Dict[Tuple[str, Optional[str]], Optional[set]] = {}
    left = 0
    for o in orphans:
        key = (o["auth"], o["colab_home"])
        if key not in server:
            server[key] = _server_endpoints(*key)
        live = server[key]
        status = (
            "unknown"
            if live is None
            else "LIVE on the server"
            if o["endpoint"] in live
            else "not listed by the server (local state only)"
        )
        base = [colab_cli() or "colab", "--config", o["state_file"]] + (
            ["--auth", o["auth"]] if o["auth"] != "adc" else []
        )
        stop_cmd = base + ["stop", "-s", o["session"]]
        print(
            "%s  %s  %s  [%s; %s]\n    %s"
            % (
                o["session"],
                o["accelerator"] or "?",
                o["run_dir"],
                o["why"],
                status,
                " ".join(shlex.quote(a) for a in stop_cmd),
            )
        )
        if live is not None and o["endpoint"] not in live:
            print("    nothing to stop: the server no longer lists this VM")
            continue
        if not yes:
            left += 1
            continue
        env = subprocess_env({"HOME": o["colab_home"]} if o["colab_home"] else None)
        res = run_capture(stop_cmd, timeout = 300, env = env)
        out = (res.output or "").lower()
        if res.returncode == 0 or "not found" in out:
            print("    stopped")
        else:
            left += 1
            print("    stop FAILED: %s" % (res.output or "")[:200])
    if not yes:
        print("\n%d orphan(s); rerun with --yes to stop them" % left)
    return 1 if left else 0


def cmd_list_gpus() -> int:
    print("colab   %s" % ", ".join(COLAB_GPUS))
    print("        Availability depends on your Colab subscription. An")
    print("        unrecognised value is silently downgraded by the CLI, so")
    print("        this script validates the tier locally first.")
    print("kaggle  T4x2 (two NvidiaTeslaT4), P100, TPU, CPU")
    print("        2 concurrent GPU kernels per account; see `kaggle quota`.")
    return 0


def cmd_check_auth(args: argparse.Namespace) -> int:
    if args.backend == "colab":
        status = check_colab_auth()
        if status.ok:
            print("Colab: authenticated. `colab sessions` succeeded.")
            if status.detail:
                print(status.detail)
            return 0
        print("Colab: NOT usable -- %s" % status.reason, file = sys.stderr)
        if status.detail:
            print("\nThe CLI said:\n  %s" % status.detail, file = sys.stderr)
        print("\n%s" % status.instructions(), file = sys.stderr)
        return 3 if status.kind == "missing_cli" else 2

    creds = resolve_kaggle_credentials(
        username = args.kaggle_username,
        key = args.kaggle_key,
        token = args.kaggle_token,
        interactive = not args.non_interactive,
        # --check-auth is "check, then exit". It has no business persisting
        # anything, least of all an API key.
        save = False,
    )
    ok, detail, username = check_kaggle_auth(creds)
    if ok:
        creds.username = username or creds.username
        print("Kaggle: authenticated as %s" % creds.redacted())
        return 0
    print("Kaggle: NOT usable -- %s" % detail, file = sys.stderr)
    return 2


def cmd_quota(
    env: Optional[Mapping[str, str]] = None, ledger: Optional[KaggleUsageLedger] = None
) -> int:
    """Kaggle hours per token from the local ledger (Kaggle has no quota API; runs made elsewhere
    are not counted) and `colab status` (Colab has none either: live sessions are the signal)."""
    env = os.environ if env is None else env
    ledger = ledger or KaggleUsageLedger()
    print("Kaggle (this tool's ledger, 7 days):")
    names = kaggle_token_env_names(env)
    for name in names:
        hours = kaggle_weekly_hours(name)
        used = ledger.used_hours(env[name].strip())
        sat = "  saturated (benched)" if ledger.saturated(env[name].strip()) else ""
        print("  %-20s %5.1f / %g h (%.0f%%)%s" % (name, used, hours, 100 * used / hours, sat))
        live = kaggle_quota(env[name].strip())
        if live:
            print(
                "  %-20s live (kaggle quota): %.2f used, %.2f left of %.0f h%s"
                % (
                    "",
                    live["used_h"],
                    live["remaining_h"],
                    live["total_h"],
                    ", refreshes %s UTC"
                    % time.strftime("%Y-%m-%d %H:%M", time.gmtime(live["refresh_at"]))
                    if live.get("refresh_at")
                    else "",
                )
            )
    if not names:
        print("  no KAGGLE_API_TOKEN* set")
    cli = colab_cli()
    print("Colab live sessions (3 parallel per tier):")
    if not cli:
        print("  colab CLI not found")
        return 0
    res = run_capture([cli, "status"], timeout = 60)
    noise = ("new version of Colab CLI", "Run 'colab update'", "To silence this check")
    lines = [
        ln
        for ln in (res.output or "").splitlines()
        if ln.strip() and not any(n in ln for n in noise)
    ]
    for line in lines or ["(none)"]:
        print("  " + line)
    return 0


_SWEEP_SKIP_DIRS = {
    "lib",
    "lib64",
    "bin",
    "include",
    "share",
    "site-packages",
    "node_modules",
    ".git",
    "__pycache__",
    ".cache",
    "hf_home",
    "unsloth_compiled_cache",
    "ms-playwright",
}


def find_recorded_kaggle_kernels(
    roots: Sequence[Path], max_depth: int = 6
) -> Dict[str, Dict[str, object]]:
    """kernel id -> record, for unsloth-nbrun-* kernels recorded under `roots`: run-dir records
    (kaggle_kernels.json), legacy run dirs (kernels/<slug>/kernel-metadata.json), and this
    user's index entries whose workdir lies under a root. Nothing else is ever returned."""
    roots = [Path(r).resolve() for r in roots]
    found: Dict[str, Dict[str, object]] = {}

    def _under(path) -> bool:
        with contextlib.suppress(Exception):
            rp = Path(str(path)).resolve()
            return any(rp == r or r in rp.parents for r in roots)
        return False

    for kid, rec in load_kaggle_index().items():
        if _under(rec.get("workdir", "")):
            found[kid] = dict(rec)
    for root in roots:
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            if len(here.parts) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _SWEEP_SKIP_DIRS and not d.startswith(("venv", ".venv"))
            ]
            if KAGGLE_RECORD_NAME in filenames:
                with contextlib.suppress(OSError, ValueError):
                    for kid, rec in json.loads(
                        (here / KAGGLE_RECORD_NAME).read_text(encoding = "utf-8")
                    ).items():
                        found[kid] = dict(found.get(kid, {}), **rec)
            if here.name == "kernels":
                for slug in [d for d in dirnames if d.startswith("unsloth-nbrun-")]:
                    meta = here / slug / "kernel-metadata.json"
                    with contextlib.suppress(OSError, ValueError):
                        kid = json.loads(meta.read_text(encoding = "utf-8"))["id"]
                        rec = found.setdefault(
                            kid, {"kernel_id": kid, "workdir": str(here.parent), "state": "legacy"}
                        )
                        rec.setdefault("slug", slug)
                dirnames[:] = []
    return {k: v for k, v in found.items() if k.split("/")[-1].startswith("unsloth-nbrun-")}


def _kernel_collected(rec: Mapping[str, object]) -> bool:
    if rec.get("state") in ("collected",):
        return True
    slug = str(rec.get("slug") or str(rec.get("kernel_id", "")).split("/")[-1])
    out = Path(str(rec.get("workdir", ""))) / "kaggle-output" / slug
    return bool(out.is_dir() and KaggleBackend._parseable_outputs(out))


def kaggle_sweep(
    roots: Sequence[Path],
    yes: bool = False,
    force: bool = False,
    include_orphans: bool = False,
    env: Optional[Mapping[str, str]] = None,
    runner = None,
) -> List[Dict[str, object]]:
    """Status every recorded kernel with each candidate token (the record's own first), and
    delete per the rules in --kaggle-sweep's help. Returns one row per kernel."""
    env = os.environ if env is None else env
    runner = runner or run_capture
    cli = kaggle_cli() or "kaggle"
    tokens = [(n, (env.get(n) or "").strip()) for n in kaggle_token_env_names(env)]
    rows = []
    for kid, rec in sorted(find_recorded_kaggle_kernels(roots).items()):
        order = sorted(tokens, key = lambda nt: nt[0] != rec.get("token_env"))
        status, used = "GONE", None
        for name, tok in order or [(None, "")]:
            kenv = subprocess_env({"KAGGLE_API_TOKEN": tok} if tok else {})
            res = runner([cli, "kernels", "status", kid], timeout = 120, env = kenv)
            m = KaggleBackend._STATUS_RE.search(res.output or "")
            if m:
                status, used = m.group(1), (name, kenv)
                break
        row = {
            "kernel_id": kid,
            "status": status,
            "token_env": used[0] if used else None,
            "workdir": rec.get("workdir"),
            "action": "none",
        }
        if rec.get("state") == "deleted" and status == "GONE":
            row["action"] = "already deleted"
        elif status == "GONE":
            row["action"] = "not visible to any token (deleted, or another account's)"
        elif status in KAGGLE_TERMINAL:
            if _kernel_collected(rec) or force:
                row["action"] = "delete"
            else:
                row["action"] = "keep: output not collected (--force deletes)"
        elif kaggle_record_live(rec):
            row["action"] = "keep: a live runner (pid %s) is driving it" % rec.get("pid")
        elif include_orphans:
            row["action"] = "delete (orphan)"
        else:
            row["action"] = "keep: %s with no live runner (--include-orphans deletes)" % status
        if row["action"].startswith("delete") and used:
            if yes:
                res = runner([cli, "kernels", "delete", "-y", kid], timeout = 180, env = used[1])
                ok = "deleted successfully" in (res.output or "").lower()
                row["action"] += ": done" if ok else ": FAILED %s" % (res.output or "")[:200]
                if ok and rec.get("workdir"):
                    with contextlib.suppress(Exception):
                        record_kaggle_kernel(
                            Path(str(rec["workdir"])),
                            kid,
                            state = "deleted",
                            deleted_why = "--kaggle-sweep",
                        )
            else:
                row["action"] += " (dry run; --yes deletes)"
        rows.append(row)
    return rows


# ---- Account-wide GC of old finished runner kernels -------------------------------
#
# --kaggle-sweep only knows kernels this host recorded. Finished kernels from other hosts, older
# runs and killed runners pile up on the accounts, so --kaggle-gc lists each account's own kernels
# and deletes the runner's (slug `unsloth-nbrun-*`) once finished (COMPLETE / ERROR / cancelled)
# and not run for NBRUN_KAGGLE_GC_DAYS (default 3). Anything QUEUED / RUNNING, any other notebook,
# and any kernel whose status cannot be read are left alone. Launches start it in the background
# at most every KAGGLE_GC_EVERY_S, host-wide (maybe_spawn_kaggle_gc; NBRUN_KAGGLE_GC=0 disables).
KAGGLE_GC_DAYS = float(os.environ.get("NBRUN_KAGGLE_GC_DAYS") or 3)
KAGGLE_GC_EVERY_S = 6 * 3600
KAGGLE_GC_PREFIX = "unsloth-nbrun-"


def _kaggle_run_time(text: str) -> Optional[float]:
    """`2026-10-02 19:38:05.923000` (UTC, as `kernels list --csv` prints it) -> epoch seconds."""
    import calendar

    with contextlib.suppress(ValueError, TypeError):
        return float(calendar.timegm(time.strptime(str(text).strip()[:19], "%Y-%m-%d %H:%M:%S")))
    return None


def _kaggle_list_mine(
    cli: str,
    kenv: Dict[str, str],
    runner,
    pages: int = 50,
) -> List[Dict[str, str]]:
    """Every page of `kernels list --mine`. A page can hold fewer than --page-size rows with more
    pages after it (77, 83, 85, 59 at size 100, live), so stop on an empty page or one with no new
    refs, never on a short one."""
    import csv
    import io

    rows: List[Dict[str, str]] = []
    seen = set()
    for page in range(1, pages + 1):
        res = runner(
            [
                cli,
                "kernels",
                "list",
                "--mine",
                "--page-size",
                "100",
                "--page",
                str(page),
                "--sort-by",
                "dateRun",
                "--csv",
            ],
            timeout = 120,
            env = kenv,
        )
        text = res.output or ""
        if res.returncode != 0 or "ref," not in text:
            break
        got = [
            r
            for r in csv.DictReader(io.StringIO(text[text.index("ref,") :]))
            if r.get("ref") and r["ref"] not in seen
        ]
        if not got:
            break
        seen.update(r["ref"] for r in got)
        rows.extend(got)
    return rows


def _kaggle_call(
    runner,
    cmd,
    kenv,
    sleep = time.sleep,
    tries = 4,
    wait_s = 20.0,
):
    """`runner(cmd)`, retried after a pause while Kaggle answers 429 Too Many Requests."""
    res = runner(cmd, timeout = 120, env = kenv)
    for k in range(tries - 1):
        if "429" not in (res.output or "") and "Too Many Requests" not in (res.output or ""):
            break
        sleep(wait_s * (k + 1))
        res = runner(cmd, timeout = 120, env = kenv)
    return res


def kaggle_gc(
    days: float = KAGGLE_GC_DAYS,
    yes: bool = False,
    env: Optional[Mapping[str, str]] = None,
    runner = None,
    now: Optional[float] = None,
    sleep = time.sleep,
) -> List[Dict[str, object]]:
    """One row per runner kernel older than `days` on every KAGGLE_API_TOKEN* account; with
    `yes`, finished ones are deleted. Never touches other notebooks or live kernels."""
    env = os.environ if env is None else env
    runner = runner or run_capture
    now = time.time() if now is None else now
    cli = kaggle_cli() or "kaggle"
    rows: List[Dict[str, object]] = []
    seen_tokens = set()
    for name in kaggle_token_env_names(env):
        tok = (env.get(name) or "").strip()
        if not tok or tok in seen_tokens:
            continue
        seen_tokens.add(tok)
        kenv = subprocess_env({"KAGGLE_API_TOKEN": tok})
        for k in ("KAGGLE_USERNAME", "KAGGLE_KEY"):
            kenv.pop(k, None)
        for r in _kaggle_list_mine(cli, kenv, runner):
            ref = r["ref"].strip()
            if not ref.split("/")[-1].startswith(KAGGLE_GC_PREFIX):
                continue
            ran = _kaggle_run_time(r.get("lastRunTime", ""))
            if ran is None or now - ran < days * 86400:
                continue
            res = _kaggle_call(runner, [cli, "kernels", "status", ref], kenv, sleep = sleep)
            m = KaggleBackend._STATUS_RE.search(res.output or "")
            status = (
                m.group(1) if m else ("RATE_LIMITED" if "429" in (res.output or "") else "UNKNOWN")
            )
            row = {
                "kernel_id": ref,
                "token_env": name,
                "status": status,
                "idle_days": round((now - ran) / 86400, 1),
                "action": "keep",
            }
            if status in KAGGLE_TERMINAL:
                row["action"] = "delete" if yes else "would delete"
                if yes:
                    d = _kaggle_call(
                        runner, [cli, "kernels", "delete", "-y", ref], kenv, sleep = sleep
                    )
                    row["action"] = (
                        "deleted"
                        if d.returncode == 0
                        else "delete failed: %s" % (d.output or "")[-160:]
                    )
            else:
                row["action"] = "keep: %s" % status
            rows.append(row)
    return rows


def _gc_stamp_dir() -> Path:
    shared = (
        Path(
            os.environ.get("STUDIO_REGRESS_LOCK_DIR")
            or "/mnt/disks/unslothai/shared/studio-regress-locks"
        )
        / "switchboard"
    )
    return shared if shared.is_dir() and os.access(shared, os.W_OK) else colab_state_home()


def maybe_spawn_kaggle_gc(now: Optional[float] = None, spawn = None) -> bool:
    """Start `--kaggle-gc --yes` detached, at most once per KAGGLE_GC_EVERY_S host-wide (stamp under
    flock in the shared switchboard dir). Off under pytest and with NBRUN_KAGGLE_GC=0."""
    import fcntl

    if os.environ.get("NBRUN_KAGGLE_GC", "1") == "0" or (
        "PYTEST_CURRENT_TEST" in os.environ and spawn is None
    ):
        return False
    now = time.time() if now is None else now
    base = _gc_stamp_dir()
    base.mkdir(parents = True, exist_ok = True)
    stamp = base / "kaggle_gc.stamp"
    with open(str(stamp) + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            last = float(stamp.read_text().strip())
        except (OSError, ValueError):
            last = None  # never ran (or unreadable stamp): run now
        if last is not None and now - last < KAGGLE_GC_EVERY_S:
            return False
        stamp.write_text(str(now))
    logs = Path(os.environ.get("WORKSPACE") or Path(__file__).resolve().parents[1]) / "logs"
    logs.mkdir(parents = True, exist_ok = True)
    log = logs / ("kaggle_gc_%s.log" % time.strftime("%Y%m%d_%H%M%S", time.localtime(now)))
    argv = [sys.executable, str(Path(__file__).resolve()), "--kaggle-gc", "--yes"]
    if spawn:
        spawn(argv, log)
    else:
        with open(log, "ab") as fh:
            subprocess.Popen(
                argv,
                stdin = subprocess.DEVNULL,
                stdout = fh,
                stderr = subprocess.STDOUT,
                start_new_session = True,
                close_fds = True,
            )
    return True


def cmd_kaggle_gc(args: argparse.Namespace) -> int:
    rows = kaggle_gc(days = args.gc_days, yes = args.yes)
    for r in rows:
        print(
            "%-55s %-18s %-9s idle %5.1f d  %s"
            % (r["kernel_id"], r["token_env"], r["status"], r["idle_days"], r["action"])
        )
    dele = sum(1 for r in rows if r["action"] in ("deleted", "would delete"))
    print(
        "%d runner kernel(s) idle >= %g days; %s %d%s"
        % (
            len(rows),
            args.gc_days,
            "deleted" if args.yes else "would delete",
            dele,
            "" if args.yes else "  (dry run: add --yes)",
        )
    )
    return 1 if any(str(r["action"]).startswith("delete failed") for r in rows) else 0


def cmd_kaggle_sweep(args: argparse.Namespace) -> int:
    roots = [Path(r) for r in (args.sweep_root or [str(Path(__file__).resolve().parent)])]
    rows = kaggle_sweep(roots, yes = args.yes, force = args.force, include_orphans = args.include_orphans)
    for r in rows:
        print(
            "%-48s %-20s %-20s %s"
            % (r["kernel_id"], r["status"], r["token_env"] or "-", r["action"])
        )
    if not rows:
        print("no recorded unsloth-nbrun-* kernels under %s" % ", ".join(map(str, roots)))
    return 1 if any("FAILED" in r["action"] for r in rows) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _QUIET
    parser = build_parser()
    args = parser.parse_args(argv)
    _QUIET = args.quiet

    if args.list_gpus:
        return cmd_list_gpus()
    if getattr(args, "reclaim_orphans", False):
        return cmd_reclaim_orphans(args.reclaim_root, args.yes)
    if args.quota:
        return cmd_quota()
    if args.kaggle_sweep:
        return cmd_kaggle_sweep(args)
    if getattr(args, "kaggle_gc", False):
        return cmd_kaggle_gc(args)
    if args.backend == "kaggle" and not args.dry_run:
        with contextlib.suppress(Exception):  # housekeeping never blocks a launch
            maybe_spawn_kaggle_gc()
    if args.gpu is None:
        args.gpu = default_gpu(args.backend)

    backend: Optional[Backend] = None
    try:
        if args.check_auth:
            return cmd_check_auth(args)

        if not args.notebooks:
            parser.error(
                "give at least one notebook (a local path, a name from "
                "unslothai/notebooks, or a URL), or use --check-auth / "
                "--list-gpus"
            )

        workdir = make_workdir(args.outdir)
        plan = build_plan(args, workdir)

        if args.dry_run:
            print(render_plan(plan))
            return 0

        log("Working directory: %s" % workdir)
        if args.backend == "colab":
            backend = ColabBackend(
                workdir,
                keep_session = plan.keep_session,
                auth_provider = args.colab_auth,
                session_name = plan.session_name,
            )
        else:
            creds = resolve_kaggle_credentials(
                username = args.kaggle_username,
                key = args.kaggle_key,
                token = args.kaggle_token,
                interactive = not args.non_interactive,
            )
            backend = KaggleBackend(
                workdir,
                creds,
                poll_interval = args.poll_interval,
                keep_kernel = args.keep_kernel,
                queue_timeout = args.kaggle_queue_timeout,
            )

        backend.preflight()
        install_teardown_handlers(backend)

        log("")
        log("Preparing %d notebook(s)" % len(plan.notebooks))
        prepared = prepare_notebooks(plan, workdir)

        try:
            results = backend.run(plan, prepared)
        finally:
            backend.release("run finished")

        print_report(results, workdir)
        write_report(results, plan, workdir / "report.json")

        if any(r.verdict.status in INFRA_STATUSES for r in results):
            return 3
        return 0 if all(r.verdict.passed for r in results) else 1

    except CredentialError as exc:
        print("\nCREDENTIAL ERROR\n%s" % exc, file = sys.stderr)
        return 2
    except UsageError as exc:
        # Deliberately NOT exit 3. See UsageError's docstring: a retry loop
        # that waits on 3 must not wait on a typo.
        print("\nUSAGE ERROR\n%s" % exc, file = sys.stderr)
        return 2
    except InfraError as exc:
        print("\nINFRASTRUCTURE ERROR\n%s" % exc, file = sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted", file = sys.stderr)
        return 130
    finally:
        if backend is not None:
            backend.release("exiting")


if __name__ == "__main__":
    sys.exit(main())
