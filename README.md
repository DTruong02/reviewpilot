# ReviewPilot

ReviewPilot is a local, advisory GitHub pull request review app. A signed webhook queues analysis of PR changes; a worker produces structured findings; a Next.js dashboard shows status, findings, and feedback. Findings are advisory only—ReviewPilot does not post GitHub comments or modify repository code.

## Requirements

- Python 3.12 or newer
- Node.js (for the dashboard)
- Docker Desktop with Docker Compose
- A GitHub App for live webhook delivery and PR retrieval

## Local setup

From the repository root:

```bash
cp .env.example .env
docker compose -f infrastructure/docker-compose.yml up -d
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In separate terminals from `backend`, start the worker and queue reconciler:

```bash
celery -A app.workers.celery_app:celery_app worker --loglevel=info
celery -A app.workers.celery_app:celery_app beat --loglevel=info
```

With the API running, start the dashboard:

```bash
cd frontend
npm ci
npm run dev
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API health | http://127.0.0.1:8000/health |
| OpenAPI docs | http://127.0.0.1:8000/docs |
| Webhook | `POST /api/webhooks/github` |

The frontend proxies `/api/*` to `API_BASE_URL` (default `http://127.0.0.1:8000`). Credentials stay server-side; the browser only calls same-origin paths.

Postgres and Redis listen on localhost ports 5432 and 6379. Stop them with `docker compose -f infrastructure/docker-compose.yml down`. Add `-v` only if you want to delete the local database volume.

## Configuration

Copy `.env.example` to `.env` and fill in values as needed. Important defaults:

| Variable | Notes |
|---|---|
| `REVIEW_DRY_RUN` | `true` uses a deterministic fake LLM (no OpenAI calls). Set `false` with `OPENAI_API_KEY` and `OPENAI_MODEL` for live reviews. |
| `REVIEW_ALLOW_SOURCE_UPLOAD` | Must be `true` to send bounded PR source/title text to the model. |
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` / `GITHUB_WEBHOOK_SECRET` | Required for live GitHub webhooks and retrieval. |

## GitHub App setup

Create a GitHub App on your test account or organization:

- Permissions: **Repository metadata**, **Pull requests**, and **Contents** — all Read-only
- Events: `pull_request` (`opened`, `reopened`, `synchronize`)
- Webhook URL: your HTTPS tunnel ending in `/api/webhooks/github`
- Set a random webhook secret in `GITHUB_WEBHOOK_SECRET`

Keep the private key and all secrets server-side. Never commit `.env` or private keys.

The backend verifies `X-Hub-Signature-256` on the raw body before processing. `X-GitHub-Delivery` is the idempotency key. Duplicate deliveries are acknowledged without repeating work; unsupported events are recorded and ignored. The webhook handler never calls the LLM.

## Using the dashboard

The dashboard lists repositories and pull requests, shows review status and findings, and accepts helpful / false-positive feedback. It polls only while a review is queued or running.

Optional local features (all disabled or safe-by-default until configured):

- **Commit indexing** — builds a bounded symbol index for a repository commit so reviews can use changed symbols and related context. Request an index via `POST /api/repositories/<repository-uuid>/indexes` with `{"commit_sha":"<sha>"}`. Default embedding mode is local mock vectors; OpenAI embeddings require opt-in allowlists and `REVIEW_ALLOW_SOURCE_UPLOAD=true`.
- **Validation planning** — propose and approve a bounded validation plan for a finding. Set `VALIDATION_API_TOKEN` (≥ 32 UTF-8 bytes) and `VALIDATION_ALLOWED_REPOSITORIES` (`owner/name` list). Isolated test execution is not enabled.

## Tests

From `backend`: `python -m pytest`, `ruff check .`, `ruff format --check .`

From `frontend`: `npm run lint`, `npm run typecheck`, `npm test -- --run`, `npm run build`

## Security and data

This app has no dashboard authentication. Keep the backend on localhost and do not deploy it publicly. Non-webhook `/api` endpoints reject non-loopback clients; the signed webhook remains reachable through your tunnel.

ReviewPilot stores repository/PR metadata, review status, findings, and feedback in PostgreSQL. It does not persist retrieved source files or diffs. For a full local reset: `docker compose -f infrastructure/docker-compose.yml down -v`.
