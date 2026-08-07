# NBA Play Recap

Build local NBA game recap videos from official per-play clips and live play-by-play data.

Personal-use tool. It runs unattended every morning on a Debian home server, scheduled by
that server's job platform — see [docs/deployment.md](docs/deployment.md).

## What it does

Given an NBA `GameID`, the pipeline:

- fetches the live play-by-play timeline,
- resolves which events have an official clip,
- estimates overlap between clips and drops the fully-covered ones,
- removes duplicate clips by comparing sampled video frames,
- writes manifest and debug files,
- stitches the kept clips into one chronological MP4.

The goal is "watch the game through official event clips", not a ranked highlight package.

### Pruning, in two passes

1. **Overlap pruning** — each clip's live-game window is estimated, and clips whose
   coverage is fully contained by other kept clips in the same period are removed.
2. **Fingerprint pruning** — within a quarter, clips of identical duration are compared by
   frame hashes at 20 %, 50 % and 80 %. Matching frames mean the later clip is a duplicate.

Both exist because NBA regularly points several event IDs at the same underlying video.

## Requirements

| | |
|---|---|
| Python | 3.10+ |
| `uv` | dependency resolution and the `nba-recap` entry point |
| `ffmpeg` / `ffprobe` | concatenation and duration probing |
| Chromium | session acquisition — Debian's `/usr/bin/chromium` is fine and preferred |
| `xvfb` | on a headless host, because the browser must run *headed* |

On Debian 13, all of it is packaged:

```bash
sudo apt-get install -y ffmpeg xvfb chromium
uv sync
```

**A real browser is not optional.** NBA rejects true headless browsers in testing, and a
plain HTTP request for a clip returns NBA's *"Video not available"* placeholder rather
than an error. So the pipeline briefly opens Chromium on an event-video page, captures the
video session from the request it makes, and closes it. On a headless host that means
`xvfb-run -a`, which keeps the browser headed from the site's point of view.

Point Playwright at the system browser rather than letting it download its own bundle:

```bash
--video-browser-executable /usr/bin/chromium
```

## Commands

All commands are `uv run nba-recap <command>`, or `uv run python -m nba_play_recap <command>`.

### One game

```bash
xvfb-run -a uv run nba-recap render-full-game \
  --game-id <GAME_ID> \
  --output-dir outputs_<GAME_ID> \
  --video-browser-executable /usr/bin/chromium
```

### A whole night — this is the scheduled one

```bash
xvfb-run -a uv run nba-recap render-night \
  --output-root <root> \
  --video-browser-executable /usr/bin/chromium
```

`render-night` defaults to **yesterday in `America/New_York`**, which is the right target
for a morning run in Europe. Useful variants:

```bash
--dry-run                       # list what would run, render nothing
--date 2026-05-05               # a specific NBA scoreboard date
--force                         # re-render games that already succeeded
--retention-days 30             # keep more than the default 14 days
```

Games with a prior successful output are skipped, so a retry or a catch-up run is safe.

### Finding a GameID without spoilers

```bash
uv run nba-recap game-id --team SAS --yesterday
uv run nba-recap game-id --team SAS --date 2026-05-05
```

Prints the `GameID`, matchup tricode and date — never the score.

### Inspecting without rendering

```bash
uv run nba-recap candidates --game-id <GAME_ID> --json
uv run nba-recap manifest --game-id <GAME_ID> --output-dir outputs_<GAME_ID>
```

`manifest` runs the full selection and duplicate detection, then stops before the MP4.

## Exit codes

The scheduling platform depends on these:

| Code | Meaning |
|---|---|
| `0` | nothing needs a human — including "there were no games that night" |
| `1` | at least one game failed to render |
| `2` | a hard error before per-game work began |

## Output

```text
<root>/<YYYY-MM-DD>/run_report.json
<root>/<YYYY-MM-DD>/<GAME_ID>/<GAME_ID>_full_game.mp4
<root>/<YYYY-MM-DD>/<GAME_ID>/game_status.json
```

For a single-game run into `--output-dir`:

```text
<GAME_ID>_manifest.txt   <GAME_ID>_manifest.json   <GAME_ID>_concat.txt
<GAME_ID>_full_game.mp4  debug/<GAME_ID>_debug.json
```

Downloaded clips are deleted after rendering unless `--keep-clips` is passed.

`run_report.json` is read by the scheduled job to report its summary, so treat its shape
as a published interface rather than a debug artefact.

## Manual session fallback

If automatic session capture stops working:

1. Open one spoiler-safe play video on `nba.com/stats/events`.
2. In DevTools → Network, select its `.mp4` request.
3. *Copy → Copy as PowerShell*, and save it as `debug_clip_download.ps1`.

```bash
uv run nba-recap render-full-game --game-id <GAME_ID> --output-dir outputs_<GAME_ID> \
  --no-auto-video-session --video-session-script ./debug_clip_download.ps1
```

Do not commit that file — it carries live browser cookies, and `.gitignore` already
excludes it.

## Notes and known fragility

- NBA clip and stats availability is not guaranteed, and this is the project's main risk.
- `stats.nba.com` and `cdn.nba.com` sit behind Akamai bot management. Responses vary with
  headers, request rate and source IP, and a `403` or a hanging request is more often
  rate-limiting than a bug here.
- **A `200` is not proof of success.** NBA serves a ~31.5 MB "Video not available" MP4
  with a valid content type. The renderer hashes downloads against that placeholder and
  refuses it; `KNOWN_VIDEO_NOT_AVAILABLE_SHA256` in `render.py` is the check.
- The `cdn.nba.com` live endpoints need the `Sec-Fetch-*` headers. Without them Akamai
  answers `403` — see the comment on `LIVE_HEADERS` in `client.py`.
- Rendering is deliberately sequential (`--max-workers 1`): friendlier to session
  acquisition, and this is not a throughput problem.
- The recap is chronological, not quality-ranked.

## References

- [docs/deployment.md](docs/deployment.md) — how and where this actually runs
- [docs/feasibility.md](docs/feasibility.md)
- [docs/plan.md](docs/plan.md)
