# Clipper

Rights-safe automated video clipping platform.

Paste a YouTube URL (optionally linked to a paid clipping campaign), let Clipper verify your legal right to clip the video through a strict **Rights Gate**, generate vertical 9:16 short clips via a swappable clipping engine, and review them before any future publishing workflow.

## Non-Negotiable Rights Gate

No clipping job can be queued, downloaded, or processed unless the source video passes at least one of four allowed rights bases (evaluated in `backend/app/services/rights.py` and re-checked inside the Celery worker before `yt-dlp` runs):

1. **`owner`** — The video's YouTube `channelId` matches one of your connected YouTube channels (`youtube_channels`, refresh tokens stored Fernet-encrypted).
2. **`campaign`** — The video URL (normalized video ID or channel) is listed in the selected active campaign's authorized sources (`campaign_sources`) and is within the campaign deadline.
3. **`cc`** — YouTube Data API v3 (`videos.list`, `part=status,snippet,contentDetails`) returns `status.license == "creativeCommon"`. Automatically sets `requires_attribution = true` and attaches mandatory attribution text to every generated clip.
4. **`permission`** — You uploaded a non-revoked, non-expired written permission document (`permissions`) covering the video's `channelId`.

If none match, the job is saved with `status = "rejected"`, a human-readable `rejection_reason`, full `rights_evidence_json`, and an `audit_log` entry. It is never enqueued to Celery.

### Optional Source File Upload Path

`POST /api/jobs` accepts either JSON (to download the YouTube URL via `yt-dlp` after rights verification) or `multipart/form-data` with an optional `source_file` (`.mp4`, `.mov`, or `.webm`, capped by `MAX_UPLOAD_SIZE_MB`, default `500` MB):

- **Rights Gate still runs first** on the YouTube URL. If the URL fails the Rights Gate, any uploaded file is immediately discarded and never written to disk.
- Validated files (checked by extension, MIME type, container magic bytes, and streaming byte limit) are stored under `${STORAGE_ROOT}/uploads/<job_id>/source.<ext>` outside any public directory, and file contents are never logged.
- When a job uses an uploaded source file, `audit_log` records `job.source_uploaded` and `job.source_ready`, and the Celery worker skips `yt-dlp` and runs the configured `ClipEngine` directly on the uploaded file.
- In the UI, the **New Clipping Job** form includes an optional **Upload source file instead of downloading** picker, and jobs that fail due to YouTube sign-in, bot-check, or rate-limit blocks display a hint suggesting the upload fallback.

## Project Structure

```text
.
├── backend/
│   ├── alembic/                  # Alembic migrations (0001_initial_schema)
│   ├── app/
│   │   ├── api/                  # health, auth, campaigns, permissions, jobs
│   │   ├── models/               # SQLAlchemy 2.x models
│   │   ├── schemas/              # Pydantic v2 request/response schemas
│   │   ├── services/             # rights.py, youtube.py, rules.py, metadata.py, audit.py
│   │   │   └── engines/          # ClipEngine ABC, StubEngine (ffmpeg 9:16), LocalEngine (TODO)
│   │   ├── workers/              # Celery app & process_clipping_job task
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── security.py           # JWT + OAuth state + Fernet refresh-token encryption
│   │   └── main.py
│   └── tests/                    # pytest unit & integration tests
├── frontend/                     # Next.js (App Router) + TypeScript + Tailwind CSS
├── docker-compose.yml
├── .env.example
└── PROJECT_SPEC.md
```

## Quick Start (Docker Compose)

1. Copy `.env.example` to `.env` and fill in secrets:

   ```bash
   cp .env.example .env
   ```

   Generate a Fernet encryption key and JWT secret:

   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Set `TOKEN_ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `YOUTUBE_API_KEY` in `.env`.

2. Start PostgreSQL, Redis, FastAPI backend, Celery worker, and Next.js frontend:

   ```bash
   docker compose up --build
   ```

3. Open the services:
   - Frontend UI: http://localhost:3000
   - Backend OpenAPI docs: http://localhost:8000/docs
   - Health check: http://localhost:8000/health
   - Readiness check: http://localhost:8000/health/ready

## Local Development (Without Docker)

### Backend API

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

### Celery Worker & Beat Scheduler

```bash
cd backend
source .venv/bin/activate
celery -A app.workers.celery_app:celery_app worker --loglevel=info
celery -A app.workers.celery_app:celery_app beat --loglevel=info
```

In Docker Compose, the `beat` service runs automatically alongside `worker` to dispatch scheduled YouTube Shorts uploads every 60 seconds.

### Backend Tests (pytest)

Runs unit tests for the 4-basis Rights Gate, Alembic upgrade/downgrade migration tests, Google OAuth + encrypted token tests, Campaign CRUD tests, Jobs API tests, and end-to-end Celery worker + `StubEngine` real `ffmpeg` 9:16 clipping tests:

```bash
cd backend
.venv/bin/pytest -v
```

### Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev
```

