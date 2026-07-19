"""Persist the exact durable SparseOutput merge Task envelope.

Revision ID: 0031_durable_merge
Revises: 0030_merge_columns_pub
"""

import sqlalchemy as sa
from alembic import op


revision = "0031_durable_merge"
down_revision = "0030_merge_columns_pub"
branch_labels = None
depends_on = None


_PRIOR_KINDS = (
    "('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','distribution_report')"
)
_KINDS = (
    "('managed_local_write','external_wait','linear_checkpoint_write',"
    "'bounded_fanout_write','merge_columns_write','distribution_report')"
)


def _replace_kind_constraints(kinds: str) -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint("ck_durable_task_kind", f"task_kind IN {kinds}")
    with op.batch_alter_table("durable_task_inbox_items") as batch:
        batch.drop_constraint("ck_durable_task_inbox_kind", type_="check")
        batch.create_check_constraint("ck_durable_task_inbox_kind", f"task_kind IN {kinds}")


def upgrade() -> None:
    _replace_kind_constraints(_KINDS)
    op.create_table(
        "merge_columns_task_envelopes",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_doc", sa.Text(), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("merge_doc", sa.Text(), nullable=False),
        sa.Column("merge_sha256", sa.String(length=64), nullable=False),
        sa.Column("sparse_output_id", sa.String(length=128), nullable=False),
        sa.Column("base_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("base_revision_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="validating"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(intent_sha256) = 64", name="ck_merge_task_intent_sha"),
        sa.CheckConstraint("length(merge_sha256) = 64", name="ck_merge_task_merge_sha"),
        sa.CheckConstraint(
            "phase IN ('validating','merging','candidate_committed','publishing',"
            "'done','failed','cancelled')", name="ck_merge_task_phase"),
        sa.ForeignKeyConstraint(["sparse_output_id"], ["sparse_outputs.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["durable_tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM merge_columns_task_envelopes LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while durable merge tasks are retained")
    op.drop_table("merge_columns_task_envelopes")
    _replace_kind_constraints(_PRIOR_KINDS)
