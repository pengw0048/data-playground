"""Certified identity_projection_v1: Arrow column pass-through over a row range.

Never compiles SQL expressions, runs Transform/plugin code, or mutates columns.
Physical row order is preserved; schema is the checkpoint Arrow schema.
"""

from __future__ import annotations

import hashlib
import io
import json
import os

import pyarrow as pa
import pyarrow.parquet as pq


OPERATION_ID = "identity_projection_v1"


def validate_identity_select_config(cfg: dict) -> None:
    """Accept only exact ``{\"select\": \"*\"}``; reject extras and non-star selects."""
    if not isinstance(cfg, dict) or cfg != {"select": "*"}:
        raise ValueError("identity projection requires exact select config {\"select\":\"*\"}")


def _schema_sha256(schema: pa.Schema) -> str:
    columns = [{"name": field.name, "type": str(field.type)} for field in schema]
    schema_json = json.dumps(
        columns, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(schema_json.encode()).hexdigest()


def project_range_from_guard(guard, start: int, end: int) -> bytes:
    """Slice ``[start, end)`` from a held checkpoint read guard into parquet bytes.

    Columns pass through unchanged. Empty ranges write an empty table with the
    checkpoint Arrow schema. Out-of-bounds ranges are rejected; the sole empty-file
    exception is exact ``[0, 0)`` when the checkpoint has zero rows.
    """
    if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
        raise ValueError("projection range bounds must be integers")
    if start < 0 or end < 0 or start > end:
        raise ValueError("projection range is invalid")

    fd = os.dup(guard.artifact_fileno())
    reader = None
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        reader = os.fdopen(fd, "rb")
        fd = -1
        parquet = pq.ParquetFile(reader)
        total = int(parquet.metadata.num_rows)
        schema = parquet.schema_arrow
        if total == 0:
            if (start, end) != (0, 0):
                raise ValueError("projection range out of bounds")
            table = schema.empty_table()
        else:
            if end > total or start > total:
                raise ValueError("projection range out of bounds")
            if start == end:
                table = schema.empty_table()
            else:
                # Physical order: full table then slice; no expression compile.
                table = parquet.read().slice(start, end - start)
        sink = io.BytesIO()
        pq.write_table(table, sink)
        guard.check()
        return sink.getvalue()
    finally:
        if reader is not None:
            reader.close()
        elif fd >= 0:
            os.close(fd)


def concat_parquet_in_order(
        parts: list[bytes], *, expected_schema_sha256: str | None = None) -> bytes:
    """Concatenate projected parquet parts in partition order into one table.

    All parts must share the checkpoint schema. An empty parts list is rejected;
    callers that need a zero-row gather should pass one empty schema-preserving part.
    """
    if not parts:
        raise ValueError("gather requires at least one projected parquet part")
    tables: list[pa.Table] = []
    schema: pa.Schema | None = None
    for part in parts:
        table = pq.read_table(io.BytesIO(part))
        if schema is None:
            schema = table.schema
        elif not table.schema.equals(schema):
            raise ValueError("gather parts disagree on Arrow schema")
        tables.append(table)
    assert schema is not None
    if expected_schema_sha256 is not None:
        digest = _schema_sha256(schema)
        if digest != str(expected_schema_sha256):
            raise ValueError("gather schema does not match the checkpoint digest")
    combined = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
    sink = io.BytesIO()
    pq.write_table(combined, sink)
    return sink.getvalue()
