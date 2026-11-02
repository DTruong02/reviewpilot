"""Add immutable commit-scoped source indexes and versioned embedding cache."""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "0003_repository_indexes"
down_revision = "0002_review_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "repository_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding_version", sa.String(length=100), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding_enabled", sa.Boolean(), nullable=False),
        sa.Column("files_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbols_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "commit_sha"),
    )
    op.create_index(
        "ix_repository_indexes_status_started", "repository_indexes", ["status", "started_at"]
    )
    op.create_table(
        "code_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.String(length=40), nullable=False),
        sa.Column("language", sa.String(length=30)),
        sa.Column("content_hash", sa.String(length=64)),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imports_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.String(length=100)),
        sa.Column("has_syntax_errors", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_id", "path"),
    )
    op.create_index("ix_code_files_index_language", "code_files", ["index_id", "language"])
    op.create_table(
        "code_symbols",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("symbol_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector()),
        sa.Column("embedding_model", sa.String(length=200)),
        sa.Column("embedding_version", sa.String(length=100)),
        sa.Column("embedding_dimension", sa.Integer()),
        sa.ForeignKeyConstraint(["file_id"], ["code_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "symbol_key"),
    )
    op.create_index("ix_code_symbols_qualified_name", "code_symbols", ["qualified_name"])
    op.create_index("ix_code_symbols_content_hash", "code_symbols", ["content_hash"])
    op.create_table(
        "symbol_edges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=False),
        sa.Column("target_file_id", sa.Uuid(), nullable=False),
        sa.Column("edge_type", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.CheckConstraint("source_file_id != target_file_id", name="ck_symbol_edges_not_self"),
        sa.ForeignKeyConstraint(["index_id"], ["repository_indexes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_file_id"], ["code_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_file_id"], ["code_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_id", "source_file_id", "target_file_id", "edge_type"),
    )
    op.create_index("ix_symbol_edges_index_source", "symbol_edges", ["index_id", "source_file_id"])
    op.create_table(
        "embedding_cache",
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.PrimaryKeyConstraint("content_hash", "model_name", "model_version"),
    )
    op.create_index(
        "ix_embedding_cache_model_version", "embedding_cache", ["model_name", "model_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_cache_model_version", table_name="embedding_cache")
    op.drop_table("embedding_cache")
    op.drop_index("ix_symbol_edges_index_source", table_name="symbol_edges")
    op.drop_table("symbol_edges")
    op.drop_index("ix_code_symbols_content_hash", table_name="code_symbols")
    op.drop_index("ix_code_symbols_qualified_name", table_name="code_symbols")
    op.drop_table("code_symbols")
    op.drop_index("ix_code_files_index_language", table_name="code_files")
    op.drop_table("code_files")
    op.drop_index("ix_repository_indexes_status_started", table_name="repository_indexes")
    op.drop_table("repository_indexes")
