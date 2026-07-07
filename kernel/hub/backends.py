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

from hub.models import CompilePlan, Graph, GraphNode, Placement, RunEstimate, RunStatus

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
