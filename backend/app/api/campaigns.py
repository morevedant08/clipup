"""Campaign CRUD API with multi-source authorization, rules text, and deadlines."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user
from app.database import get_db
from app.models.campaign import Campaign, CampaignSource
from app.models.user import User
from app.schemas.campaign import (
    CampaignCreate,
    CampaignResponse,
    CampaignSourceCreate,
    CampaignSourceResponse,
    CampaignUpdate,
)
from app.services.audit import record_audit
from app.services.rules import normalize_campaign_source

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _build_source_model(
    campaign_id: uuid.UUID,
    payload: CampaignSourceCreate,
) -> CampaignSource:
    """Validate and construct a CampaignSource ORM object."""
    try:
        source_type, video_id, channel_id = normalize_campaign_source(
            payload.source_url,
            explicit_type=payload.source_type,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return CampaignSource(
        campaign_id=campaign_id,
        source_url=payload.source_url.strip(),
        normalized_video_id=video_id,
        source_channel_id=channel_id,
        source_type=source_type,
        title=payload.title,
    )


def _get_campaign_or_404(db: Session, campaign_id: uuid.UUID) -> Campaign:
    campaign = (
        db.query(Campaign)
        .options(selectinload(Campaign.sources))
        .filter(Campaign.id == campaign_id)
        .first()
    )
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found.",
        )
    return campaign


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    """Create a clipping campaign along with its authorized source URLs."""
    campaign = Campaign(
        owner_user_id=current_user.id,
        name=payload.name.strip(),
        description=payload.description,
        rules_text=payload.rules_text,
        deadline=payload.deadline,
        is_active=payload.is_active,
    )
    db.add(campaign)
    db.flush()

    for src in payload.sources:
        db.add(_build_source_model(campaign.id, src))

    record_audit(
        db,
        "campaign.created",
        user_id=current_user.id,
        details={
            "campaign_id": str(campaign.id),
            "name": campaign.name,
            "source_count": len(payload.sources),
        },
    )
    db.commit()
    return CampaignResponse.model_validate(_get_campaign_or_404(db, campaign.id))


@router.get("", response_model=list[CampaignResponse])
def list_campaigns(
    active_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CampaignResponse]:
    """List campaigns available to the authenticated user."""
    query = (
        db.query(Campaign)
        .options(selectinload(Campaign.sources))
        .order_by(Campaign.created_at.desc())
    )
    if active_only:
        query = query.filter(Campaign.is_active.is_(True))
    campaigns = query.all()
    return [CampaignResponse.model_validate(c) for c in campaigns]


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_campaign(
    campaign_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    """Fetch a single campaign and its authorized sources."""
    campaign = _get_campaign_or_404(db, campaign_id)
    return CampaignResponse.model_validate(campaign)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    """Update campaign details, rules, deadline, or replace its source list."""
    campaign = _get_campaign_or_404(db, campaign_id)
    if campaign.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the campaign owner may modify this campaign.",
        )

    update_fields = payload.model_dump(exclude_unset=True)
    sources_payload = update_fields.pop("sources", None)

    for field_name, value in update_fields.items():
        if field_name == "name" and value is not None:
            value = value.strip()
        setattr(campaign, field_name, value)

    if sources_payload is not None:
        campaign.sources.clear()
        db.flush()
        for src in payload.sources or []:
            campaign.sources.append(_build_source_model(campaign.id, src))

    record_audit(
        db,
        "campaign.updated",
        user_id=current_user.id,
        details={"campaign_id": str(campaign.id)},
    )
    db.commit()
    return CampaignResponse.model_validate(_get_campaign_or_404(db, campaign.id))


@router.post(
    "/{campaign_id}/sources",
    response_model=CampaignSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_campaign_source(
    campaign_id: uuid.UUID,
    payload: CampaignSourceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignSourceResponse:
    """Append a new authorized source URL to an existing campaign."""
    campaign = _get_campaign_or_404(db, campaign_id)
    if campaign.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the campaign owner may add sources.",
        )

    source = _build_source_model(campaign.id, payload)
    campaign.sources.append(source)
    record_audit(
        db,
        "campaign.source_added",
        user_id=current_user.id,
        details={"campaign_id": str(campaign.id), "source_url": source.source_url},
    )
    db.commit()
    db.refresh(source)
    return CampaignSourceResponse.model_validate(source)


@router.delete(
    "/{campaign_id}/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_campaign_source(
    campaign_id: uuid.UUID,
    source_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Remove an authorized source from a campaign."""
    campaign = _get_campaign_or_404(db, campaign_id)
    if campaign.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the campaign owner may remove sources.",
        )

    source = next((s for s in campaign.sources if s.id == source_id), None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign source not found.",
        )

    campaign.sources.remove(source)
    record_audit(
        db,
        "campaign.source_removed",
        user_id=current_user.id,
        details={"campaign_id": str(campaign.id), "source_id": str(source_id)},
    )
    db.commit()


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(
    campaign_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a campaign owned by the current user."""
    campaign = _get_campaign_or_404(db, campaign_id)
    if campaign.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the campaign owner may delete this campaign.",
        )

    db.delete(campaign)
    record_audit(
        db,
        "campaign.deleted",
        user_id=current_user.id,
        details={"campaign_id": str(campaign_id)},
    )
    db.commit()
