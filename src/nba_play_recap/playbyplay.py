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


def extract_video_candidates(payload: dict[str, Any], game_id: str) -> list[CandidatePlay]:
    tables = _extract_named_tables(payload)
    play_rows = tables.get("PlayByPlay")
    if not play_rows:
        raise ValueError("The play-by-play payload did not contain a PlayByPlay dataset.")
    video_rows = tables.get("AvailableVideo")
    video_flags = _extract_video_flags(play_rows, video_rows)

    candidates: list[CandidatePlay] = []
    for row in play_rows:
        event_num = _as_int(row.get("EVENTNUM"))
        if event_num is None or not video_flags.get(event_num, False):
            continue

        candidates.append(
            CandidatePlay(
                game_id=game_id,
                event_num=event_num,
                period=_as_int(row.get("PERIOD")),
                clock=_as_str(row.get("PCTIMESTRING")),
                clock_seconds_remaining=_clock_seconds_from_display(_as_str(row.get("PCTIMESTRING"))),
                description=_build_description(row),
                home_score=_as_str(row.get("SCORE")).split(" - ")[-1] if row.get("SCORE") and " - " in str(row.get("SCORE")) else None,
                visitor_score=_as_str(row.get("SCORE")).split(" - ")[0] if row.get("SCORE") and " - " in str(row.get("SCORE")) else None,
                score_margin=_as_str(row.get("SCOREMARGIN")),
                event_msg_type=_as_int(row.get("EVENTMSGTYPE")),
                video_available=True,
            )
        )

    return candidates


def extract_live_candidate_actions(
    payload: dict[str, Any],
    game_id: str,
    include_action_types: set[str] | None = None,
) -> list[CandidatePlay]:
    actions = payload.get("game", {}).get("actions")
    if not isinstance(actions, list):
        raise ValueError("The live play-by-play payload did not contain a game.actions list.")

    candidates: list[CandidatePlay] = []
    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = _as_str(action.get("actionType"))
        if include_action_types and action_type not in include_action_types:
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
                video_available=False,
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


def _extract_named_tables(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}

    data_sets = payload.get("data_sets")
    if isinstance(data_sets, dict):
        for name, rows in data_sets.items():
            if isinstance(rows, list):
                tables[name] = [row for row in rows if isinstance(row, dict)]
        if tables:
            return tables

    result_sets = payload.get("resultSets")
    if isinstance(result_sets, list):
        for table in result_sets:
            name = table.get("name")
            headers = table.get("headers") or []
            rows = table.get("rowSet") or []
            if isinstance(name, str):
                tables[name] = [_zip_row(headers, row) for row in rows]
        return tables

    if isinstance(result_sets, dict):
        for name, table in result_sets.items():
            headers = table.get("headers") or []
            rows = table.get("rowSet") or []
            tables[name] = [_zip_row(headers, row) for row in rows]
        return tables

    raise ValueError("The play-by-play payload did not contain a recognized resultSets structure.")


def _extract_video_flags(
    play_rows: list[dict[str, Any]],
    video_rows: list[dict[str, Any]] | None,
) -> dict[int, bool]:
    if any("VIDEO_AVAILABLE_FLAG" in row for row in play_rows):
        return {
            event_num: bool(_as_int(row.get("VIDEO_AVAILABLE_FLAG")))
            for row in play_rows
            if (event_num := _as_int(row.get("EVENTNUM"))) is not None
        }

    if video_rows is None:
        raise ValueError(
            "The play-by-play payload did not expose video availability in PlayByPlay or AvailableVideo."
        )

    flags: dict[int, bool] = {}
    for index, row in enumerate(video_rows):
        event_num = _as_int(row.get("GAME_EVENT_ID") or row.get("EVENTNUM"))
        if event_num is None and index < len(play_rows):
            event_num = _as_int(play_rows[index].get("EVENTNUM"))
        if event_num is None:
            continue
        flags[event_num] = bool(_as_int(row.get("VIDEO_AVAILABLE_FLAG")))
    return flags


def _zip_row(headers: list[Any], row: list[Any]) -> dict[str, Any]:
    return {str(header): value for header, value in zip(headers, row)}


def _build_description(row: dict[str, Any]) -> str:
    for key in ("HOMEDESCRIPTION", "VISITORDESCRIPTION", "NEUTRALDESCRIPTION"):
        value = _as_str(row.get(key))
        if value:
            return value
    return "Unknown event"


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


def _clock_seconds_from_display(value: str | None) -> float | None:
    if not value or ":" not in value:
        return None
    minutes_part, seconds_part = value.split(":", maxsplit=1)
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
