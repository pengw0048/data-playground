"""Persist hidden DatasetView distribution-report tasks and result envelopes.

Revision ID: 0027_distribution_reports
Revises: 0026_dataset_views
"""

import sqlalchemy as sa
from alembic import op


revision = "0027_distribution_reports"
down_revision = "0026_dataset_views"
branch_labels = None
depends_on = None

_TASK_KINDS = (
    "('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','distribution_report')"
)
_PRIOR_TASK_KINDS = (
    "('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write')"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.add_column(sa.Column("dataset_view_id", sa.String(length=32), nullable=True))
        batch.alter_column("canvas_id", existing_type=sa.String(), nullable=True)
        batch.alter_column("target_node_id", existing_type=sa.String(), nullable=True)
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", f"task_kind IN {_TASK_KINDS}")
        batch.create_check_constraint(
            "ck_durable_task_subject",
            "(task_kind = 'distribution_report' AND canvas_id IS NULL "
            "AND target_node_id IS NULL AND dataset_view_id IS NOT NULL "
            "AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
            "AND input_manifest IS NULL AND write_intent IS NULL) OR "
            "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
            "AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_durable_task_dataset_view", "dataset_views", ["dataset_view_id"], ["id"])
        batch.create_unique_constraint(
            "uq_distribution_report_submission",
            ["owner_id", "dataset_view_id", "submission_id"],
        )
    op.create_index(
        "ix_durable_tasks_dataset_view_id", "durable_tasks", ["dataset_view_id"])

    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.add_column(sa.Column("dataset_view_id", sa.String(length=32), nullable=True))
        batch.alter_column("canvas_id", existing_type=sa.String(), nullable=True)
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind", f"task_kind IN {_TASK_KINDS}")
        batch.create_check_constraint(
            "ck_durable_task_inbox_subject",
            "(task_kind = 'distribution_report' AND canvas_id IS NULL "
            "AND dataset_view_id IS NOT NULL) OR "
            "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
            "AND dataset_view_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_durable_task_inbox_dataset_view",
            "dataset_views",
            ["dataset_view_id"],
            ["id"],
        )
    op.create_index(
        "ix_durable_task_inbox_items_dataset_view_id",
        "durable_task_inbox_items",
        ["dataset_view_id"],
    )

    op.create_table(
        "distribution_report_envelopes",
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), primary_key=True),
        sa.Column("report_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column(
            "dataset_view_id", sa.String(length=32),
            sa.ForeignKey("dataset_views.id"), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("intent_doc", sa.Text(), nullable=False),
        sa.Column("view_definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("view_snapshot_doc", sa.Text(), nullable=False),
        sa.Column("computation_version", sa.String(length=64), nullable=False),
        sa.Column("revision_retention_owner", sa.String(length=16), nullable=False),
        sa.Column("report_doc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(intent_sha256) = 64", name="ck_distribution_report_intent_sha256"),
        sa.CheckConstraint(
            "length(view_definition_sha256) = 64",
            name="ck_distribution_report_view_sha256"),
        sa.CheckConstraint(
            "revision_retention_owner = 'core'",
            name="ck_distribution_report_retention_owner"),
    )
    op.create_index(
        "ix_distribution_reports_dataset_view",
        "distribution_report_envelopes",
        ["dataset_view_id", "created_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT 1 FROM distribution_report_envelopes LIMIT 1"
    )).first() is not None:
        raise RuntimeError("cannot downgrade while distribution reports are retained")
    op.drop_index(
        "ix_distribution_reports_dataset_view",
        table_name="distribution_report_envelopes",
    )
    op.drop_table("distribution_report_envelopes")

    op.drop_index(
        "ix_durable_task_inbox_items_dataset_view_id",
        table_name="durable_task_inbox_items",
    )
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("fk_durable_task_inbox_dataset_view", type_="foreignkey")
        batch.drop_constraint("ck_durable_task_inbox_subject", type_="check")
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_inbox_kind", f"task_kind IN {_PRIOR_TASK_KINDS}")
        batch.alter_column("canvas_id", existing_type=sa.String(), nullable=False)
        batch.drop_column("dataset_view_id")

    op.drop_index("ix_durable_tasks_dataset_view_id", table_name="durable_tasks")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("uq_distribution_report_submission", type_="unique")
        batch.drop_constraint("fk_durable_task_dataset_view", type_="foreignkey")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind", f"task_kind IN {_PRIOR_TASK_KINDS}")
        batch.alter_column("target_node_id", existing_type=sa.String(), nullable=False)
        batch.alter_column("canvas_id", existing_type=sa.String(), nullable=False)
        batch.drop_column("dataset_view_id")
