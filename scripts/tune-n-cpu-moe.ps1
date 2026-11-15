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

.EXAMPLE
  .\scripts\tune-n-cpu-moe.ps1 -Profile quality -Check

.PARAMETER Check
  Load the current offload once and print whether VRAM stays under the
  headroom. Does not write runtime\overrides.json. Use this after a context
  change, before a long run, to confirm memory.used is about 1 GB under the
  dedicated total.
#>
[CmdletBinding()]
param(
    [string]$Profile = 'quality',
    [string]$InstallDir = '',
    [int]$HeadroomMib = 1024,
    [int]$LoadTimeout = 300,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

$tuneArgs = @('-m', 'local_agent', 'tune', '--profile', $Profile, '--headroom-mib', "$HeadroomMib", '--load-timeout', "$LoadTimeout")
if ($Check) {
    Write-Host "Fit check for $Profile at the current offload. Headroom ${HeadroomMib} MiB. Overrides are not changed."
    $tuneArgs += '--check'
} else {
    Write-Host "Tuning $Profile. Headroom ${HeadroomMib} MiB. This reloads the model for each candidate."
}
Write-Host 'Plug the laptop in. Set Legion performance mode. Close games and browsers.'
& $python @tuneArgs
if ($LASTEXITCODE -ne 0) {
    if ($Check) {
        throw "Fit check failed for $Profile. VRAM is over the cap, or the server did not stay up. Raise n_cpu_moe or lower context in runtime\overrides.json, then run the full tune."
    }
    throw "Tuning failed for $Profile. If every candidate OOMs, lower the profile context in runtime\overrides.json and run this again."
}
if ($Check) {
    Write-Host 'Fit check passed. nvidia-smi memory.used during the check should be about 1 GB under the dedicated total.'
} else {
    Write-Host 'Wrote runtime\overrides.json. The next start-server.ps1 uses it.'
}
