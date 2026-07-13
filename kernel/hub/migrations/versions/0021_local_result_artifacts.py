"""reference-aware lifecycle for built-in local full results

Revision ID: 0021_local_result_artifacts
Revises: 0020_object_attempt_lifecycle
Create Date: 2026-07-13
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "0021_local_result_artifacts"
down_revision = "0020_object_attempt_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_result_registry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.Column("lock_cursor_uri", sa.Text(), nullable=True),
        sa.Column("reclaim_cursor_uri", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_local_result_registry_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "local_result_registry",
            sa.column("id", sa.Integer()),
            sa.column("owner_token", sa.String()),
            sa.column("lock_cursor_uri", sa.Text()),
            sa.column("reclaim_cursor_uri", sa.Text()),
        ),
        [{
            "id": 1,
            "owner_token": uuid.uuid4().hex,
            "lock_cursor_uri": None,
            "reclaim_cursor_uri": None,
        }],
    )
    op.create_table(
        "local_result_artifacts",
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("namespace_id", sa.String(), nullable=False),
        sa.Column("storage_root", sa.Text(), nullable=False),
        sa.Column("lock_name", sa.String(), nullable=False),
        sa.Column("lock_token", sa.String(), nullable=True),
        sa.Column("lock_protected", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(), nullable=False, server_default="writing"),
        sa.Column("writer_run_id", sa.String(), nullable=True),
        sa.Column("writer_token", sa.String(), nullable=True),
        sa.Column("delete_token", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('writing', 'ready', 'deleting')",
            name="ck_local_result_artifact_state"),
        sa.CheckConstraint(
            "((writer_run_id IS NULL AND writer_token IS NULL) OR "
            "(writer_run_id IS NOT NULL AND writer_token IS NOT NULL))",
            name="ck_local_result_artifact_writer_pair"),
        sa.CheckConstraint(
            "((lock_protected AND lock_token IS NOT NULL) OR "
            "(NOT lock_protected AND lock_token IS NULL))",
            name="ck_local_result_artifact_lock_pair"),
        sa.CheckConstraint(
            "((state = 'deleting' AND delete_token IS NOT NULL "
            "AND delete_attempted_at IS NOT NULL) OR "
            "(state <> 'deleting' AND delete_token IS NULL "
            "AND delete_attempted_at IS NULL))",
            name="ck_local_result_artifact_delete_state"),
        sa.CheckConstraint(
            "state <> 'ready' OR committed_at IS NOT NULL",
            name="ck_local_result_artifact_ready_commit"),
        sa.PrimaryKeyConstraint("uri"),
        sa.UniqueConstraint(
            "namespace_id", "lock_name",
            name="uq_local_result_artifact_namespace_lock"),
    )
    op.create_index(
        "ix_local_result_artifacts_reclaim", "local_result_artifacts",
        ["namespace_id", "state", "delete_attempted_at", "created_at"])
    op.create_index(
        "ix_local_result_artifacts_writer", "local_result_artifacts",
        ["writer_run_id", "writer_token"])
    op.create_table(
        "local_result_references",
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("owner_kind", sa.String(), nullable=False),
        sa.Column("owner_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["uri"], ["local_result_artifacts.uri"]),
        sa.PrimaryKeyConstraint("uri", "owner_kind", "owner_key"),
    )
    op.create_index(
        "ix_local_result_references_owner", "local_result_references",
        ["owner_kind", "owner_key"])


def downgrade() -> None:
    op.drop_index("ix_local_result_references_owner", table_name="local_result_references")
    op.drop_table("local_result_references")
    op.drop_index("ix_local_result_artifacts_writer", table_name="local_result_artifacts")
    op.drop_index("ix_local_result_artifacts_reclaim", table_name="local_result_artifacts")
    op.drop_table("local_result_artifacts")
    op.drop_table("local_result_registry")
