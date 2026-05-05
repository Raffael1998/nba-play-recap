from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nba_play_recap.client import DEFAULT_HEADERS, NbaStatsClient, NbaStatsError
from nba_play_recap.playbyplay import CandidatePlay, attach_video_metadata, extract_live_candidate_actions

DEFAULT_PLAY_ACTION_TYPES = {
    "2pt",
    "3pt",
    "block",
    "foul",
    "freethrow",
    "jumpball",
    "rebound",
    "steal",
    "turnover",
    "violation",
}


VIDEO_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://www.nba.com/",
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
}


@dataclass(slots=True)
class RenderOutputs:
    manifest_txt_path: Path
    manifest_json_path: Path
    debug_json_path: Path
    concat_list_path: Path
    video_path: Path | None
    downloaded_clip_count: int
    available_clip_count: int
    rendered_clip_count: int
    total_event_count: int


@dataclass(slots=True)
class FetchDebugStats:
    cache_hits: int = 0
    cache_misses: int = 0
    fetch_successes: int = 0
    fetch_missing: int = 0
    fetch_failures: int = 0
    retries: int = 0
    events_probed: int = 0
    video_probe_seconds: float = 0.0
    download_seconds: float = 0.0
    render_seconds: float = 0.0
    total_seconds: float = 0.0


def build_game_manifest(
    client: NbaStatsClient,
    game_id: str,    
    max_workers: int = 4,
    action_types: set[str] | None = None,
    cache_dir: Path | None = None,
    max_events: int | None = None,
    request_retries: int = 3,
    retry_backoff_seconds: float = 1.5,
    request_timeout_seconds: int = 10,
) -> tuple[list[CandidatePlay], FetchDebugStats]:
    debug_stats = FetchDebugStats()
    probe_started_at = time.perf_counter()
    payload = client.get_live_playbyplay(game_id)
    candidates = extract_live_candidate_actions(
        payload,
        game_id,
        include_action_types=action_types or DEFAULT_PLAY_ACTION_TYPES,
    )
    if max_events is not None:
        candidates = candidates[:max_events]

    unresolved_candidates: list[CandidatePlay] = []
    for candidate in candidates:
        cached_payload = _read_cached_video_payload(cache_dir, game_id, candidate.event_num)
        if cached_payload is not None:
            debug_stats.cache_hits += 1
            attach_video_metadata(candidate, cached_payload)
        else:
            debug_stats.cache_misses += 1
            unresolved_candidates.append(candidate)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_candidate = {
            executor.submit(
                _fetch_video_event_asset_with_retry,
                client,
                game_id,
                candidate.event_num,
                request_retries,
                retry_backoff_seconds,
                request_timeout_seconds,
            ): candidate
            for candidate in unresolved_candidates
        }
        for future in as_completed(future_to_candidate):
            candidate = future_to_candidate[future]
            try:
                fetch_result = future.result()
            except Exception as exc:
                candidate.video_available = False
                candidate.availability_status = "error"
                candidate.availability_error = str(exc)
                debug_stats.fetch_failures += 1
                continue
            debug_stats.events_probed += 1
            debug_stats.retries += max(fetch_result["attempts"] - 1, 0)
            if fetch_result["payload"] is None:
                candidate.video_available = False
                candidate.availability_status = "error"
                candidate.availability_error = fetch_result["error"]
                debug_stats.fetch_failures += 1
                continue
            _write_cached_video_payload(cache_dir, game_id, candidate.event_num, fetch_result["payload"])
            attach_video_metadata(candidate, fetch_result["payload"])
            if candidate.video_available:
                debug_stats.fetch_successes += 1
            else:
                debug_stats.fetch_missing += 1
    debug_stats.video_probe_seconds = round(time.perf_counter() - probe_started_at, 3)
    return candidates, debug_stats


