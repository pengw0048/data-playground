"""Persist semantic replay truth for synchronous SparseOutput merges.

Revision ID: 0030_merge_columns_pub
Revises: 0029_sparse_output_mat
"""

import sqlalchemy as sa
from alembic import op


revision = "0030_merge_columns_pub"
down_revision = "0029_sparse_output_mat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merge_columns_publications",
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("merge_doc", sa.Text(), nullable=False),
        sa.Column("merge_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(merge_sha256) = 64", name="ck_merge_columns_publication_sha"),
        sa.ForeignKeyConstraint(["revision_id"], ["managed_local_file_revisions.revision_id"]),
        sa.PrimaryKeyConstraint("idempotency_key"),
        sa.UniqueConstraint("revision_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM merge_columns_publications LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while merge publications are retained")
    op.drop_table("merge_columns_publications")
