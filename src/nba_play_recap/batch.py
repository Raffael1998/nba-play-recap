from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from nba_play_recap.browser import NbaBrowser
from nba_play_recap.render import RenderOutputs, render_full_game


PARIS_TZ = ZoneInfo("Europe/Paris")
NEW_YORK_TZ = ZoneInfo("America/New_York")
DEFAULT_OUTPUT_ROOT = Path("outputs") / "nightly"
DEFAULT_RETENTION_DAYS = 14


@dataclass(slots=True)
class ScoreboardGame:
    game_id: str
    game_date: str
    away_tricode: str
    home_tricode: str
    status: str
    status_text: str
    renderable: bool
    skip_reason: str | None = None

    @property
    def matchup(self) -> str:
        return f"{self.away_tricode} @ {self.home_tricode}"


@dataclass(slots=True)
class RenderNightOptions:
    target_date: str
    output_root: Path = DEFAULT_OUTPUT_ROOT
    force: bool = False
    dry_run: bool = False
    ffmpeg_binary: str = "ffmpeg"
    keep_clips: bool = False
    max_events: int | None = None
    clip_retries: int = 2
    prune_overlap: bool = True
    prune_pre_buffer_seconds: float = 2.0
    prune_post_buffer_seconds: float = 2.0
    retention_days: int = DEFAULT_RETENTION_DAYS


Renderer = Callable[[NbaBrowser, str, Path, RenderNightOptions], RenderOutputs]


def resolve_nba_scoreboard_date(
    explicit_date: str | None = None,
    now: datetime | None = None,
) -> str:
    if explicit_date:
        date.fromisoformat(explicit_date)
        return explicit_date
    paris_now = now or datetime.now(PARIS_TZ)
    if paris_now.tzinfo is None:
        paris_now = paris_now.replace(tzinfo=PARIS_TZ)
    new_york_now = paris_now.astimezone(NEW_YORK_TZ)
    return (new_york_now.date() - timedelta(days=1)).isoformat()


def extract_scoreboard_games(cards: list[dict[str, Any]], target_date: str) -> list[ScoreboardGame]:
    """Turn the games page's `cardData` entries into the night's game list.

    The source is `__NEXT_DATA__.props.pageProps.gameCardFeed` on
    `www.nba.com/games?date=...` — the same server-rendered props the play-by-play comes
    from, so the whole pipeline touches no API host (D-036). An out-of-season date
    yields an empty list, which is the "nothing to do" case rather than a failure.
    """
    extracted: list[ScoreboardGame] = []
    for game in cards:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("gameId") or "")
        home_team = game.get("homeTeam", {})
        away_team = game.get("awayTeam", {})
        home_tricode = _team_tricode(home_team)
        away_tricode = _team_tricode(away_team)
        if not game_id or not home_tricode or not away_tricode:
            continue

        status = _status_value(game)
        status_text = str(game.get("gameStatusText") or game.get("gameStatusName") or "")
        renderable = _is_final_status(status, status_text)
        extracted.append(
            ScoreboardGame(
                game_id=game_id,
                game_date=str(
                    game.get("gameDateEst")
                    or game.get("gameTimeEastern")
                    or game.get("gameDate")
                    or target_date
                ),
                away_tricode=away_tricode,
                home_tricode=home_tricode,
                status=status,
                status_text=status_text,
                renderable=renderable,
                skip_reason=None if renderable else "game_not_final",
            )
        )
    return extracted


