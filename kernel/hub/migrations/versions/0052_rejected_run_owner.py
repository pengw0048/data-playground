"""Retain the submitter of run attempts rejected before allocation.

Revision ID: 0052_rejected_run_owner
Revises: 0051_canvas_result_latest
"""

import sqlalchemy as sa
from alembic import op


revision = "0052_rejected_run_owner"
down_revision = "0051_canvas_result_latest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_records", sa.Column("created_by", sa.String(), nullable=True))
    op.create_index(
        "ix_run_records_created_by", "run_records", ["created_by"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_records_created_by", table_name="run_records")
    op.drop_column("run_records", "created_by")
