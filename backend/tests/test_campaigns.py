"""Tests for Campaign CRUD API and Permission document upload API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_campaign_crud_with_multiple_sources_rules_and_deadline(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    deadline = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    # Create with 2 video sources + 1 channel source
    create_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "Q4 Podcast Clipping",
            "description": "Official creator podcast campaign",
            "rules_text": "No misleading titles. Include #PodcastClips.",
            "deadline": deadline,
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


def test_campaign_rejects_invalid_source_url(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "Bad Source Campaign",
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
            "notes": "Signed clipping license agreement",
        },
        files={"file": ("license.pdf", b"%PDF-1.4 written permission", "application/pdf")},
    )
    assert upload_resp.status_code == 201
    perm = upload_resp.json()
    assert perm["channel_id"] == "UC_PARTNER_CHANNEL_1234"
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

