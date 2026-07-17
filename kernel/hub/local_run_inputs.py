"""Immutable local-run input-manifest validation and exact Source binding."""

from __future__ import annotations

from hub import db, graph as graph_mod, metadb
from hub.backends import DatasetRevisionAdapter

_MANIFEST_FIELDS = {"node_id", "dataset_id", "revision_id", "provider", "resolved_at"}


class LocalRunInputError(RuntimeError):
    """The admitted local-run input contract is malformed, stale, or unavailable."""


def validate_manifest(value: object) -> list[dict[str, str]]:
    """Return a copied, ordered, secret-free manifest or fail closed."""
    if not isinstance(value, list) or any(
            not isinstance(item, dict) or set(item) != _MANIFEST_FIELDS
            or any(not isinstance(part, str) or not part for part in item.values())
            for item in value):
        raise LocalRunInputError("local run input manifest is malformed")
    return [{field: item[field] for field in (
        "node_id", "dataset_id", "revision_id", "provider", "resolved_at")}
        for item in value]


def source_nodes(graph, target_node_id: str | None):
    """The ordered Source cone whose identity is attested by a manifest."""
    cone = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else graph.nodes
    return [node for node in cone if node.type == "source"]


def validate_manifest_graph(graph, target_node_id: str | None, manifest: object, *,
                            require_bound_revisions: bool) -> list[dict[str, str]]:
    """Ensure one manifest exactly covers the target's ordered Source cone."""
    admitted = validate_manifest(manifest)
    sources = source_nodes(graph, target_node_id)
    if [str(node.id) for node in sources] != [item["node_id"] for item in admitted]:
        raise LocalRunInputError("local run input manifest does not match the graph")
    if require_bound_revisions:
        for node, item in zip(sources, admitted, strict=True):
            data = node.data if isinstance(node.data, dict) else {}
            config = data.get("config") if isinstance(data, dict) else None
            if (not isinstance(config, dict)
                    or config.get("_input_dataset_id") != item["dataset_id"]
                    or config.get("_input_provider") != item["provider"]
                    or config.get("_input_revision_id") != item["revision_id"]):
                raise LocalRunInputError("local run input manifest does not match bound source identity")
    return admitted


def bind_manifest(graph, target_node_id: str | None, manifest: object, resolve_adapter):
    """Reopen admitted provider revisions and bind them only to a private dispatch graph."""
    admitted = validate_manifest_graph(
        graph, target_node_id, manifest, require_bound_revisions=False)
    bound = graph.model_copy(deep=True)
    sources = source_nodes(bound, target_node_id)
    for node, item in zip(sources, admitted, strict=True):
        config = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        source_uri = str(config.get("uri") or "") if isinstance(config, dict) else ""
        source_binding = metadb.catalog_revision_binding_for_uri(source_uri)
        dataset_ref = config.get("datasetRef") if isinstance(config, dict) else None
        if (source_binding is None
                or str(source_binding["dataset_id"]) != item["dataset_id"]
                or (isinstance(dataset_ref, dict) and (
                    str(dataset_ref.get("datasetId") or "") != item["dataset_id"]
                    or str(dataset_ref.get("revisionId") or "") != item["revision_id"]))):
            raise LocalRunInputError("local run input manifest does not match the graph")
        try:
            binding = metadb.catalog_revision_binding(item["dataset_id"])
        except Exception as exc:
            raise LocalRunInputError("local run input revision is unavailable") from exc
        if binding is None:
            raise LocalRunInputError("local run input revision is unavailable")
        uri = str(binding["uri"])
        try:
            adapter = resolve_adapter(uri)
        except Exception as exc:
            raise LocalRunInputError("local run input revision is unavailable") from exc
        if (not isinstance(adapter, DatasetRevisionAdapter)
                or str(getattr(adapter, "name", "") or "") != item["provider"]):
            raise LocalRunInputError("local run input revision is unavailable")
        try:
            with db.base_guard():
                adapter.open_revision(uri, item["revision_id"])
        except Exception as exc:
            raise LocalRunInputError("local run input revision is unavailable") from exc
        config = node.data.setdefault("config", {})
        config["uri"] = uri
        # Keep the complete manifest identity on the private dispatch copy. Revision ids are only
        # provider-local and can restart after a dataset is unregistered/replaced at the same URI;
        # cache/profile keys must therefore include dataset and provider identity as well.
        config["_input_dataset_id"] = item["dataset_id"]
        config["_input_provider"] = item["provider"]
        config["_input_revision_id"] = item["revision_id"]
    return bound
