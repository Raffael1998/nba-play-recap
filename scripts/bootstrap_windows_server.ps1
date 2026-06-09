param(
    [string] $ServerRoot = "C:\srv",
    [string] $DataRoot = "C:\data",
    [string] $LogRoot = "C:\logs",
    [string] $ToolsRoot = "C:\tools",
    [string] $BackupsRoot = "C:\backups",
    [string] $AppName = "nba-play-recap",
    [switch] $SkipPackageInstall,
    [switch] $SkipProjectSetup
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Directory {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Install-WingetPackage {
    param(
        [string] $Id,
        [string] $CommandName
    )

    if ($CommandName -and (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        Write-Host "$CommandName already available."
        return
    }

    Write-Host "Installing $Id..."
    winget install --id $Id --exact --source winget --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

Write-Step "Creating server folder layout"
$paths = @(
    $ServerRoot,
    $DataRoot,
    $LogRoot,
    $ToolsRoot,
    $BackupsRoot,
    (Join-Path $DataRoot $AppName),
    (Join-Path $DataRoot "$AppName\outputs"),
    (Join-Path $LogRoot $AppName)
)

foreach ($path in $paths) {
    Ensure-Directory $path
    Write-Host $path
}

if (-not $SkipPackageInstall) {
    Write-Step "Installing core packages with winget"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is not available. Install App Installer from Microsoft Store, then rerun this script."
    }

    Install-WingetPackage -Id "Git.Git" -CommandName "git"
    Install-WingetPackage -Id "Google.Chrome" -CommandName "chrome"
    Install-WingetPackage -Id "astral-sh.uv" -CommandName "uv"
    Install-WingetPackage -Id "Gyan.FFmpeg" -CommandName "ffmpeg"
    Install-WingetPackage -Id "Microsoft.PowerShell" -CommandName "pwsh"
    Install-WingetPackage -Id "Microsoft.VisualStudioCode" -CommandName "code"
}

Write-Step "Configuring plugged-in power behavior"
if (Test-IsAdmin) {
    powercfg /hibernate off | Out-Null
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change monitor-timeout-ac 15 | Out-Null
    Write-Host "Hibernate disabled; plugged-in sleep disabled; plugged-in display timeout set to 15 minutes."
} else {
    Write-Warning "Power settings require an elevated PowerShell. Rerun as Administrator to disable sleep/hibernate."
}

if (-not $SkipProjectSetup) {
    Write-Step "Preparing nba-play-recap project"
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $repoRoot = Split-Path -Parent $scriptDir
    $pyprojectPath = Join-Path $repoRoot "pyproject.toml"

    if (-not (Test-Path -LiteralPath $pyprojectPath)) {
        Write-Warning "Could not find pyproject.toml next to this script. Clone the repo to $ServerRoot\$AppName, then rerun from that checkout."
    } else {
        Push-Location $repoRoot
        try {
            Refresh-Path
            uv sync
            uv run python -m unittest
            uv run python .\nba_recap.py render-night --dry-run
        } finally {
            Pop-Location
        }
    }
}

Write-Step "Verification"
Refresh-Path
$commands = @("git", "uv", "ffmpeg", "pwsh")
foreach ($command in $commands) {
    $resolved = Get-Command $command -ErrorAction SilentlyContinue
    if ($resolved) {
        Write-Host "$command -> $($resolved.Source)"
    } else {
        Write-Warning "$command is not available on PATH yet. Open a new PowerShell window after installs finish."
    }
}

Write-Host ""
Write-Host "Server base setup complete."
Write-Host "Use scripts\run-nightly.ps1 for scheduled NBA rendering."
