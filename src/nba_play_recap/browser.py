"""Every NBA request goes through a real browser. There is no HTTP client here.

This is not a workaround for header guessing — it is the only client nba.com serves.
Measured on 2026-08-07 (desktop) and 2026-08-08 (this server):

  - `stats.nba.com/stats/videoeventsasset` answers an in-page `fetch` from an nba.com
    origin in ~300 ms, and hangs for 300 s as a top-level navigation from the same
    browser in the same second.
  - `videos.nba.com` returns a fixed 31,580,089-byte "video not available" object to
    every client that is not a browser, with a 200 rather than a 403 — which is why
    this looked like success for two sessions. Headers, cookies, `Range`, redirects
    and every rendition were each measured dead.
  - That host sends no `Access-Control-Allow-Origin` at all, so in-page JavaScript can
    never read the bytes. A `<video>` element loads it anyway, because media elements
    load cross-origin for *playback* without CORS. So the download has to happen
    **below** the CORS layer, inside the browser's network stack, which is what CDP's
    `Fetch` domain reaches and a page cannot.

The full reasoning is in D-036 and the reference implementation this ports from is
`docs/reference/nba-cdp-intercept.mjs` in the homeserver repo.

Two things this module needs from its host, both of which the job runner must supply
explicitly (D-033):

  - a **headed** browser under `xvfb-run -a`; NBA rejects true headless, and
  - a **writable `$HOME`**, without which Chromium refuses to start.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from types import TracebackType
from typing import Any

DEFAULT_CHROMIUM_PATH = "/usr/bin/chromium"
NBA_GAMES_URL = "https://www.nba.com/games?date={game_date}"
NBA_GAME_URL = "https://www.nba.com/game/{game_id}/play-by-play"
VIDEO_EVENT_ASSET_URL = (
    "https://stats.nba.com/stats/videoeventsasset?GameEventID={event_num}&GameID={game_id}"
)

MEDIA_URL_PATTERN = re.compile(r"videos\.nba\.com/.*\.mp4")

# The object NBA's edge returns for anything it will not serve. It is path-independent:
# the same bytes come back for a subtitle path and for a deliberately bogus UUID, and
# its Last-Modified of 2025-08-06 predates games it is returned for. Never an expired
# clip, and never evidence about one.
VIDEO_NOT_AVAILABLE_SHA256 = "45934326c3cac055be7389cc27484107cafbda429afa54ca35b3ac2350df79a0"
VIDEO_NOT_AVAILABLE_BYTES = 31_580_089


class NbaBrowserError(RuntimeError):
    """Raised when the browser could not produce what was asked of nba.com."""


class NbaBrowser:
    """A single Chromium page held open on an nba.com origin, plus a CDP tap on it.

    One page serves a whole game: the play-by-play is read out of the page it is
    already on, clip metadata comes from in-page `fetch`, and each clip is fetched by
    injecting a `<video>` element and taking the bytes off the intercepted response.
    Measured 2026-08-08: one 1.2 s page load, then ~1.2 s per clip.
    """

    def __init__(
        self,
        executable_path: str | None = DEFAULT_CHROMIUM_PATH,
        navigation_timeout_seconds: int = 90,
        request_timeout_seconds: int = 30,
        clip_timeout_seconds: int = 120,
        asset_batch_size: int = 4,
    ) -> None:
        self.executable_path = executable_path
        self.navigation_timeout_ms = navigation_timeout_seconds * 1000
        self.request_timeout_seconds = request_timeout_seconds
        self.clip_timeout_seconds = clip_timeout_seconds
        self.asset_batch_size = max(asset_batch_size, 1)

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._paused_requests: list[dict[str, Any]] = []
        self._fetch_enabled = False

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> NbaBrowser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - a broken install, not a code path
            raise NbaBrowserError(
                "Playwright is not installed. Run `uv sync` in the project directory."
            ) from exc

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": False}
        if self.executable_path:
            # An explicit binary, never a channel: `channel` selects *branded* releases
            # (chrome, msedge) only, so it cannot reach Debian's own chromium. This is
            # what keeps a fourth vendor apt repo and a Playwright browser bundle off
            # this machine (D-033).
            launch_kwargs["executable_path"] = self.executable_path
        try:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            self._stop_playwright()
            raise NbaBrowserError(
                f"Could not launch Chromium at {self.executable_path!r}. "
                "A headed browser needs a display — run under `xvfb-run -a` — and a "
                f"writable $HOME. Details: {exc}"
            ) from exc

        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(self.navigation_timeout_ms)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - closing must not mask a real error
                pass
            self._browser = None
        self._stop_playwright()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise NbaBrowserError("The browser is not open. Use NbaBrowser as a context manager.")
        return self._page

    # -- reading pages ----------------------------------------------------

    def _next_data_props(self, url: str) -> dict[str, Any]:
        """Navigate to `url` and return `__NEXT_DATA__.props.pageProps`.

        Everything this project needs from nba.com is server-rendered into that blob,
        so no API host is involved in reading it.
        """
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NbaBrowserError(f"Could not load {url}: {exc}") from exc

        raw = self.page.evaluate(
            "() => { const el = document.getElementById('__NEXT_DATA__');"
            " return el ? el.textContent : null; }"
        )
        if not raw:
            raise NbaBrowserError(f"No __NEXT_DATA__ on {url} — the page shape has changed.")
        try:
            return json.loads(raw)["props"]["pageProps"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise NbaBrowserError(f"__NEXT_DATA__ on {url} was not the expected shape.") from exc

    def scheduled_games(self, game_date: str) -> list[dict[str, Any]]:
        """Return the `cardData` of every game card for an ISO date.

        Each card carries `gameId`, `gameStatus` (3 = Final), `gameStatusText` and both
        teams' `teamTricode`. An out-of-season date returns an empty list rather than
        an error, which is the pipeline's "nothing to do" case.
        """
        props = self._next_data_props(NBA_GAMES_URL.format(game_date=game_date))
        modules = (props.get("gameCardFeed") or {}).get("modules") or []
        cards: list[dict[str, Any]] = []
        for module in modules:
            for card in module.get("cards") or []:
                card_data = card.get("cardData")
                if isinstance(card_data, dict):
                    cards.append(card_data)
        return cards

    def open_game(self, game_id: str) -> list[dict[str, Any]]:
        """Load a game's play-by-play page and return its actions.

        The page stays open afterwards, and every later call in this class relies on
        that: the in-page fetches and the clip downloads both need an nba.com origin.

        No slug is needed. `/game/<gameId>/play-by-play` carries all 520 actions —
        measured 2026-08-08, correcting D-036, which reported the slug as load-bearing.
        The path that silently renders the summary tab is the *un-hyphenated*
        `/playbyplay`, which is simply not a route.
        """
        props = self._next_data_props(NBA_GAME_URL.format(game_id=game_id))
        play_by_play = props.get("playByPlay")
        if not isinstance(play_by_play, dict):
            raise NbaBrowserError(
                f"Game {game_id} has no playByPlay in its page props. "
                "Either the game does not exist or it has not been played."
            )
        actions = play_by_play.get("actions")
        if not isinstance(actions, list):
            raise NbaBrowserError(f"Game {game_id}'s playByPlay carried no actions list.")
        return [action for action in actions if isinstance(action, dict)]

    # -- clip metadata ----------------------------------------------------

    def video_event_assets(self, game_id: str, event_nums: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch `videoeventsasset` for many events, from inside the open page.

        Issued in small concurrent batches rather than all at once: volume is the one
        thing that could still earn this box a real block, and nothing here is urgent.

        A failed event is returned as `{"error": ...}` rather than raising, so one bad
        event cannot lose a whole game.
        """
        results: dict[int, dict[str, Any]] = {}
        for start in range(0, len(event_nums), self.asset_batch_size):
            batch = event_nums[start : start + self.asset_batch_size]
            results.update(self._fetch_asset_batch(game_id, batch))
        return results

    def _fetch_asset_batch(
        self, game_id: str, event_nums: list[int]
    ) -> dict[int, dict[str, Any]]:
        # `credentials: 'include'` makes this fail with "TypeError: Failed to fetch" —
        # the endpoint's CORS headers do not permit it, and the failure looks exactly
        # like a block. Measured 2026-08-08.
        script = """async ([gameId, eventNums, timeoutMs]) => {
            return await Promise.all(eventNums.map(async (eventNum) => {
                const url = `https://stats.nba.com/stats/videoeventsasset`
                    + `?GameEventID=${eventNum}&GameID=${gameId}`;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const response = await fetch(url, {signal: controller.signal});
                    if (!response.ok) {
                        return {eventNum, error: `HTTP ${response.status}`};
                    }
                    return {eventNum, body: await response.text()};
                } catch (error) {
                    return {eventNum, error: String(error)};
                } finally {
                    clearTimeout(timer);
                }
            }));
        }"""
        try:
            raw_results = self.page.evaluate(
                script, [game_id, event_nums, self.request_timeout_seconds * 1000]
            )
        except Exception as exc:
            raise NbaBrowserError(f"In-page fetch of clip metadata failed: {exc}") from exc

        parsed: dict[int, dict[str, Any]] = {}
        for entry in raw_results:
            event_num = int(entry["eventNum"])
            if entry.get("error"):
                parsed[event_num] = {"error": entry["error"]}
                continue
            try:
                parsed[event_num] = json.loads(entry["body"])
            except json.JSONDecodeError:
                parsed[event_num] = {"error": "videoeventsasset did not return JSON"}
        return parsed

    # -- clip bytes -------------------------------------------------------

    def _enable_media_interception(self) -> None:
        if self._fetch_enabled:
            return
        self._cdp = self._context.new_cdp_session(self.page)

        def queue_paused_request(params: dict[str, Any]) -> None:
            # Queued rather than handled here: Playwright's sync API deadlocks if a
            # CDP call is issued from inside an event handler. It also refuses a
            # builtin like `list.append`, which it cannot annotate.
            self._paused_requests.append(params)

        self._cdp.on("Fetch.requestPaused", queue_paused_request)
        self._cdp.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*videos.nba.com*", "requestStage": "Response"}]},
        )
        self._fetch_enabled = True

    def download_clip(self, clip_url: str, destination: Path) -> int:
        """Write one clip to `destination` and return its size in bytes.

        The bytes are taken off the response of a `<video>` element injected into the
        open nba.com page — below the CORS layer, which is the only place they are
        readable. Raises rather than writing the "video not available" object, since
        that object is a 200 and would otherwise concatenate into the recap as 15 s of
        NBA's own placeholder.
        """
        self._enable_media_interception()
        self._drain_paused_requests(capture_url=None)

        self.page.evaluate(
            """(clipUrl) => {
                const existing = document.getElementById('nba-recap-clip');
                if (existing) { existing.pause(); existing.removeAttribute('src'); existing.remove(); }
                const video = document.createElement('video');
                video.id = 'nba-recap-clip';
                video.src = clipUrl;
                video.muted = true;
                video.style.position = 'fixed';
                video.style.left = '-10000px';
                document.body.appendChild(video);
                video.play().catch(() => undefined);
            }""",
            clip_url,
        )

        deadline = time.perf_counter() + self.clip_timeout_seconds
        payload: bytes | None = None
        while payload is None and time.perf_counter() < deadline:
            payload = self._drain_paused_requests(capture_url=clip_url)
            if payload is None:
                self.page.wait_for_timeout(200)

        self.page.evaluate(
            """() => {
                const video = document.getElementById('nba-recap-clip');
                if (video) { video.pause(); video.removeAttribute('src'); video.remove(); }
            }"""
        )

        if payload is None:
            raise NbaBrowserError(
                f"No media response was intercepted within {self.clip_timeout_seconds}s "
                f"for {clip_url}"
            )
        if len(payload) == VIDEO_NOT_AVAILABLE_BYTES:
            raise NbaBrowserError(
                "NBA returned its 'video not available' placeholder rather than a clip. "
                "It is a 200, not an error, and it is not evidence that the clip expired."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return len(payload)

    def _drain_paused_requests(self, capture_url: str | None) -> bytes | None:
        """Continue every paused request, capturing the body of the one we asked for.

        Every paused request must be continued whether or not it is wanted — the
        subtitle track pauses here too, and leaving it paused stalls the page.
        """
        captured: bytes | None = None
        while self._paused_requests:
            params = self._paused_requests.pop(0)
            request_id = params["requestId"]
            url = params.get("request", {}).get("url", "")
            wanted = (
                captured is None
                and capture_url is not None
                and url == capture_url
                and MEDIA_URL_PATTERN.search(url) is not None
            )
            if wanted:
                try:
                    body = self._cdp.send("Fetch.getResponseBody", {"requestId": request_id})
                    captured = (
                        base64.b64decode(body["body"])
                        if body.get("base64Encoded")
                        else str(body["body"]).encode()
                    )
                except Exception:  # noqa: BLE001 - a retry is the caller's business
                    captured = None
            try:
                self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:  # noqa: BLE001 - the page may already have moved on
                pass
        return captured
