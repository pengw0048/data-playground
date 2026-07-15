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
    with op.batch_alter_table("run_terminal_fences") as batch:
        batch.add_column(
            sa.Column("job_type", sa.String(), nullable=False, server_default="run"))
        batch.add_column(sa.Column("target_node_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("plan_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("profile_attempt_order", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_terminal_fence_profile_attempt_positive",
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1")
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(
            sa.Column("job_type", sa.String(), nullable=False, server_default="run"))
        batch.add_column(sa.Column("target_node_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("plan_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("profile_attempt_order", sa.BigInteger(), nullable=True))
        # Nullable keeps legacy rows ordinary; new ORM rows always receive a timestamp.
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint(
            "uq_run_state_canvas_profile_attempt", ["canvas_id", "profile_attempt_order"])
        batch.create_check_constraint(
            "ck_run_state_profile_attempt_positive",
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1")
    op.create_table(
        "profile_job_latest",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("attempt_order", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "canvas_id", "attempt_order", name="uq_profile_latest_canvas_attempt"),
        sa.CheckConstraint("attempt_order >= 1", name="ck_profile_latest_attempt_positive"),
        sa.PrimaryKeyConstraint("canvas_id", "target_node_id", "plan_digest"),
    )
    op.create_index(
        "ix_profile_job_latest_canvas_attempt",
        "profile_job_latest",
        ["canvas_id", "attempt_order"],
    )
    op.create_table(
        "profile_job_retention",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("next_attempt_order", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("cutoff_attempt_order", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("next_attempt_order >= 1", name="ck_profile_next_attempt_positive"),
        sa.CheckConstraint(
            "cutoff_attempt_order IS NULL OR cutoff_attempt_order >= 1",
            name="ck_profile_cutoff_attempt_positive"),
        sa.PrimaryKeyConstraint("canvas_id"),
    )


def downgrade() -> None:
    op.drop_table("profile_job_retention")
    op.drop_index("ix_profile_job_latest_canvas_attempt", table_name="profile_job_latest")
    op.drop_table("profile_job_latest")
    with op.batch_alter_table("run_states") as batch:
        batch.drop_constraint("ck_run_state_profile_attempt_positive", type_="check")
        batch.drop_constraint("uq_run_state_canvas_profile_attempt", type_="unique")
        batch.drop_column("created_at")
        batch.drop_column("plan_digest")
        batch.drop_column("target_node_id")
        batch.drop_column("profile_attempt_order")
        batch.drop_column("job_type")
    with op.batch_alter_table("run_terminal_fences") as batch:
        batch.drop_constraint("ck_terminal_fence_profile_attempt_positive", type_="check")
        batch.drop_column("profile_attempt_order")
        batch.drop_column("plan_digest")
        batch.drop_column("target_node_id")
        batch.drop_column("job_type")
