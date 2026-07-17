"""Sampling provenance shared by bounded previews and explicit reservoir samples."""

from __future__ import annotations

import hashlib
import json

from hub import graph as graph_ops
from hub.ir import resolve_config
from hub.models import Graph, SampleProvenance, dataset_ref_identity
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
            dataset_ref = config.get("datasetRef")
            if isinstance(dataset_ref, dict):
                dataset_identity, dataset_revision = dataset_ref_identity(dataset_ref)
            else:
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


def explicit_sample_provenance(
        graph: Graph, target_node_id: str, resolve_adapter, *, returned_rows: int,
) -> SampleProvenance | None:
    """Return evidence for the effective explicit Sample node in a full execution cone.

    This is deliberately separate from bounded preview provenance: callers use it only after a full
    profile or run actually evaluated the graph. Bypassed/disabled sample nodes do not make a result
    sampled. Exact input totals are reported only for a direct source -> Sample shape, where adapter
    metadata proves the count without another scan.
    """
    chain = graph_ops.upstream_chain(graph, target_node_id)
    samples = [node for node in chain if node.type == "sample"
               and not (isinstance(node.data, dict)
                        and (node.data.get("bypassed") or node.data.get("disabled")))]
    if not samples:
        return None
    sample = samples[-1]
    # The current Sample can describe the complete result only when every input path reaches it.
    # A side branch that bypasses it (sampled or not) makes its seed and requested row count only
    # branch-local facts, so a single provenance record would be misleading.  Sequential samples
    # retain their existing behavior: the downstream, effective Sample is the one that dominates.
    pending = [target_node_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == sample.id or current in seen:
            continue
        seen.add(current)
        parents = graph_ops.parents(graph, current)
        if not parents:
            return None
        pending.extend(parents)
    config = resolve_config(sample)
    raw_n = config.get("n")
    requested_rows = max(0, int(raw_n if raw_n is not None else 1000))
    seed = int(config.get("seed", 42))
    total_rows: int | None = None
    # The Sample node's direct predecessor is the only topology whose metadata count is its exact
    # sampling population. A filter/join may change that population, so unknown is more honest.
    parents = graph_ops.parents(graph, sample.id)
    if len(parents) == 1:
        parent = next((node for node in graph.nodes if node.id == parents[0]), None)
        if parent is not None and parent.type == "source":
            parent_config = parent.data.get("config", {}) if isinstance(parent.data, dict) else {}
            uri = parent_config.get("uri") if isinstance(parent_config, dict) else None
            try:
                count = getattr(resolve_adapter(uri), "metadata_count", None) if uri else None
                value = count(uri) if callable(count) else None
                total_rows = int(value) if value is not None else None
            except Exception:  # provenance is evidence, never a reason to fail a completed result
                pass
    return provenance_for_graph(
        graph, target_node_id, resolve_adapter, strategy="reservoir", seed=seed,
        requested_rows=requested_rows, scanned_rows=total_rows, returned_rows=returned_rows,
        total_rows=total_rows,
        limitations=[
            ("A reservoir sample scanned the complete input."
             if total_rows is not None else
             "A reservoir sample scanned its complete input; total population size is unknown."),
            "Membership is deterministic for this input revision and seed.",
        ],
    )
