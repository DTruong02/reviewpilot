"""Resumable immutable commit indexing; repository source is parsed as data only."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    CodeFile,
    CodeSymbol,
    EmbeddingCache,
    Installation,
    Repository,
    RepositoryIndex,
    SymbolEdge,
)
from app.services.code_parser import CodeParser, TreeSitterCodeParser
from app.services.embeddings import EmbeddingError, EmbeddingProvider
from app.services.github import GitHubClientError, GitTreeFile

PARSER_VERSION = "tree-sitter-symbols-v1"
INDEX_NAMESPACE = uuid.UUID("86ad987d-0fb7-47a1-844b-9dfedaf684e3")
_DEFAULT_SECRET_PATTERNS = (
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"(?i)\bAKIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"][^'\"]{8,}",
)


class IndexingError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GitHubIndexClient(Protocol):
    async def fetch_commit_tree(
        self,
        installation_id: int,
        owner: str,
        repository: str,
        commit_sha: str,
        *,
        max_entries: int,
    ) -> list[GitTreeFile]: ...

    async def fetch_file_content(
        self, installation_id: int, owner: str, repository: str, path: str, ref: str
    ) -> str | None: ...


@dataclass(frozen=True)
class IndexResult:
    index_id: uuid.UUID
    status: str
    files_indexed: int
    files_skipped: int
    symbols_indexed: int
    error_code: str | None = None


@dataclass(frozen=True)
class _IndexClaim:
    index_id: uuid.UUID
    commit_sha: str
    installation_id: int
    owner: str
    repository: str


class RepositoryIndexer:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        github: GitHubIndexClient,
        *,
        parser: CodeParser | None = None,
        embeddings: EmbeddingProvider | None = None,
        max_files: int = 5000,
        max_file_bytes: int = 1_000_000,
        max_total_bytes: int = 50_000_000,
        max_tree_entries: int = 100_000,
        secret_patterns: tuple[str, ...] = (),
        stale_seconds: int = 1800,
    ) -> None:
        self.sessions = sessions
        self.github = github
        self.parser = parser or TreeSitterCodeParser(max_source_bytes=max_file_bytes)
        self.embeddings = embeddings
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_tree_entries = max_tree_entries
        self.stale_seconds = stale_seconds
        try:
            self.secret_patterns = [
                re.compile(pattern) for pattern in (*_DEFAULT_SECRET_PATTERNS, *secret_patterns)
            ]
        except re.error:
            raise IndexingError("index_secret_pattern_invalid") from None

    async def index_commit(self, index_id: uuid.UUID) -> IndexResult:
        claimed = await self._claim(index_id)
        if claimed is None:
            return await self._result(index_id)
        claim = claimed
        try:
            tree = await self.github.fetch_commit_tree(
                claim.installation_id,
                claim.owner,
                claim.repository,
                claim.commit_sha,
                max_entries=self.max_tree_entries,
            )
            if len(tree) > self.max_files:
                raise IndexingError("index_file_limit_exceeded")
            known_bytes = sum(
                item.size_bytes
                for item in tree
                if item.size_bytes is not None and item.size_bytes <= self.max_file_bytes
            )
            if known_bytes > self.max_total_bytes:
                raise IndexingError("index_total_size_limit_exceeded")
            await self._set_discovered(index_id, len(tree))

            for item in tree:
                await self._index_file(claim, item)
                await self.touch_index(index_id)

            await self._build_import_edges(index_id)
            async with self.sessions.begin() as session:
                current = await session.get(RepositoryIndex, index_id)
                if current is None:
                    raise IndexingError("index_not_found")
                indexed = (
                    await session.scalar(
                        select(func.count())
                        .select_from(CodeFile)
                        .where(CodeFile.index_id == index_id, CodeFile.status == "indexed")
                    )
                    or 0
                )
                skipped = (
                    await session.scalar(
                        select(func.count())
                        .select_from(CodeFile)
                        .where(CodeFile.index_id == index_id, CodeFile.status == "skipped")
                    )
                    or 0
                )
                symbols = (
                    await session.scalar(
                        select(func.count())
                        .select_from(CodeSymbol)
                        .join(CodeFile)
                        .where(CodeFile.index_id == index_id)
                    )
                    or 0
                )
                if indexed + skipped != current.files_discovered:
                    raise IndexingError("index_consistency_check_failed")
                current.files_indexed = indexed
                current.files_skipped = skipped
                current.symbols_indexed = symbols
                current.status = "ready"
                current.error_code = None
                current.completed_at = datetime.now(UTC)
            return await self._result(index_id)
        except (IndexingError, GitHubClientError, EmbeddingError) as exc:
            code = exc.code
            await self._mark_failed(index_id, code)
            return await self._result(index_id)
        except Exception:
            await self._mark_failed(index_id, "index_internal_error")
            return await self._result(index_id)

    async def _claim(self, index_id: uuid.UUID) -> _IndexClaim | None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            index = await session.scalar(
                select(RepositoryIndex).where(RepositoryIndex.id == index_id).with_for_update()
            )
            if index is None or index.status == "ready":
                return None
            if index.status == "indexing" and index.started_at:
                started = index.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if now - started < timedelta(seconds=self.stale_seconds):
                    return None
            repository = await session.get(Repository, index.repository_id)
            if repository is None:
                raise IndexingError("index_repository_not_found")
            installation = await session.get(Installation, repository.installation_id)
            if installation is None:
                raise IndexingError("index_installation_not_found")
            index.status = "indexing"
            index.started_at = now
            index.completed_at = None
            index.error_code = None
            index.embedding_enabled = self.embeddings is not None
            if self.embeddings is not None:
                index.embedding_model = self.embeddings.model_name
                index.embedding_version = self.embeddings.model_version
                index.embedding_dimension = self.embeddings.dimension
            else:
                index.embedding_model = "disabled"
                index.embedding_version = "disabled"
            return _IndexClaim(
                index_id=index.id,
                commit_sha=index.commit_sha,
                installation_id=installation.github_installation_id,
                owner=repository.owner,
                repository=repository.name,
            )

    async def _index_file(self, index: _IndexClaim, item: GitTreeFile) -> None:
        file_id = uuid.uuid5(index.index_id, item.path)
        async with self.sessions() as session:
            existing = await session.get(CodeFile, file_id)
            if existing and existing.blob_sha == item.blob_sha:
                if existing.status == "skipped":
                    return
                symbols = list(
                    await session.scalars(select(CodeSymbol).where(CodeSymbol.file_id == file_id))
                )
                expected = self.embeddings
                if all(
                    (symbol.embedding_model, symbol.embedding_version, symbol.embedding_dimension)
                    == (
                        expected.model_name,
                        expected.model_version,
                        expected.dimension,
                    )
                    if expected
                    else symbol.embedding is None
                    for symbol in symbols
                ):
                    return

        if item.size_bytes is not None and item.size_bytes > self.max_file_bytes:
            await self._save_skipped(index.index_id, item, "file_size_limit")
            return
        content = await self.github.fetch_file_content(
            index.installation_id,
            index.owner,
            index.repository,
            item.path,
            index.commit_sha,
        )
        if content is None:
            await self._save_skipped(index.index_id, item, "source_unavailable")
            return
        raw = content.encode("utf-8")
        if len(raw) > self.max_file_bytes:
            await self._save_skipped(index.index_id, item, "file_size_limit", len(raw))
            return
        git_object = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
        if hashlib.sha1(git_object).hexdigest() != item.blob_sha:
            raise IndexingError("github_blob_hash_mismatch")
        if b"\x00" in raw:
            await self._save_skipped(index.index_id, item, "binary_content", len(raw))
            return
        if any(pattern.search(content) for pattern in self.secret_patterns):
            await self._save_skipped(index.index_id, item, "secret_pattern_detected", len(raw))
            return

        parsed = self.parser.parse(item.path, raw)
        if parsed.status != "parsed":
            await self._save_skipped(
                index.index_id, item, parsed.skip_reason or parsed.status, len(raw)
            )
            return
        symbol_hashes = [
            hashlib.sha256(raw[symbol.start_byte : symbol.end_byte]).hexdigest()
            for symbol in parsed.symbols
        ]
        vectors = await self._vectors(
            symbol_hashes,
            [
                (symbol.signature + "\n" + (symbol.docstring_summary or ""))[:6000]
                for symbol in parsed.symbols
            ],
        )

        async with self.sessions.begin() as session:
            file_record = await session.get(CodeFile, file_id)
            if file_record is None:
                file_record = CodeFile(
                    id=file_id,
                    index_id=index.index_id,
                    path=item.path,
                    blob_sha=item.blob_sha,
                    language=parsed.language,
                    content_hash=parsed.content_hash,
                    size_bytes=parsed.size_bytes,
                    line_count=parsed.line_count,
                    imports_json=json.dumps(
                        [entry.module for entry in parsed.imports if entry.module],
                        separators=(",", ":"),
                    ),
                    status="indexed",
                    has_syntax_errors=parsed.has_syntax_errors,
                )
                session.add(file_record)
            else:
                file_record.blob_sha = item.blob_sha
                file_record.language = parsed.language
                file_record.content_hash = parsed.content_hash
                file_record.size_bytes = parsed.size_bytes
                file_record.line_count = parsed.line_count
                file_record.imports_json = json.dumps(
                    [entry.module for entry in parsed.imports if entry.module],
                    separators=(",", ":"),
                )
                file_record.status = "indexed"
                file_record.skip_reason = None
                file_record.has_syntax_errors = parsed.has_syntax_errors
                await session.execute(delete(CodeSymbol).where(CodeSymbol.file_id == file_id))
            for symbol, content_hash, vector in zip(parsed.symbols, symbol_hashes, vectors):
                symbol_id = uuid.uuid5(index.index_id, f"{item.path}:{symbol.symbol_key}")
                session.add(
                    CodeSymbol(
                        id=symbol_id,
                        file_id=file_id,
                        symbol_key=symbol.symbol_key,
                        kind=symbol.kind,
                        qualified_name=symbol.qualified_name,
                        start_line=symbol.start_line,
                        end_line=symbol.end_line,
                        content_hash=content_hash,
                        embedding=vector,
                        embedding_model=self.embeddings.model_name if self.embeddings else None,
                        embedding_version=self.embeddings.model_version
                        if self.embeddings
                        else None,
                        embedding_dimension=self.embeddings.dimension if self.embeddings else None,
                    )
                )

    async def _vectors(
        self, content_hashes: list[str], texts: list[str]
    ) -> list[list[float] | None]:
        if self.embeddings is None or not texts:
            return [None for _ in texts]
        vectors: list[list[float] | None] = [None] * len(texts)
        missing_indices: list[int] = []
        async with self.sessions() as session:
            for position, content_hash in enumerate(content_hashes):
                cached = await session.get(
                    EmbeddingCache,
                    {
                        "content_hash": content_hash,
                        "model_name": self.embeddings.model_name,
                        "model_version": self.embeddings.model_version,
                    },
                )
                if cached and cached.dimension == self.embeddings.dimension:
                    vectors[position] = list(cached.embedding)
                else:
                    missing_indices.append(position)
        if missing_indices:
            generated = await self.embeddings.embed(
                [texts[position] for position in missing_indices]
            )
            if len(generated) != len(missing_indices):
                raise IndexingError("embedding_response_invalid")
            for position, vector in zip(missing_indices, generated):
                if len(vector) != self.embeddings.dimension or not all(
                    math.isfinite(float(value)) for value in vector
                ):
                    raise IndexingError("embedding_dimension_mismatch")
                vectors[position] = [float(value) for value in vector]
        async with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            for position in missing_indices:
                vector = vectors[position]
                assert vector is not None
                values = {
                    "content_hash": content_hashes[position],
                    "model_name": self.embeddings.model_name,
                    "model_version": self.embeddings.model_version,
                    "dimension": self.embeddings.dimension,
                    "embedding": vector,
                }
                if dialect == "postgresql":
                    statement = (
                        pg_insert(EmbeddingCache)
                        .values(**values)
                        .on_conflict_do_nothing(
                            index_elements=["content_hash", "model_name", "model_version"]
                        )
                    )
                elif dialect == "sqlite":
                    statement = (
                        sqlite_insert(EmbeddingCache)
                        .values(**values)
                        .on_conflict_do_nothing(
                            index_elements=["content_hash", "model_name", "model_version"]
                        )
                    )
                else:
                    if (
                        await session.get(
                            EmbeddingCache,
                            {
                                "content_hash": values["content_hash"],
                                "model_name": values["model_name"],
                                "model_version": values["model_version"],
                            },
                        )
                        is not None
                    ):
                        continue
                    session.add(EmbeddingCache(**values))
                    continue
                await session.execute(statement)
        return vectors

    async def _save_skipped(
        self,
        index_id: uuid.UUID,
        item: GitTreeFile,
        reason: str,
        size_bytes: int | None = None,
    ) -> None:
        file_id = uuid.uuid5(index_id, item.path)
        async with self.sessions.begin() as session:
            existing = await session.get(CodeFile, file_id)
            if existing:
                existing.blob_sha = item.blob_sha
                existing.status = "skipped"
                existing.skip_reason = reason[:100]
                existing.size_bytes = size_bytes if size_bytes is not None else item.size_bytes or 0
                existing.content_hash = None
                existing.language = None
                existing.line_count = 0
                existing.imports_json = "[]"
                existing.has_syntax_errors = False
                await session.execute(delete(CodeSymbol).where(CodeSymbol.file_id == file_id))
            else:
                session.add(
                    CodeFile(
                        id=file_id,
                        index_id=index_id,
                        path=item.path,
                        blob_sha=item.blob_sha,
                        language=None,
                        content_hash=None,
                        size_bytes=size_bytes if size_bytes is not None else item.size_bytes or 0,
                        line_count=0,
                        imports_json="[]",
                        status="skipped",
                        skip_reason=reason[:100],
                        has_syntax_errors=False,
                    )
                )

    async def _build_import_edges(self, index_id: uuid.UUID) -> None:
        async with self.sessions.begin() as session:
            await session.execute(delete(SymbolEdge).where(SymbolEdge.index_id == index_id))
            files = list(
                await session.scalars(
                    select(CodeFile).where(
                        CodeFile.index_id == index_id, CodeFile.status == "indexed"
                    )
                )
            )
            by_path = {record.path: record for record in files}
            for source in files:
                seen_targets: set[str] = set()
                for module in json.loads(source.imports_json):
                    target_path = _resolve_import(source.path, module, by_path)
                    target = by_path.get(target_path or "")
                    if target is None or target.id == source.id or target.path in seen_targets:
                        continue
                    seen_targets.add(target.path)
                    edge_id = uuid.uuid5(index_id, f"imports:{source.path}:{target.path}")
                    session.add(
                        SymbolEdge(
                            id=edge_id,
                            index_id=index_id,
                            source_file_id=source.id,
                            target_file_id=target.id,
                            edge_type="imports",
                            confidence=1.0,
                        )
                    )

    async def _set_discovered(self, index_id: uuid.UUID, count: int) -> None:
        async with self.sessions.begin() as session:
            index = await session.get(RepositoryIndex, index_id)
            if index:
                index.files_discovered = count

    async def touch_index(self, index_id: uuid.UUID) -> None:
        async with self.sessions.begin() as session:
            index = await session.get(RepositoryIndex, index_id)
            if index and index.status == "indexing":
                index.updated_at = datetime.now(UTC)

    async def _mark_failed(self, index_id: uuid.UUID, code: str) -> None:
        async with self.sessions.begin() as session:
            index = await session.get(RepositoryIndex, index_id)
            if index:
                index.status = "failed"
                index.error_code = code[:100]
                index.completed_at = datetime.now(UTC)

    async def _result(self, index_id: uuid.UUID) -> IndexResult:
        async with self.sessions() as session:
            index = await session.get(RepositoryIndex, index_id)
            if index is None:
                return IndexResult(index_id, "failed", 0, 0, 0, "index_not_found")
            return IndexResult(
                index.id,
                index.status,
                index.files_indexed,
                index.files_skipped,
                index.symbols_indexed,
                index.error_code,
            )


def _resolve_import(source_path: str, module: str, files: dict[str, CodeFile]) -> str | None:
    """Resolve only explicit local module paths; bare packages remain unresolved."""
    if not module:
        return None
    parent = PurePosixPath(source_path).parent
    if module.startswith("."):
        relative_dots = len(module) - len(module.lstrip("."))
        remainder = module.lstrip(".").lstrip("/").replace(".", "/")
        target_parent = parent
        for _ in range(max(0, relative_dots - 1)):
            target_parent = target_parent.parent
        base = posixpath.normpath((target_parent / remainder).as_posix())
    elif source_path.endswith(".py"):
        base = module.replace(".", "/")
    else:
        return None
    candidates = [base]
    for suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        candidates.append(base + suffix)
        candidates.append(f"{base}/index{suffix}")
    return next((candidate for candidate in candidates if candidate in files), None)
