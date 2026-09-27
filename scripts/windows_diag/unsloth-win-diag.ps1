<#
Unsloth Windows installer diagnostic. See README.md next to this file.

  Quick pass (default, standard user, about 30-60 min, no multi-GB downloads):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\unsloth-win-diag.ps1
  Full pass (standard user, about 1-2 h, 15-20 GB, real installs of two states):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\unsloth-win-diag.ps1 -Full
  Elevated pass (run from an administrator console):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\unsloth-win-diag.ps1 -Elevated
  Finish an interrupted run (restores everything the journal recorded):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\unsloth-win-diag.ps1 -Recover

Everything happens under %LOCALAPPDATA%\unsloth-diag\<timestamp>. Before the first change the
script journals HKCU\Environment, HKCU\Software\Unsloth, HKCU\Software\Python\Astral, the Unsloth
Studio shortcuts and the default install folders, and restores them after every state. The result
is one zip, also copied to the Desktop. Windows PowerShell 5.1 and pwsh 7 compatible.
#>
[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$Elevated,
    [switch]$Recover,
    [switch]$AllowMachineInstalls,
    [string[]]$States = @(),
    [string[]]$TestStates = @(),
    [string]$ManifestPath = '',
    [switch]$SkipTests,
    [int]$DecisionTimeoutSec = 600,
    [int]$TestTimeoutSec = 300,
    [int]$FullInstallTimeoutSec = 5400,
    [switch]$NoDesktopCopy,
    [switch]$KeepWorkDir,
    # Hosted CI runners are administrators with UAC off; this lets the quick/full pass run there.
    [switch]$AllowAdmin
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Off
$ProgressPreference = 'SilentlyContinue'
$ToolVersion = '1.0.0'

$EmbeddedManifest = @'
{
  "schema": 1,
  "created": "2026-09-27",
  "states": {
    "base": {
      "repo": "unslothai/unsloth",
      "sha": "c807acbf44da1d4a0ecf465a579c31ab0c6a5f69",
      "zip_url": "https://codeload.github.com/unslothai/unsloth/zip/c807acbf44da1d4a0ecf465a579c31ab0c6a5f69",
      "files": {
        "install.ps1": "ccefc629f7d80e9fe126058962aff73e7dee0308a2e18e0ce154ce8945a0a2e1",
        "studio/setup.ps1": "2818d55498e55de253b8c6cf7bf53ac08483104b86f5bd068bdd90a86b06980d",
        "scripts/uninstall.ps1": "b353c3070e7ad9a2cd181a4b2de0e4347b8800ba4c8185874ef2697eaf26bd5b",
        "pyproject.toml": "9f1b71f3bca6f0ca4b4c82959f85942e2c9e0bd007749512ccebc2f87a7bd8b5"
      }
    },
    "stack": {
      "repo": "unslothai/unsloth",
      "sha": "8294a9b882bf6c85759780204a2d718454cd0f3f",
      "zip_url": "https://codeload.github.com/unslothai/unsloth/zip/8294a9b882bf6c85759780204a2d718454cd0f3f",
      "files": {
        "install.ps1": "55b5793d326d7194712f672bab064c9c1f70aa9f55f573f817133953e7628d58",
        "studio/setup.ps1": "3217c24b1861d07ede6216653e1db340641d77d7e1085952d572ad1bc582f8d8",
        "scripts/uninstall.ps1": "b353c3070e7ad9a2cd181a4b2de0e4347b8800ba4c8185874ef2697eaf26bd5b",
        "pyproject.toml": "9f1b71f3bca6f0ca4b4c82959f85942e2c9e0bd007749512ccebc2f87a7bd8b5"
      }
    },
    "presence": {
      "repo": "danielhanchen/unsloth-staging-2",
      "sha": "eebda7d997cc7a26562ee1084644974e50ae23d2",
      "zip_url": "https://codeload.github.com/danielhanchen/unsloth-staging-2/zip/eebda7d997cc7a26562ee1084644974e50ae23d2",
      "files": {
        "install.ps1": "331340bda9ce8f4909e6a5818d55fdca5527747d2fb192f4adc140bd38de1a78",
        "studio/setup.ps1": "0747e4ad5495fd90021696dcaa9dbf78cfffa21138c950b48d171bd7b00f24f6",
        "scripts/uninstall.ps1": "b353c3070e7ad9a2cd181a4b2de0e4347b8800ba4c8185874ef2697eaf26bd5b",
        "pyproject.toml": "9f1b71f3bca6f0ca4b4c82959f85942e2c9e0bd007749512ccebc2f87a7bd8b5"
      }
    },
    "combined": {
      "repo": "danielhanchen/unsloth-staging-2",
      "sha": "3468c0037ec27958bc768252aa7733e183b88a19",
      "zip_url": "https://codeload.github.com/danielhanchen/unsloth-staging-2/zip/3468c0037ec27958bc768252aa7733e183b88a19",
      "files": {
        "install.ps1": "68711941b1318bf9cba4ad6584c50b3cffa1b82dd02afef0dab524ffaf4cd41a",
        "studio/setup.ps1": "27bc378fd0ffe38f7e821b52472df3280e5ff702231957260e6ea58f13f4322f",
        "scripts/uninstall.ps1": "b353c3070e7ad9a2cd181a4b2de0e4347b8800ba4c8185874ef2697eaf26bd5b",
        "pyproject.toml": "9f1b71f3bca6f0ca4b4c82959f85942e2c9e0bd007749512ccebc2f87a7bd8b5"
      }
    }
  }
}
'@

# The uv release install.ps1 itself pins, with the same archive hashes.
$UvVersion = '0.12.1'
$UvAssets = @{
    'x64'   = @{ Asset = 'uv-x86_64-pc-windows-msvc.zip';  Sha256 = '8FCB0CB46E1229065E344758980924E569BEF5882EF45F46FADA8FB24E06B74A' }
    'arm64' = @{ Asset = 'uv-aarch64-pc-windows-msvc.zip'; Sha256 = '9BC7C18E616230FA2DC6FB24BC3AFDE18A95C2B5C9433DE747E9502C66041568' }
}
$DeadMirror = 'http://127.0.0.1:9'

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
$Esc = [string][char]27
$AnsiPattern = [regex]::Escape($Esc) + '\[[0-9;?]*[ -/]*[@-~]'

# ---------------------------------------------------------------- basics

function Write-Diag {
    param([string]$Message, [string]$Color = 'Gray')
    $line = "[diag $(Get-Date -Format 'HH:mm:ss')] $Message"
    Write-Host $line -ForegroundColor $Color
    if ($script:LogPath) {
        try { [System.IO.File]::AppendAllText($script:LogPath, $line + "`r`n", $Utf8NoBom) } catch { }
    }
}

function Add-DiagError {
    param([string]$Message)
    $script:Results.errors += $Message
    Write-Diag "ERROR: $Message" 'Red'
}

function Test-IsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HostArch {
    $signals = @()
    try {
        $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
        if ($k) { $signals += [string]$k.GetValue('PROCESSOR_ARCHITECTURE'); $k.Close() }
    } catch { }
    $signals += [string]$env:PROCESSOR_ARCHITEW6432
    $signals += [string]$env:PROCESSOR_ARCHITECTURE
    foreach ($s in $signals) { if ($s -and $s.ToLowerInvariant() -eq 'arm64') { return 'arm64' } }
    return 'x64'
}

function Write-TextFile {
    param([string]$Path, [string]$Text, [switch]$Bom)
    $dir = [System.IO.Path]::GetDirectoryName($Path)
    if ($dir) { [void][System.IO.Directory]::CreateDirectory($dir) }
    $enc = $Utf8NoBom
    if ($Bom) { $enc = $Utf8Bom }
    [System.IO.File]::WriteAllText($Path, [string]$Text, $enc)
}

function Write-JsonFile {
    param([string]$Path, $Object)
    Write-TextFile -Path $Path -Text ($Object | ConvertTo-Json -Depth 12)
}

function Read-SharedText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = New-Object System.IO.StreamReader($fs, $Utf8NoBom, $true)
        return $sr.ReadToEnd()
    } catch { return '' }
    finally { if ($fs) { $fs.Dispose() } }
}

function Remove-Ansi {
    param([string]$Text)
    if (-not $Text) { return '' }
    return [regex]::Replace($Text, $AnsiPattern, '')
}

function Set-ProcessEnv {
    param([string]$Name, $Value)
    if ($null -eq $Value) { Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue }
    else { Set-Item -LiteralPath "Env:$Name" -Value ([string]$Value) }
}

function Stop-Tree {
    param([int]$ProcessId)
    $ErrorActionPreference = 'Continue'
    try { & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $ProcessId /T /F 2>&1 | Out-Null } catch { }
}

