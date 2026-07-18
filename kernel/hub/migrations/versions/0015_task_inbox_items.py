"""Persist one owner-scoped Inbox outcome per durable TaskAttempt.

Revision ID: 0015_task_inbox_items
Revises: 0014_checkpoint_mat_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_task_inbox_items"
down_revision = "0014_checkpoint_mat_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "durable_task_inbox_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), nullable=False),
        sa.Column(
            "task_attempt_id", sa.String(),
            sa.ForeignKey("durable_task_attempts.id"), nullable=False),
        sa.Column("canvas_id", sa.String(), sa.ForeignKey("canvases.id"), nullable=False),
        sa.Column("task_kind", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("diagnostic_code", sa.String(64), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "task_id", "task_attempt_id", name="uq_durable_task_inbox_attempt"),
        sa.CheckConstraint(
            "task_kind IN ('managed_local_write','external_wait')",
            name="ck_durable_task_inbox_kind"),
        sa.CheckConstraint(
            "outcome IN ('completed','failed','cancelled')",
            name="ck_durable_task_inbox_outcome"),
    )
    op.create_index(
        "ix_durable_task_inbox_owner_created",
        "durable_task_inbox_items",
        ["owner_id", "created_at", "id"],
    )
    op.create_index(
        "ix_durable_task_inbox_owner_unread",
        "durable_task_inbox_items",
        ["owner_id", "read_at"],
    )
    op.create_index(
        "ix_durable_task_inbox_task_id",
        "durable_task_inbox_items",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_durable_task_inbox_task_id", table_name="durable_task_inbox_items")
    op.drop_index("ix_durable_task_inbox_owner_unread", table_name="durable_task_inbox_items")
    op.drop_index("ix_durable_task_inbox_owner_created", table_name="durable_task_inbox_items")
    op.drop_table("durable_task_inbox_items")
