"""Durable dataset-scoped task that restores an old revision as a new head.

Revision ID: 0036_restore_revision
Revises: 0035_remove_temporal
"""

import sqlalchemy as sa
from alembic import op


revision = "0036_restore_revision"
down_revision = "0035_remove_temporal"
branch_labels = None
depends_on = None


_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','restore_revision_write','distribution_report')"
)
_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'restore_revision_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'restore-revision') OR "
    "(task_kind <> 'distribution_report' AND task_kind <> 'restore_revision_write' "
    "AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_PRIOR_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report')"
)
_PRIOR_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
    "AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _KIND)
        batch.create_check_constraint("ck_durable_task_subject", _SUBJECT)
    op.create_table(
        "restore_revision_task_envelopes",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("source_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("source_revision_id", sa.String(length=256), nullable=False),
        sa.Column("write_idempotency_key", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("write_idempotency_key", name="uq_restore_revision_write_key"),
        sa.ForeignKeyConstraint(["task_id"], ["durable_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(
            sa.text("SELECT 1 FROM restore_revision_task_envelopes LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while restore revision tasks are retained")
    op.drop_table("restore_revision_task_envelopes")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _PRIOR_KIND)
        batch.create_check_constraint("ck_durable_task_subject", _PRIOR_SUBJECT)
