"""Sampling provenance shared by bounded previews and explicit reservoir samples."""

from __future__ import annotations

import hashlib
import json

from hub import graph as graph_ops
from hub.models import Graph, SampleProvenance
from hub.plan_key import plan_hash


def provenance_for_graph(
        graph: Graph, target_node_id: str, resolve_adapter, *, strategy: str,
        seed: int | None, requested_rows: int, scanned_rows: int | None,
        returned_rows: int, total_rows: int | None, limitations: list[str],
) -> SampleProvenance:
    """Describe one-source provenance without inventing an identity for a multi-source graph."""
    sources = [node for node in graph_ops.upstream_chain(graph, target_node_id) if node.type == "source"]
    dataset_identity: str | None = None
    dataset_revision: str | None = None
    notes = list(limitations)
    if len(sources) == 1:
        config = sources[0].data.get("config", {}) if isinstance(sources[0].data, dict) else {}
        uri = config.get("uri") if isinstance(config, dict) else None
        if isinstance(uri, str) and uri:
            dataset_identity = uri
            try:
                revision = resolve_adapter(uri).fingerprint(uri)
                dataset_revision = revision if revision and revision != "unknown" else None
            except Exception:  # source identity is best-effort evidence, never a preview failure
                pass
    elif len(sources) != 1:
        notes.append("Dataset identity/revision is unavailable because this result has multiple sources.")

    canonical = json.dumps({
        "strategy": strategy,
        "seed": seed,
        "requestedRows": requested_rows,
        "datasetIdentity": dataset_identity,
        "datasetRevision": dataset_revision,
        # Bind the evidence to the complete execution cone, not only the physical source. A filter,
        # join, or other upstream edit changes the sampled population even when the source revision and
        # seed stay fixed. Reuse the runtime's canonical plan identity so provenance and cached results
        # invalidate on the same execution-relevant edits.
        "planHash": plan_hash(graph, target_node_id, resolve_adapter),
    }, sort_keys=True, separators=(",", ":"))
    return SampleProvenance(
        strategy=strategy, seed=seed, requested_rows=requested_rows,
        scanned_rows=scanned_rows, returned_rows=returned_rows, total_rows=total_rows,
        dataset_identity=dataset_identity, dataset_revision=dataset_revision,
        identity=hashlib.sha256(canonical.encode()).hexdigest(), limitations=notes,
    )


def provenance_for_dataset(
        uri: str, adapter, *, requested_rows: int, scanned_rows: int | None,
        returned_rows: int, total_rows: int | None, limitations: list[str],
) -> SampleProvenance:
    """Prefix-preview provenance for a direct catalog or artifact sample."""
    revision: str | None = None
    try:
        value = adapter.fingerprint(uri)
        revision = value if value and value != "unknown" else None
    except Exception:  # provenance must not turn a readable preview into an error
        pass
    canonical = json.dumps({
        "strategy": "prefix", "seed": None, "requestedRows": requested_rows,
        "datasetIdentity": uri, "datasetRevision": revision,
    }, sort_keys=True, separators=(",", ":"))
    return SampleProvenance(
        strategy="prefix", requested_rows=requested_rows, scanned_rows=scanned_rows,
        returned_rows=returned_rows, total_rows=total_rows, dataset_identity=uri,
        dataset_revision=revision, identity=hashlib.sha256(canonical.encode()).hexdigest(),
        limitations=limitations,
    )
