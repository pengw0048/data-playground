import sqlalchemy as sa
from alembic import op


revision = "0033_temporal_task"
down_revision = "0032_temporal_pub"
branch_labels = None
depends_on = None


_KIND = ("task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
         "'bounded_fanout_write','merge_columns_write','temporal_resample_write','distribution_report')")
_SUBJECT = (
    "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
    "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
    "AND input_manifest IS NULL AND write_intent IS NULL) OR "
    "(task_kind = 'temporal_resample_write' AND canvas_id IS NULL AND dataset_view_id IS NULL "
    "AND target_node_id = 'temporal-resample') OR "
    "(task_kind <> 'distribution_report' AND task_kind <> 'temporal_resample_write' "
    "AND canvas_id IS NOT NULL AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", _KIND)
        batch.create_check_constraint("ck_durable_task_subject", _SUBJECT)
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
        sa.CheckConstraint("phase IN ('admitted','recomputing','publishing','done','failed','cancelled')",
                           name="ck_temporal_task_phase"),
        sa.ForeignKeyConstraint(["task_id"], ["durable_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM temporal_resample_task_envelopes LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while temporal resample tasks are retained")
    op.drop_table("temporal_resample_task_envelopes")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_constraint("ck_durable_task_subject", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind",
            "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write',"
            "'bounded_fanout_write','merge_columns_write','distribution_report')")
        batch.create_check_constraint(
            "ck_durable_task_subject",
            "(task_kind = 'distribution_report' AND canvas_id IS NULL AND target_node_id IS NULL "
            "AND dataset_view_id IS NOT NULL AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
            "AND input_manifest IS NULL AND write_intent IS NULL) OR "
            "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
            "AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)")
