from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nba_play_recap.batch import (
    RenderNightOptions,
    extract_scoreboard_games,
    has_failures,
    render_night,
    resolve_nba_scoreboard_date,
)
from nba_play_recap.render import RenderOutputs


class FakeBrowser:
    """Stands in for NbaBrowser: `render_night` only ever asks it for the day's cards."""

    def __init__(self, cards: list[dict]) -> None:
        self.cards = cards
        self.requested_dates: list[str] = []

    def scheduled_games(self, game_date: str) -> list[dict]:
        self.requested_dates.append(game_date)
        return self.cards


def game_cards() -> list[dict]:
    """The shape of `gameCardFeed.modules[].cards[].cardData` on www.nba.com/games."""
    return [
        {
            "gameId": "001",
            "gameStatus": 3,
            "gameStatusText": "Final",
            "gameTimeEastern": "2026-01-02",
            "awayTeam": {"teamTricode": "BOS"},
            "homeTeam": {"teamTricode": "NYK"},
        },
        {
            "gameId": "002",
            "gameStatus": 2,
            "gameStatusText": "Q3 04:12",
            "gameTimeEastern": "2026-01-02",
            "awayTeam": {"teamTricode": "LAL"},
            "homeTeam": {"teamTricode": "GSW"},
        },
    ]


def fake_outputs(output_dir: Path, game_id: str) -> RenderOutputs:
    video_path = output_dir / f"{game_id}_full_game.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")
    return RenderOutputs(
        manifest_txt_path=output_dir / f"{game_id}_manifest.txt",
        manifest_json_path=output_dir / f"{game_id}_manifest.json",
        debug_json_path=output_dir / "debug" / f"{game_id}_debug.json",
        concat_list_path=output_dir / f"{game_id}_concat.txt",
        video_path=video_path,
        downloaded_clip_count=4,
        available_clip_count=5,
        rendered_clip_count=4,
        total_event_count=12,
    )


class BatchTests(unittest.TestCase):
    def test_default_scoreboard_date_uses_previous_new_york_date(self) -> None:
        now = datetime(2026, 6, 1, 8, 30, tzinfo=ZoneInfo("Europe/Paris"))

        self.assertEqual(resolve_nba_scoreboard_date(now=now), "2026-05-31")

    def test_explicit_scoreboard_date_is_used_as_is(self) -> None:
        self.assertEqual(resolve_nba_scoreboard_date("2026-01-02"), "2026-01-02")

    def test_scoreboard_extraction_selects_only_completed_games(self) -> None:
        games = extract_scoreboard_games(game_cards(), "2026-01-02")

        self.assertEqual([game.game_id for game in games], ["001", "002"])
        self.assertTrue(games[0].renderable)
        self.assertFalse(games[1].renderable)
        self.assertEqual(games[1].skip_reason, "game_not_final")

    def test_render_night_renders_final_games_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            browser = FakeBrowser(game_cards())
            calls: list[str] = []

            def renderer(_browser, game_id, output_dir, _options):
                calls.append(game_id)
                return fake_outputs(output_dir, game_id)

            report = render_night(
                browser,
                RenderNightOptions(target_date="2026-01-02", output_root=Path(tmpdir)),
                renderer=renderer,
            )

            self.assertEqual(calls, ["001"])
            self.assertEqual(report["summary"], {"success": 1, "skipped": 1, "failed": 0})
            report_path = Path(tmpdir) / "2026-01-02" / "run_report.json"
            self.assertTrue(report_path.exists())
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["games"][0]["outputs"]["downloaded_clip_count"], 4)

    def test_render_night_skips_already_rendered_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            game_dir = root / "2026-01-02" / "001"
            (game_dir / "001_full_game.mp4").parent.mkdir(parents=True, exist_ok=True)
            (game_dir / "001_full_game.mp4").write_bytes(b"video")
            (game_dir / "game_status.json").write_text("{}", encoding="utf-8")

            def renderer(_browser, _game_id, _output_dir, _options):
                raise AssertionError("renderer should not be called")

            report = render_night(
                FakeBrowser(game_cards()),
                RenderNightOptions(target_date="2026-01-02", output_root=root),
                renderer=renderer,
            )

            self.assertEqual(report["games"][0]["status"], "skipped")
            self.assertEqual(report["games"][0]["skip_reason"], "already_rendered")

    def test_render_night_continues_after_failure_and_reports_exit_risk(self) -> None:
        cards = game_cards()
        cards[1]["gameStatus"] = 3
        cards[1]["gameStatusText"] = "Final"
        with tempfile.TemporaryDirectory() as tmpdir:

            def renderer(_browser, game_id, output_dir, _options):
                if game_id == "001":
                    raise RuntimeError("boom")
                return fake_outputs(output_dir, game_id)

            report = render_night(
                FakeBrowser(cards),
                RenderNightOptions(target_date="2026-01-02", output_root=Path(tmpdir)),
                renderer=renderer,
            )

            self.assertEqual(report["summary"], {"success": 1, "skipped": 0, "failed": 1})
            self.assertTrue(has_failures(report))


if __name__ == "__main__":
    unittest.main()
