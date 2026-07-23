"""Mint opaque canonical provider Source binding generations.

Revision ID: 0043_provider_source_binding
Revises: 0042_field_lineage
"""

import secrets

import sqlalchemy as sa
from alembic import op


revision = "0043_provider_source_binding"
down_revision = "0042_field_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_provider_datasets") as batch:
        batch.add_column(sa.Column(
            "source_binding_id", sa.String(length=32), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT mount_id, provider_dataset_id FROM workspace_provider_datasets"
    )).mappings()
    for row in rows:
        connection.execute(sa.text(
            "UPDATE workspace_provider_datasets "
            "SET source_binding_id = :source_binding_id "
            "WHERE mount_id = :mount_id AND provider_dataset_id = :provider_dataset_id"
        ), {
            "source_binding_id": secrets.token_hex(16),
            "mount_id": row["mount_id"],
            "provider_dataset_id": row["provider_dataset_id"],
        })

    with op.batch_alter_table("workspace_provider_datasets") as batch:
        batch.alter_column(
            "source_binding_id",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_workspace_provider_dataset_source_binding",
            ["mount_id", "source_binding_id"],
        )
        batch.create_check_constraint(
            "ck_workspace_provider_dataset_source_binding",
            "length(source_binding_id) = 32",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM workspace_provider_datasets LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while canonical provider Source bindings are retained")

    with op.batch_alter_table("workspace_provider_datasets") as batch:
        batch.drop_constraint(
            "ck_workspace_provider_dataset_source_binding",
            type_="check",
        )
        batch.drop_constraint(
            "uq_workspace_provider_dataset_source_binding",
            type_="unique",
        )
        batch.drop_column("source_binding_id")