def write_manifests(candidates: list[CandidatePlay], output_dir: Path, game_id: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_txt_path = output_dir / f"{game_id}_manifest.txt"
    manifest_json_path = output_dir / f"{game_id}_manifest.json"

    text_lines = [
        "event_num\tperiod\tclock\tteam\taction_type\tvideo_available\tavailability_status\tincluded_in_render\tclip_duration_seconds\testimated_clock_start\testimated_clock_end\tdescription\tclip_url"
    ]
    for candidate in candidates:
        text_lines.append(
            "\t".join(
                [
                    str(candidate.event_num),
                    str(candidate.period or ""),
                    candidate.clock or "",
                    candidate.team_tricode or "",
                    candidate.action_type or "",
                    "yes" if candidate.video_available else "no",
                    candidate.availability_status,
                    "yes" if candidate.included_in_render else "no",
                    "" if candidate.clip_duration_seconds is None else f"{candidate.clip_duration_seconds:.3f}",
                    candidate.estimated_clock_start or "",
                    candidate.estimated_clock_end or "",
                    _sanitize_field(candidate.description),
                    candidate.clip_url or "",
                ]
            )
        )

    manifest_txt_path.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    manifest_json_path.write_text(
        _json_dump(candidates),
        encoding="utf-8",
    )
    return manifest_txt_path, manifest_json_path


def download_available_clips(candidates: list[CandidatePlay], clip_dir: Path) -> dict[int, Path]:
    clip_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths: dict[int, Path] = {}
    for candidate in candidates:
        if not candidate.video_available or not candidate.clip_url or not candidate.included_in_render:
            continue
        clip_path = clip_dir / f"{candidate.event_num:05d}.mp4"
        if not clip_path.exists():
            _download_file(candidate.clip_url, clip_path)
        downloaded_paths[candidate.event_num] = clip_path
    return downloaded_paths


def write_concat_list(clip_paths: list[Path], output_dir: Path, game_id: str) -> Path:
    concat_list_path = output_dir / f"{game_id}_concat.txt"
    lines = [f"file '{path.resolve().as_posix()}'" for path in clip_paths]
    concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_list_path


def render_concat_video(concat_list_path: Path, output_path: Path, ffmpeg_binary: str = "ffmpeg") -> None:
    ffmpeg_executable = resolve_ffmpeg_binary(ffmpeg_binary)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_executable,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return

    fallback_command = [
        ffmpeg_executable,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(output_path),
    ]
    fallback = subprocess.run(fallback_command, capture_output=True, text=True)
    if fallback.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed to render the final video.\n"
            f"Copy attempt stderr:\n{result.stderr}\n\nRe-encode attempt stderr:\n{fallback.stderr}"
        )


def cleanup_clips(clip_dir: Path) -> None:
    if clip_dir.exists():
        shutil.rmtree(clip_dir)


