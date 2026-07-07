"""result_cache: persistent content-addressed result index (cross-run / cross-instance reuse)

Revision ID: 0008_result_cache
Revises: 0007_catalog_entries
Create Date: 2026-07-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_result_cache"
down_revision = "0007_catalog_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "result_cache",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("result_cache")
