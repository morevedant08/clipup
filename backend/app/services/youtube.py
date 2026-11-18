"""YouTube URL validation, Google OAuth (read-only), and YouTube Data API v3 service."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from app.models.job import Job

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_API_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

LOGIN_SCOPES: tuple[str, ...] = ("openid", "email", "profile")
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# Explicitly forbidden on the read-only connect flow (only granted via explicit "Enable uploading").
FORBIDDEN_UPLOAD_SCOPES: frozenset[str] = frozenset(
    {
        YOUTUBE_UPLOAD_SCOPE,
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    }
)
_RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB (must be a multiple of 256 KiB)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_ALLOWED_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


class YouTubeURLValidationError(ValueError):
    """Raised when a URL is not a valid YouTube video URL."""


class YouTubeAPIError(RuntimeError):
    """Raised when a Google OAuth or YouTube Data API request fails."""


class YouTubeAuthRevokedError(YouTubeAPIError):
    """Raised when a user's OAuth refresh token is expired, revoked, or lacks upload scope."""


class YouTubeQuotaExceededError(YouTubeAPIError):
    """Raised when YouTube Data API v3 rejects an upload due to daily quota limits."""


class YouTubeTransientUploadError(YouTubeAPIError):
    """Raised on transient network or 5xx/429 YouTube upload errors eligible for Celery retry."""


class VideoDownloadError(YouTubeAPIError):
    """Raised when yt-dlp fails to download a rights-verified video.

    Separates a short, UI-safe `user_message` from full technical diagnostics
    (`detail`) intended only for server/worker logs.
    """

    def __init__(self, user_message: str, detail: str | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail or user_message


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """Normalized YouTube video metadata returned from YouTube Data API v3."""

    video_id: str
    channel_id: str
    channel_title: str
    title: str
    license: str  # "youtube" or "creativeCommon"
    duration_sec: int
    privacy_status: str = "public"
    embeddable: bool = True
    published_at: str | None = None
    thumbnail_url: str | None = None


def extract_youtube_video_id(url: str) -> str:
    """Extract and validate an 11-character YouTube video ID from a URL.

    Supports standard watch URLs, youtu.be short links, shorts, live, and embed URLs.
    Raises YouTubeURLValidationError for invalid or non-YouTube URLs.
    """
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise YouTubeURLValidationError("YouTube URL must start with http:// or https://.")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_YOUTUBE_HOSTS:
        raise YouTubeURLValidationError(
            "URL must point to a valid YouTube domain (youtube.com or youtu.be)."
        )

    video_id: str | None = None
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in {"youtu.be", "www.youtu.be"}:
        if path_parts:
            video_id = path_parts[0]
    else:
        if path_parts and path_parts[0] == "watch":
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live", "v"}:
            video_id = path_parts[1]
        elif not path_parts:
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [None])[0]

    if not video_id or not _VIDEO_ID_RE.match(video_id):
        raise YouTubeURLValidationError(
            "Could not extract a valid 11-character YouTube video ID from the URL."
        )
    return video_id


