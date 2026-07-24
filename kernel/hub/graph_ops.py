"""Shared graph-editing + advisory helpers — ONE implementation used by every actor.

Two very different actors build the same typed dataflow graph: the in-process LLM agent
(`hub.agent`) and the out-of-process MCP server (`hub.mcp`, driven by a user's own Claude Code).
Rather than let each carry its own copy of "add a node / connect two ports / merge config", the
mutating primitives and the read-only advisors (node kinds, join hints, validate) live here, so a
fix or a rule (typed-wire compatibility, multi-input ports, fan-out warnings) lands in exactly one
place.

Every mutator works on a plain **working-graph dict** in the canvas-doc shape (`{"nodes": [...],
"edges": [...]}`, camelCase edge handles) — the same shape the frontend persists and the agent's
working copy uses — so a caller can load a stored canvas, apply an op, and save it straight back.
Invalid input raises `GraphOpError` (a ValueError); the caller decides how to present it (the agent
folds it into a tool-result `{"error": ...}` the model can recover from; the MCP server turns it
into an MCP tool error).
"""

from __future__ import annotations

import uuid
from typing import Any

from hub import graph as graph_mod
from hub.models import CatalogQuery, GraphNode, normalize_column_schemas


class GraphOpError(ValueError):
    """A bad edit the caller asked for (unknown kind, missing node, port already wired). Not a bug —
    an expected, recoverable outcome the caller surfaces to whoever is driving the build."""


def find_node(graph: dict, node_id: str) -> dict | None:
    return next((n for n in graph.get("nodes", []) if n.get("id") == node_id), None)


def _target_port(node_specs, target_type: str, target_handle: str | None):
    spec = node_specs.get(target_type)
    if not spec or not spec.inputs:
        return None
    return next((p for p in spec.inputs if p.id == target_handle), spec.inputs[0])


def add_node(graph: dict, node_specs, node_id: str, kind: str,
             title: str | None = None, config: dict | None = None) -> dict:
    """Append a node of `kind` (must be a registered node kind) with the given id. Returns the new
    node's id and its input/output port handles so the caller can wire it."""
    spec = node_specs.get(kind)
    if spec is None:
        raise GraphOpError(f"unknown node kind '{kind}'")
    candidate = {
        "id": node_id, "type": kind, "position": {"x": 0, "y": 0},
        "data": {"title": title or kind, "config": dict(config or {})},
    }
    try:
        node = GraphNode.model_validate(candidate)
        outputs = graph_mod.effective_output_ports_for_node(node, spec)
    except ValueError as exc:
        # Validate before mutating the working graph: an invalid dynamic declaration must not leave
        # behind a half-created node when an agent retries with corrected arguments.
        raise GraphOpError(str(exc)) from exc
    graph.setdefault("nodes", []).append(candidate)
    return {"node_id": node_id,
            "inputs": [{"id": p.id, "wire": p.wire} for p in spec.inputs],
            "outputs": [{"id": p.id, "label": p.label, "wire": p.wire}
                        for p in outputs]}


