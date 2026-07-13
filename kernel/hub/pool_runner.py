"""A reference multi-worker execution backend — capability-based placement WITHOUT a cluster.

Each "worker" is a logical slot with an advertised `ResourceSpec` capacity (configured via
DP_POOL_WORKERS); a run is placed on a worker whose capacity satisfies the graph's requirement, then
executed in an isolated OS process (reusing SubprocessRunner). This makes the whole placement path —
workers() / place() / capacity matching / the Compute view — real and testable in the open-source
build; only the "GPU" is simulated. A real k8s-pod or Ray backend is a plugin implementing the same
methods, where run_unit() actually allocates a pod / submits a Ray job.

Config (DP_POOL_WORKERS, JSON):
    [{"name": "cpu", "cpu": 8}, {"name": "gpu", "cpu": 16, "gpu": 2, "gpu_type": "a100"}]

Placement here is whole-run (the graph's max requirement → one worker); per-node placement + fusion
across workers is a later phase. Reserve/lease is not yet modeled — place() picks the
first capacity-matching worker (busy tracking is advisory, for the Compute view).
"""

from __future__ import annotations

import json
import os

from hub import placement
from hub.models import CompilePlan, Graph, Placement, ResourceSpec, RunStatus, WorkerInfo
from hub.subprocess_runner import SubprocessRunner


def pool_workers_from_env() -> list[dict] | None:
    """Parse DP_POOL_WORKERS; None if unset/blank (→ the pool backend isn't registered)."""
    raw = (os.environ.get("DP_POOL_WORKERS") or "").strip()
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, list) and cfg else None
    except ValueError:
        return None


class PoolRunner(SubprocessRunner):
    name = "local-pool"

    def __init__(self, workspace: str, data_dir: str, workers_cfg: list[dict], node_specs=None,
                 catalog=None, storage=None, resolve_adapter=None, node_builders=None):
        super().__init__(
            workspace, data_dir, catalog=catalog, storage=storage,
            resolve_adapter=resolve_adapter, node_builders=node_builders)
        self.node_specs = node_specs if node_specs is not None else {}
        self._capacity: dict[str, ResourceSpec] = {}
        for w in workers_cfg:
            name = str(w.get("name") or f"w{len(self._capacity)}")
            self._capacity[name] = ResourceSpec(cpu=w.get("cpu"), mem=w.get("mem"),
                                                gpu=w.get("gpu"), gpu_type=w.get("gpu_type") or w.get("gpuType"),
                                                labels=w.get("labels") or {})
        self._assigned: dict[str, str] = {}  # run_id -> worker id (advisory busy tracking)

    # reachable_tiers = ("local","object") is inherited from SubprocessRunner (a pool worker is a same-host
    # child sharing the workspace FS), so a local pool handoff isn't refused as if it were object-only.

    def workers(self) -> list[WorkerInfo]:
        busy = set(self._assigned.values())
        return [WorkerInfo(id=wid, capacity=cap, state="busy" if wid in busy else "idle")
                for wid, cap in self._capacity.items()]

    def place(self, requires: ResourceSpec | None) -> str | None:
        """First worker whose capacity satisfies the requirement — idle preferred, else any match
        (MVP: no lease/queue; a busy match will simply run concurrently)."""
        busy = set(self._assigned.values())
        matching = [wid for wid, cap in self._capacity.items() if placement.satisfies(cap, requires)]
        if not matching:
            return None
        return next((w for w in matching if w not in busy), matching[0])

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement_: Placement) -> RunStatus:
        req = placement.graph_requires(graph, self.node_specs)
        worker = self.place(req)
        if worker is None:
            raise RuntimeError(f"no worker in the pool satisfies {req.model_dump(exclude_none=True)}")
        status = super().run(plan, graph, target_node_id, placement_)
        with self._lock:
            self._assigned[status.run_id] = worker
        return status

    def _watch(self, run_id: str, *args) -> None:
        try:
            super()._watch(run_id, *args)
        finally:
            with self._lock:
                self._assigned.pop(run_id, None)  # free the slot when the run ends