def render_full_game(
    client: NbaStatsClient,
    game_id: str,
    output_dir: Path,
    ffmpeg_binary: str,
    keep_clips: bool,
    max_workers: int,
    max_events: int | None,
    request_retries: int,
    retry_backoff_seconds: float,
    request_timeout_seconds: int,
    prune_overlap: bool,
    prune_pre_buffer_seconds: float,
    prune_post_buffer_seconds: float,
) -> RenderOutputs:
    started_at = time.perf_counter()
    ensure_ffmpeg_available(ffmpeg_binary)
    candidates, debug_stats = build_game_manifest(
        client,
        game_id,
        max_workers=max_workers,
        cache_dir=output_dir / "cache" / "videoevents",
        max_events=max_events,
        request_retries=request_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )
    if prune_overlap:
        apply_overlap_pruning(
            candidates,
            pre_buffer_seconds=prune_pre_buffer_seconds,
            post_buffer_seconds=prune_post_buffer_seconds,
        )
    else:
        mark_all_available_clips_included(candidates)
    available_candidates = [candidate for candidate in candidates if candidate.video_available and candidate.clip_url]
    clip_dir = output_dir / f"{game_id}_clips"
    download_started_at = time.perf_counter()
    clip_paths_by_event = download_available_clips(available_candidates, clip_dir)
    apply_clip_fingerprint_pruning(
        candidates,
        clip_paths_by_event,
        ffmpeg_binary=ffmpeg_binary,
    )
    debug_stats.download_seconds = round(time.perf_counter() - download_started_at, 3)
    manifest_txt_path, manifest_json_path = write_manifests(candidates, output_dir, game_id)
    rendered_candidates = [candidate for candidate in available_candidates if candidate.included_in_render]
    clip_paths = [
        clip_paths_by_event[candidate.event_num]
        for candidate in sorted(rendered_candidates, key=lambda candidate: (candidate.period or 0, candidate.event_num))
        if candidate.event_num in clip_paths_by_event
    ]
    concat_list_path = write_concat_list(clip_paths, output_dir, game_id)

    video_path: Path | None = None
    if clip_paths:
        video_path = output_dir / f"{game_id}_full_game.mp4"
        render_started_at = time.perf_counter()
        render_concat_video(concat_list_path, video_path, ffmpeg_binary=ffmpeg_binary)
        debug_stats.render_seconds = round(time.perf_counter() - render_started_at, 3)

    if not keep_clips:
        cleanup_clips(clip_dir)

    debug_stats.total_seconds = round(time.perf_counter() - started_at, 3)
    debug_json_path = write_debug_report(
        candidates=candidates,
        output_dir=output_dir,
        game_id=game_id,
        debug_stats=debug_stats,
        max_workers=max_workers,
        request_retries=request_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        request_timeout_seconds=request_timeout_seconds,
        prune_overlap=prune_overlap,
        prune_pre_buffer_seconds=prune_pre_buffer_seconds,
        prune_post_buffer_seconds=prune_post_buffer_seconds,
    )

    return RenderOutputs(
        manifest_txt_path=manifest_txt_path,
        manifest_json_path=manifest_json_path,
        debug_json_path=debug_json_path,
        concat_list_path=concat_list_path,
        video_path=video_path,
        downloaded_clip_count=len(clip_paths),
        available_clip_count=len(available_candidates),
        rendered_clip_count=len(rendered_candidates),
        total_event_count=len(candidates),
    )


def ensure_ffmpeg_available(ffmpeg_binary: str) -> None:
    resolve_ffmpeg_binary(ffmpeg_binary)


def resolve_ffmpeg_binary(ffmpeg_binary: str) -> str:
    if Path(ffmpeg_binary).name == ffmpeg_binary:
        resolved = shutil.which(ffmpeg_binary)
        if resolved:
            return resolved
    elif Path(ffmpeg_binary).exists():
        return str(Path(ffmpeg_binary))
    raise RuntimeError(
        "ffmpeg was not found. Install ffmpeg or pass --ffmpeg-binary with the full executable path."
    )


def _download_file(url: str, destination: Path) -> None:
    request = Request(url, headers=VIDEO_HEADERS)
    try:
        with urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Failed to download clip: {url}") from exc


def _json_dump(candidates: list[CandidatePlay]) -> str:
    return json.dumps([candidate.to_dict() for candidate in candidates], indent=2)


def _sanitize_field(value: str) -> str:
    return value.replace("\t", " ").replace("\n", " ").strip()


def _read_cached_video_payload(cache_dir: Path | None, game_id: str, event_num: int) -> dict | None:
    if cache_dir is None:
        return None
    cache_path = cache_dir / game_id / f"{event_num:05d}.json"
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_cached_video_payload(cache_dir: Path | None, game_id: str, event_num: int, payload: dict) -> None:
    if cache_dir is None:
        return
    cache_path = cache_dir / game_id / f"{event_num:05d}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _fetch_video_event_asset_with_retry(
    client: NbaStatsClient,
    game_id: str,
    event_num: int,
    request_retries: int,
    retry_backoff_seconds: float,
    request_timeout_seconds: int,
) -> dict[str, object]:
    last_error: str | None = None
    attempts = max(request_retries, 1)
    for attempt in range(1, attempts + 1):
        try:
            payload = client.get_video_event_asset(
                game_id,
                event_num,
                timeout_seconds=request_timeout_seconds,
            )
            if not _is_valid_video_payload(payload):
                raise NbaStatsError(
                    f"NBA videoeventsasset payload was empty or invalid for event {event_num}."
                )
            return {
                "payload": payload,
                "attempts": attempt,
                "error": None,
            }
        except NbaStatsError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(retry_backoff_seconds * attempt)
    return {
        "payload": None,
        "attempts": attempts,
        "error": last_error or "Unknown fetch error",
    }


