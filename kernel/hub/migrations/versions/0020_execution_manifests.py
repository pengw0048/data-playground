"""Retain canonical execution manifests for graph-backed runs.

Revision ID: 0020_execution_manifests
Revises: 0019_exact_local_inputs
"""

import sqlalchemy as sa
from alembic import op


revision = "0020_execution_manifests"
down_revision = "0019_exact_local_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_manifests",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("semantic_doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sha256"),
    )
    for table in ("run_input_admissions", "run_states", "run_records"):
        op.add_column(
            table,
            sa.Column("execution_manifest_sha256", sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{table}_execution_manifest_sha256",
            table,
            ["execution_manifest_sha256"],
        )


def downgrade() -> None:
    for table in ("run_records", "run_states", "run_input_admissions"):
        op.drop_index(f"ix_{table}_execution_manifest_sha256", table_name=table)
        op.drop_column(table, "execution_manifest_sha256")
    op.drop_table("execution_manifests")
