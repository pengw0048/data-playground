"""Bounded editor evidence from the Python container passed to a Transform."""

from __future__ import annotations

import reprlib
from typing import Any, Literal

import pyarrow as pa

from hub.models import EditorInputCell, EditorInputSample

ROW_LIMIT = 5
COLUMN_LIMIT = 20
CELL_LIMIT = 300


def python_type(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


class _BoundedRepr(reprlib.Repr):
    def __init__(self):
        super().__init__()
        self.maxlevel = 3
        self.maxstring = self.maxother = CELL_LIMIT
        self.truncated = False

    def _clip(self, text: str) -> str:
        if len(text) <= CELL_LIMIT:
            return text
        self.truncated = True
        return text[:CELL_LIMIT - 1] + "…"

    def repr1(self, value: Any, level: int) -> str:
        if isinstance(value, pa.Scalar):
            return self._arrow_scalar(value)
        # NumPy string/bytes scalars are subclasses of the Python types, but reprlib dispatches
        # by exact class name. Bound their payload before reaching its generic instance handler.
        if isinstance(value, str):
            return self.repr_str(value, level)
        if isinstance(value, bytes):
            return self.repr_bytes(value, level)
        if type(value).__module__.startswith("numpy"):
            return self._numpy_value(value)
        # reprlib bounds nested containers before traversing their elements. Track that loss
        # explicitly instead of presenting its ellipsis as an exact value.
        if isinstance(value, (list, tuple, dict, set, frozenset)):
            maximum = getattr(self, f"max{type(value).__name__}")
            if len(value) > maximum or (level <= 0 and len(value)):
                self.truncated = True
        return self._clip(super().repr1(value, level))

    def repr_str(self, value: str, level: int) -> str:
        # Avoid constructing a huge escaped representation merely to truncate it afterwards.
        if len(value) > CELL_LIMIT:
            self.truncated = True
        return self._clip(repr(value[:CELL_LIMIT]))

    def repr_bytes(self, value: bytes, level: int) -> str:
        if len(value) > CELL_LIMIT:
            self.truncated = True
        return self._clip(repr(value[:CELL_LIMIT]))

    def repr_instance(self, value: Any, level: int) -> str:
        return self._clip(repr(value))

    def _arrow_scalar(self, value: pa.Scalar) -> str:
        label = f"pyarrow.{type(value).__name__}"
        if not value.is_valid:
            return f"<{label}: None>"
        if hasattr(value, "as_buffer"):
            # String/binary scalars expose a zero-copy buffer. Never call their repr/as_py on
            # the whole payload: an input cell may contain many megabytes.
            buffer = value.as_buffer()
            prefix = memoryview(buffer)[:CELL_LIMIT].tobytes()
            is_string = (pa.types.is_string(value.type) or pa.types.is_large_string(value.type)
                         or pa.types.is_string_view(value.type))
            # A byte cap can end within a UTF-8 code point; omit that incomplete character.
            fragment = prefix.decode("utf-8", errors="ignore") if is_string else prefix
            self.truncated = self.truncated or len(buffer) > CELL_LIMIT
            return self._clip(f"<{label}: {repr(fragment)}>")
        if (pa.types.is_boolean(value.type) or pa.types.is_integer(value.type)
                or pa.types.is_floating(value.type) or pa.types.is_decimal(value.type)
                or pa.types.is_temporal(value.type)):
            return self._clip(repr(value))
        # Nested/extension values are evidence of their container type, not an invitation to
        # traverse an arbitrarily large list, struct, map, or user-defined extension payload.
        self.truncated = True
        return self._clip(f"<{label}: value omitted>")

    def _numpy_value(self, value: Any) -> str:
        import numpy as np  # already present when pandas supplied a NumPy value

        if isinstance(value, np.ndarray):
            # NumPy's own repr can omit elements even when its final string is short; it can
            # also traverse everything under a caller's print options. Use metadata only.
            self.truncated = True
            return self._clip(
                f"<{python_type(value)} shape={value.shape}, dtype={value.dtype.name}; values omitted>")
        if isinstance(value, np.generic) and value.dtype.kind in "biufcmM":
            return self._clip(repr(value))  # fixed-size numeric/date/time scalars
        self.truncated = True
        return self._clip(f"<{python_type(value)}: value omitted>")


def _cell(value: Any) -> EditorInputCell:
    formatter = _BoundedRepr()
    representation = formatter.repr(value)
    return EditorInputCell(
        python_type=python_type(value), representation=representation,
        truncated=formatter.truncated,
    )


def batch_input(table, fmt: str):
    """Use the same conversion for evidence and the subsequent UDF invocation."""
    if fmt == "arrow":
        return table
    if fmt == "pandas":
        return table.to_pandas()
    return table.to_pylist()


def input_sample(value: Any, fmt: Literal["rows", "pandas", "arrow"],
                 columns: list[str], *, mode: str) -> EditorInputSample:
    selected = columns[:COLUMN_LIMIT]
    if fmt == "rows":
        rows = [{name: _cell(row[name]) for name in selected} for row in value[:ROW_LIMIT]]
        # Row operators receive a dict copy; map_batches receives the list itself.
        container_type = "builtins.list" if mode == "map_batches" else "builtins.dict"
    elif fmt == "pandas":
        # iat preserves actual per-column scalar values. iterrows/to_dict can coerce nullable
        # integer values or mix column dtypes, which would misdescribe the user's argument.
        rows = [
            {name: _cell(value.iat[row, column]) for column, name in enumerate(selected)}
            for row in range(min(ROW_LIMIT, len(value)))
        ]
        container_type = python_type(value)
    else:
        rows = [
            {name: _cell(value.column(column)[row]) for column, name in enumerate(selected)}
            for row in range(min(ROW_LIMIT, value.num_rows))
        ]
        container_type = python_type(value)
    return EditorInputSample(
        format=fmt, container_type=container_type, columns=selected,
        column_count=len(columns), rows=rows,
    )
