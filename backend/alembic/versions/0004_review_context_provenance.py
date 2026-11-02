"""Persist retrieval mode and source-free provenance for reviews."""

import sqlalchemy as sa

from alembic import op

revision = "0004_review_context_provenance"
down_revision = "0003_repository_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reviews",
        sa.Column("context_mode", sa.String(length=30), nullable=False, server_default="diff_only"),
    )
    op.add_column("reviews", sa.Column("context_fallback_reason", sa.String(length=100)))
    op.add_column("reviews", sa.Column("context_metadata", sa.JSON()))
    op.add_column("findings", sa.Column("evidence_refs", sa.JSON()))


def downgrade() -> None:
    op.drop_column("findings", "evidence_refs")
    op.drop_column("reviews", "context_metadata")
    op.drop_column("reviews", "context_fallback_reason")
    op.drop_column("reviews", "context_mode")