def apply_overlap_pruning(
    candidates: list[CandidatePlay],
    pre_buffer_seconds: float,
    post_buffer_seconds: float,
) -> None:
    for candidate in candidates:
        candidate.included_in_render = False
        candidate.prune_reason = None
        interval = estimate_live_interval(candidate, pre_buffer_seconds, post_buffer_seconds)
        if interval is None:
            continue
        candidate.estimated_interval_start = round(interval[0], 3)
        candidate.estimated_interval_end = round(interval[1], 3)
        candidate.estimated_clock_start = _format_game_clock_from_elapsed(
            candidate.period,
            candidate.estimated_interval_start,
        )
        candidate.estimated_clock_end = _format_game_clock_from_elapsed(
            candidate.period,
            candidate.estimated_interval_end,
        )
        if candidate.action_type == "freethrow" and candidate.video_available and candidate.clip_url:
            candidate.included_in_render = True
            candidate.prune_reason = None

    available_candidates = [
        candidate
        for candidate in candidates
        if candidate.video_available
        and candidate.clip_url
        and candidate.estimated_interval_start is not None
        and candidate.estimated_interval_end is not None
        and candidate.action_type != "freethrow"
    ]
    grouped: dict[int, list[CandidatePlay]] = {}
    for candidate in available_candidates:
        if candidate.period is None:
            candidate.included_in_render = True
            continue
        grouped.setdefault(candidate.period, []).append(candidate)

    for period_candidates in grouped.values():
        _apply_global_coverage_pruning(period_candidates)

    for candidate in candidates:
        if candidate.video_available and candidate.clip_url and candidate.estimated_interval_start is None:
            candidate.included_in_render = True
        if candidate.action_type == "freethrow" and candidate.video_available and candidate.clip_url:
            candidate.included_in_render = True
            candidate.prune_reason = None


def estimate_live_interval(
    candidate: CandidatePlay,
    pre_buffer_seconds: float,
    post_buffer_seconds: float,
) -> tuple[float, float] | None:
    if candidate.period is None or candidate.clock_seconds_remaining is None or candidate.clip_duration_seconds is None:
        return None
    period_length_seconds = 300.0 if candidate.period > 4 else 720.0
    elapsed_at_event = period_length_seconds - candidate.clock_seconds_remaining
    pre_play_seconds = candidate.clip_duration_seconds * 0.8
    post_play_seconds = candidate.clip_duration_seconds * 0.2
    effective_pre_play_seconds = max(pre_play_seconds - pre_buffer_seconds, 0.0)
    effective_post_play_seconds = max(post_play_seconds - post_buffer_seconds, 0.0)
    interval_start = max(0.0, elapsed_at_event - effective_pre_play_seconds)
    interval_end = min(period_length_seconds, elapsed_at_event + effective_post_play_seconds)
    if interval_end <= interval_start:
        interval_end = min(period_length_seconds, interval_start + 0.5)
    return interval_start, interval_end


def _apply_global_coverage_pruning(period_candidates: list[CandidatePlay]) -> None:
    keepers = [
        candidate
        for candidate in period_candidates
        if _candidate_interval(candidate) is not None
    ]
    for candidate in keepers:
        candidate.included_in_render = True
        candidate.prune_reason = None

    removable_candidates = sorted(
        keepers,
        key=lambda candidate: (
            _interval_length(_candidate_interval(candidate)),
            candidate.event_num,
        ),
    )
    for candidate in removable_candidates:
        others = [other for other in keepers if other.included_in_render and other is not candidate]
        current_interval = _candidate_interval(candidate)
        if current_interval is None:
            continue
        if _is_interval_fully_covered_by_others(current_interval, others):
            candidate.included_in_render = False
            candidate.prune_reason = "covered_by_period_union"


