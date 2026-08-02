"""Retain one Canvas-lifetime current result projection per target.

Revision ID: 0051_canvas_result_latest
Revises: 0050_receipt_names
"""

import sqlalchemy as sa
from alembic import op


revision = "0051_canvas_result_latest"
down_revision = "0050_receipt_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_result_latest",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("terminal_run_id", sa.String(), nullable=False),
        sa.Column("terminal_status", sa.String(), nullable=False),
        sa.Column("terminal_execution_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("terminal_doc", sa.Text(), nullable=False),
        sa.Column("terminal_submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_run_id", sa.String(), nullable=True),
        sa.Column("result_execution_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_outputs", sa.Text(), server_default="[]", nullable=False),
        sa.Column("result_input_manifest", sa.Text(), nullable=True),
        sa.Column("result_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "terminal_status IN ('done','failed','cancelled')",
            name="ck_canvas_result_latest_terminal_status",
        ),
        sa.ForeignKeyConstraint(["canvas_id"], ["canvases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canvas_id", "target_node_id", name="uq_canvas_result_latest_target"),
    )
    op.create_index(
        "ix_canvas_result_latest_canvas_id", "canvas_result_latest", ["canvas_id"])
    op.create_index(
        "ix_canvas_result_latest_terminal_run_id",
        "canvas_result_latest", ["terminal_run_id"])
    op.create_index(
        "ix_canvas_result_latest_terminal_execution_manifest_sha256",
        "canvas_result_latest", ["terminal_execution_manifest_sha256"])
    op.create_index(
        "ix_canvas_result_latest_result_run_id",
        "canvas_result_latest", ["result_run_id"])
    op.create_index(
        "ix_canvas_result_latest_result_execution_manifest_sha256",
        "canvas_result_latest", ["result_execution_manifest_sha256"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM canvas_result_latest LIMIT 1")).first():
        raise RuntimeError(
            "cannot downgrade while Canvas current results are retained")
    op.drop_index(
        "ix_canvas_result_latest_result_execution_manifest_sha256",
        table_name="canvas_result_latest")
    op.drop_index(
        "ix_canvas_result_latest_result_run_id", table_name="canvas_result_latest")
    op.drop_index(
        "ix_canvas_result_latest_terminal_execution_manifest_sha256",
        table_name="canvas_result_latest")
    op.drop_index(
        "ix_canvas_result_latest_terminal_run_id", table_name="canvas_result_latest")
    op.drop_index(
        "ix_canvas_result_latest_canvas_id", table_name="canvas_result_latest")
    op.drop_table("canvas_result_latest")
