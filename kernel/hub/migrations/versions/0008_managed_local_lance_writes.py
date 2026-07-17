"""Persist managed local Lance append receipts.

Revision ID: 0008_managed_local_lance_writes
Revises: 0007_workspace_provider_bindings
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_managed_local_lance_writes"
down_revision = "0007_workspace_provider_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_local_lance_write_receipts",
        sa.Column("idempotency_key", sa.String(), primary_key=True),
        sa.Column("dataset_id", sa.String(), nullable=False),
        sa.Column("logical_uri", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.String(256), nullable=False),
        sa.Column("write_intent_doc", sa.Text(), nullable=False),
        sa.Column("write_receipt_doc", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dataset_id", "revision_id", name="uq_managed_local_lance_write_revision"),
    )


def downgrade() -> None:
    op.drop_table("managed_local_lance_write_receipts")
