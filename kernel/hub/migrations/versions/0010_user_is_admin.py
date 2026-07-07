"""users.is_admin — a role flag gating instance-wide config (global settings + user management).

The bootstrap/default user ('local') becomes admin; new users default to non-admin. On an upgraded
DB with no admin yet, init_db promotes the default user (see metadb.init_db).

Revision ID: 0010_user_is_admin
Revises: 0009_catalog_relationships
Create Date: 2026-07-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_user_is_admin"
down_revision = "0009_catalog_relationships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute("UPDATE users SET is_admin = 1 WHERE id = 'local'")  # the bootstrap user is the admin


def downgrade() -> None:
    op.drop_column("users", "is_admin")
