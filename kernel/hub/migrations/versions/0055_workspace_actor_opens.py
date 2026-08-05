"""Personal Workspace last-opened observations per actor.

Revision ID: 0055_workspace_actor_opens
Revises: 0054_workspace_favorites
"""

import sqlalchemy as sa
from alembic import op


revision = "0055_workspace_actor_opens"
down_revision = "0054_workspace_favorites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_actor_opens",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resource_ref", sa.String(length=640), nullable=False),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "resource_ref", name="pk_workspace_actor_opens"),
        sa.CheckConstraint(
            "resource_ref LIKE 'canvas:%' OR resource_ref LIKE 'dataset:%' "
            "OR resource_ref LIKE 'dataset_view:%'",
            name="ck_workspace_actor_opens_ref",
        ),
    )
    op.create_index(
        "ix_workspace_actor_opens_user_opened",
        "workspace_actor_opens",
        ["user_id", "last_opened_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_actor_opens_user_opened", table_name="workspace_actor_opens")
    op.drop_table("workspace_actor_opens")
