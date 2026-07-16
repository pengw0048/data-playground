"""Repair databases upgraded through the drifted pre-1.0 baseline.

Revision ID: 0003_repair_historical_metadata
Revises: 0002_managed_file_revs
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_repair_historical_metadata"
down_revision = "0002_managed_file_revs"
branch_labels = None
depends_on = None


def _create_workspace_containers() -> None:
    op.create_table(
        "workspace_containers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("is_root", sa.Boolean(), server_default="0", nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_workspace_container_ordinal"),
        sa.CheckConstraint("version >= 1", name="ck_workspace_container_version"),
        sa.CheckConstraint("is_root = false OR parent_id IS NULL", name="ck_workspace_container_root"),
        sa.ForeignKeyConstraint(["parent_id"], ["workspace_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_id", "name", name="uq_workspace_container_parent_name"),
    )


def _create_workspace_placements() -> None:
    op.create_table(
        "workspace_placements",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("container_id", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.CheckConstraint("target_kind IN ('canvas', 'dataset')", name="ck_workspace_placement_kind"),
        sa.CheckConstraint("ordinal >= 0", name="ck_workspace_placement_ordinal"),
        sa.CheckConstraint("version >= 1", name="ck_workspace_placement_version"),
        sa.ForeignKeyConstraint(["container_id"], ["workspace_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_kind", "target_id", name="uq_workspace_placement_target"),
    )


def _create_index_if_missing(inspector, table_name: str, index_name: str, columns: list[str]) -> None:
    if index_name not in {index["name"] for index in inspector.get_indexes(table_name)}:
        op.create_index(index_name, table_name, columns, unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "workspace_containers" not in tables:
        _create_workspace_containers()
        tables.add("workspace_containers")
    if "workspace_placements" not in tables:
        _create_workspace_placements()
        tables.add("workspace_placements")

    # Refresh after DDL so the inspector also sees newly-created indexes and columns.
    inspector = sa.inspect(bind)
    _create_index_if_missing(
        inspector,
        "workspace_containers",
        "ix_workspace_containers_parent_id",
        ["parent_id"],
    )
    _create_index_if_missing(
        inspector,
        "workspace_placements",
        "ix_workspace_placements_container_id",
        ["container_id"],
    )

    columns = {column["name"] for column in inspector.get_columns("run_records")}
    if "profile" not in columns:
        op.add_column("run_records", sa.Column("profile", sa.Text(), nullable=True))

    bind.execute(sa.text("""
        INSERT INTO workspace_containers (id, parent_id, name, ordinal, version, is_root)
        SELECT :id, NULL, :name, 0, 1, :is_root
        WHERE NOT EXISTS (
            SELECT 1 FROM workspace_containers WHERE id = :id
        )
    """), {"id": "workspace-local-root", "name": "Workspace", "is_root": True})


def downgrade() -> None:
    # This is a non-destructive repair for a historical migration drift. Leaving repaired objects in
    # place keeps a downgraded database usable rather than recreating the bad historical shape.
    pass
