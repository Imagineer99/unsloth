# CI check for unsloth-win-diag.ps1: plants a fake existing Unsloth install, records the user state
# the diagnostic promises not to change, and after the run asserts that nothing moved and that
# the fake install came back byte for byte.
#   snapshot.ps1 -Phase before -Plant
#   snapshot.ps1 -Phase after
param(
    [Parameter(Mandatory = $true)][ValidateSet('before', 'after')][string]$Phase,
    [switch]$Plant,
    [string]$Dir = $env:RUNNER_TEMP
)
$ErrorActionPreference = 'Stop'

function Get-RegDump {
    param([string]$Key)
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Key)
    if (-not $k) { return '<absent>' }
    try {
        $lines = @()
        foreach ($n in ($k.GetValueNames() | Sort-Object)) {
            if ($n -like 'UnslothPathRefresh_*' -or $n -like 'UnslothDiagRefresh_*') { continue }
            $v = $k.GetValue($n, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $lines += "$n [$($k.GetValueKind($n))] = $(@($v) -join '|')"
        }
        foreach ($s in ($k.GetSubKeyNames() | Sort-Object)) { $lines += "[$s]"; $lines += (Get-RegDump "$Key\$s") }
        return ($lines -join "`n")
    } finally { $k.Close() }
}

function Get-TreeDigest {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '<absent>' }
    $rows = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | Sort-Object FullName)) {
        $rows += "$($f.FullName.Substring($Path.Length)) $((Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash)"
    }
    return ($rows -join "`n")
}

$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$unslothHome = Join-Path $env:USERPROFILE '.unsloth'
$launcher = Join-Path $env:LOCALAPPDATA 'Unsloth Studio'
$appData = @((Join-Path $env:LOCALAPPDATA 'ai.unsloth.studio'), (Join-Path $env:APPDATA 'ai.unsloth.studio'))

if ($Plant) {
    $token = [guid]::NewGuid().ToString('N')
    New-Item -ItemType Directory -Force -Path (Join-Path $unslothHome 'studio') | Out-Null
    Set-Content -LiteralPath (Join-Path $unslothHome 'studio\diag_marker.txt') -Value $token
    New-Item -ItemType Directory -Force -Path $launcher | Out-Null
    Set-Content -LiteralPath (Join-Path $launcher 'launch-studio.ps1') -Value "# planted $token"
    $sh = New-Object -ComObject WScript.Shell
    foreach ($d in @($desktop, $startMenu)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        $l = $sh.CreateShortcut((Join-Path $d 'Unsloth Studio.lnk'))
        $l.TargetPath = Join-Path $env:SystemRoot 'System32\notepad.exe'
        $l.Arguments = $token
        $l.Save()
    }
    $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Unsloth')
    $k.SetValue('DiagMarker', $token, [Microsoft.Win32.RegistryValueKind]::String); $k.Close()
    # What a real machine carries: the Studio shim on the user PATH (the uninstaller removes it),
    # a uv-registered Python whose key holds a "(Default)" value, and the desktop app's data.
    $shim = Join-Path $unslothHome 'studio\bin'
    New-Item -ItemType Directory -Force -Path $shim | Out-Null
    $envKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    $path = [string]$envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $envKey.SetValue('Path', (@($shim) + @($path.Split(';') | Where-Object { $_ }) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString); $envKey.Close()
    $a = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Python\Astral\CPython3.13.99\InstallPath')
    $a.SetValue('', 'C:\diag-fake-python', [Microsoft.Win32.RegistryValueKind]::String)
    $a.SetValue('ExecutablePath', 'C:\diag-fake-python\python.exe', [Microsoft.Win32.RegistryValueKind]::String); $a.Close()
    foreach ($d in $appData) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        Set-Content -LiteralPath (Join-Path $d 'diag_marker.txt') -Value $token
    }
}

$snap = [ordered]@{
    environment = (Get-RegDump 'Environment')
    unsloth_key = (Get-RegDump 'Software\Unsloth')
    astral_key = (Get-RegDump 'Software\Python\Astral')
    unsloth_home = (Get-TreeDigest $unslothHome)
    launcher = (Get-TreeDigest $launcher)
    app_local = (Get-TreeDigest $appData[0])
    app_roaming = (Get-TreeDigest $appData[1])
    desktop_lnk = (Get-TreeDigest (Join-Path $desktop 'Unsloth Studio.lnk'))
    start_lnk = (Get-TreeDigest (Join-Path $startMenu 'Unsloth Studio.lnk'))
    tc = (Test-Path -LiteralPath 'C:\tc')
    parked = @(Get-ChildItem -LiteralPath $env:USERPROFILE -Force -Filter '.unsloth.diag-parked-*' -ErrorAction SilentlyContinue | ForEach-Object Name) +
             @(Get-ChildItem -LiteralPath $env:LOCALAPPDATA -Force -Filter '*.diag-parked-*' -ErrorAction SilentlyContinue | ForEach-Object Name) +
             @(Get-ChildItem -LiteralPath $env:APPDATA -Force -Filter '*.diag-parked-*' -ErrorAction SilentlyContinue | ForEach-Object Name)
    active_journal = (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'unsloth-diag\ACTIVE_JOURNAL.txt'))
}
# Get-TreeDigest of a single file returns nothing useful through -Recurse; hash it directly.
foreach ($name in @('desktop_lnk', 'start_lnk')) {
    $p = if ($name -eq 'desktop_lnk') { Join-Path $desktop 'Unsloth Studio.lnk' } else { Join-Path $startMenu 'Unsloth Studio.lnk' }
    $snap[$name] = if (Test-Path -LiteralPath $p) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } else { '<absent>' }
}
$file = Join-Path $Dir "diag_snapshot_$Phase.json"
$snap | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $file -Encoding UTF8

if ($Phase -eq 'after') {
    $before = Get-Content -LiteralPath (Join-Path $Dir 'diag_snapshot_before.json') -Raw | ConvertFrom-Json
    $bad = 0
    foreach ($k in $snap.Keys) {
        $a = ($before.$k | ConvertTo-Json -Depth 5 -Compress); $b = ($snap[$k] | ConvertTo-Json -Depth 5 -Compress)
        if ($a -ne $b) { Write-Host "CHANGED: $k"; Write-Host "  before: $a"; Write-Host "  after:  $b"; $bad++ }
        else { Write-Host "same: $k" }
    }
    if ($bad -gt 0) { Write-Host "FAIL: $bad user-state item(s) changed"; exit 1 }
    Write-Host 'OK: zero net change to the journalled user state, planted install intact'
}
