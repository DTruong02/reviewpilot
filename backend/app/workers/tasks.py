import asyncio
import random
import uuid
from dataclasses import dataclass

from celery import Task
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.services.context_builder import ContextBuilder
from app.services.context_retriever import RepositoryContextRetriever
from app.services.github import build_github_client
from app.services.llm import build_llm_client
from app.services.review_engine import ReviewEngine
from app.workers.celery_app import celery_app
from app.workers.processing import (
    ProcessingFailure,
    mark_failed,
    process_review_once,
    reset_for_retry,
)
from app.workers.queue import reconcile_queued_reviews as reconcile_queue


@dataclass(frozen=True)
class RetryRequested:
    code: str
    countdown: int


def build_review_engine(
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> ReviewEngine:
    return ReviewEngine(
        github=build_github_client(),
        llm=build_llm_client(),
        context_builder=ContextBuilder(),
        context_retriever=RepositoryContextRetriever(sessions) if sessions else None,
    )


async def run_review_task_async(
    review_id: str,
    *,
    retries: int = 0,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    engine_factory=build_review_engine,
) -> dict[str, str] | RetryRequested:
    """Async task body; tests may supply a session factory to reuse isolated test storage."""
    review_uuid = uuid.UUID(review_id)
    owned_engine = None
    if sessions is None:
        # A fresh engine is created and disposed within this task's asyncio.run() loop.
        owned_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    try:
        try:

            def configured_engine_factory() -> ReviewEngine:
                if engine_factory is build_review_engine:
                    return build_review_engine(sessions=sessions)
                return engine_factory()

            status = await process_review_once(review_uuid, sessions, configured_engine_factory)
            return {"status": status}
        except ProcessingFailure as failure:
            if failure.retryable and retries < settings.review_task_max_retries:
                await reset_for_retry(review_uuid, failure.processing_token, failure.code, sessions)
                countdown = min(int(5 * (2**retries) + random.random() * 5), 120)
                return RetryRequested(failure.code, countdown)
            await mark_failed(review_uuid, failure.processing_token, failure.code, sessions)
            return {"status": "failed", "error_code": failure.code}
        except Exception:
            # Leave a claimed row for the periodic stale-lease reconciler if the DB itself failed.
            return {"status": "deferred", "error_code": "worker_internal_error"}
    finally:
        if owned_engine is not None:
            await owned_engine.dispose()


async def run_reconciliation_async() -> int:
    """Create one engine in this event loop; never carry pooled async connections across loops."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            return await reconcile_queue(session)
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True,
    name="reviewpilot.process_review",
    max_retries=settings.review_task_max_retries,
)
def process_review_task(self: Task, review_id: str) -> dict[str, str]:
    try:
        result = asyncio.run(run_review_task_async(review_id, retries=self.request.retries))
        if isinstance(result, RetryRequested):
            raise self.retry(
                exc=RuntimeError(result.code),
                countdown=result.countdown,
                max_retries=settings.review_task_max_retries,
            )
        return result
    except ValueError:
        return {"status": "failed", "error_code": "invalid_review_id"}


@celery_app.task(name="reviewpilot.reconcile_queued_reviews")
def reconcile_queued_reviews_task() -> dict[str, int]:
    return {"dispatched": asyncio.run(run_reconciliation_async())}
