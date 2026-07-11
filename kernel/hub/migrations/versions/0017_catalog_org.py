"""catalog organization + discovery at scale: folder/owner/description/tags/columns/usage +
lineage columns + semantic embeddings

Promotes the filterable/sortable fields out of the catalog_entries JSON `doc` into indexed columns
(tbl_id, folder, owner, description, row_count, usage) and adds join tables (catalog_tags,
catalog_columns) + a semantic index (catalog_embeddings). This is what lets browse / search / facet
push down to the DB — the catalog answers a filtered page with an indexed query instead of loading
every row into memory (the old model that didn't scale past a few hundred tables). Backfills the new
columns + join rows from each existing entry's stored doc so upgraded installs are immediately
searchable. Also adds a column-level `column` to catalog_edges for column-level lineage.

Revision ID: 0017_catalog_org
Revises: 0016_run_state_owner
Create Date: 2026-07-11
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0017_catalog_org"
down_revision = "0016_run_state_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalog_entries", sa.Column("tbl_id", sa.String(), nullable=True))
    op.add_column("catalog_entries", sa.Column("folder", sa.String(), nullable=False, server_default=""))
    op.add_column("catalog_entries", sa.Column("owner", sa.String(), nullable=True))
    op.add_column("catalog_entries", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("catalog_entries", sa.Column("row_count", sa.Integer(), nullable=True))
    op.add_column("catalog_entries", sa.Column("usage", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_catalog_entries_tbl_id", "catalog_entries", ["tbl_id"])
    op.create_index("ix_catalog_entries_folder", "catalog_entries", ["folder"])
    op.create_index("ix_catalog_entries_owner", "catalog_entries", ["owner"])

    op.add_column("catalog_edges", sa.Column("column", sa.String(), nullable=True))

    op.create_table(
        "catalog_tags",
        sa.Column("uri", sa.String(), primary_key=True),
        sa.Column("tag", sa.String(), primary_key=True),
    )
    op.create_index("ix_catalog_tags_tag", "catalog_tags", ["tag"])

    op.create_table(
        "catalog_columns",
        sa.Column("uri", sa.String(), primary_key=True),
        sa.Column("column", sa.String(), primary_key=True),
    )
    op.create_index("ix_catalog_columns_column", "catalog_columns", ["column"])

    op.create_table(
        "catalog_embeddings",
        sa.Column("uri", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vec", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    _backfill_from_docs()


def _backfill_from_docs() -> None:
    """Populate the new columns + tag/column join rows from each existing entry's `doc` JSON, so an
    upgraded catalog is immediately filterable/searchable without a re-register."""
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT uri, doc FROM catalog_entries")).fetchall()
    tags_ins = sa.text("INSERT INTO catalog_tags (uri, tag) VALUES (:uri, :tag)")
    cols_ins = sa.text("INSERT INTO catalog_columns (uri, column) VALUES (:uri, :col)")
    upd = sa.text("UPDATE catalog_entries SET tbl_id=:tid, folder=:folder, owner=:owner, "
                  "description=:descr, row_count=:rows WHERE uri=:uri")
    for uri, doc_json in rows:
        try:
            doc = json.loads(doc_json) if doc_json else {}
        except (ValueError, TypeError):
            doc = {}
        rows_val = doc.get("rowCount")
        if rows_val is None:
            rows_val = doc.get("row_count")
        conn.execute(upd, {
            "tid": doc.get("id"), "folder": (doc.get("folder") or "").strip("/"),
            "owner": doc.get("owner") or None, "descr": doc.get("description") or None,
            "rows": rows_val, "uri": uri,
        })
        seen_t = set()
        for t in doc.get("tags") or []:
            t = str(t).strip()
            if t and t not in seen_t:
                seen_t.add(t)
                conn.execute(tags_ins, {"uri": uri, "tag": t})
        seen_c = set()
        for c in doc.get("columns") or []:
            name = c.get("name") if isinstance(c, dict) else None
            if name and name not in seen_c:
                seen_c.add(name)
                conn.execute(cols_ins, {"uri": uri, "col": name})


def downgrade() -> None:
    op.drop_table("catalog_embeddings")
    op.drop_index("ix_catalog_columns_column", table_name="catalog_columns")
    op.drop_table("catalog_columns")
    op.drop_index("ix_catalog_tags_tag", table_name="catalog_tags")
    op.drop_table("catalog_tags")
    op.drop_column("catalog_edges", "column")
    op.drop_index("ix_catalog_entries_owner", table_name="catalog_entries")
    op.drop_index("ix_catalog_entries_folder", table_name="catalog_entries")
    op.drop_index("ix_catalog_entries_tbl_id", table_name="catalog_entries")
    for col in ("usage", "row_count", "description", "owner", "folder", "tbl_id"):
        op.drop_column("catalog_entries", col)
