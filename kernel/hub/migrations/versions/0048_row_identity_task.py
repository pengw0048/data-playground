"""Add durable exact-revision row identity certification tasks.

Revision ID: 0048_row_identity_task
Revises: 0047_row_identity_cert
"""

import sqlalchemy as sa
from alembic import op


revision = "0048_row_identity_task"
down_revision = "0047_row_identity_cert"
branch_labels = None
depends_on = None


_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','restore_revision_write',"
    "'keyed_upsert_write','distribution_report','row_identity_certification')"
)
_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'restore_revision_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'restore-revision') OR "
    "(task_kind = 'keyed_upsert_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'keyed-upsert') OR "
    "(task_kind = 'row_identity_certification' AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL AND target_node_id = 'row-identity-certification' "
    "AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'merge_columns_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'managed-sidecar-merge') OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write',"
    "'row_identity_certification') AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL "
    "AND dataset_view_id IS NULL)"
)
_PRIOR_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','restore_revision_write',"
    "'keyed_upsert_write','distribution_report')"
)
_PRIOR_SUBJECT = (
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
_INBOX_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report',"
    "'restore_revision_write','keyed_upsert_write','row_identity_certification')"
)
_INBOX_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL "
    "AND dataset_view_id IS NOT NULL) OR "
    "(task_kind IN ('restore_revision_write','keyed_upsert_write',"
    "'row_identity_certification') AND canvas_id IS NULL AND dataset_view_id IS NULL) OR "
    "(task_kind = 'merge_columns_write' AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL) OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write',"
    "'row_identity_certification') AND canvas_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_PRIOR_INBOX_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report',"
    "'restore_revision_write','keyed_upsert_write')"
)
_PRIOR_INBOX_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL "
    "AND dataset_view_id IS NOT NULL) OR "
    "(task_kind IN ('restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NULL AND dataset_view_id IS NULL) OR "
    "(task_kind = 'merge_columns_write' AND canvas_id IS NULL "
    "AND dataset_view_id IS NULL) OR "
    "(task_kind NOT IN ('distribution_report','restore_revision_write','keyed_upsert_write') "
    "AND canvas_id IS NOT NULL AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _KIND)
        batch.create_check_constraint("ck_durable_task_subject", _SUBJECT)
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_kind", _INBOX_KIND)
        batch.create_check_constraint("ck_durable_task_inbox_subject", _INBOX_SUBJECT)
    op.create_table(
        "row_identity_certification_task_envelopes",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=256), nullable=False),
        sa.Column("dataset_name", sa.String(length=512), nullable=True),
        sa.Column("keys_doc", sa.Text(), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("supported", sa.Boolean(), nullable=False),
        sa.Column("confirmation_sha256", sa.String(length=64), nullable=False),
        sa.Column("estimated_rows", sa.BigInteger(), nullable=True),
        sa.Column("estimated_bytes", sa.BigInteger(), nullable=True),
        sa.Column("receipt_doc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(schema_sha256) = 64", name="ck_row_identity_task_schema_sha"),
        sa.CheckConstraint(
            "length(spec_sha256) = 64", name="ck_row_identity_task_spec_sha"),
        sa.CheckConstraint(
            "length(confirmation_sha256) = 64", name="ck_row_identity_task_confirm_sha"),
        sa.CheckConstraint(
            "estimated_rows IS NULL OR estimated_rows >= 0",
            name="ck_row_identity_task_estimated_rows"),
        sa.CheckConstraint(
            "estimated_bytes IS NULL OR estimated_bytes >= 0",
            name="ck_row_identity_task_estimated_bytes"),
        sa.ForeignKeyConstraint(["task_id"], ["durable_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM row_identity_certification_task_envelopes LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while row identity certification tasks are retained")
    op.drop_table("row_identity_certification_task_envelopes")
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_kind", _PRIOR_INBOX_KIND)
        batch.create_check_constraint(
            "ck_durable_task_inbox_subject", _PRIOR_INBOX_SUBJECT)
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _PRIOR_KIND)
        batch.create_check_constraint("ck_durable_task_subject", _PRIOR_SUBJECT)
