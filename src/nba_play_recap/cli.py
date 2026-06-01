from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from nba_play_recap.batch import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_RETENTION_DAYS,
    RenderNightOptions,
    has_failures,
    render_night,
    resolve_nba_scoreboard_date,
)
from nba_play_recap.client import NbaStatsClient, NbaStatsError
from nba_play_recap.playbyplay import attach_video_metadata, extract_live_candidate_actions
from nba_play_recap.progress import ProgressReporter
from nba_play_recap.render import (
    apply_clip_fingerprint_pruning,
    apply_final_overlap_pruning,
    apply_overlap_pruning,
    build_game_manifest,
    cleanup_clips,
    download_available_clips,
    mark_all_available_clips_included,
    render_full_game,
    write_debug_report,
    write_manifests,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nba-recap",
        description="Inspect NBA play-by-play data and identify events with video.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidates = subparsers.add_parser(
        "candidates",
        help="Fetch play-by-play and list events that report video availability.",
    )
    candidates.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500151")
    candidates.add_argument(
        "--save-raw",
        type=Path,
        help="Optional output path for the raw live play-by-play JSON response.",
    )
    candidates.add_argument(
        "--json",
        action="store_true",
        help="Emit candidate events as formatted JSON instead of a text table.",
    )
    candidates.add_argument(
        "--max-events",
        type=int,
        default=60,
        help="Maximum number of timeline events to probe for clip availability.",
    )
    candidates.add_argument(
        "--action-types",
        default="2pt,3pt,freethrow,block,steal,turnover,foul,jumpball,rebound",
        help="Comma-separated action types to consider from the live play-by-play feed.",
    )

    render = subparsers.add_parser(
        "render-full-game",
        help="Create a chronological stitched video from all available event clips and write a play manifest.",
    )
    render.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500151")
    render.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Directory for the final video and manifests.",
    )
    render.add_argument(
        "--ffmpeg-binary",
        default="ffmpeg",
        help="ffmpeg executable name or full path.",
    )
    render.add_argument(
        "--keep-clips",
        action="store_true",
        help="Keep downloaded clip files after the final video is rendered.",
    )
    render.add_argument(
        "--video-session-script",
        type=Path,
        help=(
            "Fallback PowerShell script copied from browser DevTools for a working NBA video request. "
            "By default a fresh session is acquired automatically through Chromium."
        ),
    )
    render.add_argument(
        "--no-auto-video-session",
        action="store_true",
        help="Disable automatic Chromium session acquisition and rely on --video-session-script or direct requests.",
    )
    render.add_argument(
        "--headless-session-browser",
        action="store_true",
        help=(
            "Run session acquisition in headless mode. NBA currently rejects this mode in testing; "
            "use only to re-test compatibility in another environment."
        ),
    )
    render.add_argument(
        "--video-session-timeout-seconds",
        type=int,
        default=45,
        help="Maximum time to wait for Chromium to capture a valid NBA video request.",
    )
    render.add_argument(
        "--video-browser-channel",
        default="chrome",
        help="Playwright browser channel used for automatic session acquisition (default: chrome).",
    )
    render.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum concurrent clip metadata requests.",
    )
    render.add_argument(
        "--max-events",
        type=int,
        help="Optional limit for the number of play events to process, mainly for testing.",
    )
    render.add_argument(
        "--request-retries",
        type=int,
        default=3,
        help="Maximum retries for each clip metadata request.",
    )
    render.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=1.5,
        help="Base backoff delay between clip metadata retries.",
    )
    render.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=12,
        help="Timeout for each clip metadata request.",
    )
    render.add_argument(
        "--no-prune-overlap",
        action="store_true",
        help="Keep all available clips even if their estimated live windows are covered by neighboring clips.",
    )
    render.add_argument(
        "--prune-pre-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the start of a clip when estimating unique live coverage.",
    )
    render.add_argument(
        "--prune-post-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the end of a clip when estimating unique live coverage.",
    )

    render_night_parser = subparsers.add_parser(
        "render-night",
        help="Render every completed NBA game from one scoreboard date.",
    )
    render_night_parser.add_argument(
        "--date",
        dest="target_date",
        help=(
            "NBA scoreboard date in YYYY-MM-DD. Defaults to yesterday in America/New_York, "
            "which matches a morning Europe/Paris scheduled run."
        ),
    )
    render_night_parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root directory for nightly outputs.",
    )
    render_night_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render games even when a prior successful output exists.",
    )
    render_night_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch the scoreboard and report what would be rendered without writing outputs.",
    )
    render_night_parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="Delete nightly output folders older than this many days after a successful batch run.",
    )
    render_night_parser.add_argument(
        "--ffmpeg-binary",
        default="ffmpeg",
        help="ffmpeg executable name or full path.",
    )
    render_night_parser.add_argument(
        "--keep-clips",
        action="store_true",
        help="Keep downloaded clip files after each final video is rendered.",
    )
    render_night_parser.add_argument(
        "--video-session-script",
        type=Path,
        help="Fallback PowerShell script copied from browser DevTools for a working NBA video request.",
    )
    render_night_parser.add_argument(
        "--no-auto-video-session",
        action="store_true",
        help="Disable automatic Chromium session acquisition and rely on --video-session-script or direct requests.",
    )
    render_night_parser.add_argument(
        "--headless-session-browser",
        action="store_true",
        help="Run session acquisition in headless mode. Not recommended for scheduled VM use.",
    )
    render_night_parser.add_argument(
        "--video-session-timeout-seconds",
        type=int,
        default=45,
        help="Maximum time to wait for Chromium to capture a valid NBA video request.",
    )
    render_night_parser.add_argument(
        "--video-browser-channel",
        default="chrome",
        help="Playwright browser channel used for automatic session acquisition.",
    )
    render_night_parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum concurrent clip metadata requests per game.",
    )
    render_night_parser.add_argument(
        "--max-events",
        type=int,
        help="Optional limit for play events per game, mainly for testing.",
    )
    render_night_parser.add_argument(
        "--request-retries",
        type=int,
        default=3,
        help="Maximum retries for each clip metadata request.",
    )
    render_night_parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=1.5,
        help="Base backoff delay between clip metadata retries.",
    )
    render_night_parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=12,
        help="Timeout for each clip metadata request.",
    )
    render_night_parser.add_argument(
        "--no-prune-overlap",
        action="store_true",
        help="Keep all available clips even if their estimated live windows overlap heavily.",
    )
    render_night_parser.add_argument(
        "--prune-pre-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the start of a clip.",
    )
    render_night_parser.add_argument(
        "--prune-post-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the end of a clip.",
    )

    manifest = subparsers.add_parser(
        "manifest",
        help="Write per-play manifest files with clip availability and fingerprint-based de-duplication, without rendering the final video.",
    )
    manifest.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500151")
    manifest.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Directory for the manifest files.",
    )
    manifest.add_argument(
        "--ffmpeg-binary",
        default="ffmpeg",
        help="ffmpeg executable name or full path. Used for clip fingerprint de-duplication.",
    )
    manifest.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum concurrent clip metadata requests.",
    )
    manifest.add_argument(
        "--max-events",
        type=int,
        help="Optional limit for the number of play events to process, mainly for testing.",
    )
    manifest.add_argument(
        "--request-retries",
        type=int,
        default=3,
        help="Maximum retries for each clip metadata request.",
    )
    manifest.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=1.5,
        help="Base backoff delay between clip metadata retries.",
    )
    manifest.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=12,
        help="Timeout for each clip metadata request.",
    )
    manifest.add_argument(
        "--no-prune-overlap",
        action="store_true",
        help="Keep all available clips in the manifest output even if their estimated live windows overlap heavily.",
    )
    manifest.add_argument(
        "--prune-pre-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the start of a clip when estimating unique live coverage.",
    )
    manifest.add_argument(
        "--prune-post-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the end of a clip when estimating unique live coverage.",
    )

    game_id_parser = subparsers.add_parser(
        "game-id",
        help="Resolve a no-spoiler GameID for a team on a given date.",
    )
    game_id_parser.add_argument(
        "--team",
        required=True,
        help="Team tricode, for example SAS.",
    )
    date_group = game_id_parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date",
        dest="target_date",
        help="Game date in YYYY-MM-DD.",
    )
    date_group.add_argument(
        "--yesterday",
        action="store_true",
        help="Use yesterday's date in local time.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "candidates":
        return run_candidates(args)
    if args.command == "manifest":
        return run_manifest(args)
    if args.command == "render-full-game":
        return run_render_full_game(args)
    if args.command == "render-night":
        return run_render_night(args)
    if args.command == "game-id":
        return run_game_id_lookup(args)

    parser.error(f"Unsupported command: {args.command}")
    return 2


def run_candidates(args: argparse.Namespace) -> int:
    client = NbaStatsClient()

    try:
        payload = client.get_live_playbyplay(args.game_id)
        if args.save_raw:
            client.save_json(payload, args.save_raw)
        action_types = {
            value.strip()
            for value in args.action_types.split(",")
            if value.strip()
        }
        candidates = extract_live_candidate_actions(payload, args.game_id, action_types)
    except (NbaStatsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    candidates = candidates[: max(args.max_events, 0)]
    results = []
    for candidate in candidates:
        try:
            video_payload = client.get_video_event_asset(args.game_id, candidate.event_num)
        except NbaStatsError:
            continue
        attach_video_metadata(candidate, video_payload)
        if candidate.video_available:
            results.append(candidate)

    if args.json:
        print(json.dumps([candidate.to_dict() for candidate in results], indent=2))
        return 0

    print(f"GameID: {args.game_id}")
    print(f"Probed timeline events: {len(candidates)}")
    print(f"Candidate events with video: {len(results)}")
    print("")
    for candidate in results:
        score = _format_score(candidate.visitor_score, candidate.home_score)
        clock = candidate.clock or "--:--"
        period = f"Q{candidate.period}" if candidate.period else "Q?"
        action_type = candidate.action_type or "event"
        print(
            f"{candidate.event_num:>4}  {period:<3}  {clock:<5}  "
            f"{score:<9}  {action_type:<10}  {candidate.description}"
        )
    return 0


def _format_score(visitor_score: str | None, home_score: str | None) -> str:
    if visitor_score is None or home_score is None:
        return "-"
    return f"{visitor_score}-{home_score}"


def run_game_id_lookup(args: argparse.Namespace) -> int:
    client = NbaStatsClient()
    target_date = _resolve_lookup_date(args)
    team = args.team.strip().upper()

    try:
        payload = client.get_scoreboard_v3(target_date)
    except (NbaStatsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    games = payload.get("scoreboard", {}).get("games", [])
    if not isinstance(games, list):
        print("error: scoreboard response did not contain a games list.", file=sys.stderr)
        return 1

    matches = []
    for game in games:
        if not isinstance(game, dict):
            continue
        home_team = game.get("homeTeam", {})
        away_team = game.get("awayTeam", {})
        home_tricode = str(home_team.get("teamTricode", "")).upper()
        away_tricode = str(away_team.get("teamTricode", "")).upper()
        if team not in {home_tricode, away_tricode}:
            continue
        matches.append(
            {
                "game_id": str(game.get("gameId", "")),
                "away_tricode": away_tricode,
                "home_tricode": home_tricode,
            }
        )

    if not matches:
        print(f"error: no game found for team {team} on {target_date}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: multiple games found for team {team} on {target_date}.", file=sys.stderr)
        for match in matches:
            print(
                f"{match['game_id']}  {match['away_tricode']} @ {match['home_tricode']}",
                file=sys.stderr,
            )
        return 1

    match = matches[0]
    print(f"GameID: {match['game_id']}")
    print(f"Matchup: {match['away_tricode']} @ {match['home_tricode']}")
    print(f"Date: {target_date}")
    return 0


def _resolve_lookup_date(args: argparse.Namespace) -> str:
    if args.target_date:
        return args.target_date
    if args.yesterday:
        return (date.today() - timedelta(days=1)).isoformat()
    return date.today().isoformat()


def run_render_full_game(args: argparse.Namespace) -> int:
    client = NbaStatsClient()

    try:
        outputs = render_full_game(
            client=client,
            game_id=args.game_id,
            output_dir=args.output_dir,
            ffmpeg_binary=args.ffmpeg_binary,
            keep_clips=args.keep_clips,
            max_workers=args.max_workers,
            max_events=args.max_events,
            request_retries=args.request_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            prune_overlap=not args.no_prune_overlap,
            prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
            prune_post_buffer_seconds=args.prune_post_buffer_seconds,
            video_session_script=args.video_session_script,
            auto_video_session=not args.no_auto_video_session,
            show_session_browser=not args.headless_session_browser,
            video_session_timeout_seconds=args.video_session_timeout_seconds,
            video_browser_channel=args.video_browser_channel,
        )
    except (NbaStatsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"GameID: {args.game_id}")
    print(f"Manifest TXT: {outputs.manifest_txt_path}")
    print(f"Manifest JSON: {outputs.manifest_json_path}")
    print(f"Debug JSON: {outputs.debug_json_path}")
    print(f"Concat list: {outputs.concat_list_path}")
    print(f"Total timeline events: {outputs.total_event_count}")
    print(f"Events with available clips: {outputs.available_clip_count}")
    print(f"Clips kept for render: {outputs.rendered_clip_count}")
    print(f"Downloaded clips: {outputs.downloaded_clip_count}")
    print(f"Final video: {outputs.video_path if outputs.video_path else 'not created'}")
    return 0


def run_render_night(args: argparse.Namespace) -> int:
    client = NbaStatsClient()
    try:
        if args.retention_days < 1:
            raise ValueError("--retention-days must be at least 1.")
        target_date = resolve_nba_scoreboard_date(args.target_date)
        options = RenderNightOptions(
            target_date=target_date,
            output_root=args.output_root,
            force=args.force,
            dry_run=args.dry_run,
            ffmpeg_binary=args.ffmpeg_binary,
            keep_clips=args.keep_clips,
            max_workers=args.max_workers,
            max_events=args.max_events,
            request_retries=args.request_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            prune_overlap=not args.no_prune_overlap,
            prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
            prune_post_buffer_seconds=args.prune_post_buffer_seconds,
            video_session_script=args.video_session_script,
            auto_video_session=not args.no_auto_video_session,
            show_session_browser=not args.headless_session_browser,
            video_session_timeout_seconds=args.video_session_timeout_seconds,
            video_browser_channel=args.video_browser_channel,
            retention_days=args.retention_days,
        )
        report = render_night(client, options)
    except (NbaStatsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"NBA date: {report['target_date']}")
    print(f"Games discovered: {report['games_discovered']}")
    print(f"Games renderable: {report['games_renderable']}")
    print(
        "Summary: "
        f"{report['summary']['success']} success, "
        f"{report['summary']['skipped']} skipped, "
        f"{report['summary']['failed']} failed"
    )
    if not args.dry_run:
        report_path = args.output_root / report["target_date"] / "run_report.json"
        print(f"Run report: {report_path}")
    for game in report["games"]:
        suffix = ""
        if game["skip_reason"]:
            suffix = f" ({game['skip_reason']})"
        if game["error"]:
            suffix = f" ({game['error']})"
        print(f"{game['status']}: {game['game_id']} {game['matchup']}{suffix}")
    return 1 if has_failures(report) else 0


def run_manifest(args: argparse.Namespace) -> int:
    client = NbaStatsClient()
    progress = ProgressReporter(enabled=sys.stderr.isatty())

    try:
        candidates, debug_stats = build_game_manifest(
            client,
            args.game_id,
            max_workers=args.max_workers,
            cache_dir=args.output_dir / "cache" / "videoevents",
            max_events=args.max_events,
            request_retries=args.request_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            progress=progress,
        )
        if args.no_prune_overlap:
            mark_all_available_clips_included(candidates)
        else:
            apply_overlap_pruning(
                candidates,
                pre_buffer_seconds=args.prune_pre_buffer_seconds,
                post_buffer_seconds=args.prune_post_buffer_seconds,
            )
        clip_dir = args.output_dir / f"{args.game_id}_manifest_clips"
        clip_paths_by_event = download_available_clips(candidates, clip_dir, progress=progress)
        try:
            apply_clip_fingerprint_pruning(
                candidates,
                clip_paths_by_event,
                ffmpeg_binary=args.ffmpeg_binary,
                progress=progress,
            )
            if not args.no_prune_overlap:
                apply_final_overlap_pruning(candidates)
        finally:
            cleanup_clips(clip_dir)
        manifest_txt_path, manifest_json_path = write_manifests(candidates, args.output_dir, args.game_id)
        debug_json_path = write_debug_report(
            candidates=candidates,
            output_dir=args.output_dir,
            game_id=args.game_id,
            debug_stats=debug_stats,
            max_workers=args.max_workers,
            request_retries=args.request_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            prune_overlap=not args.no_prune_overlap,
            prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
            prune_post_buffer_seconds=args.prune_post_buffer_seconds,
        )
    except (NbaStatsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    available_clip_count = sum(1 for candidate in candidates if candidate.video_available)
    rendered_clip_count = sum(1 for candidate in candidates if candidate.included_in_render)
    print(f"GameID: {args.game_id}")
    print(f"Manifest TXT: {manifest_txt_path}")
    print(f"Manifest JSON: {manifest_json_path}")
    print(f"Debug JSON: {debug_json_path}")
    print(f"Total timeline events: {len(candidates)}")
    print(f"Events with available clips: {available_clip_count}")
    print(f"Clips kept for render: {rendered_clip_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
