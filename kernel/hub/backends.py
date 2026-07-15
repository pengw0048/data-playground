"""ExecutionBackend — the plugin contract for WHERE a pipeline runs.

The default `LocalRunner` executes out-of-core in this process. A plugin can register another backend
via `register(reg)` → `reg.add_runner(backend)` — a subprocess/pod, a Ray cluster, a head-pod driver,
a remote service, etc. The kernel routes each run to the first backend whose `can_run(plan)` is true
(Deps.pick_runner), then sends status/cancel for that run to the same backend (Deps.run_index).
Implement this Protocol and nothing else in the core needs to change — execution isolation/scale
becomes a matter of *which backend*, not a core rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import duckdb

from hub.models import (
    CatalogBrowse, CatalogPage, CatalogPublicationReceipt, CatalogQuery, CatalogTable, ColumnSchema,
    CompilePlan, Facets, Graph, GraphNode, LineageResult, Placement, Relationship, RunEstimate, RunStatus,
)

# The `dataset` wire is a lazy DuckDB relation — the currency a node's build produces/consumes.
Relation = duckdb.DuckDBPyRelation


def stop_acknowledged(backend, status: RunStatus) -> bool:
    """Whether a terminal-looking backend status proves execution can no longer publish.

    ``done``/``failed`` are ordinary completed outcomes. A backend must explicitly opt into cancelled
    acknowledgement after it has unwound/reaped its worker; legacy/plugin backends that relabel eagerly
    remain unacknowledged so a CLI never mistakes their optimistic status for a safe stop.
    """
    if status.status in ("done", "failed"):
        return True
    if status.status != "cancelled":
        return False
    probe = getattr(backend, "cancel_acknowledged", None)
    if callable(probe):
        try:
            return bool(probe(status.run_id))
        except Exception:  # noqa: BLE001 — fail closed on a broken plugin acknowledgement probe
            return False
    return bool(getattr(backend, "cancel_acknowledges_stop", False))


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
    """Run ownership contract.

    A backend whose terminal ``cancelled`` means its worker is genuinely unable to publish may expose
    ``cancel_acknowledges_stop = True`` or ``cancel_acknowledged(run_id)``. Without that optional seam,
    cancellation is treated as unacknowledged even if a legacy backend eagerly relabels its status.
    """
    name: str

    def can_run(self, plan: CompilePlan) -> bool:
        """Whether this backend will handle the given plan (e.g. by size/placement/capabilities)."""
        ...

    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        """Rows + estimated bytes + placement + whether the run needs confirmation (data-volume gate)."""
        ...

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None, placement: Placement) -> RunStatus:
        """Start a run; return its initial status (poll via status(run_id)).

        Optional keyword-only extensions (feature-detected by ``hub.observability.invoke_backend_run``):
        ``run_id``, ``request_id``, ``attempt_id`` — correlate the hub-minted run with the HTTP/
        WebSocket request and (when applicable) a managed object attempt. Built-in runners accept them;
        plugins may omit them until they opt in.
        """
        ...

    def status(self, run_id: str) -> RunStatus: ...

    def cancel(self, run_id: str) -> RunStatus: ...


@runtime_checkable
class SelectedDestinationCredentialsBackend(Protocol):
    """Optional execution capability for a backend that can honor a selected destination Cred.

    Backends without this capability are rejected before dispatch when a write inherits either a
    destination-specific Cred or the configured default Cred. Ambient workload identity remains valid
    and requires no transport capability.
    """

    def supports_selected_destination_credentials(self) -> bool: ...


@dataclass(frozen=True)
class DestinationCredentialRequirement:
    destination_id: str
    destination_name: str
    selection: str


class UnsupportedDestinationCredentialError(RuntimeError):
    """A selected execution backend cannot honor the write destination's selected Cred."""


def destination_credential_requirement(
        plan: CompilePlan, graph: Graph, workspace: str) -> DestinationCredentialRequirement | None:
    """Find the first selected Cred used by an executable write step, without resolving it."""
    from hub import destinations

    nodes = {node.id: node for node in graph.nodes}
    for step in plan.steps:
        if step.kind != "write":
            continue
        node = nodes.get(step.node_id)
        if node is None:
            continue
        data = node.data if isinstance(node.data, dict) else {}
        config = data.get("config", {}) if isinstance(data.get("config", {}), dict) else {}
        destination_id = str(config.get("destId") or "").strip() or None
        selected = destinations.selected_object_store_credential(
            workspace, destination_id)
        if selected is not None and destination_id is not None:
            selection, destination_name = selected
            return DestinationCredentialRequirement(
                destination_id=destination_id,
                destination_name=destination_name,
                selection=selection,
            )
    return None


