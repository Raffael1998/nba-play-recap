# Cloud VM Deployment

This guide targets the first public-project style deployment: reliable enough for daily use, simple to operate, and not the most expensive option.

## Recommended VM Options

Use Ubuntu 24.04 LTS with at least 2 vCPU, 4 GB RAM, and 40-80 GB disk.

Good low-cost choices:

1. Hetzner CX22 or similar: best price/performance when account creation is available.
2. Scaleway DEV1-style instance: good EU option, availability varies.
3. OVH VPS Starter/Comfort: familiar French/EU provider, reasonable cost.
4. DigitalOcean Basic Droplet: easiest onboarding, usually more expensive for the same resources.

Avoid serverless for this project. The renderer needs `ffmpeg`, Chrome, Playwright, a long-running process, and a headed-browser-compatible display through `xvfb`.

## Server Setup

Install system packages:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates ffmpeg xvfb
curl -L -o /tmp/google-chrome-stable_current_amd64.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y /tmp/google-chrome-stable_current_amd64.deb
```

Install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Clone and install:

```bash
sudo mkdir -p /opt/nba-play-recap
sudo chown "$USER":"$USER" /opt/nba-play-recap
git clone <YOUR_REPO_URL> /opt/nba-play-recap/app
cd /opt/nba-play-recap/app
uv sync
uv run python -m playwright install-deps chromium
```

Run a dry-run check:

```bash
cd /opt/nba-play-recap/app
uv run nba-recap render-night --dry-run
```

Run one real batch manually:

```bash
cd /opt/nba-play-recap/app
xvfb-run -a uv run nba-recap render-night --output-root /opt/nba-play-recap/outputs/nightly
```

## systemd Service

Create `/etc/systemd/system/nba-recap-nightly@.service`:

```ini
[Unit]
Description=Render nightly NBA recap videos
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=%i
WorkingDirectory=/opt/nba-play-recap/app
ExecStart=/usr/bin/xvfb-run -a /home/%i/.local/bin/uv run nba-recap render-night --output-root /opt/nba-play-recap/outputs/nightly
```

Create `/etc/systemd/system/nba-recap-nightly.timer`:

```ini
[Unit]
Description=Run NBA recap rendering every morning Paris time

[Timer]
OnCalendar=*-*-* 08:30:00 Europe/Paris
Persistent=true
Unit=nba-recap-nightly@YOUR_USERNAME.service

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nba-recap-nightly.timer
```

Check logs:

```bash
journalctl -u nba-recap-nightly@YOUR_USERNAME.service -n 200 --no-pager
```

## Output Layout

Nightly runs write:

```text
/opt/nba-play-recap/outputs/nightly/YYYY-MM-DD/run_report.json
/opt/nba-play-recap/outputs/nightly/YYYY-MM-DD/<GAME_ID>/<GAME_ID>_full_game.mp4
/opt/nba-play-recap/outputs/nightly/YYYY-MM-DD/<GAME_ID>/game_status.json
```

The command keeps 14 days of dated output folders by default. Override with:

```bash
uv run nba-recap render-night --retention-days 30
```

## Operational Notes

- Keep rendering sequential in v1. It is slower, but friendlier to cheap VMs and NBA session acquisition.
- Use the default headed Chrome behavior under `xvfb`; true headless mode has not been reliable against NBA.
- Check `run_report.json` first after failures. It records skipped games, failed games, and output paths.
- If NBA session acquisition fails repeatedly, run a manual single-game render on the VM to confirm Chrome, Playwright, and network behavior before changing code.
