#Requires -Version 5.1
<#
.SYNOPSIS
  Verify the agent pipeline.

.DESCRIPTION
  With no switches, runs the mock smoke test: a scripted model, a real git
  commit, and a real pytest, with no GPU and no llama-server. Use this
  before downloading a 22 GB model.

  -Live runs tasks\smoke-task.md against a server that is already up, inside
  a throwaway git repository under the temp directory.

.EXAMPLE
  .\scripts\smoke-test.ps1

.EXAMPLE
  .\scripts\start-server.ps1 -Profile day -Background
  .\scripts\smoke-test.ps1 -Live -Profile day
#>
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [switch]$Live,
    [string]$Profile = 'day',
    [int]$MaxMinutes = 45,
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

Write-Host 'Mock smoke (no GPU)...'
& $python -m local_agent smoke --mock
if ($LASTEXITCODE -ne 0) {
    throw "Mock smoke failed with exit $LASTEXITCODE."
}
Write-Host 'Mock smoke passed.'

if (-not $Live) {
    Write-Host 'Live smoke skipped. Pass -Live once llama-server is running.'
    exit 0
}

$base = "http://127.0.0.1:$Port"
if (-not (Test-ServerHealthy $base)) {
    throw "Nothing is answering $base/health. Start the server first: .\scripts\start-server.ps1 -Profile $Profile"
}
Write-Host "Live smoke against $base with profile $Profile (up to $MaxMinutes minutes)..."
& $python -m local_agent smoke --live --profile $Profile --base-url $base --max-minutes $MaxMinutes
if ($LASTEXITCODE -ne 0) {
    throw "Live smoke failed with exit $LASTEXITCODE."
}
Write-Host 'Live smoke passed.'
exit 0
