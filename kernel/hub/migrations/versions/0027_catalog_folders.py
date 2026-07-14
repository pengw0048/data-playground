"""catalog_folders: folders as first-class entities

Adds a `catalog_folders` table so a folder can exist empty (created up front) and be
renamed/deleted independently of the datasets in it. The folder path STRING on catalog_entries is
retained (the external CatalogProvider namespace mapping depends on it); this entity is ADDITIVE.
Backfills one row per distinct existing entry `folder` string AND each of its ancestor paths, so an
upgraded install shows exactly the folders it already had. Read-only on catalog_entries.

Revision ID: 0027_catalog_folders
Revises: 0025_run_request_id
Create Date: 2026-07-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_catalog_folders"
down_revision = "0026_creds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_folders",
        sa.Column("path", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    _backfill_from_entry_folders()


def _backfill_from_entry_folders() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT DISTINCT folder FROM catalog_entries WHERE folder IS NOT NULL AND folder != ''"
    )).fetchall()
    paths: set[str] = set()
    for (folder,) in rows:
        f = (folder or "").strip("/")
        if not f:
            continue
        segs = f.split("/")
        for i in range(1, len(segs) + 1):
            paths.add("/".join(segs[:i]))
    existing = {r[0] for r in conn.execute(sa.text("SELECT path FROM catalog_folders")).fetchall()}
    ins = sa.text("INSERT INTO catalog_folders (path, created_at) VALUES (:p, CURRENT_TIMESTAMP)")
    for p in sorted(paths - existing):
        conn.execute(ins, {"p": p})


def downgrade() -> None:
    op.drop_table("catalog_folders")
