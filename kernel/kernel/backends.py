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

from kernel.models import CompilePlan, Graph, GraphNode, Placement, RunEstimate, RunStatus

# The `dataset` wire is a lazy DuckDB relation — the currency a node lowering produces/consumes.
Relation = duckdb.DuckDBPyRelation


@runtime_checkable
class NodeLowering(Protocol):
    """The lowering callable a plugin passes to `reg.add_node(spec, lower)` — how a custom node kind
    turns its inputs into a DuckDB relation plan.

    `engine` is the `kernel.executors.engine.LoweringEngine` driving the pass (typed `Any` to avoid a
    runtime import cycle; use `engine.full`, `engine.node_specs`, `engine._view(rel)` etc.). `inputs`
    are the already-lowered upstream relations, in incoming-edge order. Return a single `Relation`
    for a single-output node, or `{port_id: Relation}` for a multi-output node — the engine routes by
    the wired `source_handle` (default port id is "out"). Lowering is LAZY: return relations, don't
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
