# Clipper — Project Specification

## Overview
Clipper is a rights-safe automated video clipping platform. Users paste a YouTube URL (optionally linked to a paid clipping campaign), the backend verifies the user has legal rights to clip that video, downloads and cuts vertical 9:16 short clips using a swappable clipping engine, and presents them for human review before any future publishing step.

## Non-Negotiable Rule: The Rights Gate
No clipping job may be queued or downloaded unless the source video passes the Rights Gate (`backend/app/services/rights.py`). Every job records both `rights_basis` and `rights_evidence_json` (plus `rejection_reason` if denied).

Allowed rights bases:
1. **`owner`** — The video's `channelId` matches one of the user's connected YouTube channels (`youtube_channels`).
2. **`campaign`** — The video URL (normalized YouTube video ID or channel) is explicitly listed in the selected active campaign's `campaign_sources` and satisfies campaign deadline/rules.
3. **`cc`** — YouTube Data API v3 `videos.list(part=status,snippet,contentDetails)` returns `status.license == "creativeCommon"`. Automatically sets `requires_attribution = true` and attaches mandatory attribution text to every generated clip.
4. **`permission`** — The user has uploaded a non-revoked, non-expired written permission document (`permissions`) covering the video's `channelId`.

If none match, the job is saved with status `rejected`, a human-readable `rejection_reason`, and an `audit_log` entry. It is never enqueued to Celery.

Defense-in-depth:
- The Celery worker re-checks the stored rights decision before calling `yt-dlp`.
- The future YouTube upload phase will re-run the full live rights check immediately before uploading.

## Job Lifecycle
```
pending_rights ──► rejected
       │
       └──► queued ──► processing ──► ready
                            │
                            └──► failed
```

## Architecture
- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2
- **Queue**: Celery + Redis
- **Database**: PostgreSQL
- **Media**: `yt-dlp` (post-rights-gate only) + `ffmpeg` (via `subprocess`)
- **Clipping Engines**: `ClipEngine` ABC (`engines/base.py`), `StubEngine` (`engines/stub.py` — evenly spaced 9:16 clips via ffmpeg), `LocalEngine` (`engines/local.py` — documented TODO for Whisper + LLM), designed for swappable third-party APIs.
- **Frontend**: Next.js (App Router), TypeScript, Tailwind CSS
- **Infrastructure**: Docker Compose (`postgres`, `redis`, `backend`, `worker`, `frontend`)

## Phased Delivery (Steps 1–8)
1. Repo scaffold, Docker Compose, health-check endpoints, README.
2. DB models + Alembic initial migration.
3. Google OAuth login + connect YouTube channel (`youtube.readonly` scope, encrypted refresh tokens).
4. Campaign CRUD API (multiple sources, rules text, deadline) + Permission document upload API.
5. `rights.py` implementing all 4 bases + comprehensive `pytest` suite.
6. `POST /jobs`, `GET /jobs`, `GET /jobs/{id}` with URL validation, metadata fetch, and rights gate.
7. Celery worker: gated download, `StubEngine`, clip persistence, status transitions, `audit_log`.
8. Minimal Next.js UI: login, New Job form, jobs list with status + rights-basis badges, clip preview grid, rejection display.

## Out of Scope (Current Phase)
- YouTube uploading (`youtube.upload` scope)
- Publishing schedules, analytics, billing
- Scraping third-party clipping websites or automating external logins

