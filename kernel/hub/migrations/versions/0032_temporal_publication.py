"""Persist private temporal output publication and compound heads.

Revision ID: 0032_temporal_pub
Revises: 0031_durable_merge
"""

import sqlalchemy as sa
from alembic import op


revision = "0032_temporal_pub"
down_revision = "0031_durable_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compound_dataset_revisions",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_doc", sa.Text(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("owner_id", "dataset_id", "revision_id"),
    )
    op.create_table(
        "compound_dataset_heads",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "dataset_id"),
    )
    op.create_table(
        "temporal_resample_publications",
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("write_intent_doc", sa.Text(), nullable=False),
        sa.Column("parent_dataset_id", sa.String(length=128), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=64), nullable=False),
        sa.Column("child_revision_id", sa.String(length=64), nullable=False),
        sa.Column("spec_doc", sa.Text(), nullable=False),
        sa.Column("evidence_doc", sa.Text(), nullable=False),
        sa.Column("candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("output_member_id", sa.String(length=128), nullable=False),
        sa.Column("output_revision_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(candidate_digest) = 64", name="ck_temporal_resample_candidate_sha"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["output_revision_id"], ["managed_local_file_revisions.revision_id"]),
        sa.PrimaryKeyConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "owner_id", "parent_dataset_id", "child_revision_id",
            name="uq_temporal_resample_owner_child"),
        sa.UniqueConstraint("output_revision_id"),
    )


def downgrade() -> None:
    retained = any(op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {table} LIMIT 1")).first() is not None for table in (
            "temporal_resample_publications", "compound_dataset_heads",
            "compound_dataset_revisions"))
    if retained:
        raise RuntimeError("cannot downgrade while temporal compound state is retained")
    op.drop_table("temporal_resample_publications")
    op.drop_table("compound_dataset_heads")
    op.drop_table("compound_dataset_revisions")
