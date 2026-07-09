"""named/versioned schema contracts (a workspace artifact multiple pipelines reference + diff)

A schema contract is a named, versioned list of columns. Nodes reference it by name (config.outputSchema
= {"ref": name}) so many pipelines share ONE contract instead of each carrying a private inline copy;
saving under an existing name mints a new version, so drift is a diff between versions, not a mystery.

Revision ID: 0013_schema_contracts
Revises: 0012_run_records_per_node
Create Date: 2026-07-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_schema_contracts"
down_revision = "0012_run_records_per_node"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schema_contracts",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("doc", sa.Text()),  # JSON: [{name, type}, ...]
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("schema_contracts")
