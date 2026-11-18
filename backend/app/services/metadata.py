"""Clip metadata formatting and mandatory Creative Commons attribution helpers."""

from __future__ import annotations

from typing import Any

from app.models.job import RightsBasis
from app.services.youtube import VideoMetadata, normalize_youtube_video_url


def build_cc_attribution(video: VideoMetadata) -> str:
    """Generate mandatory attribution text for Creative Commons (CC BY) YouTube videos."""
    canonical_url = normalize_youtube_video_url(video.video_id)
    author = video.channel_title.strip() or video.channel_id or "Original Creator"
    title = video.title.strip() or video.video_id
    return (
        f'Source: "{title}" by {author} ({canonical_url}) — '
        "Licensed under Creative Commons Attribution (CC BY)."
    )


def build_clip_title(source_title: str | None, clip_index: int, total_clips: int) -> str:
    """Create a concise vertical clip title bounded to 100 characters."""
    base = (source_title or "Clip").strip()
    suffix = f" (Part {clip_index}/{total_clips})" if total_clips > 1 else " (Clip)"
    max_base = max(10, 100 - len(suffix))
    if len(base) > max_base:
        base = base[: max_base - 1].rstrip() + "…"
    return f"{base}{suffix}"


def build_clip_metadata(
    video: VideoMetadata,
    rights_basis: RightsBasis | str,
    clip_index: int,
    total_clips: int,
    *,
    campaign_rules: str | None = None,
) -> dict[str, Any]:
    """Build clip metadata including mandatory attribution for Creative Commons jobs."""
    basis_value = rights_basis.value if isinstance(rights_basis, RightsBasis) else str(rights_basis)
    requires_attribution = basis_value == RightsBasis.CC.value
    attribution_text = build_cc_attribution(video) if requires_attribution else None

    return {
        "title": build_clip_title(video.title, clip_index, total_clips),
        "requires_attribution": requires_attribution,
        "attribution_text": attribution_text,
        "campaign_rules": campaign_rules or None,
    }
