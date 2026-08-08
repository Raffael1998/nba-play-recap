from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
import json

from nba_play_recap.browser import (
    VIDEO_NOT_AVAILABLE_BYTES,
    NbaBrowser,
    NbaBrowserError,
)
from nba_play_recap.playbyplay import CandidatePlay, attach_video_metadata, extract_candidate_actions
from nba_play_recap.progress import ProgressReporter

# Everything on a game page's timeline is a play worth watching except these. Measured
# against game 0042500402 (520 actions): Substitution 63, Timeout 14, Instant Replay 3,
# period 8 — none of which is basketball. Blocks and steals carry an EMPTY actionType,
# so they must not be excluded by accident. See extract_candidate_actions for why this
# is a denylist rather than a list of wanted types.
EXCLUDED_ACTION_TYPES = {
    "Substitution",
    "Timeout",
    "Instant Replay",
    "period",
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
    probe_aborted: bool = False
    video_probe_seconds: float = 0.0
    download_seconds: float = 0.0
    render_seconds: float = 0.0
    total_seconds: float = 0.0


def build_game_manifest(
    browser: NbaBrowser,
    game_id: str,
    exclude_action_types: set[str] | None = None,
    cache_dir: Path | None = None,
    max_events: int | None = None,
    max_consecutive_failures: int = 8,
    progress: ProgressReporter | None = None,
) -> tuple[list[CandidatePlay], FetchDebugStats]:
    """Read the game's play-by-play and resolve a clip URL for each candidate play.

    Both halves come from the browser: the actions out of the page's own props, and the
    clip metadata out of an in-page fetch. There is no thread pool because Playwright's
    sync API is not thread-safe — concurrency lives inside the page instead, where
    `video_event_assets` issues each batch as one `Promise.all`.
    """
    debug_stats = FetchDebugStats()
    probe_started_at = time.perf_counter()
    actions = browser.open_game(game_id)
    candidates = extract_candidate_actions(
        actions,
        game_id,
        exclude_action_types=(
            EXCLUDED_ACTION_TYPES if exclude_action_types is None else exclude_action_types
        ),
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

    if progress is not None:
        progress.start_phase("Probing clip metadata", total=len(unresolved_candidates))

    # SEVERAL PLAYS CAN SHARE ONE EVENT NUMBER, and they share one clip with it.
    # Measured on 0042500402: 520 actions carry only 489 distinct `actionNumber`s, and
    # the 31 duplicates are exactly the 31 blocks and steals — each recorded against the
    # shot or turnover it belongs to. `actionNumber` is still the right GameEventID
    # (`actionId` is merely a 1..n row index); the duplication is real, not an id
    # mix-up. So group by event and ask NBA once per event, then give the answer to
    # every play that shares it — otherwise those plays keep `video_available` from the
    # page while having no clip URL, and the manifest claims a video that was never
    # fetched.
    by_event: dict[int, list[CandidatePlay]] = {}
    for candidate in unresolved_candidates:
        by_event.setdefault(candidate.event_num, []).append(candidate)

    consecutive_failures = 0
    for event_num, payload in browser.iter_video_event_assets(game_id, list(by_event)):
        siblings = by_event[event_num]
        candidate = siblings[0]
        debug_stats.events_probed += 1
        if payload.get("error") or not _is_valid_video_payload(payload):
            error = str(
                payload.get("error") or "videoeventsasset returned no usable clip URL"
            )
            for sibling in siblings:
                sibling.video_available = False
                sibling.availability_status = "error"
                sibling.availability_error = error
            debug_stats.fetch_failures += 1
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                # Stop rather than grind. This endpoint rate-limits by going SILENT —
                # it neither refuses nor 429s, it just stops replying — so continuing
                # means one full timeout per remaining event, which for a whole game is
                # over an hour of hammering something that has already said no. Every
                # answer received so far is cached, so a later run resumes from here.
                debug_stats.probe_aborted = True
                print(
                    f"warning: {consecutive_failures} clip-metadata requests in a row "
                    f"failed; stopping the probe for game {game_id} after "
                    f"{debug_stats.events_probed}/{len(by_event)} events. "
                    "stats.nba.com rate-limits by not answering.",
                    file=sys.stderr,
                )
                break
            continue
        consecutive_failures = 0
        _write_cached_video_payload(cache_dir, game_id, event_num, payload)
        for sibling in siblings:
            attach_video_metadata(sibling, payload)
        if candidate.video_available:
            debug_stats.fetch_successes += 1
        else:
            debug_stats.fetch_missing += 1
        if progress is not None:
            progress.advance()
    if progress is not None:
        progress.complete_phase("Clip metadata probe")
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


def download_available_clips(
    browser: NbaBrowser,
    candidates: list[CandidatePlay],
    clip_dir: Path,
    progress: ProgressReporter | None = None,
    retries: int = 2,
) -> dict[int, Path]:
    """Download each included clip through the browser, skipping what is already there.

    A clip that cannot be obtained drops out of the render rather than failing the game:
    one missing play is a slightly shorter recap, and a whole game lost to it is not a
    trade worth making. What it must never do is keep NBA's placeholder object — that is
    served with a 200, so nothing else would notice, and it would splice 15 s of "video
    not available" into the middle of the recap.
    """
    clip_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths: dict[int, Path] = {}
    clips_to_download = [
        candidate
        for candidate in candidates
        if candidate.video_available and candidate.clip_url and candidate.included_in_render
    ]
    if progress is not None:
        progress.start_phase("Downloading clips", total=len(clips_to_download))

    for candidate in clips_to_download:
        clip_path = clip_dir / f"{candidate.event_num:05d}.mp4"
        if clip_path.exists() and clip_path.stat().st_size == VIDEO_NOT_AVAILABLE_BYTES:
            clip_path.unlink()

        if not clip_path.exists():
            error = _download_clip_with_retries(
                browser, candidate.clip_url or "", clip_path, retries
            )
            if error is not None:
                clip_path.unlink(missing_ok=True)
                candidate.video_available = False
                candidate.availability_status = "placeholder" if "placeholder" in error else "error"
                candidate.availability_error = error
                candidate.included_in_render = False
                if progress is not None:
                    progress.advance()
                continue

        downloaded_paths[candidate.event_num] = clip_path
        if progress is not None:
            progress.advance()

    if progress is not None:
        progress.complete_phase("Clip download")
    return downloaded_paths


def _download_clip_with_retries(
    browser: NbaBrowser,
    clip_url: str,
    clip_path: Path,
    retries: int,
) -> str | None:
    """Return None on success, or the last error message."""
    attempts = max(retries, 1)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            browser.download_clip(clip_url, clip_path)
            return None
        except NbaBrowserError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(1.0 * attempt)
    return last_error or "clip download failed"


def write_concat_list(clip_paths: list[Path], output_dir: Path, game_id: str) -> Path:
    concat_list_path = output_dir / f"{game_id}_concat.txt"
    lines = [f"file '{path.resolve().as_posix()}'" for path in clip_paths]
    concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_list_path


def render_concat_video(
    concat_list_path: Path,
    output_path: Path,
    ffmpeg_binary: str = "ffmpeg",
    total_duration_seconds: float | None = None,
    progress: ProgressReporter | None = None,
) -> None:
    ffmpeg_executable = resolve_ffmpeg_binary(ffmpeg_binary)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_executable,
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
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
    result = _run_ffmpeg_with_progress(
        command,
        total_duration_seconds=total_duration_seconds,
        progress=progress,
    )
    if result.returncode == 0:
        return

    fallback_command = [
        ffmpeg_executable,
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
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
    if progress is not None:
        progress.info("Concat copy failed, retrying with re-encode.")
    fallback = _run_ffmpeg_with_progress(
        fallback_command,
        total_duration_seconds=total_duration_seconds,
        progress=progress,
    )
    if fallback.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed to render the final video.\n"
            f"Copy attempt stderr:\n{result.stderr}\n\nRe-encode attempt stderr:\n{fallback.stderr}"
        )


def cleanup_clips(clip_dir: Path) -> None:
    if not clip_dir.exists():
        return

    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            shutil.rmtree(clip_dir)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.5 * attempt)

    print(
        f"warning: could not remove temporary clip directory {clip_dir}: {last_error}",
        file=sys.stderr,
    )


def render_full_game(
    browser: NbaBrowser,
    game_id: str,
    output_dir: Path,
    ffmpeg_binary: str,
    keep_clips: bool,
    max_events: int | None,
    clip_retries: int,
    prune_overlap: bool,
    prune_pre_buffer_seconds: float,
    prune_post_buffer_seconds: float,
) -> RenderOutputs:
    started_at = time.perf_counter()
    progress = ProgressReporter(enabled=sys.stderr.isatty())
    progress.info(f"Preparing render for game {game_id}")
    ensure_ffmpeg_available(ffmpeg_binary)
    candidates, debug_stats = build_game_manifest(
        browser,
        game_id,
        cache_dir=output_dir / "cache" / "videoevents",
        max_events=max_events,
        progress=progress,
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
    clip_paths_by_event = download_available_clips(
        browser,
        available_candidates,
        clip_dir,
        progress=progress,
        retries=clip_retries,
    )
    apply_clip_fingerprint_pruning(
        candidates,
        clip_paths_by_event,
        ffmpeg_binary=ffmpeg_binary,
        progress=progress,
    )
    if prune_overlap:
        apply_final_overlap_pruning(candidates)
    debug_stats.download_seconds = round(time.perf_counter() - download_started_at, 3)
    manifest_txt_path, manifest_json_path = write_manifests(candidates, output_dir, game_id)
    rendered_candidates = [candidate for candidate in available_candidates if candidate.included_in_render]
    # One clip per event, even when several plays share the event. A block and the shot
    # it blocked are two rows of the timeline and one video; without this guard the same
    # file is concatenated twice and the recap stutters.
    clip_paths: list[Path] = []
    seen_events: set[int] = set()
    for candidate in sorted(rendered_candidates, key=_candidate_chronology_key):
        if candidate.event_num in seen_events:
            continue
        clip_path = clip_paths_by_event.get(candidate.event_num)
        if clip_path is None:
            continue
        seen_events.add(candidate.event_num)
        clip_paths.append(clip_path)
    concat_list_path = write_concat_list(clip_paths, output_dir, game_id)

    video_path: Path | None = None
    if clip_paths:
        video_path = output_dir / f"{game_id}_full_game.mp4"
        render_started_at = time.perf_counter()
        total_duration_seconds = sum(
            candidate.clip_duration_seconds or 0.0
            for candidate in rendered_candidates
        )
        progress.start_phase("Rendering final video", total=max(int(round(total_duration_seconds)), 1))
        render_concat_video(
            concat_list_path,
            video_path,
            ffmpeg_binary=ffmpeg_binary,
            total_duration_seconds=total_duration_seconds,
            progress=progress,
        )
        progress.complete_phase("Final video render")
        debug_stats.render_seconds = round(time.perf_counter() - render_started_at, 3)

    if not keep_clips:
        cleanup_clips(clip_dir)

    debug_stats.total_seconds = round(time.perf_counter() - started_at, 3)
    debug_json_path = write_debug_report(
        candidates=candidates,
        output_dir=output_dir,
        game_id=game_id,
        debug_stats=debug_stats,
        clip_retries=clip_retries,
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


def _run_ffmpeg_with_progress(
    command: list[str],
    total_duration_seconds: float | None,
    progress: ProgressReporter | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stdout_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        stdout_lines.append(raw_line)
        if progress is None or total_duration_seconds is None or total_duration_seconds <= 0:
            continue
        if not line.startswith("out_time_ms="):
            continue
        try:
            out_time_seconds = int(line.split("=", maxsplit=1)[1]) / 1_000_000
        except ValueError:
            continue
        estimated_seconds = min(out_time_seconds, total_duration_seconds)
        progress.update(current=int(round(estimated_seconds)), total=max(int(round(total_duration_seconds)), 1))

    return_code = process.wait()
    combined_output = "".join(stdout_lines)
    return subprocess.CompletedProcess(
        args=command,
        returncode=return_code,
        stdout=combined_output,
        stderr=combined_output,
    )


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

    for candidate in candidates:
        if candidate.video_available and candidate.clip_url and candidate.estimated_interval_start is None:
            candidate.included_in_render = True
            candidate.prune_reason = None
        if candidate.action_type == "freethrow" and candidate.video_available and candidate.clip_url:
            candidate.included_in_render = True
            candidate.prune_reason = None
        if (
            candidate.video_available
            and candidate.clip_url
            and candidate.estimated_interval_start is not None
            and candidate.estimated_interval_end is not None
            and candidate.action_type != "freethrow"
        ):
            candidate.included_in_render = True
            candidate.prune_reason = None


def apply_final_overlap_pruning(candidates: list[CandidatePlay]) -> None:
    grouped: dict[int, list[CandidatePlay]] = {}
    for candidate in candidates:
        if not candidate.included_in_render:
            continue
        if candidate.action_type == "freethrow":
            continue
        if candidate.estimated_interval_start is None or candidate.estimated_interval_end is None:
            continue
        if candidate.period is None:
            continue
        grouped.setdefault(candidate.period, []).append(candidate)

    for period_candidates in grouped.values():
        _apply_global_coverage_pruning(period_candidates)


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
    progress: ProgressReporter | None = None,
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
    fingerprint_candidates = sum(
        len(period_candidates)
        for period_candidates in grouped_candidates.values()
        if len(period_candidates) >= 2
    )
    if progress is not None and fingerprint_candidates > 0:
        progress.start_phase("Fingerprinting clips", total=fingerprint_candidates)
    for group_key in sorted(grouped_candidates):
        period_candidates = sorted(grouped_candidates[group_key], key=_candidate_chronology_key)
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
            if progress is not None:
                progress.advance()
            if signature in seen_signatures:
                candidate.included_in_render = False
                candidate.prune_reason = "duplicate_clip_fingerprint"
                continue
            seen_signatures.add(signature)
            candidate.included_in_render = True
            if candidate.prune_reason == "duplicate_clip_fingerprint":
                candidate.prune_reason = None
    if progress is not None and fingerprint_candidates > 0:
        progress.complete_phase("Clip fingerprinting")


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


def _candidate_chronology_key(candidate: CandidatePlay) -> tuple[float, float, int]:
    period = float(candidate.period) if candidate.period is not None else float("inf")
    clock_sort = (
        -float(candidate.clock_seconds_remaining)
        if candidate.clock_seconds_remaining is not None
        else float("inf")
    )
    return (period, clock_sort, candidate.event_num)


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
    clip_retries: int,
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
            "clip_retries": clip_retries,
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
            "probe_aborted": debug_stats.probe_aborted,
        },
        "failures": failures,
        "pruned": pruned,
    }
    debug_json_path.write_text(json.dumps(debug_payload, indent=2), encoding="utf-8")
    return debug_json_path
