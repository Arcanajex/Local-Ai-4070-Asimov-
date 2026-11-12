#Requires -Version 5.1
<#
.SYNOPSIS
  Prepare a native Windows 11 machine for the local coding agent.

.DESCRIPTION
  Checks that this is not WSL, that an NVIDIA driver is visible, that Python
  3.11+ exists, and that the target drive has room for the GGUF files.
  Creates runtime\venv, downloads the latest llama.cpp Windows CUDA build
  (the tools zip and the matching cudart zip), and downloads the model
  profiles you name. Downloads resume if you run the script again.

.PARAMETER Models
  Profile names from profiles\models.json. The default is the two models
  you actually want on this laptop: quality and day. Add max only if you
  want the slow dense experiment.

.PARAMETER Cuda
  12.4 (default, widest driver support on an RTX 4070 Laptop), 13.4, or latest.

.EXAMPLE
  .\scripts\setup.ps1

.EXAMPLE
  .\scripts\setup.ps1 -Models day

.EXAMPLE
  .\scripts\setup.ps1 -SkipModels
#>
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string[]]$Models = @('quality', 'day'),
    [string]$Cuda = '12.4',
    [switch]$SkipModels,
    [switch]$SkipGpuCheck,
    [switch]$SkipVenv
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$repo = Get-RepoRoot
$InstallDir = Get-InstallDir $InstallDir
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'models') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'logs') | Out-Null

Write-Host "Repo:    $repo"
Write-Host "Runtime: $InstallDir"

if (-not $SkipGpuCheck) {
    Write-Host 'Checking the NVIDIA driver...'
    Test-NvidiaGpu
} else {
    Write-Warning 'Skipping the NVIDIA driver check.'
}

$battery = Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue
if ($battery) {
    $onBattery = @($battery | Where-Object { $_.BatteryStatus -ne 2 })
    if ($onBattery.Count -gt 0) {
        Write-Warning 'The laptop does not look like it is plugged in. Plug in before downloading or running models. Legion firmware cuts CPU and GPU power on battery.'
    }
}

if (-not $SkipVenv) {
    $system = Find-SystemPython
    $venv = Join-Path $InstallDir 'venv'
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
        Write-Host 'Creating the virtualenv...'
        & $system.Exe @($system.Args) -m venv $venv
        if ($LASTEXITCODE -ne 0) {
            throw 'python -m venv failed.'
        }
    }
    $python = Join-Path $venv 'Scripts\python.exe'
    Write-Host 'Installing the agent package...'
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
    $spec = '{0}[dev]' -f $repo
    & $python -m pip install -e $spec
    if ($LASTEXITCODE -ne 0) { throw 'pip install -e failed.' }
} else {
    $python = Get-VenvPython $InstallDir
}

# llama.cpp zips are about 0.7 GB together. Model sizes come from the catalog,
# which was checked against Hugging Face. Five extra GB covers extraction.
$need = [int64]5GB
if (-not $SkipModels) {
    foreach ($name in $Models) {
        $record = Get-ProfileRecord -Python $python -Name $name
        $need += [int64]$record.size_bytes
    }
}
$free = Get-FreeBytes $InstallDir
$needGb = [math]::Round($need / 1GB, 1)
$freeGb = [math]::Round($free / 1GB, 1)
Write-Host "Disk: about $needGb GB required, $freeGb GB free."
if ($free -lt $need) {
    throw "Not enough free disk space on $InstallDir. Free $freeGb GB, need about $needGb GB. Point -InstallDir at a larger drive."
}

Write-Host "Resolving the latest llama.cpp Windows CUDA $Cuda release..."
$planRaw = & $python -m local_agent releases --cuda $Cuda
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the llama.cpp release. Check your network and try again.' }
$plan = ($planRaw | Out-String).Trim() | ConvertFrom-Json
$llamaDir = Join-Path $InstallDir 'llama.cpp'
New-Item -ItemType Directory -Force -Path $llamaDir | Out-Null
$zipDir = Join-Path $InstallDir 'downloads'
New-Item -ItemType Directory -Force -Path $zipDir | Out-Null

foreach ($asset in @($plan.llama_zip, $plan.cudart_zip)) {
    $dest = Join-Path $zipDir $asset.name
    Write-Host "Downloading $($asset.name)..."
    & $python -m local_agent download --url $asset.url --dest $dest --expected-size $asset.size
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $($asset.name). Re-run setup; the download resumes." }
    Write-Host "Extracting $($asset.name)..."
    Expand-ZipFlat -Zip $dest -Destination $llamaDir
}

$exe = Join-Path $llamaDir 'llama-server.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    $found = Get-ChildItem -LiteralPath $llamaDir -Filter 'llama-server.exe' -Recurse | Select-Object -First 1
    if (-not $found) {
        throw "llama-server.exe was not in the CUDA zip. The release layout may have changed. Look in $llamaDir."
    }
    $exe = $found.FullName
    $llamaDir = $found.DirectoryName
}
Save-InstallManifest -InstallDir $InstallDir -LlamaDir $llamaDir -Tag $plan.tag -Cuda $plan.cuda_version
Write-Host "llama-server: $exe (tag $($plan.tag), CUDA $($plan.cuda_version))"

if (-not $SkipModels) {
    & (Join-Path $PSScriptRoot 'download-models.ps1') -InstallDir $InstallDir -Models $Models
}

Write-Host ''
Write-Host 'Setup finished.'
Write-Host 'Next, with the laptop plugged in and Legion performance mode on:'
Write-Host "  .\scripts\tune-n-cpu-moe.ps1 -Profile quality"
Write-Host "  .\scripts\start-server.ps1 -Profile quality"
Write-Host 'The mock smoke test does not need a GPU:'
Write-Host '  .\scripts\smoke-test.ps1'
