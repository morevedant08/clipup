"""YouTube URL validation, Google OAuth (read-only), and YouTube Data API v3 service."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

LOGIN_SCOPES: tuple[str, ...] = ("openid", "email", "profile")
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
# Explicitly forbidden in Phase 1 (no upload scope yet).
FORBIDDEN_UPLOAD_SCOPES: frozenset[str] = frozenset(
    {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    }
)

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
        assert FORBIDDEN_UPLOAD_SCOPES.isdisjoint(scopes), "Upload scopes are prohibited in Phase 1"
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.youtube_connect_redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "false",
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
        """Exchange a decrypted refresh token for a fresh short-lived access token."""
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
            raise YouTubeAPIError("Unable to refresh Google OAuth token.") from exc

        if resp.status_code != 200:
            logger.warning("Google token refresh failed with status %s", resp.status_code)
            raise YouTubeAPIError("Google OAuth token refresh failed.")
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
