"""Durable request replay for local Workspace Folder creation.

Revision ID: 0039_folder_replays
Revises: 0038_inbox_dataset_scoped
"""

import sqlalchemy as sa
from alembic import op


revision = "0039_folder_replays"
down_revision = "0038_inbox_dataset_scoped"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_folder_create_replays",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(intent_sha256) = 64", name="ck_workspace_folder_create_replay_sha"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("owner_id", "request_id"),
    )


def downgrade() -> None:
    if op.get_bind().execute(
            sa.text("SELECT 1 FROM workspace_folder_create_replays LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while Workspace Folder create replays are retained")
    op.drop_table("workspace_folder_create_replays")
