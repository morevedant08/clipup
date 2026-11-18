"""Clip metadata formatting, CC attribution, and YouTube Shorts upload metadata helpers."""

from __future__ import annotations

import re
from typing import Any

from app.models.job import RightsBasis
from app.services.youtube import VideoMetadata, normalize_youtube_video_url

_HASHTAG_RE = re.compile(r"(?<!\w)(#[A-Za-z0-9_]{2,50})")
MAX_YOUTUBE_TITLE_LEN = 100
MAX_YOUTUBE_DESCRIPTION_LEN = 5000


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
    max_base = max(10, MAX_YOUTUBE_TITLE_LEN - len(suffix))
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
    basis_value = (
        rights_basis.value if isinstance(rights_basis, RightsBasis) else str(rights_basis)
    )
    requires_attribution = basis_value == RightsBasis.CC.value
    attribution_text = build_cc_attribution(video) if requires_attribution else None

    return {
        "title": build_clip_title(video.title, clip_index, total_clips),
        "requires_attribution": requires_attribution,
        "attribution_text": attribution_text,
        "campaign_rules": campaign_rules or None,
    }


def extract_campaign_hashtags(rules_text: str | None) -> list[str]:
    """Extract required #hashtags declared in a campaign's rules text (order-preserving)."""
    if not rules_text:
        return []
    seen: set[str] = set()
    hashtags: list[str] = []
    for match in _HASHTAG_RE.finditer(rules_text):
        tag = match.group(1)
        lowered = tag.lower()
        if lowered == "#shorts" or lowered in seen:
            continue
        seen.add(lowered)
        hashtags.append(tag)
    return hashtags


def compose_upload_metadata(
    *,
    title: str | None,
    description: str | None,
    tags: list[str] | None,
    rights_basis: RightsBasis | str,
    fallback_title: str | None = None,
    cc_attribution_text: str | None = None,
    campaign_rules_text: str | None = None,
) -> dict[str, Any]:
    """Compose final YouTube Shorts upload metadata.

    Guarantees:
      - Title is non-empty and <= 100 chars.
      - Description includes `#Shorts`.
      - Description includes required campaign hashtags when a campaign is linked.
      - Description automatically includes mandatory CC attribution text when rights_basis == "cc".
      - Tags are deduplicated and include "Shorts".
    """
    final_title = (title or fallback_title or "Clipper Short").strip()
    if len(final_title) > MAX_YOUTUBE_TITLE_LEN:
        final_title = final_title[: MAX_YOUTUBE_TITLE_LEN - 1].rstrip() + "…"

    user_desc = (description or "").strip()
    lowered_desc = user_desc.lower()

    required_hashtags: list[str] = []
    if "#shorts" not in lowered_desc:
        required_hashtags.append("#Shorts")

    for tag in extract_campaign_hashtags(campaign_rules_text):
        if tag.lower() not in lowered_desc:
            required_hashtags.append(tag)

    basis_value = (
        rights_basis.value if isinstance(rights_basis, RightsBasis) else str(rights_basis)
    )
    footer_blocks: list[str] = []
    if required_hashtags:
        footer_blocks.append(" ".join(required_hashtags))

    if basis_value == RightsBasis.CC.value and cc_attribution_text:
        clean_attr = cc_attribution_text.strip()
        if clean_attr and clean_attr not in user_desc:
            footer_blocks.append(clean_attr)

    footer = "\n\n".join(footer_blocks)
    if user_desc and footer:
        max_user_len = MAX_YOUTUBE_DESCRIPTION_LEN - len(footer) - 2
        if len(user_desc) > max_user_len:
            user_desc = user_desc[: max(0, max_user_len - 1)].rstrip() + "…"
        final_description = f"{user_desc}\n\n{footer}"
    elif footer:
        final_description = footer[:MAX_YOUTUBE_DESCRIPTION_LEN]
    else:
        final_description = user_desc[:MAX_YOUTUBE_DESCRIPTION_LEN]

    cleaned_tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in (tags or []):
        tag_val = raw_tag.strip().lstrip("#")
        if not tag_val or tag_val.lower() in seen_tags:
            continue
        seen_tags.add(tag_val.lower())
        cleaned_tags.append(tag_val[:60])

    if "shorts" not in seen_tags:
        cleaned_tags.append("Shorts")

    return {
        "title": final_title,
        "description": final_description,
        "tags": cleaned_tags[:30],
    }
