import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.entities import (
    Category,
    Finding,
    Installation,
    PullRequest,
    Repository,
    Review,
    ReviewOutbox,
    ReviewStatus,
    Severity,
)
from app.schemas.review import ReviewFinding, ReviewResult
from app.services.review_engine import ReviewEngineError
from app.workers.processing import process_review_once
from app.workers.queue import dispatch_queued_review, reconcile_queued_reviews
from app.workers.tasks import RetryRequested, run_review_task_async

SHA1 = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    await engine.dispose()


async def queued_review(sessions, *, head_sha=SHA1, commit_sha=SHA1):
    async with sessions() as session:
        installation = Installation(github_installation_id=10, account_login="octo")
        session.add(installation)
        await session.flush()
        repository = Repository(
            installation_id=installation.id,
            github_repository_id=20,
            owner="octo",
            name="demo",
            full_name="octo/demo",
            private=True,
        )
        session.add(repository)
        await session.flush()
        pull_request = PullRequest(
            repository_id=repository.id,
            github_pr_number=7,
            title="Change",
            state="open",
            head_sha=head_sha,
            html_url="https://github.com/octo/demo/pull/7",
        )
        session.add(pull_request)
        await session.flush()
        review = Review(
            pull_request_id=pull_request.id,
            commit_sha=commit_sha,
            status=ReviewStatus.queued,
        )
        session.add(review)
        await session.flush()
        session.add(ReviewOutbox(review_id=review.id))
        await session.commit()
        return review.id, pull_request.id


def sample_result() -> ReviewResult:
    return ReviewResult(
        findings=(
            ReviewFinding(
                file_path="src/example.py",
                line_number=3,
                severity="high",
                category="bug",
                title="Missing check",
                explanation="The updated path does not validate the input.",
                suggested_fix="Validate before use.",
            ),
        ),
        model_name="fake-review-model",
        input_tokens=100,
        output_tokens=20,
        duration_ms=42,
    )


class FakeReviewEngine:
    def __init__(self, result=None, before_return=None, failure=None):
        self.result = result or sample_result()
        self.before_return = before_return
        self.failure = failure
        self.calls = 0

    async def review_pull_request(self, **_kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        if self.before_return:
            await self.before_return()
        return self.result


@pytest.mark.asyncio
async def test_worker_persists_findings_metrics_and_is_idempotent(database):
    review_id, _ = await queued_review(database)
    engine = FakeReviewEngine()

    def factory():
        return engine

    first = await process_review_once(review_id, database, factory)
    replay = await process_review_once(review_id, database, factory)

    assert first == "completed"
    assert replay == "completed"
    assert engine.calls == 1
    async with database() as session:
        review = await session.get(Review, review_id)
        findings = list((await session.scalars(select(Finding))).all())
        assert review.status == ReviewStatus.completed
        assert review.model_name == "fake-review-model"
        assert review.input_tokens == 100
        assert review.output_tokens == 20
        assert review.duration_ms == 42
        assert review.processing_token is None
        assert len(findings) == 1
        assert findings[0].severity == Severity.high
        assert findings[0].category == Category.bug


@pytest.mark.asyncio
async def test_replayed_task_does_not_claim_an_already_running_review(database):
    review_id, _ = await queued_review(database)
    async with database() as session:
        review = await session.get(Review, review_id)
        review.status = ReviewStatus.running
        review.processing_token = str(uuid.uuid4())
        review.started_at = datetime.now(UTC)
        await session.commit()

    engine = FakeReviewEngine()
    assert await process_review_once(review_id, database, lambda: engine) == "running"
    assert engine.calls == 0


@pytest.mark.asyncio
async def test_worker_marks_result_superseded_if_head_changes_during_review(database):
    review_id, pull_request_id = await queued_review(database)

    async def advance_head():
        async with database() as session:
            pull_request = await session.get(PullRequest, pull_request_id)
            pull_request.head_sha = SHA2
            await session.commit()

    result = await process_review_once(
        review_id, database, lambda: FakeReviewEngine(before_return=advance_head)
    )

    assert result == "superseded"
    async with database() as session:
        review = await session.get(Review, review_id)
        assert review.status == ReviewStatus.superseded
        assert await session.scalar(select(Finding.review_id)) == review_id


@pytest.mark.asyncio
async def test_transient_review_failure_resets_to_queued_then_terminal_failure_is_saved(database):
    review_id, _ = await queued_review(database)
    transient = FakeReviewEngine(failure=ReviewEngineError("github_unavailable"))

    first = await run_review_task_async(
        str(review_id), sessions=database, engine_factory=lambda: transient
    )
    assert isinstance(first, RetryRequested)
    async with database() as session:
        review = await session.get(Review, review_id)
        assert review.status == ReviewStatus.queued
        assert review.processing_token is None
        assert review.error_code == "github_unavailable"

    permanent = FakeReviewEngine(failure=ReviewEngineError("invalid_review_reference"))
    result = await run_review_task_async(
        str(review_id), sessions=database, engine_factory=lambda: permanent
    )
    assert result == {"status": "failed", "error_code": "invalid_review_reference"}
    async with database() as session:
        review = await session.get(Review, review_id)
        assert review.status == ReviewStatus.failed
        assert review.error_code == "invalid_review_reference"


@pytest.mark.asyncio
async def test_broker_failure_is_recoverable_and_success_is_not_immediately_republished(
    database, monkeypatch
):
    review_id, _ = await queued_review(database)
    calls = []

    def unavailable(value):
        calls.append(value)
        raise ConnectionError("redis credentials must not be persisted")

    async with database() as session:
        assert not await dispatch_queued_review(session, review_id, unavailable)
    async with database() as session:
        outbox = await session.scalar(
            select(ReviewOutbox).where(ReviewOutbox.review_id == review_id)
        )
        assert outbox.enqueue_attempts == 1
        assert outbox.last_error_code == "broker_unavailable"
        outbox.last_enqueued_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

    def available(value):
        calls.append(value)

    async with database() as session:
        assert await dispatch_queued_review(session, review_id, available)
    async with database() as session:
        assert not await dispatch_queued_review(session, review_id, available)
        outbox = await session.scalar(
            select(ReviewOutbox).where(ReviewOutbox.review_id == review_id)
        )
        assert outbox.enqueue_attempts == 2
        assert outbox.last_error_code is None
    assert calls == [str(review_id), str(review_id)]


@pytest.mark.asyncio
async def test_reconciler_recovers_stale_running_review_and_dispatches_once(database):
    review_id, _ = await queued_review(database)
    async with database() as session:
        review = await session.get(Review, review_id)
        review.status = ReviewStatus.running
        review.started_at = datetime.now(UTC) - timedelta(hours=1)
        review.processing_token = str(uuid.uuid4())
        outbox = await session.scalar(
            select(ReviewOutbox).where(ReviewOutbox.review_id == review_id)
        )
        outbox.last_enqueued_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

    calls = []

    def enqueue(value):
        calls.append(value)

    async with database() as session:
        assert await reconcile_queued_reviews(session, enqueue) == 1
    async with database() as session:
        review = await session.get(Review, review_id)
        assert review.status == ReviewStatus.queued
        assert review.processing_token is None
    assert calls == [str(review_id)]
