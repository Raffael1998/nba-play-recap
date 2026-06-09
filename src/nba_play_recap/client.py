from __future__ import annotations

import gzip
from http.client import RemoteDisconnected
import json
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zlib


NBA_STATS_BASE_URL = "https://stats.nba.com/stats"
NBA_CDN_BASE_URL = "https://cdn.nba.com/static/json/liveData"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.nba.com",
    "Pragma": "no-cache",
    "Referer": "https://stats.nba.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

LIVE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Referer": "https://www.nba.com/",
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
}


class NbaStatsError(RuntimeError):
    """Raised when the NBA stats API returns an unusable response."""


class NbaStatsClient:
    def __init__(
        self,
        timeout_seconds: int = 30,
        request_retries: int = 3,
        retry_backoff_seconds: float = 1.5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.request_retries = request_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    def get_json(
        self,
        endpoint: str,
        params: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        query = urlencode({key: str(value) for key, value in params.items()})
        url = f"{NBA_STATS_BASE_URL}/{endpoint}?{query}"
        return self.get_json_from_url(url, DEFAULT_HEADERS, timeout_seconds=timeout_seconds)

    def get_json_from_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        request = Request(url, headers=DEFAULT_HEADERS)
        if headers is not None:
            request = Request(url, headers=headers)

        raw_payload: bytes | None = None
        encoding = ""
        attempts = max(self.request_retries, 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                    raw_payload = response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                break
            except HTTPError as exc:
                raise NbaStatsError(f"NBA stats request failed with HTTP {exc.code}: {url}") from exc
            except (RemoteDisconnected, URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(self.retry_backoff_seconds * attempt)

        if raw_payload is None:
            if isinstance(last_error, URLError):
                raise NbaStatsError(
                    f"NBA stats request failed after {attempts} attempts: {last_error.reason}"
                ) from last_error
            if isinstance(last_error, TimeoutError):
                raise NbaStatsError(
                    f"NBA stats request timed out after {attempts} attempts: {url}"
                ) from last_error
            raise NbaStatsError(
                f"NBA stats request failed after {attempts} attempts: {url}"
            ) from last_error

        payload = _decode_payload(raw_payload, encoding)

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise NbaStatsError("NBA stats response was not valid JSON.") from exc

    def get_playbyplay(self, game_id: str, start_period: int = 1, end_period: int = 10) -> dict[str, Any]:
        return self.get_json(
            "playbyplayv2",
            {
                "EndPeriod": end_period,
                "GameID": game_id,
                "StartPeriod": start_period,
            },
        )

    def get_live_playbyplay(self, game_id: str) -> dict[str, Any]:
        url = f"{NBA_CDN_BASE_URL}/playbyplay/playbyplay_{game_id}.json"
        return self.get_json_from_url(url, LIVE_HEADERS)

    def get_scoreboard_v3(self, game_date: str) -> dict[str, Any]:
        return self.get_json(
            "scoreboardv3",
            {
                "GameDate": game_date,
                "LeagueID": "00",
            },
        )

    def get_video_event_asset(
        self,
        game_id: str,
        event_num: int,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.get_json(
            "videoeventsasset",
            {
                "GameEventID": event_num,
                "GameID": game_id,
            },
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def save_json(data: dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _decode_payload(raw_payload: bytes, encoding: str) -> str:
    if encoding == "gzip":
        return gzip.decompress(raw_payload).decode("utf-8")
    if encoding == "deflate":
        return zlib.decompress(raw_payload).decode("utf-8")
    return raw_payload.decode("utf-8")
