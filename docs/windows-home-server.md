# Windows Home Server Setup

This guide sets up a reset Windows 11 MSI laptop as a flexible local server, with
`nba-play-recap` as the first workload.

## Server Layout

Use neutral top-level folders so future apps can live beside this project:

```powershell
C:\srv       # source code, one folder per app
C:\data      # generated app data and outputs
C:\logs      # scheduled task logs
C:\tools     # manually installed tools if needed
C:\backups   # exported configs and recovery notes
```

For this app:

```powershell
C:\srv\nba-play-recap
C:\data\nba-play-recap\outputs
C:\logs\nba-play-recap
```

## First Boot After Reset

1. Finish Windows setup with a dedicated server account where possible.
2. Avoid restoring old apps, OneDrive sync, and personal settings.
3. Run Windows Update until the machine is current.
4. Keep the laptop plugged in and logged in for NBA rendering. The app needs a
   headed Chrome session to acquire NBA video headers reliably.
5. Install a visual remote access tool such as Chrome Remote Desktop or RustDesk.

## Bootstrap

Clone the repo under `C:\srv`:

```powershell
mkdir C:\srv
cd C:\srv
git clone <repo-url> nba-play-recap
cd C:\srv\nba-play-recap
```

Run PowerShell as Administrator, then:

```powershell
.\scripts\bootstrap_windows_server.ps1
```

The bootstrap script:

- creates the server folder layout,
- installs Git, Chrome, uv, ffmpeg, PowerShell, and VS Code through `winget`,
- disables plugged-in sleep and hibernate when run elevated,
- runs `uv sync`, unit tests, and `render-night --dry-run`.

If tools were installed during the script, open a new PowerShell window before
manual verification.

## Manual Verification

```powershell
git --version
uv --version
ffmpeg -version
pwsh --version
cd C:\srv\nba-play-recap
uv run python -m unittest
uv run python .\nba_recap.py render-night --dry-run
```

Run one real batch after dry-run succeeds:

```powershell
uv run python .\nba_recap.py render-night --output-root C:\data\nba-play-recap\outputs\nightly
```

To validate with a known scoreboard date:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\srv\nba-play-recap\scripts\run-nightly.ps1 -Date 2026-05-30
```

## Scheduled Task

Create a Windows Task Scheduler task that runs only when the server user is
logged on.

Recommended action:

```text
Program/script:
powershell.exe

Arguments:
-NoProfile -ExecutionPolicy Bypass -File C:\srv\nba-play-recap\scripts\run-nightly.ps1

Start in:
C:\srv\nba-play-recap
```

Recommended trigger:

```text
Daily at 08:30 Europe/Paris local time
```

The scheduled runner:

- pulls the latest repo changes with `git pull --ff-only`,
- runs `uv sync`,
- renders the previous NBA night to `C:\data\nba-play-recap\outputs\nightly`,
- writes a transcript log to `C:\logs\nba-play-recap`,
- treats a post-render browser/session exit as successful only when the written
  `run_report.json` contains zero failed games.

## YouTube Upload Automation

YouTube publishing is disabled until OAuth is configured and a manual upload is
validated.

1. In Google Cloud, enable YouTube Data API v3 and create an OAuth Desktop app.
2. Save the OAuth client JSON here:

```text
C:\data\nba-play-recap\secrets\youtube_client_secret.json
```

3. Authorize the channel once from an interactive PowerShell session:

```powershell
cd C:\srv\nba-play-recap
uv run python .\nba_recap.py youtube-auth `
  --client-secrets C:\data\nba-play-recap\secrets\youtube_client_secret.json
```

The refresh token is saved to:

```text
C:\data\nba-play-recap\secrets\youtube_token.json
```

4. Publish a known rendered night manually:

```powershell
uv run python .\nba_recap.py publish-night `
  --report C:\data\nba-play-recap\outputs\nightly\2026-05-30\run_report.json
```

5. Re-run the same command and confirm it skips with `already_uploaded`.
6. After manual validation, enable scheduled publishing by adding
   `-PublishYouTube` to the scheduled task action:

```text
-NoProfile -ExecutionPolicy Bypass -File C:\srv\nba-play-recap\scripts\run-nightly.ps1 -PublishYouTube
```

Uploads request public visibility and set `selfDeclaredMadeForKids` to false.
YouTube may still force private uploads for unverified API projects.

## Future Apps

For each new app:

- place source code in `C:\srv\<app-name>`,
- put generated files in `C:\data\<app-name>`,
- write logs to `C:\logs\<app-name>`,
- keep project dependencies in that app's own virtual environment,
- avoid installing dependencies globally unless they are shared tools.

Install Docker, Node.js, databases, or other runtimes only when a concrete app
needs them.
