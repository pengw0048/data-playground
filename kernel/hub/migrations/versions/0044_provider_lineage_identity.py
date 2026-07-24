"""Allow canonical provider identities in source field-lineage evidence.

Revision ID: 0044_provider_lineage_identity
Revises: 0043_provider_source_binding
"""

import sqlalchemy as sa
from alembic import op


revision = "0044_provider_lineage_identity"
down_revision = "0043_provider_source_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provider identities are opaque base64url tokens and may legally exceed the former 128-byte
    # ordinary-registration bound. Only the source side is widened: destination identities retain
    # their existing catalog contract.
    with op.batch_alter_table("catalog_field_lineage_projections") as batch:
        batch.alter_column(
            "source_dataset_id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=512),
            existing_nullable=False,
        )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM catalog_field_lineage_projections "
            "WHERE length(source_dataset_id) > 128 LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while long provider field-lineage identities are retained")
    with op.batch_alter_table("catalog_field_lineage_projections") as batch:
        batch.alter_column(
            "source_dataset_id",
            existing_type=sa.String(length=512),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
