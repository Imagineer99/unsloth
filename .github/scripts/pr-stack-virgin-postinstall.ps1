# SPDX-License-Identifier: AGPL-3.0-only

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$studioHome = 'C:\studio-home'
$venv = Join-Path $studioHome 'unsloth_studio'
$scripts = Join-Path $venv 'Scripts'
$python = Join-Path $scripts 'python.exe'
$stub = Join-Path $scripts 'unsloth.exe'
$cmdShim = Join-Path $studioHome 'bin\unsloth.cmd'
$launcher = Join-Path $studioHome 'launch-studio.ps1'
$reinstallLog = 'C:\ci-out\reinstall.log'

function Require-Path([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required path is missing: $Path"
    }
}

function Snapshot-File([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $item = Get-Item -LiteralPath $Path
    [pscustomobject]@{
        Path = $Path
        Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
        LastWriteTimeUtc = $item.LastWriteTimeUtc.Ticks
    }
}

Require-Path $python
Require-Path $stub
Require-Path $cmdShim
$tracked = @($cmdShim, $launcher) |
    ForEach-Object { Snapshot-File $_ } |
    Where-Object { $null -ne $_ }

Write-Host '=== System32 managed CLI probe ==='
$env:UNSLOTH_STUDIO_HOME = $studioHome
$env:UNSLOTH_DESKTOP_MANAGED = '1'
Push-Location (Join-Path $env:WINDIR 'System32')
try {
    & $python -m unsloth_cli --version
    if ($LASTEXITCODE -ne 0) { throw "module entry point failed from System32: $LASTEXITCODE" }
    & $python -m unsloth_cli studio desktop-capabilities --json
    if ($LASTEXITCODE -ne 0) { throw "desktop capability probe failed from System32: $LASTEXITCODE" }
} finally {
    Pop-Location
    Remove-Item Env:\UNSLOTH_DESKTOP_MANAGED -ErrorAction SilentlyContinue
}
Write-Host 'PASS: managed CLI commands escape System32.'

Write-Host '=== quarantined launcher probe ==='
$quarantined = "$stub.quarantined"
Move-Item -LiteralPath $stub -Destination $quarantined
try {
    & $cmdShim --version
    if ($LASTEXITCODE -ne 0) { throw "policy-safe cmd shim failed: $LASTEXITCODE" }
    & $python -m unsloth_cli --version
    if ($LASTEXITCODE -ne 0) { throw "module entry point failed without unsloth.exe: $LASTEXITCODE" }
} finally {
    if (Test-Path -LiteralPath $quarantined) {
        Move-Item -LiteralPath $quarantined -Destination $stub
    }
}
Write-Host 'PASS: CLI remains usable with unsloth.exe quarantined.'

Write-Host '=== reinstall and idempotency probe ==='
$env:UNSLOTH_SKIP_AUTOSTART = '1'
$env:UNSLOTH_STUDIO_DISABLE_PUBLIC_CHECK = '1'
$env:UNSLOTH_VERBOSE = '1'
$env:UNSLOTH_CI_SOURCE_OVERLAY = 'C:\ci'
& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File C:\ci\install.ps1 *>&1 | Tee-Object -FilePath $reinstallLog
$reinstallExit = $LASTEXITCODE
if ($reinstallExit -ne 0) { throw "reinstall exited $reinstallExit" }

foreach ($before in $tracked) {
    $after = Snapshot-File $before.Path
    if ($null -eq $after) { throw "reinstall removed $($before.Path)" }
    if ($before.Hash -ne $after.Hash) { throw "reinstall changed the bytes of $($before.Path)" }
    if ($before.LastWriteTimeUtc -ne $after.LastWriteTimeUtc) {
        throw "reinstall rewrote unchanged file $($before.Path)"
    }
}

$userBin = Join-Path $env:USERPROFILE '.local\bin'
$uv = Join-Path $userBin 'uv.exe'
$uvx = Join-Path $userBin 'uvx.exe'
Require-Path $uv
Require-Path $uvx
& $uv --version
if ($LASTEXITCODE -ne 0) { throw "uv failed after reinstall: $LASTEXITCODE" }
& $uvx --version
if ($LASTEXITCODE -ne 0) { throw "uvx failed after reinstall: $LASTEXITCODE" }

$leftovers = Get-ChildItem -LiteralPath $userBin -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '\.(rollback|tmp)\.' }
if ($leftovers) {
    throw "reinstall left transactional files: $($leftovers.Name -join ', ')"
}

Write-Host 'PASS: reinstall is idempotent and leaves a working uv/uvx pair.'
Write-Host 'VIRGIN WINDOWS STACK SCENARIOS PASSED'
