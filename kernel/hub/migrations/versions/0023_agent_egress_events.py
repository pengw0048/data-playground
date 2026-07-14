"""agent egress audit events (SEC-01 AgentDataPolicy)

Revision ID: 0023_agent_egress_events
Revises: 0022_backend_jobs
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op


revision = "0023_agent_egress_events"
down_revision = "0022_backend_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_egress_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("provider", sa.String(), nullable=False, server_default=""),
        sa.Column("model", sa.String(), nullable=False, server_default=""),
        sa.Column("tool", sa.String(), nullable=False, server_default=""),
        sa.Column("dataset", sa.Text(), nullable=True),
        sa.Column("columns_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("event_json", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_egress_events_created_at", "agent_egress_events", ["created_at"])
    op.create_index("ix_agent_egress_events_tool", "agent_egress_events", ["tool"])


def downgrade() -> None:
    op.drop_index("ix_agent_egress_events_tool", table_name="agent_egress_events")
    op.drop_index("ix_agent_egress_events_created_at", table_name="agent_egress_events")
    op.drop_table("agent_egress_events")
