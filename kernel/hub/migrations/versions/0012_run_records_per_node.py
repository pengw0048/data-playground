"""run telemetry: run_records.per_node (durable per-node breakdown)

Keeps the per-node timing/row breakdown (a JSON list of {node_id,label,status,rows,ms}) with each
finished run, so the run-history telemetry view can chart where time/rows went AFTER a restart — the
live RunState.doc has it, but that's keyed by the runner's run_id and gets reaped; this column makes
the breakdown part of durable history.

Revision ID: 0012_run_records_per_node
Revises: 0011_kernels
Create Date: 2026-07-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_run_records_per_node"
down_revision = "0011_kernels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_records", sa.Column("per_node", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_records", "per_node")