def extract_youtube_channel_id(url: str) -> str | None:
    """Extract a YouTube channel ID (UC...) from a channel URL if present."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_YOUTUBE_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID_RE.match(parts[1]):
        return parts[1]
    return None


def normalize_youtube_video_url(video_id: str) -> str:
    """Return canonical YouTube watch URL for a validated video ID."""
    if not _VIDEO_ID_RE.match(video_id):
        raise YouTubeURLValidationError("Invalid YouTube video ID.")
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_iso8601_duration(duration: str) -> int:
    """Convert an ISO 8601 YouTube duration string (e.g., PT12M34S) into total seconds."""
    match = _ISO_DURATION_RE.match(duration or "")
    if not match:
        return 0
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class YouTubeService:
    """Client for Google OAuth (read-only) and YouTube Data API v3."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _require_oauth_config(self) -> None:
        if not self.settings.google_client_id or not self.settings.google_client_secret:
            raise YouTubeAPIError(
                "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
            )

    def build_google_login_url(self, state: str) -> str:
        """Build the Google OpenID Connect login URL (profile/email only)."""
        self._require_oauth_config()
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": " ".join(LOGIN_SCOPES),
            "state": state,
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def build_youtube_connect_url(self, state: str) -> str:
        """Build the Google OAuth URL requesting ONLY read-only YouTube access."""
        self._require_oauth_config()
        scopes = (YOUTUBE_READONLY_SCOPE,)
        assert FORBIDDEN_UPLOAD_SCOPES.isdisjoint(scopes), (
            "Upload scopes are prohibited on the read-only connect flow"
        )
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "false",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def build_youtube_upload_connect_url(self, state: str) -> str:
        """Build the explicit opt-in Google OAuth URL requesting youtube.upload + youtube.readonly."""
        self._require_oauth_config()
        scopes = (YOUTUBE_READONLY_SCOPE, YOUTUBE_UPLOAD_SCOPE)
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange an OAuth authorization code for Google OAuth tokens."""
        self._require_oauth_config()
        payload = {
            "code": code,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            logger.error("Google token exchange network error: %s", type(exc).__name__)
            raise YouTubeAPIError("Unable to reach Google OAuth token endpoint.") from exc

        if resp.status_code != 200:
            logger.warning("Google token exchange failed with status %s", resp.status_code)
            raise YouTubeAPIError("Google OAuth token exchange failed.")
        return resp.json()

    def refresh_access_token(self, refresh_token: str) -> str:
        """Exchange a decrypted refresh token for a fresh short-lived access token.

        Raises YouTubeAuthRevokedError when Google reports invalid_grant / revoked access.
        """
        self._require_oauth_config()
        payload = {
            "refresh_token": refresh_token,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
            "grant_type": "refresh_token",
        }
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            logger.error("Google token refresh network error: %s", type(exc).__name__)
            raise YouTubeTransientUploadError(
                "Unable to reach Google OAuth token endpoint."
            ) from exc

        if resp.status_code != 200:
            err_code = ""
            try:
                err_code = str(resp.json().get("error", "")).lower()
            except ValueError:
                err_code = ""
            logger.warning(
                "Google token refresh failed with status %s (error=%s)",
                resp.status_code,
                err_code or "unknown",
            )
            if err_code in {"invalid_grant", "unauthorized_client", "access_denied"} or resp.status_code in {400, 401}:
                raise YouTubeAuthRevokedError(
                    "Your YouTube authorization has expired or was revoked. "
                    "Click 'Enable uploading' to reconnect your channel."
                )
            raise YouTubeTransientUploadError(
                "Temporary Google OAuth error while refreshing access token."
            )

        access_token = resp.json().get("access_token")
        if not access_token:
            raise YouTubeAPIError("Google token refresh returned no access token.")
        return str(access_token)

    def fetch_google_userinfo(self, access_token: str) -> dict[str, Any]:
        """Fetch authenticated Google user profile via OpenID Connect userinfo endpoint."""
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as exc:
            logger.error("Google userinfo network error: %s", type(exc).__name__)
            raise YouTubeAPIError("Unable to fetch Google user profile.") from exc

        if resp.status_code != 200:
            logger.warning("Google userinfo failed with status %s", resp.status_code)
            raise YouTubeAPIError("Failed to retrieve Google user profile.")
        return resp.json()

    def fetch_owned_channels(self, access_token: str) -> list[dict[str, Any]]:
        """List YouTube channels owned by the OAuth user (channels.list?part=snippet&mine=true)."""
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{YOUTUBE_API_BASE}/channels",
                    params={"part": "snippet", "mine": "true"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as exc:
            logger.error("YouTube channels.list network error: %s", type(exc).__name__)
            raise YouTubeAPIError("Unable to fetch YouTube channels.") from exc

        if resp.status_code != 200:
            logger.warning("YouTube channels.list failed with status %s", resp.status_code)
            raise YouTubeAPIError("YouTube Data API rejected the channels lookup.")

        items = resp.json().get("items", [])
        channels: list[dict[str, Any]] = []
        for item in items:
            snippet = item.get("snippet", {})
            thumbs = snippet.get("thumbnails", {})
            thumb_url = (
                thumbs.get("default", {}).get("url")
                or thumbs.get("medium", {}).get("url")
            )
            channels.append(
                {
                    "channel_id": item["id"],
                    "title": snippet.get("title", "Untitled Channel"),
                    "handle": snippet.get("customUrl"),
                    "thumbnail_url": thumb_url,
                }
            )
        return channels

    def fetch_video_metadata(
        self,
        video_id: str,
        *,
        access_token: str | None = None,
    ) -> VideoMetadata:
        """Fetch video snippet, status (including license), and duration via videos.list."""
        params: dict[str, str] = {
            "part": "status,snippet,contentDetails",
            "id": video_id,
        }
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        elif self.settings.youtube_api_key:
            params["key"] = self.settings.youtube_api_key
        else:
            raise YouTubeAPIError(
                "YouTube metadata lookup requires YOUTUBE_API_KEY or a connected YouTube channel."
            )

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{YOUTUBE_API_BASE}/videos",
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.error("YouTube videos.list network error: %s", type(exc).__name__)
            raise YouTubeAPIError("Unable to reach YouTube Data API.") from exc

        if resp.status_code != 200:
            logger.warning("YouTube videos.list failed with status %s", resp.status_code)
            raise YouTubeAPIError("YouTube Data API rejected the video metadata request.")

        items = resp.json().get("items", [])
        if not items:
            raise YouTubeAPIError(f"YouTube video '{video_id}' was not found or is private.")

        item = items[0]
        snippet = item.get("snippet", {})
        status = item.get("status", {})
        content_details = item.get("contentDetails", {})
        thumbs = snippet.get("thumbnails", {})
        thumb_url = (
            thumbs.get("high", {}).get("url")
            or thumbs.get("medium", {}).get("url")
            or thumbs.get("default", {}).get("url")
        )

        return VideoMetadata(
            video_id=video_id,
            channel_id=snippet.get("channelId", ""),
            channel_title=snippet.get("channelTitle", ""),
            title=snippet.get("title", ""),
            license=status.get("license", "youtube"),
            duration_sec=parse_iso8601_duration(content_details.get("duration", "")),
            privacy_status=status.get("privacyStatus", "public"),
            embeddable=bool(status.get("embeddable", True)),
            published_at=snippet.get("publishedAt"),
            thumbnail_url=thumb_url,
        )

    @staticmethod
    def _classify_upload_http_error(resp: httpx.Response) -> YouTubeAPIError:
        """Translate a YouTube Data API v3 upload HTTP error into a typed exception."""
        reasons: set[str] = set()
        api_message = ""
        try:
            err_obj = resp.json().get("error", {})
            api_message = str(err_obj.get("message", ""))
            for item in err_obj.get("errors", []):
                if isinstance(item, dict) and item.get("reason"):
                    reasons.add(str(item["reason"]).lower())
        except ValueError:
            pass

        quota_reasons = {"quotaexceeded", "dailylimitexceeded", "uploadlimitexceeded"}
        if resp.status_code == 403 and (
            reasons & quota_reasons or "quota" in api_message.lower()
        ):
            return YouTubeQuotaExceededError(
                "YouTube API daily upload quota exceeded. Please try again after the quota resets."
            )

        auth_reasons = {"autherror", "insufficientpermissions", "forbidden"}
        if resp.status_code == 401 or (
            resp.status_code == 403 and bool(reasons & auth_reasons)
        ):
            return YouTubeAuthRevokedError(
                "YouTube upload permission was revoked or is missing. "
                "Click 'Enable uploading' to reconnect your channel."
            )

        if resp.status_code in {429, 500, 502, 503, 504}:
            return YouTubeTransientUploadError(
                f"YouTube upload service temporarily unavailable (HTTP {resp.status_code})."
            )

        return YouTubeAPIError(
            f"YouTube rejected the video upload (HTTP {resp.status_code}): "
            f"{api_message[:180] or 'check worker logs for details.'}"
        )

    def upload_video_resumable(
        self,
        file_path: Path,
        *,
        access_token: str,
        title: str,
        description: str,
        tags: list[str],
        privacy: str = "private",
    ) -> str:
        """Upload a vertical Short MP4 via YouTube Data API v3 resumable upload protocol.

        Returns the newly created YouTube video ID.
        """
        if not file_path.is_file():
            raise YouTubeAPIError("Clip video file does not exist on disk.")

        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise YouTubeAPIError("Clip video file is empty.")

        metadata_body: dict[str, Any] = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(file_size),
            "X-Upload-Content-Type": "video/mp4",
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
                init_resp = client.post(
                    YOUTUBE_UPLOAD_API_URL,
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers=init_headers,
                    json=metadata_body,
                )
                if init_resp.status_code not in {200, 201}:
                    logger.warning(
                        "YouTube resumable upload init failed with status %s",
                        init_resp.status_code,
                    )
                    raise self._classify_upload_http_error(init_resp)

                upload_uri = init_resp.headers.get("Location") or init_resp.headers.get("location")
                if not upload_uri:
                    raise YouTubeAPIError(
                        "YouTube resumable upload did not return an upload session URI."
                    )

                with file_path.open("rb") as video_fp:
                    offset = 0
                    final_resp: httpx.Response | None = None
                    while offset < file_size:
                        chunk = video_fp.read(_RESUMABLE_CHUNK_SIZE)
                        if not chunk:
                            break
                        end_byte = offset + len(chunk) - 1
                        chunk_headers = {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        }
                        chunk_resp = client.put(
                            upload_uri,
                            headers=chunk_headers,
                            content=chunk,
                        )
                        if chunk_resp.status_code == 308:
                            offset = end_byte + 1
                            continue
                        if chunk_resp.status_code in {200, 201}:
                            final_resp = chunk_resp
                            break
                        logger.warning(
                            "YouTube resumable chunk upload failed with status %s",
                            chunk_resp.status_code,
                        )
                        raise self._classify_upload_http_error(chunk_resp)

                if final_resp is None:
                    raise YouTubeTransientUploadError(
                        "YouTube resumable upload ended without a completion response."
                    )
                video_id = final_resp.json().get("id")
                if not video_id:
                    raise YouTubeAPIError(
                        "YouTube upload completed but returned no video ID."
                    )
                return str(video_id)
        except httpx.HTTPError as exc:
            logger.error("YouTube resumable upload network error: %s", type(exc).__name__)
            raise YouTubeTransientUploadError(
                "Network error while uploading clip to YouTube."
            ) from exc


def _summarize_yt_dlp_error(stderr: str) -> str:
    """Convert raw yt-dlp stderr into a short, clear message suitable for the UI."""
    lowered = (stderr or "").lower()
    if "no supported javascript runtime" in lowered or "javascript runtime" in lowered:
        return (
            "Video download failed because the worker's JavaScript runtime (Deno) "
            "is unavailable. Rebuild the worker image and retry."
        )
    if "private video" in lowered:
        return "This YouTube video is private and cannot be downloaded."
    if "sign in to confirm" in lowered or "not a bot" in lowered or "age-restricted" in lowered:
        return "YouTube blocked automated download with a sign-in or bot check."
    if "video unavailable" in lowered or "this video is not available" in lowered:
        return "YouTube reported this video as unavailable for download."
    if "http error 429" in lowered or "too many requests" in lowered or "rate-limit" in lowered or "rate limit" in lowered:
        return "YouTube rate-limited the download. Please try again later."
    return "Failed to download the source video from YouTube."


def _build_yt_dlp_command(source_url: str, output_template: str) -> list[str]:
    """Construct the yt-dlp subprocess command with Deno JS runtime explicitly enabled."""
    deno_path = shutil.which("deno")
    js_runtime_arg = f"deno:{deno_path}" if deno_path else "deno"
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-progress",
        "--js-runtimes",
        js_runtime_arg,
        "-f",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        output_template,
        source_url,
    ]


def download_rights_verified_video(job: Job, output_dir: Path) -> Path:
    """Download a YouTube video via yt-dlp ONLY after enforcing the Rights Gate.

    Raises RightsGateError if the job has not passed the Rights Gate.
    """
    from app.services.rights import assert_job_rights_approved

    # NON-NEGOTIABLE GUARD: never invoke yt-dlp without verified rights
    assert_job_rights_approved(job)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / f"{job.youtube_video_id}.%(ext)s")
    cmd = _build_yt_dlp_command(job.source_url, output_template)
    logger.info("Downloading rights-verified video %s for job %s", job.youtube_video_id, job.id)
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "yt-dlp timed out downloading video %s for job %s",
            job.youtube_video_id,
            job.id,
        )
        raise VideoDownloadError(
            "Video download timed out before completing.",
            detail=f"yt-dlp timed out after {exc.timeout}s for video {job.youtube_video_id}",
        ) from exc
    except subprocess.CalledProcessError as exc:
        full_stderr = (exc.stderr or "").strip()
        full_stdout = (exc.stdout or "").strip()
        logger.error(
            "yt-dlp failed for job %s (video=%s, exit_code=%s)\nstderr:\n%s\nstdout:\n%s",
            job.id,
            job.youtube_video_id,
            exc.returncode,
            full_stderr,
            full_stdout,
        )
        raise VideoDownloadError(
            _summarize_yt_dlp_error(full_stderr),
            detail=full_stderr or f"yt-dlp exited with code {exc.returncode}",
        ) from exc
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        logger.error(
            "yt-dlp invocation error for job %s (video=%s): %s",
            job.id,
            job.youtube_video_id,
            exc,
        )
        raise VideoDownloadError(
            "Failed to start video downloader on worker.",
            detail=str(exc),
        ) from exc

    expected_mp4 = output_dir / f"{job.youtube_video_id}.mp4"
    if expected_mp4.is_file():
        return expected_mp4

    candidates = sorted(output_dir.glob(f"{job.youtube_video_id}.*"))
    if not candidates:
        raise VideoDownloadError(
            "Download completed but no video file was produced.",
            detail=f"No files matched {job.youtube_video_id}.* in {output_dir}",
        )
    return candidates[0]
