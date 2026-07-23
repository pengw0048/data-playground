"""Retain exact source-owned field-lineage projections.

Revision ID: 0042_field_lineage
Revises: 0041_provider_canonical
"""

import sqlalchemy as sa
from alembic import op


revision = "0042_field_lineage"
down_revision = "0041_provider_canonical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_field_lineage_projections",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("projection_key", sa.String(length=96), nullable=False),
        sa.Column(
            "fact_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("fact_key", sa.String(length=512), nullable=False),
        sa.Column("publication_key", sa.String(length=96), nullable=False),
        sa.Column("source_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("source_version", sa.String(length=512), nullable=False),
        sa.Column("source_field", sa.String(length=512), nullable=False),
        sa.Column("source_field_id", sa.String(length=512), nullable=True),
        sa.Column("destination_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("destination_revision_id", sa.String(length=512), nullable=False),
        sa.Column("destination_field", sa.String(length=512), nullable=False),
        sa.Column("destination_dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("destination_revision_hash", sa.String(length=64), nullable=False),
        sa.Column("destination_field_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fact_id"], ["catalog_lineage_facts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "projection_key", name="uq_catalog_field_lineage_projection_key"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_catalog_field_lineage_exact_output",
        "catalog_field_lineage_projections",
        [
            "destination_dataset_hash",
            "destination_revision_hash",
            "destination_field_hash",
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_catalog_field_lineage_fact_id",
        "catalog_field_lineage_projections",
        ["fact_id"],
        unique=False,
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM catalog_field_lineage_projections LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while exact field-lineage projections are retained")
    op.drop_index(
        "ix_catalog_field_lineage_fact_id",
        table_name="catalog_field_lineage_projections",
    )
    op.drop_index(
        "ix_catalog_field_lineage_exact_output",
        table_name="catalog_field_lineage_projections",
    )
    op.drop_table("catalog_field_lineage_projections")
