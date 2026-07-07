"""catalog_entries + catalog_edges: DB-backed catalog (cross-instance dataset/output visibility)

Revision ID: 0007_catalog_entries
Revises: 0006_run_states
Create Date: 2026-07-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_catalog_entries"
down_revision = "0006_run_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_entries",
        sa.Column("uri", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_catalog_entries_name", "catalog_entries", ["name"])
    op.create_table(
        "catalog_edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent", sa.String(), nullable=False),
        sa.Column("child", sa.String(), nullable=False),
        sa.Column("pipeline", sa.String(), nullable=True),
        sa.UniqueConstraint("parent", "child", name="uq_catalog_edge"),
    )
    op.create_index("ix_catalog_edges_parent", "catalog_edges", ["parent"])
    op.create_index("ix_catalog_edges_child", "catalog_edges", ["child"])


def downgrade() -> None:
    op.drop_index("ix_catalog_edges_child", table_name="catalog_edges")
    op.drop_index("ix_catalog_edges_parent", table_name="catalog_edges")
    op.drop_table("catalog_edges")
    op.drop_index("ix_catalog_entries_name", table_name="catalog_entries")
    op.drop_table("catalog_entries")
