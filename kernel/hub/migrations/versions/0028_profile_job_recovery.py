"""persist bounded latest profile jobs for reopen recovery

Revision ID: 0028_profile_job_recovery
Revises: 0027_catalog_folders
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_profile_job_recovery"
down_revision = "0027_catalog_folders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_states",
        sa.Column("job_type", sa.String(), nullable=False, server_default="run"),
    )
    op.add_column("run_states", sa.Column("target_node_id", sa.String(), nullable=True))
    op.add_column("run_states", sa.Column("plan_digest", sa.String(length=64), nullable=True))
    # Nullable keeps SQLite's ALTER TABLE path portable. New ORM rows always receive a timestamp;
    # pre-upgrade rows are ordinary runs and do not participate in profile recovery.
    op.add_column("run_states", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "profile_job_latest",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("canvas_id", "target_node_id", "plan_digest"),
    )
    op.create_index(
        "ix_profile_job_latest_canvas_submitted",
        "profile_job_latest",
        ["canvas_id", "submitted_at"],
    )
    op.create_table(
        "profile_job_retention",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("cutoff_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutoff_run_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("canvas_id"),
    )


def downgrade() -> None:
    op.drop_table("profile_job_retention")
    op.drop_index("ix_profile_job_latest_canvas_submitted", table_name="profile_job_latest")
    op.drop_table("profile_job_latest")
    op.drop_column("run_states", "created_at")
    op.drop_column("run_states", "plan_digest")
    op.drop_column("run_states", "target_node_id")
    op.drop_column("run_states", "job_type")
