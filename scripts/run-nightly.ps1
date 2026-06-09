param(
    [string] $AppDir = "C:\srv\nba-play-recap",
    [string] $OutputRoot = "C:\data\nba-play-recap\outputs\nightly",
    [string] $LogRoot = "C:\logs\nba-play-recap",
    [string] $Date,
    [int] $RetentionDays = 14,
    [switch] $PublishYouTube,
    [switch] $SkipGitPull,
    [string[]] $ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "C:\Program Files\Git\cmd;$machinePath;$userPath"
    $env:PYTHONWARNINGS = "ignore::FutureWarning:google.api_core._python_version_support"
}

function Get-RunReportPath {
    param([string[]] $Lines)
    $reportLine = $Lines | Where-Object { $_ -like "Run report:*" } | Select-Object -Last 1
    if (-not $reportLine) {
        return $null
    }
    return ($reportLine -replace "^Run report:\s*", "").Trim()
}

function Test-RunReportHasNoFailures {
    param([string] $ReportPath)
    if (-not $ReportPath -or -not (Test-Path -LiteralPath $ReportPath)) {
        return $false
    }
    $report = Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json
    return [int] $report.summary.failed -eq 0
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
    Write-Host "PublishYouTube: $PublishYouTube"

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
        )

        if ($Date) {
            $arguments += @("--date", $Date)
        }

        if ($PublishYouTube) {
            $arguments += "--publish-youtube"
        }

        $arguments += $ExtraArgs

        $renderOutput = & uv run python @arguments 2>&1
        $renderExitCode = $LASTEXITCODE
        $renderOutput | ForEach-Object { Write-Host $_ }

        if ($renderExitCode -ne 0) {
            $reportPath = Get-RunReportPath -Lines $renderOutput
            if (-not $PublishYouTube -and (Test-RunReportHasNoFailures -ReportPath $reportPath)) {
                Write-Warning "uv exited with $renderExitCode, but the run report has zero failed games: $reportPath"
            } else {
                throw "render-night failed with exit code $renderExitCode"
            }
        }
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