def mark_all_available_clips_included(candidates: list[CandidatePlay]) -> None:
    for candidate in candidates:
        if candidate.video_available and candidate.clip_url:
            candidate.included_in_render = True
            candidate.prune_reason = None


def apply_clip_fingerprint_pruning(
    candidates: list[CandidatePlay],
    clip_paths_by_event: dict[int, Path],
    ffmpeg_binary: str,
) -> None:
    ffmpeg_executable = resolve_ffmpeg_binary(ffmpeg_binary)
    grouped_candidates: dict[tuple[int, int], list[CandidatePlay]] = {}
    for candidate in candidates:
        if not candidate.included_in_render:
            continue
        if candidate.period is None or candidate.clip_duration_ms is None:
            continue
        if candidate.event_num not in clip_paths_by_event:
            continue
        grouped_candidates.setdefault((candidate.period, candidate.clip_duration_ms), []).append(candidate)

    signature_cache: dict[int, tuple[str, str, str]] = {}
    for group_key in sorted(grouped_candidates):
        period_candidates = sorted(grouped_candidates[group_key], key=lambda candidate: candidate.event_num)
        if len(period_candidates) < 2:
            continue

        seen_signatures: set[tuple[str, str, str]] = set()
        for candidate in period_candidates:
            signature = signature_cache.get(candidate.event_num)
            if signature is None:
                signature = compute_clip_frame_signature(
                    ffmpeg_executable,
                    clip_paths_by_event[candidate.event_num],
                )
                signature_cache[candidate.event_num] = signature
            if signature in seen_signatures:
                candidate.included_in_render = False
                candidate.prune_reason = "duplicate_clip_fingerprint"
                continue
            seen_signatures.add(signature)
            candidate.included_in_render = True
            if candidate.prune_reason == "duplicate_clip_fingerprint":
                candidate.prune_reason = None


def compute_clip_frame_signature(
    ffmpeg_executable: str,
    clip_path: Path,
) -> tuple[str, str, str]:
    duration_seconds = probe_clip_duration_seconds(ffmpeg_executable, clip_path)
    sample_fractions = (0.2, 0.5, 0.8)
    return tuple(
        extract_clip_frame_hash(
            ffmpeg_executable,
            clip_path,
            max(0.0, min(duration_seconds - 0.05, duration_seconds * fraction)),
        )
        for fraction in sample_fractions
    )


