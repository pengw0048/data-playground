"""Node-authoring SDK — what a plugin pack imports to add a typed node.

A plugin's `register(reg)` calls `reg.add_node(spec, build)`. `spec` is a NodeSpec (typed ports
+ params, rendered generically by the SPA — no frontend code needed). `build(engine, node,
inputs) -> relation` contributes one step to the logical plan; use the `ctx` helpers to build it
from DuckDB SQL, a Polars transform, or an Arrow-batch UDF — all out-of-core, runner-portable.

Example plugin (`plugins/mypack/__init__.py`):

    from hub.sdk import NodeSpec, PortSpec, ParamSpec, ctx, identifier, quote_identifier

    SPEC = NodeSpec(kind="upper", title="uppercase", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="name")])

    def build(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "name")
        column = quote_identifier(identifier(col, inputs[0].columns, label="uppercase column"))
        return ctx.sql(inputs[0], f"SELECT * REPLACE (upper({column}) AS {column}) FROM input")

    def register(reg):
        reg.add_node(SPEC, build)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar

import pyarrow as pa

from hub import db, graph as graph_mod
from hub.nodespecs import NodeSpec, ParamSpec, PortSpec, WireType  # re-export
from hub.sqlpolicy import bind_input_ctes, identifier, quote_identifier, validate_query

__all__ = [
    "NodeSpec", "ParamSpec", "PortSpec", "WireType", "ctx", "identifier", "quote_identifier",
    "close_resources", "DatasetBinding", "ImmediateInput", "ImmediateInputPort",
    "ImmediateInputs", "UnsupportedUpstreamError",
]

_T = TypeVar("_T")
_RESOURCES: dict[str, object] = {}   # process-global warm handles, kept alive across batches AND runs
_RESOURCE_LOCK = threading.RLock()   # REENTRANT: a factory may itself call ctx.resource() for another key
_MISSING = object()                  # sentinel so a factory returning None is still cached (not rebuilt)


@dataclass(frozen=True)
class DatasetBinding:
    """One canonical dataset identity proved for an immediate Source input.

    ``revision_id`` is absent for a current/mutable source.  A missing binding means that the
    source has no single identity the core can prove; plugins must not reconstruct one from a URI,
    name, or another node's configuration.
    """

    dataset_id: str
    revision_id: str | None = None


@dataclass(frozen=True)
class ImmediateInput:
    """The deliberately small public description of one directly wired input."""

    kind: str | None
    dataset: DatasetBinding | None = None


@dataclass(frozen=True)
class ImmediateInputPort:
    """All direct inputs wired to one declared input port, in canvas edge order."""

    id: str
    inputs: tuple[ImmediateInput, ...]

    @property
    def count(self) -> int:
        return len(self.inputs)


@dataclass(frozen=True)
class ImmediateInputs:
    """A bounded snapshot of a builder node's declared input ports."""

    ports: tuple[ImmediateInputPort, ...]

    def port(self, port_id: str) -> ImmediateInputPort:
        for port in self.ports:
            if port.id == port_id:
                return port
        raise KeyError(f"node has no input port {port_id!r}")


class UnsupportedUpstreamError(ValueError):
    """A plugin's documented immediate-input contract is not satisfied."""


def _source_dataset_binding(source) -> DatasetBinding | None:
    """Return only a Source's already-admitted canonical binding, never a guessed identity."""
    data = source.data if isinstance(source.data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return None

    dataset_ref = config.get("datasetRef")
    selected: tuple[str, str] | None = None
    if dataset_ref is not None:
        try:
            from hub.models import dataset_ref_identity
            selected = dataset_ref_identity(dataset_ref)
        except ValueError:
            return None

    bound_id = config.get("_input_dataset_id")
    bound_revision = config.get("_input_revision_id")
    bound = (
        (bound_id, bound_revision)
        if isinstance(bound_id, str) and bound_id and isinstance(bound_revision, str) and bound_revision
        else None
    )
    if selected is not None and bound is not None and selected != bound:
        return None

    uri = config.get("uri")
    if isinstance(uri, str):
        from hub import workspace_providers
        if workspace_providers.is_provider_dataset_uri(uri):
            try:
                canonical_id = workspace_providers.provider_dataset_identity(uri)
            except workspace_providers.ProviderDatasetUnavailable:
                return None
            if ((selected is not None and selected[0] != canonical_id)
                    or (bound is not None and bound[0] != canonical_id)):
                return None
            revision_id = selected[1] if selected is not None else (
                bound[1] if bound is not None else None)
            return DatasetBinding(dataset_id=canonical_id, revision_id=revision_id)

    identity = selected or bound
    return DatasetBinding(*identity) if identity is not None else None


class _Ctx:
    """Safe builders that turn relations into relations without forcing materialization."""

    def sql(self, rel, query: str):
        """Run one validated SELECT over `rel`, referenced by the query-scope CTE ``input``."""
        validated = validate_query(query, 1, con=db.conn())
        name = db.unique_view("sdk")  # process-globally-unique + tracked for cleanup
        rel.create_view(name, replace=True)
        return db.conn().sql(bind_input_ctes(validated, [name]))

    def immediate_inputs(self, engine, node) -> ImmediateInputs:
        """Describe only ``node``'s directly wired inputs, grouped by declared input port.

        The snapshot exposes input counts, producing node kinds, and a canonical Source dataset
        binding when core has already proved one.  It deliberately exposes no graph, node data,
        edges, URI, or transitive ancestry.  Plugins that need a stricter shape should raise
        :class:`UnsupportedUpstreamError` before doing work.
        """
        spec = getattr(engine, "node_specs", {}).get(node.type)
        if spec is None:
            return ImmediateInputs(())
        by_port: dict[str, list[ImmediateInput]] = {port.id: [] for port in spec.inputs}
        nodes = getattr(engine, "_nodes", {})
        graph = getattr(engine, "graph", None)
        generated_refs = (
            set(getattr(graph, "_publication_source_uris", {}))
            | set(getattr(graph, "_controller_generated_source_ids", ()))
        )
        for edge in getattr(graph, "edges", ()):
            if edge.target != node.id:
                continue
            # Reuse structural validation's authoritative default-handle resolver: an omitted
            # target handle selects "in" when declared, otherwise the first declared input port.
            resolved_port = graph_mod._port(node, spec, edge.target_handle, "target")
            port_id = resolved_port[0] if resolved_port is not None else None
            if port_id not in by_port:
                continue
            upstream = nodes.get(edge.source)
            if upstream is None:
                continue
            # A controller-generated cross-region ref is implemented as an execution Source, but it
            # is not the directly wired canvas Source a plugin contract may require. The private,
            # parent-owned publication sidecar is unforgeable through Graph validation and lets this
            # bounded snapshot report no producer kind without exposing the ref URI or graph data.
            generated_ref = upstream.id in generated_refs
            by_port[port_id].append(ImmediateInput(
                kind=None if generated_ref else upstream.type,
                dataset=(
                    _source_dataset_binding(upstream)
                    if upstream.type == "source" and not generated_ref else None
                ),
            ))
        return ImmediateInputs(tuple(
            ImmediateInputPort(id=port.id, inputs=tuple(by_port[port.id]))
            for port in spec.inputs
        ))

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
