#Requires -Version 5.1
<#
.SYNOPSIS
  Run every task markdown file in a folder, one at a time.

.DESCRIPTION
  Keeps the machine awake and the high-performance power scheme for the
  whole queue, and starts llama-server once. Tasks run in filename order
  (01 before 10). A passing task is merged back into the branch the project
  was on, then the next file starts. A failure stops the queue unless
  -ContinueOnFailure is set.

  Progress is queue-state.json in the task folder. A passing file is moved
  to tasks\archive in this repo, keeping the folder it lived in
  (tasks\ui\step-2.md becomes tasks\archive\ui\step-2.md). A failing file
  stays where it is. -NoArchive uses the older done\ and failed\ folders.
  Run the same command again to resume.

  Ctrl+C in this window is delivered to the Python process and aborts the
  task that is running. It is not a graceful stop. From another window run
  scripts\stop-queue.ps1. The queue finishes the current task, starts
  nothing else, and exits 0. A second Ctrl+C is not special-cased: the
  console interrupt is not reliable on both Windows PowerShell 5.1 and
  PowerShell 7 when a child python.exe owns the console.

  When the queue ends, a toast and a beep fire. If Windows refuses the
  toast, the queue result is unchanged.

.EXAMPLE
  .\scripts\queue.ps1 -TaskDir C:\src\my-app\tasks -Project C:\src\my-app -Profile quality

.EXAMPLE
  .\scripts\queue.ps1 -TaskDir C:\src\my-app\tasks -Project C:\src\new-app -InitRepo -MaxHoursPerTask 6

.PARAMETER ContinueOnFailure
  Keep going after a task fails. The failed branch is not merged, so the
  next task starts from the last passing merge.

.PARAMETER MaxHoursPerTask
  Wall-clock budget for each task. Default 8.

.PARAMETER ArchiveDir
  Folder passing task files are moved into. Default is tasks\archive in
  this repo. The path under the tasks root is kept.

.PARAMETER TasksRoot
  Folder archive paths are relative to. Default is the tasks directory
  that contains the queue (tasks\ui\step-2.md -> tasks\archive\ui\step-2.md).

.PARAMETER NoArchive
  Leave passing files in done\ and failing files in failed\, and do not
  write to the archive.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TaskDir,
    [Parameter(Mandatory = $true)][string]$Project,
    [string]$Profile = 'quality',
    [string]$InstallDir = '',
    [string]$ArchiveDir = '',
    [string]$TasksRoot = '',
    [double]$MaxHoursPerTask = 8,
    [int]$MaxIterations = 6,
    [int]$Port = 8080,
    [switch]$InitRepo,
    [switch]$ContinueOnFailure,
    [switch]$NoStrict,
    [switch]$AllowMain,
    [switch]$NoArchive,
    [switch]$NoStayAwake,
    [switch]$SkipPerformancePlan,
    [switch]$NoStartServer
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib.ps1')

