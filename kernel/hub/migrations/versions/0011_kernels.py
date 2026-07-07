"""kernels — the lease registry for per-canvas execution kernels, + run_states.kernel_id fencing.

One row per canvas that has (or is starting) a live kernel. `canvas_id` is the PK, so the atomic
INSERT is the single-spawner claim; `kernel_id` fences a replaced/zombie kernel out of heartbeating,
dropping the row, or writing run status. `run_states.kernel_id` stamps which kernel owns a run, so a
boot-time reaper fails a run only when its owning kernel is genuinely gone (not on every restart).

Revision ID: 0011_kernels
Revises: 0010_user_is_admin
Create Date: 2026-07-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_kernels"
down_revision = "0010_user_is_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kernels",
        sa.Column("canvas_id", sa.String(), primary_key=True),
        sa.Column("kernel_id", sa.String(), nullable=False),   # fencing token: the current owner's id
        sa.Column("endpoint", sa.String(), nullable=True),     # host:port of the command channel (set when ready)
        sa.Column("token", sa.String(), nullable=False),       # bearer token for the loopback command channel
        sa.Column("state", sa.String(), nullable=False),       # starting | ready
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("run_states", sa.Column("kernel_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_states", "kernel_id")
    op.drop_table("kernels")
