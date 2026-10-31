import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    Category,
    Finding,
    Installation,
    PullRequest,
    Repository,
    Review,
    ReviewStatus,
    Severity,
)
from app.services.review_engine import ReviewEngine, ReviewEngineError


@dataclass(frozen=True)
class ProcessingFailure(Exception):
    code: str
    retryable: bool
    processing_token: str

    def __str__(self) -> str:
        return self.code


def is_retryable_code(code: str) -> bool:
    if code in {
        "github_network_error",
        "github_rate_limited",
        "github_unavailable",
    }:
        return True
    return code.startswith("openai_") and any(
        marker in code
        for marker in (
            "timeout",
            "connection",
            "ratelimit",
            "internalserver",
            "serviceunavailable",
        )
    )


def sanitized_error_code(value: object, default: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,100}", value):
        return value
    return default


async def _close_engine(engine: ReviewEngine | None) -> None:
    if engine is None:
        return
    for client in (getattr(engine, "github", None), getattr(engine, "llm", None)):
        close = getattr(client, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass


async def process_review_once(
    review_id: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession],
    engine_factory: Callable[[], ReviewEngine],
) -> str:
    """Claim a queued review, execute it, and atomically persist its result by lease token."""
    processing_token = str(uuid.uuid4())
    now = datetime.now(UTC)
    async with sessions() as session:
        review = await session.get(Review, review_id)
        if review is None:
            return "missing"
        if review.status != ReviewStatus.queued:
            return review.status.value
        claim = await session.execute(
            update(Review)
            .where(Review.id == review_id, Review.status == ReviewStatus.queued)
            .values(
                status=ReviewStatus.running,
                started_at=now,
                completed_at=None,
                error_code=None,
                processing_token=processing_token,
            )
        )
        if claim.rowcount != 1:
            await session.rollback()
            return "not_claimed"
        await session.commit()

        details = await session.execute(
            select(
                Review.commit_sha,
                PullRequest.id,
                PullRequest.github_pr_number,
                PullRequest.title,
                PullRequest.head_sha,
                Repository.owner,
                Repository.name,
                Installation.github_installation_id,
            )
            .join(PullRequest, PullRequest.id == Review.pull_request_id)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .join(Installation, Installation.id == Repository.installation_id)
            .where(Review.id == review_id)
        )
        row = details.one_or_none()
        if row is None:
            await mark_failed(review_id, processing_token, "review_context_missing", sessions)
            return "failed"

    engine = None
    try:
        engine = engine_factory()
        result = await engine.review_pull_request(
            installation_id=row.github_installation_id,
            owner=row.owner,
            repository=row.name,
            pull_request_number=row.github_pr_number,
            pull_request_title=row.title,
            head_sha=row.commit_sha,
        )
    except ReviewEngineError as exc:
        code = sanitized_error_code(exc.code, "review_engine_failed")
        raise ProcessingFailure(code, is_retryable_code(code), processing_token) from None
    except Exception as exc:
        code = sanitized_error_code(getattr(exc, "code", None), "worker_internal_error")
        raise ProcessingFailure(code, is_retryable_code(code), processing_token) from None
    finally:
        await _close_engine(engine)

    async with sessions() as session:
        review = await session.scalar(
            select(Review).where(Review.id == review_id).with_for_update()
        )
        if (
            review is None
            or review.status != ReviewStatus.running
            or review.processing_token != processing_token
        ):
            return "lease_lost"

        current_head_sha = await session.scalar(
            select(PullRequest.head_sha).where(PullRequest.id == row.id)
        )
        await session.execute(delete(Finding).where(Finding.review_id == review_id))
        for finding in result.findings:
            session.add(
                Finding(
                    review_id=review_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    severity=Severity(finding.severity),
                    category=Category(finding.category),
                    title=finding.title,
                    explanation=finding.explanation,
                    suggested_fix=finding.suggested_fix,
                )
            )
        is_stale = current_head_sha != row.commit_sha
        review.status = ReviewStatus.superseded if is_stale else ReviewStatus.completed
        review.model_name = result.model_name
        review.input_tokens = result.input_tokens
        review.output_tokens = result.output_tokens
        review.duration_ms = result.duration_ms
        review.completed_at = datetime.now(UTC)
        review.error_code = None
        review.processing_token = None
        await session.commit()
    return "superseded" if is_stale else "completed"


async def reset_for_retry(
    review_id: uuid.UUID,
    processing_token: str,
    code: str,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        review = await session.scalar(
            select(Review).where(Review.id == review_id).with_for_update()
        )
        if (
            review is not None
            and review.status == ReviewStatus.running
            and review.processing_token == processing_token
        ):
            review.status = ReviewStatus.queued
            review.started_at = None
            review.completed_at = None
            review.processing_token = None
            review.error_code = code[:100]
            await session.commit()


async def mark_failed(
    review_id: uuid.UUID,
    processing_token: str,
    code: str,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        review = await session.scalar(
            select(Review).where(Review.id == review_id).with_for_update()
        )
        if (
            review is not None
            and review.status == ReviewStatus.running
            and review.processing_token == processing_token
        ):
            review.status = ReviewStatus.failed
            review.completed_at = datetime.now(UTC)
            review.processing_token = None
            review.error_code = code[:100]
            await session.commit()
