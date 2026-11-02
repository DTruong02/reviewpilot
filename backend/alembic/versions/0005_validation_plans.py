"""Add bounded validation plan, approval, and simulation records."""

import sqlalchemy as sa

from alembic import op

revision = "0005_validation_plans"
down_revision = "0004_review_context_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "validation_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("policy_version", sa.String(length=30), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_validation_plans_finding_id", "validation_plans", ["finding_id"])
    op.create_index("ix_validation_plans_repository_id", "validation_plans", ["repository_id"])
    op.create_table(
        "validation_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("approver", sa.String(length=100), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["plan_id"], ["validation_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id"),
    )
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("conclusion", sa.String(length=30), nullable=False),
        sa.Column("executor", sa.String(length=50), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["plan_id"], ["validation_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_validation_runs_plan_id", "validation_runs", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_validation_runs_plan_id", table_name="validation_runs")
    op.drop_table("validation_runs")
    op.drop_table("validation_approvals")
    op.drop_index("ix_validation_plans_repository_id", table_name="validation_plans")
    op.drop_index("ix_validation_plans_finding_id", table_name="validation_plans")
    op.drop_table("validation_plans")
