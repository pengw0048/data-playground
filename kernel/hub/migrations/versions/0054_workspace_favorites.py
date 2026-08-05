"""Personal Workspace favorites for datasets and Canvases.

Revision ID: 0054_workspace_favorites
Revises: 0053_run_boundary_admission
"""

import sqlalchemy as sa
from alembic import op


revision = "0054_workspace_favorites"
down_revision = "0053_run_boundary_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_favorites",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(length=768), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_id LIKE 'canvas:%' OR resource_id LIKE 'dataset:%'",
            name="ck_workspace_favorites_kind",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("owner_id", "resource_id"),
    )
    op.create_index(
        "ix_workspace_favorites_owner_created",
        "workspace_favorites",
        ["owner_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_favorites_owner_created", table_name="workspace_favorites")
    op.drop_table("workspace_favorites")
