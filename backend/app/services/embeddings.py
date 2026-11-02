"""Replaceable, versioned source embedding adapter.

The adapter accepts only explicitly selected symbol summaries; callers enforce repository
source-upload consent before invoking it. No code content is written to logs.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI


class EmbeddingError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EmbeddingProvider(Protocol):
    model_name: str
    model_version: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class DeterministicHashEmbeddingProvider:
    """Offline fixture/demo adapter; these vectors are deterministic, not semantic."""

    model_name = "local-deterministic-mock"
    model_version = "sha256-projection-v1"

    def __init__(self, dimension: int) -> None:
        if dimension < 1:
            raise EmbeddingError("embedding_dimension_invalid")
        self.dimension = dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            seed = text.encode("utf-8")
            data = bytearray()
            counter = 0
            while len(data) < self.dimension:
                data.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
                counter += 1
            vector = [(byte / 127.5) - 1.0 for byte in data[: self.dimension]]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class OpenAIEmbeddingProvider:
    """OpenAI embeddings implementation with injectable SDK client and safe errors."""

    model_version = "openai-embeddings-v1"

    def __init__(
        self,
        api_key: str,
        model_name: str,
        dimension: int,
        *,
        timeout_seconds: float = 45,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if not api_key or not model_name or dimension < 1:
            raise EmbeddingError("embedding_not_configured")
        self.model_name = model_name
        self.dimension = dimension
        self._client = client or AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            result = await self._client.embeddings.create(
                model=self.model_name,
                input=list(texts),
                dimensions=self.dimension,
            )
            return [list(item.embedding) for item in result.data]
        except Exception:
            # Provider exception messages can include request details; never return/log them.
            raise EmbeddingError("embedding_provider_failed") from None
