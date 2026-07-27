"""Persist exact managed-Lance row identity fences and certificates.

Revision ID: 0049_lance_row_identity_cert
Revises: 0048_row_identity_task
"""

import sqlalchemy as sa
from alembic import op


revision = "0049_lance_row_identity_cert"
down_revision = "0048_row_identity_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_local_lance_row_identity_fences",
        sa.Column("registration_id", sa.String(length=32), nullable=False),
        sa.Column("revision_id", sa.String(length=256), nullable=False),
        sa.Column("physical_incarnation_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_identity_spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(physical_incarnation_sha256) = 64",
            name="ck_lance_row_identity_fence_incarnation_sha"),
        sa.CheckConstraint(
            "length(schema_sha256) = 64", name="ck_lance_row_identity_fence_schema_sha"),
        sa.CheckConstraint(
            "length(row_identity_spec_sha256) = 64",
            name="ck_lance_row_identity_fence_spec_sha"),
        sa.ForeignKeyConstraint(
            ["registration_id"], ["catalog_entries.registration_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("registration_id", "revision_id"),
    )
    op.create_table(
        "managed_local_lance_row_identity_certificates",
        sa.Column("registration_id", sa.String(length=32), nullable=False),
        sa.Column("revision_id", sa.String(length=256), nullable=False),
        sa.Column("certificate_doc", sa.Text(), nullable=False),
        sa.Column("certificate_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(certificate_sha256) = 64", name="ck_lance_row_identity_cert_doc_sha"),
        sa.ForeignKeyConstraint(
            ["registration_id", "revision_id"],
            [
                "managed_local_lance_row_identity_fences.registration_id",
                "managed_local_lance_row_identity_fences.revision_id",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("registration_id", "revision_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
            "managed_local_lance_row_identity_certificates",
            "managed_local_lance_row_identity_fences"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("cannot downgrade while managed Lance row identity evidence is retained")
    op.drop_table("managed_local_lance_row_identity_certificates")
    op.drop_table("managed_local_lance_row_identity_fences")
