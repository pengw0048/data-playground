"""Core wire DTOs and the canvas graph model.

Everything here is backend-agnostic. camelCase on the wire (to match the frontend),
snake_case in Python. These shapes ARE the contract.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# dataset/selection/sample/sql-view are the data wires; metric/value are leaf/value wires
# (a metric or a node value driving another node's param). All must be representable on an edge.
WireType = Literal["dataset", "selection", "sample", "sql-view", "metric", "value"]
NodeStatus = Literal["draft", "latest", "stale", "queued", "running", "failed"]
Placement = Literal["local", "distributed"]
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


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class RunEstimate(Wire):
    rows: int | None = None   # real source-row count; None when no source is countable (size unknown)
    placement: Placement
    needs_confirm: bool
    breakdown: str | None = None


class PerNodeStatus(Wire):
    node_id: str
    status: str  # per-step run state: queued | running | done | failed (not a NodeStatus)
    rows: int | None = None
    ms: int | None = None
    label: str | None = None


class RunStatus(Wire):
    run_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    rows_processed: int = 0
    total_rows: int | None = None
    ms: int = 0
    placement: Placement = "local"
    per_node: list[PerNodeStatus] = []
    error: str | None = None
    output_uri: str | None = None
    output_table: str | None = None


class PlanStep(Wire):
    node_id: str
    kind: str
    mode: str | None = None
    previewable: bool = True
    label: str


class CompilePlan(Wire):
    target_node_id: str | None = None
    steps: list[PlanStep] = []
    acyclic: bool = True
    error: str | None = None


class ResourceSpec(Wire):
    """A compute-resource shape, used BOTH ways: a worker advertises its `capacity`, a step declares
    its `requires`. A worker satisfies a step when its capacity ⊇ the requirement (kernel.placement,
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


class KernelInfo(Wire):
    mode: Literal["local", "distributed"] = "local"
    backend: str = "duckdb"
    warm: bool = True
    version: str = "0.1.0"
    adapters: list[str] = []
    runners: list[str] = []
    processors: list[str] = []
    capabilities: list[str] = []
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


class GraphNode(Wire):
    id: str
    type: str
    position: Position = Position(x=0, y=0)
    data: dict[str, Any] = {}
    parent_id: str | None = None  # visual containment: this node lives inside a section (its parent)


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
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class SampleRequest(Wire):
    uri: str
    k: int = 50
    columns: list[str] | None = None


class ImportRequest(Wire):
    config: str
    params: dict[str, Any] | None = None


class CompileRequest(Wire):
    graph: Graph
    target_node_id: str | None = None


class PreviewRequest(Wire):
    graph: Graph
    node_id: str
    k: int = 50
    offset: int = 0


class EstimateRequest(Wire):
    graph: Graph
    target_node_id: str | None = None


class RunRequest(Wire):
    graph: Graph
    target_node_id: str | None = None
    confirmed: bool = False