def connect(graph: dict, node_specs, edge_id: str, source_id: str, target_id: str,
            target_handle: str | None = None, source_handle: str | None = None) -> dict:
    """Wire `source_id`'s output to `target_id`'s input (a specific `target_handle` for a multi-input
    node like `join`). Refuses a duplicate wire into a single-fan-in port; a `multi` port (e.g.
    `union`) accepts many DISTINCT sources but still refuses the exact same source twice. The edge
    carries the source port's wire type so the typed-wire checks and the frontend agree on what flows
    down it. A multi-output source always requires ``source_handle``; no actor may guess its first port.
    """
    src, tgt = find_node(graph, source_id), find_node(graph, target_id)
    if not src or not tgt:
        raise GraphOpError("source_id or target_id not found")
    port = _target_port(node_specs, tgt.get("type"), target_handle)
    if port is None:
        raise GraphOpError(f"'{tgt.get('type')}' has no input port")
    # a named handle that matches no input port would silently fall back to the first port — reject it
    # so a mistyped handle surfaces instead of mis-wiring.
    if target_handle is not None and port is not None and port.id != target_handle:
        raise GraphOpError(f"'{tgt.get('type')}' has no input handle '{target_handle}'")
    sspec = node_specs.get(src.get("type"))
    if sspec is None:
        raise GraphOpError(f"unknown source node kind '{src.get('type')}'")
    try:
        source_ports = graph_mod.effective_output_ports_for_node(
            GraphNode.model_validate(src), sspec)
    except ValueError as exc:
        raise GraphOpError(str(exc)) from exc
    if source_handle is None:
        if len(source_ports) != 1:
            raise GraphOpError(
                f"source node '{source_id}' has multiple outputs; source_handle is required")
        source_port = source_ports[0]
    else:
        source_port = next((candidate for candidate in source_ports
                            if candidate.id == source_handle), None)
        if source_port is None:
            raise GraphOpError(
                f"'{src.get('type')}' has no output handle '{source_handle}'")
    canonical_source_handle = source_port.id
    canonical_target_handle = port.id
    default_source_handle = source_ports[0].id if len(source_ports) == 1 else None
    default_target = _target_port(node_specs, tgt.get("type"), None)
    default_target_handle = default_target.id if default_target is not None else None
    is_multi = bool(getattr(port, "multi", False))
    edges = graph.setdefault("edges", [])
    # even a multi port rejects the EXACT same wire twice (same source → same target+handle) — feeding
    # a source into a `union` twice would silently double that source's rows.
    if any(e.get("source") == source_id and e.get("target") == target_id
           and (e.get("sourceHandle") or default_source_handle) == canonical_source_handle
           and (e.get("targetHandle") or default_target_handle) == canonical_target_handle
           for e in edges):
        raise GraphOpError(f"{source_id} is already wired to {target_handle or 'in'} of {target_id}")
    if not is_multi and any(e.get("target") == target_id
                            and (e.get("targetHandle") or default_target_handle)
                            == canonical_target_handle
                            for e in edges):
        raise GraphOpError(f"input {target_handle or 'in'} of {target_id} is already connected")
    wire = source_port.wire
    edges.append({"id": edge_id, "source": source_id, "target": target_id,
                  "sourceHandle": canonical_source_handle,
                  "targetHandle": canonical_target_handle,
                  "data": {"wire": wire}})
    return {"ok": True, "edge_id": edge_id, "wire": wire,
            "source_handle": canonical_source_handle,
            "target_handle": canonical_target_handle}


def set_config(graph: dict, node_specs, node_id: str, config: dict) -> dict:
    """Merge config atomically and preserve only edges whose named output still exists."""
    n = find_node(graph, node_id)
    if not n:
        raise GraphOpError("node_id not found")
    spec = node_specs.get(n.get("type"))
    if spec is None:
        raise GraphOpError(f"unknown node kind '{n.get('type')}'")
    data = dict(n.get("data") or {})
    old_config = data.get("config")
    cfg = dict(old_config) if isinstance(old_config, dict) else {}
    try:
        cfg.update(dict(config or {}))
    except (TypeError, ValueError) as exc:
        raise GraphOpError("config must be an object") from exc
    candidate = dict(n)
    candidate["data"] = {**data, "config": cfg}
    try:
        candidate_node = GraphNode.model_validate(candidate)
        new_ports = graph_mod.effective_output_ports_for_node(candidate_node, spec)
    except ValueError as exc:
        raise GraphOpError(str(exc)) from exc
    try:
        old_ports = graph_mod.effective_output_ports_for_node(
            GraphNode.model_validate(n), spec)
    except ValueError:
        # A valid edit may repair a graph written by an older/broken actor. Unknown implicit edges
        # have no trustworthy prior port identity and are dropped below instead of being guessed.
        old_ports = []

    n["data"] = candidate["data"]
    removed_edges = 0
    if n.get("type") == "section" and "outputs" in (config or {}):
        new_port_ids = {port.id for port in new_ports}
        retained = []
        for edge in graph.get("edges", []):
            if edge.get("source") != node_id:
                retained.append(edge)
                continue
            prior_port = edge.get("sourceHandle")
            if prior_port is None and len(old_ports) == 1:
                prior_port = old_ports[0].id
            if prior_port in new_port_ids:
                retained.append({**edge, "sourceHandle": prior_port})
            else:
                removed_edges += 1
        graph["edges"] = retained
    return {"ok": True, "config": cfg, "removed_edges": removed_edges}


