"""Add deterministic Unicode keys for bounded Transform library queries.

Revision ID: 0025_transform_library_keys
Revises: 0024_promoted_transforms
"""

import unicodedata

import sqlalchemy as sa
from alembic import op


revision = "0025_transform_library_keys"
down_revision = "0024_promoted_transforms"
branch_labels = None
depends_on = None


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _sort_key(title: object) -> str:
    return _text(title).encode("utf-8").hex()


def upgrade() -> None:
    op.add_column(
        "promoted_transforms",
        sa.Column("library_sort_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "promoted_transform_versions",
        sa.Column("library_search_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "promoted_transform_versions",
        sa.Column("library_category_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "promoted_transform_versions",
        sa.Column("library_mode_key", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT transform_id, version, title, blurb, category, mode "
        "FROM promoted_transform_versions ORDER BY transform_id, version"
    )).mappings().all()
    identity_sort_keys: dict[str, str] = {}
    for row in rows:
        identity_sort_keys.setdefault(row["transform_id"], _sort_key(row["title"]))
        connection.execute(sa.text(
            "UPDATE promoted_transform_versions "
            "SET library_search_text = :search_text, "
            "library_category_key = :category_key, library_mode_key = :mode_key "
            "WHERE transform_id = :transform_id AND version = :version"
        ), {
            "transform_id": row["transform_id"],
            "version": row["version"],
            "search_text": "\n".join(_text(row[field]) for field in (
                "title", "blurb", "category", "mode", "transform_id")),
            "category_key": _text(row["category"]),
            "mode_key": _text(row["mode"]),
        })
    for transform_id, sort_key in identity_sort_keys.items():
        connection.execute(sa.text(
            "UPDATE promoted_transforms SET library_sort_key = :sort_key WHERE id = :transform_id"
        ), {"transform_id": transform_id, "sort_key": sort_key})
    # Normal promotion commits identity and v1 together. Preserve an operable key for any legacy
    # orphan instead of making the migration impossible; a later first version never rewrites it.
    orphaned = connection.execute(sa.text(
        "SELECT id, key FROM promoted_transforms WHERE library_sort_key IS NULL"
    )).mappings().all()
    for row in orphaned:
        connection.execute(sa.text(
            "UPDATE promoted_transforms SET library_sort_key = :sort_key WHERE id = :transform_id"
        ), {"transform_id": row["id"], "sort_key": _sort_key(row["key"])})
    with op.batch_alter_table("promoted_transforms") as batch:
        batch.alter_column("library_sort_key", existing_type=sa.Text(), nullable=False)
    with op.batch_alter_table("promoted_transform_versions") as batch:
        batch.alter_column("library_search_text", existing_type=sa.Text(), nullable=False)
        batch.alter_column("library_category_key", existing_type=sa.Text(), nullable=False)
        batch.alter_column("library_mode_key", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("promoted_transform_versions") as batch:
        batch.drop_column("library_mode_key")
        batch.drop_column("library_category_key")
        batch.drop_column("library_search_text")
    with op.batch_alter_table("promoted_transforms") as batch:
        batch.drop_column("library_sort_key")
