# ReviewPilot

ReviewPilot is a local, advisory GitHub pull request review dashboard. Milestone 1 delivers the API foundation and authenticated webhook ingestion. The review worker and dashboard are not implemented yet.

## Requirements

- Python 3.12 or newer
- Docker Desktop with Docker Compose
- GitHub App configuration only for live webhook verification

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

## GitHub App setup

Create a GitHub App for the test account or organization. Grant **Repository metadata: Read-only**, **Pull requests: Read-only**, and **Contents: Read-only**. Subscribe to `pull_request`. Do not grant write permissions. Set the webhook URL to the HTTPS tunnel URL ending in `/api/webhooks/github`; configure a random webhook secret and put it in `GITHUB_WEBHOOK_SECRET` in `.env`. Keep the app private key path and all credentials server-side. Never commit `.env` or private keys.

Supported actions are `opened`, `reopened`, and `synchronize`. The backend verifies `X-Hub-Signature-256` over the raw request body before parsing it. `X-GitHub-Delivery` is the idempotency key. Duplicate deliveries are acknowledged without repeating writes; other event types and PR actions are recorded and ignored. No review job is enqueued in this milestone.

## Local data services

Postgres and Redis run on loopback ports 5432 and 6379. Postgres state is held in the `postgres_data` Compose volume. Stop services with `docker compose -f infrastructure/docker-compose.yml down`; add `-v` only if you intend to delete the local database volume.

## Security boundary

This MVP has no dashboard authentication. Keep the backend bound to localhost and do not deploy it publicly. Private PR metadata is stored in PostgreSQL. No source code is fetched or sent to an LLM in Milestone 1. See [docs/implementation-status.md](docs/implementation-status.md) for milestone scope and remaining work.
