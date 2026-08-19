$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).ProviderPath
$SetupPath = Join-Path $RepoRoot "studio/setup.ps1"
$Setup = Get-Content -Raw -LiteralPath $SetupPath
$StartMarker = '    if ($InstalledVer -and $LatestVer -and ($InstalledVer -eq $LatestVer)) {'
$EndMarker = '        # ...but not if an AMD GPU is present and installed PyTorch is CPU-only'
$Start = $Setup.IndexOf($StartMarker, [StringComparison]::Ordinal)
$End = $Setup.IndexOf($EndMarker, $Start, [StringComparison]::Ordinal)
if ($Start -lt 0 -or $End -lt 0) {
    throw "Could not isolate setup.ps1's dependency-skip fast path"
}

$RealPython = (Get-Command python -ErrorAction Stop).Source
$FastPath = $Setup.Substring($Start, $End - $Start)
# Keep imports deterministic: neither runner image packages nor user site-packages may affect the result.
$FastPath = $FastPath.Replace('& python -c', '& $RealPython -S -c') + "`n    }"

function Invoke-FastPath([string]$Installed, [string]$Required) {
    $InstalledVer = $Installed
    $LatestVer = $Installed
    $env:UNSLOTH_DESKTOP_BACKEND_VERSION = $Required
    $_PkgName = "unsloth"
    $SkipPythonDeps = $false
    function step { }
    function substep { }
    Invoke-Expression $FastPath
    return [bool]$SkipPythonDeps
}

$Got = Invoke-FastPath "2026.8.14" "2026.8.15"
if ($Got) {
    throw "Stale backend incorrectly stayed on the dependency-skip fast path"
}

$Got = Invoke-FastPath "2026.8.15" "2026.8.15"
if (-not $Got) {
    throw "Satisfying backend unnecessarily forced repair"
}

Write-Host "PASS: setup.ps1 deterministically repairs stale backends and skips satisfying ones"
& $RealPython (Join-Path $RepoRoot ".github/repro/pr8670_source_contract.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
