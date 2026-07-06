"""Grain propagation — the key column(s) at which each row of a relation is distinct, tracked
through the relational ops on the canvas.

This is what makes "a sampled/filtered/aggregated dataset is still joinable" a fact the system
knows rather than a thing the user has to remember: a source's grain is its primary key; filter /
sample / sort / a row-wise transform PRESERVE it; group-by re-grains to the group keys; a code op
that explodes or an opaque query LOSE it. A downstream node is joinable to dataset X on key K iff
K is still contained in that node's grain.

Grain is computed structurally (no data scan). `known=False` means we couldn't determine it (an
opaque transform / SQL / section, or an un-keyed source) — honest, never guessed.
"""

from __future__ import annotations

from kernel import graph as g
from kernel.models import Graph, GrainInfo

# a transform mode that keeps the key intact: `filter` drops rows but never rewrites a value, so the
# key stays unique. `map`/`map_batches` are arbitrary Python that can rename OR rewrite the key's
# values → we can't assume the key survived, so they lose the grain (honest, per the design's
# raise-on-ambiguity stance). `flat_map` explodes → definitely a new grain.
_KEY_PRESERVING_MODES = {"filter"}
_UNKNOWN = GrainInfo(columns=None, known=False)


def _cfg(node) -> dict:
    return node.data.get("config", {}) if isinstance(node.data, dict) else {}


def _cols(text: str) -> list[str]:
    """Split a comma-separated column list (the on/groupBy/dedup fields), trimming quotes/space."""
    return [c.strip().strip('"').strip() for c in str(text or "").split(",") if c.strip()]


def _survives_select(text: str, cols: list[str]) -> bool:
    """Does every grain column survive a select as a BARE passthrough output column? A select item is
    bare iff it is exactly the column name (optionally quoted) — NOT `id AS x`, `f(id)`, `user_id AS
    id`, or `* EXCLUDE (id)`. `*` or empty keeps everything. Conservative: anything we can't confirm
    is a plain passthrough loses the grain (a rename/derivation must not keep the old key as grain).
    Splitting on commas can mis-split a `f(a, b)` item, but that only yields non-bare fragments → we
    fall to unknown, which is the safe direction."""
    t = str(text or "").strip()
    if t in ("", "*"):
        return True
    items = {i.strip().strip('"').strip() for i in t.split(",")}
    return all(c in items for c in cols)


def grain_of(graph: Graph, node_id: str, catalog) -> GrainInfo:
    """The grain of node_id's output. catalog supplies a source dataset's key (its PK candidate)."""
    grains: dict[str, GrainInfo] = {}
    for n in g.upstream_chain(graph, node_id):  # topo order — parents computed before children
        grains[n.id] = _grain_for(graph, n, grains, catalog)
    return grains.get(node_id, _UNKNOWN)


def _grain_for(graph: Graph, node, grains: dict[str, GrainInfo], catalog) -> GrainInfo:
    t, cfg = node.type, _cfg(node)
    ps = g.parents(graph, node.id)
    parent = grains.get(ps[0], _UNKNOWN) if ps else _UNKNOWN

    if t == "source":
        return _source_grain(cfg.get("uri") or cfg.get("table"), catalog)
    if t in ("filter", "sort", "sample", "vector-search", "write"):
        return parent  # row-preserving (or row-subsetting) — the key stays unique
    if t == "select":
        if parent.known and parent.columns is not None and _survives_select(cfg.get("select", ""), parent.columns):
            return parent
        return _UNKNOWN  # a projection that may drop/rename the key
    if t == "transform":
        return parent if str(cfg.get("mode", "map")) in _KEY_PRESERVING_MODES else _UNKNOWN
    if t == "dedup":
        on = _cols(cfg.get("on"))
        if on:
            return GrainInfo(columns=on, known=True, verified=True, note="distinct on these columns")
        return parent  # distinct over all columns can only tighten uniqueness
    if t == "aggregate":
        by = _cols(cfg.get("groupBy") or cfg.get("group_by"))
        return GrainInfo(columns=by, known=True, verified=True,
                         note="group-by output is unique per group key" if by else "single-row aggregate")
    # join / sql / section / metric / plugin op → grain not determinable without measurement/execution
    return _UNKNOWN


def _source_grain(uri, catalog) -> GrainInfo:
    if not uri:
        return _UNKNOWN
    try:
        table = catalog.get_table(uri)
    except Exception:  # noqa: BLE001 — not a registered dataset
        return _UNKNOWN
    if not table.keys:
        return GrainInfo(columns=None, known=False, note="no key detected for this dataset")
    best = next((k for k in table.keys if k.confidence == "verified"), table.keys[0])
    return GrainInfo(columns=list(best.columns), known=True, verified=(best.confidence == "verified"),
                     note=f"{best.confidence} primary key")
