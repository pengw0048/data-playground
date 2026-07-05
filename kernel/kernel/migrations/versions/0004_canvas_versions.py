"""canvas_versions: server-side snapshot history for restore

Revision ID: 0004_canvas_versions
Revises: 0003_sharing
Create Date: 2026-07-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_canvas_versions"
down_revision = "0003_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_versions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("canvas_id", sa.String(), sa.ForeignKey("canvases.id"), index=True),
        sa.Column("version", sa.Integer()),
        sa.Column("doc", sa.Text()),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("canvas_versions")
