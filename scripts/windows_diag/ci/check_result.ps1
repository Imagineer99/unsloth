# CI gate for a diagnostic run: the script itself always exits through its packaging step, so a
# run that stopped early still leaves a zip. This reads that zip and fails the job unless the run
# actually did its work.
#   check_result.ps1 -Mode quick|full [-SkipTests]
param(
    [Parameter(Mandatory = $true)][ValidateSet('quick', 'full')][string]$Mode,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = Get-ChildItem "$env:LOCALAPPDATA\unsloth-diag" -Recurse -Filter "unsloth-diag-*-$Mode-*.zip" |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $zip) { Write-Host "FAIL: no $Mode zip was written"; exit 1 }
$za = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
try {
    $entry = $za.Entries | Where-Object { $_.FullName -like '*results.json' } | Select-Object -First 1
    $reader = New-Object System.IO.StreamReader($entry.Open())
    try { $r = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
} finally { $za.Dispose() }

$bad = @()
foreach ($e in @($r.errors)) { if ("$e" -match '^stopped:') { $bad += "the run stopped early: $e" } }
foreach ($p in $r.states.PSObject.Properties) { if (-not $p.Value.verified) { $bad += "state $($p.Name) was not verified" } }
if ($Mode -eq 'quick') {
    if (@($r.decisions).Count -eq 0) { $bad += 'no decision runs' }
    foreach ($d in @($r.decisions)) { if (-not $d.reached) { $bad += "decision $($d.state)/$($d.shell) never reached the torch step" } }
    if (@($r.llama).Count -eq 0) { $bad += 'no llama.cpp bundle rows' }
    if (-not $SkipTests -and @($r.tests).Count -eq 0) { $bad += 'no repo test rows' }
} else {
    if (@($r.full).Count -eq 0) { $bad += 'no full-pass rows' }
}
Write-Host "timing: $(@($r.timing.PSObject.Properties | ForEach-Object { "$($_.Name) $($_.Value)" }) -join ', ')"
if ($bad.Count -gt 0) { foreach ($b in $bad) { Write-Host "FAIL: $b" }; exit 1 }
Write-Host "OK: $($zip.Name) did its work"
