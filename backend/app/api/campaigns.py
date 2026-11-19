"""Campaign CRUD API with multi-source authorization, proof of authorization, and deadlines."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models.base import utcnow
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

ALLOWED_PROOF_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".txt", ".docx", ".html"})
MAX_PROOF_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


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


def _get_campaign_or_404(
    db: Session,
    campaign_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> Campaign:
    """Retrieve campaign scoped strictly to owner; return 404 for other users' campaigns."""
    campaign = (
        db.query(Campaign)
        .options(selectinload(Campaign.sources))
        .filter(
            Campaign.id == campaign_id,
            Campaign.owner_user_id == owner_user_id,
        )
        .first()
    )
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found.",
        )
    return campaign


async def _parse_campaign_create_request(
    request: Request,
    user_id: uuid.UUID,
) -> tuple[CampaignCreate, str | None, str | None]:
    """Parse campaign creation payload from JSON or multipart form-data, handling proof uploads."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        name = form.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="name is required.",
            )

        description = form.get("description") if isinstance(form.get("description"), str) else None
        rules_text = str(form.get("rules_text") or "")
        deadline_raw = form.get("deadline")
        deadline = (
            datetime.fromisoformat(deadline_raw)
            if isinstance(deadline_raw, str) and deadline_raw.strip()
            else None
        )
        is_active = form.get("is_active") not in ("false", "0", False)
        proof_url_raw = form.get("proof_url")
        proof_url = (
            proof_url_raw.strip()
            if isinstance(proof_url_raw, str) and proof_url_raw.strip()
            else None
        )
        attested_raw = form.get("attested")
        attested = attested_raw in ("true", "1", True, "on")

        sources_raw = form.get("sources")
        sources_list: list[CampaignSourceCreate] = []
        if isinstance(sources_raw, str) and sources_raw.strip():
            try:
                parsed_sources = json.loads(sources_raw)
                if isinstance(parsed_sources, list):
                    for item in parsed_sources:
                        if isinstance(item, dict):
                            sources_list.append(CampaignSourceCreate.model_validate(item))
                        elif isinstance(item, str) and item.strip():
                            sources_list.append(CampaignSourceCreate(source_url=item.strip()))
            except (json.JSONDecodeError, ValidationError):
                # Fallback: newline separated URLs
                for line in sources_raw.splitlines():
                    cleaned = line.strip()
                    if cleaned:
                        sources_list.append(CampaignSourceCreate(source_url=cleaned))

        raw_file = form.get("proof_file") or form.get("file")
        proof_doc_path: str | None = None
        proof_doc_sha256: str | None = None
        if isinstance(raw_file, StarletteUploadFile) and raw_file.filename:
            orig_name = Path(raw_file.filename).name
            ext = Path(orig_name).suffix.lower()
            if ext not in ALLOWED_PROOF_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Unsupported proof document format. Allowed extensions: "
                        + ", ".join(sorted(ALLOWED_PROOF_EXTENSIONS))
                    ),
                )
            content = await raw_file.read()
            if not content:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded campaign proof document is empty.",
                )
            if len(content) > MAX_PROOF_FILE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Campaign proof document exceeds 10 MB maximum size.",
                )
            proof_doc_sha256 = hashlib.sha256(content).hexdigest()
            settings = get_settings()
            proof_dir = settings.storage_path / "campaign_proofs" / str(user_id)
            proof_dir.mkdir(parents=True, exist_ok=True)
            stored_filename = f"{uuid.uuid4().hex}{ext}"
            (proof_dir / stored_filename).write_bytes(content)
            proof_doc_path = str(Path("campaign_proofs") / str(user_id) / stored_filename)

        payload = CampaignCreate(
            name=name.strip(),
            description=description,
            rules_text=rules_text,
            deadline=deadline,
            is_active=is_active,
            sources=sources_list,
            proof_url=proof_url,
            proof_document_path=proof_doc_path,
            proof_document_sha256=proof_doc_sha256,
            attested=attested,
        )
        return payload, proof_doc_path, proof_doc_sha256

    # JSON path
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON or multipart/form-data.",
        ) from exc

    try:
        payload = CampaignCreate.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc

    return payload, payload.proof_document_path, payload.proof_document_sha256


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    """Create a clipping campaign requiring terms URL or proof document + attestation timestamp."""
    payload, proof_doc_path, proof_doc_sha256 = await _parse_campaign_create_request(
        request, current_user.id
    )

    # NON-NEGOTIABLE: Campaign basis requires proof (URL or file) + attestation checkbox
    has_proof = bool(payload.proof_url and payload.proof_url.strip()) or bool(proof_doc_sha256)
    if not payload.attested:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Campaign attestation is required: must attest rights authorization.",
        )
    if not has_proof:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Campaign proof is required: provide a terms URL (proof_url) or uploaded proof document.",
        )

    now_utc = utcnow()
    campaign = Campaign(
        owner_user_id=current_user.id,
        name=payload.name.strip(),
        description=payload.description,
        rules_text=payload.rules_text,
        deadline=payload.deadline,
        is_active=payload.is_active,
        proof_url=payload.proof_url.strip() if payload.proof_url else None,
        proof_document_path=proof_doc_path,
        proof_document_sha256=proof_doc_sha256,
        attested_at=now_utc,
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
            "proof_url": campaign.proof_url,
            "proof_document_sha256": campaign.proof_document_sha256,
            "attested_at": campaign.attested_at.isoformat() if campaign.attested_at else None,
        },
    )
    db.commit()
    return CampaignResponse.model_validate(_get_campaign_or_404(db, campaign.id, current_user.id))


@router.get("", response_model=list[CampaignResponse])
def list_campaigns(
    active_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CampaignResponse]:
    """List campaigns owned by the authenticated user."""
    query = (
        db.query(Campaign)
        .options(selectinload(Campaign.sources))
        .filter(Campaign.owner_user_id == current_user.id)
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
    """Fetch a single campaign and its authorized sources (strictly scoped to owner; 404 otherwise)."""
    campaign = _get_campaign_or_404(db, campaign_id, owner_user_id=current_user.id)
    return CampaignResponse.model_validate(campaign)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    """Update campaign details, rules, deadline, or replace its source list."""
    campaign = _get_campaign_or_404(db, campaign_id, owner_user_id=current_user.id)

    update_fields = payload.model_dump(exclude_unset=True)
    sources_payload = update_fields.pop("sources", None)
    attested_update = update_fields.pop("attested", None)
    if attested_update is True:
        campaign.attested_at = utcnow()

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
    return CampaignResponse.model_validate(_get_campaign_or_404(db, campaign.id, current_user.id))


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
    campaign = _get_campaign_or_404(db, campaign_id, owner_user_id=current_user.id)
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
    campaign = _get_campaign_or_404(db, campaign_id, owner_user_id=current_user.id)
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
    campaign = _get_campaign_or_404(db, campaign_id, owner_user_id=current_user.id)
    db.delete(campaign)
    record_audit(
        db,
        "campaign.deleted",
        user_id=current_user.id,
        details={"campaign_id": str(campaign_id)},
    )
    db.commit()