def backend_supports_selected_destination_credentials(backend) -> bool:
    """Feature-detect the explicit capability; missing or broken probes fail closed."""
    probe = getattr(backend, "supports_selected_destination_credentials", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - an uncertain transport capability must not select another identity
        return False


def destination_credential_error(
        backend, plan: CompilePlan, graph: Graph, workspace: str) -> str | None:
    requirement = destination_credential_requirement(plan, graph, workspace)
    if requirement is None or backend_supports_selected_destination_credentials(backend):
        return None
    backend_name = str(getattr(backend, "name", "unknown")).replace("\n", " ").replace("\r", " ")[:120]
    return (
        f"Execution backend '{backend_name}' cannot use the {requirement.selection} credential "
        f"selected for destination '{requirement.destination_name}'. Select 'local-out-of-core' "
        "for in-process credential resolution, or clear the destination/default credential to use "
        "ambient workload identity. No run was started."
    )


def require_destination_credential_support(
        backend, plan: CompilePlan, graph: Graph, workspace: str) -> None:
    message = destination_credential_error(backend, plan, graph, workspace)
    if message is not None:
        raise UnsupportedDestinationCredentialError(message)


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
class WholeGraphRequirementBackend(Protocol):
    """Optional admission seam for a backend that owns a requirement as one whole-graph run.

    This is deliberately separate from ``PlaceableBackend.place``: a durable external backend may be
    able to recover one submitted graph while region orchestration remains hub-local and non-durable.
    Returning true routes the whole graph to this backend, which must then reject unsupported pinned
    work explicitly instead of silently falling back to the default engine.
    """
    def accepts_whole_graph(self, requires: Any) -> bool: ...


@runtime_checkable
class PreboundRunIdentityBackend(Protocol):
    """Optional seam for external allocation that requires a durable principal first.

    ``preallocate_run_id`` must be side-effect free. The hub binds that ID to the authorized creator and
    canvas before calling ``run(..., run_id=...)``; the backend must use the supplied ID unchanged.
    """
    def preallocate_run_id(self) -> str: ...


@runtime_checkable
class DatasetAdapter(Protocol):
    """How a URI becomes readable/writable columnar data — the seam for a new storage/format/warehouse
    (Iceberg, Delta, a REST source, …). Register a plugin adapter via `reg.add_adapter(a)`; it is
    inserted FIRST, so it claims a URI before the built-ins. The built-in DuckDBAdapter (parquet/csv/
    json/arrow/dir/object-store) and LanceAdapter both conform — i.e. the built-ins are just the first
    adapters through this same seam. `scan` is the full-run path and MUST be lazy where the source permits
    it. Interactive preview is a separate, optional `DatasetPreviewAdapter` capability. Its
    `preview_scan` MUST enforce `limit` at the source and must never eagerly materialize or scan the full
    input. An adapter that cannot prove that bound omits the capability; an adapter that supports only
    some URIs may reject those it cannot bound. The UI will offer a durable full run instead. Merely
    accepting a `limit` argument on `scan` is not proof of bounded work. An adapter may optionally accept
    a `cancelled` callable in `write`; LocalRunner feature-
    detects it, and the adapter should call it immediately before any externally visible publish point.
    Adapters without that optional argument retain the legacy pre-call-only cancellation behavior. An
    optional `nearest(uri, column, query, k)` adds native ANN (LanceAdapter has it). An optional
    `metadata_count(uri)` may return an exact count only from a bounded metadata lookup; it MUST NOT scan
    rows or enumerate an unbounded partition/directory namespace. Interactive/preflight callers do not use
    the potentially full-scanning `count(uri)` fallback. `fingerprint(uri)` is also a preflight / recovery
    capability: it MUST be bounded and metadata-only and MUST NOT scan or materialize source rows. It may
    return the adapter's best available revision identity (including URI-only when that is all the source
    exposes); callers must not assume it is a content hash or a versioned snapshot id."""
    name: str

    def matches(self, uri: str) -> bool: ...
    def scan(self, uri: str, columns: "list[str] | None" = None, predicate: "str | None" = None,
             limit: "int | None" = None, options: "dict | None" = None) -> Relation: ...
    def schema(self, uri: str) -> list[ColumnSchema]: ...
    def count(self, uri: str) -> "int | None": ...
    def fingerprint(self, uri: str) -> str: ...
    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict: ...


@runtime_checkable
class DatasetPreviewAdapter(Protocol):
    """Optional source-bounded interactive preview capability for a `DatasetAdapter`.

    Structural conformance to the base adapter contract deliberately does not require this method: a
    full-run-only third-party source remains a valid adapter and fails closed only when preview is asked
    for. Implementations may raise their adapter-specific bounded-preview exception for unsupported URIs.
    """

    def preview_scan(self, uri: str, columns: "list[str] | None" = None,
                     limit: int = 2000, options: "dict | None" = None) -> Relation: ...


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


@runtime_checkable
class DurableCatalogPublisher(Protocol):
    """Optional write capability required by durable, at-least-once execution backends.

    Output effects use one key per sink. Usage/popularity is a separate run-level effect over the union
    of source parents, so retries and multi-sink graphs remain both idempotent and correctly counted.
    """
    def register_output_idempotent(
        self, idempotency_key: str, **kwargs: Any
    ) -> CatalogPublicationReceipt: ...
    def record_usage_idempotent(self, idempotency_key: str, parents: list[str]) -> bool: ...
