"""Core wire DTOs and the canvas graph model.

Everything here is backend-agnostic. camelCase on the wire (to match the frontend),
snake_case in Python. These shapes ARE the contract.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    UUID4, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator,
)
from pydantic.alias_generators import to_camel

# dataset/selection/sample/sql-view are the data wires; metric/value are leaf/value wires
# (a metric or a node value driving another node's param). All must be representable on an edge.
WireType = Literal["dataset", "selection", "sample", "sql-view", "metric", "value"]
NodeStatus = Literal["draft", "latest", "stale", "queued", "running", "failed"]
Placement = Literal["local", "distributed"]
PlanDigest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
ProcessorMode = Literal[
    "map",
    "map_batches",
    "filter",
    "flat_map",
    "flat_map_generator",
    "callable",
    "aggregate",
]

PREVIEWABLE_MODES: set[str] = {
    "map",
    "map_batches",
    "filter",
    "flat_map",
    "flat_map_generator",
}


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------- #
# Credentials (first-class Cred entity — references only, never raw secret bytes)
# --------------------------------------------------------------------------- #
class Cred(Wire):
    id: str
    name: str
    kind: str  # 'object_store' | 'agent'
    fields: dict = {}
    created_at: str | None = None


class CredUpsert(Wire):
    id: str | None = None
    name: str
    kind: str
    fields: dict = {}


# --------------------------------------------------------------------------- #
# Schema / catalog
# --------------------------------------------------------------------------- #
class ColumnSchema(Wire):
    name: str
    type: str
    capabilities: list[str] = []


class KeyInfo(Wire):
    """A candidate/known key of a dataset — the column(s) that identify a row (a primary key).
    Composite = more than one column. `confidence`: 'declared' (owner-asserted) | 'verified'
    (measured unique on the data) | 'inferred' (name heuristic, unmeasured)."""
    columns: list[str]
    confidence: Literal["declared", "verified", "inferred"] = "inferred"
    unique: bool | None = None  # measured: distinct(cols) == count; None = not measured


class CatalogTable(Wire):
    id: str
    name: str
    uri: str
    row_count: int | None = None
    version: str | None = None
    columns: list[ColumnSchema] = []
    keys: list[KeyInfo] = []  # candidate/known keys (primary-key candidates), composite-aware
    missing: bool = False  # a local-path dataset whose file no longer exists (grey out / offer removal)
    updated_at: str | None = None
    meta: str | None = None
    # --- organization primitives (what makes a catalog of thousands of tables navigable) --------- #
    # `folder` is a delimiter-joined path ("prod/images/curated") — the browse hierarchy (a namespace).
    # `tags` are free-form labels for faceted filtering; `owner` and `description` are curation metadata.
    # All are generic + owner-asserted; nothing here is tied to any particular external catalog — but
    # they map cleanly onto the namespace/tag/owner model every mature catalog exposes, so an external
    # provider (via the CatalogProvider seam) can round-trip them.
    folder: str = ""
    tags: list[str] = []
    owner: str | None = None
    description: str | None = None
    usage: int = 0  # how often this dataset has been read (popularity signal; drives "most used" sort)


class CatalogPublicationReceipt(Wire):
    """Durable acknowledgement returned by an idempotent catalog output publication."""
    idempotency_key: str
    uri: str
    version: str | None = None
    durable: Literal[True] = True


Cardinality = Literal["1:1", "1:N", "N:1", "N:M", "unknown"]


class Relationship(Wire):
    """A declared relationship between two datasets — the user-asserted join edge shown in the ER
    view. `confidence='declared'` (owner-asserted, trusted like a real FK). Composite via the column
    lists. This is the escape hatch for opaque transforms: declare what the code produced."""
    left_uri: str
    left_columns: list[str]
    right_uri: str
    right_columns: list[str]
    cardinality: Cardinality = "unknown"
    confidence: Literal["declared", "verified", "inferred"] = "declared"


class JoinSuggestion(Wire):
    """A proposed way to join two datasets: matching key column(s) on each side + the measured
    join cardinality. Surfaced in the join node's inspector (catalog-driven join hints)."""
    left_columns: list[str]
    right_columns: list[str]
    cardinality: Cardinality = "unknown"
    confidence: Literal["declared", "verified", "inferred"] = "inferred"
    score: float = 0.0            # ranking (higher = more likely the intended join)
    reason: str = ""              # human-readable why


