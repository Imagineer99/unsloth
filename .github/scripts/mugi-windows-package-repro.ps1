# SPDX-License-Identifier: AGPL-3.0-only

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $InstallerUrl,
    [Parameter(Mandatory = $true)][string] $ExpectedSha256
)

$ErrorActionPreference = 'Stop'
$logs = Join-Path $env:GITHUB_WORKSPACE 'mugi-package-logs'
$installer = Join-Path $env:RUNNER_TEMP 'Unsloth-Desktop-0_1_702_beta-Windows.exe'
$studioHome = Join-Path $env:USERPROFILE '.unsloth\studio'
$managedVenv = Join-Path $studioHome 'unsloth_studio'
$managedPython = Join-Path $managedVenv 'Scripts\python.exe'
$managedStub = Join-Path $managedVenv 'Scripts\unsloth.exe'
$cmdShim = Join-Path $studioHome 'bin\unsloth.cmd'

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Start-Transcript -Path (Join-Path $logs 'package-repro.log') -Force | Out-Null

function Require-Path([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
}

function Invoke-Installer([string] $Path, [string] $LogName) {
    $started = Get-Date
    $proc = Start-Process -FilePath $Path -ArgumentList '/S' -Wait -PassThru
    "exit=$($proc.ExitCode) elapsed_seconds=$([int]((Get-Date) - $started).TotalSeconds)" |
        Set-Content -LiteralPath (Join-Path $logs $LogName)
    if ($proc.ExitCode -ne 0) { throw "NSIS installer exited $($proc.ExitCode)" }
}

function Get-DesktopInstall {
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($key in Get-ChildItem $root -ErrorAction SilentlyContinue) {
            $item = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
            if ($item.DisplayName -ne 'Unsloth') { continue }
            $dir = ([string]$item.InstallLocation).Trim('"')
            if ($dir -and (Test-Path -LiteralPath $dir -PathType Container)) { return $dir }
        }
    }
    $fallback = Join-Path $env:LOCALAPPDATA 'Unsloth'
    if (Test-Path -LiteralPath $fallback -PathType Container) { return $fallback }
    throw 'Could not resolve the installed Unsloth desktop directory.'
}

function Get-DesktopExe([string] $InstallDir) {
    $candidate = Get-ChildItem -LiteralPath $InstallDir -Filter '*.exe' -File |
        Where-Object { $_.Name -ne 'uninstall.exe' -and $_.VersionInfo.ProductName -eq 'Unsloth' } |
        Select-Object -First 1
    if (-not $candidate) { throw "No signed Unsloth desktop executable found in $InstallDir" }
    return $candidate.FullName
}

function Stop-TestProcesses([string] $DesktopExe) {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ExecutablePath -and (
                $_.ExecutablePath -ieq $DesktopExe -or
                $_.ExecutablePath.StartsWith($managedVenv, [StringComparison]::OrdinalIgnoreCase)
            )
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Seconds 3
}

function Wait-StudioHealth([int] $TimeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        foreach ($port in 8888..8908) {
            foreach ($path in '/api/liveness', '/api/health') {
                try {
                    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port$path" `
                        -TimeoutSec 2
                    if ($response.StatusCode -eq 200) {
                        "port=$port path=$path body=$($response.Content)" |
                            Set-Content -LiteralPath (Join-Path $logs 'desktop-health.log')
                        return $port
                    }
                } catch {}
            }
        }
        Start-Sleep -Seconds 3
    }
    throw 'Desktop backend did not become healthy on ports 8888-8908.'
}

