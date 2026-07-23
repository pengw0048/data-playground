"""Canonical, bounded execution identity for graph-backed Canvas runs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
_URI_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "password", "passwd", "secret", "client_secret",
    "clientsecret", "credential", "credentials", "authorization", "sig",
})
_URI_CREDENTIAL_SUFFIXES = ("token", "signature", "credential", "credentials")


def _is_uri_credential_key(raw_key: str) -> bool:
    """Recognize standard credential query keys with linear-time string operations."""
    normalized = raw_key.casefold().replace("-", "_")
    return (
        normalized in _URI_CREDENTIAL_KEYS
        or normalized.endswith(_URI_CREDENTIAL_SUFFIXES)
    )


class ExecutionManifestError(ValueError):
    """The submitted definition cannot cross the durable manifest boundary."""


def core_package_version() -> str:
    try:
        return package_version("data-playground")
    except PackageNotFoundError:
        return "0.2.3"


def assert_secret_free(
    value: Any,
    path: tuple[str, ...] = (),
    *,
    allow_secret_refs: bool = True,
) -> None:
    """Reject material credentials at the shared durable/export boundary."""
    from hub.secrets import is_registered_secret_ref, is_secret_ref

    if isinstance(value, str):
        if not allow_secret_refs and is_registered_secret_ref(value):
            raise ExecutionManifestError(
                "execution manifest cannot retain a SecretRef at "
                f"{'.'.join(path)}")
        if "://" in value or value.startswith(("file:", "git+", "ssh:")):
            try:
                parsed = urlsplit(value)
                # Accessing ``port`` also rejects malformed bracketed authorities deterministically.
                _ = parsed.port
            except ValueError as exc:
                raise ExecutionManifestError(
                    f"execution manifest contains an invalid URI at {'.'.join(path)}") from exc
            if parsed.username is not None or parsed.password is not None:
                raise ExecutionManifestError(
                    "execution manifest cannot retain URI credentials at "
                    f"{'.'.join(path)}")
            query_parts = [parsed.query]
            if parsed.fragment:
                # OAuth-style credentials commonly live directly in the fragment. Router fragments
                # may put their query after ``?``, so inspect that portion as query data too.
                query_parts.append(parsed.fragment.split("?", 1)[-1])
            if any(
                raw_value not in (None, "") and _is_uri_credential_key(raw_key)
                for part in query_parts
                for raw_key, raw_value in parse_qsl(part, keep_blank_values=True)
            ):
                raise ExecutionManifestError(
                    "execution manifest cannot retain URI credential material at "
                    f"{'.'.join(path)}")
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
            assert_secret_free(
                child, (*path, key), allow_secret_refs=allow_secret_refs)
            if key == "documentJson" and isinstance(child, str):
                try:
                    document = json.loads(child)
                except (TypeError, ValueError):
                    continue
                assert_secret_free(
                    document, (*path, key), allow_secret_refs=allow_secret_refs)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_secret_free(
                child, (*path, str(index)), allow_secret_refs=allow_secret_refs)


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
        # Most titles are editor-only labels, but three existing execution seams consume them:
        # metric emits its title as a value, Write uses it as the destination fallback, and a
        # Section addresses contained children by title. Retain only those semantic titles.
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        if node.type == "transform" and config.get("source") == "library":
            # The exact promoted `(id, version)` is the durable definition. Never retain a stale
            # inline body as a second, hidden persistence or replay path.
            config = dict(config)
            config.pop("code", None)
            data["config"] = config
        semantic_title = bool(
            node.type == "metric"
            or (node.type == "write" and not (config.get("filename") or config.get("name")))
            or node.parent_id is not None
        )
        if semantic_title and "title" in node.data:
            data["title"] = node.data["title"]
        input_ref = admitted.get(node.id)
        if input_ref is not None:
            config = dict(data.get("config") or {})
            # The admitted dataset/revision pair is the durable source identity.  A local path or
            # provider URL is dispatch-time resolution and must not leak into the manifest.
            config.pop("uri", None)
            config.pop("_inputArtifactUri", None)
            config.pop("_input_artifact_uri", None)
            config.pop("_input_provider_uri", None)
            config.pop("_input_provider_preview_uri", None)
            # A provider placement is display/navigation context only. The admitted exact ref is
            # the canonical execution identity and must not retain its originating occurrence.
            config.pop("providerResourceRef", None)
            config.pop("providerMountId", None)
            config.pop("providerName", None)
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
        # Package declaration order does not change the environment contract (the execution/cache
        # identity already treats it as a set), so keep harmless editor reordering digest-stable.
        "requirements": sorted(graph.requirements),
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


def descriptor_snapshot(graph: Graph, deps) -> dict[str, Any]:
    """Return the one canonical core/node/plugin compatibility identity."""
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
        "core": {"apiVersion": CORE_API_VERSION, "packageVersion": core_package_version()},
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
        "descriptors": descriptor_snapshot(graph, deps),
    }
    if graph._parameter_bindings:
        doc["parameters"] = graph._parameter_bindings
    assert_secret_free(doc)
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
    parameters = doc.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, list) or len(parameters) > 128:
            raise ExecutionManifestError("execution manifest parameters are invalid")
        from hub.run_parameters import (
            ParameterResolutionError, validate_canonical_parameter_binding,
        )
        names: set[str] = set()
        for item in parameters:
            try:
                canonical_item = validate_canonical_parameter_binding(item)
            except ParameterResolutionError as exc:
                raise ExecutionManifestError(
                    "execution manifest parameters are invalid") from exc
            if canonical_item["name"] in names:
                raise ExecutionManifestError("execution manifest parameters are invalid")
            names.add(canonical_item["name"])
    assert_secret_free(doc)
    observed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(observed, sha256):
        raise ExecutionManifestError("execution manifest digest does not match its document")
    return doc


def execution_manifest_admission(sha256: str, payload: str) -> dict[str, Any]:
    """Reopen the executable admission encoded by one canonical manifest.

    The canonical graph deliberately omits editor identity, positions, and edge IDs. Deterministic
    private values rebuild them, except a managed Write's provenance restores the producer identity
    needed by its execution fence. Admission timestamps are non-semantic and use one fixed value.
    """
    doc = validate_execution_manifest(sha256, payload)
    graph_doc = doc.get("graph")
    target = doc.get("target")
    admitted = doc.get("admittedInputs")
    if (not isinstance(graph_doc, dict)
            or set(graph_doc) != {"nodes", "edges", "requirements"}
            or not isinstance(graph_doc.get("nodes"), list)
            or not isinstance(graph_doc.get("edges"), list)
            or not isinstance(graph_doc.get("requirements"), list)
            or not isinstance(target, dict)
            or set(target) != {"nodeId", "portId"}
            or not isinstance(admitted, list)):
        raise ExecutionManifestError("execution manifest admission is invalid")
    edges: list[dict[str, Any]] = []
    for index, edge in enumerate(graph_doc["edges"]):
        if not isinstance(edge, dict):
            raise ExecutionManifestError("execution manifest graph edge is invalid")
        edges.append({"id": f"manifest-edge-{index}", **edge})
    raw_write_intent = doc.get("writeIntent")
    publication = (
        raw_write_intent.get("provenance", {}).get("publication", {})
        if isinstance(raw_write_intent, dict) else {})
    graph_id = publication.get("producer") or f"execution-manifest-{sha256[:32]}"
    graph_version = publication.get("producerVersion") or 1
    try:
        graph = Graph.model_validate({
            "id": graph_id,
            "version": graph_version,
            "nodes": graph_doc["nodes"],
            "edges": edges,
            "requirements": graph_doc["requirements"],
        })
    except ValueError as exc:
        raise ExecutionManifestError("execution manifest graph is invalid") from exc

    inputs: list[dict[str, str]] = []
    for item in admitted:
        if not isinstance(item, dict) or set(item) != {
            "nodeId", "datasetId", "revisionId", "provider",
        } or any(not isinstance(value, str) or not value for value in item.values()):
            raise ExecutionManifestError("execution manifest admitted inputs are invalid")
        inputs.append({
            "node_id": item["nodeId"],
            "dataset_id": item["datasetId"],
            "revision_id": item["revisionId"],
            "provider": item["provider"],
            "resolved_at": "1970-01-01T00:00:00+00:00",
        })
    try:
        write_intent = (
            WriteIntent.model_validate(raw_write_intent).model_dump(by_alias=True, mode="json")
            if raw_write_intent is not None else None
        )
    except ValueError as exc:
        raise ExecutionManifestError("execution manifest write intent is invalid") from exc
    node_id, port_id = target.get("nodeId"), target.get("portId")
    if (node_id is not None and not isinstance(node_id, str)) or (
            port_id is not None and not isinstance(port_id, str)):
        raise ExecutionManifestError("execution manifest target is invalid")
    if write_intent is not None and node_id is not None:
        target_node = next((node for node in graph.nodes if node.id == node_id), None)
        if target_node is None:
            raise ExecutionManifestError("execution manifest target is absent from its graph")
        config = dict(target_node.data.get("config") or {})
        config["_admittedWriteIntent"] = write_intent
        target_node.data["config"] = config
    return {
        "graph_doc": graph.model_dump(by_alias=True, mode="json"),
        "input_manifest": inputs,
        "write_intent": write_intent,
        "target_node_id": node_id,
        "target_port_id": port_id,
        "parameters": doc.get("parameters"),
    }


def execution_manifest_accepts_graph_replay(
    sha256: str,
    payload: str,
    graph: Graph,
    *,
    target_node_id: str | None,
    target_port_id: str | None,
) -> bool:
    """Compare replayed graph semantics without consulting mutable source state.

    The retained exact inputs and compatibility descriptors remain authoritative. Re-canonicalizing
    only the caller-controlled graph and target catches a retargeted submission while allowing the
    original response-loss retry after a source alias or installed package has moved.
    """
    doc = validate_execution_manifest(sha256, payload)
    admitted = doc.get("admittedInputs")
    if not isinstance(admitted, list):
        raise ExecutionManifestError("execution manifest admitted inputs are invalid")
    source_inputs: list[dict[str, str]] = []
    for item in admitted:
        if not isinstance(item, dict) or set(item) != {
            "nodeId", "datasetId", "revisionId", "provider",
        } or any(not isinstance(value, str) or not value for value in item.values()):
            raise ExecutionManifestError("execution manifest admitted inputs are invalid")
        source_inputs.append({
            "node_id": item["nodeId"],
            "dataset_id": item["datasetId"],
            "revision_id": item["revisionId"],
            "provider": item["provider"],
        })
    def parameter_intent(value):
        if not isinstance(value, list):
            return value
        result = []
        for item in value:
            if not isinstance(item, dict):
                return value
            copied = dict(item)
            binding = copied.get("value")
            if isinstance(binding, dict) and binding.get("kind") == "latest":
                copied["value"] = {
                    key: child for key, child in binding.items()
                    if key != "resolvedRevisionId"
                }
            result.append(copied)
        return result

    return bool(
        doc.get("graph") == _canonical_graph(graph, source_inputs)
        and doc.get("target") == {
            "nodeId": target_node_id, "portId": target_port_id,
        }
        and parameter_intent(doc.get("parameters"))
        == parameter_intent(graph._parameter_bindings or None)
    )
