# NBA Play Recap

Build local NBA game recap videos from official per-play clips and live play-by-play data.

## What It Does

Given an NBA `GameID`, the pipeline:

- fetches the live play-by-play timeline,
- resolves which events have an official clip,
- estimates overlap between clips,
- removes duplicate clips by comparing sampled video frames,
- writes manifest/debug files,
- stitches the kept clips into one chronological MP4.

The current goal is a robust "watch the game through official event clips" workflow, not a ranked highlight package yet.

## Current Behavior

The clip selection pipeline currently does two main kinds of pruning:

1. Overlap pruning
It estimates each clip's live-game window and removes clips whose estimated coverage is fully contained by other kept clips in the same period.

2. Fingerprint pruning
Within each quarter, clips that share the same exact duration are compared by frame hashes at `20%`, `50%`, and `80%` of the clip. If the sampled frames match, the later clip is treated as a duplicate and removed.

This is meant to handle NBA cases where multiple event IDs point to the same underlying video.

## Requirements

- Python `3.10+`
- `ffmpeg` available on `PATH`
- `uv` installed locally
- Google Chrome installed locally for automatic NBA video-session acquisition

Optional:

- editable install if you want the `nba-recap` command instead of `uv run python .\nba_recap.py`

## Setup

Install the local environment with:

```powershell
uv sync
```

Then run commands with `uv run`.

## Main Command

To generate the full recap video for one game:

```powershell
uv run python .\nba_recap.py render-full-game `
  --game-id <GAME_ID> `
  --output-dir outputs_<GAME_ID>
```

That single command:

- fetches play-by-play,
- probes clip metadata,
- opens Chrome briefly on the first event page to obtain a fresh NBA video session,
- stops immediately if NBA returns a `Video not available` placeholder,
- applies overlap and duplicate pruning,
- writes manifest/debug outputs,
- downloads the kept clips,
- renders the final MP4.

The browser is opened directly on an event-video page rather than a score page. NBA currently rejects tested headless-browser requests, so the working default uses a visible Chrome window and closes it as soon as the video request is captured.

If Chrome is unavailable, pass another installed Chromium browser channel, for example:

```powershell
uv run python .\nba_recap.py render-full-game `
  --game-id <GAME_ID> `
  --output-dir outputs_<GAME_ID> `
  --video-browser-channel msedge
```

## Manual Session Fallback

If automatic session capture stops working:

1. Open one spoiler-safe play video on `nba.com/stats/events`.
2. In Chrome DevTools Network, select its `.mp4` request.
3. Use `Copy > Copy as PowerShell`.
4. Save it locally as `debug_clip_download.ps1`.

Then run:

```powershell
uv run python .\nba_recap.py render-full-game `
  --game-id <GAME_ID> `
  --output-dir outputs_<GAME_ID> `
  --no-auto-video-session `
  --video-session-script .\debug_clip_download.ps1
```

Do not commit `debug_clip_download.ps1`; it contains temporary browser cookies and is ignored by Git.

## No-Spoiler GameID Lookup

If you want the `GameID` without opening the NBA website and risking spoilers:

```powershell
uv run python .\nba_recap.py game-id --team SAS --yesterday
uv run python .\nba_recap.py game-id --team SAS --date 2026-05-05
```

That command prints:

- `GameID`
- matchup tricode
- date

It does not print the score.

## Nightly Batch Rendering

To render every completed NBA game from the previous NBA night:

```powershell
uv run python .\nba_recap.py render-night
```

By default, `render-night` uses yesterday's date in `America/New_York`, which is the intended behavior for a morning run in France.

To inspect what would run without rendering:

```powershell
uv run python .\nba_recap.py render-night --dry-run
```

To render a specific NBA scoreboard date:

```powershell
uv run python .\nba_recap.py render-night --date 2026-05-05 --output-root outputs\nightly
```

Nightly outputs are written under:

- `outputs\nightly\<YYYY-MM-DD>\run_report.json`
- `outputs\nightly\<YYYY-MM-DD>\<GAME_ID>\<GAME_ID>_full_game.mp4`
- `outputs\nightly\<YYYY-MM-DD>\<GAME_ID>\game_status.json`

Existing successful game outputs are skipped unless you pass `--force`. Dated nightly output folders older than 14 days are deleted after each non-dry-run batch; override this with `--retention-days`.

## Output Files

For `--output-dir outputs_<GAME_ID>`, the pipeline writes:

- `outputs_<GAME_ID>\<GAME_ID>_manifest.txt`
- `outputs_<GAME_ID>\<GAME_ID>_manifest.json`
- `outputs_<GAME_ID>\debug\<GAME_ID>_debug.json`
- `outputs_<GAME_ID>\<GAME_ID>_concat.txt`
- `outputs_<GAME_ID>\<GAME_ID>_full_game.mp4`

Temporary downloaded clips are deleted after rendering unless you pass `--keep-clips`.

## Manifest-Only Mode

If you want to inspect the selected clips without rendering the final video:

```powershell
uv run python .\nba_recap.py manifest --game-id <GAME_ID> --output-dir outputs_<GAME_ID> --ffmpeg-binary ffmpeg
```

This still performs fingerprint-based duplicate detection. It just stops before building the final MP4.

Current safer defaults for both commands are:

- `--max-workers 1`
- `--request-retries 3`
- `--request-timeout-seconds 12`
- `--retry-backoff-seconds 1.5`

## Scheduled VM Execution

The automated session refresh needs a browser that behaves as headed Chrome. In current testing, NBA rejects Chrome/Chromium in true headless mode.

For a Linux VM, run the scheduled job under a virtual display such as `xvfb`, keeping Chrome in headed mode from the site's point of view:

```bash
xvfb-run -a uv run python ./nba_recap.py render-full-game \
  --game-id <GAME_ID> \
  --output-dir outputs_<GAME_ID>
```

For a Windows VM, schedule the command in an interactive desktop session with Chrome installed. A fully non-interactive Windows scheduled task is not yet validated.

See [docs/deployment.md](docs/deployment.md) for cloud VM options and a `systemd` timer setup.

## Windows Home Server

For a local always-on Windows laptop/server setup, see [docs/windows-home-server.md](docs/windows-home-server.md).

The repo includes:

- `scripts/bootstrap_windows_server.ps1` to create the server folder layout, install base tools, configure plugged-in power behavior, and verify the project.
- `scripts/run-nightly.ps1` to run the nightly batch with logs and an output root outside the repo.

## Candidate Inspection

To inspect raw clip-backed candidates before the full pipeline:

```powershell
uv run python .\nba_recap.py candidates --game-id <GAME_ID>
uv run python .\nba_recap.py candidates --game-id <GAME_ID> --json
uv run python .\nba_recap.py candidates --game-id <GAME_ID> --max-events 120
uv run python .\nba_recap.py candidates --game-id <GAME_ID> --save-raw data\raw\<GAME_ID>_playbyplay.json
```

## Entry Points

After `uv sync`, you can use:

```powershell
uv run python -m nba_play_recap render-full-game --game-id <GAME_ID> --output-dir outputs_<GAME_ID>
uv run nba-recap render-full-game --game-id <GAME_ID> --output-dir outputs_<GAME_ID>
```

## Notes

- This is a personal-use local tool.
- NBA clip and stats availability is not guaranteed.
- Some clip URLs are fragile and may return placeholders or `403` responses depending on headers or timing.
- Automatic session refresh has been verified with visible Google Chrome; true headless mode is currently not reliable against NBA.
- The current recap is chronological, not quality-ranked yet.

## References

See [docs/feasibility.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/feasibility.md) and [docs/plan.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/plan.md).