class JoinAnalysis(Wire):
    """Everything the join node's inspector needs: ranked key suggestions for its two inputs, and a
    fan-out warning when the join isn't 1:1 (the result lands at the finer grain — a later
    parent-grain metric would double-count unless you aggregate)."""
    suggestions: list[JoinSuggestion] = []
    warning: str | None = None
    note: str | None = None  # why suggestions are empty / cardinality unknown, when applicable


class GrainInfo(Wire):
    """The grain of a relation on the canvas: the key column(s) at which each row is distinct,
    propagated through relational ops. `known=False` means the grain couldn't be determined (an
    opaque transform, an un-keyed source). This is what lets a filtered/sampled/aggregated dataset
    still be recognized as joinable — its grain still carries the key."""
    columns: list[str] | None = None
    known: bool = False
    verified: bool = False        # the grain columns are measured-unique at the source
    note: str = ""


class LineageNode(Wire):
    id: str
    name: str
    uri: str
    kind: str = "dataset"


class LineageEdge(Wire):
    parent: str
    child: str
    column: str | None = None
    pipeline: str | None = None


class LineageResult(Wire):
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []
    truncated: bool = False  # the connected component was larger than max_nodes / deeper than depth


# --------------------------------------------------------------------------- #
# Catalog browse / search / facets — the discovery surface that scales to
# thousands of tables (server-side filter + paginate + facet, never "load all").
# --------------------------------------------------------------------------- #
class CatalogQuery(Wire):
    """A filter/sort/paginate request over the catalog. Every field is optional; the empty query is
    'the first page of everything, by name'. This is the ONE shape a CatalogProvider answers for
    browsing — a `q` substring, a `folder` subtree, `tags` (ALL must match), an `owner`, required
    `has_columns`, plus sort + a bounded window. Pushed down to the store (indexed), never realized
    into an in-memory list first."""
    q: str | None = None
    folder: str | None = None          # a folder path; matches that folder AND its subtree
    tags: list[str] = []               # every listed tag must be present (AND)
    owner: str | None = None
    uris: list[str] = []               # restrict to these exact uris (a batch "get these", no 404 on a miss)
    has_columns: list[str] = []        # dataset must expose every listed column (by name)
    sort: Literal["name", "rows", "updated", "usage", "folder"] = "name"
    order: Literal["asc", "desc"] = "asc"
    limit: int = 50
    offset: int = 0


class FacetValue(Wire):
    value: str
    count: int


class Facets(Wire):
    """Distinct values + counts for each facetable dimension, computed over the ACTIVE filter set
    (drill-down semantics) — what powers the facet rail's clickable, counted filters.
    `semantic_available` rides along so a UI knows whether search-by-meaning exists (an embedder
    plugin is installed) without a separate capability round-trip."""
    folders: list[FacetValue] = []
    tags: list[FacetValue] = []
    owners: list[FacetValue] = []
    semantic_available: bool = False


class CatalogPage(Wire):
    """One window of a filtered catalog: the page's items plus the totals a UI needs to paginate
    (total match count, whether more follow) — so the client shows '1–50 of 4,213' and loads the
    next page on demand instead of holding every table in memory."""
    items: list[CatalogTable] = []
    total: int = 0
    offset: int = 0
    limit: int = 50
    has_more: bool = False


class FolderNode(Wire):
    """A folder in the browse tree: its leaf name, full path, and how many tables live in its subtree."""
    name: str
    path: str
    table_count: int = 0


class CatalogBrowse(Wire):
    """One level of the browse tree at a prefix: the immediate child folders (with subtree counts) and
    the tables filed directly at this prefix — a bounded sample (`total_tables`/`truncated` signal
    when there are more; the full listing is the paginated list query with folder=prefix). Lets the
    UI lazily expand a folder tree of any size."""
    prefix: str = ""
    folders: list[FolderNode] = []
    tables: list[CatalogTable] = []
    total_tables: int = 0
    truncated: bool = False


class CatalogFolder(Wire):
    """A first-class browse folder. Additive to the per-dataset `folder` path string: it lets an EMPTY
    folder exist and be renamed/deleted, so a folder can be created up front and filled later."""
    path: str


class CatalogMetadata(Wire):
    """The owner-editable organization fields of a dataset (everything but the probed schema/rows).
    A PUT of this is how a table gets filed into a folder, tagged, owned, or described."""
    folder: str | None = None
    tags: list[str] | None = None
    owner: str | None = None
    description: str | None = None
    name: str | None = None          # optional friendly rename; blank keeps the current name


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
class SampleResult(Wire):
    columns: list[ColumnSchema] = []
    rows: list[dict[str, Any]] = []
    row_count: int | None = None
    has_more: bool = False     # another page exists after this one (for paginated previews)
    truncated: bool = False
    preview_ref: str | None = None
    not_previewable: bool = False
    error: bool = False        # a real failure (bad code / bad query), distinct from P8 not_previewable
    reason: str | None = None
    wire: WireType = "dataset"


class ColumnProfile(Wire):
    name: str
    type: str
    non_null: int = 0
    nulls: int = 0
    distinct: int | None = None    # exact over the sample; None for nested/uncomparable types
    min: str | None = None         # stringified (numeric / temporal / text); None if not applicable
    max: str | None = None
    mean: float | None = None      # numeric columns only


class ProfileResult(Wire):
    columns: list[ColumnProfile] = []
    row_count: int = 0             # rows actually profiled (the bounded sample, NOT the full total)
    sampled: bool = True           # stats are over the previewed sample, not the whole dataset
    not_previewable: bool = False
    error: bool = False
    reason: str | None = None


# --------------------------------------------------------------------------- #
# Pipeline import
# --------------------------------------------------------------------------- #
class ImportStage(Wire):
    name: str
    processor: str
    mode: ProcessorMode
    previewable: bool


class DriverStep(Wire):
    kind: str  # read | op | write | commit | error_gate
    label: str
    node_type: str | None = None


class PipelineImport(Wire):
    config: str
    params: dict[str, Any] = {}
    input_columns: list[str] = []
    output_columns: list[str] = []
    data_filter: str | None = None
    stages: list[ImportStage] = []
    driver_steps: list[DriverStep] = []
    # A runnable canvas graph the importer decomposed the foreign pipeline into. When present, the SPA
    # drops it straight onto a fresh canvas (via applyAgentGraph) and it runs like any other graph —
    # this is what makes "import an external pipeline → runnable canvas" real. stages/driver_steps stay
    # as the human-readable description. None ⇒ the importer only described the pipeline, didn't build it.
    graph: "Graph | None" = None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class RunEstimate(Wire):
    rows: int | None = None   # real source-row count; None when no source is countable (size unknown)
    bytes: int | None = None  # estimated peak data volume (rows × row width); the confirm gate's cost signal
    placement: Placement
    needs_confirm: bool
    breakdown: str | None = None


class ProfileEstimate(RunEstimate):
    """Whole-profile preflight plus the server-minted identity required by submission."""
    plan_digest: PlanDigest


class ProfileIdentity(Wire):
    """Current server identity for recovery without re-running the size estimate."""
    plan_digest: PlanDigest


class PerNodeStatus(Wire):
    node_id: str
    status: str  # per-step run state: queued | running | done | failed (not a NodeStatus)
    rows: int | None = None
    ms: int | None = None
    label: str | None = None
    error: str | None = None  # set on the step that failed — the error (+ a fix hint) attributed to its node


class RunBackendRef(Wire):
    """Durable handle for a run owned by an external execution control plane.

    The handle is intentionally provider-neutral and contains no credentials. A backend can reconstruct
    its supervisor from this record plus its own operator configuration after the hub/kernel restarts.
    """
    backend: str
    cluster_ref: str | None = None
    submission_id: str
    attempt_id: str
    job_uri: str
    result_uri: str
    code_ref: str | None = None
    durable: bool = True


class RunOutput(Wire):
    """One declared output port and its durable publication state.

    Port metadata is a snapshot, not a live lookup: history remains intelligible after a node spec or
    Section declaration changes.  A URI becomes public only after publication commits; pending and
    terminal non-committed outcomes therefore cannot carry storage or catalog identities.
    """

    node_id: str = Field(min_length=1, max_length=256)
    port_id: str = Field(min_length=1, max_length=128)
    port_label: str | None = Field(default=None, max_length=256)
    wire: WireType
    publication_kind: Literal["result", "catalog"]
    outcome: Literal["pending", "committed", "failed", "skipped", "cancelled"]
    uri: str | None = Field(default=None, max_length=8192)
    table: str | None = Field(default=None, max_length=512)
    rows: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _publication_shape(self) -> "RunOutput":
        if self.port_id != self.port_id.strip():
            raise ValueError("run output portId cannot contain surrounding whitespace")
        if self.outcome == "committed":
            if not self.uri:
                raise ValueError("a committed run output requires a URI")
            if self.publication_kind == "catalog" and not self.table:
                raise ValueError("a committed catalog output requires a table identity")
            if self.publication_kind == "result" and self.table is not None:
                raise ValueError("a non-catalog run output cannot carry a table identity")
        elif self.uri is not None or self.table is not None:
            raise ValueError("a non-committed run output cannot expose a URI or table identity")
        return self


def validate_run_output_rows(
        outputs: list[RunOutput], rows: int | None, *, field_name: str) -> None:
    """Validate the one scalar row-count projection shared by live status and history."""
    if rows is None:
        return
    committed = [output for output in outputs if output.outcome == "committed"]
    if (len(outputs) != 1 or len(committed) != 1 or committed[0].rows is None
            or rows != committed[0].rows):
        raise ValueError(
            f"{field_name} requires one committed output and must equal its row count")


class RunStatus(Wire):
    run_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    # ``run`` materializes a graph result; ``profile`` is a whole-dataset inspection job.
    # Both share one durable status/cancel/recovery lifecycle, but consumers must not mistake a
    # profile completion for a newly materialized node result.
    job_type: Literal["run", "profile"] = "run"
    target_node_id: str | None = None   # the run's sink — lets a reattaching client re-bind the run to its node
    rows_processed: int = 0
    total_rows: int | None = None
    ms: int = 0
    placement: Placement = "local"
    per_node: list[PerNodeStatus] = []
    progress: float | None = None       # 0..1 fraction of steps complete (deterministic; any backend can report)
    stalled: bool = False               # running but no step has completed for a while (a soft "stuck?" hint)
    error: str | None = None
    # Ordinary runs publish the declaration-ordered expected port set from their first live status.
    # #263 keeps that set to exactly one until the local/subprocess multi-output state machines land.
    # Profile jobs are inspection jobs and deliberately keep this collection empty.
    outputs: list[RunOutput] = Field(default_factory=list, max_length=64)
    # A profile result is present only on a successful full-profile job. ``plan_digest`` is the fixed-size
    # SHA-256 of the server-authoritative execution/source identity, so durable status never duplicates
    # the raw graph while still fencing results to the exact data revision that was profiled.
    profile: ProfileResult | None = None
    plan_digest: PlanDigest | None = None
    # Monotonic per-canvas submission order allocated by the metadata DB. Recovery uses it instead of
    # host clocks or random run ids; the parent stamps it on statuses and workers cannot choose it.
    profile_attempt_order: int | None = Field(default=None, ge=1)
    # HTTP/WebSocket request id that started this run (OPS-01). Optional so legacy/plugin backends
    # that omit it still deserialize; durable copy also lives on run_states / run_records.
    request_id: str | None = None
    backend_ref: RunBackendRef | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_singular_output_fields(cls, value):
        if isinstance(value, dict) and any(
                key in value for key in ("output_uri", "outputUri", "output_table", "outputTable")):
            raise ValueError("singular run output fields are not part of the public contract")
        return value

    @model_validator(mode="after")
    def _output_collection(self) -> "RunStatus":
        keys = [(output.node_id, output.port_id) for output in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError("run outputs must have unique (nodeId, portId) identities")
        if self.job_type == "profile":
            if self.outputs:
                raise ValueError("profile jobs cannot publish run outputs")
            if self.total_rows is not None:
                raise ValueError("profile jobs report result rows only through profile.rowCount")
        if self.job_type == "run":
            if self.outputs and self.target_node_id is None:
                raise ValueError("run outputs require a targetNodeId")
            if self.target_node_id is not None and any(
                    output.node_id != self.target_node_id for output in self.outputs):
                raise ValueError("every run output nodeId must match targetNodeId")
            if self.status == "done" and self.target_node_id is not None:
                if not self.outputs or any(
                        output.outcome != "committed" for output in self.outputs):
                    raise ValueError(
                        "a successful targeted run requires committed outputs")
            validate_run_output_rows(
                self.outputs, self.total_rows, field_name="totalRows")
            if self.status in ("done", "failed", "cancelled") and any(
                    output.outcome == "pending" for output in self.outputs):
                raise ValueError("a terminal run cannot retain pending outputs")
        return self


class RunHistoryRecord(Wire):
    """Bounded, explicit response model for one durable run-history row."""

    id: str
    run_id: str | None = None
    request_id: str | None = None
    job_type: Literal["run", "profile"]
    status: Literal["done", "failed", "cancelled"]
    target_node_id: str | None = None
    rows: int | None = Field(default=None, ge=0)
    ms: int | None = Field(default=None, ge=0)
    error: str | None = None
    outputs: list[RunOutput] = Field(default_factory=list, max_length=64)
    per_node: list[PerNodeStatus] | None = None
    created_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_singular_output_fields(cls, value):
        if isinstance(value, dict) and any(
                key in value for key in ("output_uri", "outputUri", "output_table", "outputTable")):
            raise ValueError("singular run-history output fields are not part of the public contract")
        return value

    @model_validator(mode="after")
    def _unique_outputs(self) -> "RunHistoryRecord":
        keys = [(output.node_id, output.port_id) for output in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError("run-history outputs must have unique (nodeId, portId) identities")
        if any(output.outcome == "pending" for output in self.outputs):
            raise ValueError("finished run history cannot retain pending outputs")
        if self.job_type == "profile":
            if self.outputs or self.rows is not None:
                raise ValueError("profile history stores result rows only in the profile status")
        else:
            if self.outputs and self.target_node_id is None:
                raise ValueError("run-history outputs require a targetNodeId")
            if self.target_node_id is not None and any(
                    output.node_id != self.target_node_id for output in self.outputs):
                raise ValueError("every history output nodeId must match targetNodeId")
            if self.status == "done" and self.target_node_id is not None:
                if not self.outputs or any(
                        output.outcome != "committed" for output in self.outputs):
                    raise ValueError(
                        "successful targeted run history requires committed outputs")
            validate_run_output_rows(self.outputs, self.rows, field_name="history rows")
        return self


class PlanStep(Wire):
    node_id: str
    kind: str
    mode: str | None = None
    previewable: bool = True
    label: str
    op: str = ""  # the engine-neutral IR op (hub.ir) — lets ExecutionBackend.can_run gate on the clean subset


class CompilePlan(Wire):
    target_node_id: str | None = None
    steps: list[PlanStep] = []
    acyclic: bool = True
    error: str | None = None


class ResourceSpec(Wire):
    """A compute-resource shape, used BOTH ways: a worker advertises its `capacity`, a step declares
    its `requires`. A worker satisfies a step when its capacity ⊇ the requirement (hub.placement,
    Phase C). All fields optional — an empty spec means "no particular requirement / unspecified"."""
    cpu: float | None = None       # cores
    mem: str | None = None         # e.g. "64GB"
    gpu: int | None = None         # gpu count
    gpu_type: str | None = None    # e.g. "a100"
    labels: dict[str, str] = {}


class WorkerInfo(Wire):
    """One execution slot in a backend — a pod, a process, or the local host (the backend decides
    which). `capacity` is what it advertises; Phase C's scheduler matches a step's requires against it."""
    id: str
    capacity: ResourceSpec = ResourceSpec()
    state: Literal["idle", "busy", "down"] = "idle"


class BackendInfo(Wire):
    """An execution backend and the workers it currently offers (powers the real Compute view — the
    honest replacement for the hardcoded `warm`)."""
    name: str
    workers: list[WorkerInfo] = []


class CapabilityView(Wire):
    """A plugin capability that contributes a VIEWER TAB, declaratively. `viewer.kind` names a generic
    renderer the SPA ships (e.g. 'grid' = media/image grid, 'json' = pretty-printed cell) — so a plugin
    adds a viewer tab (for columns it tags via its detector) with NO frontend code, the same way a
    NodeSpec renders a node card. See kernel/hub/plugins/capabilities.py + web/src/nodes/capabilities."""
    id: str
    label: str
    viewer: dict[str, Any]  # {kind: str, ...} — the generic frontend renderer + its params


class KernelInfo(Wire):
    mode: Literal["local", "distributed"] = "local"
    backend: str = "duckdb"
    warm: bool = True
    version: str = "0.1.0"
    adapters: list[str] = []
    runners: list[str] = []
    processors: list[str] = []
    capabilities: list[str] = []
    capability_views: list[CapabilityView] = []  # plugin capabilities that declare a viewer tab (additive)
    backends: list[BackendInfo] = []  # real backend/worker topology + capacities (additive; runners kept)


class ProcessorDescriptor(Wire):
    id: str
    version: str
    title: str
    mode: ProcessorMode
    category: str = "processor"
    input_columns: list[str] = []
    output_schema: list[ColumnSchema] = []
    params_schema: dict[str, Any] = {}
    previewable: bool = True
    blurb: str = ""


# --------------------------------------------------------------------------- #
# Canvas graph
# --------------------------------------------------------------------------- #
class Position(Wire):
    x: float
    y: float


# SEC-10: bound graph complexity + per-node code/SQL so one request can't carry a runaway blob or a
# pathological graph. Generous vs. any real canvas; raise via a new release if a real workload needs more.
MAX_GRAPH_NODES = 5000
MAX_GRAPH_EDGES = 10000
MAX_CODE_LEN = 200_000  # chars, per code/SQL field on a node


class GraphNode(Wire):
    # RunOutput snapshots persist this identity with the same bound. Enforce it at graph ingress so a
    # structurally valid graph cannot fail only after execution output initialization.
    id: str = Field(min_length=1, max_length=256)
    type: str
    position: Position = Position(x=0, y=0)
    data: dict[str, Any] = {}
    parent_id: str | None = None  # visual containment: this node lives inside a section (its parent)

    @field_validator("data")
    @classmethod
    def _cap_embedded_code(cls, v):
        if isinstance(v, dict):
            cfg = v.get("config")
            if isinstance(cfg, dict):
                for key in ("code", "sql"):
                    val = cfg.get(key)
                    if isinstance(val, str) and len(val) > MAX_CODE_LEN:
                        raise ValueError(f"node {key} exceeds the {MAX_CODE_LEN}-char limit")
        return v


class GraphEdgeData(Wire):
    wire: WireType = "dataset"


class GraphEdge(Wire):
    id: str
    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None
    data: GraphEdgeData = GraphEdgeData()


class Graph(Wire):
    id: str = "canvas"
    version: int = 1
    nodes: Annotated[list[GraphNode], Field(max_length=MAX_GRAPH_NODES)] = []
    edges: Annotated[list[GraphEdge], Field(max_length=MAX_GRAPH_EDGES)] = []
    requirements: list[str] = []  # pip specs the canvas needs; the kernel installs them + allows importing them
    # Parent-owned provenance for synthetic region ref-sources. PrivateAttr keeps this control-plane
    # metadata out of the client wire model, workload serialization, and user-controlled node data.
    _publication_source_uris: dict[str, tuple[str, ...]] = PrivateAttr(default_factory=dict)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class SampleRequest(Wire):
    uri: str
    k: int = 50
    columns: list[str] | None = None
    offset: int = 0


class ImportRequest(Wire):
    config: str
    params: dict[str, Any] | None = None


class CompileRequest(Wire):
    graph: Graph
    target_node_id: str | None = None


class PreviewRequest(Wire):
    model_config = ConfigDict(extra="forbid")

    graph: Graph
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    k: int | None = None  # None → fall back to settings.preview_k (DP_PREVIEW_K); an explicit int wins
    offset: int = 0


class ProfileEstimateRequest(Wire):
    """Estimate a whole-dataset profile before the user chooses whether to submit it."""
    graph: Graph
    node_id: str


class ProfileIdentityRequest(Wire):
    """Compute the current server identity for one node without starting work."""
    graph: Graph
    node_id: str


class ProfileJobRequest(Wire):
    """Submit a whole-dataset profile through the durable job lifecycle.

    ``plan_digest`` is minted by the server preflight and checked again at submission. The fixed wire
    value is persisted so a late result cannot be presented for a newer graph or source revision without
    duplicating the submitted graph in durable status rows.
    """
    graph: Graph
    node_id: str
    plan_digest: PlanDigest
    submission_id: UUID4
    confirmed: bool = False


class EstimateRequest(Wire):
    graph: Graph
    target_node_id: str | None = None


class RunRequest(Wire):
    graph: Graph
    target_node_id: str | None = None
    confirmed: bool = False
