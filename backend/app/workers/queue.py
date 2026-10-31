import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import Review, ReviewOutbox, ReviewStatus
from app.workers.celery_app import celery_app


def enqueue_review_task(review_id: str) -> None:
    """Publish with a stable task id. Reconciliation may republish safely."""
    celery_app.send_task(
        "reviewpilot.process_review",
        args=[review_id],
        task_id=review_id,
        retry=False,
    )


async def dispatch_queued_review(
    session: AsyncSession,
    review_id: uuid.UUID,
    enqueue: Callable[[str], object] | None = None,
) -> bool:
    """Claim an outbox attempt, commit it, then contact Redis outside the DB transaction."""
    outbox = await session.scalar(
        select(ReviewOutbox).where(ReviewOutbox.review_id == review_id).with_for_update()
    )
    review = await session.get(Review, review_id)
    if outbox is None or review is None or review.status != ReviewStatus.queued:
        return False

    now = datetime.now(UTC)
    if outbox.last_enqueued_at is not None:
        last_enqueued_at = outbox.last_enqueued_at
        if last_enqueued_at.tzinfo is None:
            last_enqueued_at = last_enqueued_at.replace(tzinfo=UTC)
        if outbox.last_error_code:
            wait_seconds = min(
                settings.review_enqueue_retry_after_seconds
                * (2 ** min(max(0, outbox.enqueue_attempts - 1), 6)),
                3600,
            )
        else:
            wait_seconds = settings.review_reenqueue_after_seconds
        if last_enqueued_at + timedelta(seconds=wait_seconds) > now:
            return False

    outbox.enqueue_attempts += 1
    outbox.last_enqueued_at = now
    outbox.last_error_code = None
    await session.commit()

    try:
        publisher = enqueue or enqueue_review_task
        await asyncio.to_thread(publisher, str(review_id))
    except Exception:
        latest = await session.get(ReviewOutbox, outbox.id)
        if latest is not None:
            latest.last_error_code = "broker_unavailable"
            await session.commit()
        return False
    return True


async def reconcile_queued_reviews(
    session: AsyncSession,
    enqueue: Callable[[str], object] | None = None,
) -> int:
    """Recover stale running leases and dispatch queued reviews whose broker window elapsed."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.review_running_stale_seconds)
    stale_reviews = list(
        (
            await session.scalars(
                select(Review)
                .where(
                    Review.status == ReviewStatus.running,
                    Review.started_at.is_not(None),
                    Review.started_at < stale_before,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for review in stale_reviews:
        review.status = ReviewStatus.queued
        review.processing_token = None
        review.started_at = None
        review.error_code = None
    await session.commit()

    queued_ids = list(
        (await session.scalars(select(Review.id).where(Review.status == ReviewStatus.queued))).all()
    )
    # Repair any queued row created before the outbox was deployed or left by partial data fixes.
    for review_id in queued_ids:
        outbox = await session.scalar(
            select(ReviewOutbox.id).where(ReviewOutbox.review_id == review_id)
        )
        if outbox is None:
            session.add(ReviewOutbox(review_id=review_id))
    await session.commit()

    dispatched = 0
    for review_id in queued_ids:
        if await dispatch_queued_review(session, review_id, enqueue):
            dispatched += 1
    return dispatched
