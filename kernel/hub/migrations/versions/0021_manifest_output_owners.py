"""Retain execution manifests through durable outputs and lineage.

Revision ID: 0021_manifest_output_owners
Revises: 0020_execution_manifests
"""

import sqlalchemy as sa
from alembic import op


revision = "0021_manifest_output_owners"
down_revision = "0020_execution_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalog_lineage_facts",
        sa.Column("execution_manifest_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_catalog_lineage_facts_execution_manifest_sha256",
        "catalog_lineage_facts",
        ["execution_manifest_sha256"],
    )
    op.create_index(
        "ix_catalog_lineage_facts_run_id",
        "catalog_lineage_facts",
        ["run_id"],
    )
    for table in ("managed_local_file_revisions", "managed_local_lance_write_receipts"):
        op.add_column(table, sa.Column("run_id", sa.String(), nullable=True))
        op.create_index(f"ix_{table}_run_id", table, ["run_id"])
        op.add_column(
            table,
            sa.Column("execution_manifest_sha256", sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{table}_execution_manifest_sha256",
            table,
            ["execution_manifest_sha256"],
        )


def downgrade() -> None:
    for table in ("managed_local_lance_write_receipts", "managed_local_file_revisions"):
        op.drop_index(f"ix_{table}_execution_manifest_sha256", table_name=table)
        op.drop_column(table, "execution_manifest_sha256")
        op.drop_index(f"ix_{table}_run_id", table_name=table)
        op.drop_column(table, "run_id")
    op.drop_index(
        "ix_catalog_lineage_facts_run_id",
        table_name="catalog_lineage_facts",
    )
    op.drop_index(
        "ix_catalog_lineage_facts_execution_manifest_sha256",
        table_name="catalog_lineage_facts",
    )
    op.drop_column("catalog_lineage_facts", "execution_manifest_sha256")
