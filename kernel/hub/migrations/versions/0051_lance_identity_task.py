"""Freeze source-specific authority for row identity certification Tasks.

Revision ID: 0051_lance_identity_task
Revises: 0050_receipt_names
"""

import sqlalchemy as sa
from alembic import op


revision = "0051_lance_identity_task"
down_revision = "0050_receipt_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("row_identity_certification_task_envelopes") as batch:
        batch.add_column(sa.Column(
            "source_kind", sa.String(length=16), nullable=False,
            server_default="parquet"))
        batch.add_column(sa.Column(
            "physical_incarnation_sha256", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ck_row_identity_task_source_fence",
            "(source_kind = 'parquet' AND physical_incarnation_sha256 IS NULL) "
            "OR (source_kind = 'lance' AND physical_incarnation_sha256 IS NOT NULL "
            "AND length(physical_incarnation_sha256) = 64)")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text(
            "SELECT 1 FROM row_identity_certification_task_envelopes "
            "WHERE source_kind = 'lance' LIMIT 1")).first() is not None:
        raise RuntimeError(
            "cannot downgrade while managed Lance row identity Tasks are retained")
    with op.batch_alter_table("row_identity_certification_task_envelopes") as batch:
        batch.drop_constraint("ck_row_identity_task_source_fence", type_="check")
        batch.drop_column("physical_incarnation_sha256")
        batch.drop_column("source_kind")
