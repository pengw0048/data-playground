"""Retain exact core-managed local file revisions.

Revision ID: 0002_managed_local_file_revisions
Revises: 0001_schema_baseline
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_managed_local_file_revisions"
down_revision = "0001_schema_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_local_file_revisions",
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("logical_id", sa.String(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("publish_seq", sa.BigInteger(), nullable=False),
        sa.Column("table_doc", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_uri"], ["local_result_artifacts.uri"]),
        sa.ForeignKeyConstraint(["logical_id"], ["catalog_logical_datasets.logical_id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("artifact_uri"),
        sa.UniqueConstraint("logical_id", "publish_seq", name="uq_managed_local_file_revision_sequence"),
    )
    op.create_index(
        "ix_managed_local_file_revisions_history",
        "managed_local_file_revisions",
        ["logical_id", "publish_seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_managed_local_file_revisions_history",
        table_name="managed_local_file_revisions",
    )
    op.drop_table("managed_local_file_revisions")
