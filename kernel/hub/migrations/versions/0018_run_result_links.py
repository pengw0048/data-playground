"""durable run-history links to the logical run and its materialized result

Revision ID: 0018_run_result_links
Revises: 0017_catalog_org
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_run_result_links"
down_revision = "0017_catalog_org"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable preserves legacy history rows, which predate durable run/result links.
    op.add_column("run_records", sa.Column("run_id", sa.String(), nullable=True))
    op.add_column("run_records", sa.Column("output_uri", sa.Text(), nullable=True))
    op.create_index("ix_run_records_run_id", "run_records", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_records_run_id", table_name="run_records")
    op.drop_column("run_records", "output_uri")
    op.drop_column("run_records", "run_id")
