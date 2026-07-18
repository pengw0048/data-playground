"""Persist immutable exact DatasetViews in the local Workspace.

Revision ID: 0026_dataset_views
Revises: 0025_transform_library_keys
"""

import sqlalchemy as sa
from alembic import op


revision = "0026_dataset_views"
down_revision = "0025_transform_library_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_views",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("submission_id", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("definition_doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(definition_sha256) = 64",
            name="ck_dataset_view_definition_sha256",
        ),
        sa.CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_dataset_view_request_sha256",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "submission_id", name="uq_dataset_view_owner_submission"),
    )
    op.create_index(
        "ix_dataset_views_owner_deleted",
        "dataset_views",
        ["owner_id", "deleted_at", "created_at"],
    )
    with op.batch_alter_table("workspace_placements") as batch:
        batch.drop_constraint("ck_workspace_placement_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_placement_kind",
            "target_kind IN ('canvas', 'dataset', 'dataset_view')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM dataset_views LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade while DatasetView identities are retained")
    if connection.execute(sa.text(
        "SELECT 1 FROM workspace_placements "
        "WHERE target_kind = 'dataset_view' LIMIT 1"
    )).first() is not None:
        raise RuntimeError("cannot downgrade while DatasetView placements are retained")
    with op.batch_alter_table("workspace_placements") as batch:
        batch.drop_constraint("ck_workspace_placement_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_placement_kind",
            "target_kind IN ('canvas', 'dataset')",
        )
    op.drop_index("ix_dataset_views_owner_deleted", table_name="dataset_views")
    op.drop_table("dataset_views")
