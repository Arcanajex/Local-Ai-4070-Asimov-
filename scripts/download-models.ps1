#Requires -Version 5.1
<#
.SYNOPSIS
  Download GGUF files for one or more profiles. Safe to re-run; partial files resume.

.EXAMPLE
  .\scripts\download-models.ps1 -Models quality,day

.EXAMPLE
  .\scripts\download-models.ps1 -Models max
#>
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string[]]$Models = @('quality', 'day')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir
$modelsDir = Join-Path $InstallDir 'models'
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

foreach ($name in $Models) {
    $record = Get-ProfileRecord -Python $python -Name $name
    $dest = Join-Path $modelsDir $record.filename
    $sizeGb = [math]::Round(([int64]$record.size_bytes) / 1GB, 2)
    Write-Host "Downloading $name ($($record.filename), $sizeGb GiB)..."
    Write-Host "  $($record.url)"
    & $python -m local_agent download --url $record.url --dest $dest --expected-size ([int64]$record.size_bytes)
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed for $name. Re-run this script; the partial file is resumed."
    }
    Write-Host "Ready: $dest"
}
