#Requires -Version 5.1
<#
.SYNOPSIS
  Ask a running task queue to stop after the current task.

.DESCRIPTION
  Writes runtime\STOP_QUEUE (LOCAL_AGENT_RUNTIME\STOP_QUEUE when that
  variable is set). scripts\queue.ps1 checks the file only between tasks.
  The task already running finishes, the queue writes queue-summary.md,
  deletes the file, and exits 0.

  Create the request after the queue has started. A file that is already
  present when a queue starts is stale: that queue warns, deletes it, and
  runs. -Cancel withdraws the request so the queue keeps going.

  Ctrl+C in the queue window is not this. It interrupts python.exe in the
  middle of the task.

.EXAMPLE
  .\scripts\stop-queue.ps1

.EXAMPLE
  .\scripts\stop-queue.ps1 -Cancel

.PARAMETER Cancel
  Delete the stop request. The queue starts the next task as usual.
#>
[CmdletBinding()]
param(
    [switch]$Cancel,
    [string]$InstallDir = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

$agentArgs = @('-m', 'local_agent', 'stop')
if ($Cancel) {
    $agentArgs += '--cancel'
}
& $python @agentArgs
exit $LASTEXITCODE
