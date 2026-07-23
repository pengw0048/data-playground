"""Allow the durable merge lifecycle to consume one retained exact managed sidecar.

Revision ID: 0040_managed_sidecar
Revises: 0039_folder_replays
"""

import sqlalchemy as sa
from alembic import op


revision = "0040_managed_sidecar"
down_revision = "0039_folder_replays"
branch_labels = None
depends_on = None


_TASK_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'restore_revision_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'restore-revision') OR "
    "(task_kind = 'keyed_upsert_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'keyed-upsert') OR "
    "(task_kind = 'merge_columns_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'managed-sidecar-merge') OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_PRIOR_TASK_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'restore_revision_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'restore-revision') OR "
    "(task_kind = 'keyed_upsert_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'keyed-upsert') OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_INBOX_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND dataset_view_id IS NOT NULL) OR "
    "(task_kind IN ('restore_revision_write','keyed_upsert_write') AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL) OR (task_kind = 'merge_columns_write' AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL) OR (task_kind NOT IN "
    "('distribution_report','restore_revision_write','keyed_upsert_write') AND canvas_id IS NOT NULL "
    "AND dataset_view_id IS NULL)"
)
_PRIOR_INBOX_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND dataset_view_id IS NOT NULL) OR "
    "(task_kind IN ('restore_revision_write','keyed_upsert_write') AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL) OR (task_kind NOT IN "
    "('distribution_report','restore_revision_write','keyed_upsert_write') AND canvas_id IS NOT NULL "
    "AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_subject", _TASK_SUBJECT)
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_subject", _INBOX_SUBJECT)
    with op.batch_alter_table("merge_columns_task_envelopes") as batch:
        batch.alter_column("sparse_output_id", existing_type=sa.String(length=128), nullable=True)
        batch.add_column(sa.Column("producer_kind", sa.String(length=32), nullable=False,
                                  server_default="sparse-output"))
        batch.add_column(sa.Column("sidecar_dataset_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("sidecar_revision_id", sa.String(length=128), nullable=True))
        batch.create_check_constraint(
            "ck_merge_task_producer",
            "(producer_kind = 'sparse-output' AND sparse_output_id IS NOT NULL "
            "AND sidecar_dataset_id IS NULL AND sidecar_revision_id IS NULL) OR "
            "(producer_kind = 'managed-sidecar' AND sparse_output_id IS NULL "
            "AND sidecar_dataset_id IS NOT NULL AND sidecar_revision_id IS NOT NULL)")


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM merge_columns_task_envelopes "
            "WHERE producer_kind = 'managed-sidecar' LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while managed sidecar merge tasks are retained")
    with op.batch_alter_table("merge_columns_task_envelopes") as batch:
        batch.drop_constraint("ck_merge_task_producer", type_="check")
        batch.drop_column("sidecar_revision_id")
        batch.drop_column("sidecar_dataset_id")
        batch.drop_column("producer_kind")
        batch.alter_column("sparse_output_id", existing_type=sa.String(length=128), nullable=False)
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_subject", _PRIOR_INBOX_SUBJECT)
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_subject", _PRIOR_TASK_SUBJECT)
