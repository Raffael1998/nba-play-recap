# NBA Play Recap

Build short, game-level NBA video recaps from official per-play clips and play-by-play data.

## Goal

Given a `GameID`, fetch the play-by-play timeline, discover which events have video, rank the most important plays, and produce a compact "watch the game in 5 minutes" recap.

## Why This Project

The NBA site already exposes short clips for many game events. The missing piece is a workflow that:

- collects the event timeline for a game,
- resolves available clips,
- scores which moments matter,
- assembles them into a coherent recap.

## Initial Scope

- Personal-use research project.
- Start with one full game recap.
- Prefer official NBA clips over scraping broadcast streams.
- Produce a JSON recap timeline first.
- Add video stitching only after the data pipeline is reliable.

## Constraints

- NBA clip and stats usage appears restricted to personal, non-commercial use.
- Event clips are not guaranteed for every play.
- The best recap is not "all scoring plays"; it needs context such as runs, lead changes, clutch possessions, blocks, turnovers, and notable star plays.

## Current References

See [docs/feasibility.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/feasibility.md) and [docs/plan.md](C:/Users/rgros/Documents/python_projects/nba-play-recap/docs/plan.md).

## Phase 1 CLI

The current CLI validates the data path by:

- pulling the game timeline from the NBA live play-by-play feed,
- probing selected event numbers against the NBA `videoeventsasset` endpoint,
- listing the events that resolve to a clip URL.

```powershell
python .\nba_recap.py candidates --game-id 0042500151
python .\nba_recap.py candidates --game-id 0042500151 --json
python .\nba_recap.py candidates --game-id 0042500151 --save-raw data\raw\0042500151_playbyplay.json
python .\nba_recap.py candidates --game-id 0042500151 --max-events 120
```

To render the full chronological video from all available event clips:

```powershell
python .\nba_recap.py manifest --game-id 0042500151 --output-dir outputs
python .\nba_recap.py render-full-game --game-id 0042500151 --output-dir outputs
```

That command writes:

- `outputs\0042500151_manifest.txt`
- `outputs\0042500151_manifest.json`
- `outputs\0042500151_concat.txt`
- `outputs\0042500151_full_game.mp4`

Downloaded clip files are deleted after rendering unless you pass `--keep-clips`.

If you install the project in editable mode later, you can use:

```powershell
python -m nba_play_recap candidates --game-id 0042500151
nba-recap candidates --game-id 0042500151
```
