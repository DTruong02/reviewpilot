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
4. Milestone 4: complete; local Next.js dashboard, typed API client, feedback, filters, and active-review polling.
5. Milestone 5: complete for localhost demo; CI, labeled evaluation fixtures/scorer, setup/security/retention documentation.

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

## Milestone 4 — Next.js dashboard

- [x] Add responsive dashboard, repositories, repository pull request list, pull request review history, and review detail pages.
- [x] Display findings grouped by severity, allow severity filtering and helpful/false-positive feedback.
- [x] Add pagination, loading/error/empty states, and poll only queued/running reviews.
- [x] Proxy browser `/api/*` requests from Next.js to the backend; keep provider credentials server-side.
- [x] Add frontend component tests and startup/source-upload privacy guidance.
- [ ] Manually verify the browser flow against live local Postgres/Redis and webhook-created review data.

The frontend implementation passed lint, typecheck, three Vitest/RTL tests, and a production build in its isolated worktree. Root review fixed FastAPI structured error handling and added recent pull request/review summary content to the dashboard, with an added structured-error regression test. The final integrated frontend checks still need a fresh dependency install in the primary worktree.

## Milestone 5 — Quality, evaluation, and deployment readiness

- [x] Add backend/frontend CI with mock credentials and no live external API requirements.
- [x] Add ten hand-authored, labeled unified diff fixtures and a scorer for precision, recall, false positives, duration, and estimated cost.
- [x] Document fixture validation/scoring, setup checks, source upload opt-in, and local retention/reset behavior.
- [x] Keep public deployment explicitly unsupported until authenticated access control and installation-scoped authorization exist.
- [ ] Run CI on the hosting provider and verify Compose startup/migrations against PostgreSQL and Redis locally.

No live model benchmark results are claimed. Fixture timing and cost remain unavailable unless supplied as observed prediction data. The reviewer labels are synthetic and should be independently checked before model comparisons.

## Advanced extension — Milestone A: parser audit and Tree-sitter extraction

- [x] Audit current webhook, GitHub retrieval, review engine, worker, database, API, dashboard, and tests before extending them.
- [x] Add a pure `CodeParser` interface and Tree-sitter extraction for Python, JavaScript/JSX, and TypeScript/TSX.
- [x] Extract deterministic symbol keys, qualified names, kinds, signatures, docstring summaries, exports, import statements/modules, content hashes, and byte/line ranges.
- [x] Bound source size and safely report unsupported extensions, binary data, invalid UTF-8, and syntax errors without executing repository content.
- [x] Add parser fixtures/tests for nested scopes, imports, exports, TypeScript/TSX, malformed syntax, oversized/binary input, and renamed symbols.
- [x] Install the pinned Tree-sitter dependency set in an isolated Python 3.12 Windows environment and run the complete existing suite.
- [ ] Verify grammar wheel installation and the complete suite on Linux CI.

The parser milestone remains standalone from PR review inputs and dashboard behavior. Dynamic imports, aliases, and runtime call relationships are not resolved. Milestone B adds persisted indexing but still does not feed indexed context into reviews.

## Advanced extension — Milestone B: commit-scoped indexing

- [x] Add immutable repository/SHA indexes, file/symbol/import-edge storage, versioned embeddings, and a content/model/version cache.
- [x] Add bounded GitHub tree ingestion, source and secret filters, per-file resumability, deterministic identifiers, and ready/failed status.
- [x] Add local index submission/status endpoints and periodic queue recovery for queued or stale work.
- [x] Keep external embedding upload disabled by default; require `REVIEW_ALLOW_SOURCE_UPLOAD=true`, explicit OpenAI mode, API key, and repository allowlisting. Demo mode uses deterministic local hash vectors.
- [x] Add SQLite/mock tests for idempotence, commit isolation, cache reuse, filters, limits, status, and failure recovery.
- [ ] Verify pgvector extension migration and GitHub/OpenAI integrations against live services; no external credentials or services were used.

The B implementation stores parser metadata and vectors, not full source files or symbol snippets. OpenAI receives only bounded signature/docstring summaries for explicitly allowlisted repositories. Retrieval and PR review integration remain out of scope until Milestone C.
