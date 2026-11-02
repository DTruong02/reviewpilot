"""Commit-scoped repository context retrieval for advisory reviews.

Source snippets are fetched on demand from the indexed commit and returned in
memory only. Provenance metadata is safe to persist without storing source.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CodeFile, CodeSymbol, RepositoryIndex, SymbolEdge
from app.schemas.review import PullRequestFile, RetrievedCodeContext
from app.services.context_builder import added_line_numbers, redact_obvious_secrets

FileFetcher = Callable[[str, str], Awaitable[str | None]]

RETRIEVAL_VERSION = "commit-context-v1"
MAX_SYMBOLS_SCANNED = 2_000
MAX_DIRECT_SYMBOLS = 500
MAX_GRAPH_EDGES = 1_000
MAX_ADJACENT_FILES = 100
MAX_ADJACENT_SYMBOLS = 500
MAX_CONTEXT_ITEMS = 40
MAX_CONTEXT_ITEM_LINES = 80
MAX_CONTEXT_ITEM_CHARS = 8_000


@dataclass(frozen=True)
class ContextRetrievalResult:
    mode: str
    reason: str | None
    items: tuple[RetrievedCodeContext, ...]


@dataclass(frozen=True)
class _Candidate:
    symbol: CodeSymbol
    file: CodeFile
    reason: str
    score: float


def _vector(value: object) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _snippet(source: str, start: int, end: int) -> tuple[str, int]:
    lines = source.splitlines()
    if start < 1 or start > len(lines):
        return "", start - 1
    upper = min(len(lines), end, start + MAX_CONTEXT_ITEM_LINES - 1)
    rendered: list[str] = []
    rendered_end = start - 1
    for number in range(start, upper + 1):
        line = redact_obvious_secrets(f"{number}: {lines[number - 1]}")
        combined_length = sum(len(item) + 1 for item in rendered) + len(line)
        if combined_length <= MAX_CONTEXT_ITEM_CHARS:
            rendered.append(line)
            rendered_end = number
            continue
        if not rendered:
            rendered.append(line[: MAX_CONTEXT_ITEM_CHARS - 1] + "…")
            rendered_end = number
        break
    return "\n".join(rendered), rendered_end


class RepositoryContextRetriever:
    """Retrieve symbols only from the exact repository index and commit SHA."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        max_symbols_scanned: int = MAX_SYMBOLS_SCANNED,
        max_items: int = MAX_CONTEXT_ITEMS,
    ) -> None:
        self.sessions = sessions
        self.max_symbols_scanned = max(1, min(max_symbols_scanned, MAX_SYMBOLS_SCANNED))
        self.max_items = max(1, min(max_items, MAX_CONTEXT_ITEMS))

    async def retrieve(
        self,
        *,
        repository_id: uuid.UUID,
        head_sha: str,
        changed_files: Sequence[PullRequestFile],
        fetch_content: FileFetcher,
        token_budget: int,
    ) -> ContextRetrievalResult:
        if token_budget <= 0:
            return ContextRetrievalResult("diff_only_fallback", "context_budget_exhausted", ())
        async with self.sessions() as session:
            index = await session.scalar(
                select(RepositoryIndex).where(
                    RepositoryIndex.repository_id == repository_id,
                    RepositoryIndex.commit_sha == head_sha.lower(),
                )
            )
            if index is None:
                return ContextRetrievalResult("diff_only_fallback", "index_not_requested", ())
            if index.status != "ready":
                return ContextRetrievalResult("diff_only_fallback", f"index_{index.status}", ())

            changed_lines = {
                item.path: added_line_numbers(item.patch or "") for item in changed_files
            }
            changed_paths = {path for path, lines in changed_lines.items() if lines}
            if not changed_paths:
                return ContextRetrievalResult("diff_only_fallback", "no_changed_lines", ())

            file_rows = list(
                await session.scalars(
                    select(CodeFile).where(
                        CodeFile.index_id == index.id,
                        CodeFile.status == "indexed",
                        CodeFile.path.in_(changed_paths),
                    )
                )
            )
            files_by_id = {item.id: item for item in file_rows}
            symbols = (
                list(
                    await session.scalars(
                        select(CodeSymbol)
                        .where(CodeSymbol.file_id.in_(files_by_id))
                        .order_by(CodeSymbol.start_line, CodeSymbol.id)
                        .limit(MAX_DIRECT_SYMBOLS)
                    )
                )
                if files_by_id
                else []
            )
            direct = [
                _Candidate(symbol, files_by_id[symbol.file_id], "changed_symbol", 1.0)
                for symbol in symbols
                if any(
                    symbol.start_line <= line <= symbol.end_line
                    for line in changed_lines.get(files_by_id[symbol.file_id].path, ())
                )
            ]
            if not direct:
                return ContextRetrievalResult("diff_only_fallback", "changed_symbol_not_found", ())

            candidates: dict[uuid.UUID, _Candidate] = {item.symbol.id: item for item in direct}
            changed_file_ids = {item.id for item in files_by_id.values()}
            edges = list(
                await session.scalars(
                    select(SymbolEdge)
                    .where(
                        SymbolEdge.index_id == index.id,
                        (SymbolEdge.source_file_id.in_(changed_file_ids))
                        | (SymbolEdge.target_file_id.in_(changed_file_ids)),
                    )
                    .order_by(SymbolEdge.source_file_id, SymbolEdge.target_file_id, SymbolEdge.id)
                    .limit(MAX_GRAPH_EDGES)
                )
            )
            adjacent_file_reasons: dict[uuid.UUID, str] = {}
            for edge in edges:
                if edge.source_file_id in changed_file_ids:
                    adjacent_file_reasons.setdefault(edge.target_file_id, "imported_file")
                if edge.target_file_id in changed_file_ids:
                    adjacent_file_reasons.setdefault(edge.source_file_id, "importer_file")
            adjacent_file_ids = sorted(adjacent_file_reasons, key=str)[:MAX_ADJACENT_FILES]
            if adjacent_file_ids:
                adjacent_files = list(
                    await session.scalars(
                        select(CodeFile).where(
                            CodeFile.index_id == index.id,
                            CodeFile.status == "indexed",
                            CodeFile.id.in_(adjacent_file_ids),
                        )
                    )
                )
                adjacent_symbols_seen = 0
                for file in adjacent_files:
                    remaining_adjacent = MAX_ADJACENT_SYMBOLS - adjacent_symbols_seen
                    if remaining_adjacent <= 0:
                        break
                    for symbol in await session.scalars(
                        select(CodeSymbol)
                        .where(CodeSymbol.file_id == file.id)
                        .order_by(CodeSymbol.start_line, CodeSymbol.id)
                        .limit(min(20, remaining_adjacent))
                    ):
                        adjacent_symbols_seen += 1
                        candidates.setdefault(
                            symbol.id,
                            _Candidate(symbol, file, adjacent_file_reasons[file.id], 0.75),
                        )

            anchor_vectors = [
                vector
                for candidate in direct
                if (vector := _vector(candidate.symbol.embedding)) is not None
                and candidate.symbol.embedding_model == index.embedding_model
                and candidate.symbol.embedding_version == index.embedding_version
                and candidate.symbol.embedding_dimension == index.embedding_dimension
            ]
            semantic_vectors_enabled = (
                index.embedding_model.casefold() != "local-deterministic-mock"
                and not index.embedding_model.casefold().startswith("mock-")
            )
            if index.embedding_enabled and semantic_vectors_enabled and anchor_vectors:
                semantic_query = (
                    select(CodeSymbol, CodeFile)
                    .join(CodeFile, CodeFile.id == CodeSymbol.file_id)
                    .where(
                        CodeFile.index_id == index.id,
                        CodeFile.status == "indexed",
                        CodeSymbol.embedding_model == index.embedding_model,
                        CodeSymbol.embedding_version == index.embedding_version,
                        CodeSymbol.embedding_dimension == index.embedding_dimension,
                        CodeSymbol.embedding.is_not(None),
                    )
                )
                if session.get_bind().dialect.name == "postgresql":
                    distances = [
                        CodeSymbol.embedding.cosine_distance(vector)
                        for vector in anchor_vectors[:8]
                    ]
                    semantic_query = semantic_query.order_by(
                        distances[0] if len(distances) == 1 else func.least(*distances)
                    )
                else:
                    semantic_query = semantic_query.order_by(
                        CodeFile.path, CodeSymbol.start_line, CodeSymbol.id
                    )
                semantic_rows = await session.execute(
                    semantic_query.limit(self.max_symbols_scanned)
                )
                scored: list[tuple[float, CodeSymbol, CodeFile]] = []
                for symbol, file in semantic_rows:
                    if symbol.id in candidates:
                        continue
                    candidate_vector = _vector(symbol.embedding)
                    if candidate_vector is None:
                        continue
                    score = max(_cosine(anchor, candidate_vector) for anchor in anchor_vectors)
                    if score > 0:
                        scored.append((score, symbol, file))
                scored.sort(key=lambda item: (-item[0], item[2].path, item[1].start_line))
                for score, symbol, file in scored[: self.max_items]:
                    candidates.setdefault(
                        symbol.id,
                        _Candidate(symbol, file, "semantic_similarity", max(0.0, min(score, 1.0))),
                    )

            ordered = sorted(
                candidates.values(),
                key=lambda item: (
                    (
                        0
                        if item.reason == "changed_symbol"
                        else 1
                        if item.reason in {"imported_file", "importer_file"}
                        else 2
                    ),
                    -item.score,
                    item.file.path,
                    item.symbol.start_line,
                    str(item.symbol.id),
                ),
            )[: self.max_items]

        source_cache: dict[str, str | None] = {}
        result: list[RetrievedCodeContext] = []
        remaining = token_budget
        for candidate in ordered:
            path = candidate.file.path
            if path not in source_cache:
                source_cache[path] = await fetch_content(path, head_sha.lower())
            source = source_cache[path]
            if source is None:
                continue
            body, rendered_end_line = _snippet(
                source, candidate.symbol.start_line, candidate.symbol.end_line
            )
            if not body:
                continue
            metadata_chars = len(path) + len(candidate.reason) + len(str(candidate.symbol.id)) + 96
            cost = max(1, math.ceil((len(body) + metadata_chars) / 4))
            if cost > remaining:
                continue
            result.append(
                RetrievedCodeContext(
                    file_path=path,
                    symbol_id=str(candidate.symbol.id),
                    start_line=candidate.symbol.start_line,
                    end_line=rendered_end_line,
                    commit_sha=head_sha.lower(),
                    retrieval_reason=candidate.reason,
                    score=round(candidate.score, 4),
                    snippet=body,
                )
            )
            remaining -= cost

        if not result:
            return ContextRetrievalResult("diff_only_fallback", "no_retrievable_symbols", ())
        return ContextRetrievalResult("context_aware", None, tuple(result))
