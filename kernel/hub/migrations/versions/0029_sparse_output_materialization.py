"""Add the fenced managed-local sidecar lifecycle for SparseOutput.

Revision ID: 0029_sparse_output_mat
Revises: 0028_sparse_output_admission
"""

import sqlalchemy as sa
from alembic import op


revision = "0029_sparse_output_mat"
down_revision = "0028_sparse_output_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sparse_output_materializations",
        sa.Column("sparse_id", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("owner_token", sa.String(length=32), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_uri", sa.Text(), nullable=False),
        sa.Column("generation", sa.String(length=64), nullable=False),
        sa.Column("candidate_dev", sa.BigInteger(), nullable=True),
        sa.Column("candidate_ino", sa.BigInteger(), nullable=True),
        sa.Column("committed_rows", sa.BigInteger(), nullable=True),
        sa.Column("committed_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("schema_sha256", sa.String(length=64), nullable=True),
        sa.Column("coverage_doc", sa.Text(), nullable=True),
        sa.Column("coverage_sha256", sa.String(length=64), nullable=True),
        sa.CheckConstraint("phase IN ('reserved', 'committed')", name="ck_sparse_mat_phase"),
        sa.CheckConstraint("length(owner_token) = 32", name="ck_sparse_mat_owner_token"),
        sa.CheckConstraint("length(generation) = 64", name="ck_sparse_mat_generation"),
        sa.CheckConstraint("candidate_dev IS NULL OR candidate_dev >= 0", name="ck_sparse_mat_dev"),
        sa.CheckConstraint("candidate_ino IS NULL OR candidate_ino >= 0", name="ck_sparse_mat_ino"),
        sa.CheckConstraint("committed_rows IS NULL OR committed_rows >= 0", name="ck_sparse_mat_rows"),
        sa.CheckConstraint("committed_bytes IS NULL OR committed_bytes > 0", name="ck_sparse_mat_bytes"),
        sa.CheckConstraint("content_sha256 IS NULL OR length(content_sha256) = 64", name="ck_sparse_mat_content"),
        sa.CheckConstraint("schema_sha256 IS NULL OR length(schema_sha256) = 64", name="ck_sparse_mat_schema"),
        sa.CheckConstraint("coverage_sha256 IS NULL OR length(coverage_sha256) = 64", name="ck_sparse_mat_coverage"),
        sa.CheckConstraint("(candidate_dev IS NULL AND candidate_ino IS NULL) OR "
                           "(candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL)",
                           name="ck_sparse_mat_inode_pair"),
        sa.CheckConstraint("(phase = 'reserved' AND committed_rows IS NULL AND committed_bytes IS NULL "
                           "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND coverage_doc IS NULL "
                           "AND coverage_sha256 IS NULL) OR (phase = 'committed' AND candidate_dev IS NOT NULL "
                           "AND candidate_ino IS NOT NULL AND committed_rows IS NOT NULL AND committed_bytes IS NOT NULL "
                           "AND content_sha256 IS NOT NULL AND schema_sha256 IS NOT NULL AND coverage_doc IS NOT NULL "
                           "AND coverage_sha256 IS NOT NULL)", name="ck_sparse_mat_evidence"),
        sa.ForeignKeyConstraint(["sparse_id"], ["sparse_outputs.id"]),
        sa.ForeignKeyConstraint(["candidate_uri"], ["local_result_artifacts.uri"]),
        sa.PrimaryKeyConstraint("sparse_id"),
        sa.UniqueConstraint("candidate_uri"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM sparse_output_materializations LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while SparseOutput materializations are retained")
    op.drop_table("sparse_output_materializations")
