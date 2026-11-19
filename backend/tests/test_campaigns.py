"""Tests for Campaign CRUD API and Permission document upload API."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.security import create_access_token


def test_campaign_crud_with_multiple_sources_rules_and_deadline(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    deadline = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    # Create with 2 video sources + 1 channel source + proof URL & attestation
    create_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "Q4 Podcast Clipping",
            "description": "Official creator podcast campaign",
            "rules_text": "No misleading titles. Include #PodcastClips.",
            "deadline": deadline,
            "proof_url": "https://creator.example.com/podcast-terms",
            "attested": True,
            "sources": [
                {
                    "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "title": "Episode 1",
                },
                {
                    "source_url": "https://youtu.be/9bZkp7q19f0",
                    "title": "Episode 2",
                },
                {
                    "source_url": "https://www.youtube.com/channel/UC1234567890123456789012",
                    "title": "Official Channel",
                },
            ],
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    campaign_id = created["id"]
    assert created["name"] == "Q4 Podcast Clipping"
    assert created["proof_url"] == "https://creator.example.com/podcast-terms"
    assert created["attested_at"] is not None
    assert len(created["sources"]) == 3
    video_ids = {s["normalized_video_id"] for s in created["sources"]}
    assert "dQw4w9WgXcQ" in video_ids
    assert "9bZkp7q19f0" in video_ids
    channel_ids = {s["source_channel_id"] for s in created["sources"]}
    assert "UC1234567890123456789012" in channel_ids

    # List
    list_resp = client.get("/api/campaigns", headers=auth_headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # Add source
    add_src_resp = client.post(
        f"/api/campaigns/{campaign_id}/sources",
        headers=auth_headers,
        json={"source_url": "https://www.youtube.com/shorts/abcdefghijk"},
    )
    assert add_src_resp.status_code == 201
    added_source = add_src_resp.json()
    assert added_source["normalized_video_id"] == "abcdefghijk"

    # Update rules & name
    patch_resp = client.patch(
        f"/api/campaigns/{campaign_id}",
        headers=auth_headers,
        json={"name": "Q4 Podcast Clipping v2", "rules_text": "Updated rules."},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "Q4 Podcast Clipping v2"
    assert len(patch_resp.json()["sources"]) == 4

    # Remove added source
    rm_resp = client.delete(
        f"/api/campaigns/{campaign_id}/sources/{added_source['id']}",
        headers=auth_headers,
    )
    assert rm_resp.status_code == 204

    # Verify remaining sources
    get_resp = client.get(f"/api/campaigns/{campaign_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert len(get_resp.json()["sources"]) == 3

    # Delete campaign
    del_resp = client.delete(f"/api/campaigns/{campaign_id}", headers=auth_headers)
    assert del_resp.status_code == 204
    assert client.get(f"/api/campaigns/{campaign_id}", headers=auth_headers).status_code == 404


def test_create_campaign_requires_proof_and_attestation(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Campaign creation must require proof of authorization and attestation checkbox."""
    # Missing proof
    no_proof_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "No Proof Campaign",
            "attested": True,
            "sources": [{"source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}],
        },
    )
    assert no_proof_resp.status_code == 422
    assert "proof is required" in no_proof_resp.json()["detail"].lower()

    # Missing attestation
    no_attest_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "No Attest Campaign",
            "proof_url": "https://creator.example.com/terms",
            "attested": False,
            "sources": [{"source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}],
        },
    )
    assert no_attest_resp.status_code == 422
    assert "attestation is required" in no_attest_resp.json()["detail"].lower()


def test_create_campaign_with_uploaded_proof_file(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Campaigns can be created with an uploaded proof file (storing its sha256)."""
    resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        data={
            "name": "File Proof Campaign",
            "rules_text": "Proof in document",
            "attested": "true",
            "sources": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        },
        files={"proof_file": ("campaign_terms.pdf", b"%PDF-1.4 proof agreement", "application/pdf")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "File Proof Campaign"
    assert len(data["proof_document_sha256"]) == 64
    assert data["attested_at"] is not None


def test_other_users_campaign_endpoints_return_404(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
) -> None:
    """Campaign endpoints must strictly scope to owner and return 404 for other users."""
    # User A creates a campaign
    create_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "User A Campaign",
            "proof_url": "https://creator.example.com/terms",
            "attested": True,
            "sources": [{"source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}],
        },
    )
    assert create_resp.status_code == 201
    campaign_id = create_resp.json()["id"]

    # Create User B in DB and generate auth headers
    user_b = User(
        id=uuid.uuid4(),
        email="user_b@example.com",
        google_sub="sub_user_b",
    )
    db_session.add(user_b)
    db_session.commit()
    other_token = create_access_token(user_b.id, user_b.email)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    # User B list -> does NOT see User A's campaign
    list_b = client.get("/api/campaigns", headers=other_headers)
    assert list_b.status_code == 200
    assert not any(c["id"] == campaign_id for c in list_b.json())

    # User B GET -> 404
    assert client.get(f"/api/campaigns/{campaign_id}", headers=other_headers).status_code == 404

    # User B PATCH -> 404
    assert (
        client.patch(
            f"/api/campaigns/{campaign_id}",
            headers=other_headers,
            json={"name": "Hacked"},
        ).status_code
        == 404
    )

    # User B add source -> 404
    assert (
        client.post(
            f"/api/campaigns/{campaign_id}/sources",
            headers=other_headers,
            json={"source_url": "https://www.youtube.com/watch?v=9bZkp7q19f0"},
        ).status_code
        == 404
    )

    # User B DELETE -> 404
    assert client.delete(f"/api/campaigns/{campaign_id}", headers=other_headers).status_code == 404


def test_campaign_rejects_invalid_source_url(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "Bad Source Campaign",
            "proof_url": "https://creator.example.com/terms",
            "attested": True,
            "sources": [{"source_url": "https://example.com/not-youtube"}],
        },
    )
    assert resp.status_code == 422


def test_permission_document_upload_and_revoke(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    upload_resp = client.post(
        "/api/permissions",
        headers=auth_headers,
        data={
            "channel_id": "UC_PARTNER_CHANNEL_1234",
            "channel_name": "Partner Studio",
            "scope": "Shorts clipping up to 60s",
            "notes": "Signed clipping license agreement",
        },
        files={"file": ("license.pdf", b"%PDF-1.4 written permission", "application/pdf")},
    )
    assert upload_resp.status_code == 201
    perm = upload_resp.json()
    assert perm["channel_id"] == "UC_PARTNER_CHANNEL_1234"
    assert perm["scope"] == "Shorts clipping up to 60s"
    assert perm["revoked"] is False
    assert len(perm["document_sha256"]) == 64

    list_resp = client.get("/api/permissions", headers=auth_headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    revoke_resp = client.post(
        f"/api/permissions/{perm['id']}/revoke",
        headers=auth_headers,
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["revoked"] is True
