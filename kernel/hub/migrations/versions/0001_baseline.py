"""baseline schema: users, canvases, settings

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "canvases",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id"), index=True),
        sa.Column("name", sa.String()),
        sa.Column("version", sa.Integer()),
        sa.Column("doc", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String()),
        sa.Column("scope_id", sa.String()),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.Text()),
        sa.UniqueConstraint("scope", "scope_id", "key", name="uq_setting"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("canvases")
    op.drop_table("users")
