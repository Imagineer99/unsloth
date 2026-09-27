# Unsloth Windows installer diagnostic

One script that checks the pending Windows installer changes on a real machine and packs everything
into a single zip. It compares four pinned versions of the installer:

| state | what it is |
|---|---|
| `base` | current `main` (the merge base) |
| `stack` | the installer stack (#11104 -> #11115 -> #11116 -> #11173 -> #11193) |
| `presence` | #11166 (NVIDIA adapter presence) on `main` |
| `combined` | the stack and #11166 merged together |

Each version is downloaded as a pinned commit archive and checked against SHA-256 hashes built into the
script, so no git is needed and nothing unverified runs.

## Run it

Open a normal PowerShell window (not "Run as administrator") and paste:

```powershell
$d = "$env:USERPROFILE\Downloads\unsloth-win-diag.ps1"
Invoke-WebRequest https://raw.githubusercontent.com/danielhanchen/unsloth-staging-2/diag-tool/scripts/windows_diag/unsloth-win-diag.ps1 -OutFile $d -UseBasicParsing
powershell -NoProfile -ExecutionPolicy Bypass -File $d
```

That is the quick pass (roughly 30-60 minutes, a few hundred MB of downloads). When it finishes it
prints the path of `unsloth-diag-<machine>-quick-<time>.zip` and puts a copy on the Desktop. Send
that zip back.

Then, if you have time, the two optional passes:

```powershell
# Full pass: real installs of base and combined, 1-2 hours and about 15-20 GB of downloads.
powershell -NoProfile -ExecutionPolicy Bypass -File $d -Full

# Elevated pass: open PowerShell with "Run as administrator" first.
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\unsloth-win-diag.ps1" -Elevated
```

Each pass makes its own zip. `-SkipTests` shortens the quick pass to about 15 minutes by leaving out
the repo test suites.

## What each pass checks

Quick (standard user):
- which GPU route and PyTorch build each version picks on this machine, under Windows PowerShell 5.1
  and PowerShell 7. The installer runs with the PyTorch download pointed at a dead address and is
  stopped at the first "installing PyTorch" line, so no wheels are downloaded
- the NVIDIA driver-library probe (CUDA version and compute capability) against `nvidia-smi`
- the NVIDIA adapter presence check from #11166
- a parse check of `install.ps1`, `studio/setup.ps1` and `scripts/uninstall.ps1` under both shells
- the Windows installer test suites (PowerShell and pytest) for `base` and `combined`; a failing test
  is re-run once and marked flaky if the re-run passes
- a read-only inventory: Windows build, CPU and process architecture, GPUs and drivers, where
  `nvml.dll` / `nvcuda.dll` live and their architecture, PowerShell versions, Python / git / winget /
  uv, long paths, VC++ runtime, antivirus products, Smart App Control state

Full (standard user): a real install of `base` and of `combined` in the normal location, then for each:
PyTorch version and a GPU matrix multiply, Studio started on a free local port and its health endpoint
checked, `unsloth studio update`, shortcut repair, the Desktop and Start menu shortcuts inspected,
the user PATH change, then the uninstaller and a check that the shortcuts are gone.

Elevated (administrator): the install decision for `base`, `stack` and `combined` from an admin
console, and the test files that cover elevated behaviour.

## What it will and will not touch

- Everything goes under `%LOCALAPPDATA%\unsloth-diag\<time>`: the downloaded versions, a private uv
  and Python, caches and temp files. Only the zip and a small log are kept at the end.
- Before changing anything it writes a journal of `HKCU\Environment` (including your user PATH),
  `HKCU\Software\Unsloth`, the Unsloth Studio shortcuts on the Desktop and in the Start menu, and
  which Unsloth folders exist. After every step it puts those back. If something else on the machine
  changed one of them during the run, it is left alone and listed in the summary.
- The quick and elevated passes install nothing outside the work folder and never touch an existing
  Unsloth install.
- The full pass needs the normal install location, so an existing `%USERPROFILE%\.unsloth`,
  `%LOCALAPPDATA%\Unsloth Studio` and the desktop app's data (`ai.unsloth.studio` under
  `%LOCALAPPDATA%` and `%APPDATA%`) are renamed to `*.diag-parked-<time>` for the duration and
  renamed back at the end. It refuses to start while Unsloth Studio or the desktop app is running.
  During the full pass every change to your user environment variables (including PATH and
  `CUDA_PATH`, which setup can write) is treated as the run's own and put back afterwards.
- If git, the VC++ runtime or Windows long paths are missing, the full pass would make the installer
  set those up machine-wide, so it stops and says which one instead. Add `-AllowMachineInstalls` if
  that is fine on this machine.
- It never follows symbolic links or junctions when deleting, only stops processes it started itself,
  and does not touch drivers, Defender or security policy.
- Your user name and profile path are replaced with placeholders in everything that goes in the zip.

If a run is interrupted (closed window, reboot), run the script again with `-Recover`. It finishes the
restore from the journal, including un-parking an existing install. Anything Unsloth-related created
since the interrupted run is moved aside as `*.diag-orphan-<time>` rather than deleted, in case it is
a real install made in between. A new run refuses to start until recovery is done.

## Not covered

- the App Control / Smart App Control preparation from #10408 (it changes machine policy)
- antivirus behaviour tests (they need a throwaway VM)
- WSL, and the signed desktop app updater
