"""ExecutionBackend — the plugin contract for WHERE a pipeline runs.

The default `LocalRunner` executes out-of-core in this process. A plugin can register another backend
via `register(reg)` → `reg.add_runner(backend)` — a subprocess/pod, a Ray cluster, a head-pod driver,
a remote service, etc. The kernel routes each run to the first backend whose `can_run(plan)` is true
(Deps.pick_runner), then sends status/cancel for that run to the same backend (Deps.run_index).
Implement this Protocol and nothing else in the core needs to change — execution isolation/scale
becomes a matter of *which backend*, not a core rewrite.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import duckdb

from hub.models import (
    CatalogBrowse, CatalogPage, CatalogQuery, CatalogTable, ColumnSchema, CompilePlan, Facets, Graph,
    GraphNode, LineageResult, Placement, Relationship, RunEstimate, RunStatus,
)

# The `dataset` wire is a lazy DuckDB relation — the currency a node's build produces/consumes.
Relation = duckdb.DuckDBPyRelation


@runtime_checkable
class NodeBuilder(Protocol):
    """The build callable a plugin passes to `reg.add_node(spec, build)` — how a custom node kind
    turns its inputs into a DuckDB relation plan.

    `engine` is the `hub.executors.engine.BuildEngine` driving the pass (typed `Any` to avoid a
    runtime import cycle; use `engine.full`, `engine.node_specs`, `engine._view(rel)` etc.). `inputs`
    are the already-built upstream relations, in incoming-edge order. Return a single `Relation`
    for a single-output node, or `{port_id: Relation}` for a multi-output node — the engine routes by
    the wired `source_handle` (default port id is "out"). Building is LAZY: return relations, don't
    force execution; the runner materializes at the end.
    """

    def __call__(self, engine: Any, node: GraphNode, inputs: list[Relation]) -> "Relation | dict[str, Relation]": ...


@runtime_checkable
class ExecutionBackend(Protocol):
    name: str

    def can_run(self, plan: CompilePlan) -> bool:
        """Whether this backend will handle the given plan (e.g. by size/placement/capabilities)."""
        ...

    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        """Rows + estimated bytes + placement + whether the run needs confirmation (data-volume gate)."""
        ...

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None, placement: Placement) -> RunStatus:
        """Start a run; return its initial status (poll via status(run_id))."""
        ...

    def status(self, run_id: str) -> RunStatus: ...

    def cancel(self, run_id: str) -> RunStatus: ...


@runtime_checkable
class KernelSpawner(Protocol):
    """How a per-canvas kernel is launched — the substrate seam under KernelBackend. `spawn` starts a
    kernel that binds a command channel and marks its lease ready (via metadb.mark_kernel_ready) with a
    reachable endpoint; `kill` force-removes it (a hub on another host can't SIGKILL a process, so the
    cluster substrate deletes the pod). The built-in LocalProcessSpawner runs a detached local process;
    a PodSpawner (a per-canvas k8s Pod + Service) is the cross-host substrate — same protocol."""
    name: str

    def spawn(self, canvas_id: str, kernel_id: str, token: str) -> None: ...

    def kill(self, canvas_id: str, kernel_id: str) -> None:
        """Best-effort force-remove of a canvas's kernel (local process self-exits on fence/idle, so
        this is a no-op there; a pod substrate deletes the Pod + Service)."""
        ...


@runtime_checkable
class PlaceableBackend(Protocol):
    """OPTIONAL companion to ExecutionBackend for a DISTRIBUTED backend that places work on typed workers
    (GPU / region routing) and runs a single placed region. The core FEATURE-DETECTS these via hasattr
    (deps.py `_place`/info, runs.py `_route_by_capability`, run_controller.run_unit), so a backend
    implements them ONLY if it supports placement — they are NOT part of the required ExecutionBackend
    contract. A Ray/k8s cluster backend implements all three; the built-in PoolRunner has workers/place
    and SubprocessRunner has run_unit. `place(requires)` picks a worker for a resource need (or None);
    `workers()` advertises capacities (WorkerInfo); `run_unit` runs one region's subgraph to `output_uri`
    (returning the uri it actually wrote — a single file, or a DIRECTORY of shards for a worker-direct
    parallel write). `requires` is the region's resource need (gpu/cpu/labels) the planner resolved, so
    the backend can place the work on a matching worker / pass it to the cluster scheduler."""
    def workers(self) -> list: ...
    def place(self, requires: Any) -> "str | None": ...
    def run_unit(self, graph: Graph, output_node: str, output_uri: str, requires: Any = None) -> RunStatus: ...


@runtime_checkable
class DatasetAdapter(Protocol):
    """How a URI becomes readable/writable columnar data — the seam for a new storage/format/warehouse
    (Iceberg, Delta, a REST source, …). Register a plugin adapter via `reg.add_adapter(a)`; it is
    inserted FIRST, so it claims a URI before the built-ins. The built-in DuckDBAdapter (parquet/csv/
    json/arrow/dir/object-store) and LanceAdapter both conform — i.e. the built-ins are just the first
    adapters through this same seam. `scan` MUST be LAZY (return a relation; the runner materializes at
    the end). An optional `nearest(uri, column, query, k)` adds native ANN (LanceAdapter has it)."""
    name: str

    def matches(self, uri: str) -> bool: ...
    def scan(self, uri: str, columns: "list[str] | None" = None, predicate: "str | None" = None,
             limit: "int | None" = None, options: "dict | None" = None) -> Relation: ...
    def schema(self, uri: str) -> list[ColumnSchema]: ...
    def count(self, uri: str) -> "int | None": ...
    def fingerprint(self, uri: str) -> str: ...
    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict: ...


@runtime_checkable
class CatalogProvider(Protocol):
    """The dataset catalog — browse/search/resolve/lineage + write-back, built to scale to thousands of
    tables. Swap the WHOLE provider via `reg.set_catalog(obj)` to back it with an external metadata
    service; the built-in InMemoryCatalog (DB-backed, cross-instance) conforms.

    Contract notes: `get_table` MUST raise `KeyError` on a miss (`resolve_ref` and `declare_key` depend
    on it); `resolve_ref` maps a source's bare name/id → a real uri (path/scheme'd uris pass through) —
    it runs on every run/preview, so a remote provider should cache. The discovery surface — `list_page`
    (filter+sort+paginate), `facets`, `browse` (folder tree), `search` (lexical/semantic/hybrid) — is
    what a UI uses; every one is expected to PUSH DOWN to the backing store as a bounded query, never
    to realize the whole catalog. ``search(..., query=CatalogQuery(...))`` carries the same structured
    folder/tag/owner/column filters into semantic and hybrid ranking; providers may keep accepting the
    legacy three-argument form, in which case the router applies a bounded compatibility filter. A
    read-only external catalog can subclass InMemoryCatalog and inherit
    the write/browse/search/lineage machinery while overriding only how rows are fetched.

    A provider written against the PRE-scale protocol (no list_page/facets/browse/search) still works:
    `reg.set_catalog` wraps it in `hub.plugins.catalog.CatalogCompat`, which synthesizes the discovery
    surface from bounded `list_tables()` calls (the old provider's own cost model)."""
    # discovery (bounded, pushed-down)
    def list_page(self, query: CatalogQuery) -> CatalogPage: ...
    def facets(self, query: CatalogQuery) -> Facets: ...
    def browse(self, prefix: str = "") -> CatalogBrowse: ...
    def search(self, q: str, mode: str = "hybrid", limit: int = 50,
               *, query: "CatalogQuery | None" = None) -> list[CatalogTable]: ...
    def search_modes(self) -> list[str]: ...  # ["lexical"] (+ "semantic", "hybrid" once an embedder is live)
    def list_tables(self, q: "str | None") -> list[CatalogTable]: ...  # bounded back-compat convenience
    def get_table(self, id_or_name: str) -> CatalogTable: ...
    def lineage(self, uri: str, depth: int = 6, max_nodes: int = 500) -> LineageResult: ...
    def relationships(self, uri: "str | None" = None) -> list[Relationship]: ...
    def resolve_ref(self, ref: str) -> str: ...
    # write-back + curation. register_output's organization kwargs (folder/tags/owner/description) are
    # optional: None/"" → keep whatever the entry already has (a re-run must not wipe curation).
    def register(self, table: CatalogTable, parents: "list[str] | None" = None, pipeline: str = "canvas") -> None: ...
    def register_output(self, name: str, uri: str, version: "str | None" = None,
                        parents: "list[str] | None" = None, pipeline: "str | None" = "canvas",
                        folder: str = "", tags: "list[str] | None" = None, owner: "str | None" = None,
                        description: "str | None" = None) -> CatalogTable: ...
    def set_metadata(self, uri: str, *, folder: "str | None" = None, tags: "list[str] | None" = None,
                     owner: "str | None" = None, description: "str | None" = None) -> CatalogTable: ...
    def unregister(self, id_or_name: str) -> bool: ...
    def set_declared_key(self, uri: str, columns: "list[str] | None") -> None: ...
    def add_relationship(self, rel: Relationship) -> None: ...
    def remove_relationship(self, rel: Relationship) -> None: ...
