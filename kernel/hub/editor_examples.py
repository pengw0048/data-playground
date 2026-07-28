"""Request-local JSON fixtures for fullscreen Transform code tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pyarrow as pa

from hub import db
from hub.plugins.adapters import relation_columns
from hub.sqlpolicy import identifier, quote_identifier

EDITOR_EXAMPLE_MAX_ROWS = 20
EDITOR_EXAMPLE_MAX_BYTES = 16 * 1024


def parse_editor_example_rows(value: str) -> tuple[list[dict[str, Any]], pa.Table]:
    """Validate one bounded JSON fixture and materialize its in-memory Arrow schema."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Example rows must use valid Unicode") from exc
    if len(encoded) > EDITOR_EXAMPLE_MAX_BYTES:
        raise ValueError(
            f"Example rows must be at most {EDITOR_EXAMPLE_MAX_BYTES} UTF-8 bytes")
    try:
        document = json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number '{token}' is not supported")),
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"Example rows must be valid JSON: {exc}") from exc
    if not isinstance(document, list):
        raise ValueError("Example rows must be a JSON array of objects")
    if not document:
        raise ValueError("Example rows must contain at least one row")
    if len(document) > EDITOR_EXAMPLE_MAX_ROWS:
        raise ValueError(
            f"Example rows may contain at most {EDITOR_EXAMPLE_MAX_ROWS} rows")
    if any(not isinstance(row, dict) for row in document):
        raise ValueError("Every example row must be a JSON object")
    first_shape = set(document[0])
    if not first_shape:
        raise ValueError("Example rows must contain at least one field")
    if any(set(row) != first_shape for row in document[1:]):
        raise ValueError("Every example row must use the same fields")
    try:
        table = pa.Table.from_pylist(document)
    except (pa.ArrowException, OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            "Example row fields must use consistent Arrow-compatible JSON value types") from exc
    return document, table


class EditorExampleRowsAdapter:
    """One request-owned Arrow table exposed through the normal bounded preview adapter seam."""

    name = "editor-example-rows"

    def __init__(self, uri: str, table: pa.Table, fixture_json: str):
        self.uri = uri
        self.table = table
        self._fingerprint = hashlib.sha256(fixture_json.encode("utf-8")).hexdigest()[:16]

    def _relation(self, uri: str):
        if uri != self.uri:
            raise ValueError("example-row adapter received an unrelated URI")
        return db.conn().from_arrow(self.table)

    def preview_scan(
            self, uri: str, columns: list[str] | None = None,
            limit: int = 2000, options: dict | None = None):
        del options
        relation = self._relation(uri)
        if columns:
            selected = [
                identifier(column, relation.columns, label="projection column")
                for column in columns
            ]
            relation = relation.project(
                ", ".join(quote_identifier(column) for column in selected))
        return relation.limit(int(limit))

    def schema(self, uri: str):
        return relation_columns(self._relation(uri).limit(0))

    def fingerprint(self, uri: str) -> str:
        if uri != self.uri:
            raise ValueError("example-row adapter received an unrelated URI")
        return f"editor-example:{self._fingerprint}"