function Invoke-EmbeddedInstall([string] $Script, [string] $LogName) {
    $env:UNSLOTH_SKIP_AUTOSTART = '1'
    $env:UNSLOTH_STUDIO_DISABLE_PUBLIC_CHECK = '1'
    $env:UNSLOTH_VERBOSE = '1'
    Remove-Item Env:\UNSLOTH_STUDIO_HOME -ErrorAction SilentlyContinue
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned `
        -File $Script --no-torch *>&1 | Tee-Object -FilePath (Join-Path $logs $LogName)
    if ($LASTEXITCODE -ne 0) { throw "embedded install.ps1 exited $LASTEXITCODE" }
}

try {
    Write-Host '=== download, identity and endpoint scan ==='
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $installer
    $actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    Write-Host "SHA256: $actualHash"
    if ($actualHash -ne $ExpectedSha256) { throw "SHA256 mismatch: $actualHash" }

    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    $signature | Format-List Status,StatusMessage,SignerCertificate,TimeStamperCertificate |
        Out-File (Join-Path $logs 'authenticode.txt')
    if ($signature.Status -ne 'Valid') { throw "Invalid Authenticode signature: $($signature.Status)" }
    if ($signature.SignerCertificate.Subject -notlike 'CN=Unsloth AI Inc.*') {
        throw "Unexpected signer: $($signature.SignerCertificate.Subject)"
    }

    $defenderScanned = $false
    try {
        $defender = Get-MpComputerStatus
        $defender | Format-List * | Out-File (Join-Path $logs 'defender-status.txt')
        if ($defender.AntivirusEnabled) {
            Start-MpScan -ScanType CustomScan -ScanPath $installer
            $detections = @(Get-MpThreatDetection | Where-Object {
                ($_.Resources | Out-String) -match [regex]::Escape($installer)
            })
            $defenderScanned = $true
        } else {
            Write-Host '::warning::Microsoft Defender is disabled on this hosted runner.'
        }
    } catch {
        Write-Host "::warning::Hosted-runner Defender probe unavailable: $($_.Exception.Message)"
    }
    if ($defenderScanned) {
        if ($detections) { throw 'Microsoft Defender recorded a detection for the installer.' }
        Write-Host 'PASS: Microsoft Defender scanned the package with no detection.'
    }

    Write-Host '=== install packaged desktop twice ==='
    Invoke-Installer $installer 'nsis-first-install.txt'
    $installDir = Get-DesktopInstall
    $desktopExe = Get-DesktopExe $installDir
    Require-Path $desktopExe
    Write-Host "desktop: $desktopExe"
    $desktopSignature = Get-AuthenticodeSignature -LiteralPath $desktopExe
    if ($desktopSignature.Status -ne 'Valid') { throw 'Installed desktop signature is not valid.' }
    Invoke-Installer $installer 'nsis-second-install.txt'
    Require-Path $desktopExe
    Write-Host 'PASS: two silent package installs exited 0.'

    Write-Host '=== run the package embedded installer ==='
    $embedded = Get-ChildItem -LiteralPath $installDir -Filter 'install.ps1' -File -Recurse |
        Select-Object -First 1
    if (-not $embedded) { throw 'The installed package contains no install.ps1 resource.' }
    Copy-Item -LiteralPath $embedded.FullName -Destination (Join-Path $logs 'embedded-install.ps1')
    Invoke-EmbeddedInstall $embedded.FullName 'embedded-first-install.log'
    Require-Path $managedPython
    Require-Path $managedStub
    Require-Path $cmdShim
    & $cmdShim --version
    if ($LASTEXITCODE -ne 0) { throw 'Policy-safe CLI shim failed after packaged install.' }

    Write-Host '=== launch packaged desktop from System32 ==='
    Stop-TestProcesses $desktopExe
    $desktop = Start-Process -FilePath $desktopExe -WorkingDirectory "$env:WINDIR\System32" -PassThru
    Start-Sleep -Seconds 8
    if ($desktop.HasExited) { throw "Desktop exited early with $($desktop.ExitCode)" }
    $firstPort = Wait-StudioHealth
    Write-Host "PASS: desktop and backend are healthy from System32 on port $firstPort."

    $studioLogs = Join-Path $studioHome 'logs'
    if (Test-Path -LiteralPath $studioLogs) {
        Copy-Item -LiteralPath $studioLogs -Destination (Join-Path $logs 'studio-logs-first') -Recurse
        $blocked = Get-ChildItem -LiteralPath $studioLogs -File -Recurse -ErrorAction SilentlyContinue |
            Select-String -SimpleMatch 'Unsloth cannot run from C:\Windows\System32' -ErrorAction SilentlyContinue
        if ($blocked) { throw 'Studio logs contain the System32 refusal.' }
    }

    Write-Host '=== quarantine generated launcher and relaunch desktop ==='
    Stop-TestProcesses $desktopExe
    $quarantined = "$managedStub.quarantined"
    Move-Item -LiteralPath $managedStub -Destination $quarantined
    try {
        & $cmdShim --version
        if ($LASTEXITCODE -ne 0) { throw 'unsloth.cmd failed with unsloth.exe quarantined.' }
        & $cmdShim studio desktop-capabilities --json
        if ($LASTEXITCODE -ne 0) { throw 'Desktop capability probe failed with unsloth.exe quarantined.' }
        # This command can return credentials. Exercise it, but never write its
        # stdout/stderr to the public Actions log or evidence artifact.
        & $cmdShim studio provision-desktop-auth *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Desktop auth provisioning failed with unsloth.exe quarantined.' }
        $desktop = Start-Process -FilePath $desktopExe -WorkingDirectory "$env:WINDIR\System32" -PassThru
        Start-Sleep -Seconds 8
        if ($desktop.HasExited) { throw "Desktop exited with launcher quarantined: $($desktop.ExitCode)" }
        $secondPort = Wait-StudioHealth
        Write-Host "PASS: desktop backend is healthy with unsloth.exe quarantined on port $secondPort."
    } finally {
        Stop-TestProcesses $desktopExe
        if (Test-Path -LiteralPath $quarantined) {
            Move-Item -LiteralPath $quarantined -Destination $managedStub
        }
    }

    Write-Host '=== reinstall embedded environment and verify persistence ==='
    $tracked = @($cmdShim, (Join-Path $studioHome 'share\launch-studio.ps1')) |
        Where-Object { Test-Path -LiteralPath $_ } |
        ForEach-Object {
            [pscustomobject]@{
                Path = $_
                Hash = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash
                Ticks = (Get-Item -LiteralPath $_).LastWriteTimeUtc.Ticks
            }
        }
    Invoke-EmbeddedInstall $embedded.FullName 'embedded-second-install.log'
    foreach ($before in $tracked) {
        if ((Get-FileHash -LiteralPath $before.Path -Algorithm SHA256).Hash -ne $before.Hash) {
            throw "Reinstall changed $($before.Path)"
        }
        if ((Get-Item -LiteralPath $before.Path).LastWriteTimeUtc.Ticks -ne $before.Ticks) {
            throw "Reinstall rewrote unchanged file $($before.Path)"
        }
    }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    "user_path=$userPath" | Set-Content -LiteralPath (Join-Path $logs 'user-path.txt')

    $savedProcessPath = $env:Path
    try {
        $env:Path = @(
            [Environment]::GetEnvironmentVariable('Path', 'Machine'),
            [Environment]::GetEnvironmentVariable('Path', 'User')
        ) -join ';'
        & cmd.exe /d /c "uv --version && uvx --version && unsloth.cmd --version" |
            Tee-Object -FilePath (Join-Path $logs 'fresh-terminal.log')
        if ($LASTEXITCODE -ne 0) { throw 'A fresh registry-PATH terminal could not run uv, uvx and unsloth.cmd.' }

        $transactionDirs = @(
            (Join-Path $env:USERPROFILE '.local\bin'),
            (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
            (Split-Path -Parent (Get-Command uv.exe -CommandType Application -ErrorAction Stop).Source),
            (Split-Path -Parent (Get-Command uvx.exe -CommandType Application -ErrorAction Stop).Source),
            (Split-Path -Parent $cmdShim)
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } |
            Select-Object -Unique
        $leftovers = @($transactionDirs | ForEach-Object {
            Get-ChildItem -LiteralPath $_ -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '\.(rollback|tmp)\.' }
        })
        if ($leftovers) { throw "Transactional files remain: $($leftovers.FullName -join ', ')" }
    } finally {
        $env:Path = $savedProcessPath
    }

    Stop-TestProcesses $desktopExe
    $desktop = Start-Process -FilePath $desktopExe -PassThru
    Start-Sleep -Seconds 8
    if ($desktop.HasExited) { throw "Desktop exited after reinstall: $($desktop.ExitCode)" }
    $finalPort = Wait-StudioHealth
    Write-Host "PASS: Studio starts after reinstall on port $finalPort."

    Write-Host 'MUGI WINDOWS PACKAGE MATRIX PASSED'
} finally {
    try { Stop-TestProcesses $desktopExe } catch {}
    Stop-Transcript | Out-Null
}
