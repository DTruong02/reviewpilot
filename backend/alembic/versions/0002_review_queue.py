"""Add a durable review dispatch record and worker lease token."""

import sqlalchemy as sa

from alembic import op

revision = "0002_review_queue"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reviews", sa.Column("processing_token", sa.String(length=36), nullable=True))
    op.create_table(
        "review_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("enqueue_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id"),
    )
    op.create_index("ix_review_outbox_last_enqueued_at", "review_outbox", ["last_enqueued_at"])


def downgrade() -> None:
    op.drop_index("ix_review_outbox_last_enqueued_at", table_name="review_outbox")
    op.drop_table("review_outbox")
    op.drop_column("reviews", "processing_token")
