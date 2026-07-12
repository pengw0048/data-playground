"""shared object-attempt ownership and GC registry

Revision ID: 0019_object_attempts
Revises: 0018_run_result_links
Create Date: 2026-07-12
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "0019_object_attempts"
down_revision = "0018_run_result_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installation_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_installation_identity_singleton"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_token", name="uq_installation_identity_owner_token"),
    )
    op.bulk_insert(
        sa.table(
            "installation_identity",
            sa.column("id", sa.Integer()),
            sa.column("owner_token", sa.String()),
        ),
        [{"id": 1, "owner_token": uuid.uuid4().hex}],
    )

    op.create_table(
        "object_attempts",
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("logical_uri", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="writing"),
        sa.Column("reference_key", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gc_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('region', 'sink')", name="ck_object_attempt_kind"),
        sa.CheckConstraint(
            "state IN ('writing', 'published', 'retiring', 'retired', 'discarding')",
            name="ck_object_attempt_state",
        ),
        sa.PrimaryKeyConstraint("uri"),
    )
    op.create_index("ix_object_attempts_logical_uri", "object_attempts", ["logical_uri"])
    op.create_index("ix_object_attempts_kind", "object_attempts", ["kind"])
    op.create_index("ix_object_attempts_run_id", "object_attempts", ["run_id"])
    op.create_index("ix_object_attempts_state", "object_attempts", ["state"])
    op.create_index("ix_object_attempts_reference_key", "object_attempts", ["reference_key"])
    op.create_index(
        "ix_object_attempts_gc", "object_attempts",
        ["state", "gc_attempted_at", "retired_at", "created_at", "uri"],
    )
    op.create_index(
        "ix_object_attempts_sink_target", "object_attempts", ["kind", "logical_uri", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_object_attempts_sink_target", table_name="object_attempts")
    op.drop_index("ix_object_attempts_gc", table_name="object_attempts")
    op.drop_index("ix_object_attempts_reference_key", table_name="object_attempts")
    op.drop_index("ix_object_attempts_state", table_name="object_attempts")
    op.drop_index("ix_object_attempts_run_id", table_name="object_attempts")
    op.drop_index("ix_object_attempts_kind", table_name="object_attempts")
    op.drop_index("ix_object_attempts_logical_uri", table_name="object_attempts")
    op.drop_table("object_attempts")
    op.drop_table("installation_identity")
