"""Retain canonical execution manifests for graph-backed durable Tasks.

Revision ID: 0022_task_manifests
Revises: 0021_manifest_output_owners
"""

import sqlalchemy as sa
from alembic import op


revision = "0022_task_manifests"
down_revision = "0021_manifest_output_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("durable_tasks", "durable_task_attempts", "durable_task_inbox_items"):
        op.add_column(
            table,
            sa.Column("execution_manifest_sha256", sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{table}_execution_manifest_sha256",
            table,
            ["execution_manifest_sha256"],
        )
    with op.batch_alter_table("durable_tasks") as batch:
        batch.alter_column("graph_doc", existing_type=sa.Text(), nullable=True)
        batch.alter_column("input_manifest", existing_type=sa.Text(), nullable=True)
        batch.alter_column("write_intent", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    retained = connection.execute(sa.text(
        "SELECT 1 FROM durable_tasks "
        "WHERE execution_manifest_sha256 IS NOT NULL OR graph_doc IS NULL "
        "OR input_manifest IS NULL OR write_intent IS NULL LIMIT 1"
    )).first()
    if retained is not None:
        raise RuntimeError(
            "cannot downgrade while canonical-manifest durable Tasks are retained")
    with op.batch_alter_table("durable_tasks") as batch:
        batch.alter_column("write_intent", existing_type=sa.Text(), nullable=False)
        batch.alter_column("input_manifest", existing_type=sa.Text(), nullable=False)
        batch.alter_column("graph_doc", existing_type=sa.Text(), nullable=False)
    for table in ("durable_task_inbox_items", "durable_task_attempts", "durable_tasks"):
        op.drop_index(f"ix_{table}_execution_manifest_sha256", table_name=table)
        op.drop_column(table, "execution_manifest_sha256")
