"""Persist immutable local-run source admissions.

Revision ID: 0004_local_run_input_admissions
Revises: 0003_repair_historical_metadata
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_local_run_input_admissions"
down_revision = "0003_repair_historical_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_input_admissions",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("creator_id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=True),
        sa.Column("submission_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.Text(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("creator_id", "canvas_id", "submission_id", name="uq_run_input_admission_submission"),
    )
    op.create_index("ix_run_input_admissions_canvas_id", "run_input_admissions", ["canvas_id"])
    op.add_column("run_records", sa.Column("input_manifest", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_records", "input_manifest")
    op.drop_index("ix_run_input_admissions_canvas_id", table_name="run_input_admissions")
    op.drop_table("run_input_admissions")
