"""Separate mounted provider dataset state from placement occurrences.

Revision ID: 0041_provider_canonical
Revises: 0040_managed_sidecar
"""

import sqlalchemy as sa
from alembic import op


revision = "0041_provider_canonical"
down_revision = "0040_managed_sidecar"
branch_labels = None
depends_on = None


_STATES = "('current', 'offline', 'permission_lost', 'detached', 'provider_error')"


def upgrade() -> None:
    op.create_table(
        "workspace_provider_datasets",
        sa.Column("mount_id", sa.String(length=128), nullable=False),
        sa.Column("provider_dataset_id", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=256), nullable=False),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("columns_doc", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("mount_id", "provider_dataset_id"),
        sa.CheckConstraint(
            f"state IN {_STATES}",
            name="ck_workspace_provider_dataset_state",
        ),
        sa.CheckConstraint(
            "(uri IS NULL AND columns_doc IS NULL) OR "
            "(uri IS NOT NULL AND columns_doc IS NOT NULL)",
            name="ck_workspace_provider_dataset_detail_pair",
        ),
    )
    op.create_index(
        "ix_workspace_provider_datasets_updated_at",
        "workspace_provider_datasets",
        ["updated_at"],
        unique=False,
    )

    op.drop_index(
        "ix_workspace_provider_binding_resource",
        table_name="workspace_provider_bindings",
    )
    with op.batch_alter_table("workspace_provider_bindings") as batch:
        batch.alter_column(
            "resource_id",
            new_column_name="provider_placement_id",
            existing_type=sa.String(length=512),
            existing_nullable=False,
        )
        batch.add_column(sa.Column(
            "parent_provider_placement_id", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column(
            "provider_dataset_id", sa.String(length=512), nullable=True))
    with op.batch_alter_table("workspace_external_overlay_anchors") as batch:
        batch.alter_column(
            "resource_id",
            new_column_name="provider_placement_id",
            existing_type=sa.String(length=512),
            existing_nullable=False,
        )

    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE workspace_provider_bindings AS child "
        "SET parent_provider_placement_id = ("
        "SELECT parent.provider_placement_id FROM workspace_provider_bindings AS parent "
        "WHERE parent.id = child.parent_binding_id"
        ") WHERE child.parent_binding_id IS NOT NULL"
    ))

    with op.batch_alter_table("workspace_provider_bindings") as batch:
        batch.create_foreign_key(
            "fk_workspace_provider_binding_dataset",
            "workspace_provider_datasets",
            ["mount_id", "provider_dataset_id"],
            ["mount_id", "provider_dataset_id"],
        )
    op.create_index(
        "ix_workspace_provider_binding_placement",
        "workspace_provider_bindings",
        ["mount_id", "provider_placement_id", "active"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_provider_binding_dataset",
        "workspace_provider_bindings",
        ["mount_id", "provider_dataset_id", "active"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM workspace_provider_bindings "
            "WHERE provider_dataset_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while canonical provider dataset placements are retained")
    if connection.execute(sa.text(
            "SELECT 1 FROM workspace_provider_datasets LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while canonical provider dataset state is retained")

    op.drop_index(
        "ix_workspace_provider_binding_dataset",
        table_name="workspace_provider_bindings",
    )
    op.drop_index(
        "ix_workspace_provider_binding_placement",
        table_name="workspace_provider_bindings",
    )
    with op.batch_alter_table("workspace_provider_bindings") as batch:
        batch.drop_constraint(
            "fk_workspace_provider_binding_dataset",
            type_="foreignkey",
        )
        batch.drop_column("provider_dataset_id")
        batch.drop_column("parent_provider_placement_id")
        batch.alter_column(
            "provider_placement_id",
            new_column_name="resource_id",
            existing_type=sa.String(length=512),
            existing_nullable=False,
        )
    with op.batch_alter_table("workspace_external_overlay_anchors") as batch:
        batch.alter_column(
            "provider_placement_id",
            new_column_name="resource_id",
            existing_type=sa.String(length=512),
            existing_nullable=False,
        )
    op.create_index(
        "ix_workspace_provider_binding_resource",
        "workspace_provider_bindings",
        ["mount_id", "provider", "resource_id", "active"],
        unique=False,
    )
    op.drop_index(
        "ix_workspace_provider_datasets_updated_at",
        table_name="workspace_provider_datasets",
    )
    op.drop_table("workspace_provider_datasets")
