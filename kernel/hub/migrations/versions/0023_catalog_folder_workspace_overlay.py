"""Project built-in Catalog folders into stable local Workspace overlays.

Revision ID: 0023_catalog_folder_overlay
Revises: 0022_task_manifests
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "0023_catalog_folder_overlay"
down_revision = "0022_task_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalog_folders", sa.Column("id", sa.String(length=32), nullable=True))
    op.add_column("workspace_containers", sa.Column("catalog_folder_id", sa.String(length=32), nullable=True))
    op.add_column("workspace_containers", sa.Column("catalog_folder_state", sa.String(length=16), nullable=True))
    op.add_column("workspace_containers", sa.Column("catalog_folder_path", sa.String(), nullable=True))
    connection = op.get_bind()
    folders = connection.execute(sa.text(
        "SELECT path FROM catalog_folders ORDER BY path"
    )).mappings().all()
    for folder in folders:
        connection.execute(sa.text(
            "UPDATE catalog_folders SET id = :id WHERE path = :path"
        ), {"id": uuid.uuid4().hex, "path": folder["path"]})
    with op.batch_alter_table("catalog_folders") as batch:
        batch.alter_column("id", existing_type=sa.String(length=32), nullable=False)
    op.create_index("ix_catalog_folders_id", "catalog_folders", ["id"], unique=True)
    op.create_index("ix_workspace_containers_catalog_folder_id", "workspace_containers", ["catalog_folder_id"], unique=True)
    # A Catalog projection and a separately owned local overlay can share a display name.  The
    # writer paths still preserve local-overlay sibling uniqueness explicitly.
    with op.batch_alter_table("workspace_containers") as batch:
        batch.drop_constraint("uq_workspace_container_parent_name", type_="unique")
    op.create_index(
        "uq_workspace_local_container_parent_name", "workspace_containers", ["parent_id", "name"],
        unique=True, postgresql_where=sa.text("catalog_folder_id IS NULL"),
        sqlite_where=sa.text("catalog_folder_id IS NULL"),
    )

    rows = connection.execute(sa.text(
        "SELECT path, id FROM catalog_folders"
    )).mappings().all()
    by_path = {row["path"]: row["id"] for row in rows}
    projection_ids: dict[str, str] = {}
    for path in sorted(by_path, key=lambda value: (value.count("/"), value)):
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        projection_id = uuid.uuid4().hex
        projection_ids[path] = projection_id
        connection.execute(sa.text(
            "INSERT INTO workspace_containers "
            "(id, parent_id, name, ordinal, version, is_root, catalog_folder_id, catalog_folder_state, catalog_folder_path) "
            "VALUES (:id, :parent_id, :name, 0, 1, 0, :folder_id, 'current', :path)"
        ), {
            "id": projection_id,
            "parent_id": projection_ids.get(parent_path, "workspace-local-root"),
            "name": path.rsplit("/", 1)[-1],
            "folder_id": by_path[path],
            "path": path,
        })
    entries = connection.execute(sa.text(
        "SELECT registration_id, folder FROM catalog_entries WHERE folder IS NOT NULL AND folder != ''"
    )).mappings().all()
    for entry in entries:
        projection_id = projection_ids.get(entry["folder"])
        if projection_id is not None:
            connection.execute(sa.text(
                "UPDATE workspace_placements SET container_id = :container_id "
                "WHERE target_kind = 'dataset' AND target_id = :dataset_id"
            ), {"container_id": projection_id, "dataset_id": entry["registration_id"]})


def downgrade() -> None:
    connection = op.get_bind()
    retained = connection.execute(sa.text(
        "SELECT 1 FROM workspace_placements p JOIN workspace_containers c "
        "ON c.id = p.container_id WHERE c.catalog_folder_id IS NOT NULL "
        "AND p.target_kind != 'dataset' LIMIT 1"
    )).first()
    if retained is not None:
        raise RuntimeError("cannot downgrade while Catalog folder overlays contain placements")
    # Dataset placement is Catalog-owned and existed before this migration at the local root. Move it
    # back there so a database with ordinary registered datasets can round-trip through this revision;
    # only independently owned overlays require an explicit move-out before downgrade.
    connection.execute(sa.text(
        "UPDATE workspace_placements SET container_id = 'workspace-local-root' "
        "WHERE target_kind = 'dataset' AND container_id IN "
        "(SELECT id FROM workspace_containers WHERE catalog_folder_id IS NOT NULL)"
    ))
    connection.execute(sa.text(
        "DELETE FROM workspace_containers WHERE catalog_folder_id IS NOT NULL"
    ))
    with op.batch_alter_table("workspace_containers") as batch:
        batch.create_unique_constraint("uq_workspace_container_parent_name", ["parent_id", "name"])
    op.drop_index("uq_workspace_local_container_parent_name", table_name="workspace_containers")
    op.drop_index("ix_workspace_containers_catalog_folder_id", table_name="workspace_containers")
    op.drop_column("workspace_containers", "catalog_folder_state")
    op.drop_column("workspace_containers", "catalog_folder_id")
    op.drop_column("workspace_containers", "catalog_folder_path")
    op.drop_index("ix_catalog_folders_id", table_name="catalog_folders")
    op.drop_column("catalog_folders", "id")
