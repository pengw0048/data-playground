"""First-class catalog folder entities.

Revision ID: 0027_catalog_folders
Revises: 0026_creds
Create Date: 2026-07-14
"""

from __future__ import annotations

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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT DISTINCT folder FROM catalog_entries WHERE folder != '' ORDER BY folder"),
    ).fetchall()
    seen: set[str] = set()
    for (folder,) in rows:
        f = (folder or "").strip("/")
        if not f:
            continue
        parts = f.split("/")
        for i in range(1, len(parts) + 1):
            path = "/".join(parts[:i])
            if path in seen:
                continue
            seen.add(path)
            exists = conn.execute(
                sa.text("SELECT 1 FROM catalog_folders WHERE path = :path"), {"path": path},
            ).fetchone()
            if not exists:
                conn.execute(sa.text("INSERT INTO catalog_folders (path) VALUES (:path)"), {"path": path})


def downgrade() -> None:
    op.drop_table("catalog_folders")
