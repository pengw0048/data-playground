"""Canonical, bounded execution identity for graph-backed Canvas runs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

from hub.deps import CORE_API_VERSION
from hub.models import Graph, WriteIntent

SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTOR_KINDS = 512
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_KEY = re.compile(r"(?:Id|Ref|Uri|[_-](?:id|ref|uri))$")
_DISPLAY_NODE_FIELDS = {"history", "result", "status", "title"}
_MAX_SECRET_REFERENCE_BYTES = 2048
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|password|passwd|private[_-]?key|secret|"
    r"token|access[_-]?token|auth[_-]?token|bearer[_-]?token|session[_-]?token|"
    r"credential|credentials|authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)


class ExecutionManifestError(ValueError):
    """The submitted definition cannot cross the durable manifest boundary."""


def _core_version() -> str:
    try:
        return package_version("data-playground")
    except PackageNotFoundError:
        return "0.1.0"


def _assert_secret_free(value: Any, path: tuple[str, ...] = ()) -> None:
    from hub.secrets import is_secret_ref

    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            reference_match = _REFERENCE_KEY.search(key)
            sensitive_key = (
                key[:reference_match.start()] if reference_match is not None else key)
            if _SENSITIVE_KEY.search(sensitive_key) and child not in (None, ""):
                is_bounded_reference = bool(
                    reference_match is not None
                    and isinstance(child, str)
                    and len(child.encode("utf-8")) <= _MAX_SECRET_REFERENCE_BYTES
                    and child == child.strip()
                    and (
                        reference_match.group().endswith(("Id", "_id", "-id"))
                        or is_secret_ref(child)
                    )
                )
                if not is_bounded_reference:
                    raise ExecutionManifestError(
                        "execution manifest cannot retain sensitive field "
                        f"{'.'.join((*path, key))}")
            _assert_secret_free(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_secret_free(child, (*path, str(index)))


def _canonical_graph(
    graph: Graph,
    admitted_inputs: list[dict[str, str]],
) -> dict[str, Any]:
    admitted = {item["node_id"]: item for item in admitted_inputs}
    nodes: list[dict[str, Any]] = []
    for node in graph.nodes:
        data = {
            key: value
            for key, value in node.data.items()
            if key not in _DISPLAY_NODE_FIELDS
        }
        input_ref = admitted.get(node.id)
        if input_ref is not None:
            config = dict(data.get("config") or {})
            # The admitted dataset/revision pair is the durable source identity.  A local path or
            # provider URL is dispatch-time resolution and must not leak into the manifest.
            config.pop("uri", None)
            config.pop("_inputArtifactUri", None)
            config.pop("_input_artifact_uri", None)
            config["datasetRef"] = {
                "kind": "exact",
                "datasetId": input_ref["dataset_id"],
                "revisionId": input_ref["revision_id"],
            }
            data["config"] = config
        item: dict[str, Any] = {"id": node.id, "type": node.type, "data": data}
        if node.parent_id is not None:
            item["parentId"] = node.parent_id
        nodes.append(item)
    edges = [
        {
            "source": edge.source,
            "target": edge.target,
            "sourceHandle": edge.source_handle,
            "targetHandle": edge.target_handle,
            "data": edge.data.model_dump(by_alias=True, mode="json"),
        }
        for edge in graph.edges
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "requirements": list(graph.requirements),
    }


def _canonical_inputs(items: list[dict[str, str]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items or []:
        if set(item) != {"node_id", "dataset_id", "revision_id", "provider", "resolved_at"}:
            raise ExecutionManifestError("execution manifest input has an invalid field set")
        result.append({
            "nodeId": item["node_id"],
            "datasetId": item["dataset_id"],
            "revisionId": item["revision_id"],
            "provider": item["provider"],
        })
    return result


def _descriptor_snapshot(graph: Graph, deps) -> dict[str, Any]:
    from hub.nodespecs import BUILTIN_NODE_SPECS

    builtin = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    kinds = sorted({node.type for node in graph.nodes})
    if len(kinds) > MAX_DESCRIPTOR_KINDS:
        raise ExecutionManifestError("execution manifest has too many node descriptor kinds")
    nodes: list[dict[str, Any]] = []
    plugin_names: set[str] = set()
    for kind in kinds:
        spec = getattr(deps, "node_specs", {}).get(kind) or builtin.get(kind)
        if spec is None:
            raise ExecutionManifestError(f"execution manifest has no descriptor for node kind '{kind}'")
        raw_descriptor = spec.model_dump(by_alias=True, mode="json")
        # Titles, labels, grouping, help text, and editor visibility are presentation. Retain the
        # bounded execution/validation contract that can actually change how this submitted graph is
        # interpreted, while plugin ownership is captured separately below.
        descriptor = {
            "kind": raw_descriptor["kind"],
            "inputs": [
                {key: value for key, value in port.items() if key != "label"}
                for port in raw_descriptor["inputs"]
            ],
            "outputs": [
                {key: value for key, value in port.items() if key != "label"}
                for port in raw_descriptor["outputs"]
            ],
            "params": [
                {key: value for key, value in param.items()
                 if key not in ("label", "showWhen")}
                for param in raw_descriptor["params"]
            ],
            "canBypass": raw_descriptor["canBypass"],
            "requires": raw_descriptor["requires"],
            "source": raw_descriptor["source"],
        }
        nodes.append(descriptor)
        source = str(descriptor.get("source") or "builtin")
        if source.startswith("plugin:"):
            plugin_names.add(source.removeprefix("plugin:"))
    plugins = []
    by_name = {str(entry.get("name")): entry for entry in getattr(deps, "plugins", [])}
    for name in sorted(plugin_names):
        entry = by_name.get(name, {})
        plugins.append({
            "name": name,
            "package": str(entry.get("package") or name),
            "version": str(entry["version"]) if entry.get("version") is not None else None,
            "source": str(entry.get("source") or "unknown"),
        })
    return {
        "core": {"apiVersion": CORE_API_VERSION, "packageVersion": _core_version()},
        "nodes": nodes,
        "plugins": plugins,
    }


def build_execution_manifest(
    graph: Graph,
    *,
    target_node_id: str | None,
    target_port_id: str | None,
    input_manifest: list[dict[str, str]] | None,
    write_intent: WriteIntent | None,
    deps,
) -> tuple[str, str]:
    """Return ``(semantic_sha256, canonical_json)`` for one admitted definition.

    Canvas/creator/time metadata is deliberately owned by the referencing admission rows.  The
    returned document contains only execution semantics and is therefore safe to content-address.
    """
    canonical_inputs = _canonical_inputs(input_manifest)
    source_inputs = [
        {
            "node_id": item["nodeId"],
            "dataset_id": item["datasetId"],
            "revision_id": item["revisionId"],
            "provider": item["provider"],
        }
        for item in canonical_inputs
    ]
    doc: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "graph": _canonical_graph(graph, source_inputs),
        "target": {"nodeId": target_node_id, "portId": target_port_id},
        "admittedInputs": canonical_inputs,
        "writeIntent": (
            write_intent.model_dump(by_alias=True, mode="json")
            if write_intent is not None else None
        ),
        "descriptors": _descriptor_snapshot(graph, deps),
    }
    # ``parameters`` is intentionally absent until #477 admits typed bindings.
    _assert_secret_free(doc)
    payload = json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ExecutionManifestError(
            f"execution manifest exceeds {MAX_MANIFEST_BYTES} encoded bytes")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


def validate_execution_manifest(sha256: str, payload: str) -> dict[str, Any]:
    """Validate bytes read from the purpose-specific durable relation."""
    if not _SHA256.fullmatch(str(sha256)):
        raise ExecutionManifestError("execution manifest identity is not a SHA-256")
    if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ExecutionManifestError("persisted execution manifest exceeds the durable limit")
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ExecutionManifestError("persisted execution manifest is invalid JSON") from exc
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if canonical != payload:
        raise ExecutionManifestError("persisted execution manifest is not canonical")
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        raise ExecutionManifestError("execution manifest schema version is unsupported")
    _assert_secret_free(doc)
    observed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(observed, sha256):
        raise ExecutionManifestError("execution manifest digest does not match its document")
    return doc
