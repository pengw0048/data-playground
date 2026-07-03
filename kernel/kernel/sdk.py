"""Node-authoring SDK (PRD §8.1) — what a plugin pack imports to add a typed node.

A plugin's `register(reg)` calls `reg.add_node(spec, lower)`. `spec` is a NodeSpec (typed ports
+ params, rendered generically by the SPA — no frontend code needed). `lower(engine, node,
inputs) -> relation` contributes one step to the logical plan; use the `ctx` helpers to build it
from DuckDB SQL, a Polars transform, or an Arrow-batch UDF — all out-of-core, runner-portable.

Example plugin (`plugins/mypack/__init__.py`):

    from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx

    SPEC = NodeSpec(kind="upper", title="uppercase", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="name")])

    def lower(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "name")
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM {input}')

    def register(reg):
        reg.add_node(SPEC, lower)
"""

from __future__ import annotations

from typing import Callable

import pyarrow as pa

from kernel import db
from kernel.nodespecs import NodeSpec, ParamSpec, PortSpec, WireType  # re-export

__all__ = ["NodeSpec", "ParamSpec", "PortSpec", "WireType", "ctx"]


class _Ctx:
    """Safe builders that turn relations into relations without forcing materialization."""

    def sql(self, rel, query: str):
        """Run SQL over `rel`, referenced as the placeholder token ``{input}``. Returns a relation.

        (``{input}`` can't occur in valid SQL, so unlike a bare ``_`` it never rewrites an
        underscore inside a string literal or LIKE pattern.)
        """
        name = db.unique_view("sdk")  # process-globally-unique + tracked for cleanup
        rel.create_view(name, replace=True)
        return db.conn().sql(query.replace("{input}", name))

    def arrow_map(self, rel, fn: Callable[["pa.RecordBatch"], "pa.RecordBatch | list[dict]"]):
        """Apply a Python fn over Arrow batches (the escape hatch). Returns a relation."""
        rows: list[dict] = []
        for batch in rel.to_arrow_reader(batch_size=2048):
            out = fn(batch)
            if isinstance(out, pa.RecordBatch):
                rows.extend(out.to_pylist())
            else:
                rows.extend(out)
        table = pa.Table.from_pylist(rows) if rows else rel.limit(0).to_arrow_table()
        return db.conn().from_arrow(table)

    def polars(self, rel, fn):
        """Apply a Polars transform: fn(polars.DataFrame) -> polars.DataFrame. Returns a relation."""
        import polars as pl  # noqa: F401
        out = fn(rel.pl())
        return db.conn().from_arrow(out.to_arrow())


ctx = _Ctx()
