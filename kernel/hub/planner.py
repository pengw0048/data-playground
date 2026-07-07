"""Placement planner — split a run's graph into regions (fused execution units) for per-node
placement. Each region is a maximal same-target subgraph; at its boundaries
(a different-target edge, a fan-out, or the run target) its output is materialized to a ResultRef so
a downstream region — possibly on another worker — reads it as a ref-source.

Pure + place_fn-injected so it's testable without a live backend pool. Sections are placement-OPAQUE
atomic units (the planner never descends into their parent_id children; a section's requirement is the
max over the section node + its descendants). "Whole graph on one target" is the degenerate case — one
region — i.e. exactly today's single run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from hub import graph as g
from hub.models import Graph, ResourceSpec


@dataclass
class Region:
    id: str
    node_ids: set[str]
    output_node: str                    # the boundary node whose output this region materializes
    backend: str                        # target backend name ("default" = in-process local)
    worker: str | None                  # target worker id (None for the default in-process backend)
    requires: ResourceSpec
    # cut inputs from UPSTREAM regions: (upstream_output_node, source_handle, into_node, target_handle)
    cut_inputs: list[tuple] = field(default_factory=list)


def _has_req(r: ResourceSpec) -> bool:
    return bool(r.cpu or r.gpu or r.gpu_type or r.mem or r.labels)


def _region_requires(graph: Graph, node, node_specs: dict) -> ResourceSpec:
    """A node's requirement; for a section, the MAX over the section node + its parent_id descendants
    (a section runs whole on one worker — it's placement-opaque)."""
    from hub import placement
    r = placement.node_requires(node, node_specs) or ResourceSpec()
    if node.type == "section":
        from hub.placement import _mem_gb
        from hub.section import _descendants
        cpu = r.cpu or 0.0
        gpu = r.gpu or 0
        gpu_type = r.gpu_type
        mem = _mem_gb(r.mem)
        labels = dict(r.labels or {})
        for c in _descendants(graph, node.id):
            cr = placement.node_requires(c, node_specs)
            if not cr:
                continue
            cpu = max(cpu, cr.cpu or 0)
            gpu = max(gpu, cr.gpu or 0)
            gpu_type = gpu_type or cr.gpu_type
            mem = max(mem, _mem_gb(cr.mem))
            labels.update(cr.labels or {})
        r = ResourceSpec(cpu=cpu or None, gpu=int(gpu) or None, gpu_type=gpu_type,
                         mem=f"{mem:g}GB" if mem else None, labels=labels)
    return r


def plan_regions(graph: Graph, target: str, node_specs: dict,
                 place_fn: Callable[[ResourceSpec], "tuple[str, str] | None"]) -> list[Region]:
    """Partition the upstream cone of `target` into topologically-ordered regions.

    place_fn(requires) -> (backend_name, worker_id) that satisfies it, or None → the default
    in-process backend. A node with no requirement always lands on the default (so plain relational
    graphs stay one fused region)."""
    chain = [n.id for n in g.upstream_chain(graph, target)]
    if not chain:
        return []
    cs = set(chain)
    nm = g.node_map(graph)

    # 1. resolve each node's target (backend, worker)
    tgt: dict[str, tuple[str, str | None]] = {}
    reqs: dict[str, ResourceSpec] = {}
    for nid in chain:
        r = _region_requires(graph, nm[nid], node_specs)
        reqs[nid] = r
        placed = place_fn(r) if _has_req(r) else None
        tgt[nid] = placed if placed else ("default", None)

    def outs(nid):
        return [e for e in g.outgoing(graph, nid) if e.target in cs]

    # 2. materialization points: the run target, any node with a cross-target out-edge, or a fan-out
    #    (out-degree ≥ 2 within the cone) — the last keeps regions tree-shaped so no node is computed twice.
    mat: set[str] = {target}
    for nid in chain:
        cfg = nm[nid].data.get("config", {}) if isinstance(nm[nid].data, dict) else {}
        if cfg.get("checkpoint") is True:  # user pinned this node to materialize (inspect + reuse)
            mat.add(nid)
        oe = outs(nid)
        if len(oe) >= 2:
            mat.add(nid)
        if any(tgt[e.target] != tgt[nid] for e in oe):
            mat.add(nid)

    # 3. one region per materialization point: BFS upstream, stopping at (and cutting to a ref at) any
    #    other materialization point. `chain` is topo (upstream-first), so iterating mat-points in chain
    #    order yields upstream regions before the regions that consume their refs.
    regions: list[Region] = []
    for M in [nid for nid in chain if nid in mat]:
        region_nodes: set[str] = set()
        cut: list[tuple] = []
        stack = [M]
        while stack:
            x = stack.pop()
            if x in region_nodes:
                continue
            region_nodes.add(x)
            for e in g.incoming(graph, x):
                if e.source not in cs:
                    continue
                if e.source in mat:
                    cut.append((e.source, e.source_handle, x, e.target_handle))  # read the upstream ref
                else:
                    stack.append(e.source)
        b, w = tgt[M]
        regions.append(Region(id=f"r_{M}", node_ids=region_nodes, output_node=M,
                              backend=b, worker=w, requires=reqs[M], cut_inputs=cut))
    return regions
