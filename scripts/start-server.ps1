#Requires -Version 5.1
<#
.SYNOPSIS
  Start llama-server with a profile from profiles\models.json.

.DESCRIPTION
  The argument list is built by `python -m local_agent server-args` so the
  flags in the README, the tuner, and this launcher cannot drift apart.
  The process stays in the foreground unless -Background is set. Listen
  address is 127.0.0.1. Overrides in runtime\overrides.json are applied.

.EXAMPLE
  .\scripts\start-server.ps1 -Profile quality

.EXAMPLE
  .\scripts\start-server.ps1 -Profile day -Background
#>
[CmdletBinding()]
param(
    [string]$Profile = 'quality',
    [string]$InstallDir = '',
    [int]$Port = 0,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows

if ($Background) {
    $argList = @(
        '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
        '-Profile', $Profile
    )
    if ($InstallDir) {
        $argList += @('-InstallDir', $InstallDir)
    }
    if ($Port -gt 0) {
        $argList += @('-Port', "$Port")
    }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList | Out-Null
    Write-Host "Started llama-server ($Profile) in a new window."
    return
}

$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir
$exe = Get-LlamaServerExe $InstallDir

$argList = @('-m', 'local_agent', 'server-args', '--profile', $Profile, '--format', 'json')
if ($Port -gt 0) {
    $argList += @('--port', "$Port")
}
$raw = & $python @argList
if ($LASTEXITCODE -ne 0) {
    throw "Could not build llama-server arguments for profile $Profile."
}
$serverArgs = @((($raw | Out-String).Trim() | ConvertFrom-Json))

$logDir = Join-Path $InstallDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$argvPath = Join-Path $logDir "server-$Profile-$stamp.argv.txt"
@($exe) + $serverArgs | Set-Content -LiteralPath $argvPath -Encoding UTF8
Write-Host "Command saved to $argvPath"
Write-Host "Starting $exe"
Write-Host "Profile: $Profile"
Write-Host 'The API is http://127.0.0.1:<port>/v1  (health: /health). Ctrl+C stops the server.'

$binDir = Split-Path -Parent $exe
$code = 0
Push-Location $binDir
try {
    # The CUDA DLLs sit next to the exe. Windows searches that directory
    # when the executable path is absolute, which it is.
    & $exe @serverArgs
    if ($null -ne $LASTEXITCODE) {
        $code = $LASTEXITCODE
    }
} finally {
    Pop-Location
}
if ($code -ne 0) {
    throw "llama-server exited with code $code. See the console output above. Common causes are in the README under Troubleshooting."
}
