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
    extract_scoreboard_games,
    has_failures,
    render_night,
    resolve_nba_scoreboard_date,
)
from nba_play_recap.browser import DEFAULT_CHROMIUM_PATH, NbaBrowser, NbaBrowserError
from nba_play_recap.playbyplay import attach_video_metadata, extract_candidate_actions
from nba_play_recap.progress import ProgressReporter
from nba_play_recap.render import (
    EXCLUDED_ACTION_TYPES,
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


def _add_browser_arguments(parser: argparse.ArgumentParser) -> None:
    """Options every command shares, because every command drives a browser (D-036)."""
    parser.add_argument(
        "--chromium-path",
        default=DEFAULT_CHROMIUM_PATH,
        help=(
            "Absolute path to the Chromium binary. Defaults to Debian's own "
            f"{DEFAULT_CHROMIUM_PATH}, which keeps a vendor apt repo and a Playwright "
            "browser bundle off the machine. The browser runs HEADED, so a headless "
            "host must invoke this tool under `xvfb-run -a`."
        ),
    )
    parser.add_argument(
        "--asset-batch-size",
        type=int,
        default=4,
        help="How many clip-metadata requests to issue concurrently from inside the page.",
    )
    parser.add_argument(
        "--clip-timeout-seconds",
        type=int,
        default=120,
        help="Maximum wait for one clip's bytes to arrive.",
    )


def _add_prune_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-prune-overlap",
        action="store_true",
        help="Keep all available clips even when their estimated live windows are covered by neighbours.",
    )
    parser.add_argument(
        "--prune-pre-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the start of a clip when estimating unique live coverage.",
    )
    parser.add_argument(
        "--prune-post-buffer-seconds",
        type=float,
        default=2.0,
        help="Assumed replay or setup time at the end of a clip when estimating unique live coverage.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nba-recap",
        description=(
            "Build NBA recap videos from official per-play clips. Every request goes "
            "through a real browser, because that is the only client nba.com serves."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidates = subparsers.add_parser(
        "candidates",
        help="List a game's events that report an available clip.",
    )
    candidates.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500402")
    candidates.add_argument(
        "--json",
        action="store_true",
        help="Emit candidate events as formatted JSON instead of a text table.",
    )
    candidates.add_argument(
        "--max-events",
        type=int,
        default=60,
        help="Maximum number of timeline events to resolve clip metadata for.",
    )
    candidates.add_argument(
        "--exclude-action-types",
        default=",".join(sorted(EXCLUDED_ACTION_TYPES)),
        help=(
            "Comma-separated timeline action types to skip. Everything else is kept, "
            "including blocks and steals, which carry an empty action type."
        ),
    )
    _add_browser_arguments(candidates)

    render = subparsers.add_parser(
        "render-full-game",
        help="Stitch every available event clip of one game into a chronological video.",
    )
    render.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500402")
    render.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Directory for the final video and manifests.",
    )
    render.add_argument("--ffmpeg-binary", default="ffmpeg", help="ffmpeg executable name or full path.")
    render.add_argument(
        "--keep-clips",
        action="store_true",
        help="Keep downloaded clip files after the final video is rendered.",
    )
    render.add_argument(
        "--max-events",
        type=int,
        help="Optional limit on play events processed, mainly for testing.",
    )
    render.add_argument(
        "--clip-retries",
        type=int,
        default=2,
        help="Attempts per clip before it is dropped from the render.",
    )
    _add_prune_arguments(render)
    _add_browser_arguments(render)

    render_night_parser = subparsers.add_parser(
        "render-night",
        help="Render every completed NBA game from one date.",
    )
    render_night_parser.add_argument(
        "--date",
        dest="target_date",
        help=(
            "NBA date in YYYY-MM-DD. Defaults to yesterday in America/New_York, which "
            "matches a morning Europe/Paris scheduled run."
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
        help="Report what would be rendered without writing outputs.",
    )
    render_night_parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="Delete nightly output folders older than this many days after a successful run.",
    )
    render_night_parser.add_argument(
        "--ffmpeg-binary", default="ffmpeg", help="ffmpeg executable name or full path."
    )
    render_night_parser.add_argument(
        "--keep-clips",
        action="store_true",
        help="Keep downloaded clip files after each final video is rendered.",
    )
    render_night_parser.add_argument(
        "--max-events",
        type=int,
        help="Optional limit on play events per game, mainly for testing.",
    )
    render_night_parser.add_argument(
        "--clip-retries",
        type=int,
        default=2,
        help="Attempts per clip before it is dropped from the render.",
    )
    _add_prune_arguments(render_night_parser)
    _add_browser_arguments(render_night_parser)

    manifest = subparsers.add_parser(
        "manifest",
        help="Write per-play manifest files without rendering the final video.",
    )
    manifest.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500402")
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
        "--max-events",
        type=int,
        help="Optional limit on play events processed, mainly for testing.",
    )
    manifest.add_argument(
        "--clip-retries",
        type=int,
        default=2,
        help="Attempts per clip before it is dropped.",
    )
    _add_prune_arguments(manifest)
    _add_browser_arguments(manifest)

    game_id_parser = subparsers.add_parser(
        "game-id",
        help="Resolve a no-spoiler GameID for a team on a given date.",
    )
    game_id_parser.add_argument("--team", required=True, help="Team tricode, for example SAS.")
    date_group = game_id_parser.add_mutually_exclusive_group()
    date_group.add_argument("--date", dest="target_date", help="Game date in YYYY-MM-DD.")
    date_group.add_argument(
        "--yesterday",
        action="store_true",
        help="Use yesterday's date in local time.",
    )
    _add_browser_arguments(game_id_parser)
    return parser


