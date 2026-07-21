"""Surface canvas-less dataset-scoped Tasks (restore-revision, keyed-upsert) in the Inbox.

Revision ID: 0038_inbox_dataset_scoped
Revises: 0037_keyed_upsert
"""

import sqlalchemy as sa
from alembic import op


revision = "0038_inbox_dataset_scoped"
down_revision = "0037_keyed_upsert"
branch_labels = None
depends_on = None


_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report',"
    "'restore_revision_write','keyed_upsert_write')"
)
_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND dataset_view_id IS NOT NULL) OR "
    "(task_kind IN ('restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NULL AND dataset_view_id IS NULL) OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_PRIOR_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report')"
)
_PRIOR_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND dataset_view_id IS NOT NULL) OR "
    "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_kind", _KIND)
        batch.create_check_constraint("ck_durable_task_inbox_subject", _SUBJECT)


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM durable_task_inbox_items "
            "WHERE task_kind IN ('restore_revision_write','keyed_upsert_write') LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while dataset-scoped Inbox items are retained")
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_kind", _PRIOR_KIND)
        batch.create_check_constraint("ck_durable_task_inbox_subject", _PRIOR_SUBJECT)
