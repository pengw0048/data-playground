"""Persist typed local write intents and durable receipts.

Revision ID: 0006_typed_local_writes
Revises: 0005_profile_output_ports
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_typed_local_writes"
down_revision = "0005_profile_output_ports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("managed_local_file_revisions") as batch:
        batch.add_column(sa.Column("write_idempotency_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("write_intent_doc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("write_receipt_doc", sa.Text(), nullable=True))
        batch.create_unique_constraint(
            "uq_managed_local_file_revision_write_key",
            ["write_idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("managed_local_file_revisions") as batch:
        batch.drop_constraint(
            "uq_managed_local_file_revision_write_key", type_="unique")
        batch.drop_column("write_receipt_doc")
        batch.drop_column("write_intent_doc")
        batch.drop_column("write_idempotency_key")
