from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from nba_play_recap import cli
from nba_play_recap.youtube import (
    YouTubeUploadResult,
    build_youtube_metadata,
    publish_night_report,
)


def game_entry(root: Path, status: str = "success") -> dict:
    game_dir = root / "2026-01-02" / "001"
    video_path = game_dir / "001_full_game.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")
    return {
        "game_id": "001",
        "game_date": "2026-01-02",
        "matchup": "BOS @ NYK",
        "away_tricode": "BOS",
        "home_tricode": "NYK",
        "status": status,
        "skip_reason": None,
        "error": None,
        "output_dir": str(game_dir),
        "outputs": {
            "video_path": str(video_path),
        },
    }


def write_report(root: Path, games: list[dict]) -> Path:
    report_path = root / "2026-01-02" / "run_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "target_date": "2026-01-02",
                "dry_run": False,
                "games": games,
                "summary": {"success": 1, "skipped": 0, "failed": 0},
            }
        ),
        encoding="utf-8",
    )
    return report_path


class YouTubePublishTests(unittest.TestCase):
    def test_builds_default_metadata_from_game(self) -> None:
        metadata = build_youtube_metadata(
            {
                "matchup": "BOS @ NYK",
                "game_date": "2026-01-02",
            }
        )

        self.assertEqual(metadata["snippet"]["title"], "BOS @ NYK Full Game Recap - 2026-01-02")
        self.assertIn("Automatically generated chronological NBA recap", metadata["snippet"]["description"])
        self.assertEqual(metadata["status"]["privacyStatus"], "public")
        self.assertFalse(metadata["status"]["selfDeclaredMadeForKids"])

    def test_publish_report_uploads_successful_render_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = write_report(root, [game_entry(root)])
            uploads: list[Path] = []

            def uploader(video_path, _metadata, _client_secrets, _token):
                uploads.append(video_path)
                return YouTubeUploadResult(
                    video_id="yt123",
                    privacy_status="public",
                    response={"id": "yt123", "status": {"privacyStatus": "public"}},
                )

            report = publish_night_report(
                report_path,
                client_secrets_path=root / "client.json",
                token_path=root / "token.json",
                uploader=uploader,
            )

            self.assertEqual(report["summary"], {"success": 1, "skipped": 0, "failed": 0})
            self.assertEqual(len(uploads), 1)
            status = json.loads((root / "2026-01-02" / "001" / "youtube_status.json").read_text())
            self.assertEqual(status["video_id"], "yt123")
            self.assertEqual(status["privacy_requested"], "public")

    def test_publish_report_skips_already_uploaded_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            game = game_entry(root)
            status_path = root / "2026-01-02" / "001" / "youtube_status.json"
            status_path.write_text(
                json.dumps({"status": "success", "video_id": "existing", "matchup": "BOS @ NYK"}),
                encoding="utf-8",
            )
            report_path = write_report(root, [game])

            def uploader(*_args):
                raise AssertionError("uploader should not run")

            report = publish_night_report(report_path, root / "client.json", root / "token.json", uploader=uploader)

            self.assertEqual(report["summary"], {"success": 0, "skipped": 1, "failed": 0})
            self.assertEqual(report["games"][0]["skip_reason"], "already_uploaded")
            self.assertEqual(report["games"][0]["video_id"], "existing")

    def test_publish_report_uploads_already_rendered_game_from_game_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            prior_success = game_entry(root)
            game_dir = root / "2026-01-02" / "001"
            (game_dir / "game_status.json").write_text(json.dumps(prior_success), encoding="utf-8")
            skipped_entry = dict(prior_success)
            skipped_entry["status"] = "skipped"
            skipped_entry["skip_reason"] = "already_rendered"
            skipped_entry["outputs"] = None
            report_path = write_report(root, [skipped_entry])

            def uploader(_video_path, _metadata, _client_secrets, _token):
                return YouTubeUploadResult(
                    video_id="yt456",
                    privacy_status="public",
                    response={"id": "yt456", "status": {"privacyStatus": "public"}},
                )

            report = publish_night_report(report_path, root / "client.json", root / "token.json", uploader=uploader)

            self.assertEqual(report["summary"], {"success": 1, "skipped": 0, "failed": 0})
            self.assertEqual(report["games"][0]["video_id"], "yt456")

    def test_publish_report_ignores_non_successful_render_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = write_report(root, [game_entry(root, status="failed")])

            def uploader(*_args):
                raise AssertionError("uploader should not run")

            report = publish_night_report(report_path, root / "client.json", root / "token.json", uploader=uploader)

            self.assertEqual(report["summary"], {"success": 0, "skipped": 1, "failed": 0})
            self.assertEqual(report["games"][0]["skip_reason"], "render_not_successful")

    def test_publish_report_writes_failure_state_on_upload_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_path = write_report(root, [game_entry(root)])

            def uploader(*_args):
                raise RuntimeError("upload failed")

            report = publish_night_report(report_path, root / "client.json", root / "token.json", uploader=uploader)

            self.assertEqual(report["summary"], {"success": 0, "skipped": 0, "failed": 1})
            status = json.loads((root / "2026-01-02" / "001" / "youtube_status.json").read_text())
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], "upload failed")

    def test_render_night_publish_youtube_calls_publisher_after_render(self) -> None:
        args = Namespace(
            target_date="2026-01-02",
            output_root=Path("C:/data/nba-play-recap/outputs/nightly"),
            force=False,
            dry_run=False,
            ffmpeg_binary="ffmpeg",
            keep_clips=False,
            max_workers=1,
            max_events=None,
            request_retries=3,
            retry_backoff_seconds=1.5,
            request_timeout_seconds=12,
            no_prune_overlap=False,
            prune_pre_buffer_seconds=2.0,
            prune_post_buffer_seconds=2.0,
            video_session_script=None,
            no_auto_video_session=False,
            headless_session_browser=False,
            video_session_timeout_seconds=45,
            video_browser_channel="chrome",
            retention_days=14,
            publish_youtube=True,
            youtube_client_secrets=Path("C:/data/nba-play-recap/secrets/youtube_client_secret.json"),
            youtube_token=Path("C:/data/nba-play-recap/secrets/youtube_token.json"),
            youtube_privacy="public",
            force_upload=False,
        )
        render_report = {
            "target_date": "2026-01-02",
            "games_discovered": 1,
            "games_renderable": 1,
            "summary": {"success": 1, "skipped": 0, "failed": 0},
            "games": [{"status": "success", "game_id": "001", "matchup": "BOS @ NYK", "skip_reason": None, "error": None}],
        }
        publish_report = {"summary": {"success": 1, "skipped": 0, "failed": 0}, "games": []}

        with patch.object(cli, "NbaStatsClient"), patch.object(cli, "render_night", return_value=render_report) as render_mock, patch.object(
            cli, "publish_night_report", return_value=publish_report
        ) as publish_mock:
            exit_code = cli.run_render_night(args)

        self.assertEqual(exit_code, 0)
        self.assertTrue(render_mock.called)
        publish_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
