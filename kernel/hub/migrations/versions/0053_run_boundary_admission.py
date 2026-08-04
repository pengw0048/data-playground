"""Persist one opaque reusable local execution boundary per admitted run.

Revision ID: 0053_run_boundary_admission
Revises: 0052_rejected_run_owner
"""

import sqlalchemy as sa
from alembic import op


revision = "0053_run_boundary_admission"
down_revision = "0052_rejected_run_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_boundary_admissions",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("boundary_node_id", sa.String(), nullable=False),
        sa.Column("boundary_port_id", sa.String(), nullable=False),
        sa.Column("boundary_run_id", sa.String(), nullable=False),
        sa.Column("boundary_execution_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(boundary_execution_manifest_sha256) = 64",
            name="ck_run_boundary_manifest_sha256",
        ),
        sa.ForeignKeyConstraint(["canvas_id"], ["canvases.id"]),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run_input_admissions.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_run_boundary_admissions_canvas_id",
        "run_boundary_admissions",
        ["canvas_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_boundary_admissions_canvas_id", table_name="run_boundary_admissions")
    op.drop_table("run_boundary_admissions")
