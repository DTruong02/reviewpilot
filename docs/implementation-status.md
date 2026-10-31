# Implementation status


Updated: 2026-10-31

## Milestone 1 — Foundation + authenticated webhook ingestion

- [x] Monorepo backend, environment template, ignore rules, and local Postgres/Redis Compose services.
- [x] FastAPI health endpoint and signed GitHub webhook endpoint.
- [x] SQLAlchemy models for installations, repositories, pull requests, webhook deliveries, reviews, and findings.
- [x] Initial Alembic schema migration.
- [x] Webhook signature, duplicate delivery, unsupported event/action, and PR head update tests.
- [x] README setup and least-privilege GitHub App/tunnel instructions.
- [x] Run the initial migration against SQLite and automated tests in Python 3.12+ environment.
- [x] Validate the Docker Compose configuration.
- [ ] Verify migrations and local services against PostgreSQL/Redis with Docker Compose.
- [ ] Manually verify a live GitHub delivery after App configuration is supplied.

Invalid signatures and malformed supported PR events are rejected; accepted events are committed with their installation/repository/PR metadata in one database transaction. Unsupported events are retained as processed delivery records.

Docker Desktop was not running during implementation, so Postgres/Redis container startup and the PostgreSQL migration remain unverified. Live GitHub delivery also needs a configured GitHub App and HTTPS tunnel.

## Planned sequence

1. Milestone 1: complete; SQLite migration and webhook behavior verified.
2. Milestone 2: complete; mocked GitHub and LLM review engine verified.
3. Milestone 3: complete; durable queue, idempotent worker, local-only APIs, and finding feedback implemented.
4. Milestone 4: Next.js dashboard, depending on the read/feedback APIs.
5. Milestone 5: CI, evaluation fixtures, documentation polish, and deployment readiness, after backend and frontend behavior stabilize.

Work within each milestone can be split by bounded components. Cross-milestone work should wait for the preceding API and data contracts. Public deployment remains blocked on authenticated access control.

## Milestone 2 — GitHub retrieval + review engine

- [x] GitHub App JWT and cached installation token provider with short expiry margin.
- [x] Paginated PR file retrieval, conservative path filtering, source-size caps, and limited file-content retrieval.
- [x] Context builder with changed-line anchoring, context/file/change limits, generated/binary skipping, and obvious credential redaction.
- [x] Injectable LLM interface, deterministic fake, and configurable OpenAI Structured Outputs adapter.
- [x] Pydantic findings validation, changed-line anchor enforcement, deduplication, max findings, and bounded invalid-output retry.
- [x] Mock-based tests for token caching, pagination, content/context limits, line anchoring, invalid output, and empty results.
- [ ] Verify with a configured GitHub App and opted-in OpenAI model; no live integration was exercised.

After integration review, the combined Milestone 1–3 suite passes 42 tests. Ruff lint/format and whitespace checks pass. The complete Alembic chain upgrades against SQLite. Live GitHub/OpenAI access and PostgreSQL/Redis container startup remain unverified in this environment.

## Milestone 3 — Async execution, persistence, and read APIs

- [x] Persist one queued review and outbox record per PR commit in the webhook transaction.
- [x] Dispatch Celery jobs after commit and periodically reconcile queued work and stale running leases.
- [x] Use a per-task async engine, lease-token fencing, idempotent finding replacement, sanitized failures, and commit-SHA superseding.
- [x] Add local-only, paginated repository/PR/review APIs and validated finding feedback.
- [x] Test worker persistence/replay, enqueue recovery, stale SHA handling, API access boundaries, and feedback.
- [ ] Verify the worker and recovery loop with live Redis/PostgreSQL and a configured GitHub App; external services were not exercised.
