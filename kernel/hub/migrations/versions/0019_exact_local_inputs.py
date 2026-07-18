"""Retain content-identified snapshots for ordinary local run inputs.

Revision ID: 0019_exact_local_inputs
Revises: 0018_bounded_fanout_write
"""

import sqlalchemy as sa
from alembic import op


revision = "0019_exact_local_inputs"
down_revision = "0018_bounded_fanout_write"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_file_input_revisions",
        sa.Column("dataset_id", sa.String(), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_uri"], ["local_result_artifacts.uri"]),
        sa.PrimaryKeyConstraint("dataset_id", "revision_id"),
        sa.UniqueConstraint("artifact_uri"),
    )


def downgrade() -> None:
    op.drop_table("local_file_input_revisions")
