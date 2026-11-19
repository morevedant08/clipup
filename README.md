# Clipper

Rights-safe automated video clipping platform.

Paste a YouTube URL (optionally linked to a paid clipping campaign), let Clipper verify your legal right to clip the video through a strict **Rights Gate**, generate vertical 9:16 short clips via a swappable clipping engine, and review them before any future publishing workflow.

## Non-Negotiable Rights Gate (Hardened)

No clipping job can be queued, downloaded, or processed unless the source video passes at least one of four allowed rights bases (evaluated in `backend/app/services/rights.py` and re-checked inside the Celery worker before `yt-dlp` runs):

1. **`owner`** (`api_verified`) — The video's YouTube `channelId` matches one of your connected YouTube channels (`youtube_channels`, refresh tokens stored Fernet-encrypted).
2. **`campaign`** (`self_attested`) — The video URL (normalized video ID or channel) is listed in the selected active campaign's authorized sources (`campaign_sources`), is within the campaign deadline, and has required authorization proof (terms URL or uploaded proof document with SHA-256) plus an attestation timestamp. Campaigns are strictly scoped to the owner user; referencing another user's campaign returns `404 Not Found`.
3. **`cc`** (`api_verified`) — YouTube Data API v3 (`videos.list`, `part=status,snippet,contentDetails`) returns `status.license == "creativeCommon"`. Automatically sets `requires_attribution = true` and attaches mandatory attribution text to every generated clip.
4. **`permission`** (`self_attested`) — You uploaded a non-revoked, non-expired written permission document (`permissions`) covering the video's `channelId`, verified to exist on disk in storage, with an optional free-text scope.

If none match, the job is saved with `status = "rejected"`, a human-readable `rejection_reason`, full `rights_evidence_json`, and an `audit_log` entry. It is never enqueued to Celery.

### Rights Gate Hardening & Verification Levels

- **Verification Levels**: Every approved job records `verification_level` in its evidence:
  - `api_verified` for `owner` and `cc` (verified directly via Google/YouTube API).
  - `self_attested` for `campaign` and `permission` (user-attested terms or written documentation).
  - Displayed as green (`API Verified`) or amber (`Self-Attested`) badges across the UI.
  - **Explicit Confirmation Checkbox**: Uploads for `self_attested` clips require the user to check an explicit rights confirmation checkbox before the upload can be queued.
- **Fail-Closed Pre-Upload Re-Check**: The pre-upload Rights Gate re-check fails closed: any exception, missing/deleted video, video becoming private, or non-allowed decision immediately blocks the upload, marks it `failed`, and records `upload.rights_blocked` in `audit_log`.
- **Campaign Scope & Proof**: Campaigns are strictly private to the owner user (`campaign.owner_user_id == current_user.id`). Other users' campaign IDs return 404 across all endpoints. Creating a campaign requires a terms URL (`proof_url`) or uploaded proof file (storing its SHA-256) and an attestation timestamp.
- **Permission Document Integrity**: Permission evaluation verifies the physical document file exists on disk within `STORAGE_ROOT`, and records the document's `scope` in rights evidence.
- **Source File Duration Validation**: When uploading a source file, Clipper probes the file's duration using `ffprobe` against the verified YouTube video's duration (5s / 5% tolerance), stores `uploaded_source_sha256` in job evidence, and rejects duration mismatches with `400 Bad Request`.

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
│   ├── alembic/                  # Alembic migrations (0001_initial_schema .. 0004_add_clip_fields)
│   ├── app/
│   │   ├── api/                  # health, auth, campaigns, permissions, jobs
│   │   ├── models/               # SQLAlchemy 2.x models
│   │   ├── schemas/              # Pydantic v2 request/response schemas
│   │   ├── services/             # rights.py, youtube.py, rules.py, metadata.py, audit.py
│   │   │   └── engines/          # ClipEngine ABC, LocalEngine (Whisper + Claude), StubEngine
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

## Phase 2: LocalEngine (Whisper Transcription & Claude Virality Scoring)

Clipper includes `LocalEngine`, the default AI-driven clipping engine (`CLIP_ENGINE=local`), combining local speech recognition with LLM virality analysis and 9:16 vertical rendering:

1. **Audio Extraction & Whisper Transcription**:
   - Audio is extracted as 16 kHz mono WAV via `ffmpeg`.
   - Transcribed using `faster-whisper` (CPU, int8) with configurable model size (`WHISPER_MODEL`, default `small`).
   - Produces word-level timestamps (`word.start`, `word.end`).
   - Transcripts are cached per job under `${STORAGE_ROOT}/jobs/<job_id>/transcript.json` to avoid redundant transcription on reruns.
2. **Sentence-Bounded Candidate Generation**:
   - Groups words into complete sentences based on terminal punctuation (`.`, `!`, `?`).
   - Generates candidate segments spanning contiguous sentences within the job's configurable length bounds (`min_clip_duration_sec` and `max_clip_duration_sec`, default 20–60s).
   - Segments strictly start and end on sentence boundaries, never cutting mid-word.
3. **Virality Scoring with Claude**:
   - Scores candidates in batches using Anthropic's Messages API (`ANTHROPIC_API_KEY`, model `ANTHROPIC_MODEL`, default `claude-sonnet-5-5`).
   - Evaluates:
     1. Hook strength in the first 3 seconds
     2. Complete idea with clear payoff
     3. Standalone value without prior context
     4. Emotional or informational value
   - Parses strict JSON with automatic retry on malformed outputs.
   - Handles API errors gracefully without failing the job, falling back to heuristic scoring.
4. **Fallback Heuristic Scorer**:
   - When `ANTHROPIC_API_KEY` is empty or unset, automatically scores candidates using speech density (words/sec), sentence completeness, and target duration.
   - Tags clips with `"heuristic"` in metadata so jobs succeed without requiring third-party API keys.
5. **Non-Overlapping Diverse Selection**:
   - Greedily selects top N candidates by score.
   - Enforces strict non-overlap and a minimum gap between clips (>= 3 seconds).
   - Enforces topic diversity using token Jaccard similarity to prevent duplicate variations of the same highlight.
   - Stores `score`, virality `reason`, and `transcript_excerpt` on each `Clip` row.
6. **FFmpeg 9:16 Rendering & Word-Level Captions**:
   - Renders 1080x1920 vertical video (default center-crop; optional blurred-background fit via `blurred_background`).
   - Normalizes audio loudness to web standards using `loudnorm=I=-16:TP=-1.5:LRA=11`.
   - Generates readable ASS subtitles grouping 2–4 words at a time with yellow/white bold styling and burns them in.
7. **Cost & Privacy Guard**:
   - Never logs transcripts or API keys.
   - Enforces `MAX_TRANSCRIPT_CHARS` (default 150,000 chars) per job to control costs.

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
