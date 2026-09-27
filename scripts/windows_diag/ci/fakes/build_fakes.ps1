# Builds the stand-in NVIDIA binaries for the Windows GPU decision matrix.
# Windows PowerShell 5.1 compatible. cl.exe is found through vswhere + vcvarsall, native arch.
#
#   <OutDir>\smi\nvidia-smi.exe    every image (x64 or arm64, whichever the host is)
#   <OutDir>\lib\fake_nvml.dll     x64 images only
#   <OutDir>\lib\fake_nvcuda.dll   x64 images only
#   <OutDir>\hip\hipinfo.cmd       every image
#   <OutDir>\build.json            what was built, for the job log and the artifact
#
# ARM64 images get no DLLs: System32 there would need ARM64X images to serve both the native
# powershell.exe and an x64 venv Python, so the lib* scenarios are recorded as NOT COVERED.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$SourceDir = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'

function Get-NativeArch {
    $signals = @()
    try { $signals += [string][Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE', 'Machine') } catch { }
    $signals += [string]$env:PROCESSOR_ARCHITEW6432
    $signals += [string]$env:PROCESSOR_ARCHITECTURE
    foreach ($s in $signals) { if ($s -and $s.ToLowerInvariant() -eq 'arm64') { return 'arm64' } }
    return 'x64'
}

$arch = Get-NativeArch
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
foreach ($sub in @('smi', 'lib', 'hip', 'obj')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutDir $sub) | Out-Null
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw "vswhere.exe not found at $vswhere" }
$component = if ($arch -eq 'arm64') { 'Microsoft.VisualStudio.Component.VC.Tools.ARM64' } else { 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' }
$vs = & $vswhere -latest -products * -requires $component -property installationPath
if (-not $vs) { $vs = & $vswhere -latest -products * -property installationPath }
if (-not $vs) { throw "no Visual Studio installation with $component" }
$vs = "$vs".Trim()
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "vcvarsall.bat not found at $vcvars" }
Write-Host "arch=$arch vs=$vs"

$smiExe = Join-Path $OutDir 'smi\nvidia-smi.exe'
$nvmlDll = Join-Path $OutDir 'lib\fake_nvml.dll'
$cudaDll = Join-Path $OutDir 'lib\fake_nvcuda.dll'
$obj = Join-Path $OutDir 'obj'

$lines = @(
    '@echo off',
    "call `"$vcvars`" $arch >nul",
    'if errorlevel 1 exit /b 10',
    "cd /d `"$obj`"",
    "cl /nologo /O2 /W3 /Fo`"$obj\\`" `"$SourceDir\fake_nvidia_smi.c`" /Fe`"$smiExe`" /link kernel32.lib",
    'if errorlevel 1 exit /b 11'
)
if ($arch -eq 'x64') {
    $lines += @(
        "cl /nologo /O2 /W3 /LD /Fo`"$obj\\`" `"$SourceDir\fake_nvml.c`" /Fe`"$nvmlDll`" /link kernel32.lib",
        'if errorlevel 1 exit /b 12',
        "cl /nologo /O2 /W3 /LD /Fo`"$obj\\`" `"$SourceDir\fake_nvcuda.c`" /Fe`"$cudaDll`" /link kernel32.lib",
        'if errorlevel 1 exit /b 13',
        "dumpbin /nologo /exports `"$nvmlDll`" > `"$OutDir\exports_nvml.txt`"",
        "dumpbin /nologo /exports `"$cudaDll`" > `"$OutDir\exports_nvcuda.txt`""
    )
}
$lines += 'exit /b 0'
$bat = Join-Path $OutDir 'build.cmd'
[System.IO.File]::WriteAllText($bat, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.ASCIIEncoding))
& cmd.exe /d /c "`"$bat`""
if ($LASTEXITCODE -ne 0) { throw "build.cmd failed with exit $LASTEXITCODE" }

Copy-Item -LiteralPath (Join-Path $SourceDir 'fake_hipinfo.cmd') -Destination (Join-Path $OutDir 'hip\hipinfo.cmd') -Force

# Positive controls: a fake that does not answer the contract makes every cell VOID, so stop here.
$env:FAKE_CUDA = '12.8'; $env:FAKE_CC = '8.9'
$list = (& $smiExe -L | Out-String)
if ($LASTEXITCODE -ne 0 -or $list -notmatch '(?m)^GPU 0: NVIDIA GeForce RTX 4090 \(UUID: GPU-11111111-2222-3333-4444-555555555555\)') {
    throw "fake nvidia-smi -L contract broken: $list"
}
$banner = (& $smiExe | Out-String)
if ($banner -notmatch 'CUDA(?: UMD)? Version:\s+12\.8') { throw "fake nvidia-smi banner contract broken: $banner" }
$csv = (& $smiExe '--query-gpu=name,compute_cap,driver_version' '--format=csv,noheader' | Out-String).Trim()
if ($csv -ne 'NVIDIA GeForce RTX 4090, 8.9, 572.83') { throw "fake nvidia-smi query contract broken: '$csv'" }
# Continue while stderr is swallowed: Windows PowerShell 5.1 turns native stderr into a
# terminating error under 'Stop'.
$ErrorActionPreference = 'Continue'
& $smiExe '--query-gpu=bogus' '--format=csv' 2>&1 | Out-Null
$unknownExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($unknownExit -ne 2) { throw "fake nvidia-smi should exit 2 on an unknown field, got $unknownExit" }
Remove-Item Env:FAKE_CUDA, Env:FAKE_CC -ErrorAction SilentlyContinue

$required = @{
    nvml   = @('nvmlInit_v2', 'nvmlShutdown', 'nvmlSystemGetCudaDriverVersion_v2', 'nvmlDeviceGetCount_v2',
               'nvmlDeviceGetHandleByIndex_v2', 'nvmlDeviceGetCudaComputeCapability')
    nvcuda = @('cuInit', 'cuDriverGetVersion', 'cuDeviceGetCount', 'cuDeviceGet', 'cuDeviceGetAttribute')
}
if ($arch -eq 'x64') {
    foreach ($lib in @('nvml', 'nvcuda')) {
        $exports = Get-Content -LiteralPath (Join-Path $OutDir "exports_$lib.txt") -Raw
        foreach ($name in $required[$lib]) {
            # dumpbin rows end with the undecorated name; x64 has no stdcall decoration.
            if ($exports -notmatch "(?m)\s$([regex]::Escape($name))\r?$") { throw "fake_$lib.dll does not export $name" }
        }
    }
}

$info = [ordered]@{
    arch       = $arch
    vs         = $vs
    smi        = $smiExe
    nvml       = $(if ($arch -eq 'x64') { $nvmlDll } else { $null })
    nvcuda     = $(if ($arch -eq 'x64') { $cudaDll } else { $null })
    hipinfo    = (Join-Path $OutDir 'hip\hipinfo.cmd')
    dlls_built = ($arch -eq 'x64')
}
[System.IO.File]::WriteAllText((Join-Path $OutDir 'build.json'), ($info | ConvertTo-Json -Depth 3), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ($info | ConvertTo-Json -Depth 3)
