# PR #10081 UI evidence

Pinned comparison for `unslothai/unsloth#10081`:

- BEFORE merge base: `1400031e2fe0383c0bdbd26e07759aaf5dfa62bd`
- AFTER PR head: `ccd7679d8cfffc4188178c03ec16fecc76755f1f`

Both GIFs show the same deterministic Audio generation request held in flight.
The BEFORE panel is static; the AFTER panel contains a 2.58-second loop covering
two complete sweeps of the indeterminate progress segment.

The scene uses isolated production frontend builds and a mocked Audio endpoint.
It validates frontend presentation and request-state behavior, not audio quality,
model inference, or GPU behavior.
