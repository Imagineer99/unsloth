# leak_hunt runtime harness

Boots Unsloth Studio with its periodic timers compressed to ~1 s, drives it for a soak window,
stops it gracefully, and reports processes that leaked, piled up, spun or survived.
Static side (`static_audit.py`, `timers.py`) is separate; this README covers the runtime side.

## Files
- `run.py`: orchestrator (CLI below). Exit 0 CLEAN, 1 LEAK, 2 VOID. Writes `<out>`, `<out>.md`, artefacts in `<out>.d/`.
- `monitor.py`: psutil sampler (4 Hz) + pure `evaluate()` verdicts.
- `drive.py`: soak workload; `python drive.py --tool-adversary` is the in-venv tool helper.
- `accel/`: `leak_hunt_accel.py` (spawn log + AST timer compression), `sitecustomize.py` (PYTHONPATH entry, chains
  any other sitecustomize), `__init__.py` (install as PYTHONPATH (default) or `.pth` into a private venv).
- `tests/test_runtime_verdicts.py` (unit, synthetic timelines + AST rewrite), `tests/test_runtime_e2e.py`
  (fake server, positive + negative control, ~50 s).

## CLI
```
python leak_hunt/run.py --studio-python <Studio venv python> --home <fresh UNSLOTH_STUDIO_HOME> \
  --port 18931 --timers timers.json --compress-to 1.0 --soak 60 --post-stop 20 \
  --out leak_hunt_<label>.json [--label base|head] [--inject nofile-unlimited] \
  [--drive default|api|none] [--allow REGEX]... [--studio-cmd "<argv, PORT substituted>"] \
  [--install-home <shared install, shimmed into --home>] [--accel pythonpath|pth] [--sites spawn_sites.json] \
  [--orphan-s 10 --pileup-n 3 --accumulate-n 2 --accumulate-span-s 10 --spin-frac 0.8 --spin-s 10 --zombie-s 10]
```
Default server command: `<venv>/bin/unsloth studio --api-only -H 127.0.0.1 -p PORT` (on POSIX the CLI execs
into the Studio venv python, same pid; on Windows it runs the backend as a child). Deps: Python >= 3.10 + psutil
in the harness interpreter; nothing extra in the Studio venv.

## Verdicts (deterministic)
- ORPHAN: original parent exited, still alive at the end of the soak, alive > orphan-s after orphaning (orphaned
  then reaped before the soak ends, e.g. by a tool timeout's killpg, is not a leak).
- PILEUP: >= pileup-n processes from one spawn site alive at once, or >= accumulate-n from one site created over
  >= accumulate-span-s and all alive at stop.
- SURVIVOR: alive after Studio exited + the post-stop window (then killed and listed in `survivors_killed`).
- SPINNER: non-server process >= spin-frac of a core for >= spin-s.
- ZOMBIE: unreaped by a Studio process for > zombie-s (zombies adopted by the harness are reaped, not counted).
- VOID: not healthy in 120 s; login failed; with `--timers`, accel never claimed the server or rewrote nothing;
  `--inject` requested but the server's RLIMIT_NOFILE (read back by accel) is not unlimited.

## Attribution
Linux: harness is `PR_SET_CHILD_SUBREAPER`, so orphans stay its descendants (and are reaped by it).
macOS: descendants of the roots, previously tracked pid+create_time after re-parenting to launchd, plus a sweep of
ppid==1 same-uid processes created after start whose env has `LEAK_HUNT_TAG` or whose cmdline/cwd contains the
run's Studio home (tool workdirs live there; tool envs are scrubbed). Windows: Job object around the roots
(`QueryInformationJobObject` member list) plus descendants. Spawn site = first non-stdlib frame from the accel
spawn log (`spawns-*.jsonl`); processes started by non-Python code are `child-of[...]`, `tool-workdir:<exe>` or
`unattributed:<exe>`.

## Timer compression
Only the process that first imports a module named in `--timers` (the server) rewrites; it pops
`LEAK_HUNT_COMPRESS` so its children do not. Constants (module or class scope) and listed literal loop sleeps
become `min(value, compress-to)` (ms units x1000) in the AST before exec, so derived constants and default
args follow; no bytecode is cached. Evidence: `accel.server_claims`, `accel.rewrites` in the report.

## Local results (Linux x86_64, this host; bwrap present but user namespaces denied, so the sandbox probe FAILS
and re-probes on its 60 s fail TTL, compressed to 1 s)
Installs from the switchboard cache (`studio_regress.py prebuild --pr 15345`): base 9e1ddce97 (merge base),
head 46d390fa9. Timers: `outputs/leak_hunt/runs/timers_fixture.json` (5 sandbox constants; Fork A's
`timers.json` replaces it).

| run | command delta | exit | result |
|---|---|---|---|
| unit | `pytest tests/test_runtime_verdicts.py` | 0 | 8 passed |
| fake clean / leaky | `pytest tests/test_runtime_e2e.py` | 0 | clean fake CLEAN; leaky fake reports ORPHAN, PILEUP (site = the fake's Popen line), SPINNER, SURVIVOR |
| head, `--drive api` | soak 60 | 0 | CLEAN; 5 rewrites; 197 spawns, 60 x `sandbox_probe.py:519` (probe ~1/s) |
| base, `--drive api` | soak 60 | 0 | CLEAN; 5 rewrites; 199 spawns, 60 x `sandbox_probe.py:481` |
| head, `--drive default` | soak 60 | 1 | LEAK: tool-code daemons outlive the tool call and Studio (below) |
| base, `--drive default` | soak 60 (earlier rule set) | 1 | same tool-daemon findings as head |
| head, `--inject nofile-unlimited` | soak 5 | 2 | VOID: Linux caps RLIMIT_NOFILE (server read back 1048576) |

Tool-phase findings, identical on base and head (pre-existing, not #15345): with the OS sandbox unavailable
(auto mode falls back to software safeguards), Python tool code that `os.setsid()` + double-forks, and Terminal
commands `setsid sh -c 'sleep 600' &` and `nohup sleep 600 &`, leave processes that outlive the tool call and the
Studio process (ORPHAN + SURVIVOR). Plain `sleep 600 &` is killed by the tool's timeout killpg (not reported).
The #15345 bug itself does not reproduce on Linux (bwrap pid namespace + `close_range`); macOS staging is needed.

Untested here: the macOS (launchd sweep, ulimit inject) and Windows (Job object, CTRL_BREAK stop) paths.
