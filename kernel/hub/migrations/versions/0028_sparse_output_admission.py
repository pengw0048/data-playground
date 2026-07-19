"""Persist immutable admission for one exact sparse output.

Revision ID: 0028_sparse_output_admission
Revises: 0027_distribution_reports
"""

import sqlalchemy as sa
from alembic import op


revision = "0028_sparse_output_admission"
down_revision = "0027_distribution_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sparse_outputs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("submission_id", sa.String(length=128), nullable=False),
        sa.Column("input_dataset_id", sa.String(), nullable=False),
        sa.Column("input_revision_id", sa.String(length=32), nullable=False),
        sa.Column("row_identity_spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_doc", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("producer_doc", sa.Text(), nullable=False),
        sa.Column("producer_sha256", sa.String(length=64), nullable=False),
        sa.Column("config_doc", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_doc", sa.Text(), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("provenance_doc", sa.Text(), nullable=False),
        sa.Column("provenance_sha256", sa.String(length=64), nullable=False),
        sa.Column("evidence_doc", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("intent_doc", sa.Text(), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_sparse_output_input_sha256"),
        sa.CheckConstraint("length(row_identity_spec_sha256) = 64", name="ck_sparse_output_spec_sha256"),
        sa.CheckConstraint("length(producer_sha256) = 64", name="ck_sparse_output_producer_sha256"),
        sa.CheckConstraint("length(config_sha256) = 64", name="ck_sparse_output_config_sha256"),
        sa.CheckConstraint("length(schema_sha256) = 64", name="ck_sparse_output_schema_sha256"),
        sa.CheckConstraint("length(provenance_sha256) = 64", name="ck_sparse_output_provenance_sha256"),
        sa.CheckConstraint("length(evidence_sha256) = 64", name="ck_sparse_output_evidence_sha256"),
        sa.CheckConstraint("length(intent_sha256) = 64", name="ck_sparse_output_intent_sha256"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["canvas_id"], ["canvases.id"]),
        sa.ForeignKeyConstraint(["input_dataset_id"], ["catalog_logical_datasets.logical_id"]),
        sa.ForeignKeyConstraint(["input_revision_id"], ["managed_local_file_revisions.revision_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "canvas_id", "submission_id",
            name="uq_sparse_output_owner_canvas_submission"),
    )
    op.create_index(
        "ix_sparse_outputs_owner_canvas_created", "sparse_outputs",
        ["owner_id", "canvas_id", "created_at"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM sparse_outputs LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while SparseOutput admissions are retained")
    if connection.execute(sa.text(
        "SELECT 1 FROM local_result_references WHERE owner_kind = 'sparse_output' LIMIT 1"
    )).first() is not None:
        raise RuntimeError("cannot downgrade while SparseOutput retention references are retained")
    op.drop_index("ix_sparse_outputs_owner_canvas_created", table_name="sparse_outputs")
    op.drop_table("sparse_outputs")
