# Clipper

Rights-safe automated video clipping platform.

Paste a YouTube URL (optionally linked to a paid clipping campaign), let Clipper verify your legal right to clip the video through a strict **Rights Gate**, generate vertical 9:16 short clips via a swappable clipping engine, and review them before any future publishing workflow.

## Non-Negotiable Rights Gate

No clipping job can be queued or downloaded unless the source video passes at least one of four allowed rights bases:

1. **`owner`** — The video's YouTube `channelId` matches one of your connected YouTube channels.
2. **`campaign`** — The video URL is listed in the selected active campaign's authorized sources.
3. **`cc`** — YouTube Data API v3 (`videos.list`, `part=status`) reports `license == "creativeCommon"` (attribution text is automatically required and attached to every clip).
4. **`permission`** — You uploaded a valid, non-revoked written permission document covering that channel.

If none match, the job is saved as `rejected` with a clear human-readable reason and never queued.

## Project Structure

```text
.
├── backend/
│   ├── alembic/                  # Database migrations
│   ├── app/
│   │   ├── api/                  # FastAPI routers
│   │   ├── models/               # SQLAlchemy 2.x models
│   │   ├── schemas/              # Pydantic v2 schemas
│   │   ├── services/             # rights.py, youtube.py, rules.py, metadata.py
│   │   │   └── engines/          # ClipEngine ABC, StubEngine, LocalEngine (TODO)
│   │   ├── workers/              # Celery worker + tasks
│   │   └── main.py
│   └── tests/                    # pytest unit & integration tests
├── frontend/                     # Next.js (App Router) + TypeScript + Tailwind
├── docker-compose.yml
├── .env.example
└── PROJECT_SPEC.md
```

## Quick Start (Docker Compose)

1. Copy the example environment file and configure your secrets:

   ```bash
   cp .env.example .env
   ```

   Generate a Fernet encryption key for YouTube refresh tokens and a JWT secret:

   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Paste them into `TOKEN_ENCRYPTION_KEY` and `JWT_SECRET_KEY` in `.env`, along with your Google OAuth credentials (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `YOUTUBE_API_KEY`).

2. Start the full stack:

   ```bash
   docker compose up --build
   ```

3. Verify the services:
   - Backend API health: http://localhost:8000/health
   - Backend readiness: http://localhost:8000/health/ready
   - OpenAPI docs: http://localhost:8000/docs
   - Frontend UI: http://localhost:3000

## Local Development (Without Docker)

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Run tests:

```bash
cd backend
pytest
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Google OAuth Setup (Read-Only Scope)

In Google Cloud Console:
1. Enable **YouTube Data API v3**.
2. Create an OAuth 2.0 Web Application client.
3. Add Authorized Redirect URIs:
   - `http://localhost:8000/api/auth/google/callback`
   - `http://localhost:8000/api/auth/youtube/callback`
4. Only `openid`, `email`, `profile`, and `https://www.googleapis.com/auth/youtube.readonly` scopes are requested in this phase (no upload scope yet).
