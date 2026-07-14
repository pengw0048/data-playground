"""correlate durable runs with HTTP request / trace ids

Revision ID: 0022_run_request_id
Revises: 0021_local_result_artifacts
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_run_request_id"
down_revision = "0021_local_result_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: legacy rows and ad-hoc paths without an HTTP entry have no request id.
    op.add_column("run_records", sa.Column("request_id", sa.String(), nullable=True))
    op.add_column("run_states", sa.Column("request_id", sa.String(), nullable=True))
    op.create_index("ix_run_records_request_id", "run_records", ["request_id"])
    op.create_index("ix_run_states_request_id", "run_states", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_run_states_request_id", table_name="run_states")
    op.drop_index("ix_run_records_request_id", table_name="run_records")
    op.drop_column("run_states", "request_id")
    op.drop_column("run_records", "request_id")