Verify types and production build:

```bash
cd frontend
npm run typecheck
npm run build
```

## Google OAuth Setup (Read-Only + Opt-In Uploading)

In Google Cloud Console (**APIs & Services**):
1. Enable **YouTube Data API v3**.
2. Create (or open) your OAuth 2.0 Web Application client and ensure this exact Authorized Redirect URI is registered:
   - `http://localhost:8000/auth/google/callback`
3. Configure the **OAuth Consent Screen** (`Google Auth Platform -> Data Access / Scopes`):
   - `openid`, `email`, `profile` (Google sign-in)
   - `https://www.googleapis.com/auth/youtube.readonly` (read-only channel connect)
   - `https://www.googleapis.com/auth/youtube.upload` (explicit opt-in **Enable uploading** flow)
4. While the app is in **Testing** publishing status (`Google Auth Platform -> Audience`), add every Google account you want to test with under **Test users** (otherwise Google blocks OAuth with `Error 403: access_denied`).
   - Note: Unverified OAuth apps in Testing mode have YouTube API uploads automatically restricted to `private` until Google app verification is completed.
5. Sign-in (`google_login`), read-only channel connect (`youtube_connect`), and opt-in upload enablement (`youtube_upload_connect`) all share `/auth/google/callback` using a signed, short-lived, `HttpOnly`-cookie session-bound `state` token. Refresh tokens are stored Fernet-encrypted via `TOKEN_ENCRYPTION_KEY`.

## Phase 3: Uploading Approved Clips to YouTube as Shorts

- **Explicit approval required**: Only clips with `review_status == "approved"` can be uploaded (never auto-uploaded).
- **Live pre-upload Rights Gate re-check**: Rights are re-verified immediately before queuing and again inside the Celery worker immediately before uploading (blocking expired campaigns, revoked permissions, disconnected channels, or changed YouTube licenses).
- **Resumable upload & metadata**: Uses YouTube Data API v3 resumable uploads, defaults privacy to `private` (`private`, `unlisted`, or `public` selectable per upload), lets users edit title/description/tags before uploading, automatically adds `#Shorts`, attaches mandatory CC BY attribution for `cc` jobs, and appends required `#hashtags` from linked campaign rules.
- **Scheduling & reliability**: Users can optionally set a future publish date/time (dispatched by Celery Beat), transient errors retry with exponential backoff, quota exhaustion surfaces a clear quota error, and revoked/expired OAuth tokens automatically revoke the channel's upload-enabled state so the user is prompted to re-authorize.

## Core API Endpoints

- `GET /health`, `GET /health/ready`
- `GET /auth/google/callback` (root-level shared OAuth callback redirecting back to `http://localhost:3000`)
- `GET /api/auth/google/login`, `GET /api/auth/me`
- `GET /api/auth/youtube/connect`, `GET /api/auth/youtube/enable-upload`, `GET /api/auth/youtube/channels`, `DELETE /api/auth/youtube/channels/{channel_id}`
- `POST /api/campaigns`, `GET /api/campaigns`, `GET /api/campaigns/{id}`, `PATCH /api/campaigns/{id}`, `DELETE /api/campaigns/{id}`
- `POST /api/campaigns/{id}/sources`, `DELETE /api/campaigns/{id}/sources/{source_id}`
- `POST /api/permissions`, `GET /api/permissions`, `POST /api/permissions/{id}/revoke`
- `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}`
- `PATCH /api/jobs/{id}/clips/{clip_id}/review`, `GET /api/jobs/{id}/clips/{clip_id}/media`
- `POST /api/jobs/{id}/clips/{clip_id}/upload`, `GET /api/uploads`, `GET /api/uploads/{id}`
