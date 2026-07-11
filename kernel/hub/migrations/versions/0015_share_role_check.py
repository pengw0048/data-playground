"""canvas_shares.role CHECK — a share may only grant 'editor' or 'viewer', never 'owner'.

Before the fix, POST /canvas/{id}/share accepted an arbitrary `role`, so an owner could grant the
literal 'owner' role and canvas_role() would then treat the recipient as owner (delete/reshare). This
migration downgrades any such out-of-band role to 'viewer' and adds a CHECK so the DB rejects it too.

Revision ID: 0015_share_role_check
Revises: 0014_user_token_epoch
Create Date: 2026-07-11
"""
from alembic import op

revision = "0015_share_role_check"
down_revision = "0014_user_token_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # clean first: any legacy/out-of-band role (incl. an escalated 'owner') becomes the safe minimum
    op.execute("UPDATE canvas_shares SET role = 'viewer' WHERE role NOT IN ('editor', 'viewer')")
    # batch mode: recreates the table with the constraint on SQLite, ALTER ADD CONSTRAINT elsewhere
    with op.batch_alter_table("canvas_shares") as b:
        b.create_check_constraint("ck_share_role", "role IN ('editor', 'viewer')")


def downgrade() -> None:
    with op.batch_alter_table("canvas_shares") as b:
        b.drop_constraint("ck_share_role", type_="check")
