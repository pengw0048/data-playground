"""Allow linear_checkpoint_write Inbox emissions for the #414 product consumer.

Revision ID: 0017_linear_checkpoint_inbox
Revises: 0016_bounded_fanout_plan
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_linear_checkpoint_inbox"
down_revision = "0016_bounded_fanout_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind",
            "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write')",
        )


def downgrade() -> None:
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind",
            "task_kind IN ('managed_local_write','external_wait')",
        )
