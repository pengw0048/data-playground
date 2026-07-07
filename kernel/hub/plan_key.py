"""Content-addressed plan keys — the ONE source of truth for cache identity, shared by the runner's
durable result_cache and the kernel's warm relation cache, so both invalidate on the same edits
(node config, source fingerprint, wired edges/handles, and a section's contained nodes).
"""

from __future__ import annotations

import hashlib
import json

from hub import graph as g
from hub.models import Graph


def plan_hash(graph: Graph, target: str | None, resolve_adapter) -> str:
    """A stable content hash of the plan up to `target` (whole graph if None). Includes each node's
    type+config, a source's data fingerprint, the wired edges (with handles), and a section's contained
    nodes — so any edit that changes the result changes the hash."""
    from hub.section import _descendants  # section's parent_id-contained nodes
    chain = g.upstream_chain(graph, target) if target else g.topo_order(graph)
    parts: list[str] = []

    def _fold(n, prefix=""):
        data = n.data if isinstance(n.data, dict) else {}
        cfg = data.get("config", {})
        # bypassed/disabled are SIBLINGS of config on data, and the engine changes the lowered relation
        # based on them (engine.py reads node.data.bypassed / .disabled) — so they must be in the key,
        # else toggling bypass/disable serves a stale cached preview/result.
        flags = f"b{int(bool(data.get('bypassed')))}d{int(bool(data.get('disabled')))}"
        parts.append(f"{prefix}{n.id}:{n.type}:{flags}:{json.dumps(cfg, sort_keys=True, default=str)}")
        if n.type == "source":
            uri = cfg.get("uri") or cfg.get("table")
            if uri:
                try:
                    parts.append(f"{prefix}fp:{resolve_adapter(uri).fingerprint(uri)}")
                except Exception:  # noqa: BLE001
                    pass
        if n.type == "section":  # a section's behavior lives on its contained nodes, not its own config
            for c in sorted(_descendants(graph, n.id), key=lambda x: x.id):
                _fold(c, prefix=f"sub[{n.id}]:")

    for n in chain:
        _fold(n)
    ids = {n.id for n in chain}
    parts += sorted(f"e:{e.source}:{e.source_handle}:{e.target}:{e.target_handle}"
                    for e in graph.edges if e.source in ids and e.target in ids)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def plan_cacheable(graph: Graph, target: str | None, node_builders) -> bool:
    """Whether this plan's result may be REUSED from a cache — conservative, since a stale hit is
    permanent wrong data while a miss just recomputes. Non-cacheable when the content key can't fully
    capture identity: an explicit `config.cacheable=False`; an object-store / mem:// source (URI-only
    fingerprint → an in-place overwrite wouldn't change the key); a library transform or plugin node
    kind (its CODE isn't in the key); or an append write (not idempotent)."""
    from hub.plugins.adapters import is_object_uri
    from hub.section import _descendants
    chain = list(g.upstream_chain(graph, target) if target else g.topo_order(graph))
    nodes = list(chain)
    for n in chain:
        if n.type == "section":
            nodes += _descendants(graph, n.id)
    for n in nodes:
        cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
        if cfg.get("cacheable") is False:
            return False
        if n.type == "source":
            uri = str(cfg.get("uri") or cfg.get("table") or "")
            if uri.startswith("mem://") or is_object_uri(uri):
                return False
        if n.type == "write" and cfg.get("writeMode") == "append":
            return False
        if cfg.get("source") == "library" or n.type in (node_builders or {}):
            return False
    return True
