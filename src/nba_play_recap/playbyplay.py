from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class CandidatePlay:
    game_id: str
    event_num: int
    period: int | None
    clock: str | None
    clock_seconds_remaining: float | None
    description: str
    home_score: str | None
    visitor_score: str | None
    score_margin: str | None
    event_msg_type: int | None
    video_available: bool
    action_type: str | None = None
    team_tricode: str | None = None
    clip_url: str | None = None
    clip_duration_ms: int | None = None
    clip_duration_seconds: float | None = None
    availability_status: str = "unprobed"
    availability_error: str | None = None
    included_in_render: bool = False
    prune_reason: str | None = None
    estimated_interval_start: float | None = None
    estimated_interval_end: float | None = None
    estimated_clock_start: str | None = None
    estimated_clock_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_candidate_actions(
    actions: list[dict[str, Any]],
    game_id: str,
    exclude_action_types: set[str] | None = None,
    video_only: bool = True,
) -> list[CandidatePlay]:
    """Turn a game page's `playByPlay.actions` into candidates.

    The actions come from `__NEXT_DATA__.props.pageProps.playByPlay` on the game page,
    which is server-rendered and needs no API host (D-036). Each action carries its own
    `videoAvailable` flag, so `video_only` filters out the ~100 actions per game that
    have no clip **before** any request is made for one — the previous design probed
    every event and learned the same thing 100 requests later.

    **The filter is a denylist on purpose**, and it is the second thing this page's
    vocabulary has caught out. The action types here are *not* the live CDN feed's
    (`2pt`, `3pt`, `freethrow`, …) but a display vocabulary — `Made Shot`, `Missed
    Shot`, `Free Throw` — and **blocks and steals carry an empty `actionType`
    entirely**. An allowlist written against the old names selected nothing at all and
    reported a game with zero events, which reads exactly like a broken feed. A
    denylist fails the other way: an unrecognised type ends up *in* the recap, which is
    visible in the output rather than silent.
    """
    candidates: list[CandidatePlay] = []
    excluded = exclude_action_types or set()
    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = _as_str(action.get("actionType"))
        if action_type in excluded:
            continue

        has_video = bool(_as_int(action.get("videoAvailable")))
        if video_only and not has_video:
            continue

        event_num = _as_int(action.get("actionNumber"))
        if event_num is None:
            continue

        candidates.append(
            CandidatePlay(
                game_id=game_id,
                event_num=event_num,
                period=_as_int(action.get("period")),
                clock=_clock_from_iso_duration(_as_str(action.get("clock"))),
                clock_seconds_remaining=_clock_seconds_from_iso_duration(_as_str(action.get("clock"))),
                description=_as_str(action.get("description")) or "Unknown event",
                home_score=_as_str(action.get("scoreHome")),
                visitor_score=_as_str(action.get("scoreAway")),
                score_margin=_score_margin(_as_str(action.get("scoreAway")), _as_str(action.get("scoreHome"))),
                event_msg_type=None,
                video_available=has_video,
                action_type=action_type,
                team_tricode=_as_str(action.get("teamTricode")),
            )
        )

    return candidates


def attach_video_metadata(candidate: CandidatePlay, payload: dict[str, Any]) -> CandidatePlay:
    result_sets = payload.get("resultSets", {})
    meta = result_sets.get("Meta", {}) if isinstance(result_sets, dict) else {}
    video_urls = meta.get("videoUrls", []) if isinstance(meta, dict) else []
    clip_url = None
    clip_duration_ms = None
    if isinstance(video_urls, list) and video_urls:
        primary = video_urls[0]
        if isinstance(primary, dict):
            clip_url = _as_str(primary.get("lurl") or primary.get("murl") or primary.get("surl"))
            clip_duration_ms = (
                _as_int(primary.get("ldur"))
                or _as_int(primary.get("mdur"))
                or _as_int(primary.get("sdur"))
            )

    candidate.video_available = bool(clip_url)
    candidate.clip_url = clip_url
    candidate.clip_duration_ms = clip_duration_ms
    candidate.clip_duration_seconds = round(clip_duration_ms / 1000, 3) if clip_duration_ms is not None else None
    candidate.availability_status = "available" if clip_url else "missing"
    candidate.availability_error = None
    return candidate


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _clock_from_iso_duration(value: str | None) -> str | None:
    seconds_remaining = _clock_seconds_from_iso_duration(value)
    if seconds_remaining is None:
        return value
    minutes = int(seconds_remaining // 60)
    seconds = seconds_remaining - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"


def _clock_seconds_from_iso_duration(value: str | None) -> float | None:
    if not value or not value.startswith("PT") or "M" not in value:
        return None
    minutes_part = value[2:].split("M", maxsplit=1)[0]
    seconds_part = value.split("M", maxsplit=1)[1].rstrip("S")
    try:
        return int(minutes_part) * 60 + float(seconds_part)
    except ValueError:
        return None


def _score_margin(visitor_score: str | None, home_score: str | None) -> str | None:
    if visitor_score is None or home_score is None:
        return None
    try:
        return str(int(home_score) - int(visitor_score))
    except ValueError:
        return None
