"""run_states: DB-backed live run status (stateless web tier can serve /run/{id} + survive restart)

Revision ID: 0006_run_states
Revises: 0005_user_password
Create Date: 2026-07-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_run_states"
down_revision = "0005_user_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_states",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("canvas_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_run_states_canvas_id", "run_states", ["canvas_id"])
    op.create_index("ix_run_states_status", "run_states", ["status"])


def downgrade() -> None:
    op.drop_index("ix_run_states_status", table_name="run_states")
    op.drop_index("ix_run_states_canvas_id", table_name="run_states")
    op.drop_table("run_states")
