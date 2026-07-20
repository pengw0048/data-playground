"""Remove the unreleased compound temporal publication and task state.

Revision ID: 0035_remove_temporal
Revises: 0034_external_overlay
"""

import sqlalchemy as sa
from alembic import op


revision = "0035_remove_temporal"
down_revision = "0034_external_overlay"
branch_labels = None
depends_on = None


_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report')"
)
_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
    "AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)
_TEMPORAL_KIND = (
    "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','temporal_resample_write','distribution_report')"
)
_TEMPORAL_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'temporal_resample_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'temporal-resample') OR "
    "(task_kind <> 'distribution_report' AND task_kind <> 'temporal_resample_write' "
    "AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    connection = op.get_bind()
    temporal_tasks = "SELECT id FROM durable_tasks WHERE task_kind = 'temporal_resample_write'"
    # Remove dependants explicitly so the cleanup is correct even when foreign-key cascades are off.
    connection.execute(sa.text(
        f"DELETE FROM durable_task_inbox_items WHERE task_id IN ({temporal_tasks})"))
    connection.execute(sa.text(
        f"DELETE FROM durable_task_attempts WHERE task_id IN ({temporal_tasks})"))
    connection.execute(sa.text(
        f"DELETE FROM temporal_resample_task_envelopes WHERE task_id IN ({temporal_tasks})"))
    connection.execute(sa.text(
        "DELETE FROM durable_tasks WHERE task_kind = 'temporal_resample_write'"))

    op.drop_table("temporal_resample_task_envelopes")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _KIND)
        batch.create_check_constraint("ck_durable_task_subject", _SUBJECT)

    # These tables own only the removed compound experiment. Managed output revisions remain intact.
    op.drop_table("temporal_resample_publications")
    op.drop_table("compound_dataset_heads")
    op.drop_table("compound_dataset_revisions")


def downgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _TEMPORAL_KIND)
        batch.create_check_constraint("ck_durable_task_subject", _TEMPORAL_SUBJECT)

    op.create_table(
        "compound_dataset_revisions",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_doc", sa.Text(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("owner_id", "dataset_id", "revision_id"),
    )
    op.create_table(
        "compound_dataset_heads",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "dataset_id"),
    )
    op.create_table(
        "temporal_resample_publications",
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("write_intent_doc", sa.Text(), nullable=False),
        sa.Column("parent_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=64), nullable=False),
        sa.Column("child_revision_id", sa.String(length=64), nullable=False),
        sa.Column("spec_doc", sa.Text(), nullable=False),
        sa.Column("evidence_doc", sa.Text(), nullable=False),
        sa.Column("candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("output_member_id", sa.String(length=128), nullable=False),
        sa.Column("output_revision_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(candidate_digest) = 64", name="ck_temporal_resample_candidate_sha"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["output_revision_id"], ["managed_local_file_revisions.revision_id"]),
        sa.PrimaryKeyConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "owner_id", "parent_dataset_id", "child_revision_id",
            name="uq_temporal_resample_owner_child"),
        sa.UniqueConstraint("output_revision_id"),
    )
    op.create_table(
        "temporal_resample_task_envelopes",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("request_doc", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("write_idempotency_key", sa.String(length=2048), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="admitted"),
        sa.Column("result_doc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_sha256) = 64", name="ck_temporal_task_request_sha"),
        sa.CheckConstraint("length(candidate_sha256) = 64", name="ck_temporal_task_candidate_sha"),
        sa.UniqueConstraint("write_idempotency_key", name="uq_temporal_task_write_key"),
        sa.CheckConstraint(
            "phase IN ('admitted','recomputing','publishing','done','failed','cancelled')",
            name="ck_temporal_task_phase"),
        sa.ForeignKeyConstraint(["task_id"], ["durable_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )
