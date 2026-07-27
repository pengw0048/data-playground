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

import errno
import logging
import re
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Literal, TypeVar

import pyarrow as pa

from hub import db, graph as graph_mod
from hub.nodespecs import NodeSpec, ParamSpec, PortSpec, WireType  # re-export
from hub.sqlpolicy import bind_input_ctes, identifier, quote_identifier, validate_query

__all__ = [
    "NodeSpec", "ParamSpec", "PortSpec", "WireType", "ctx", "identifier", "quote_identifier",
    "close_resources", "DatasetBinding", "ProviderBinding", "ImmediateInput", "ImmediateInputPort",
    "ImmediateInputs", "ExactSourceRowRestriction", "NodePreparation",
    "UnsupportedUpstreamError", "RevisionUnavailable", "RevisionPermissionLost",
    "RevisionProviderOffline", "RevisionResolutionAmbiguous",
    "raise_revision_access_error_from_os",
]

_T = TypeVar("_T")
_RESOURCES: dict[str, object] = {}   # process-global warm handles, kept alive across batches AND runs
_RESOURCE_LOCK = threading.RLock()   # REENTRANT: a factory may itself call ctx.resource() for another key
_MISSING = object()                  # sentinel so a factory returning None is still cached (not rebuilt)


class RevisionUnavailable(RuntimeError):
    """An exact provider-native revision cannot be opened; callers must never fall back to head."""


class RevisionPermissionLost(RuntimeError):
    """An exact revision still has identity, but the provider now denies access to it."""


class RevisionProviderOffline(RuntimeError):
    """An exact revision could not be checked because its provider is temporarily unreachable."""


class RevisionResolutionAmbiguous(RuntimeError):
    """A provider cannot prove one exact revision for the requested ordering boundary."""


_OS_ERROR = re.compile(r"\bos error\s+(\d+)\b", re.IGNORECASE)
_PERMISSION_ERRNOS = {errno.EACCES, errno.EPERM}
_OFFLINE_ERRNOS = {
    errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH,
    errno.ENETDOWN, errno.ENETRESET, errno.ENETUNREACH, errno.ETIMEDOUT,
}
_PERMISSION_MARKERS = ("permission denied", "access denied", "operation not permitted")
_OFFLINE_MARKERS = (
    "connection refused", "connection reset", "connection timed out", "host is unreachable",
    "network is down", "network is unreachable", "temporary failure in name resolution",
)


def _revision_error_chain(exc: BaseException):
    """Yield one finite provider error chain, including wrappers that preserve only context."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _os_error_evidence(error: BaseException) -> tuple[set[int], str]:
    """Extract standard OS evidence, including wrappers such as pylance's plain ValueError."""
    message = str(error).lower()
    numbers = {int(match) for match in _OS_ERROR.findall(message)}
    number = getattr(error, "errno", None)
    if isinstance(number, int):
        numbers.add(number)
    return numbers, message


def raise_revision_access_error_from_os(exc: Exception) -> None:
    """Translate one OS/object-store-style failure into the stable revision-access taxonomy.

    This intentionally recognizes only standard exception types, errno evidence, and the small set of
    messages emitted by OS wrappers used by compatible object-store clients. Providers with structured
    native status codes must map those codes directly instead of passing them through this helper.
    """
    chain = list(_revision_error_chain(exc))
    if any(isinstance(error, PermissionError) for error in chain):
        raise RevisionPermissionLost("revision_permission_lost") from exc
    if any(isinstance(error, (ConnectionError, TimeoutError, socket.gaierror))
           for error in chain):
        raise RevisionProviderOffline("revision_provider_offline") from exc
    evidence = [_os_error_evidence(error) for error in chain]
    if any(numbers & _PERMISSION_ERRNOS or any(marker in message for marker in _PERMISSION_MARKERS)
           for numbers, message in evidence):
        raise RevisionPermissionLost("revision_permission_lost") from exc
    if any(numbers & _OFFLINE_ERRNOS or any(marker in message for marker in _OFFLINE_MARKERS)
           for numbers, message in evidence):
        raise RevisionProviderOffline("revision_provider_offline") from exc
    raise RevisionUnavailable("revision_unavailable") from exc


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
class ProviderBinding:
    """The non-secret provider facts admitted for one immediate provider Source input.

    This projection supplements, and never replaces, the opaque ``workspace-provider:*``
    :class:`DatasetBinding`.  It is absent unless the current canonical Workspace record agrees
    with that binding.  It never includes a URI, physical location, mount configuration, or
    credentials.
    """

    provider: str
    mount_id: str
    provider_dataset_id: str
    read_mode: Literal["exact", "current"]
    revision_id: str | None = None


@dataclass(frozen=True)
class ImmediateInput:
    """The deliberately small public description of one directly wired input."""

    kind: str | None
    dataset: DatasetBinding | None = None
    provider: ProviderBinding | None = None


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


@dataclass(frozen=True)
class ExactSourceRowRestriction:
    """One bounded native-row restriction for a directly wired exact Source input."""

    input_port: str
    native_row_ids: tuple[int, ...]


@dataclass(frozen=True)
class NodePreparation:
    """One full-pass preparation result.

    ``state`` is opaque to core and delivered once to the matching prepared builder. ``restriction``
    is the sole supported pre-lowering input operation; general predicates and graph rewrites are
    intentionally outside this contract.
    """

    state: object = None
    restriction: ExactSourceRowRestriction | None = None


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


def _source_provider_binding(source, dataset: DatasetBinding | None) -> ProviderBinding | None:
    """Project one provider Source's same admitted record only when every identity agrees."""
    if dataset is None:
        return None
    data = source.data if isinstance(source.data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return None
    uri = config.get("uri")
    if not isinstance(uri, str):
        return None

    from hub import metadb, workspace_providers

    if not workspace_providers.is_provider_dataset_uri(uri):
        return None
    identity = workspace_providers.provider_dataset_binding_for_identity(dataset.dataset_id)
    if identity is None:
        return None
    mount_id, source_binding_id = identity
    if workspace_providers.provider_dataset_uri(mount_id, source_binding_id) != uri:
        return None
    # State, detail, identity, and projected facts come from this one final database snapshot.
    # It neither loads a provider nor resolves an adapter.
    canonical = metadb.workspace_provider_usable_dataset_for_source_binding(
        mount_id=mount_id, source_binding_id=source_binding_id)
    if not isinstance(canonical, dict):
        return None
    if (canonical.get("mountId") != mount_id
            or canonical.get("sourceBindingId") != source_binding_id):
        return None
    provider = canonical.get("provider")
    provider_dataset_id = canonical.get("providerDatasetId")
    if not isinstance(provider, str) or not provider or not isinstance(provider_dataset_id, str) or not provider_dataset_id:
        return None

    provider_read_mode = config.get("providerReadMode")
    if provider_read_mode == "exact":
        if dataset.revision_id is None:
            return None
        read_mode: Literal["exact", "current"] = "exact"
    elif provider_read_mode == "mutable":
        if dataset.revision_id is not None:
            return None
        read_mode = "current"
    else:
        return None
    return ProviderBinding(
        provider=provider,
        mount_id=mount_id,
        provider_dataset_id=provider_dataset_id,
        read_mode=read_mode,
        revision_id=dataset.revision_id,
    )


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
            dataset = (
                _source_dataset_binding(upstream)
                if upstream.type == "source" and not generated_ref else None
            )
            by_port[port_id].append(ImmediateInput(
                kind=None if generated_ref else upstream.type,
                dataset=dataset,
                provider=(
                    _source_provider_binding(upstream, dataset)
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
