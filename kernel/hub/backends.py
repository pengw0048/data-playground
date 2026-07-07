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
    CatalogTable, ColumnSchema, CompilePlan, Graph, GraphNode, LineageResult, Placement,
    Relationship, RunEstimate, RunStatus,
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

    def estimate(self, plan: CompilePlan, rows: int) -> RunEstimate:
        """Rows/seconds/cost/placement + whether the run needs confirmation."""
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
    `workers()` advertises capacities (WorkerInfo); `run_unit` runs one region's subgraph to `output_uri`."""
    def workers(self) -> list: ...
    def place(self, requires: Any) -> "str | None": ...
    def run_unit(self, graph: Graph, output_node: str, output_uri: str) -> RunStatus: ...


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
    """The dataset catalog — browse/resolve/lineage + write-back. Swap the WHOLE provider via
    `reg.set_catalog(obj)` to back it with an external metadata service; the built-in InMemoryCatalog
    (DB-backed, cross-instance) conforms. Contract notes: `get_table` MUST raise `KeyError` on a miss
    (`resolve_ref` and `declare_key` depend on it); `resolve_ref` maps a source's bare name/id → a real
    uri (and passes a path/scheme'd uri through unchanged) — it runs on every run/preview, so a remote
    provider should cache. A read-only external catalog can subclass InMemoryCatalog and inherit the
    write/declared-key/relationship methods (local side-store) while overriding only the reads."""
    def list_tables(self, q: "str | None") -> list[CatalogTable]: ...
    def get_table(self, id_or_name: str) -> CatalogTable: ...
    def lineage(self, uri: str) -> LineageResult: ...
    def relationships(self, uri: "str | None" = None) -> list[Relationship]: ...
    def resolve_ref(self, ref: str) -> str: ...
    def register(self, table: CatalogTable, parents: "list[str] | None" = None, pipeline: str = "canvas") -> None: ...
    def register_output(self, name: str, uri: str, version: str, parents: list[str], pipeline: str = "canvas") -> CatalogTable: ...
    def unregister(self, id_or_name: str) -> bool: ...
    def set_declared_key(self, uri: str, columns: "list[str] | None") -> None: ...
    def add_relationship(self, rel: Relationship) -> None: ...
    def remove_relationship(self, rel: Relationship) -> None: ...