function ConvertTo-QueueResult {
    param($Output)
    if (-not $Output) { return $null }
    $text = ($Output | Out-String).Trim()
    $line = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1).Trim()
    try {
        return ($line | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Escape-ToastText {
    param([string]$Text)
    if (-not $Text) { return '' }
    return (($Text -replace '&', '&amp;') -replace '<', '&lt;') -replace '>', '&gt;'
}

function Show-QueueNotification {
    # WinRT toast plus a console beep. Any failure here is a warning only.
    param([string]$Title, [string]$Body)
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $safeTitle = Escape-ToastText $Title
        $safeBody = Escape-ToastText $Body
        $xmlText = "<toast><visual><binding template=`"ToastGeneric`"><text>$safeTitle</text><text>$safeBody</text></binding></visual></toast>"
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($xmlText)
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
        $appIds = @(
            '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe',
            'PowerShell',
            'Windows PowerShell'
        )
        $shown = $false
        foreach ($appId in $appIds) {
            try {
                $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
                $notifier.Show($toast)
                $shown = $true
                break
            } catch {
                continue
            }
        }
        if (-not $shown) {
            Write-Warning 'Toast notification was not shown.'
        }
    } catch {
        Write-Warning "Toast notification was not shown. $_"
    }
    try {
        [console]::Beep(880, 250)
        [console]::Beep(1175, 250)
    } catch {
        Write-Warning 'Beep was not played.'
    }
}

Assert-NativeWindows
if ($NoArchive -and $ArchiveDir) {
    throw '-NoArchive and -ArchiveDir cannot be used together.'
}
$InstallDir = Get-InstallDir $InstallDir
$python = Get-VenvPython $InstallDir

$taskPath = (Resolve-Path -LiteralPath $TaskDir).Path
if (-not (Test-Path -LiteralPath $Project)) {
    if (-not $InitRepo) {
        throw "Project folder does not exist: $Project. Create it, or pass -InitRepo."
    }
    New-Item -ItemType Directory -Force -Path $Project | Out-Null
}
$projectPath = (Resolve-Path -LiteralPath $Project).Path

$logDir = Join-Path $InstallDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('queue-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

$previousScheme = $null
if (-not $SkipPerformancePlan) {
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
    Write-Host 'Sleep is suspended until this queue exits. The screen may still turn off.'
}

$code = 1
$parsed = $null
$toastTitle = 'Queue ended'
$toastBody = 'The queue stopped before it could report a result. See the log.'
try {
    Start-Transcript -LiteralPath $log -Force | Out-Null
    Write-Host "Tasks:   $taskPath"
    Write-Host "Project: $projectPath"
    Write-Host "Profile: $Profile"
    Write-Host "Budget:  $MaxHoursPerTask hours per task"
    Write-Host "Log:     $log"

    if ($PSBoundParameters.ContainsKey('Port')) {
        $base = "http://127.0.0.1:$Port"
    } else {
        if ($Profile -notmatch '^[A-Za-z0-9_-]+$') { throw "Invalid profile name: $Profile" }
        $py = "from local_agent.config import get_profile; p = get_profile('" + $Profile + "'); print('http://%s:%s' % (p.host, p.port))"
        $base = ((& $python -c $py) | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $base) { throw "Could not read the listen address for profile $Profile." }
    }
    if (-not $NoStartServer -and -not (Test-ServerHealthy $base)) {
        Write-Host 'llama-server is not up. Starting it once for the whole queue...'
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
            throw "llama-server did not answer $base/health within 20 minutes. Read the server window."
        }
    }

    $agentArgs = @(
        '-m', 'local_agent', 'queue',
        '--task-dir', $taskPath,
        '--project', $projectPath,
        '--profile', $Profile,
        '--base-url', $base,
        '--max-hours-per-task', "$MaxHoursPerTask",
        '--max-iterations', "$MaxIterations",
        '--log-dir', (Join-Path $logDir ('queue-run-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
    )
    if ($InitRepo) { $agentArgs += '--init-repo' }
    if ($ContinueOnFailure) { $agentArgs += '--continue-on-failure' }
    if ($NoStrict) { $agentArgs += '--no-strict' }
    if ($AllowMain) { $agentArgs += '--allow-main' }
    if ($NoArchive) { $agentArgs += '--no-archive' }
    if ($ArchiveDir) { $agentArgs += @('--archive-dir', $ArchiveDir) }
    if ($TasksRoot) { $agentArgs += @('--tasks-root', $TasksRoot) }
    $output = & $python @agentArgs
    $code = $LASTEXITCODE
    $parsed = ConvertTo-QueueResult -Output $output
    if ($null -ne $parsed) {
        if ($parsed.status -eq 'stopped_on_request') {
            $toastTitle = 'Queue stopped on request'
            $toastBody = [string]$parsed.message
            if (-not $toastBody) {
                $toastBody = 'The current task finished. No new task was started.'
            }
        } elseif ($parsed.status -eq 'passed') {
            $toastTitle = 'Queue finished'
            $toastBody = "All tasks passed ($($parsed.passed) passed)."
        } elseif ($parsed.stopped_on) {
            $toastTitle = 'Queue stopped'
            $toastBody = "Stopped on $($parsed.stopped_on). $($parsed.passed) passed, $($parsed.failed) failed."
        } else {
            $toastTitle = 'Queue finished with failures'
            $toastBody = "$($parsed.passed) passed, $($parsed.failed) failed. See queue-summary.md."
        }
    }
} catch {
    $toastTitle = 'Queue ended'
    $toastBody = $_.Exception.Message
    Write-Host $_
    if (-not $code) { $code = 1 }
} finally {
    try { Stop-Transcript | Out-Null } catch { }
    try {
        Show-QueueNotification -Title $toastTitle -Body $toastBody
    } catch {
        Write-Warning "Notification failed. The queue result is unchanged. $_"
    }
    if (-not $NoStayAwake) {
        Disable-StayAwake
    }
    if ($previousScheme) {
        & powercfg /setactive $previousScheme
        Write-Host "Restored power scheme $previousScheme."
    }
}

if ($null -ne $parsed -and $parsed.status -eq 'stopped_on_request') {
    if ($parsed.message) {
        Write-Host $parsed.message
    } else {
        Write-Host 'Stopped on request. The current task was allowed to finish. No new task was started.'
    }
    Write-Host 'See queue-summary.md in the task folder.'
    exit 0
}
if ($code -ne 0) {
    Write-Host "Queue finished without every task passing (exit $code). See queue-summary.md in the task folder and $log."
    exit $code
}
Write-Host 'Queue passed. See queue-summary.md in the task folder. Passing tasks were archived under tasks\archive unless -NoArchive was set.'
exit 0
