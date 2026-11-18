"""Campaign rules, deadline checks, and source URL normalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.campaign import Campaign
from app.services.youtube import (
    YouTubeURLValidationError,
    extract_youtube_channel_id,
    extract_youtube_video_id,
)


@dataclass(frozen=True, slots=True)
class CampaignRuleEvaluation:
    """Result of validating whether a campaign can currently accept clipping jobs."""

    is_eligible: bool
    reason: str
    rules_text: str
    deadline: datetime | None


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize naive datetimes (e.g. from SQLite) to UTC for safe comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_campaign_eligibility(
    campaign: Campaign,
    *,
    now: datetime | None = None,
) -> CampaignRuleEvaluation:
    """Validate whether a campaign is active and before its submission deadline."""
    current_time = _ensure_utc(now or datetime.now(timezone.utc))

    if not campaign.is_active:
        return CampaignRuleEvaluation(
            is_eligible=False,
            reason=f"Campaign '{campaign.name}' is inactive and no longer accepting clips.",
            rules_text=campaign.rules_text,
            deadline=campaign.deadline,
        )

    if campaign.deadline is not None:
        deadline_utc = _ensure_utc(campaign.deadline)
        if current_time > deadline_utc:
            return CampaignRuleEvaluation(
                is_eligible=False,
                reason=(
                    f"Campaign '{campaign.name}' expired on "
                    f"{deadline_utc.strftime('%Y-%m-%d %H:%M UTC')}."
                ),
                rules_text=campaign.rules_text,
                deadline=deadline_utc,
            )

    return CampaignRuleEvaluation(
        is_eligible=True,
        reason="Campaign is active and within its deadline.",
        rules_text=campaign.rules_text,
        deadline=_ensure_utc(campaign.deadline) if campaign.deadline else None,
    )


def normalize_campaign_source(
    source_url: str,
    explicit_type: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Validate and normalize a campaign source URL into (source_type, video_id, channel_id).

    Raises ValueError if the URL is neither a valid YouTube video URL nor a channel ID URL.
    """
    cleaned = source_url.strip()
    if explicit_type in (None, "video"):
        try:
            video_id = extract_youtube_video_id(cleaned)
            return ("video", video_id, None)
        except YouTubeURLValidationError:
            if explicit_type == "video":
                raise

    channel_id = extract_youtube_channel_id(cleaned)
    if channel_id:
        return ("channel", None, channel_id)

    raise ValueError(
        "Campaign source URL must be a valid YouTube video URL "
        "(e.g. https://www.youtube.com/watch?v=...) or channel URL "
        "(https://www.youtube.com/channel/UC...)."
    )
