#Requires -Version 5.1
<#
.SYNOPSIS
  Sweep --n-cpu-moe (or --n-cpu-ffn for the dense profile) and keep the fastest value that fits.

.DESCRIPTION
  Loads the model once per candidate in profiles\models.json, from the safest
  offload (more weights in system RAM) toward more of the model on the GPU.
  Stops at the first OOM or at the first run whose nvidia-smi memory.used
  exceeds dedicated VRAM minus -HeadroomMib (default 1024). Windows will
  happily allocate past the 8 GB and then crawl, so a clean process exit is
  not treated as success.

  The winner is written to runtime\overrides.json. The committed defaults
  are not modified. Stop any llama-server already using the profile port
  before you run this. Each candidate reloads the GGUF, so a quality sweep
  takes a while.

.EXAMPLE
  .\scripts\tune-n-cpu-moe.ps1 -Profile quality

.EXAMPLE
  .\scripts\tune-n-cpu-moe.ps1 -Profile day -HeadroomMib 1536
#>
[CmdletBinding()]
param(
    [string]$Profile = 'quality',
    [string]$InstallDir = '',
    [int]$HeadroomMib = 1024,
    [int]$LoadTimeout = 300
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

Write-Host "Tuning $Profile. Headroom ${HeadroomMib} MiB. This reloads the model for each candidate."
Write-Host 'Plug the laptop in. Set Legion performance mode. Close games and browsers.'
& $python -m local_agent tune --profile $Profile --headroom-mib $HeadroomMib --load-timeout $LoadTimeout
if ($LASTEXITCODE -ne 0) {
    throw "Tuning failed for $Profile. If every candidate OOMs, lower the profile context in runtime\overrides.json and run this again."
}
Write-Host 'Wrote runtime\overrides.json. The next start-server.ps1 uses it.'