def probe_clip_duration_seconds(ffmpeg_executable: str, clip_path: Path) -> float:
    command = [
        ffmpeg_executable,
        "-hide_banner",
        "-i",
        str(clip_path),
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    duration_prefix = "Duration: "
    for line in result.stderr.splitlines():
        if duration_prefix not in line:
            continue
        duration_text = line.split(duration_prefix, maxsplit=1)[1].split(",", maxsplit=1)[0].strip()
        hours_text, minutes_text, seconds_text = duration_text.split(":")
        return int(hours_text) * 3600 + int(minutes_text) * 60 + float(seconds_text)
    raise RuntimeError(f"Could not determine clip duration for {clip_path}.")


def extract_clip_frame_hash(
    ffmpeg_executable: str,
    clip_path: Path,
    timestamp_seconds: float,
) -> str:
    command = [
        ffmpeg_executable,
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=16:16,format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, check=True)
    return hashlib.sha256(result.stdout).hexdigest()


def _candidate_interval(candidate: CandidatePlay) -> tuple[float, float] | None:
    if candidate.estimated_interval_start is None or candidate.estimated_interval_end is None:
        return None
    return candidate.estimated_interval_start, candidate.estimated_interval_end


def _interval_length(interval: tuple[float, float] | None) -> float:
    if interval is None:
        return 0.0
    return max(interval[1] - interval[0], 0.0)


def _is_interval_fully_covered_by_others(
    target_interval: tuple[float, float],
    other_candidates: list[CandidatePlay],
) -> bool:
    other_intervals = [
        interval
        for candidate in other_candidates
        if (interval := _candidate_interval(candidate)) is not None
    ]
    merged_intervals = _merge_intervals(other_intervals)
    covered_until = target_interval[0]
    for start, end in merged_intervals:
        if end <= covered_until:
            continue
        if start > covered_until:
            break
        covered_until = max(covered_until, min(end, target_interval[1]))
        if covered_until >= target_interval[1]:
            return True
    return covered_until >= target_interval[1]


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda interval: (interval[0], interval[1]))
    merged: list[tuple[float, float]] = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _format_game_clock_from_elapsed(period: int | None, elapsed_seconds: float | None) -> str | None:
    if period is None or elapsed_seconds is None:
        return None
    period_length_seconds = 300.0 if period > 4 else 720.0
    remaining_seconds = max(0.0, min(period_length_seconds, period_length_seconds - elapsed_seconds))
    minutes = int(remaining_seconds // 60)
    seconds = remaining_seconds - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"


def _is_valid_video_payload(payload: dict[str, object]) -> bool:
    result_sets = payload.get("resultSets")
    if not isinstance(result_sets, dict):
        return False
    meta = result_sets.get("Meta")
    return isinstance(meta, dict)


def write_debug_report(
    candidates: list[CandidatePlay],
    output_dir: Path,
    game_id: str,
    debug_stats: FetchDebugStats,
    max_workers: int,
    request_retries: int,
    retry_backoff_seconds: float,
    request_timeout_seconds: int,
    prune_overlap: bool,
    prune_pre_buffer_seconds: float,
    prune_post_buffer_seconds: float,
) -> Path:
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_json_path = debug_dir / f"{game_id}_debug.json"
    failures = [
        {
            "event_num": candidate.event_num,
            "period": candidate.period,
            "clock": candidate.clock,
            "description": candidate.description,
            "availability_status": candidate.availability_status,
            "availability_error": candidate.availability_error,
        }
        for candidate in candidates
        if candidate.availability_status == "error"
    ]
    pruned = [
        {
            "event_num": candidate.event_num,
            "period": candidate.period,
            "clock": candidate.clock,
            "description": candidate.description,
            "clip_duration_seconds": candidate.clip_duration_seconds,
            "estimated_interval_start": candidate.estimated_interval_start,
            "estimated_interval_end": candidate.estimated_interval_end,
            "estimated_clock_start": candidate.estimated_clock_start,
            "estimated_clock_end": candidate.estimated_clock_end,
            "prune_reason": candidate.prune_reason,
        }
        for candidate in candidates
        if candidate.prune_reason
    ]
    debug_payload = {
        "game_id": game_id,
        "settings": {
            "max_workers": max_workers,
            "request_retries": request_retries,
            "retry_backoff_seconds": retry_backoff_seconds,
            "request_timeout_seconds": request_timeout_seconds,
            "prune_overlap": prune_overlap,
            "prune_pre_buffer_seconds": prune_pre_buffer_seconds,
            "prune_post_buffer_seconds": prune_post_buffer_seconds,
        },
        "counts": {
            "total_events": len(candidates),
            "available_events": sum(1 for candidate in candidates if candidate.video_available),
            "rendered_events": sum(1 for candidate in candidates if candidate.included_in_render),
            "pruned_events": len(pruned),
        },
        "timing_seconds": {
            "video_probe_seconds": debug_stats.video_probe_seconds,
            "download_seconds": debug_stats.download_seconds,
            "render_seconds": debug_stats.render_seconds,
            "total_seconds": debug_stats.total_seconds,
        },
        "fetch_stats": {
            "cache_hits": debug_stats.cache_hits,
            "cache_misses": debug_stats.cache_misses,
            "fetch_successes": debug_stats.fetch_successes,
            "fetch_missing": debug_stats.fetch_missing,
            "fetch_failures": debug_stats.fetch_failures,
            "retries": debug_stats.retries,
            "events_probed": debug_stats.events_probed,
        },
        "failures": failures,
        "pruned": pruned,
    }
    debug_json_path.write_text(json.dumps(debug_payload, indent=2), encoding="utf-8")
    return debug_json_path
