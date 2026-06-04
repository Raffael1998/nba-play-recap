param(
    [string] $AppDir = "C:\srv\nba-play-recap",
    [string] $OutputRoot = "C:\data\nba-play-recap\outputs\nightly",
    [string] $LogRoot = "C:\logs\nba-play-recap",
    [int] $RetentionDays = 14,
    [switch] $SkipGitPull,
    [string[]] $ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $LogRoot "render-night-$timestamp.log"

Start-Transcript -Path $logPath -Append | Out-Null
try {
    Refresh-Path
    Write-Host "Started nightly NBA render at $(Get-Date -Format o)"
    Write-Host "AppDir: $AppDir"
    Write-Host "OutputRoot: $OutputRoot"
    Write-Host "LogPath: $logPath"

    if (-not (Test-Path -LiteralPath $AppDir)) {
        throw "App directory not found: $AppDir"
    }

    Push-Location $AppDir
    try {
        if (-not $SkipGitPull -and (Test-Path -LiteralPath ".git")) {
            git pull --ff-only
        }

        uv sync

        $arguments = @(
            ".\nba_recap.py",
            "render-night",
            "--output-root",
            $OutputRoot,
            "--retention-days",
            $RetentionDays
        ) + $ExtraArgs

        uv run python @arguments
    } finally {
        Pop-Location
    }

    Write-Host "Finished nightly NBA render at $(Get-Date -Format o)"
} catch {
    Write-Error $_
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
