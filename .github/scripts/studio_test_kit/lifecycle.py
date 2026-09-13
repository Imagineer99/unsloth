"""Install + launch Unsloth Studio for arbitrary branches/ports.

The pre-PR vs post-PR test pattern installs Studio twice, once per branch,
each pinned to its own UNSLOTH_STUDIO_HOME so the two installs share
nothing (separate `.venv_t5_*`, `auth/`, `studio.db`, llama.cpp build).

`install_studio(branch=..., home=...)` clones unslothai/unsloth (or
re-uses an existing clone), checks out the branch, then runs
`./install.sh --local` with UNSLOTH_STUDIO_HOME exported.

`launch_studio(install, port=..., log_path=...)` starts `unsloth studio
-p <port>` detached via setsid and tails the log for the bootstrap
password (Studio prints it on first run).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StudioInstall:
    """Where a Studio install lives + the credentials it minted on first run."""

    home: Path                   # UNSLOTH_STUDIO_HOME
    repo: Path                   # clone of unslothai/unsloth
    branch: str
    bootstrap_password: Optional[str] = None
    port: Optional[int] = None
    pid: Optional[int] = None


def _run(cmd: str | list[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
         check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = cmd
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd_list, cwd=cwd, env=full_env, check=check, timeout=timeout,
        text=True, capture_output=True,
    )


def install_studio(
    branch: str,
    home: Path,
    repo: Optional[Path] = None,
    remote: str = "https://github.com/unslothai/unsloth",
    reuse_clone: bool = True,
) -> StudioInstall:
    """Clone (or re-use) the repo at `branch`, then run `./install.sh --local`.

    `home` is exported as UNSLOTH_STUDIO_HOME for the install. After this
    returns, `home/.venv_t5_550/`, `home/auth/`, etc. exist.
    """
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    repo = (repo or (home.parent / f"{home.name}_repo")).resolve()

    if reuse_clone and (repo / ".git").exists():
        _run(["git", "fetch", "origin", branch], cwd=repo)
        _run(["git", "checkout", branch], cwd=repo)
        _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo)
    else:
        if repo.exists():
            shutil.rmtree(repo)
        _run(["git", "clone", "--branch", branch, remote, str(repo)])

    env = {"UNSLOTH_STUDIO_HOME": str(home)}
    if os.name == "nt":
        install_ps1 = repo / "install.ps1"
        if not install_ps1.exists():
            raise FileNotFoundError(f"install.ps1 missing at {install_ps1}")
        _run(
            ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(install_ps1), "--local"],
            cwd=repo,
            env=env,
            timeout=60 * 30,
        )
    else:
        install_sh = repo / "install.sh"
        if not install_sh.exists():
            raise FileNotFoundError(f"install.sh missing at {install_sh}")
        _run(["bash", str(install_sh), "--local"], cwd=repo, env=env, timeout=60 * 30)
    return StudioInstall(home=home, repo=repo, branch=branch)


def _find_unsloth_bin(install: StudioInstall) -> str:
    """Return the absolute path to the `unsloth` CLI inside the install."""
    candidates = [
        install.home / "bin" / "unsloth",
        install.home / "bin" / "unsloth.exe",
        install.home / "unsloth_studio" / "bin" / "unsloth",
        install.home / "unsloth_studio" / "Scripts" / "unsloth.exe",
        install.home / ".venv_t5_550" / "bin" / "unsloth",
        install.home / ".venv_t5_530" / "bin" / "unsloth",
    ]
    candidates.extend(install.home.glob(".venv*/*/unsloth"))
    candidates.extend(install.home.glob(".venv*/Scripts/unsloth.exe"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"`unsloth` CLI not found under {install.home}")


# Password log line shapes seen in practice:
#   "Bootstrap password: secret"
#   "Initial password = secret"
#   "Generated password is secret"
#   "bootstrap password is: secret"
# The mandatory `\s+` before the value and the EXPLICIT `[:=]?` separator
# (rather than `[:\s]+` greedy class) stop the regex from backtracking
# to capture `=` itself as the password.
_PW_RE = re.compile(
    r"(?i)(?:bootstrap|initial|generated)\s*password"
    r"(?:\s+is)?\s*[:=]?\s+(\S+)"
)


def _read_password_from_log(log_path: Path, deadline: float) -> Optional[str]:
    """Tail the log file until the password line appears or `deadline` passes."""
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text(errors="ignore")
            m = _PW_RE.search(text)
            if m:
                return m.group(1).strip().strip(".,")
        time.sleep(0.5)
    return None


def launch_studio(
    install: StudioInstall,
    port: int,
    log_path: Path,
    extra_env: Optional[dict] = None,
    wait_for_healthz: bool = True,
    healthz_timeout_s: int = 180,
    password_timeout_s: int = 30,
    timeout_s: Optional[int] = None,
) -> StudioInstall:
    """Start `unsloth studio -p <port>` detached. Updates `install` in place
    with `port`, `pid`, and `bootstrap_password` (parsed from the log).

    Two INDEPENDENT timeouts:
      - `password_timeout_s`: how long to wait for the bootstrap password
        line. Relaunching an existing install often skips reprinting the
        password, so this should be SHORT (default 30s).
      - `healthz_timeout_s`: how long to wait for `/healthz` to return 200.
        Studio cold-start can take a couple of minutes (default 180s).

    `timeout_s` is accepted for backward compatibility and overrides
    `healthz_timeout_s` if set. With the legacy single-deadline behavior
    a quiet log starved the healthz check and raised a spurious
    TimeoutError even when Studio was up.
    """
    if timeout_s is not None:
        healthz_timeout_s = timeout_s
    log_path = Path(log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")

    bin_path = _find_unsloth_bin(install)
    env = {"UNSLOTH_STUDIO_HOME": str(install.home), **(extra_env or {})}
    env.pop("STUDIO_HOME", None)
    cmd = [bin_path, "studio", "-H", "127.0.0.1", "-p", str(port)]
    log_handle = log_path.open("a", encoding="utf-8")
    kwargs: dict = {
        "env": {**os.environ, **env},
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    log_handle.close()

    install.port = port
    install.pid = proc.pid
    install.bootstrap_password = _read_password_from_log(
        log_path, time.time() + password_timeout_s
    )

    if wait_for_healthz:
        import urllib.request
        url = f"http://127.0.0.1:{port}/healthz"
        healthz_deadline = time.time() + healthz_timeout_s
        while time.time() < healthz_deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise TimeoutError(
                f"Studio on :{port} did not pass /healthz within "
                f"{healthz_timeout_s}s"
            )

    return install


def stop_studio(install: StudioInstall) -> None:
    """Best-effort terminate the launched Studio process group."""
    if install.pid:
        try:
            if os.name == "nt":
                os.kill(install.pid, signal.SIGTERM)
            else:
                os.killpg(os.getpgid(install.pid), signal.SIGTERM)
        except Exception:
            pass
