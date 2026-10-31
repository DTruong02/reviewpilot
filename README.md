# ReviewPilot

ReviewPilot is a local, advisory GitHub pull request review dashboard. It includes signed webhook ingestion, an injectable review engine, asynchronous worker recovery, local read/feedback APIs, and a Next.js dashboard.

## Requirements

- Python 3.12 or newer
- Docker Desktop with Docker Compose
- GitHub App configuration for live webhook delivery and retrieval

## Local setup

From the repository root:

```powershell
Copy-Item .env.example .env
docker compose -f infrastructure/docker-compose.yml up -d
cd backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API listens on localhost. `GET http://127.0.0.1:8000/health` is a liveness check. Webhook requests go to `POST /api/webhooks/github`. OpenAPI docs are available at `/docs` during development. To run the tests, execute `python -m pytest` from `backend`.

## Dashboard

With the API running, open a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Visit `http://localhost:3000`. The Next.js server proxies `/api/*` to `API_BASE_URL` (default `http://127.0.0.1:8000`); set `API_BASE_URL` in the frontend process environment when using another local API address. The browser only calls same-origin paths and does not receive GitHub or OpenAI credentials.

The dashboard displays repository and pull request metadata, stored review findings and status, and lets users submit helpful/false-positive feedback. It polls only while a review is queued or running. ReviewPilot is advisory; findings are not automated decisions. Source context may be sent to the configured OpenAI provider only when the backend source-upload opt-in is enabled. See the security boundary below and keep the application local.

## Quality checks and evaluation

Run the backend suite and formatting checks from `backend` with `python -m pytest`, `ruff check .`, and `ruff format --check .`. Run frontend checks from `frontend` with `npm ci`, `npm run lint`, `npm run typecheck`, `npm test -- --run`, and `npm run build`. CI runs these checks with fake credentials and no live GitHub/OpenAI calls. The ten synthetic review fixtures can be validated from the repository root with `python scripts/evaluate_fixtures.py`; scoring requires recorded model predictions. See [evaluation/README.md](evaluation/README.md) for the methodology and limits.

In separate terminals from `backend`, start the worker and queue reconciler:

```powershell
celery -A app.workers.celery_app:celery_app worker --loglevel=info
celery -A app.workers.celery_app:celery_app beat --loglevel=info
```

The webhook transaction stores one queued review per PR commit and a durable outbox record. The API attempts to enqueue after commit; the periodic Beat task retries queued work and recovers abandoned worker leases. Worker replays are idempotent. A stale commit is marked superseded.

## GitHub App setup

Create a GitHub App for the test account or organization. Grant **Repository metadata: Read-only**, **Pull requests: Read-only**, and **Contents: Read-only**. Subscribe to `pull_request`. Do not grant write permissions. Set the webhook URL to the HTTPS tunnel URL ending in `/api/webhooks/github`; configure a random webhook secret and put it in `GITHUB_WEBHOOK_SECRET` in `.env`. Keep the app private key path and all credentials server-side. Never commit `.env` or private keys.

Supported actions are `opened`, `reopened`, and `synchronize`. The backend verifies `X-Hub-Signature-256` over the raw request body before parsing it. `X-GitHub-Delivery` is the idempotency key. Duplicate deliveries are acknowledged without repeating writes; other event types and PR actions are recorded and ignored. Accepted events enqueue review work without calling the LLM in the webhook handler.

## Local data services

Postgres and Redis run on loopback ports 5432 and 6379. Postgres state is held in the `postgres_data` Compose volume. Stop services with `docker compose -f infrastructure/docker-compose.yml down`; add `-v` only if you intend to delete the local database volume.

## Security boundary

This MVP has no dashboard authentication. Keep the backend bound to localhost and do not deploy it publicly. Non-webhook `/api` endpoints also reject non-loopback clients; the signed webhook endpoint remains available through the configured tunnel. Private PR metadata and findings are stored in PostgreSQL. See [docs/implementation-status.md](docs/implementation-status.md) for milestone scope and remaining work.

## Local data retention

ReviewPilot stores repository and pull request metadata, review status and metrics, findings, and feedback in PostgreSQL. It does not persist retrieved source files or diffs. There is no per-repository deletion UI or API yet. For a full local data reset, stop Compose with `docker compose -f infrastructure/docker-compose.yml down -v`; this deletes the PostgreSQL volume and all stored ReviewPilot data.

## Milestone 2 review engine

The backend includes an injectable GitHub App token provider/client, paginated pull-request file retrieval, a bounded context builder, strict finding validation, and an injectable LLM interface. `REVIEW_DRY_RUN=true` selects a deterministic fake provider and makes no model request. To use OpenAI, configure `OPENAI_API_KEY` and `OPENAI_MODEL`, set `REVIEW_DRY_RUN=false`, and separately set `REVIEW_ALLOW_SOURCE_UPLOAD=true`. This sends bounded pull request source and title text to the configured model provider. The review engine does not write GitHub comments or persist review source.

The GitHub App needs Repository metadata, Pull requests, and Contents read-only permissions. Configure `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` for retrieval. The client uses short-lived installation tokens, bounded pagination, and capped source retrieval. Tests use mock HTTP transports and fake LLM responses; no live GitHub or OpenAI credentials are required.
