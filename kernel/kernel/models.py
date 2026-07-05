"""Core wire DTOs and the canvas graph model.

Everything here is backend-agnostic. camelCase on the wire (to match the frontend),
snake_case in Python. These shapes ARE the contract in PRD §9.
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


class CatalogTable(Wire):
    id: str
    name: str
    uri: str
    row_count: int | None = None
    version: str | None = None
    columns: list[ColumnSchema] = []
    updated_at: str | None = None
    meta: str | None = None


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
    truncated: bool = False
    preview_ref: str | None = None
    not_previewable: bool = False
    error: bool = False        # a real failure (bad code / bad query), distinct from P8 not_previewable
    reason: str | None = None
    wire: WireType = "dataset"


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
    rows: int
    seconds: float
    cost_usd: float
    placement: Placement
    needs_confirm: bool
    breakdown: str | None = None


class PerNodeStatus(Wire):
    node_id: str
    status: NodeStatus
    rows: int | None = None
    ms: int | None = None
    label: str | None = None


class RunStatus(Wire):
    run_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    rows_processed: int = 0
    total_rows: int | None = None
    cost_usd: float = 0.0
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


class KernelInfo(Wire):
    mode: Literal["local", "distributed"] = "local"
    backend: str = "duckdb"
    warm: bool = True
    version: str = "0.1.0"
    adapters: list[str] = []
    runners: list[str] = []
    processors: list[str] = []
    capabilities: list[str] = []


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
# Canvas graph (mirrors PRD §8)
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