def _open_browser(args: argparse.Namespace) -> NbaBrowser:
    return NbaBrowser(
        executable_path=args.chromium_path,
        asset_batch_size=args.asset_batch_size,
        clip_timeout_seconds=args.clip_timeout_seconds,
    )


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
    excluded = {value.strip() for value in args.exclude_action_types.split(",") if value.strip()}

    try:
        with _open_browser(args) as browser:
            actions = browser.open_game(args.game_id)
            candidates = extract_candidate_actions(actions, args.game_id, excluded)
            candidates = candidates[: max(args.max_events, 0)]
            payloads = browser.video_event_assets(
                args.game_id, [candidate.event_num for candidate in candidates]
            )
    except (NbaBrowserError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results = []
    for candidate in candidates:
        payload = payloads.get(candidate.event_num) or {}
        if payload.get("error"):
            continue
        attach_video_metadata(candidate, payload)
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
    target_date = _resolve_lookup_date(args)
    team = args.team.strip().upper()

    try:
        with _open_browser(args) as browser:
            games = extract_scoreboard_games(browser.scheduled_games(target_date), target_date)
    except (NbaBrowserError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    matches = [game for game in games if team in {game.home_tricode, game.away_tricode}]

    if not matches:
        print(f"error: no game found for team {team} on {target_date}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: multiple games found for team {team} on {target_date}.", file=sys.stderr)
        for match in matches:
            print(f"{match.game_id}  {match.matchup}", file=sys.stderr)
        return 1

    match = matches[0]
    print(f"GameID: {match.game_id}")
    print(f"Matchup: {match.matchup}")
    print(f"Date: {target_date}")
    return 0


def _resolve_lookup_date(args: argparse.Namespace) -> str:
    if args.target_date:
        return args.target_date
    if args.yesterday:
        return (date.today() - timedelta(days=1)).isoformat()
    return date.today().isoformat()


def run_render_full_game(args: argparse.Namespace) -> int:
    try:
        with _open_browser(args) as browser:
            outputs = render_full_game(
                browser=browser,
                game_id=args.game_id,
                output_dir=args.output_dir,
                ffmpeg_binary=args.ffmpeg_binary,
                keep_clips=args.keep_clips,
                max_events=args.max_events,
                clip_retries=args.clip_retries,
                prune_overlap=not args.no_prune_overlap,
                prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
                prune_post_buffer_seconds=args.prune_post_buffer_seconds,
            )
    except (NbaBrowserError, ValueError, RuntimeError) as exc:
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
            max_events=args.max_events,
            clip_retries=args.clip_retries,
            prune_overlap=not args.no_prune_overlap,
            prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
            prune_post_buffer_seconds=args.prune_post_buffer_seconds,
            retention_days=args.retention_days,
        )
        with _open_browser(args) as browser:
            report = render_night(browser, options)
    except (NbaBrowserError, ValueError, RuntimeError) as exc:
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
    if has_failures(report):
        return 1
    return 0


def run_manifest(args: argparse.Namespace) -> int:
    progress = ProgressReporter(enabled=sys.stderr.isatty())

    try:
        with _open_browser(args) as browser:
            candidates, debug_stats = build_game_manifest(
                browser,
                args.game_id,
                cache_dir=args.output_dir / "cache" / "videoevents",
                max_events=args.max_events,
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
            clip_paths_by_event = download_available_clips(
                browser, candidates, clip_dir, progress=progress, retries=args.clip_retries
            )
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
        manifest_txt_path, manifest_json_path = write_manifests(
            candidates, args.output_dir, args.game_id
        )
        debug_json_path = write_debug_report(
            candidates=candidates,
            output_dir=args.output_dir,
            game_id=args.game_id,
            debug_stats=debug_stats,
            clip_retries=args.clip_retries,
            prune_overlap=not args.no_prune_overlap,
            prune_pre_buffer_seconds=args.prune_pre_buffer_seconds,
            prune_post_buffer_seconds=args.prune_post_buffer_seconds,
        )
    except (NbaBrowserError, ValueError, RuntimeError) as exc:
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
