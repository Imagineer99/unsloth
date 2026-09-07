# Research MLX HTTP 400 reproduction

Base: upstream unslothai/unsloth commit acad3f00f6bef9a8c5c9ec57e5fe04ad96a11ff6.
Branch: fix/research-mlx-http400-repro.
Intended remote: Imagineer99/unsloth. No PR required.

## Proof

The actual ResearchSupervisor._stream_completion builds a JSON-mode request
and sends it through HTTPX ASGITransport to the actual /v1/chat/completions route
and error handlers. Auth, research persistence/lease bookkeeping and the loaded
non-GGUF model backend are test doubles. Its model entry is marked is_mlx=True,
using the shared orchestrator route used by MLX.

For planning, decision and synthesis_audit, the unchanged request must return
HTTP 400 with code unsupported_parameter and param response_format. Generation
must not run. The supervisor must format the exception as the reported
"Local model request failed with HTTP 400".

The paired control removes only response_format at the transport boundary. It
must reach the scripted generator, return HTTP 200 and produce the expected JSON.
This is a causal control, not a proposed production fix.

A green workflow means the bug and controls reproduced as expected. Setup errors
and unexpected statuses fail the job. No xfail or continue-on-error is used.

## Scope

Linux and macOS jobs; Python 3.12. No MLX package, model weights, M5 hardware or
real inference. This establishes a pre-generation integration failure. It does
not reproduce the exact 0.1.806-beta desktop binary or prove full research succeeds
after removing the field. No application implementation is changed.

## Local validation

WSL Linux: six passed (three reported-shape cases and three single-field controls).
The isolated Python 3.12 environment uses the dependency command in the workflow.

## Evidence

Each OS uploads research-mlx-http400-<os>: source commit, host and architecture,
dependency versions, pytest log, JUnit XML and six JSON payload/response records.
Auth headers are excluded. No external research tools or model downloads run.

Pushing the branch starts the branch-scoped repro workflow, subject to any existing
fork workflows. Await user feedback before pushing. No pull request is needed.
