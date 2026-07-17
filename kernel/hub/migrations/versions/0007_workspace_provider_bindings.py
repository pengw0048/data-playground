"""Persist bounded external Workspace reference display facts.

Revision ID: 0007_workspace_provider_bindings
Revises: 0006_typed_local_writes
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_workspace_provider_bindings"
down_revision = "0006_typed_local_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_provider_bindings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("mount_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=256), nullable=False),
        sa.Column("container_id", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("parent_binding_id", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("relinked_from_id", sa.String(length=32), nullable=True),
        sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('container', 'dataset')",
            name="ck_workspace_provider_binding_kind",
        ),
        sa.CheckConstraint(
            "state IN ('current', 'offline', 'permission_lost', 'detached', 'provider_error')",
            name="ck_workspace_provider_binding_state",
        ),
        sa.ForeignKeyConstraint(
            ["parent_binding_id"], ["workspace_provider_bindings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["relinked_from_id"], ["workspace_provider_bindings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_provider_bindings_mount_id",
        "workspace_provider_bindings", ["mount_id"], unique=False,
    )
    op.create_index(
        "ix_workspace_provider_bindings_updated_at",
        "workspace_provider_bindings", ["updated_at"], unique=False,
    )
    op.create_index(
        "ix_workspace_provider_binding_resource",
        "workspace_provider_bindings", ["mount_id", "provider", "resource_id", "active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_provider_binding_resource", table_name="workspace_provider_bindings",
    )
    op.drop_index(
        "ix_workspace_provider_bindings_updated_at", table_name="workspace_provider_bindings",
    )
    op.drop_index(
        "ix_workspace_provider_bindings_mount_id", table_name="workspace_provider_bindings",
    )
    op.drop_table("workspace_provider_bindings")
