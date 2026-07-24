"""Content-addressed plan keys — the ONE source of truth for cache identity, shared by the runner's
durable result_cache and the kernel's warm relation cache, so both invalidate on the same edits
(node config, source fingerprint, wired edges/handles, and a section's contained nodes).
"""

from __future__ import annotations

import functools
import hashlib
import json
import platform

from hub import graph as g
from hub.models import Graph, dataset_ref_identity

# Bump when the KEYING SCHEME changes — old cache entries then fall out of the namespace and recompute
# instead of being served stale. v1 folded requirements + runtime env into the key (P0-CACHE-01).
# v2 invalidates results produced before the central SQL/expression execution policy; a cache hit happens
# before BuildEngine lowering, so namespace invalidation is what prevents a legacy unsafe plan from being
# served without passing the new gate.
CACHE_SCHEMA_VERSION = 3


@functools.lru_cache(maxsize=1)
def _env_digest() -> str:
    """Digest of the code + runtime that PRODUCES a cached result, so a version bump can't serve a stale
    hit: the core package (which ships the IR + DuckDB engine code), Python, and the DuckDB/PyArrow
    runtimes that compute the output. Constant per process → computed once. Plugin / library-processor
    versions are intentionally omitted — plan_cacheable already makes those nodes non-cacheable."""
    try:
        import duckdb
        import pyarrow
        try:
            from importlib.metadata import version as _v
            core = _v("data-playground")
        except Exception:  # noqa: BLE001 — not installed as a dist (a bare source checkout)
            core = "src"
        return "|".join([core, platform.python_version(), duckdb.__version__, pyarrow.__version__])
    except Exception:  # noqa: BLE001 — a missing __version__ must degrade the digest, never break a run
        return f"src|{platform.python_version()}"


def plan_hash(graph: Graph, target: str | None, resolve_adapter) -> str:
    """A stable content hash of the plan up to `target` (whole graph if None). Includes each node's
    type+config, a source's data fingerprint, the wired edges (with handles), and a section's contained
    nodes — so any edit that changes the result changes the hash."""
    from hub.section import _descendants  # section's parent_id-contained nodes
    chain = g.upstream_chain(graph, target) if target else g.topo_order(graph)
    parts: list[str] = []
    # P0-CACHE-01: identity must also cover the canvas's declared requirements (a package-version edit
    # changes them, and a transform node can import them) and the code/runtime producing the result —
    # else a bump silently serves a stale hit. Sorted so declared order doesn't matter.
    parts.append(f"schema:{CACHE_SCHEMA_VERSION}")
    parts.append(f"env:{_env_digest()}")
    parts.append("reqs:" + json.dumps(sorted(getattr(graph, "requirements", None) or [])))

    def _fold(n, prefix=""):
        data = n.data if isinstance(n.data, dict) else {}
        cfg = data.get("config", {})
        if n.type == "source" and isinstance(cfg, dict):
            # Placement is navigation-only for admitted provider Sources.  Do not let a retained
            # legacy placement hint fork cache identity from the canonical URI/DatasetRef.
            cfg = {key: value for key, value in cfg.items() if key not in {
                "providerResourceRef", "providerMountId", "providerSourceBindingId", "providerName",
            }}
        # bypassed/disabled/title are SIBLINGS of config on data, and the engine changes the lowered
        # relation based on them (engine.py reads node.data.bypassed / .disabled; a metric node emits its
        # title as the output value) — so they must be in the key, else a toggle or a metric rename serves
        # a stale cached preview/result. Folding title over-invalidates a plain rename (safe: just recompute).
        canonical_provider_source = (
            n.type == "source" and n.parent_id is None
            and str(cfg.get("uri") or "").startswith("workspace-provider://")
        )
        title = "" if canonical_provider_source else data.get("title", "")
        flags = f"b{int(bool(data.get('bypassed')))}d{int(bool(data.get('disabled')))}t{title}"
        parts.append(f"{prefix}{n.id}:{n.type}:{flags}:{json.dumps(cfg, sort_keys=True, default=str)}")
        if n.type == "source":
            uri = cfg.get("uri")
            if uri:
                admitted_revision = cfg.get("_input_revision_id")
                dataset_ref = cfg.get("datasetRef")
                if isinstance(admitted_revision, str) and admitted_revision:
                    # Exact preview/run/profile bindings must not consult mutable head while keying
                    # work. The private revision is attached only to the dispatch/inspection copy.
                    parts.append(f"{prefix}admitted:{admitted_revision}")
                elif isinstance(dataset_ref, dict):
                    dataset_id, revision_id = dataset_ref_identity(dataset_ref)
                    parts.append(
                        f"{prefix}ref:{dataset_id}:{revision_id}")
                else:
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
            uri = str(cfg.get("uri") or "")
            if uri.startswith("mem://") or is_object_uri(uri):
                return False
        if n.type == "write" and cfg.get("writeMode") == "append":
            return False
        if cfg.get("source") == "library" or n.type in (node_builders or {}):
            return False
    return True