def remove_node(graph: dict, node_id: str) -> dict:
    """Delete a node and every edge touching it. Returns how many edges went with it."""
    if not find_node(graph, node_id):
        raise GraphOpError("node_id not found")
    edges = graph.get("edges", [])
    kept = [e for e in edges if e.get("source") != node_id and e.get("target") != node_id]
    graph["nodes"] = [n for n in graph.get("nodes", []) if n.get("id") != node_id]
    graph["edges"] = kept
    return {"ok": True, "removed_edges": len(edges) - len(kept)}


def fresh_id(graph: dict, prefix: str) -> str:
    """A short, readable id unique among the graph's node AND edge ids (so `filter_1`, `filter_2`, …
    never collide across incremental edits that each reload the persisted doc)."""
    taken = {n.get("id") for n in graph.get("nodes", [])} | {e.get("id") for e in graph.get("edges", [])}
    i = 1
    while f"{prefix}_{i}" in taken:
        i += 1
    return f"{prefix}_{i}"


# --------------------------------------------------------------------------- #
# Read-only advisors — the ground truth a builder consults before editing.
# --------------------------------------------------------------------------- #
def node_kinds(deps) -> list[dict]:
    """Every registered node kind (built-in + plugin) with its params and input/output ports — the
    menu a builder picks from and the param names it must configure."""
    out = []
    for spec in deps.node_specs.values():
        d = spec.model_dump(by_alias=False)
        out.append({
            "kind": d["kind"], "title": d.get("title"), "blurb": d.get("blurb", ""),
            "previewable": d.get("previewable", True),
            "inputs": [{"id": p["id"], "wire": p.get("wire"), "accepts": p.get("accepts"),
                        "multi": p.get("multi", False)} for p in d.get("inputs", [])],
            "outputs": [{"id": p["id"], "wire": p.get("wire")} for p in d.get("outputs", [])],
            "params": [{"name": p["name"], "type": p["type"], "default": p.get("default"),
                        "options": p.get("options"), "label": p.get("label")}
                       for p in d.get("params", [])],
        })
    return out


def catalog_tables(deps) -> list[dict]:
    """The catalog's datasets with columns (name + type), measured row count, and primary-key
    candidate column(s) — everything needed to pick a `source` uri and the keys to join on."""
    out = []
    for t in deps.catalog.list_page(CatalogQuery(limit=5000)).items:
        out.append({
            "name": t.name, "uri": t.uri, "id": t.id, "rowCount": t.row_count,
            "columns": [{"name": c.name, "type": c.type} for c in t.columns],
            "keys": [k.columns for k in t.keys],
            # organization, so an agent can filter/pick by folder/tags/owner the way a human browses
            "folder": t.folder, "tags": t.tags, "owner": t.owner,
        })
    return out


def _resolve_uri_and_cols(deps, arg: str):
    """Resolve a catalog name/id without reading data; raw URIs defer schema I/O to the caller scope."""
    try:
        t = deps.catalog.get_table(arg)
        return t.uri, t.columns  # a registered table's uri was confined at register time — trusted
    except KeyError:
        from hub import paths
        paths.ensure_local_uri_allowed(arg)  # auth mode: don't probe an arbitrary local file's schema
        return arg, None


def join_hints(deps, left: str, right: str) -> dict:
    """How two datasets can join: ranked key-column pairs with the join cardinality MEASURED on the
    data (1:1 / 1:N / N:1 / N:M), plus any owner-declared relationship. `left`/`right` may be catalog
    names/ids or raw uris. Raises on an unreadable dataset — the caller wraps it."""
    from hub import relationships as rel
    from hub.storage import source_read_scope
    (luri, lcols), (ruri, rcols) = (
        _resolve_uri_and_cols(deps, left), _resolve_uri_and_cols(deps, right))

    def stable_identity(value: str):
        try:
            table = deps.catalog.get_table(value)
        except (KeyError, ValueError):
            return None
        return rel.input_identity(
            dataset_id=table.registration_id, revision_id=table.version)

    # Resolve independently: one raw/unavailable side must not erase the other side's retained fact.
    left_identity, right_identity = stable_identity(left), stable_identity(right)
    with source_read_scope(
            deps.storage, [luri, ruri], owner=f"join-hints:{uuid.uuid4().hex}"):
        lcols = lcols if lcols is not None else normalize_column_schemas(deps.resolve_adapter(luri).schema(luri))
        rcols = rcols if rcols is not None else normalize_column_schemas(deps.resolve_adapter(ruri).schema(ruri))
        sugg = rel.suggest_joins(lcols, rcols,
                                 rel.measured_unique(luri, deps.resolve_adapter),
                                 rel.measured_unique(ruri, deps.resolve_adapter),
                                 left_identity=left_identity, right_identity=right_identity)
        return {"suggestions": [s.model_dump(by_alias=True) for s in sugg],
                "declared": [r.model_dump(by_alias=True) for r in deps.catalog.relationships(luri)
                             if ruri in (r.left_uri, r.right_uri)]}


