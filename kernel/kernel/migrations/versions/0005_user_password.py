"""per-user credentials: users.password_hash

Revision ID: 0005_user_password
Revises: 0004_canvas_versions
Create Date: 2026-07-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_user_password"
down_revision = "0004_canvas_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("password_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("password_hash")
