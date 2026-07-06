"""Capability matching for execution placement (design: docs/EXECUTION.md §2/§4).

A worker advertises a `ResourceSpec` capacity; a step (or a whole graph) declares a `ResourceSpec`
requirement. `satisfies(capacity, requires)` decides whether the worker can host it. Pure + tiny so
it's the one place the match rule lives — the pool backend (and a future k8s/Ray backend) reuse it.
"""

from __future__ import annotations

from kernel.models import Graph, ResourceSpec


def _mem_gb(s: str | None) -> float:
    if not s:
        return 0.0
    t = str(s).strip().upper().rstrip("B")
    try:
        if t.endswith("T"):
            return float(t[:-1]) * 1024
        if t.endswith("G"):
            return float(t[:-1])
        if t.endswith("M"):
            return float(t[:-1]) / 1024
        return float(t) / (1024 ** 3)  # bare bytes
    except ValueError:
        return 0.0


def satisfies(capacity: ResourceSpec, requires: ResourceSpec | None) -> bool:
    """True if a worker with `capacity` can host a step needing `requires`. Unset requirement fields
    mean 'don't care'; gpu_type must match exactly when required."""
    if requires is None:
        return True
    if requires.cpu and (capacity.cpu or 0) < requires.cpu:
        return False
    if requires.gpu and (capacity.gpu or 0) < requires.gpu:
        return False
    if requires.gpu_type and capacity.gpu_type != requires.gpu_type:
        return False
    if requires.mem and _mem_gb(capacity.mem) < _mem_gb(requires.mem):
        return False
    caps_labels = capacity.labels or {}
    return all(caps_labels.get(k) == v for k, v in (requires.labels or {}).items())


def node_requires(node, node_specs: dict) -> ResourceSpec | None:
    """A node's compute requirement: the per-instance override (config.requires) if present, else the
    plugin-declared default (NodeSpec.requires). None = no particular requirement."""
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    override = cfg.get("requires")
    if isinstance(override, dict) and override:
        try:
            return ResourceSpec(**override)  # accepts camelCase (gpuType) or snake (gpu_type)
        except Exception:  # noqa: BLE001
            return None
    spec = node_specs.get(node.type)
    return getattr(spec, "requires", None) if spec is not None else None


def graph_requires(graph: Graph, node_specs: dict) -> ResourceSpec:
    """The aggregate requirement of a whole graph — the MAX over its nodes (a worker must satisfy the
    heaviest step). Used for whole-run placement (C1); per-node placement (C2/C3) matches each unit."""
    cpu = gpu = 0.0
    gpu_type: str | None = None
    mem_gb = 0.0
    labels: dict[str, str] = {}
    for n in graph.nodes:
        r = node_requires(n, node_specs)
        if not r:
            continue
        cpu = max(cpu, r.cpu or 0)
        gpu = max(gpu, r.gpu or 0)
        gpu_type = gpu_type or r.gpu_type
        mem_gb = max(mem_gb, _mem_gb(r.mem))
        labels.update(r.labels or {})
    return ResourceSpec(cpu=cpu or None, gpu=int(gpu) or None, gpu_type=gpu_type,
                        mem=f"{mem_gb:g}GB" if mem_gb else None, labels=labels)
