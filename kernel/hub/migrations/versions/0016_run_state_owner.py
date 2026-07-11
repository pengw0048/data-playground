"""run_states.created_by + auth_canvas_id — durable run ownership for authorization (P0-AUTH-02).

A run's authorization must not rest on the client-supplied graph id (canvas_id) alone: that lives in
the same namespace as canvas ids, and a stranger could POST a canvas with an ad-hoc run's id to
"claim" it. `created_by` records the run's creator (authoritative, survives restart / other stateless
instances). `auth_canvas_id` is set ONLY when the run was launched against a real canvas the creator
could reach at creation time, so the shared-collaborator grant (canvas_role) is consulted only for a
genuinely-bound canvas — never for an ad-hoc id a stranger could later create.

Revision ID: 0016_run_state_owner
Revises: 0015_share_role_check
Create Date: 2026-07-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_run_state_owner"
down_revision = "0015_share_role_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_states", sa.Column("created_by", sa.String(), nullable=True))
    op.add_column("run_states", sa.Column("auth_canvas_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_states", "auth_canvas_id")
    op.drop_column("run_states", "created_by")
