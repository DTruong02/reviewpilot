"""Local-only commit index submission and status endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.models.entities import CodeFile, Repository, RepositoryIndex, SymbolEdge
from app.schemas.api import IndexRequest, IndexStatusRead
from app.services.repository_indexer import PARSER_VERSION
from app.workers.index_tasks import index_repository_task

router = APIRouter(prefix="/api/repositories", tags=["repository indexes"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
logger = logging.getLogger(__name__)
EMBEDDING_VERSION = "openai-embeddings-v1"


def _embedding_allowed(full_name: str) -> bool:
    allowlist = {
        name.strip().casefold()
        for name in settings.index_embedding_repository_allowlist.split(",")
        if name.strip()
    }
    return full_name.casefold() in allowlist


def _embedding_profile(full_name: str) -> tuple[bool, str, str]:
    if settings.index_embedding_mode == "mock":
        return True, "local-deterministic-mock", "sha256-projection-v1"
    if (
        settings.index_embedding_mode == "openai"
        and settings.review_allow_source_upload
        and _embedding_allowed(full_name)
    ):
        return True, settings.index_embedding_model, EMBEDDING_VERSION
    return False, "disabled", "disabled"


def _read(index: RepositoryIndex) -> IndexStatusRead:
    return IndexStatusRead.model_validate(index, from_attributes=True)


@router.post(
    "/{repository_id}/indexes", status_code=status.HTTP_202_ACCEPTED, response_model=IndexStatusRead
)
async def request_commit_index(
    repository_id: uuid.UUID,
    payload: IndexRequest,
    session: SessionDep,
    response: Response,
) -> IndexStatusRead:
    if not settings.index_enabled:
        raise HTTPException(
            status_code=503,
            detail={"code": "indexing_disabled", "message": "Repository indexing is disabled."},
        )
    repository = await session.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "repository_not_found", "message": "Repository was not found."},
        )
    sha = payload.commit_sha.lower()
    index = await session.scalar(
        select(RepositoryIndex).where(
            RepositoryIndex.repository_id == repository_id,
            RepositoryIndex.commit_sha == sha,
        )
    )
    should_enqueue = False
    if index is None:
        embedding_enabled, model_name, model_version = _embedding_profile(repository.full_name)
        index = RepositoryIndex(
            id=uuid.uuid4(),
            repository_id=repository_id,
            commit_sha=sha,
            status="queued",
            parser_version=PARSER_VERSION,
            embedding_model=model_name,
            embedding_version=model_version,
            embedding_dimension=settings.index_embedding_dimension,
            embedding_enabled=embedding_enabled,
        )
        session.add(index)
        should_enqueue = True
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            index = await session.scalar(
                select(RepositoryIndex).where(
                    RepositoryIndex.repository_id == repository_id,
                    RepositoryIndex.commit_sha == sha,
                )
            )
            should_enqueue = False
    elif index.status == "failed":
        embedding_enabled, expected_model, expected_version = _embedding_profile(
            repository.full_name
        )
        if (
            index.parser_version != PARSER_VERSION
            or index.embedding_model != expected_model
            or index.embedding_version != expected_version
            or index.embedding_dimension != settings.index_embedding_dimension
        ):
            await session.execute(delete(SymbolEdge).where(SymbolEdge.index_id == index.id))
            await session.execute(delete(CodeFile).where(CodeFile.index_id == index.id))
            index.files_discovered = 0
            index.files_indexed = 0
            index.files_skipped = 0
            index.symbols_indexed = 0
        index.status = "queued"
        index.parser_version = PARSER_VERSION
        index.embedding_model = expected_model
        index.embedding_version = expected_version
        index.embedding_dimension = settings.index_embedding_dimension
        index.embedding_enabled = embedding_enabled
        index.error_code = None
        index.completed_at = None
        should_enqueue = True
        await session.commit()
    if index is None:
        raise HTTPException(
            status_code=500,
            detail={"code": "index_state_unavailable", "message": "Index state is unavailable."},
        )
    if should_enqueue:
        try:
            index_repository_task.delay(str(index.id))
        except Exception:
            # Durable queued state is reconciled by Beat; no provider or source details are logged.
            logger.warning("Repository index enqueue deferred", extra={"index_id": str(index.id)})
    else:
        response.status_code = status.HTTP_200_OK
    return _read(index)


@router.get("/{repository_id}/indexes/{commit_sha}", response_model=IndexStatusRead)
async def get_commit_index(
    repository_id: uuid.UUID, commit_sha: str, session: SessionDep
) -> IndexStatusRead:
    if len(commit_sha) != 40 or any(char not in "0123456789abcdefABCDEF" for char in commit_sha):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_commit_sha",
                "message": "Commit SHA must be 40 hexadecimal characters.",
            },
        )
    index = await session.scalar(
        select(RepositoryIndex).where(
            RepositoryIndex.repository_id == repository_id,
            RepositoryIndex.commit_sha == commit_sha.lower(),
        )
    )
    if index is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "repository_index_not_found",
                "message": "Repository index was not found.",
            },
        )
    return _read(index)
