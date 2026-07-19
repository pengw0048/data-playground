"""Persist fenced local overlay anchors for external Workspace containers.

Revision ID: 0034_external_overlay
Revises: 0033_temporal_task
"""

import sqlalchemy as sa
from alembic import op


revision = "0034_external_overlay"
down_revision = "0033_temporal_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_external_overlay_anchors",
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("container_id", sa.String(), nullable=False),
        # These are identity snapshots, not a mutable lookup key.  A relink always receives a
        # different binding generation and therefore a different anchor even when an upstream
        # provider reuses the same resource ID.
        sa.Column("mount_id", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["workspace_provider_bindings.id"]),
        sa.ForeignKeyConstraint(["container_id"], ["workspace_containers.id"]),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint("container_id", name="uq_workspace_external_overlay_container"),
    )
    op.create_table(
        "workspace_canvas_create_replays",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("owner_id", "request_id"),
        sa.CheckConstraint("length(intent_sha256) = 64", name="ck_workspace_canvas_create_replay_sha"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM workspace_canvas_create_replays LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while Canvas create replay records are retained")
    if connection.execute(sa.text("SELECT 1 FROM workspace_external_overlay_anchors LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while external Workspace overlay anchors are retained")
    op.drop_table("workspace_canvas_create_replays")
    op.drop_table("workspace_external_overlay_anchors")
