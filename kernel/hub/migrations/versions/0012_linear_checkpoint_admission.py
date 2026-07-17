"""Add hidden linear-checkpoint admission and candidate binding.

Revision ID: 0012_linear_checkpoint_admission
Revises: 0011_external_wait_publication
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_linear_checkpoint_admission"
down_revision = "0011_external_wait_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind",
            "task_kind IN ('managed_local_write','external_wait','linear_checkpoint_write')",
        )
    op.create_table(
        "durable_checkpoints",
        sa.Column("task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), primary_key=True),
        sa.Column("checkpoint_id", sa.String(128), nullable=False),
        sa.Column("checkpoint_node_id", sa.String(256), nullable=False),
        sa.Column("output_port_id", sa.String(128), nullable=False),
        sa.Column("task_intent_sha256", sa.String(64), nullable=False),
        sa.Column("graph_prefix_sha256", sa.String(64), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("candidate_uri", sa.Text(),
                  sa.ForeignKey("local_result_artifacts.uri"), nullable=True),
        sa.Column("candidate_generation", sa.String(64), nullable=True),
        sa.Column("candidate_attempt_id", sa.String(),
                  sa.ForeignKey("durable_task_attempts.id"), nullable=True),
        sa.Column("candidate_dev", sa.BigInteger(), nullable=True),
        sa.Column("candidate_ino", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("checkpoint_id", name="uq_durable_checkpoint_id"),
        sa.UniqueConstraint("candidate_uri", name="uq_durable_checkpoint_candidate_uri"),
        sa.UniqueConstraint(
            "candidate_generation", name="uq_durable_checkpoint_candidate_generation"),
        sa.CheckConstraint(
            "phase IN ('pending','reserved')", name="ck_durable_checkpoint_phase"),
        sa.CheckConstraint(
            "(candidate_uri IS NULL AND candidate_generation IS NULL "
            "AND candidate_attempt_id IS NULL) OR (candidate_uri IS NOT NULL "
            "AND candidate_generation IS NOT NULL AND candidate_attempt_id IS NOT NULL)",
            name="ck_durable_checkpoint_candidate_binding"),
        sa.CheckConstraint(
            "(candidate_dev IS NULL AND candidate_ino IS NULL) OR "
            "(candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL "
            "AND candidate_dev >= 0 AND candidate_ino >= 0)",
            name="ck_durable_checkpoint_candidate_inode"),
        sa.CheckConstraint(
            "(phase = 'pending' AND candidate_uri IS NULL) OR "
            "(phase = 'reserved' AND candidate_uri IS NOT NULL)",
            name="ck_durable_checkpoint_phase_binding"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    hidden = bind.execute(sa.text(
        "SELECT COUNT(*) FROM durable_tasks WHERE task_kind = 'linear_checkpoint_write'"
    )).scalar_one()
    checkpoints = bind.execute(sa.text(
        "SELECT COUNT(*) FROM durable_checkpoints"
    )).scalar_one()
    if hidden or checkpoints:
        raise RuntimeError(
            "cannot downgrade while hidden linear-checkpoint admissions are retained")
    op.drop_table("durable_checkpoints")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.create_check_constraint(
            "ck_durable_task_kind", "task_kind IN ('managed_local_write','external_wait')")
