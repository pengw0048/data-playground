"""users.token_epoch — a per-user session epoch for token revocation.

A session token embeds the epoch it was signed under; auth.verify rejects a token whose embedded
epoch != the user's current epoch. Bumping the epoch (on password change, and — once they exist —
account disable/delete) invalidates every outstanding token for that user immediately, instead of
waiting out the 7-day TTL.

Revision ID: 0014_user_token_epoch
Revises: 0013_schema_contracts
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_user_token_epoch"
down_revision = "0013_schema_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_epoch", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "token_epoch")
