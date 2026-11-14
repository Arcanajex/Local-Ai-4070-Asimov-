#Requires -Version 5.1
<#
.SYNOPSIS
  Run an unattended coding task: plan, implement, test, fix, review, commit.

.DESCRIPTION
  Holds the machine awake while the Python runner works. Optionally switches
  the Windows power scheme to High Performance and restores the previous
  scheme afterwards. Starts llama-server in another window if it is not
  already healthy. The model does not commit; the runner does, on a new
  branch in the target project.

  The target project must be a clean git repository. Pass -InitRepo when the
  folder is not a repository yet: that runs git init and an empty initial
  commit. Results land in AGENT_PLAN.md, AGENT_REPORT.md, and a git commit.
  Logs go to runtime\logs\overnight-*.log plus the JSONL written by the runner.

.EXAMPLE
  .\scripts\overnight.ps1 -Task .\tasks\smoke-task.md -Project C:\src\my-app -Profile quality

.EXAMPLE
  .\scripts\overnight.ps1 -Task .\TASK.md -Project C:\src\new-app -InitRepo

.PARAMETER InitRepo
  Create the project folder if needed, then git init and an empty initial
  commit, when the folder is not already a repository.

.PARAMETER MaxHours
  Wall-clock budget for the whole run. Default 8.

.PARAMETER MaxIterations
  How many times the runner may execute the test command and hand a failure
  back to the model. Default 6.

.PARAMETER ArchiveDir
  When the run passes, move the task file here. Default is tasks\archive
  in this repo. The path under the tasks root is kept.

.PARAMETER TasksRoot
  Folder the archive path is relative to.

.PARAMETER NoArchive
  Leave the task file where it is, including when the run passes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Task,
    [Parameter(Mandatory = $true)][string]$Project,
    [string]$Profile = 'quality',
    [string]$InstallDir = '',
    [string]$ArchiveDir = '',
    [string]$TasksRoot = '',
    [double]$MaxHours = 8,
    [int]$MaxIterations = 6,
    [int]$Port = 8080,
    [switch]$NoStayAwake,
    [switch]$SkipPerformancePlan,
    [switch]$NoStartServer,
    [switch]$InitRepo,
    [switch]$NoStrict,
    [switch]$NoArchive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

Assert-NativeWindows
if ($NoArchive -and $ArchiveDir) {
    throw '-NoArchive and -ArchiveDir cannot be used together.'
}
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

$taskPath = (Resolve-Path -LiteralPath $Task).Path
if (-not (Test-Path -LiteralPath $Project)) {
    if (-not $InitRepo) {
        throw "Project folder does not exist: $Project. Create it, or pass -InitRepo."
    }
    New-Item -ItemType Directory -Force -Path $Project | Out-Null
}
$projectPath = (Resolve-Path -LiteralPath $Project).Path

$logDir = Join-Path $InstallDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('overnight-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

$previousScheme = $null
if (-not $SkipPerformancePlan) {
    # Standard Windows "High performance" GUID. Legion Vantage's own
    # performance mode is separate and has to be set in that app; this only
    # stops Windows from parking the CPU in Balanced.
    $active = powercfg /getactivescheme | Out-String
    $match = [regex]::Match($active, '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
    if ($match.Success) {
        $previousScheme = $match.Value
    }
    $high = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
    & powercfg /setactive $high
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Could not switch to the High performance power scheme. Continuing with the current scheme.'
        $previousScheme = $null
    } else {
        Write-Host "Power scheme set to High performance (was $previousScheme)."
    }
}

if (-not $NoStayAwake) {
    Enable-StayAwake
    Write-Host 'Sleep is suspended until this script exits. The screen may still turn off.'
}

$code = 1
try {
    Start-Transcript -LiteralPath $log -Force | Out-Null
    Write-Host "Task:    $taskPath"
    Write-Host "Project: $projectPath"
    Write-Host "Profile: $Profile"
    Write-Host "Budget:  $MaxHours hours, $MaxIterations test iterations"
    Write-Host "Log:     $log"

    # Default port comes from the profile, including runtime\overrides.json.
    # -Port forces both the health check and the server onto that port.
    if ($PSBoundParameters.ContainsKey('Port')) {
        $base = "http://127.0.0.1:$Port"
    } else {
        if ($Profile -notmatch '^[A-Za-z0-9_-]+$') { throw "Invalid profile name: $Profile" }
        $py = "from local_agent.config import get_profile; p = get_profile('" + $Profile + "'); print('http://%s:%s' % (p.host, p.port))"
        $base = ((& $python -c $py) | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $base) { throw "Could not read the listen address for profile $Profile." }
    }
    if (-not $NoStartServer -and -not (Test-ServerHealthy $base)) {
        Write-Host 'llama-server is not up. Starting it in a new window...'
        if ($PSBoundParameters.ContainsKey('Port')) {
            & (Join-Path $PSScriptRoot 'start-server.ps1') -Profile $Profile -InstallDir $InstallDir -Port $Port -Background
        } else {
            & (Join-Path $PSScriptRoot 'start-server.ps1') -Profile $Profile -InstallDir $InstallDir -Background
        }
        $deadline = (Get-Date).AddMinutes(20)
        while ((Get-Date) -lt $deadline) {
            if (Test-ServerHealthy $base) { break }
            Start-Sleep -Seconds 5
        }
        if (-not (Test-ServerHealthy $base)) {
            throw "llama-server did not answer $base/health within 20 minutes. Read the server window. A 22 GB model can take several minutes; an OOM fails faster than that."
        }
    }

    $agentArgs = @(
        '-m', 'local_agent', 'overnight',
        '--task', $taskPath,
        '--project', $projectPath,
        '--profile', $Profile,
        '--base-url', $base,
        '--max-hours', "$MaxHours",
        '--max-iterations', "$MaxIterations",
        '--log-dir', (Join-Path $logDir ('run-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
    )
    if ($InitRepo) {
        $agentArgs += '--init-repo'
    }
    if ($NoStrict) {
        $agentArgs += '--no-strict'
    }
    if ($NoArchive) {
        $agentArgs += '--no-archive'
    }
    if ($ArchiveDir) {
        $agentArgs += @('--archive-dir', $ArchiveDir)
    }
    if ($TasksRoot) {
        $agentArgs += @('--tasks-root', $TasksRoot)
    }
    & $python @agentArgs
    $code = $LASTEXITCODE
} finally {
    try { Stop-Transcript | Out-Null } catch { }
    if (-not $NoStayAwake) {
        Disable-StayAwake
    }
    if ($previousScheme) {
        & powercfg /setactive $previousScheme
        Write-Host "Restored power scheme $previousScheme."
    }
}

if ($code -ne 0) {
    Write-Host "Overnight run finished without passing tests (exit $code). The branch, AGENT_REPORT.md, and $log still have the work."
    exit $code
}
if ($NoArchive) {
    Write-Host 'Overnight run passed. See .agent\AGENT_REPORT.md in the target project. The task file was left in place.'
} else {
    Write-Host 'Overnight run passed. See .agent\AGENT_REPORT.md in the target project. When the review accepts the change, the task file is moved under tasks\archive.'
}
exit 0