# Deletes a tree without ever following a link: a link is unlinked, and rd /s removes junctions
# and directory symlinks inside the tree as entries rather than walking into their targets.
# Refuses anything outside the work dir unless the caller names it in -Allowed.
function Remove-TreeNoFollow {
    param([string]$Path, [string[]]$Allowed = @())
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $true }
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $ok = $false
    if ($script:Work -and $full.StartsWith($script:Work.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { $ok = $true }
    foreach ($a in $Allowed) { if ($a -and $full -ieq ([System.IO.Path]::GetFullPath($a).TrimEnd('\'))) { $ok = $true } }
    if (-not $ok) { Add-DiagError "refused to delete $full (outside the work dir)"; return $false }
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                if ($item.PSIsContainer) { [System.IO.Directory]::Delete($full, $false) } else { [System.IO.File]::Delete($full) }
            } elseif (-not $item.PSIsContainer) {
                $item.Attributes = [System.IO.FileAttributes]::Normal
                [System.IO.File]::Delete($full)
            } else {
                $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                & (Join-Path $env:SystemRoot 'System32\cmd.exe') /d /c "rd /s /q `"\\?\$full`"" 2>&1 | Out-Null
                $ErrorActionPreference = $prev
            }
        } catch { }
        if (-not (Test-Path -LiteralPath $full)) { return $true }
        Start-Sleep -Seconds 3
    }
    Add-DiagError "could not fully delete $full"
    return $false
}

# ---------------------------------------------------------------- bounded child runner

function Start-HiddenChild {
    param([string]$Label, [string]$CommandLine, [string]$WorkDir, [hashtable]$Env = @{})
    $raw = Join-Path $script:RunDir "$Label.txt"
    $wrapper = Join-Path $script:RunDir "$Label.cmd"
    if (Test-Path -LiteralPath $raw) { Remove-Item -LiteralPath $raw -Force }
    # cmd expands %NAME% even inside quotes, so a literal percent in a path is doubled.
    $body = @(
        '@echo off',
        'chcp 65001 >nul',
        "cd /d `"$($WorkDir.Replace('%', '%%'))`"",
        "$($CommandLine.Replace('%', '%%')) <nul > `"$($raw.Replace('%', '%%'))`" 2>&1",
        'exit /b %ERRORLEVEL%'
    ) -join "`r`n"
    [System.IO.File]::WriteAllText($wrapper, $body + "`r`n", $Utf8NoBom)
    $saved = @{}
    foreach ($k in @($Env.Keys)) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process'); Set-ProcessEnv $k $Env[$k] }
    try {
        $proc = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList "/d /c `"$wrapper`"" `
            -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru
    } finally {
        foreach ($k in @($saved.Keys)) { Set-ProcessEnv $k $saved[$k] }
    }
    $null = $proc.Handle
    $script:CurrentChild = $proc.Id
    return [pscustomobject]@{ Proc = $proc; Raw = $raw }
}

# Runs one command line through cmd.exe in a hidden console so stdout and stderr land in ONE file in
# order, stdin is NUL (a prompt can never hang the run), and the child writes UTF-8. $Env entries
# are set only for the child: the parent sets them, starts the child, and puts them back.
# $DoneMarker is a regex: once it matches, the child gets $GraceSec more and is stopped.
function Invoke-Bounded {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][int]$Timeout,
        [string]$DoneMarker = '',
        [int]$GraceSec = 3,
        [hashtable]$Env = @{}
    )
    $start = Get-Date
    $child = Start-HiddenChild -Label $Label -CommandLine $CommandLine -WorkDir $WorkDir -Env $Env
    $proc = $child.Proc; $raw = $child.Raw
    $timedOut = $false; $stopped = $false; $markerAt = $null
    while (-not $proc.WaitForExit(2000)) {
        $elapsed = ((Get-Date) - $start).TotalSeconds
        if ($elapsed -gt $Timeout) {
            $timedOut = $true
            Stop-Tree $proc.Id
            break
        }
        if ($DoneMarker) {
            if ($null -eq $markerAt) {
                if ((Read-SharedText $raw) -match $DoneMarker) { $markerAt = Get-Date }
            } elseif (((Get-Date) - $markerAt).TotalSeconds -ge $GraceSec) {
                $stopped = $true
                Stop-Tree $proc.Id
                break
            }
        }
    }
    $null = $proc.WaitForExit(30000)
    $script:CurrentChild = $null
    $exit = $null
    try { if ($proc.HasExited) { $exit = $proc.ExitCode } } catch { $exit = $null }
    $text = Read-SharedText $raw
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    return [pscustomobject]@{
        Label = $Label; Text = $text; ExitCode = $exit; TimedOut = $timedOut; Stopped = $stopped
        ElapsedSec = [math]::Round(((Get-Date) - $start).TotalSeconds, 1); RawPath = $raw
    }
}

function Save-Transcript {
    param([string]$Name, [string]$Text)
    $rel = "t/$Name.txt"
    Write-TextFile -Path (Join-Path $script:Out ($rel -replace '/', '\')) -Text (Remove-Ansi $Text)
    return $rel
}

function Get-QuotedExe {
    param([string]$Exe)
    return "`"$Exe`""
}

# ---------------------------------------------------------------- journal and restore

function ConvertFrom-RegValue {
    param($Key, [string]$Name)
    $kind = $Key.GetValueKind($Name)
    $data = $Key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    switch ($kind.ToString()) {
        'Binary' { $data = [Convert]::ToBase64String([byte[]]$data) }
        'MultiString' { $data = @($data) }
        'DWord' { $data = [int64]$data }
        'QWord' { $data = [int64]$data }
        default { $data = [string]$data }
    }
    return [ordered]@{ kind = $kind.ToString(); data = $data }
}

function ConvertTo-RegData {
    param($Value)
    switch ([string]$Value.kind) {
        'Binary' { return , [Convert]::FromBase64String([string]$Value.data) }
        'MultiString' { return , [string[]]@($Value.data) }
        'DWord' { return [int]$Value.data }
        'QWord' { return [int64]$Value.data }
        default { return [string]$Value.data }
    }
}

function Get-RegTree {
    param([string]$SubKey)
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($SubKey)
    if (-not $k) { return [ordered]@{ exists = $false; values = [ordered]@{}; subkeys = [ordered]@{} } }
    try {
        $vals = [ordered]@{}
        foreach ($n in $k.GetValueNames()) { $vals[$n] = ConvertFrom-RegValue $k $n }
        $subs = [ordered]@{}
        foreach ($s in $k.GetSubKeyNames()) { $subs[$s] = Get-RegTree "$SubKey\$s" }
        return [ordered]@{ exists = $true; values = $vals; subkeys = $subs }
    } finally { $k.Close() }
}

function Write-RegTree {
    param([string]$SubKey, $Tree)
    $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($SubKey)
    try {
        foreach ($p in $Tree.values.PSObject.Properties) {
            $k.SetValue($p.Name, (ConvertTo-RegData $p.Value), [Microsoft.Win32.RegistryValueKind]([string]$p.Value.kind))
        }
    } finally { $k.Close() }
    foreach ($p in $Tree.subkeys.PSObject.Properties) { Write-RegTree "$SubKey\$($p.Name)" $p.Value }
}

function Get-ShortcutPaths {
    $paths = @()
    try { $d = [Environment]::GetFolderPath('Desktop'); if ($d) { $paths += (Join-Path $d 'Unsloth Studio.lnk') } } catch { }
    if ($env:APPDATA) { $paths += (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Unsloth Studio.lnk') }
    return $paths
}

function Get-FileSha {
    param([string]$Path)
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } catch { return $null }
}

# The journal is plain JSON so -Recover can finish from it in a fresh process. Values that the
# harness is allowed to put back are written from the snapshot; anything else that moved is
# reported, never overwritten.
function New-Journal {
    $j = [ordered]@{
        schema = 1
        created = (Get-Date).ToString('o')
        work = $script:Work
        restored = $false
        environment = (Get-RegTree 'Environment')
        unsloth_key = (Get-RegTree 'Software\Unsloth')
        astral_key = (Get-RegTree 'Software\Python\Astral')
        shortcuts = @()
        dirs = [ordered]@{}
        parked = @()
    }
    $backupDir = Join-Path $script:Work 'journal_lnk'
    $i = 0
    foreach ($p in (Get-ShortcutPaths)) {
        $entry = [ordered]@{ path = $p; existed = (Test-Path -LiteralPath $p); sha = $null; backup = $null }
        if ($entry.existed) {
            [void][System.IO.Directory]::CreateDirectory($backupDir)
            $b = Join-Path $backupDir "$i.lnk"
            [System.IO.File]::Copy($p, $b, $true)
            $entry.sha = Get-FileSha $p
            $entry.backup = $b
        }
        $j.shortcuts += $entry
        $i++
    }
    foreach ($d in (Get-GuardedDirs)) { $j.dirs[$d] = (Test-Path -LiteralPath $d) }
    return $j
}

function Get-GuardedDirs {
    $dirs = @()
    if ($env:SystemDrive) { $dirs += (Join-Path ($env:SystemDrive + '\') 'tc') }
    if ($env:LOCALAPPDATA) { $dirs += (Join-Path $env:LOCALAPPDATA 'Unsloth Studio') }
    if ($env:USERPROFILE) { $dirs += (Join-Path $env:USERPROFILE '.unsloth') }
    return $dirs
}

function Save-Journal {
    Write-JsonFile -Path $script:JournalPath -Object $script:Journal
}

function Test-OurPathEntry {
    param([string]$Entry)
    $e = [Environment]::ExpandEnvironmentVariables($Entry).Trim().Trim('"').TrimEnd('\')
    if (-not $e) { return $false }
    $roots = @()
    if ($script:Work) { $roots += $script:Work }
    if ($env:USERPROFILE) { $roots += (Join-Path $env:USERPROFILE '.unsloth'); $roots += (Join-Path $env:USERPROFILE '.local\bin') }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Unsloth Studio') }
    foreach ($r in $roots) {
        $r = $r.TrimEnd('\')
        if ($e -ieq $r -or $e.StartsWith($r + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-PathKey { param([string]$p) return [Environment]::ExpandEnvironmentVariables($p).Trim().Trim('"').TrimEnd('\').ToLowerInvariant() }

function Get-JournalValue {
    param($Tree, [string]$Name)
    if (-not $Tree -or -not $Tree.values) { return $null }
    $p = $Tree.values.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Test-SameRegValue {
    param($A, $B)
    if ($null -eq $A -and $null -eq $B) { return $true }
    if ($null -eq $A -or $null -eq $B) { return $false }
    return ([string]$A.kind -eq [string]$B.kind) -and ((@($A.data) -join "`n") -ceq (@($B.data) -join "`n"))
}

# Environment names the installer (or this harness) writes, so the snapshot value is put back.
function Test-OwnedEnvName {
    param([string]$Name)
    if ($Name -in @('TORCHINDUCTOR_CACHE_DIR')) { return $true }
    if ($Name -like 'UnslothPathRefresh_*') { return $true }
    if ($Name -like 'UNSLOTH_*') { return $true }
    return $false
}

function Restore-EnvironmentKey {
    param($Journal, $Report)
    $snap = $Journal.environment
    $cur = Get-RegTree 'Environment'
    $names = @()
    foreach ($p in $snap.values.PSObject.Properties) { $names += $p.Name }
    foreach ($n in $cur.values.Keys) { if ($names -notcontains $n) { $names += $n } }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        foreach ($n in $names) {
            $was = Get-JournalValue $snap $n
            $now = $null
            if ($cur.values.Contains($n)) { $now = [pscustomobject]$cur.values[$n] }
            if (Test-SameRegValue $was $now) { continue }
            if ($n -ieq 'Path') {
                $wasEntries = @(); if ($was) { $wasEntries = @(([string]$was.data).Split(';') | Where-Object { $_ }) }
                $nowEntries = @(); if ($now) { $nowEntries = @(([string]$now.data).Split(';') | Where-Object { $_ }) }
                $wasKeys = @($wasEntries | ForEach-Object { Get-PathKey $_ })
                $nowKeys = @($nowEntries | ForEach-Object { Get-PathKey $_ })
                $added = @($nowEntries | Where-Object { $wasKeys -notcontains (Get-PathKey $_) })
                $removed = @($wasEntries | Where-Object { $nowKeys -notcontains (Get-PathKey $_) })
                $ours = @($added | Where-Object { Test-OurPathEntry $_ })
                $foreign = @($added | Where-Object { -not (Test-OurPathEntry $_) })
                foreach ($a in $ours) { $Report.changes += "user PATH gained $a (removed again)" }
                if ($removed.Count -eq 0 -and $foreign.Count -eq 0) {
                    if ($was) { $key.SetValue('Path', (ConvertTo-RegData $was), [Microsoft.Win32.RegistryValueKind]([string]$was.kind)) }
                    else { $key.DeleteValue('Path', $false) }
                } else {
                    $keep = @($nowEntries | Where-Object { -not (Test-OurPathEntry $_) -or ($wasKeys -contains (Get-PathKey $_)) })
                    $kind = 'ExpandString'; if ($now) { $kind = [string]$now.kind }
                    $key.SetValue('Path', ($keep -join ';'), [Microsoft.Win32.RegistryValueKind]$kind)
                    foreach ($f in $foreign) { $Report.conflicts += "user PATH gained $f during the run, not from this harness; left in place" }
                    foreach ($r in $removed) { $Report.conflicts += "user PATH lost $r during the run; not re-added" }
                }
                continue
            }
            if (Test-OwnedEnvName $n) {
                if ($was) { $key.SetValue($n, (ConvertTo-RegData $was), [Microsoft.Win32.RegistryValueKind]([string]$was.kind)) }
                else { $key.DeleteValue($n, $false) }
                $Report.changes += "user environment $n was changed by the run (put back)"
            } else {
                $Report.conflicts += "user environment $n changed during the run and is not one the installer writes; left in place"
            }
        }
    } finally { $key.Close() }
    # Tell Explorer the environment moved, the same way the installer does.
    try {
        $d = "UnslothDiagRefresh_$([guid]::NewGuid().ToString('N').Substring(0,8))"
        [Environment]::SetEnvironmentVariable($d, '1', 'User')
        [Environment]::SetEnvironmentVariable($d, [NullString]::Value, 'User')
    } catch { }
}

function Restore-OwnedKey {
    param([string]$SubKey, $Snapshot, $Report)
    $cur = Get-RegTree $SubKey
    $same = (($cur | ConvertTo-Json -Depth 12 -Compress) -eq ($Snapshot | ConvertTo-Json -Depth 12 -Compress))
    if ($same) { return }
    if (-not $Snapshot.exists -and -not $cur.exists) { return }
    try {
        if ($cur.exists) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($SubKey, $false) }
        if ($Snapshot.exists) { Write-RegTree $SubKey $Snapshot }
        $Report.changes += "HKCU\$SubKey was changed by the run (put back)"
    } catch {
        $Report.failures += "HKCU\$SubKey could not be put back: $($_.Exception.Message)"
    }
}

function Restore-Shortcuts {
    param($Journal, $Report)
    foreach ($s in @($Journal.shortcuts)) {
        $p = [string]$s.path
        $exists = Test-Path -LiteralPath $p
        if ($s.existed) {
            if (-not $exists -or (Get-FileSha $p) -ne [string]$s.sha) {
                try {
                    [System.IO.File]::Copy([string]$s.backup, $p, $true)
                    $Report.changes += "shortcut $p was rewritten or removed by the run (original bytes put back)"
                } catch { $Report.failures += "shortcut $p could not be put back: $($_.Exception.Message)" }
            }
        } elseif ($exists) {
            try { [System.IO.File]::Delete($p); $Report.changes += "shortcut $p was created by the run (removed)" }
            catch { $Report.failures += "shortcut $p could not be removed: $($_.Exception.Message)" }
        }
    }
}

function Restore-Dirs {
    param($Journal, $Report)
    # Unpark first: whatever now sits at the original path was created after parking, by this run.
    $parked = $null
    if (@($Journal.parked).Count -gt 0) { $parked = [ordered]@{ was_parked = $true; restored = $true } }
    foreach ($pk in @($Journal.parked)) {
        $orig = [string]$pk.original; $moved = [string]$pk.parked
        if (-not (Test-Path -LiteralPath $moved)) {
            if (Test-Path -LiteralPath $orig) { continue }  # never parked (crash before the rename)
            $Report.failures += "parked folder $moved is missing"; $parked.restored = $false; continue
        }
        if (Test-Path -LiteralPath $orig) {
            if (-not (Remove-TreeNoFollow -Path $orig -Allowed @($orig))) { $parked.restored = $false; $Report.failures += "could not clear $orig before unparking"; continue }
            $Report.changes += "$orig was created by the run (removed before unparking)"
        }
        try {
            Rename-Item -LiteralPath $moved -NewName ([System.IO.Path]::GetFileName($orig)) -ErrorAction Stop
        } catch {
            $Report.failures += "could not move $moved back to ${orig}: $($_.Exception.Message)"; $parked.restored = $false
        }
    }
    $parkedOrigs = @(@($Journal.parked) | ForEach-Object { [string]$_.original })
    foreach ($p in $Journal.dirs.PSObject.Properties) {
        if ($p.Value) { continue }
        if ($parkedOrigs -contains $p.Name) { continue }
        if (Test-Path -LiteralPath $p.Name) {
            if (Remove-TreeNoFollow -Path $p.Name -Allowed @($p.Name)) { $Report.changes += "$($p.Name) was created by the run (removed)" }
            else { $Report.failures += "could not remove $($p.Name)" }
        }
    }
    return $parked
}

function Invoke-Restore {
    param([string]$When)
    $j = Get-Content -LiteralPath $script:JournalPath -Raw | ConvertFrom-Json
    $report = [ordered]@{ ok = $true; when = $When; changes = @(); conflicts = @(); failures = @(); parked = $null }
    try { Restore-EnvironmentKey $j $report } catch { $report.failures += "environment restore failed: $($_.Exception.Message)" }
    Restore-OwnedKey 'Software\Unsloth' $j.unsloth_key $report
    Restore-OwnedKey 'Software\Python\Astral' $j.astral_key $report
    try { Restore-Shortcuts $j $report } catch { $report.failures += "shortcut restore failed: $($_.Exception.Message)" }
    try { $report.parked = Restore-Dirs $j $report } catch { $report.failures += "folder restore failed: $($_.Exception.Message)" }
    if ($report.failures.Count -gt 0) { $report.ok = $false }
    return $report
}

function Complete-Journal {
    $j = Get-Content -LiteralPath $script:JournalPath -Raw | ConvertFrom-Json
    $j.restored = $true
    $j.parked = @()
    Write-JsonFile -Path $script:JournalPath -Object $j
    $ptr = Join-Path $script:DiagRoot 'ACTIVE_JOURNAL.txt'
    if (Test-Path -LiteralPath $ptr) { Remove-Item -LiteralPath $ptr -Force }
}

# ---------------------------------------------------------------- inventory (read-only)

function Get-PeMachine {
    param([string]$Path)
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        $br = New-Object System.IO.BinaryReader($fs)
        [void]$fs.Seek(0x3C, 'Begin'); $off = $br.ReadInt32()
        [void]$fs.Seek($off + 4, 'Begin'); $m = $br.ReadUInt16()
        switch ($m) { 0x8664 { return 'x64' } 0xAA64 { return 'arm64' } 0x14C { return 'x86' } 0xA641 { return 'arm64ec' } default { return ('0x{0:X4}' -f $m) } }
    } catch { return $null }
    finally { if ($fs) { $fs.Dispose() } }
}

function Get-ToolProbe {
    param([string]$Name, [string]$Exe, [string]$Arguments, [int]$Timeout = 20)
    if (-not $Exe) { return $null }
    $r = Invoke-Bounded -Label "inv_$Name" -CommandLine "$(Get-QuotedExe $Exe) $Arguments" -WorkDir $script:Work -Timeout $Timeout
    return [ordered]@{ exe = $Exe; exit = $r.ExitCode; timed_out = $r.TimedOut; output = ((Remove-Ansi $r.Text).Trim()) }
}

function Find-NvidiaSmi {
    $c = @()
    foreach ($cmd in @(Get-Command nvidia-smi.exe -All -CommandType Application -ErrorAction SilentlyContinue)) { $c += $cmd.Source }
    if ($env:SystemRoot) { $c += (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe') }
    if ($env:ProgramFiles) { $c += (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe') }
    foreach ($p in $c) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    return $null
}

function Get-Inventory {
    $inv = [ordered]@{}
    $os = $null
    try { $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 30 } catch { }
    $inv.os = [ordered]@{
        caption = $(if ($os) { $os.Caption } else { $null })
        version = $(if ($os) { $os.Version } else { [Environment]::OSVersion.VersionString })
        build = $(if ($os) { $os.BuildNumber } else { $null })
        os_architecture = $(if ($os) { $os.OSArchitecture } else { $null })
        host_arch = (Get-HostArch)
        ps_process_arch = $env:PROCESSOR_ARCHITECTURE
        ubr = $null
    }
    try { $inv.os.ubr = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop).UBR } catch { }
    $inv.powershell = [ordered]@{
        this_version = $PSVersionTable.PSVersion.ToString()
        this_edition = [string]$PSVersionTable.PSEdition
        language_mode = [string]$ExecutionContext.SessionState.LanguageMode
        pwsh = $null
        execution_policy = @()
    }
    try { $inv.powershell.execution_policy = @(Get-ExecutionPolicy -List | ForEach-Object { "$($_.Scope)=$($_.ExecutionPolicy)" }) } catch { }
    if ($script:PwshExe) {
        $p = Get-ToolProbe 'pwsh' $script:PwshExe '-NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
        if ($p) { $inv.powershell.pwsh = $p.output }
    }
    $inv.elevated = (Test-IsElevated)
    $inv.integrity = $null
    try {
        $w = Get-ToolProbe 'whoami' (Join-Path $env:SystemRoot 'System32\whoami.exe') '/groups /fo csv /nh'
        if ($w -and $w.output -match 'S-1-16-(\d+)') { $inv.integrity = "S-1-16-$($Matches[1])" }
    } catch { }

    $gpus = @()
    try {
        foreach ($v in @(Get-CimInstance Win32_VideoController -OperationTimeoutSec 30)) {
            $gpus += [ordered]@{
                name = $v.Name; pnp = $v.PNPDeviceID; driver_version = $v.DriverVersion
                config_error = $v.ConfigManagerErrorCode; vendor = $v.AdapterCompatibility
                nvidia_ven = ([string]$v.PNPDeviceID -match '(?i)ven_10de')
            }
        }
    } catch { $inv.gpu_error = $_.Exception.Message }
    $inv.gpus = $gpus

    $smi = [ordered]@{ path = (Find-NvidiaSmi); list = $null; query = $null; banner_cuda = $null; cc = @(); driver = $null; names = @() }
    if ($smi.path) {
        $l = Get-ToolProbe 'smi_list' $smi.path '-L' 30
        if ($l) { $smi.list = $l.output }
        $q = Get-ToolProbe 'smi_query' $smi.path '--query-gpu=name,compute_cap,driver_version --format=csv,noheader' 30
        if ($q) {
            $smi.query = $q.output
            foreach ($row in ($q.output -split "`r?`n")) {
                $cells = @($row.Split(',') | ForEach-Object { $_.Trim() })
                if ($cells.Count -ge 3 -and $cells[1] -match '^\d+\.\d+$') { $smi.names += $cells[0]; $smi.cc += $cells[1]; $smi.driver = $cells[2] }
            }
        }
        $b = Get-ToolProbe 'smi_banner' $smi.path '' 30
        if ($b -and $b.output -match 'CUDA Version:\s*([0-9]+\.[0-9]+)') { $smi.banner_cuda = $Matches[1] }
    }
    $inv.nvidia_smi = $smi

    $libs = @()
    $candidates = @()
    if ($env:SystemRoot) {
        foreach ($n in @('nvml.dll', 'nvcuda.dll')) {
            $candidates += (Join-Path $env:SystemRoot "System32\$n")
            $candidates += (Join-Path $env:SystemRoot "SysWOW64\$n")
        }
        foreach ($store in @('System32\DriverStore\FileRepository', 'System32\HostDriverStore\FileRepository')) {
            $root = Join-Path $env:SystemRoot $store
            if (Test-Path -LiteralPath $root) {
                foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory -Filter 'nv*' -ErrorAction SilentlyContinue)) {
                    foreach ($n in @('nvml.dll', 'nvcuda.dll', 'nvcuda64.dll')) { $candidates += (Join-Path $d.FullName $n) }
                }
            }
        }
    }
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvml.dll') }
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $ver = $null
            try { $ver = (Get-Item -LiteralPath $c).VersionInfo.FileVersion } catch { }
            $libs += [ordered]@{ path = $c; machine = (Get-PeMachine $c); version = $ver }
        }
    }
    $inv.nvidia_libraries = $libs

    $inv.long_paths = $null
    try { $inv.long_paths = [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled } catch { }
    $inv.vc_redist_x64 = (Test-VcRedist)
    $inv.smart_app_control = $null
    try { $inv.smart_app_control = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { }
    $inv.antivirus = @()
    try {
        foreach ($a in @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -OperationTimeoutSec 30)) {
            $inv.antivirus += [ordered]@{ name = $a.displayName; state = ('0x{0:X6}' -f [int]$a.productState) }
        }
    } catch { }
    $inv.defender = $null
    try {
        $m = Get-MpComputerStatus -ErrorAction Stop
        $inv.defender = [ordered]@{ mode = [string]$m.AMRunningMode; realtime = [bool]$m.RealTimeProtectionEnabled; engine = [string]$m.AMEngineVersion; signatures = [string]$m.AntivirusSignatureVersion }
    } catch { }

    $tools = [ordered]@{}
    foreach ($t in @('python', 'python3', 'py', 'git', 'winget', 'uv', 'conda')) {
        $all = @()
        foreach ($cmd in @(Get-Command "$t.exe" -All -CommandType Application -ErrorAction SilentlyContinue)) { $all += $cmd.Source }
        $first = $null
        if ($all.Count -gt 0 -and $all[0] -notlike '*\WindowsApps\*') {
            $argv = '--version'
            if ($t -eq 'py') { $argv = '-0p' }
            $first = Get-ToolProbe "tool_$t" $all[0] $argv 20
        }
        $tools[$t] = [ordered]@{ paths = $all; probe = $first }
    }
    $inv.tools = $tools

    $installs = [ordered]@{}
    foreach ($d in (Get-GuardedDirs)) { $installs[$d] = (Test-Path -LiteralPath $d) }
    if ($env:LOCALAPPDATA) {
        foreach ($d in @(Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs') -Directory -Filter 'Unsloth*' -ErrorAction SilentlyContinue)) { $installs[$d.FullName] = $true }
    }
    foreach ($s in (Get-ShortcutPaths)) { $installs[$s] = (Test-Path -LiteralPath $s) }
    $installs['HKCU\Software\Unsloth'] = [bool]([Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Unsloth'))
    $inv.existing_installs = $installs
    $inv.studio_processes = @(Get-StudioProcesses)
    $envNames = @()
    foreach ($k in @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(UNSLOTH_|UV_|HF_|CUDA|TORCH|PIP_|FAKE_)' })) {
        $v = [string]$k.Value
        if ($k.Name -match '(?i)token|key|secret|password') { $v = '<redacted>' }
        $envNames += "$($k.Name)=$v"
    }
    $inv.process_env = $envNames
    return $inv
}

function Test-VcRedist {
    foreach ($k in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')) {
        try {
            $r = Get-ItemProperty -Path $k -ErrorAction Stop
            if ($r.Installed -eq 1 -and [int]$r.Major -ge 14 -and [int]$r.Minor -ge 20) { return $true }
        } catch { }
    }
    if ((Get-HostArch) -eq 'arm64') { return $false }
    return (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\vcruntime140_1.dll'))
}

function Get-StudioProcesses {
    $out = @()
    $unslothRoot = ''
    if ($env:USERPROFILE) { $unslothRoot = (Join-Path $env:USERPROFILE '.unsloth') }
    try {
        foreach ($p in @(Get-CimInstance Win32_Process -OperationTimeoutSec 30)) {
            $hit = $false
            if ([string]$p.Name -like 'unsloth*') { $hit = $true }
            if ($unslothRoot -and ([string]$p.ExecutablePath).StartsWith($unslothRoot, [StringComparison]::OrdinalIgnoreCase)) { $hit = $true }
            if ($unslothRoot -and ([string]$p.CommandLine).IndexOf($unslothRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true }
            if ($hit -and [int]$p.ProcessId -ne $PID) { $out += [ordered]@{ pid = [int]$p.ProcessId; name = [string]$p.Name } }
        }
    } catch { }
    return $out
}

# ---------------------------------------------------------------- tools in the work dir

function Get-Download {
    param([string]$Url, [string]$Dest)
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing -TimeoutSec 600; return } catch {
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds (5 * $i)
        }
    }
}

function Initialize-Tools {
    $arch = Get-HostArch
    $uvDir = Join-Path $script:Work 'tools\uv'
    $uvExe = Join-Path $uvDir 'uv.exe'
    if (-not (Test-Path -LiteralPath $uvExe)) {
        $asset = $UvAssets[$arch]
        $zip = Join-Path $script:Work "tools\$($asset.Asset)"
        [void][System.IO.Directory]::CreateDirectory($uvDir)
        Write-Diag "downloading uv $UvVersion ($arch) into the work dir"
        Get-Download -Url "https://github.com/astral-sh/uv/releases/download/$UvVersion/$($asset.Asset)" -Dest $zip
        if ((Get-FileSha $zip) -ne $asset.Sha256.ToLowerInvariant()) { throw "uv archive hash mismatch" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $za = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try {
            foreach ($e in $za.Entries) {
                $leaf = [System.IO.Path]::GetFileName($e.FullName)
                if ($leaf -in @('uv.exe', 'uvx.exe', 'uvw.exe')) { [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $uvDir $leaf), $true) }
            }
        } finally { $za.Dispose() }
    }
    $script:UvExe = $uvExe
    $script:PyDirs = @()
    $wanted = @('cpython-3.13-windows-x86_64-none')
    if ($arch -eq 'arm64') { $wanted = @('cpython-3.13-windows-aarch64-none', 'cpython-3.13-windows-x86_64-none') }
    $pyRoot = Join-Path $script:Work 'tools\py'
    foreach ($req in $wanted) {
        $r = Invoke-Bounded -Label "uv_python_$($req.Split('-')[3])" -CommandLine "$(Get-QuotedExe $uvExe) python install $req --install-dir `"$pyRoot`" --no-bin --no-registry" `
            -WorkDir $script:Work -Timeout 600 -Env (Get-IsolationEnv -StudioHome $null -NoToolPath)
        if ($r.ExitCode -ne 0) { Add-DiagError "uv python install $req failed (exit $($r.ExitCode)): $((Remove-Ansi $r.Text).Trim())" }
    }
    foreach ($req in $wanted) {
        $archPart = $req.Split('-')[3]
        $dir = @(Get-ChildItem -LiteralPath $pyRoot -Directory -Filter "cpython-3.13*-windows-$archPart-none" -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($dir.Count -gt 0 -and (Test-Path -LiteralPath (Join-Path $dir[0].FullName 'python.exe'))) { $script:PyDirs += $dir[0].FullName }
    }
    if ($script:PyDirs.Count -eq 0) { throw "no Python could be provisioned in the work dir" }
    $script:NativePython = Join-Path $script:PyDirs[0] 'python.exe'
}

# The child environment every installer run gets. Studio home, uv, its Python store, caches and TEMP
# all live in the work dir; the work-dir uv and Pythons go LAST on PATH, so a machine's own Python
# still wins exactly as it would for a real install, and only a machine without one uses ours.
function Get-IsolationEnv {
    param([string]$StudioHome, [switch]$NoToolPath, [string]$Overlay = '')
    $e = @{
        UNSLOTH_STUDIO_HOME = $StudioHome
        UV_INSTALL_DIR = (Join-Path $script:Work 'tools\uvi')
        UV_PYTHON_INSTALL_DIR = (Join-Path $script:Work 'tools\py')
        UV_PYTHON_INSTALL_BIN = '0'
        UV_PYTHON_INSTALL_REGISTRY = '0'
        UV_NO_MODIFY_PATH = '1'
        UV_CACHE_DIR = (Join-Path $script:Work 'cache\uv')
        PIP_CACHE_DIR = (Join-Path $script:Work 'cache\pip')
        UNSLOTH_SKIP_AUTOSTART = '1'
        UNSLOTH_DISABLE_AUTO_UPDATES = '1'
        TEMP = (Join-Path $script:Work 'tmp')
        TMP = (Join-Path $script:Work 'tmp')
        UNSLOTH_CI_SOURCE_OVERLAY = $null
    }
    if ($Overlay) { $e.UNSLOTH_CI_SOURCE_OVERLAY = $Overlay }
    [void][System.IO.Directory]::CreateDirectory($e.TEMP)
    if (-not $NoToolPath) {
        $tail = @()
        if ($script:UvExe) { $tail += (Split-Path -Parent $script:UvExe) }
        $tail += @($script:PyDirs)
        $e.PATH = (@($env:PATH) + $tail) -join ';'
    }
    return $e
}

# ---------------------------------------------------------------- states

function Get-ZipComment {
    param([string]$Path)
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $len = [int][Math]::Min(65557, $fs.Length)
        [void]$fs.Seek(-$len, 'End')
        $buf = New-Object byte[] $len
        [void]$fs.Read($buf, 0, $len)
        for ($i = $len - 22; $i -ge 0; $i--) {
            if ($buf[$i] -eq 0x50 -and $buf[$i + 1] -eq 0x4B -and $buf[$i + 2] -eq 5 -and $buf[$i + 3] -eq 6) {
                $cl = [int]$buf[$i + 20] + 256 * [int]$buf[$i + 21]
                return [System.Text.Encoding]::ASCII.GetString($buf, $i + 22, $cl)
            }
        }
    } finally { $fs.Dispose() }
    return $null
}

function Expand-StateZip {
    param([string]$Zip, [string]$Dest)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destFull = [System.IO.Path]::GetFullPath($Dest).TrimEnd('\') + '\'
    $za = [System.IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        foreach ($e in $za.Entries) {
            $rel = $e.FullName
            $i = $rel.IndexOf('/')
            if ($i -lt 0) { continue }
            $rel = $rel.Substring($i + 1)
            if (-not $rel) { continue }
            $target = [System.IO.Path]::GetFullPath((Join-Path $Dest ($rel -replace '/', '\')))
            if (-not $target.StartsWith($destFull, [StringComparison]::OrdinalIgnoreCase)) { throw "zip entry escapes the state dir: $($e.FullName)" }
            if ($rel.EndsWith('/')) { [void][System.IO.Directory]::CreateDirectory($target); continue }
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target))
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
        }
    } finally { $za.Dispose() }
}

function Initialize-State {
    param([string]$Name)
    $m = $script:Manifest.states.$Name
    $row = [ordered]@{ sha = [string]$m.sha; repo = [string]$m.repo; verified = $false; problems = @() }
    $script:Results.states[$Name] = $row
    try {
        $zip = Join-Path $script:Work "src\$Name.zip"
        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $zip))
        Write-Diag "fetching $Name ($($m.repo) $($m.sha.Substring(0, 12)))"
        Get-Download -Url ([string]$m.zip_url) -Dest $zip
        $comment = Get-ZipComment $zip
        if ($comment -ne [string]$m.sha) { $row.problems += "archive comment '$comment' is not the pinned sha" }
        $dir = Join-Path $script:Work "r\$Name"
        Expand-StateZip -Zip $zip -Dest $dir
        Remove-Item -LiteralPath $zip -Force
        foreach ($p in $m.files.PSObject.Properties) {
            $f = Join-Path $dir ($p.Name -replace '/', '\')
            $h = Get-FileSha $f
            if ($h -ne [string]$p.Value) { $row.problems += "$($p.Name) hash $h does not match the manifest" }
        }
        $row.verified = ($row.problems.Count -eq 0)
        if ($row.verified) { $script:StateDirs[$Name] = $dir }
        else { Add-DiagError "state $Name failed verification: $($row.problems -join '; ')" }
    } catch {
        $row.problems += $_.Exception.Message
        Add-DiagError "state $Name could not be fetched: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- decision runs

function ConvertTo-Family {
    param([string]$Url)
    if (-not $Url) { return $null }
    $leaf = ((($Url -split '[?#]', 2)[0].TrimEnd('/') -split '/')[-1]).ToLowerInvariant()
    if ($leaf -match '^cu\d+$' -or $leaf -eq 'cpu' -or $leaf -eq 'xpu') { return $leaf }
    if ($leaf -match '^rocm' -or $leaf -match '^gfx[0-9]') { return 'rocm' }
    return 'auto'
}

function Get-InstallDecision {
    param([string]$Text)
    $clean = Remove-Ansi $Text
    $gpu = $null; $url = $null; $skipped = $false; $notes = @()
    foreach ($l in ($clean -split '\r?\n')) {
        if ($l -match '^\s{1,4}gpu\s{2,}(\S.*?)\s*$') { $gpu = $Matches[1] }
        if (-not $url -and $l -match 'installing PyTorch (?:from )?\(?(https?://[^\s)]+?)\)?(?:\.\.\.)?\s*$') { $url = $Matches[1] }
        if ($l -match 'skipping PyTorch') { $skipped = $true }
        if ($l -match '(?i)elevat|administrator|icacls|private director|path exactly|declin|nvidia|cuda|compute capab|windows on arm') {
            $t = $l.Trim()
            if ($t -and $notes.Count -lt 60) { $notes += $t }
        }
    }
    $family = ConvertTo-Family $url
    if ($skipped -and -not $family) { $family = 'none' }
    return [ordered]@{
        gpu_line = $gpu; torch_url = $url; family = $family; reached = [bool]($url -or $skipped)
        path_warn = [bool]($clean -match 'Could not resolve a path exactly'); notes = $notes
    }
}

function Get-UpdateDecision {
    param([string]$Text)
    foreach ($l in ((Remove-Ansi $Text) -split '\r?\n')) {
        if ($l -match 'installing PyTorch with CUDA support \(([^)]+)\)') { return $Matches[1].ToLowerInvariant() }
        if ($l -match 'installing PyTorch \(CPU-only\)') { return 'cpu' }
        if ($l -match 'installing PyTorch \(AMD ROCm') { return 'rocm' }
        if ($l -match 'installing PyTorch \(Intel XPU\)') { return 'xpu' }
    }
    return $null
}

function Get-ShellExe {
    param([string]$Shell)
    if ($Shell -eq 'powershell') { return (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') }
    return $script:PwshExe
}

# install.ps1 in env mode with dead torch mirrors: detection runs and prints its decision, and the
# run is stopped at the first "installing PyTorch" line, before any wheel is fetched. Nothing ahead
# of that line downloads more than uv's own venv bootstrap (and, on Windows on ARM, a pyarrow wheel).
function Invoke-DecisionRun {
    param([string]$State, [string]$Shell)
    $sd = $script:StateDirs[$State]
    $exe = Get-ShellExe $Shell
    $studioDir = Join-Path $script:Work "h\$State-$Shell"
    [void][System.IO.Directory]::CreateDirectory($studioDir)
    $envT = Get-IsolationEnv -StudioHome $studioDir
    $envT.UNSLOTH_PYTORCH_MIRROR = $DeadMirror
    $envT.UNSLOTH_ROCM_WINDOWS_MIRROR = $DeadMirror
    $envT.UNSLOTH_MIRROR_FALLBACK = '0'
    $envT.UNSLOTH_INSTALL_RETRIES = '1'
    Write-Diag "decision run: $State under $Shell"
    $r = Invoke-Bounded -Label "decision_${State}_$Shell" -CommandLine "$(Get-QuotedExe $exe) -NoProfile -ExecutionPolicy Bypass -File `"$sd\install.ps1`"" `
        -WorkDir $sd -Timeout $DecisionTimeoutSec -DoneMarker 'installing PyTorch|skipping PyTorch' -GraceSec 2 -Env $envT
    $d = Get-InstallDecision $r.Text
    $row = [ordered]@{
        state = $State; shell = $Shell; reached = $d.reached; gpu_line = $d.gpu_line; torch_url = $d.torch_url
        family = $d.family; path_warn = $d.path_warn; elapsed_s = $r.ElapsedSec; exit = $r.ExitCode
        timed_out = $r.TimedOut; transcript = (Save-Transcript "decision_${State}_$Shell" $r.Text); notes = $d.notes
        mode = $script:Mode
    }
    $script:Results.decisions += $row
    Write-Diag ("  gpu: {0} | torch: {1} | {2}s" -f $d.gpu_line, $d.family, $r.ElapsedSec)
    [void](Remove-TreeNoFollow -Path $studioDir)
}

# ---------------------------------------------------------------- repo tests

function Get-FailedChecks {
    param([string]$Text)
    $out = @()
    foreach ($l in ((Remove-Ansi $Text) -split '\r?\n')) {
        if ($l -match '^\s*FAIL\s+(.+?)\s*$') { $out += $Matches[1] }
    }
    return $out
}

function Invoke-Ps1Tests {
    param([string]$State, [string]$Shell, [string]$Filter = '')
    $sd = $script:StateDirs[$State]
    $exe = Get-ShellExe $Shell
    $files = @(Get-ChildItem -LiteralPath (Join-Path $sd 'tests\studio') -Filter '*.ps1' -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($Filter) { $files = @($files | Where-Object { $_.Name -match $Filter }) }
    Write-Diag "ps1 tests: $State under $Shell ($($files.Count) files)"
    $envT = Get-IsolationEnv -StudioHome (Join-Path $script:Work "h\t-$State-$Shell")
    foreach ($f in $files) {
        $rel = "tests/studio/$($f.Name)"
        $label = "ps1_${State}_${Shell}_$($f.BaseName)"
        $r = Invoke-Bounded -Label $label -CommandLine "$(Get-QuotedExe $exe) -NoProfile -ExecutionPolicy Bypass -File `"$($f.FullName)`"" `
            -WorkDir $sd -Timeout $TestTimeoutSec -Env $envT
        $failed = @(Get-FailedChecks $r.Text)
        $passed = ($r.ExitCode -eq 0) -and ($failed.Count -eq 0) -and (-not $r.TimedOut)
        $flaky = $false; $firstText = $null
        if (-not $passed -and -not $r.TimedOut) {
            # One retry: a real driver under load can drop a single probe answer. A pass on the
            # retry is recorded as flaky, with the first transcript kept.
            $firstText = $r.Text; $firstFailed = $failed
            $r = Invoke-Bounded -Label "${label}_retry" -CommandLine "$(Get-QuotedExe $exe) -NoProfile -ExecutionPolicy Bypass -File `"$($f.FullName)`"" `
                -WorkDir $sd -Timeout $TestTimeoutSec -Env $envT
            $failed = @(Get-FailedChecks $r.Text)
            $passed = ($r.ExitCode -eq 0) -and ($failed.Count -eq 0) -and (-not $r.TimedOut)
            if ($passed) { $flaky = $true; $failed = @($firstFailed) }
        }
        $row = [ordered]@{ state = $State; shell = $Shell; kind = 'ps1'; file = $rel; present = $true; exit = $r.ExitCode
            passed = $passed; failed_checks = $failed; timed_out = $r.TimedOut; elapsed_s = $r.ElapsedSec; transcript = $null; mode = $script:Mode; flaky = $flaky }
        if (-not $passed) { $row.transcript = Save-Transcript "tests/$label" $r.Text }
        elseif ($flaky) { $row.transcript = Save-Transcript "tests/${label}_flaky_first" $firstText }
        $script:Results.tests += $row
    }
    [void](Remove-TreeNoFollow -Path (Join-Path $script:Work "h\t-$State-$Shell"))
}

function Initialize-PytestVenv {
    if ($script:PytestPython) { return $true }
    $venv = Join-Path $script:Work 'tools\pytest-venv'
    $envT = Get-IsolationEnv -StudioHome $null
    $r = Invoke-Bounded -Label 'pytest_venv' -CommandLine "$(Get-QuotedExe $script:UvExe) venv --python `"$script:NativePython`" `"$venv`"" -WorkDir $script:Work -Timeout 300 -Env $envT
    $py = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) { Add-DiagError "pytest venv could not be created: $((Remove-Ansi $r.Text).Trim())"; return $false }
    $r = Invoke-Bounded -Label 'pytest_deps' -CommandLine "$(Get-QuotedExe $script:UvExe) pip install --python `"$py`" pytest pyyaml packaging" -WorkDir $script:Work -Timeout 600 -Env $envT
    if ($r.ExitCode -ne 0) { Add-DiagError "pytest dependencies could not be installed: $((Remove-Ansi $r.Text).Trim())"; return $false }
    $script:PytestPython = $py
    return $true
}

function Get-PytestFiles {
    param([string]$StateDir, [string]$Filter = '')
    $files = @()
    foreach ($pat in @('tests\python\test_windows_*.py', 'tests\python\test_installer_*.py', 'tests\studio\test_windows_*.py', 'tests\studio\test_installer_*.py', 'tests\python\test_cross_platform_parity.py')) {
        $files += @(Get-ChildItem -Path (Join-Path $StateDir $pat) -File -ErrorAction SilentlyContinue)
    }
    $files = @($files | Where-Object { $_.Name -ne 'test_installer_av_shapes.py' } | Sort-Object FullName -Unique)
    if ($Filter) { $files = @($files | Where-Object { $_.Name -match $Filter }) }
    return $files
}

function Invoke-Pytests {
    param([string]$State, [string]$Filter = '')
    if (-not (Initialize-PytestVenv)) { return }
    $sd = $script:StateDirs[$State]
    $files = @(Get-PytestFiles $sd $Filter)
    Write-Diag "pytest: $State ($($files.Count) files)"
    $envT = Get-IsolationEnv -StudioHome (Join-Path $script:Work "h\p-$State")
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($sd.Length + 1) -replace '\\', '/'
        $label = "pytest_${State}_$($f.BaseName)"
        $xml = Join-Path $script:RunDir "$label.xml"
        $r = Invoke-Bounded -Label $label -CommandLine "$(Get-QuotedExe $script:PytestPython) -m pytest -q -p no:cacheprovider --junitxml=`"$xml`" `"$($f.FullName)`"" `
            -WorkDir $sd -Timeout ($TestTimeoutSec * 3) -Env $envT
        $failed = @()
        if (Test-Path -LiteralPath $xml) {
            try {
                [xml]$doc = Get-Content -LiteralPath $xml -Raw
                foreach ($tc in @($doc.SelectNodes('//testcase'))) {
                    if ($tc.SelectSingleNode('failure') -or $tc.SelectSingleNode('error')) { $failed += "$($tc.GetAttribute('classname'))::$($tc.GetAttribute('name'))" }
                }
            } catch { }
        }
        $passed = ($r.ExitCode -eq 0) -and (-not $r.TimedOut)
        $row = [ordered]@{ state = $State; shell = 'python'; kind = 'pytest'; file = $rel; present = $true; exit = $r.ExitCode
            passed = $passed; failed_checks = $failed; timed_out = $r.TimedOut; elapsed_s = $r.ElapsedSec; transcript = $null; mode = $script:Mode }
        if (-not $passed) { $row.transcript = Save-Transcript "tests/$label" $r.Text }
        $script:Results.tests += $row
    }
    [void](Remove-TreeNoFollow -Path (Join-Path $script:Work "h\p-$State"))
}

# Rows for test files that exist in some states and not others, so a head-only test is visible.
function Add-AbsentTestRows {
    $byKey = @{}
    foreach ($t in $script:Results.tests) { $byKey["$($t.kind)|$($t.shell)|$($t.file)|$($t.state)"] = $true }
    $combos = @{}
    foreach ($t in $script:Results.tests) { $combos["$($t.kind)|$($t.shell)|$($t.file)"] = $t }
    $testedStates = @($script:Results.tests | ForEach-Object { "$($_.state)|$($_.shell)" } | Sort-Object -Unique)
    foreach ($c in $combos.Keys) {
        $t = $combos[$c]
        foreach ($ss in $testedStates) {
            $parts = $ss.Split('|')
            if ($parts[1] -ne $t.shell) { continue }
            if (-not $byKey.ContainsKey("$c|$($parts[0])")) {
                $script:Results.tests += [ordered]@{ state = $parts[0]; shell = $t.shell; kind = $t.kind; file = $t.file; present = $false
                    exit = $null; passed = $null; failed_checks = @(); timed_out = $false; elapsed_s = 0; transcript = $null; mode = $script:Mode }
            }
        }
    }
}

# ---------------------------------------------------------------- NVIDIA probe and presence

# The helpers are nested inside install.ps1's main function, so they are lifted out the way the
# repo tests do it: parse the file, take the named functions and every function they call.
function Get-FunctionClosureText {
    param([string]$Path, [string[]]$Roots)
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errs)
    $defs = @{}
    foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if (-not $defs.ContainsKey($f.Name)) { $defs[$f.Name] = $f }
    }
    $seen = @{}; $order = New-Object System.Collections.Generic.List[string]
    $queue = New-Object System.Collections.Queue
    foreach ($r in $Roots) { if ($defs.ContainsKey($r)) { $queue.Enqueue($r) } }
    while ($queue.Count -gt 0) {
        $n = [string]$queue.Dequeue()
        if ($seen.ContainsKey($n)) { continue }
        $seen[$n] = $true; $order.Add($n)
        foreach ($c in $defs[$n].Body.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            $cn = $c.GetCommandName()
            if ($cn -and $defs.ContainsKey($cn) -and -not $seen.ContainsKey($cn)) { $queue.Enqueue($cn) }
        }
    }
    $text = ($order | ForEach-Object { $defs[$_].Extent.Text }) -join "`r`n`r`n"
    return [pscustomobject]@{ Text = $text; Found = @($Roots | Where-Object { $defs.ContainsKey($_) }) }
}

function New-ProbeScript {
    param([string]$State)
    $path = Join-Path $script:RunDir "probe_$State.ps1"
    if (Test-Path -LiteralPath $path) { return $path }
    $c = Get-FunctionClosureText -Path (Join-Path $script:StateDirs[$State] 'install.ps1') -Roots @('Get-NvidiaLibraryInventory', 'Read-NvidiaLibraryRaw', 'Test-NvidiaAdapterPresent')
    $head = @'
param([string]$OutJson, [string]$PythonExe)
$ErrorActionPreference = 'Continue'
$VenvPython = $PythonExe
$script:NvidiaLibraryInventoryProbed = $false
$script:NvidiaLibraryInventory = $null
'@
    $tail = @'

$r = [ordered]@{ inventory_fn = $false; raw = $null; cuda = $null; count = $null; cc = @(); error = $null; presence_fn = $false; nvidia_present = $null; presence_error = $null }
if (Get-Command Read-NvidiaLibraryRaw -CommandType Function -ErrorAction SilentlyContinue) {
    try { $r.raw = [string](Read-NvidiaLibraryRaw -TimeoutMs 30000) } catch { $r.error = "raw: $($_.Exception.Message)" }
}
if (Get-Command Get-NvidiaLibraryInventory -CommandType Function -ErrorAction SilentlyContinue) {
    $r.inventory_fn = $true
    $script:NvidiaLibraryInventoryProbed = $false
    try {
        $inv = $null
        try { $inv = Get-NvidiaLibraryInventory -TimeoutSec 30 } catch { $inv = Get-NvidiaLibraryInventory }
        if ($inv) { $r.cuda = "$($inv.CudaMajor).$($inv.CudaMinor)"; $r.count = $inv.Count; $r.cc = @($inv.ComputeCaps) }
    } catch { $r.error = $_.Exception.Message }
}
if (Get-Command Test-NvidiaAdapterPresent -CommandType Function -ErrorAction SilentlyContinue) {
    $r.presence_fn = $true
    try { $r.nvidia_present = [bool](Test-NvidiaAdapterPresent) } catch { $r.presence_error = $_.Exception.Message }
}
[System.IO.File]::WriteAllText($OutJson, ($r | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
'@
    Write-TextFile -Path $path -Text ($head + "`r`n" + $c.Text + "`r`n" + $tail) -Bom
    return $path
}

function Invoke-ProbeCheck {
    param([string]$State, [string]$Shell)
    $exe = Get-ShellExe $Shell
    $probeRow = [ordered]@{ state = $State; shell = $Shell; available = $false; raw = $null; cuda = $null; count = $null; cc = @(); error = $null }
    $presRow = [ordered]@{ state = $State; shell = $Shell; available = $false; nvidia_present = $null; error = $null }
    try {
        $probePs = New-ProbeScript $State
        $json = Join-Path $script:RunDir "probe_${State}_$Shell.json"
        $r = Invoke-Bounded -Label "probe_${State}_$Shell" -CommandLine "$(Get-QuotedExe $exe) -NoProfile -ExecutionPolicy Bypass -File `"$probePs`" -OutJson `"$json`" -PythonExe `"$script:NativePython`"" `
            -WorkDir $script:StateDirs[$State] -Timeout 180 -Env (Get-IsolationEnv -StudioHome $null)
        if (Test-Path -LiteralPath $json) {
            $o = Get-Content -LiteralPath $json -Raw | ConvertFrom-Json
            $probeRow.available = [bool]$o.inventory_fn; $probeRow.raw = $o.raw; $probeRow.cuda = $o.cuda; $probeRow.count = $o.count
            $probeRow.cc = @($o.cc | Where-Object { $_ }); $probeRow.error = $o.error
            $presRow.available = [bool]$o.presence_fn; $presRow.nvidia_present = $o.nvidia_present; $presRow.error = $o.presence_error
        } else {
            $err = "probe produced no result (exit $($r.ExitCode), timed out $($r.TimedOut))"
            $probeRow.error = $err; $presRow.error = $err
            [void](Save-Transcript "probe_${State}_$Shell" $r.Text)
        }
    } catch { $probeRow.error = $_.Exception.Message; $presRow.error = $_.Exception.Message }
    $script:Results.probe += $probeRow
    $script:Results.presence += $presRow
}

function Invoke-SmokeCheck {
    param([string]$State, [string]$Shell)
    $exe = Get-ShellExe $Shell
    $sd = $script:StateDirs[$State]
    $smoke = Join-Path $script:RunDir 'smoke.ps1'
    if (-not (Test-Path -LiteralPath $smoke)) {
        Write-TextFile -Path $smoke -Text @'
param([string]$Root)
$n = 0
foreach ($f in @('install.ps1', 'studio\setup.ps1', 'scripts\uninstall.ps1')) {
    $t = $null; $e = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root $f), [ref]$t, [ref]$e)
    foreach ($x in @($e)) { if ($x) { $n++; Write-Output "PARSE $f $($x.Extent.StartLineNumber): $($x.Message)" } }
}
Write-Output "PARSE_ERRORS=$n"
'@
    }
    $r = Invoke-Bounded -Label "smoke_${State}_$Shell" -CommandLine "$(Get-QuotedExe $exe) -NoProfile -ExecutionPolicy Bypass -File `"$smoke`" -Root `"$sd`"" -WorkDir $sd -Timeout 120
    $n = $null
    if ($r.Text -match 'PARSE_ERRORS=(\d+)') { $n = [int]$Matches[1] }
    if ($null -eq $n -or $n -gt 0) { [void](Save-Transcript "smoke_${State}_$Shell" $r.Text) }
    $script:Results.smoke += [ordered]@{ state = $State; shell = $Shell; parse_errors = $n; help_exit = $null }
}

# ---------------------------------------------------------------- full pass

function Get-ShortcutReport {
    $rows = @(); $dangling = 0
    $sh = $null
    try { $sh = New-Object -ComObject WScript.Shell } catch { }
    foreach ($p in (Get-ShortcutPaths)) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $row = [ordered]@{ path = $p; target = $null; arguments = $null; icon = $null; workdir = $null; target_exists = $null; script_exists = $null }
        if ($sh) {
            try {
                $l = $sh.CreateShortcut($p)
                $row.target = $l.TargetPath; $row.arguments = $l.Arguments; $row.icon = $l.IconLocation; $row.workdir = $l.WorkingDirectory
                $row.target_exists = [bool]($l.TargetPath -and (Test-Path -LiteralPath $l.TargetPath))
                if ($l.Arguments -match '-File\s+"([^"]+)"') { $row.script_exists = (Test-Path -LiteralPath $Matches[1]) }
                if (-not $row.target_exists -or $row.script_exists -eq $false) { $dangling++ }
            } catch { $row.target = "error: $($_.Exception.Message)"; $dangling++ }
        }
        $rows += $row
    }
    return [ordered]@{ ok = ($rows.Count -eq 2 -and $dangling -eq 0); count = $rows.Count; dangling = $dangling; rows = $rows }
}

function Get-UserPathEntries {
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
    if (-not $k) { return @() }
    try { return @(([string]$k.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)).Split(';') | Where-Object { $_ }) }
    finally { $k.Close() }
}

function Get-FreePort {
    $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $l.Start(); $p = $l.LocalEndpoint.Port; $l.Stop()
    return $p
}

function Test-FullPreflight {
    $problems = @()
    if (-not (Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue)) { $problems += 'git is missing: studio/setup.ps1 would run "winget install Git.Git"' }
    if (-not (Test-VcRedist)) { $problems += 'the VC++ 2015-2022 x64 runtime is missing: studio/setup.ps1 would install it machine-wide (UAC)' }
    $lp = $null
    try { $lp = [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled } catch { }
    if ($lp -ne 1) { $problems += 'LongPathsEnabled is off: studio/setup.ps1 would raise a UAC prompt to turn it on' }
    return $problems
}

function Invoke-Park {
    foreach ($d in @((Join-Path $env:USERPROFILE '.unsloth'), (Join-Path $env:LOCALAPPDATA 'Unsloth Studio'))) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        $item = Get-Item -LiteralPath $d -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw "$d is a link; refusing to park it" }
        $parkedName = "$([System.IO.Path]::GetFileName($d)).diag-parked-$($script:Stamp)"
        $parked = Join-Path (Split-Path -Parent $d) $parkedName
        # Journal first, rename second: a crash in between is recognised by -Recover.
        $script:Journal.parked += [ordered]@{ original = $d; parked = $parked }
        Save-Journal
        Rename-Item -LiteralPath $d -NewName $parkedName -ErrorAction Stop
        Write-Diag "parked $d -> $parkedName"
    }
}

function Invoke-FullState {
    param([string]$State)
    $sd = $script:StateDirs[$State]
    $ps51 = Get-ShellExe 'powershell'
    $row = [ordered]@{ state = $State; install_exit = $null; install_timed_out = $null; family = $null; torch = $null; health_ok = $null
        update_exit = $null; update_tag = $null; shortcuts_only_exit = $null; shortcuts = $null; path_added = @(); uninstall_exit = $null
        shortcuts_removed = $null; transcripts = @() }
    $script:Results.full += $row
    $envT = Get-IsolationEnv -StudioHome $null -Overlay $sd
    $pathBefore = @(Get-UserPathEntries | ForEach-Object { Get-PathKey $_ })

    Write-Diag "full install: $State (this is the long step)"
    $r = Invoke-Bounded -Label "full_install_$State" -CommandLine "$(Get-QuotedExe $ps51) -NoProfile -ExecutionPolicy Bypass -File `"$sd\install.ps1`"" `
        -WorkDir $sd -Timeout $FullInstallTimeoutSec -Env $envT
    $row.install_exit = $r.ExitCode; $row.install_timed_out = $r.TimedOut
    $row.family = (Get-InstallDecision $r.Text).family
    $row.transcripts += Save-Transcript "full_install_$State" $r.Text
    Write-Diag "  install exit $($r.ExitCode), torch $($row.family)"

    $scripts = Join-Path $env:USERPROFILE '.unsloth\studio\unsloth_studio\Scripts'
    $venvPy = Join-Path $scripts 'python.exe'
    $unslothExe = Join-Path $scripts 'unsloth.exe'
    if (Test-Path -LiteralPath $venvPy) {
        $check = Join-Path $script:RunDir 'torch_check.py'
        Write-TextFile -Path $check -Text @'
import json
r = {"version": None, "cuda_available": None, "device": None, "matmul_ok": None, "error": None}
try:
    import torch
    r["version"] = torch.__version__
    r["cuda_available"] = bool(torch.cuda.is_available())
    dev = None
    if r["cuda_available"]:
        dev = "cuda"
        r["device"] = torch.cuda.get_device_name(0)
    elif getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
        dev = "xpu"
        r["device"] = torch.xpu.get_device_name(0)
    if dev:
        torch.manual_seed(0)
        a = torch.randn(1024, 1024)
        b = torch.randn(1024, 1024)
        got = (a.to(dev) @ b.to(dev)).cpu()
        r["matmul_ok"] = bool(torch.allclose(a @ b, got, rtol=1e-2, atol=1e-2))
except Exception as e:
    r["error"] = repr(e)
print("DIAGJSON " + json.dumps(r))
'@
        $t = Invoke-Bounded -Label "torch_$State" -CommandLine "$(Get-QuotedExe $venvPy) `"$check`"" -WorkDir $script:Work -Timeout 600 -Env $envT
        if ($t.Text -match 'DIAGJSON (\{.*\})') { $row.torch = ($Matches[1] | ConvertFrom-Json) }
        else { $row.torch = [ordered]@{ version = $null; cuda_available = $null; device = $null; matmul_ok = $null; error = "no result (exit $($t.ExitCode))" } }
    }

    if (Test-Path -LiteralPath $unslothExe) {
        $port = Get-FreePort
        $child = Start-HiddenChild -Label "studio_$State" -CommandLine "$(Get-QuotedExe $unslothExe) studio -H 127.0.0.1 -p $port" -WorkDir $script:Work -Env $envT
        $sp = $child.Proc
        $row.health_ok = $false
        $deadline = (Get-Date).AddSeconds(300)
        while ((Get-Date) -lt $deadline -and -not $sp.HasExited) {
            try {
                $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5
                if ($resp.StatusCode -eq 200) { $row.health_ok = $true; break }
            } catch { }
            Start-Sleep -Seconds 3
        }
        Stop-Tree $sp.Id
        $script:CurrentChild = $null
        $null = $sp.WaitForExit(15000)
        $row.transcripts += Save-Transcript "studio_$State" (Read-SharedText $child.Raw)
        Write-Diag "  studio health: $($row.health_ok)"

        $u = Invoke-Bounded -Label "update_$State" -CommandLine "$(Get-QuotedExe $unslothExe) studio update" -WorkDir $script:Work -Timeout 3600 -Env $envT
        $row.update_exit = $u.ExitCode; $row.update_tag = Get-UpdateDecision $u.Text
        $row.transcripts += Save-Transcript "update_$State" $u.Text
        Write-Diag "  update exit $($u.ExitCode), torch tag $($row.update_tag)"
    }

    $so = Invoke-Bounded -Label "shortcuts_only_$State" -CommandLine "$(Get-QuotedExe $ps51) -NoProfile -ExecutionPolicy Bypass -File `"$sd\install.ps1`" --shortcuts-only" `
        -WorkDir $sd -Timeout 600 -Env $envT
    $row.shortcuts_only_exit = $so.ExitCode
    $row.transcripts += Save-Transcript "shortcuts_only_$State" $so.Text
    $row.shortcuts = Get-ShortcutReport
    $row.path_added = @(Get-UserPathEntries | Where-Object { $pathBefore -notcontains (Get-PathKey $_) })
    Write-Diag "  shortcuts: $($row.shortcuts.count) found, $($row.shortcuts.dangling) dangling"

    $un = Invoke-Bounded -Label "uninstall_$State" -CommandLine "$(Get-QuotedExe $ps51) -NoProfile -ExecutionPolicy Bypass -File `"$sd\scripts\uninstall.ps1`"" `
        -WorkDir $sd -Timeout 900 -Env $envT
    $row.uninstall_exit = $un.ExitCode
    $row.transcripts += Save-Transcript "uninstall_$State" $un.Text
    $row.shortcuts_removed = (@(Get-ShortcutPaths | Where-Object { Test-Path -LiteralPath $_ }).Count -eq 0)
    Write-Diag "  uninstall exit $($un.ExitCode), shortcuts removed: $($row.shortcuts_removed)"
}

# ---------------------------------------------------------------- summary and packaging

function Get-Redactions {
    $pairs = @()
    $profiles = @()
    if ($env:USERPROFILE) { $profiles += $env:USERPROFILE }
    try {
        $short = (& (Join-Path $env:SystemRoot 'System32\cmd.exe') /d /c "for %I in (`"$env:USERPROFILE`") do @echo %~sI" 2>$null | Out-String).Trim()
        if ($short -and $short -ne $env:USERPROFILE) { $profiles += $short }
    } catch { }
    foreach ($p in $profiles) {
        $pairs += , @($p, '%USERPROFILE%')
        $pairs += , @($p.Replace('\', '\\'), '%USERPROFILE%')
        $pairs += , @($p.Replace('\', '/'), '%USERPROFILE%')
    }
    return $pairs
}

function Protect-Text {
    param([string]$Text)
    if (-not $Text) { return $Text }
    foreach ($pair in $script:Redactions) { $Text = [regex]::Replace($Text, [regex]::Escape($pair[0]), $pair[1], 'IgnoreCase') }
    if ($env:USERNAME -and $env:USERNAME.Length -ge 2) {
        $u = [regex]::Escape($env:USERNAME)
        $Text = [regex]::Replace($Text, '(?i)(\\{1,2}Users\\{1,2})' + $u + '(?=\\|/|"|''|\s|$)', '${1}<user>')
        if ($env:USERDOMAIN) { $Text = [regex]::Replace($Text, '(?i)\b' + [regex]::Escape($env:USERDOMAIN) + '(\\{1,2})' + $u + '\b', '<domain>${1}<user>') }
    }
    return $Text
}

function Protect-OutDir {
    foreach ($f in @(Get-ChildItem -LiteralPath $script:Out -Recurse -File -Include '*.txt', '*.json', '*.md')) {
        try {
            $t = [System.IO.File]::ReadAllText($f.FullName)
            $p = Protect-Text $t
            if ($p -ne $t) { [System.IO.File]::WriteAllText($f.FullName, $p, $Utf8NoBom) }
        } catch { }
    }
}

function Format-Cell { param($v) if ($null -eq $v -or "$v" -eq '') { return '-' } return ("$v" -replace '\|', '/') }

function New-SummaryMarkdown {
    $R = $script:Results
    $L = New-Object System.Collections.Generic.List[string]
    $L.Add("# Unsloth Windows installer diagnostic ($($R.mode) pass)")
    $L.Add('')
    $L.Add("tool $($R.tool_version), $((Get-Date).ToString('yyyy-MM-dd HH:mm'))")
    $L.Add('')
    $m = $R.machine
    $L.Add("Machine: $($m.os_caption) build $($m.os_build), OS arch $($m.os_arch), PowerShell process arch $($m.ps_arch), elevated $($m.elevated)")
    $L.Add("GPUs: $(@($m.gpus) -join '; ')")
    $L.Add("nvidia-smi: $($m.nvidia_smi), CUDA $($m.smi_cuda), compute capability $(@($m.smi_cc) -join ','); NVIDIA PCI adapters in WMI: $($m.nvidia_ven_adapters)")
    if ($m.spoof) { $L.Add("SPOOFED GPU ($($m.spoof)): stand-in NVIDIA binaries report CUDA $($m.expect_cuda), compute capability $(@($m.expect_cc) -join ',')") }
    $L.Add('')
    $L.Add('| state | sha | verified |'); $L.Add('|---|---|---|')
    foreach ($k in $R.states.Keys) { $L.Add("| $k | $($R.states[$k].sha.Substring(0, 12)) | $($R.states[$k].verified) |") }
    if ($R.decisions.Count -gt 0) {
        $L.Add(''); $L.Add('## Install decisions (dead torch mirror, stopped at the torch step)'); $L.Add('')
        $L.Add('| state | shell | reached | gpu line | torch | vs base | path warning | seconds |'); $L.Add('|---|---|---|---|---|---|---|---|')
        foreach ($d in $R.decisions) {
            $base = @($R.decisions | Where-Object { $_.state -eq 'base' -and $_.shell -eq $d.shell -and $_.mode -eq $d.mode } | Select-Object -First 1)
            $cmp = '-'
            if ($d.state -ne 'base' -and $base.Count -gt 0) {
                if (-not $d.reached -or -not $base[0].reached) { $cmp = 'VOID' }
                elseif ($d.family -eq $base[0].family) { $cmp = 'SAME' }
                else { $cmp = "$($base[0].family) -> $($d.family)" }
            }
            $L.Add("| $($d.state) | $($d.shell) | $($d.reached) | $(Format-Cell $d.gpu_line) | $(Format-Cell $d.family) | $cmp | $($d.path_warn) | $($d.elapsed_s) |")
        }
    }
    if ($R.probe.Count -gt 0) {
        $L.Add(''); $L.Add('## NVIDIA library probe vs nvidia-smi'); $L.Add('')
        $L.Add('| state | shell | probe present | CUDA | compute caps | matches nvidia-smi | adapter present (#11166) | error |'); $L.Add('|---|---|---|---|---|---|---|---|')
        foreach ($p in $R.probe) {
            $pres = @($R.presence | Where-Object { $_.state -eq $p.state -and $_.shell -eq $p.shell } | Select-Object -First 1)
            $match = '-'
            $wantCuda = $m.smi_cuda; $wantCc = @($m.smi_cc)
            if (-not $m.nvidia_smi -and $m.expect_cuda) { $wantCuda = $m.expect_cuda; $wantCc = @($m.expect_cc) }
            if ($wantCuda -and $p.cuda) {
                $ccOk = ((@($p.cc) | Sort-Object -Unique) -join ',') -eq ((@($wantCc) | Sort-Object -Unique) -join ',')
                $match = [string](($p.cuda -eq $wantCuda) -and $ccOk)
            }
            $presVal = '-'
            if ($pres.Count -gt 0 -and $pres[0].available) { $presVal = [string]$pres[0].nvidia_present }
            $L.Add("| $($p.state) | $($p.shell) | $($p.available) | $(Format-Cell $p.cuda) | $(Format-Cell (@($p.cc) -join ',')) | $match | $presVal | $(Format-Cell $p.error) |")
        }
    }
    if ($R.tests.Count -gt 0) {
        $L.Add(''); $L.Add('## Repo tests on this machine'); $L.Add('')
        $L.Add('| state | shell | kind | files | passed | failed | timed out |'); $L.Add('|---|---|---|---|---|---|---|')
        $groups = $R.tests | Where-Object { $_.present } | Group-Object { "$($_.state)|$($_.shell)|$($_.kind)" }
        foreach ($g in $groups) {
            $p = $g.Name.Split('|')
            $ok = @($g.Group | Where-Object { $_.passed }).Count
            $to = @($g.Group | Where-Object { $_.timed_out }).Count
            $L.Add("| $($p[0]) | $($p[1]) | $($p[2]) | $($g.Count) | $ok | $($g.Count - $ok) | $to |")
        }
        $L.Add(''); $L.Add('Failing at a head state but passing at base (same shell):'); $L.Add('')
        $any = $false
        foreach ($t in @($R.tests | Where-Object { $_.state -ne 'base' -and $_.present -and -not $_.passed })) {
            $b = @($R.tests | Where-Object { $_.state -eq 'base' -and $_.shell -eq $t.shell -and $_.file -eq $t.file -and $_.present } | Select-Object -First 1)
            $label = 'head-only test'
            if ($b.Count -gt 0) { if ($b[0].passed) { $label = 'NEW FAILURE' } else { $label = 'also fails at base' } }
            if ($label -eq 'also fails at base') { continue }
            $any = $true
            $L.Add("- $($t.state) / $($t.shell) / $($t.file): $label; $(@($t.failed_checks | Select-Object -First 5) -join '; ')")
        }
        if (-not $any) { $L.Add('- none') }
    }
    if ($R.full.Count -gt 0) {
        $L.Add(''); $L.Add('## Full pass'); $L.Add('')
        $L.Add('| state | install | torch | cuda | matmul | health | update | shortcuts | dangling | uninstall | shortcuts removed |'); $L.Add('|---|---|---|---|---|---|---|---|---|---|---|')
        foreach ($f in $R.full) {
            $tv = '-'; $cu = '-'; $mm = '-'
            if ($f.torch) { $tv = Format-Cell $f.torch.version; $cu = Format-Cell $f.torch.cuda_available; $mm = Format-Cell $f.torch.matmul_ok }
            $sc = '-'; $dg = '-'
            if ($f.shortcuts) { $sc = $f.shortcuts.count; $dg = $f.shortcuts.dangling }
            $L.Add("| $($f.state) | $(Format-Cell $f.install_exit) | $tv | $cu | $mm | $(Format-Cell $f.health_ok) | $(Format-Cell $f.update_exit) $(Format-Cell $f.update_tag) | $sc | $dg | $(Format-Cell $f.uninstall_exit) | $(Format-Cell $f.shortcuts_removed) |")
        }
    }
    $L.Add(''); $L.Add('## Restore'); $L.Add('')
    if ($R.restore) {
        $L.Add("ok: $($R.restore.ok)")
        foreach ($c in @($R.restore.changes)) { $L.Add("- put back: $c") }
        foreach ($c in @($R.restore.conflicts)) { $L.Add("- changed by something else, left in place: $c") }
        foreach ($c in @($R.restore.failures)) { $L.Add("- COULD NOT PUT BACK: $c") }
    } else { $L.Add('not run') }
    if ($R.errors.Count -gt 0) { $L.Add(''); $L.Add('## Errors'); $L.Add(''); foreach ($e in $R.errors) { $L.Add("- $e") } }
    $L.Add(''); $L.Add('## Not covered by this script'); $L.Add('')
    $L.Add('- #10408 App Control / Smart App Control prepare stage (changes machine policy)')
    $L.Add('- antivirus behavioural checks (need a disposable VM)')
    $L.Add('- WSL and the signed Tauri desktop updater')
    return ($L -join "`r`n")
}

function New-OutputZip {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $machine = ($env:COMPUTERNAME -replace '[^A-Za-z0-9_-]', '')
    $zip = Join-Path $script:Work "unsloth-diag-$machine-$($script:Mode)-$($script:Stamp).zip"
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($script:Out, $zip)
    return $zip
}

# ---------------------------------------------------------------- recover

function Invoke-RecoverMode {
    $ptr = Join-Path $script:DiagRoot 'ACTIVE_JOURNAL.txt'
    if (-not (Test-Path -LiteralPath $ptr)) { Write-Diag 'nothing to recover: no interrupted run is recorded' 'Green'; return 0 }
    $script:JournalPath = (Get-Content -LiteralPath $ptr -Raw).Trim()
    if (-not (Test-Path -LiteralPath $script:JournalPath)) { Write-Diag "the recorded journal $script:JournalPath is gone" 'Red'; return 1 }
    $j = Get-Content -LiteralPath $script:JournalPath -Raw | ConvertFrom-Json
    $script:Work = [string]$j.work
    $script:Results = [ordered]@{ errors = @() }
    $rep = Invoke-Restore 'recover'
    foreach ($c in @($rep.changes)) { Write-Diag "put back: $c" }
    foreach ($c in @($rep.conflicts)) { Write-Diag "left in place (not ours): $c" 'Yellow' }
    foreach ($c in @($rep.failures)) { Write-Diag "COULD NOT PUT BACK: $c" 'Red' }
    if ($rep.ok) { Complete-Journal; Write-Diag 'recovered; the machine is back to the journalled state' 'Green'; return 0 }
    Write-Diag 'recovery could not put everything back (listed above). Send this output and the journal folder.' 'Yellow'
    return 1
}

# ---------------------------------------------------------------- main

$script:DiagRoot = Join-Path $env:LOCALAPPDATA 'unsloth-diag'
[void][System.IO.Directory]::CreateDirectory($script:DiagRoot)

if ($Recover) { exit (Invoke-RecoverMode) }

if ($Full -and $Elevated) { Write-Host 'Use -Full or -Elevated, not both.' -ForegroundColor Red; exit 2 }
$script:Mode = 'quick'
if ($Full) { $script:Mode = 'full' }
if ($Elevated) { $script:Mode = 'elevated' }
$isAdmin = Test-IsElevated
if ($script:Mode -eq 'elevated' -and -not $isAdmin) { Write-Host '-Elevated must be run from an administrator console.' -ForegroundColor Red; exit 2 }
if ($script:Mode -ne 'elevated' -and $isAdmin -and -not $AllowAdmin) { Write-Host 'This pass runs as a standard user. Open a normal (non-administrator) console, or use -Elevated.' -ForegroundColor Red; exit 2 }
if ([string]$ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { Write-Host "PowerShell is in $($ExecutionContext.SessionState.LanguageMode); this script needs FullLanguage." -ForegroundColor Red; exit 2 }
$ptrFile = Join-Path $script:DiagRoot 'ACTIVE_JOURNAL.txt'
if (Test-Path -LiteralPath $ptrFile) { Write-Host 'An earlier run did not finish. Run this script with -Recover first.' -ForegroundColor Red; exit 2 }

$script:Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:Work = Join-Path $script:DiagRoot $script:Stamp
$script:Out = Join-Path $script:Work 'out'
$script:RunDir = Join-Path $script:Work 'run'
foreach ($d in @($script:Work, $script:Out, $script:RunDir)) { [void][System.IO.Directory]::CreateDirectory($d) }
$script:LogPath = Join-Path $script:Out 'diag.log'
$script:JournalPath = Join-Path $script:Work 'journal.json'
$script:StateDirs = @{}
$script:CurrentChild = $null
$script:PytestPython = $null
$script:PwshExe = $null
$pw = @(Get-Command pwsh.exe -All -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($pw.Count -gt 0) { $script:PwshExe = $pw[0].Source }
$script:Results = [ordered]@{
    schema = 1; tool_version = $ToolVersion; mode = $script:Mode; machine = $null; states = [ordered]@{}
    decisions = @(); tests = @(); probe = @(); presence = @(); smoke = @(); full = @(); restore = $null; errors = @()
}
$exitCode = 0

try {
    if ($ManifestPath) { $script:Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json }
    else { $script:Manifest = $EmbeddedManifest | ConvertFrom-Json }

    Write-Diag "work dir: $script:Work"
    $script:Journal = New-Journal
    Save-Journal
    [System.IO.File]::WriteAllText($ptrFile, $script:JournalPath, $Utf8NoBom)

    Write-Diag 'collecting the machine inventory (read-only)'
    $inv = Get-Inventory
    Write-JsonFile -Path (Join-Path $script:Out 'inventory.json') -Object $inv
    $script:Results.machine = [ordered]@{
        os_caption = $inv.os.caption; os_build = "$($inv.os.build).$($inv.os.ubr)"; os_arch = $inv.os.host_arch; ps_arch = $inv.os.ps_process_arch
        gpus = @($inv.gpus | ForEach-Object { $_.name }); nvidia_smi = [bool]($inv.nvidia_smi.path -and $inv.nvidia_smi.cc.Count -gt 0)
        smi_cuda = $inv.nvidia_smi.banner_cuda; smi_cc = @($inv.nvidia_smi.cc); elevated = $inv.elevated
        nvidia_ven_adapters = @($inv.gpus | Where-Object { $_.nvidia_ven -and "$($_.config_error)" -eq '0' }).Count
        # Set only by the staging dry run that plants stand-in NVIDIA binaries: what they report.
        spoof = $env:UNSLOTH_DIAG_SPOOF
        expect_cuda = $env:UNSLOTH_DIAG_EXPECT_CUDA
        expect_cc = @("$env:UNSLOTH_DIAG_EXPECT_CC".Split(',') | Where-Object { $_ })
    }

    $shells = @('powershell')
    if ($script:PwshExe) { $shells += 'pwsh' }
    $allStates = @('base', 'stack', 'presence', 'combined')
    $sel = $States
    if ($sel.Count -eq 0) {
        switch ($script:Mode) { 'quick' { $sel = $allStates } 'full' { $sel = @('base', 'combined') } 'elevated' { $sel = @('base', 'stack', 'combined') } }
    }
    if ($sel -notcontains 'base') { $sel = @('base') + $sel }
    foreach ($s in $sel) { if ($allStates -notcontains $s) { throw "unknown state $s (use $($allStates -join ', '))" } }

    Initialize-Tools
    foreach ($s in $sel) { Initialize-State $s }
    $ready = @($sel | Where-Object { $script:StateDirs.ContainsKey($_) })

    if ($script:Mode -eq 'quick' -or $script:Mode -eq 'elevated') {
        foreach ($s in $ready) { foreach ($sh in $shells) { Invoke-DecisionRun $s $sh } }
        foreach ($s in $ready) { foreach ($sh in $shells) { Invoke-ProbeCheck $s $sh; Invoke-SmokeCheck $s $sh } }
        $rep = Invoke-Restore 'after decisions'
        if (-not $rep.ok -or $rep.changes.Count -gt 0) { $script:Results.errors += "the decision runs changed user state: $(@($rep.changes + $rep.failures) -join '; ')" }
        if (-not $SkipTests) {
            $filter = ''
            if ($script:Mode -eq 'elevated') { $filter = '(?i)parity|resolver|concurrency|elevat|child_script|admin|probe' }
            $ts = $TestStates
            if ($ts.Count -eq 0) { $ts = @('base', 'combined') }
            $ts = @($ts | Where-Object { $script:StateDirs.ContainsKey($_) })
            foreach ($s in $ts) { foreach ($sh in $shells) { Invoke-Ps1Tests $s $sh $filter } }
            foreach ($s in $ts) { Invoke-Pytests $s $filter }
            Add-AbsentTestRows
        }
    }

    if ($script:Mode -eq 'full') {
        $problems = @(Test-FullPreflight)
        $running = @(Get-StudioProcesses)
        if ($running.Count -gt 0) {
            Add-DiagError "Unsloth Studio is running ($(@($running | ForEach-Object { "$($_.name) pid $($_.pid)" }) -join ', ')); close it and run again. The full pass was skipped."
        } elseif ($problems.Count -gt 0 -and -not $AllowMachineInstalls) {
            foreach ($p in $problems) { Add-DiagError "full pass skipped: $p. Re-run with -AllowMachineInstalls to let the installer do that." }
        } else {
            Invoke-Park
            foreach ($s in $ready) {
                try { Invoke-FullState $s } catch { Add-DiagError "full pass for $s stopped: $($_.Exception.Message)" }
                finally {
                    if ($script:CurrentChild) { Stop-Tree $script:CurrentChild; $script:CurrentChild = $null }
                    # Between states only the install is undone; the parked folders stay parked until the end.
                    $j = Get-Content -LiteralPath $script:JournalPath -Raw | ConvertFrom-Json
                    $rep = [ordered]@{ ok = $true; changes = @(); conflicts = @(); failures = @() }
                    try { Restore-EnvironmentKey $j $rep } catch { $rep.failures += $_.Exception.Message }
                    Restore-OwnedKey 'Software\Unsloth' $j.unsloth_key $rep
                    Restore-Shortcuts $j $rep
                    $left = Join-Path $env:USERPROFILE '.unsloth'
                    if (Test-Path -LiteralPath $left) { [void](Remove-TreeNoFollow -Path $left -Allowed @($left)) }
                    $leftLauncher = Join-Path $env:LOCALAPPDATA 'Unsloth Studio'
                    if (Test-Path -LiteralPath $leftLauncher) { [void](Remove-TreeNoFollow -Path $leftLauncher -Allowed @($leftLauncher)) }
                    $row = @($script:Results.full | Where-Object { $_.state -eq $s } | Select-Object -Last 1)
                    if ($row.Count -gt 0) { $row[0]['restore_changes'] = @($rep.changes); $row[0]['restore_conflicts'] = @($rep.conflicts + $rep.failures) }
                }
            }
        }
    }
} catch {
    Add-DiagError "stopped: $($_.Exception.Message) at line $($_.InvocationInfo.ScriptLineNumber)"
    $exitCode = 1
} finally {
    if ($script:CurrentChild) { Stop-Tree $script:CurrentChild }
    if ($script:JournalPath -and (Test-Path -LiteralPath $script:JournalPath)) {
        try {
            $rep = Invoke-Restore 'final'
            $script:Results.restore = $rep
            if ($rep.ok) { Complete-Journal }
            else { Write-Diag 'restore could not put everything back; the journal is kept, run -Recover.' 'Yellow' }
        } catch { Add-DiagError "final restore failed: $($_.Exception.Message); run with -Recover" }
        try { Copy-Item -LiteralPath $script:JournalPath -Destination (Join-Path $script:Out 'journal.json') -Force } catch { }
    }
    try {
        Write-JsonFile -Path (Join-Path $script:Out 'results.json') -Object $script:Results
        Write-TextFile -Path (Join-Path $script:Out 'summary.md') -Text (New-SummaryMarkdown)
        $script:Redactions = Get-Redactions
        Protect-OutDir
        $zip = New-OutputZip
        if (-not $NoDesktopCopy) {
            try {
                $desk = [Environment]::GetFolderPath('Desktop')
                if ($desk) { Copy-Item -LiteralPath $zip -Destination $desk -Force; Write-Diag "copied to $desk" }
            } catch { }
        }
        if (-not $KeepWorkDir) {
            foreach ($d in @('r', 'h', 'tools', 'cache', 'tmp', 'src', 'run')) { [void](Remove-TreeNoFollow -Path (Join-Path $script:Work $d)) }
        }
        Write-Host ''
        Write-Host "Result: $zip" -ForegroundColor Green
        Write-Host 'Send that zip back. summary.md inside it has the tables.' -ForegroundColor Green
    } catch {
        Write-Host "packaging failed: $($_.Exception.Message). Raw output is in $script:Out" -ForegroundColor Red
        $exitCode = 1
    }
}
exit $exitCode
