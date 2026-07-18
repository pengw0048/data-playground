"""Persist immutable owner-scoped promoted Transform versions.

Revision ID: 0024_promoted_transforms
Revises: 0023_catalog_folder_overlay
"""

import sqlalchemy as sa
from alembic import op


revision = "0024_promoted_transforms"
down_revision = "0023_catalog_folder_overlay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promoted_transforms",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "key", name="uq_promoted_transform_owner_key"),
    )
    op.create_index(
        "ix_promoted_transforms_owner_id", "promoted_transforms", ["owner_id"])
    op.create_table(
        "promoted_transform_versions",
        sa.Column("transform_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("semantic_digest", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("blurb", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.Text(), nullable=False),
        sa.Column("output_schema", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("creator_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_promoted_transform_version_positive"),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["transform_id"], ["promoted_transforms.id"]),
        sa.PrimaryKeyConstraint("transform_id", "version"),
        sa.UniqueConstraint(
            "transform_id", "semantic_digest", name="uq_promoted_transform_digest"),
    )
    op.create_table(
        "promoted_transform_version_refs",
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_key", sa.String(length=512), nullable=False),
        sa.Column("transform_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "owner_kind IN ('canvas', 'canvas_version', 'execution_manifest')",
            name="ck_promoted_transform_ref_owner_kind",
        ),
        sa.ForeignKeyConstraint(
            ["transform_id", "version"],
            ["promoted_transform_versions.transform_id", "promoted_transform_versions.version"],
            name="fk_promoted_transform_version_ref",
        ),
        sa.PrimaryKeyConstraint("owner_kind", "owner_key", "transform_id", "version"),
    )
    op.create_index(
        "ix_promoted_transform_version_refs_version",
        "promoted_transform_version_refs",
        ["transform_id", "version"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained = connection.execute(sa.text(
        "SELECT 1 FROM promoted_transforms LIMIT 1"
    )).first()
    if retained is not None:
        raise RuntimeError(
            "cannot downgrade while promoted Transform identities are retained")
    op.drop_index(
        "ix_promoted_transform_version_refs_version",
        table_name="promoted_transform_version_refs",
    )
    op.drop_table("promoted_transform_version_refs")
    op.drop_table("promoted_transform_versions")
    op.drop_index("ix_promoted_transforms_owner_id", table_name="promoted_transforms")
    op.drop_table("promoted_transforms")
