from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


PARIS_TZ = ZoneInfo("Europe/Paris")
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DEFAULT_CLIENT_SECRETS_PATH = Path("C:/data/nba-play-recap/secrets/youtube_client_secret.json")
DEFAULT_TOKEN_PATH = Path("C:/data/nba-play-recap/secrets/youtube_token.json")
DEFAULT_PRIVACY_STATUS = "public"
DEFAULT_CATEGORY_ID = "17"
DEFAULT_TAGS = ["NBA", "basketball", "recap"]


@dataclass(slots=True)
class YouTubeUploadResult:
    video_id: str
    privacy_status: str | None
    response: dict[str, Any]


Uploader = Callable[[Path, dict[str, Any], Path, Path], YouTubeUploadResult]


def authorize_youtube(client_secrets_path: Path, token_path: Path) -> Path:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("YouTube OAuth support requires google-auth-oauthlib. Run `uv sync`.") from exc

    if not client_secrets_path.exists():
        raise RuntimeError(f"YouTube OAuth client secret file not found: {client_secrets_path}")

    token_path.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), [YOUTUBE_UPLOAD_SCOPE])
    credentials = flow.run_local_server(port=0)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return token_path


def publish_night_report(
    report_path: Path,
    client_secrets_path: Path = DEFAULT_CLIENT_SECRETS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
    privacy_status: str = DEFAULT_PRIVACY_STATUS,
    force_upload: bool = False,
    uploader: Uploader | None = None,
) -> dict[str, Any]:
    if not report_path.exists():
        raise RuntimeError(f"run_report.json not found: {report_path}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    uploader = uploader or upload_video
    publish_report: dict[str, Any] = {
        "report_path": str(report_path),
        "started_at": datetime.now(PARIS_TZ).isoformat(),
        "ended_at": None,
        "privacy_requested": privacy_status,
        "games": [],
        "summary": {"success": 0, "skipped": 0, "failed": 0},
    }

    for game in report.get("games", []):
        entry = publish_game(
            game=game,
            client_secrets_path=client_secrets_path,
            token_path=token_path,
            privacy_status=privacy_status,
            force_upload=force_upload,
            uploader=uploader,
        )
        publish_report["games"].append(entry)
        publish_report["summary"][entry["status"]] += 1

    publish_report["ended_at"] = datetime.now(PARIS_TZ).isoformat()
    return publish_report


def publish_game(
    game: dict[str, Any],
    client_secrets_path: Path,
    token_path: Path,
    privacy_status: str,
    force_upload: bool,
    uploader: Uploader,
) -> dict[str, Any]:
    output_dir = Path(str(game.get("output_dir") or ""))
    status_path = output_dir / "youtube_status.json"
    game = _resolve_publishable_game_entry(game, output_dir)
    entry = _base_status(game, status_path, privacy_status)

    if game.get("status") != "success":
        entry["status"] = "skipped"
        entry["skip_reason"] = "render_not_successful"
        _write_status(status_path, entry)
        return entry

    existing = _load_existing_status(status_path)
    if not force_upload and _is_successfully_uploaded(existing):
        entry.update(existing)
        entry["status"] = "skipped"
        entry["skip_reason"] = "already_uploaded"
        return entry

    video_path = _video_path_from_game(game)
    if video_path is None or not video_path.exists():
        entry["status"] = "failed"
        entry["error"] = f"video file not found: {video_path}"
        _write_status(status_path, entry)
        return entry

    metadata = build_youtube_metadata(game, privacy_status)
    try:
        result = uploader(video_path, metadata, client_secrets_path, token_path)
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = str(exc)
        entry["metadata"] = metadata
        _write_status(status_path, entry)
        return entry

    entry["status"] = "success"
    entry["uploaded_at"] = datetime.now(PARIS_TZ).isoformat()
    entry["video_id"] = result.video_id
    entry["privacy_returned"] = result.privacy_status
    entry["metadata"] = metadata
    _write_status(status_path, entry)
    return entry


def build_youtube_metadata(game: dict[str, Any], privacy_status: str = DEFAULT_PRIVACY_STATUS) -> dict[str, Any]:
    matchup = str(game.get("matchup") or "").strip()
    game_date = str(game.get("game_date") or "").strip()
    title = f"{matchup} Full Game Recap - {game_date}".strip()
    description = (
        f"Automatically generated chronological NBA recap for {matchup} on {game_date}.\n\n"
        "Built from official per-play clips and play-by-play data."
    )
    return {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": DEFAULT_CATEGORY_ID,
            "tags": DEFAULT_TAGS,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }


def upload_video(
    video_path: Path,
    metadata: dict[str, Any],
    client_secrets_path: Path,
    token_path: Path,
) -> YouTubeUploadResult:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload support requires google-api-python-client, "
            "google-auth-oauthlib, and google-auth-httplib2. Run `uv sync`."
        ) from exc

    if not client_secrets_path.exists():
        raise RuntimeError(f"YouTube OAuth client secret file not found: {client_secrets_path}")
    if not token_path.exists():
        raise RuntimeError(f"YouTube token file not found: {token_path}. Run `youtube-auth` first.")

    credentials = Credentials.from_authorized_user_file(str(token_path), [YOUTUBE_UPLOAD_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise RuntimeError("YouTube OAuth token is invalid. Run `youtube-auth` again.")

    youtube = build("youtube", "v3", credentials=credentials)
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=metadata,
        media_body=media,
    )

    response = None
    while response is None:
        _status, response = request.next_chunk()

    video_id = str(response.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube upload completed without a video id in the response.")
    returned_privacy = response.get("status", {}).get("privacyStatus")
    return YouTubeUploadResult(
        video_id=video_id,
        privacy_status=str(returned_privacy) if returned_privacy else None,
        response=response,
    )


def has_publish_failures(report: dict[str, Any]) -> bool:
    return int(report.get("summary", {}).get("failed", 0)) > 0


def _base_status(game: dict[str, Any], status_path: Path, privacy_status: str) -> dict[str, Any]:
    return {
        "game_id": game.get("game_id"),
        "game_date": game.get("game_date"),
        "matchup": game.get("matchup"),
        "status": "pending",
        "skip_reason": None,
        "error": None,
        "video_id": None,
        "uploaded_at": None,
        "privacy_requested": privacy_status,
        "privacy_returned": None,
        "status_path": str(status_path),
    }


def _write_status(status_path: Path, status: dict[str, Any]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _load_existing_status(status_path: Path) -> dict[str, Any] | None:
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _is_successfully_uploaded(status: dict[str, Any] | None) -> bool:
    return bool(status and status.get("status") == "success" and status.get("video_id"))


def _video_path_from_game(game: dict[str, Any]) -> Path | None:
    outputs = game.get("outputs")
    if not isinstance(outputs, dict):
        return None
    video_path = outputs.get("video_path")
    if not video_path:
        return None
    return Path(str(video_path))


def _resolve_publishable_game_entry(game: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    if game.get("status") == "success" or game.get("skip_reason") != "already_rendered":
        return game

    prior_status_path = output_dir / "game_status.json"
    if not prior_status_path.exists():
        return game
    try:
        prior_status = json.loads(prior_status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return game
    if prior_status.get("status") != "success":
        return game
    return prior_status
