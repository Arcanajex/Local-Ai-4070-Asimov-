# Shared helpers for the Windows scripts. Dot-source this file; it does not
# start a server or download anything on its own.
# PowerShell 5.1 (the Windows 11 default) and PowerShell 7 both work.

Set-StrictMode -Version Latest

function Get-RepoRoot {
    # lib.ps1 lives in <repo>\scripts.
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Assert-NativeWindows {
    # WSL reports a Linux environment to the guest and caps the memory the
    # model can commit. That cap is what ran 35B MoE models out of memory in
    # the 8GB community bench. These scripts must run in the Windows host.
    if ($env:WSL_DISTRO_NAME -or $env:WSL_INTEROP) {
        throw "This setup is for native Windows 11, not WSL. Open Windows PowerShell on the Windows host. WSL's RAM cap OOM'd these MoE models in the 8GB bench."
    }
    if ($env:OS -ne 'Windows_NT') {
        throw "These scripts target native Windows 11 (detected OS='$($env:OS)'). Run the unit tests with: python -m pytest -q"
    }
}

function Get-InstallDir {
    param([string]$InstallDir)
    if ($InstallDir) {
        return $InstallDir
    }
    return (Join-Path (Get-RepoRoot) 'runtime')
}

function Find-SystemPython {
    # Prefer the py launcher so "python" pointing at a store alias is skipped.
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.13') },
        @{ Exe = 'py'; Args = @('-3.11') },
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) {
            continue
        }
        & $cmd.Source @($candidate.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        if ($LASTEXITCODE -eq 0) {
            return @{ Exe = $cmd.Source; Args = @($candidate.Args) }
        }
    }
    throw "Python 3.11 or newer was not found. Install it, then open a new PowerShell window: winget install Python.Python.3.12"
}

function Get-VenvPython {
    param([Parameter(Mandatory)][string]$InstallDir)
    $venv = Join-Path $InstallDir 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venv)) {
        throw "No virtualenv at $venv. Run scripts\setup.ps1 first."
    }
    return $venv
}

function Test-ServerHealthy {
    param([string]$BaseUrl = 'http://127.0.0.1:8080')
    $url = $BaseUrl.TrimEnd('/') + '/health'
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return ($response.StatusCode -eq 200 -and ($response.Content -match 'ok'))
    } catch {
        return $false
    }
}

function Enable-StayAwake {
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED. The CPU stays awake while this
    # PowerShell process is alive. The screen is allowed to turn off.
    if (-not ('LocalAgent.StayAwake' -as [type])) {
        Add-Type -Namespace LocalAgent -Name StayAwake -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
    }
    [LocalAgent.StayAwake]::SetThreadExecutionState([uint32]2147483649) | Out-Null
}

function Disable-StayAwake {
    if ('LocalAgent.StayAwake' -as [type]) {
        # ES_CONTINUOUS alone clears the previous request.
        [LocalAgent.StayAwake]::SetThreadExecutionState([uint32]2147483648) | Out-Null
    }
}

function Get-FreeBytes {
    param([Parameter(Mandatory)][string]$Path)
    $parent = $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        $parent = Split-Path -Parent $parent
    }
    while ($parent -and -not (Test-Path -LiteralPath $parent)) {
        $parent = Split-Path -Parent $parent
    }
    $full = [System.IO.Path]::GetFullPath($parent)
    $root = [System.IO.Path]::GetPathRoot($full)
    $drive = New-Object System.IO.DriveInfo $root
    return [int64]$drive.AvailableFreeSpace
}

function Test-NvidiaGpu {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        throw "nvidia-smi was not found. Install the current NVIDIA driver for the RTX 4070 Laptop, then open a new PowerShell window."
    }
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi failed. The CUDA build of llama.cpp cannot start without a working NVIDIA driver."
    }
}

function Get-ProfileRecord {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Name
    )
    if ($Name -notmatch '^[A-Za-z0-9_-]+$') {
        throw "Invalid profile name: $Name"
    }
    $code = "import json; from local_agent.config import get_profile; p = get_profile('" + $Name + "'); print(json.dumps({'name': p.name, 'filename': p.filename, 'size_bytes': p.size_bytes, 'url': p.download_url, 'optional': p.optional}))"
    $raw = & $Python -c $code
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read profile '$Name'."
    }
    return (($raw | Out-String).Trim() | ConvertFrom-Json)
}

function Expand-ZipFlat {
    # llama.cpp ships two zips (tools + CUDA runtime DLLs). They must land in
    # the same directory. A single top-level folder inside the zip is unwrapped.
    param(
        [Parameter(Mandatory)][string]$Zip,
        [Parameter(Mandatory)][string]$Destination
    )
    $staging = Join-Path $Destination ('_extract_' + [guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    & tar.exe -xf $Zip -C $staging
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe could not extract $Zip"
    }
    $children = @(Get-ChildItem -LiteralPath $staging)
    $source = $staging
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $source = $children[0].FullName
    }
    Copy-Item -Path (Join-Path $source '*') -Destination $Destination -Recurse -Force
    Remove-Item -LiteralPath $staging -Recurse -Force
}

function Save-InstallManifest {
    param(
        [Parameter(Mandatory)][string]$InstallDir,
        [Parameter(Mandatory)][string]$LlamaDir,
        [string]$Tag,
        [string]$Cuda
    )
    $manifest = @{
        llama_dir = $LlamaDir
        llama_tag = $Tag
        cuda = $Cuda
        installed_at = (Get-Date).ToString('o')
    }
    $path = Join-Path $InstallDir 'install.json'
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8
}

function Get-LlamaServerExe {
    param([Parameter(Mandatory)][string]$InstallDir)
    $manifestPath = Join-Path $InstallDir 'install.json'
    if (Test-Path -LiteralPath $manifestPath) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $fromManifest = Join-Path $manifest.llama_dir 'llama-server.exe'
        if (Test-Path -LiteralPath $fromManifest) {
            return $fromManifest
        }
    }
    $root = Join-Path $InstallDir 'llama.cpp'
    $found = Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $found) {
        throw "llama-server.exe was not found under $root. Run scripts\setup.ps1."
    }
    return $found.FullName
}
