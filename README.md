# Windows Vulkan discovery causal A/B

Standalone evidence branch for unslothai/unsloth#10564. No product changes.

The Windows job runs the released detector (A), adds the missed adapter/component
manifest discovery through a test-only intervention (B), then restores A (A2).
The hardware profile is fixed to Windows x64 AMD gfx1151. Release source is pinned
to cedbb58e4a49befe12d4f28f385abc4393c763e5 and verified by SHA-256.

Expected: adapter REG_SZ, adapter REG_MULTI_SZ and software-component registration
produce ROCm / Vulkan / ROCm. Ten other cases check legacy registrations, missing
or invalid driver evidence, masks and loader filters. Every case checks explicit
ROCm and Vulkan selection. Success requires all 13 cases and A/B/A2 assertions.

Uses real winreg APIs with HKLM reads redirected into private temporary HKCU test
subtrees, removed in finally. No system GPU registry writes. Hardware and bundle
availability are fixtures; the released backend resolver otherwise stays real.

This is causal evidence for the detection gap, not a test of the PR implementation.
B has a fixture active-device inventory, not a production Windows device enumerator.
No AMD GPU, functional Vulkan driver, model inference or benchmark is involved.
It does not establish the affected Reddit user's registration layout.

The workflow uploads evidence/result.json as windows-vulkan-registry-ab.
The runner was previously validated locally on native Windows with Python 3.11.9.
