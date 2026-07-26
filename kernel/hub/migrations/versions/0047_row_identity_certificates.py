"""Persist reusable logical row identity proofs for exact managed-local revisions.

Revision ID: 0047_row_identity_cert
Revises: 0046_relationship_incident
"""

import sqlalchemy as sa
from alembic import op


revision = "0047_row_identity_cert"
down_revision = "0046_relationship_incident"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_local_row_identity_certificates",
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("logical_id", sa.String(), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_identity_spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("certificate_doc", sa.Text(), nullable=False),
        sa.Column("certificate_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_dev", sa.BigInteger(), nullable=False),
        sa.Column("artifact_ino", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(schema_sha256) = 64", name="ck_row_identity_cert_schema_sha"),
        sa.CheckConstraint("length(row_identity_spec_sha256) = 64", name="ck_row_identity_cert_spec_sha"),
        sa.CheckConstraint("length(certificate_sha256) = 64", name="ck_row_identity_cert_doc_sha"),
        sa.CheckConstraint("artifact_dev >= 0", name="ck_row_identity_cert_artifact_dev"),
        sa.CheckConstraint("artifact_ino >= 0", name="ck_row_identity_cert_artifact_ino"),
        sa.ForeignKeyConstraint(["logical_id"], ["catalog_logical_datasets.logical_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["managed_local_file_revisions.revision_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("revision_id"),
    )
    op.create_index("ix_row_identity_certificates_logical_revision",
                    "managed_local_row_identity_certificates", ["logical_id", "revision_id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM managed_local_row_identity_certificates LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while row identity certificates are retained")
    op.drop_index("ix_row_identity_certificates_logical_revision",
                  table_name="managed_local_row_identity_certificates")
    op.drop_table("managed_local_row_identity_certificates")
