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

The implementation does not enqueue review work. Invalid signatures and malformed supported PR events are rejected; accepted events are committed with their installation/repository/PR metadata in one database transaction. Unsupported events are retained as processed delivery records.

Docker Desktop was not running during implementation, so Postgres/Redis container startup and the PostgreSQL migration remain unverified. Live GitHub delivery also needs a configured GitHub App and HTTPS tunnel.

## Planned sequence

1. Milestone 1: complete verification, then stop for explicit approval.
2. Milestone 2: GitHub retrieval and LLM review engine, depending on the schema and safe event ingestion from Milestone 1.
3. Milestone 3: Celery queue, idempotent review persistence, and read APIs, depending on the review engine contract.
4. Milestone 4: Next.js dashboard, depending on stable read/feedback APIs.
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

After integration review, the combined Milestone 1 and 2 suite passes 26 tests. Ruff lint/format and whitespace checks pass. Live GitHub/OpenAI access and PostgreSQL/Redis startup remain unverified in this environment.
