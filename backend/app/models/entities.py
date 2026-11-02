import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


@compiles(Vector, "sqlite")
def _compile_vector_for_sqlite(_type, _compiler, **_kw):
    """Keep SQLite test schemas usable; production migrations use PostgreSQL vector."""
    return "TEXT"


class ReviewStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    superseded = "superseded"


class Severity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Category(StrEnum):
    bug = "bug"
    security = "security"
    reliability = "reliability"
    maintainability = "maintainability"


class Installation(Base):
    __tablename__ = "installations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    account_login: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), nullable=False
    )
    github_repository_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repository_id", "github_pr_number"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    github_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    html_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    github_delivery_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processing_status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("pull_request_id", "commit_sha"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False), nullable=False
    )
    model_name: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error_code: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    processing_token: Mapped[str | None] = mapped_column(String(36))
    context_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="diff_only")
    context_fallback_reason: Mapped[str | None] = mapped_column(String(100))
    context_metadata: Mapped[list[dict] | None] = mapped_column(JSON)


class ReviewOutbox(Base):
    """One durable dispatch record per review; retries reuse the same review identity."""

    __tablename__ = "review_outbox"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enqueue_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_number: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[Severity] = mapped_column(Enum(Severity, native_enum=False), nullable=False)
    category: Mapped[Category] = mapped_column(Enum(Category, native_enum=False), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    feedback: Mapped[str | None] = mapped_column(String(30))
    evidence_refs: Mapped[list[dict] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepositoryIndex(Base):
    __tablename__ = "repository_indexes"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha"),
        Index("ix_repository_indexes_status_started", "status", "started_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    files_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbols_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CodeFile(Base):
    __tablename__ = "code_files"
    __table_args__ = (
        UniqueConstraint("index_id", "path"),
        Index("ix_code_files_index_language", "index_id", "language"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    index_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_indexes.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str | None] = mapped_column(String(30))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imports_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(100))
    has_syntax_errors: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (
        UniqueConstraint("file_id", "symbol_key"),
        Index("ix_code_symbols_qualified_name", "qualified_name"),
        Index("ix_code_symbols_content_hash", "content_hash"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False
    )
    symbol_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    embedding_version: Mapped[str | None] = mapped_column(String(100))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)


class SymbolEdge(Base):
    """A statically resolved file import; dynamic and ambiguous references are omitted."""

    __tablename__ = "symbol_edges"
    __table_args__ = (
        UniqueConstraint("index_id", "source_file_id", "target_file_id", "edge_type"),
        CheckConstraint("source_file_id != target_file_id", name="ck_symbol_edges_not_self"),
        Index("ix_symbol_edges_index_source", "index_id", "source_file_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    index_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_indexes.id", ondelete="CASCADE"), nullable=False
    )
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False
    )
    target_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("code_files.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (Index("ix_embedding_cache_model_version", "model_name", "model_version"),)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(100), primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
