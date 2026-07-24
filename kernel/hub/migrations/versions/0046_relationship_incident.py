"""Index declared relationship endpoints for bounded incident reads.

Revision ID: 0046_relationship_incident
Revises: 0045_canvas_dataset_add_replays
"""

import json

import sqlalchemy as sa
from alembic import op


revision = "0046_relationship_incident"
down_revision = "0045_canvas_dataset_add_replays"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_relationship_endpoints",
        sa.Column("rel_key", sa.String(), nullable=False),
        sa.Column("catalog_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["rel_key"], ["catalog_relationships.rel_key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rel_key", "catalog_key"),
    )
    op.create_index("ix_catalog_relationship_endpoints_catalog_key",
                    "catalog_relationship_endpoints", ["catalog_key"])
    # Existing documents were already normalized to catalog keys at write time. Backfill only valid
    # endpoints; malformed historical documents remain listable but cannot make discovery scan them.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT rel_key, doc FROM catalog_relationships")).mappings()
    endpoint_rows: list[dict[str, str]] = []
    for row in rows:
        try:
            doc = json.loads(row["doc"])
            keys = {str(doc[name]) for name in ("leftUri", "left_uri", "rightUri", "right_uri")
                    if doc.get(name)}
        except (TypeError, ValueError):
            continue
        endpoint_rows.extend({"rel_key": row["rel_key"], "catalog_key": key} for key in keys)
    if endpoint_rows:
        bind.execute(sa.text(
            "INSERT INTO catalog_relationship_endpoints (rel_key, catalog_key) "
            "VALUES (:rel_key, :catalog_key)"), endpoint_rows)


def downgrade() -> None:
    op.drop_index("ix_catalog_relationship_endpoints_catalog_key",
                  table_name="catalog_relationship_endpoints")
    op.drop_table("catalog_relationship_endpoints")