def render_night(
    browser: NbaBrowser,
    options: RenderNightOptions,
    renderer: Renderer | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(PARIS_TZ)
    day_output_dir = options.output_root / options.target_date
    cards = browser.scheduled_games(options.target_date)
    games = extract_scoreboard_games(cards, options.target_date)
    renderer = renderer or _render_single_game

    report: dict[str, Any] = {
        "target_date": options.target_date,
        "started_at": started_at.isoformat(),
        "ended_at": None,
        "dry_run": options.dry_run,
        "output_root": str(options.output_root),
        "games_discovered": len(games),
        "games_renderable": sum(1 for game in games if game.renderable),
        "games": [],
        "summary": {
            "success": 0,
            "skipped": 0,
            "failed": 0,
        },
    }

    for game in games:
        game_output_dir = day_output_dir / game.game_id
        entry = _base_game_report(game, game_output_dir)
        if not game.renderable:
            entry["status"] = "skipped"
            entry["skip_reason"] = game.skip_reason
            report["summary"]["skipped"] += 1
            report["games"].append(entry)
            continue

        if options.dry_run:
            entry["status"] = "skipped"
            entry["skip_reason"] = "dry_run"
            report["summary"]["skipped"] += 1
            report["games"].append(entry)
            continue

        if not options.force and _game_already_rendered(game_output_dir, game.game_id):
            entry["status"] = "skipped"
            entry["skip_reason"] = "already_rendered"
            report["summary"]["skipped"] += 1
            report["games"].append(entry)
            continue

        try:
            outputs = renderer(browser, game.game_id, game_output_dir, options)
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            report["summary"]["failed"] += 1
            report["games"].append(entry)
            _write_game_status(game_output_dir, entry)
            continue

        entry["status"] = "success"
        entry["outputs"] = _render_outputs_to_dict(outputs)
        report["summary"]["success"] += 1
        report["games"].append(entry)
        _write_game_status(game_output_dir, entry)

    report["ended_at"] = datetime.now(PARIS_TZ).isoformat()
    if not options.dry_run:
        day_output_dir.mkdir(parents=True, exist_ok=True)
        (day_output_dir / "run_report.json").write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )
        prune_old_nightly_outputs(options.output_root, options.target_date, options.retention_days)
    return report


def has_failures(report: dict[str, Any]) -> bool:
    summary = report.get("summary", {})
    return int(summary.get("failed", 0)) > 0


def prune_old_nightly_outputs(output_root: Path, target_date: str, retention_days: int) -> None:
    if retention_days < 1 or not output_root.exists():
        return
    cutoff = date.fromisoformat(target_date) - timedelta(days=retention_days)
    for child in output_root.iterdir():
        if not child.is_dir():
            continue
        try:
            child_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if child_date < cutoff:
            shutil.rmtree(child)


def _render_single_game(
    browser: NbaBrowser,
    game_id: str,
    output_dir: Path,
    options: RenderNightOptions,
) -> RenderOutputs:
    return render_full_game(
        browser=browser,
        game_id=game_id,
        output_dir=output_dir,
        ffmpeg_binary=options.ffmpeg_binary,
        keep_clips=options.keep_clips,
        max_events=options.max_events,
        clip_retries=options.clip_retries,
        prune_overlap=options.prune_overlap,
        prune_pre_buffer_seconds=options.prune_pre_buffer_seconds,
        prune_post_buffer_seconds=options.prune_post_buffer_seconds,
    )


def _base_game_report(game: ScoreboardGame, output_dir: Path) -> dict[str, Any]:
    return {
        "game_id": game.game_id,
        "game_date": game.game_date,
        "matchup": game.matchup,
        "away_tricode": game.away_tricode,
        "home_tricode": game.home_tricode,
        "status_text": game.status_text,
        "status_value": game.status,
        "output_dir": str(output_dir),
        "status": "pending",
        "skip_reason": None,
        "error": None,
        "outputs": None,
    }


def _team_tricode(team: object) -> str:
    if not isinstance(team, dict):
        return ""
    return str(
        team.get("teamTricode")
        or team.get("tricode")
        or team.get("teamAbbreviation")
        or ""
    ).upper()


def _status_value(game: dict[str, Any]) -> str:
    for key in ("gameStatus", "gameStatusCode", "statusNum"):
        value = game.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _is_final_status(status: str, status_text: str) -> bool:
    normalized_status = status.strip().lower()
    normalized_text = status_text.strip().lower()
    if normalized_status == "3":
        return True
    final_markers = ("final", "game over", "completed")
    return any(marker in normalized_text for marker in final_markers)


def _game_already_rendered(output_dir: Path, game_id: str) -> bool:
    return (output_dir / f"{game_id}_full_game.mp4").exists() and (output_dir / "game_status.json").exists()


def _write_game_status(output_dir: Path, entry: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "game_status.json").write_text(
        json.dumps(entry, indent=2),
        encoding="utf-8",
    )


def _render_outputs_to_dict(outputs: RenderOutputs) -> dict[str, Any]:
    payload = asdict(outputs)
    for key, value in payload.items():
        if isinstance(value, Path):
            payload[key] = str(value)
    return payload
