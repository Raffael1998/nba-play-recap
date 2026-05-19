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
uv run python .\nba_recap.py render-full-game --game-id <GAME_ID> --output-dir outputs_<GAME_ID> --ffmpeg-binary ffmpeg
```

That single command:

- fetches play-by-play,
- probes clip metadata,
- applies overlap and duplicate pruning,
- writes manifest/debug outputs,
- downloads the kept clips,
- renders the final MP4.

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
- The current recap is chronological, not quality-ranked yet.

## References

See [docs/feasibility.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/feasibility.md) and [docs/plan.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/plan.md).
