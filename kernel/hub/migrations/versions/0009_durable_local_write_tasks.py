"""Add bounded durable managed-local write tasks.

Revision ID: 0009_durable_local_write_tasks
Revises: 0008_managed_local_lance_writes
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_durable_local_write_tasks"
down_revision = "0008_managed_local_lance_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "durable_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("canvas_id", sa.String(), sa.ForeignKey("canvases.id"), nullable=False),
        sa.Column("submission_id", sa.String(), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("backend_kind", sa.String(), nullable=False, server_default="local"),
        sa.Column("graph_doc", sa.Text(), nullable=False),
        sa.Column("input_manifest", sa.Text(), nullable=False),
        sa.Column("write_intent", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("status_doc", sa.Text(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("output_receipt", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "canvas_id", "submission_id", name="uq_durable_task_submission"),
        sa.CheckConstraint("backend_kind = 'local'", name="ck_durable_task_backend"),
        sa.CheckConstraint("status IN ('queued','running','done','failed','cancelled')", name="ck_durable_task_status"),
        sa.CheckConstraint("retry_count >= 0 AND max_attempts >= 1 AND retry_count < max_attempts", name="ck_durable_task_retry_bounds"),
    )
    op.create_index("ix_durable_tasks_owner_id", "durable_tasks", ["owner_id"])
    op.create_index("ix_durable_tasks_canvas_id", "durable_tasks", ["canvas_id"])
    op.create_index("ix_durable_tasks_status", "durable_tasks", ["status"])
    op.create_table(
        "durable_task_attempts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("retry_request_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("owner_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("output_receipt", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_durable_task_attempt_number"),
        sa.UniqueConstraint("task_id", "retry_request_id", name="uq_durable_task_retry_request"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_durable_task_attempt_number"),
        sa.CheckConstraint("status IN ('queued','running','done','failed','cancelled','fenced')", name="ck_durable_task_attempt_status"),
    )
    op.create_index("ix_durable_task_attempts_task_id", "durable_task_attempts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_durable_task_attempts_task_id", table_name="durable_task_attempts")
    op.drop_table("durable_task_attempts")
    op.drop_index("ix_durable_tasks_status", table_name="durable_tasks")
    op.drop_index("ix_durable_tasks_canvas_id", table_name="durable_tasks")
    op.drop_index("ix_durable_tasks_owner_id", table_name="durable_tasks")
    op.drop_table("durable_tasks")
