"""Server-authoritative identity for whole-dataset profile results.

The browser may use this digest to decide whether a recovered result still belongs to the open graph,
but it never gets to mint the identity persisted with a job.  Source references must be resolved before
calling :func:`profile_plan_digest` so catalog aliases cannot create two identities for one execution.
"""

from __future__ import annotations

import hashlib
import json

from hub import graph as graph_mod
from hub.models import Graph


_PROFILE_IDENTITY_SCHEMA = 3


def profile_plan_digest(graph: Graph, node_id: str, port_id: str, resolve_adapter) -> str:
    """Return a canonical SHA-256 for the server-observed profile identity at ``node_id``.

    Layout, transient node status, history, edge ids, and document version are deliberately omitted.
    Fields that can change execution are retained, including metric/section titles and each adapter's
    best-available source fingerprint. The adapter contract requires that fingerprint lookup be bounded
    and metadata-only, but it may be URI-only or snapshot-agnostic when the source exposes no cheap
    revision identity. This digest therefore prevents graph/runtime identity mix-ups only to the strength
    of those adapter-provided values; strongly versioned runtime/plugin/source identity remains #226.
    """
    chain = graph_mod.upstream_chain(graph, node_id)
    if not chain:
        raise ValueError(f"node '{node_id}' not found")

    from hub.section import _descendants

    by_id = {node.id: node for node in chain}
    for node in list(chain):
        if node.type == "section":
            by_id.update({child.id: child for child in _descendants(graph, node.id)})
    node_ids = set(by_id)

    nodes = []
    for node in sorted(by_id.values(), key=lambda item: item.id):
        data = node.data if isinstance(node.data, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        nodes.append({
            "id": node.id,
            "type": node.type,
            "parentId": node.parent_id,
            "title": data.get("title"),
            "config": config,
            "bypassed": bool(data.get("bypassed")),
            "disabled": bool(data.get("disabled")),
        })

    edges = sorted((
        {
            "source": edge.source,
            "target": edge.target,
            "sourceHandle": edge.source_handle,
            "targetHandle": edge.target_handle,
            "wire": edge.data.wire,
        }
        for edge in graph.edges
        if edge.source in node_ids and edge.target in node_ids
    ), key=lambda edge: (
        edge["source"], edge["target"], edge["sourceHandle"] or "",
        edge["targetHandle"] or "", edge["wire"],
    ))

    from hub.local_run_inputs import source_nodes

    fingerprints = []
    for position, source in enumerate(source_nodes(graph, node_id)):
        data = source.data if isinstance(source.data, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        uri = str(config.get("uri") or "")
        revision_id = config.get("_input_revision_id")
        # A bound manifest must not consult mutable head again while minting its identity. The exact
        # provider revision is already validated and reopened before this function runs.
        fingerprint = (
            f"revision:{revision_id}" if isinstance(revision_id, str) and revision_id
            else str(resolve_adapter(uri).fingerprint(uri))
        )
        fingerprints.append({
            "position": position, "nodeId": source.id, "uri": uri,
            "fingerprint": fingerprint,
        })

    canonical = {
        "schema": _PROFILE_IDENTITY_SCHEMA,
        "canvasId": graph.id,
        "targetNodeId": node_id,
        "targetPortId": port_id,
        "requirements": sorted(graph.requirements or []),
        "nodes": nodes,
        "edges": edges,
        "sourceFingerprints": fingerprints,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
