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
        "promoted_transform_versions",
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
        "FROM promoted_transform_versions"
    )).mappings().all()
    for row in rows:
        connection.execute(sa.text(
            "UPDATE promoted_transform_versions "
            "SET library_sort_key = :sort_key, library_search_text = :search_text, "
            "library_category_key = :category_key, library_mode_key = :mode_key "
            "WHERE transform_id = :transform_id AND version = :version"
        ), {
            "transform_id": row["transform_id"],
            "version": row["version"],
            "sort_key": _sort_key(row["title"]),
            "search_text": "\n".join(_text(row[field]) for field in (
                "title", "blurb", "category", "mode", "transform_id")),
            "category_key": _text(row["category"]),
            "mode_key": _text(row["mode"]),
        })
    with op.batch_alter_table("promoted_transform_versions") as batch:
        batch.alter_column("library_sort_key", existing_type=sa.Text(), nullable=False)
        batch.alter_column("library_search_text", existing_type=sa.Text(), nullable=False)
        batch.alter_column("library_category_key", existing_type=sa.Text(), nullable=False)
        batch.alter_column("library_mode_key", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("promoted_transform_versions") as batch:
        batch.drop_column("library_mode_key")
        batch.drop_column("library_category_key")
        batch.drop_column("library_search_text")
        batch.drop_column("library_sort_key")
