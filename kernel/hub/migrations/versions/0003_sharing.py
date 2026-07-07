"""sharing: canvases.visibility + canvas_shares

Revision ID: 0003_sharing
Revises: 0002_run_records
Create Date: 2026-07-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_sharing"
down_revision = "0002_run_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("canvases") as b:
        b.add_column(sa.Column("visibility", sa.String(), server_default="private", nullable=False))
    op.create_table(
        "canvas_shares",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("canvas_id", sa.String(), sa.ForeignKey("canvases.id"), index=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), index=True),
        sa.Column("role", sa.String(), server_default="editor"),
        sa.UniqueConstraint("canvas_id", "user_id", name="uq_share"),
    )


def downgrade() -> None:
    op.drop_table("canvas_shares")
    with op.batch_alter_table("canvases") as b:
        b.drop_column("visibility")
