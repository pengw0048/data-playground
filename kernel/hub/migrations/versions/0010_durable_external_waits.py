"""Add the bounded durable external-wait task state.

Revision ID: 0010_durable_external_waits
Revises: 0009_durable_local_write_tasks
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_durable_external_waits"
down_revision = "0009_durable_local_write_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("durable_tasks") as batch:
        batch.add_column(sa.Column(
            "task_kind", sa.String(), nullable=False, server_default="managed_local_write"))
        batch.create_check_constraint(
            "ck_durable_task_kind", "task_kind IN ('managed_local_write','external_wait')")
    op.create_table(
        "durable_external_waits",
        sa.Column("task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), primary_key=True),
        sa.Column("provider_kind", sa.String(64), nullable=False),
        sa.Column("submit_request", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("handle_doc", sa.Text(), nullable=True),
        sa.Column("checkpoint_doc", sa.Text(), nullable=True),
        sa.Column("phase", sa.String(32), nullable=False, server_default="unsubmitted"),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("diagnostic_code", sa.String(64), nullable=True),
        sa.Column("owner_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('unsubmitted','submitting','accepted','running','provider_succeeded',"
            "'provider_failed','provider_cancelled','cancelled_before_submit')",
            name="ck_external_wait_phase"),
        sa.CheckConstraint("poll_count >= 0", name="ck_external_wait_poll_count"),
    )


def downgrade() -> None:
    op.drop_table("durable_external_waits")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.drop_constraint("ck_durable_task_kind", type_="check")
        batch.drop_column("task_kind")