def validate_graph(deps, graph: dict) -> dict:
    """Static graph validity plus join cardinality, without executing the pipeline."""
    from hub import graph as gmod
    from hub import relationships as rel
    from hub.executors.schema import schema_for_graph
    from hub.models import Graph

    g = Graph.model_validate(graph)
    invalid = gmod.validation_error(
        g, deps.node_specs, getattr(deps, "node_builders", {}))
    if invalid:
        return {
            "errors": [invalid[0]],
            "type_errors": gmod.type_errors(g, deps.node_specs),
            "joins": {},
        }
    out: dict[str, Any] = {"type_errors": gmod.type_errors(g, deps.node_specs)}
    cols = schema_for_graph(
        g, deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs,
        storage=deps.storage)
    joins: dict[str, dict] = {}
    for n in g.nodes:
        if n.type == "join":
            ja = rel.analyze_join(
                g, n.id, cols, deps.catalog, deps.resolve_adapter, storage=deps.storage)
            joins[n.id] = {"cardinality": (ja.suggestions[0].cardinality if ja.suggestions else "unknown"),
                           "warning": ja.warning, "note": ja.note,
                           "rowReference": [item.model_dump(by_alias=True)
                                            for item in ja.configured_row_reference],
                           "blockingCode": ja.blocking_code}
            if ja.blocking_code:
                out.setdefault("errors", []).append(ja.blocking_code)
    out["joins"] = joins
    return out


# --------------------------------------------------------------------------- #
# Layout — place freshly-added nodes without disturbing what's already arranged.
# --------------------------------------------------------------------------- #
def layout_new(graph: dict, keep_ids: set[str]) -> None:
    """Assign positions to the nodes NOT in `keep_ids` via a left-to-right topological layering,
    placed below any pre-existing content so a build never lands on top of the user's nodes. Existing
    nodes keep their positions (a hand-arranged canvas isn't reshuffled when one node is added)."""
    nodes = graph.get("nodes", [])
    new = [n for n in nodes if n["id"] not in keep_ids]
    if not new:
        return
    # base the placement on existing TOP-LEVEL nodes only — a section child's position is relative to
    # its parent frame, so mixing it into these absolute-coordinate min/max would throw the anchor off.
    old = [n for n in nodes if n["id"] in keep_ids and not n.get("parentId")]
    base_y = (max((n["position"]["y"] for n in old), default=0) + 280) if old else 80
    base_x = (min((n["position"]["x"] for n in old), default=80)) if old else 80

    # depth = longest path from a root, within the new nodes only
    idset = {n["id"] for n in new}
    parents: dict[str, list[str]] = {n["id"]: [] for n in new}
    for e in graph.get("edges", []):
        if e.get("target") in idset and e.get("source") in idset:
            parents[e["target"]].append(e["source"])
    depth: dict[str, int] = {}

    def d(nid: str, seen: set[str] | None = None) -> int:
        seen = seen or set()
        if nid in depth:
            return depth[nid]
        if nid in seen or not parents.get(nid):
            depth[nid] = 0
            return 0
        depth[nid] = 1 + max(d(p, seen | {nid}) for p in parents[nid])
        return depth[nid]

    per_col: dict[int, int] = {}
    for n in new:
        col = d(n["id"])
        row = per_col.get(col, 0)
        per_col[col] = row + 1
        n["position"] = {"x": base_x + col * 280, "y": base_y + row * 170}
