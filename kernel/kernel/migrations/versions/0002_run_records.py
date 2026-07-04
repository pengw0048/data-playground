"""run history: run_records (kept with each canvas)

Revision ID: 0002_run_records
Revises: 0001_baseline
Create Date: 2026-07-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_run_records"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("canvas_id", sa.String(), sa.ForeignKey("canvases.id"), index=True),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("status", sa.String()),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output_table", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("run_records")
