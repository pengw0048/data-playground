"""Allow bounded_fanout_write parent DurableTask and Inbox emissions.

Revision ID: 0018_bounded_fanout_write
Revises: 0017_linear_checkpoint_inbox
"""

from alembic import op


revision = "0018_bounded_fanout_write"
down_revision = "0017_linear_checkpoint_inbox"
branch_labels = None
depends_on = None

_KINDS = (
    "('managed_local_write','external_wait',"
    "'linear_checkpoint_write','bounded_fanout_write')"
)
_PRIOR_KINDS = (
    "('managed_local_write','external_wait','linear_checkpoint_write')"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind",
            f"task_kind IN {_KINDS}",
        )
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind",
            f"task_kind IN {_KINDS}",
        )


def downgrade() -> None:
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind",
            f"task_kind IN {_PRIOR_KINDS}",
        )
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind",
            f"task_kind IN {_PRIOR_KINDS}",
        )
