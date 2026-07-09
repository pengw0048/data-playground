"""Node-authoring SDK — what a plugin pack imports to add a typed node.

A plugin's `register(reg)` calls `reg.add_node(spec, build)`. `spec` is a NodeSpec (typed ports
+ params, rendered generically by the SPA — no frontend code needed). `build(engine, node,
inputs) -> relation` contributes one step to the logical plan; use the `ctx` helpers to build it
from DuckDB SQL, a Polars transform, or an Arrow-batch UDF — all out-of-core, runner-portable.

Example plugin (`plugins/mypack/__init__.py`):

    from hub.sdk import NodeSpec, PortSpec, ParamSpec, ctx

    SPEC = NodeSpec(kind="upper", title="uppercase", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="name")])

    def build(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "name")
        # NOTE: {{input}} in an f-string emits the literal {input} token that ctx.sql substitutes with
        # the input view name. A bare {input} would be interpolated by Python to the builtin `input`.
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM {{input}}')

    def register(reg):
        reg.add_node(SPEC, build)
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar

import pyarrow as pa

from hub import db
from hub.nodespecs import NodeSpec, ParamSpec, PortSpec, WireType  # re-export

__all__ = ["NodeSpec", "ParamSpec", "PortSpec", "WireType", "ctx", "close_resources"]

_T = TypeVar("_T")
_RESOURCES: dict[str, object] = {}   # process-global warm handles, kept alive across batches AND runs
_RESOURCE_LOCK = threading.RLock()   # REENTRANT: a factory may itself call ctx.resource() for another key
_MISSING = object()                  # sentinel so a factory returning None is still cached (not rebuilt)


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

    def resource(self, key: str, factory: Callable[[], _T]) -> _T:
        """A WARM resource handle: an expensive-to-construct object built ONCE by `factory()` and reused
        across batches AND across runs on the same (warm) per-canvas kernel — a loaded model, a media
        decoder, a DB connection pool, a GPU context. Without this, a `build()` that constructs such a
        thing per batch/run pays the cost every time (the exact fragility distributed media pipelines hit).

        Keyed by `key`; NAMESPACE it (e.g. f"{pack}:{model_id}") so two plugins can't collide. Thread-safe,
        constructed at most once. For PLUGIN nodes (trusted, run in the hub process) — NOT for the sandboxed
        `transform` cell. If the object holds an OS/GPU handle, give it a `close()`/`__exit__` and the kernel
        releases it on graceful shutdown (see close_resources); a hard kill relies on the OS to reclaim."""
        r = _RESOURCES.get(key, _MISSING)
        if r is _MISSING:
            with _RESOURCE_LOCK:  # reentrant → a factory that calls ctx.resource() for another key won't deadlock
                r = _RESOURCES.get(key, _MISSING)
                if r is _MISSING:
                    r = factory()  # cache even a None result, so "constructed at most once" holds
                    _RESOURCES[key] = r
        return r


def close_resources() -> None:
    """Release warm resources that expose close()/__exit__ (called on graceful kernel shutdown). A broken
    close never blocks teardown. A hard SIGKILL skips this — the OS reclaims the process's handles."""
    with _RESOURCE_LOCK:
        items = list(_RESOURCES.items())
        _RESOURCES.clear()
    for key, r in items:
        closer = getattr(r, "close", None) or getattr(r, "__exit__", None)
        if not callable(closer):
            continue
        try:
            closer() if getattr(r, "close", None) else closer(None, None, None)
        except Exception:  # noqa: BLE001
            logging.getLogger("hub").warning("warm resource %s failed to close", key, exc_info=True)


ctx = _Ctx()
