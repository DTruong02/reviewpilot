"""Allow at most one gate result per validation plan."""

from alembic import op

revision = "0006_single_validation_run"
down_revision = "0005_validation_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("validation_runs") as batch_op:
        batch_op.create_unique_constraint("uq_validation_runs_plan_id", ["plan_id"])


def downgrade() -> None:
    with op.batch_alter_table("validation_runs") as batch_op:
        batch_op.drop_constraint("uq_validation_runs_plan_id", type_="unique")
