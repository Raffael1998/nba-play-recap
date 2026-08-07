# Deployment

This project is deployed on a **personal home server**, not a rented VM, and its
schedule is owned by that server's scheduling platform rather than by unit files kept
here.

**The deployment lives in the `homeserver` repo**, not in this one:

- the job definition — schedule, timeout, retries, the exact command — is one `[[job]]`
  block in that repo's `scripts/jobs.toml`;
- the systemd `.service` and `.timer` are **generated** from that block by `job deploy`;
- runs are recorded in `/srv/data/jobs/runs.jsonl` and read back with `job log`,
  `job show` and `job status`.

Nothing in this repo should define a schedule, a systemd unit or a retention policy that
competes with it. The previous version of this file did all three — it described renting
an Ubuntu VM, installing Google Chrome from Google's apt repository, and hand-writing an
`nba-recap-nightly@.service` — and every part of that is now wrong for how this actually
runs.

## What the program needs from its host

This is the part still worth recording here, because it is a property of the program
rather than of one deployment:

| Need | Why |
|---|---|
| `ffmpeg` and `ffprobe` | clip concatenation and duration probing |
| A **Chromium** binary | session acquisition; pass it with `--video-browser-executable` |
| `xvfb` | NBA rejects true headless browsers, so Chromium must run *headed* against a virtual display, via `xvfb-run -a` |
| `uv` | dependency resolution and the entry point |
| A writable `HOME` | Chromium refuses to start without one, and a scheduler does not supply one by default |

On Debian 13 all of `ffmpeg`, `xvfb` and `chromium` come from Debian's own repositories.
**Prefer them over Google Chrome's `.deb`**: a browser that updates itself outside the
host's normal patching is the last thing a scheduled job should depend on. Playwright
drives `/usr/bin/chromium` directly through `--video-browser-executable`, which also
avoids downloading Playwright's own browser bundle into `~/.cache/ms-playwright`.

## Running one batch by hand

```bash
xvfb-run -a uv run nba-recap render-night \
  --output-root <root> \
  --video-browser-executable /usr/bin/chromium
```

`render-night` defaults to *yesterday in America/New_York*, which is the correct target
for a morning run in Europe. With no games on that date it does nothing and exits 0 —
which is also exactly what it does for the four months a year the NBA is out of season.

## Exit codes

These are the contract, and the scheduling platform relies on them:

| Code | Meaning |
|---|---|
| `0` | nothing needs a human — including "there were no games" |
| `1` | at least one game failed to render |
| `2` | a hard error before per-game work began |

## Output layout

```text
<root>/<YYYY-MM-DD>/run_report.json
<root>/<YYYY-MM-DD>/<GAME_ID>/<GAME_ID>_full_game.mp4
<root>/<YYYY-MM-DD>/<GAME_ID>/game_status.json
```

`run_report.json` records what was discovered, rendered, skipped and failed. It is the
source the scheduled job reads to report a summary, so treat its shape as a published
interface.

## Operational notes

- Keep rendering sequential. It is slower, but far friendlier to NBA session acquisition.
- Use headed Chromium under `xvfb`. True headless has never been reliable against NBA.
- Read `run_report.json` first after any failure.
- If session acquisition fails repeatedly, render a single game by hand before changing
  code — it is usually the browser or the network, not the pipeline.
