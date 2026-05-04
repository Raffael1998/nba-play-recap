from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nba_play_recap.client import NbaStatsClient, NbaStatsError
from nba_play_recap.playbyplay import attach_video_metadata, extract_live_candidate_actions
from nba_play_recap.render import (
    apply_overlap_pruning,
    build_game_manifest,
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
        "--max-workers",
        type=int,
        default=4,
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
        default=10,
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

    manifest = subparsers.add_parser(
        "manifest",
        help="Write per-play manifest files with clip availability, without downloading or rendering clips.",
    )
    manifest.add_argument("--game-id", required=True, help="NBA GameID, for example 0042500151")
    manifest.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Directory for the manifest files.",
    )
    manifest.add_argument(
        "--max-workers",
        type=int,
        default=4,
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
        default=10,
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


def run_manifest(args: argparse.Namespace) -> int:
    client = NbaStatsClient()

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
        )
        if args.no_prune_overlap:
            for candidate in candidates:
                if candidate.video_available and candidate.clip_url:
                    candidate.included_in_render = True
                    candidate.prune_reason = None
        else:
            apply_overlap_pruning(
                candidates,
                pre_buffer_seconds=args.prune_pre_buffer_seconds,
                post_buffer_seconds=args.prune_post_buffer_seconds,
            )
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
