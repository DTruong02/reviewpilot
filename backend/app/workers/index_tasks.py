"""Celery worker and durable queue reconciliation for repository indexes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.entities import (
    CodeFile,
    CodeSymbol,
    EmbeddingCache,
    PullRequest,
    Repository,
    RepositoryIndex,
    Review,
    SymbolEdge,
)
from app.services.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingError,
    OpenAIEmbeddingProvider,
)
from app.services.github import GitHubClientError, build_github_client
from app.services.repository_indexer import RepositoryIndexer
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _source_upload_allowed(repository_full_name: str) -> bool:
    allowlist = {
        item.strip().casefold()
        for item in settings.index_embedding_repository_allowlist.split(",")
        if item.strip()
    }
    return repository_full_name.casefold() in allowlist


async def run_index_task_async(
    index_id: str,
    *,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    github_client=None,
    embedding_factory=None,
) -> dict[str, str | int]:
    owned_engine = None
    index_uuid = None
    if sessions is None:
        owned_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    client = github_client
    try:
        index_uuid = uuid.UUID(index_id)
        async with sessions() as session:
            index = await session.get(RepositoryIndex, index_uuid)
            repository = await session.get(Repository, index.repository_id) if index else None
        if index is None or repository is None:
            return {"status": "failed", "error_code": "index_not_found"}
        if client is None:
            client = build_github_client()
        embeddings = None
        if settings.index_embedding_mode == "mock":
            embeddings = DeterministicHashEmbeddingProvider(settings.index_embedding_dimension)
        elif (
            settings.index_embedding_mode == "openai"
            and _source_upload_allowed(repository.full_name)
            and settings.review_allow_source_upload
        ):
            factory = embedding_factory or OpenAIEmbeddingProvider
            embeddings = factory(
                settings.openai_api_key,
                settings.index_embedding_model,
                settings.index_embedding_dimension,
                timeout_seconds=settings.index_embedding_timeout_seconds,
            )
        indexer = RepositoryIndexer(
            sessions,
            client,
            embeddings=embeddings,
            max_files=settings.index_max_files,
            max_file_bytes=settings.index_max_file_bytes,
            max_total_bytes=settings.index_max_total_bytes,
            max_tree_entries=settings.index_max_tree_entries,
            secret_patterns=tuple(
                pattern
                for pattern in settings.index_secret_patterns.splitlines()
                if pattern.strip()
            ),
            stale_seconds=settings.index_stale_seconds,
        )
        result = await indexer.index_commit(index_uuid)
        return {
            "status": result.status,
            "files_indexed": result.files_indexed,
            "files_skipped": result.files_skipped,
            "symbols_indexed": result.symbols_indexed,
            **({"error_code": result.error_code} if result.error_code else {}),
        }
    except ValueError:
        return {"status": "failed", "error_code": "invalid_index_id"}
    except GitHubClientError as exc:
        await _mark_failed(sessions, index_uuid, exc.code)
        return {"status": "failed", "error_code": exc.code}
    except EmbeddingError as exc:
        await _mark_failed(sessions, index_uuid, exc.code)
        return {"status": "failed", "error_code": exc.code}
    except Exception:
        logger.error("Repository index task failed with an internal error")
        await _mark_failed(sessions, index_uuid, "index_worker_error")
        return {"status": "failed", "error_code": "index_worker_error"}
    finally:
        if client is not None and github_client is None:
            try:
                await client.aclose()
            except Exception:
                logger.warning("Repository index GitHub client cleanup failed")
        if owned_engine is not None:
            await owned_engine.dispose()


async def _mark_failed(
    sessions: async_sessionmaker[AsyncSession] | None,
    index_id: uuid.UUID | None,
    code: str,
) -> None:
    if sessions is None or index_id is None:
        return
    try:
        async with sessions.begin() as session:
            index = await session.get(RepositoryIndex, index_id)
            if index and index.status not in {"ready", "failed"}:
                index.status = "failed"
                index.error_code = code[:100]
                index.completed_at = datetime.now(UTC)
    except Exception:
        logger.error("Unable to persist repository index failure status")


async def run_index_reconciliation_async(
    *,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    enqueue=None,
) -> int:
    enqueue = enqueue or index_repository_task_delay
    owned_engine = None
    if sessions is None:
        owned_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.index_stale_seconds)
        async with sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(RepositoryIndex)
                    .where(
                        or_(
                            RepositoryIndex.status == "queued",
                            (RepositoryIndex.status == "indexing")
                            & (RepositoryIndex.updated_at < cutoff),
                        )
                    )
                    .order_by(RepositoryIndex.created_at, RepositoryIndex.id)
                    .limit(100)
                )
            )
            ids = [str(row.id) for row in rows]
            for row in rows:
                if row.status == "indexing":
                    row.status = "queued"
        dispatched = 0
        for index_id in ids:
            try:
                enqueue(index_id)
                dispatched += 1
            except Exception:
                logger.warning("Repository index re-enqueue deferred", extra={"index_id": index_id})
        return dispatched
    finally:
        if owned_engine is not None:
            await owned_engine.dispose()


async def cleanup_old_repository_indexes_async(
    *,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    cutoff: datetime | None = None,
    batch_size: int = 100,
) -> int:
    """Delete expired indexes only when no stored review references their repo and SHA."""
    owned_engine = None
    if sessions is None:
        owned_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    cutoff = cutoff or datetime.now(UTC) - timedelta(days=settings.index_retention_days)
    batch_size = max(1, min(batch_size, 1000))
    try:
        async with sessions.begin() as session:
            referenced_by_review = exists(
                select(Review.id)
                .join(PullRequest, PullRequest.id == Review.pull_request_id)
                .where(
                    PullRequest.repository_id == RepositoryIndex.repository_id,
                    Review.commit_sha == RepositoryIndex.commit_sha,
                )
            )
            indexes = list(
                await session.scalars(
                    select(RepositoryIndex)
                    .where(
                        RepositoryIndex.created_at < cutoff,
                        RepositoryIndex.status.in_(("ready", "failed")),
                        ~referenced_by_review,
                    )
                    .order_by(RepositoryIndex.created_at, RepositoryIndex.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            index_ids = [index.id for index in indexes]
            if not index_ids:
                return 0
            file_ids = select(CodeFile.id).where(CodeFile.index_id.in_(index_ids))
            await session.execute(delete(SymbolEdge).where(SymbolEdge.index_id.in_(index_ids)))
            await session.execute(delete(CodeSymbol).where(CodeSymbol.file_id.in_(file_ids)))
            await session.execute(delete(CodeFile).where(CodeFile.index_id.in_(index_ids)))
            await session.execute(delete(RepositoryIndex).where(RepositoryIndex.id.in_(index_ids)))
            orphaned_embeddings = ~exists(
                select(CodeSymbol.id).where(
                    CodeSymbol.content_hash == EmbeddingCache.content_hash,
                    CodeSymbol.embedding_model == EmbeddingCache.model_name,
                    CodeSymbol.embedding_version == EmbeddingCache.model_version,
                )
            )
            await session.execute(delete(EmbeddingCache).where(orphaned_embeddings))
            return len(index_ids)
    finally:
        if owned_engine is not None:
            await owned_engine.dispose()


def index_repository_task_delay(index_id: str) -> None:
    index_repository_task.delay(index_id)


@celery_app.task(
    name="reviewpilot.index_repository_commit",
    autoretry_for=(),
    acks_late=True,
    reject_on_worker_lost=True,
)
def index_repository_task(index_id: str) -> dict[str, str | int]:
    return asyncio.run(run_index_task_async(index_id))


@celery_app.task(name="reviewpilot.reconcile_repository_indexes")
def reconcile_repository_indexes_task() -> dict[str, int]:
    return {"dispatched": asyncio.run(run_index_reconciliation_async())}


@celery_app.task(name="reviewpilot.cleanup_old_repository_indexes")
def cleanup_old_repository_indexes_task() -> dict[str, int]:
    deleted = asyncio.run(cleanup_old_repository_indexes_async())
    return {"deleted_indexes": deleted}
