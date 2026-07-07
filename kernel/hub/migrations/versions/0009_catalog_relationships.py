"""catalog_relationships + catalog_declared_keys: per-row storage for owner-declared relationships
and primary keys (replaces the single-JSON-blob Setting, so cross-instance writes can't clobber).

Revision ID: 0009_catalog_relationships
Revises: 0008_result_cache
Create Date: 2026-07-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_catalog_relationships"
down_revision = "0008_result_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_relationships",
        sa.Column("rel_key", sa.String(), primary_key=True),
        sa.Column("doc", sa.Text(), nullable=False),
    )
    op.create_table(
        "catalog_declared_keys",
        sa.Column("uri", sa.String(), primary_key=True),
        sa.Column("columns", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("catalog_declared_keys")
    op.drop_table("catalog_relationships")
