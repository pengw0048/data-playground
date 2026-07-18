"""Bounded, versioned Data Playground Canvas interchange.

This is deliberately a native document format, not a generic pipeline format.  It carries only
the graph a supported Data Playground workspace can understand plus the identities needed to say
why another workspace cannot run it.  Credentials, results and execution history never enter this
module's output.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import ValidationError

from hub import execution_manifest, graph as graph_mod, metadb
from hub.models import Graph
from hub.settings import settings

FORMAT = "dataplay.native-canvas"
VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_BYTES + 8192
MAX_DIAGNOSTICS = 100
FILENAME_SUFFIX = ".dp-canvas.json"
_ENVELOPE_KEYS = frozenset({
    "format", "version", "canvas", "descriptors", "dataReferences", "libraryProcessors",
})
_CANVAS_KEYS = frozenset({"name", "nodes", "edges", "requirements", "parameters"})
_NODE_KEYS = frozenset({"id", "type", "position", "data", "parentId"})
_EDGE_KEYS = frozenset({"id", "source", "target", "sourceHandle", "targetHandle", "data"})
_NODE_DATA_KEYS = frozenset({"title", "status", "config", "bypassed", "disabled"})
_NODE_DATA_RUNTIME_FIELDS = frozenset({"history", "lastRun", "meta", "result"})
_CORE_CONFIG_KEYS = frozenset({
    "uri", "tableId", "datasetRef", "providerResourceRef", "providerMountId", "providerName", "providerReadMode",
    "delimiter", "header", "n", "seed", "method", "predicate", "select", "columns", "source", "processor",
    "version", "params", "code", "io", "mode", "onError", "scope", "outputSchema", "outputSchemaSource",
    "outputSchemaCodeHash", "on", "how", "sql", "agg", "column", "chartType", "x", "y", "name", "writeMode",
    "partitionBy", "filename", "destination", "aggs", "by", "align", "count", "k", "lang", "markdown", "script",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NativeCanvasError(ValueError):
    """A non-sensitive reason a native document is not safe to import."""


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    path: str | None = None

    def wire(self) -> dict[str, str]:
        result = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.path:
            result["path"] = self.path
        return result


def filename_for(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip(".-")[:96] or "canvas"
    return f"{safe}{FILENAME_SUFFIX}"


def import_canvas_id(uid: str, import_id: str) -> str:
    try:
        normalized_import_id = str(UUID(import_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise NativeCanvasError("importId must be a UUID") from exc
    digest = hashlib.sha256(
        f"native-canvas-import-v1\0{uid}\0{normalized_import_id}".encode()).hexdigest()
    return f"native-import-{digest[:32]}"


def _reject_unknown(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise NativeCanvasError(f"{label} contains unsupported field(s): {', '.join(unknown[:5])}")


def _assert_secret_free(value: Any) -> None:
    try:
        execution_manifest.assert_secret_free(
            value, ("nativeCanvas",), allow_secret_refs=False)
    except execution_manifest.ExecutionManifestError as exc:
        raise NativeCanvasError(
            "native Canvas contains credentials or a SecretRef and cannot cross the export boundary"
        ) from exc

    def reject_local_outputs(child: Any, path: str) -> None:
        if isinstance(child, str) and ".dp-results" in child and "__result_" in child:
            raise NativeCanvasError(f"{path} references a local run output; outputs cannot be exported")
        if isinstance(child, list):
            for index, item in enumerate(child):
                reject_local_outputs(item, f"{path}[{index}]")
        elif isinstance(child, dict):
            for key, item in child.items():
                reject_local_outputs(item, f"{path}.{key}")

    reject_local_outputs(value, "canvas")


def _portable_canvas(doc: dict[str, Any]) -> dict[str, Any]:
    # Canvas IDs and persisted versions are workspace-local identities.  Dropping runtime state also
    # avoids claiming exported output/history is available to the recipient.
    result: dict[str, Any] = {
        "name": str(doc.get("name") or "untitled"),
        "nodes": [], "edges": doc.get("edges", []),
        "requirements": doc.get("requirements", []),
        "parameters": doc.get("parameters", []),
    }
    for raw in doc.get("nodes", []):
        if not isinstance(raw, dict):
            raise NativeCanvasError("canvas has an invalid node")
        node = {key: raw[key] for key in _NODE_KEYS if key in raw}
        data = node.get("data")
        if not isinstance(data, dict):
            raise NativeCanvasError(f"node '{raw.get('id', '?')}' has invalid data")
        # These fields describe local execution history, not the source document.
        # Keep only fields with established document semantics.  Unknown node-data fields are not
        # carried because a plugin could make them execution-affecting in another workspace.
        unknown = sorted(set(data) - _NODE_DATA_KEYS - _NODE_DATA_RUNTIME_FIELDS)
        if unknown:
            raise NativeCanvasError(
                f"node '{raw.get('id', '?')}' has unsupported execution-affecting data: "
                f"{', '.join(unknown[:5])}")
        node["data"] = {
            key: value for key, value in data.items()
            if key in _NODE_DATA_KEYS and key not in _NODE_DATA_RUNTIME_FIELDS
        }
        node["data"]["status"] = "draft"
        result["nodes"].append(node)
    _assert_secret_free(result)
    return result


def _is_annotation(node: dict[str, Any]) -> bool:
    return node.get("type") in {"note", "code"}


def _execution_graph(canvas: dict[str, Any]) -> Graph:
    nodes = [node for node in canvas["nodes"] if not _is_annotation(node)]
    ids = {node["id"] for node in nodes}
    edges = [edge for edge in canvas["edges"] if edge["source"] in ids and edge["target"] in ids]
    return Graph.model_validate({**canvas, "nodes": nodes, "edges": edges})


def _data_references(canvas: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for node in canvas["nodes"]:
        if node.get("type") != "source":
            continue
        config = node.get("data", {}).get("config", {})
        if not isinstance(config, dict):
            continue
        intent = {key: config[key] for key in (
            "uri", "tableId", "datasetRef", "providerResourceRef", "providerMountId", "providerName", "providerReadMode",
        ) if key in config}
        if intent:
            refs.append({"nodeId": node.get("id"), "intent": intent})
    return refs


def _library_processors(canvas: dict[str, Any], registry: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in canvas["nodes"]:
        config = node.get("data", {}).get("config", {})
        if (node.get("type") != "transform" or not isinstance(config, dict)
                or config.get("source") != "library"):
            continue
        processor, version = config.get("processor"), config.get("version")
        if not isinstance(processor, str) or not isinstance(version, str):
            raise NativeCanvasError(
                f"library Transform '{node.get('id', '?')}' must name an exact processor and version")
        try:
            descriptor = registry.get(processor, version).descriptor().model_dump(
                by_alias=True, exclude_none=True, mode="json")
        except KeyError as exc:
            raise NativeCanvasError(
                f"library processor '{processor}' exact version '{version}' is unavailable") from exc
        result.append({
            "nodeId": node["id"], "processor": processor, "version": version,
            "descriptor": descriptor,
        })
    return result


def export_envelope(doc: dict[str, Any], deps: Any) -> dict[str, Any]:
    canvas = _portable_canvas(doc)
    requirement_errors = [
        item for item in _requirements_diagnostics(canvas.get("requirements", []))
        if item.severity == "error"
    ]
    if requirement_errors:
        raise NativeCanvasError(requirement_errors[0].message)
    config_errors = _config_diagnostics(canvas, deps.node_specs)
    if config_errors:
        raise NativeCanvasError(config_errors[0].message)
    kinds = sorted({str(node.get("type")) for node in canvas["nodes"] if not _is_annotation(node)})
    missing = [kind for kind in kinds if kind not in deps.node_specs]
    if missing:
        raise NativeCanvasError(f"canvas uses unavailable node kind(s): {', '.join(missing[:5])}")
    graph = _execution_graph(canvas)
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "canvas": canvas,
        "descriptors": execution_manifest.descriptor_snapshot(graph, deps),
        "dataReferences": _data_references(canvas),
        "libraryProcessors": _library_processors(canvas, deps.registry),
    }
    _assert_secret_free(envelope)
    if canonical_size(envelope) > MAX_BYTES:
        raise NativeCanvasError("native Canvas export exceeds the 2 MiB limit")
    return envelope


def parse_envelope(value: Any, *, filename: str) -> dict[str, Any]:
    if not isinstance(filename, str) or not filename.lower().endswith(FILENAME_SUFFIX):
        raise NativeCanvasError(f"filename must end in {FILENAME_SUFFIX}")
    if not isinstance(value, dict):
        raise NativeCanvasError("native Canvas document must be a JSON object")
    _reject_unknown(value, _ENVELOPE_KEYS, "native Canvas envelope")
    if value.get("format") != FORMAT:
        raise NativeCanvasError("file is not a Data Playground native Canvas document")
    if value.get("version") != VERSION:
        raise NativeCanvasError(f"unsupported native Canvas version {value.get('version')!r}; supported version is {VERSION}")
    canvas = value.get("canvas")
    descriptors = value.get("descriptors")
    refs = value.get("dataReferences")
    processors = value.get("libraryProcessors")
    if (not isinstance(canvas, dict) or not isinstance(descriptors, dict)
            or not isinstance(refs, list) or not isinstance(processors, list)):
        raise NativeCanvasError(
            "native Canvas envelope is missing canvas, descriptors, dataReferences, or libraryProcessors")
    _reject_unknown(canvas, _CANVAS_KEYS, "native Canvas payload")
    required_canvas_fields = {"name", "nodes", "edges", "requirements", "parameters"}
    if not required_canvas_fields.issubset(canvas):
        raise NativeCanvasError("native Canvas payload is missing one of name, nodes, edges, requirements, or parameters")
    if not isinstance(canvas["name"], str):
        raise NativeCanvasError("native Canvas name must be a string")
    for raw in canvas.get("nodes", []):
        if not isinstance(raw, dict):
            raise NativeCanvasError("native Canvas payload has an invalid node")
        _reject_unknown(raw, _NODE_KEYS, "native Canvas node")
        data = raw.get("data")
        if not isinstance(data, dict):
            raise NativeCanvasError("native Canvas node data must be an object")
        _reject_unknown(data, _NODE_DATA_KEYS, "native Canvas node data")
    for raw in canvas.get("edges", []):
        if not isinstance(raw, dict):
            raise NativeCanvasError("native Canvas payload has an invalid edge")
        _reject_unknown(raw, _EDGE_KEYS, "native Canvas edge")
    _assert_secret_free(value)
    try:
        graph = Graph.model_validate({"id": "native-import", "version": 1, **canvas})
    except ValidationError as exc:
        raise NativeCanvasError(f"native Canvas graph is invalid: {exc.errors()[0]['msg']}") from exc
    normalized = graph.model_dump(by_alias=True, mode="json")
    normalized.pop("id", None)
    normalized.pop("version", None)
    # Name is a document/UI field rather than an execution Graph field, so preserve it outside the
    # Graph model after the graph's nodes, edges, requirements and parameters were normalized.
    normalized["name"] = canvas["name"]
    return {
        "canvas": normalized, "descriptors": descriptors, "dataReferences": refs,
        "libraryProcessors": processors,
    }


def _requirements_diagnostics(requirements: Iterable[Any]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for index, raw in enumerate(requirements):
        if not isinstance(raw, str) or len(raw) > 512:
            result.append(Diagnostic("invalid_requirement", "error", "Requirement must be a bounded PEP 508 string", f"canvas.requirements[{index}]"))
            continue
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            result.append(Diagnostic("invalid_requirement", "error", f"Invalid requirement '{raw}'", f"canvas.requirements[{index}]"))
            continue
        if requirement.url is not None:
            try:
                parsed = urlsplit(requirement.url)
                _ = parsed.port
            except ValueError:
                result.append(Diagnostic(
                    "invalid_requirement", "error", f"Invalid direct URL requirement '{raw}'",
                    f"canvas.requirements[{index}]"))
                continue
            if len(requirement.url.encode("utf-8")) > 512 or not parsed.scheme:
                result.append(Diagnostic(
                    "invalid_requirement", "error", "Direct requirement URL must be bounded and absolute",
                    f"canvas.requirements[{index}]"))
                continue
            try:
                execution_manifest.assert_secret_free(
                    requirement.url, ("canvas", "requirements", str(index)),
                    allow_secret_refs=False)
            except execution_manifest.ExecutionManifestError:
                result.append(Diagnostic(
                    "invalid_requirement", "error",
                    "Direct requirement URL cannot contain credentials or a SecretRef",
                    f"canvas.requirements[{index}]"))
    if requirements and not settings.canvas_pip_deps:
        result.append(Diagnostic("requirements_unavailable", "warning", "This workspace disables per-Canvas dependency installation; use a compatible pre-baked environment."))
    return result


def _exact_dataset_diagnostic(ref: dict[str, Any], path: str) -> Diagnostic | None:
    dataset_id, revision_id = ref.get("datasetId"), ref.get("revisionId")
    if not isinstance(dataset_id, str) or not isinstance(revision_id, str):
        return None  # Graph validation reports malformed DatasetRefs as blocking errors.
    if (metadb.managed_local_file_revision_artifact(dataset_id, revision_id) is not None
            or metadb.local_file_input_revision_artifact(dataset_id, revision_id) is not None):
        return None
    binding = metadb.catalog_revision_binding(dataset_id)
    if binding is None:
        message = (
            f"Dataset '{dataset_id}' exact revision '{revision_id}' is unavailable here. "
            "Relink it before running.")
    else:
        message = (
            f"Dataset '{dataset_id}' is registered, but exact revision '{revision_id}' cannot be "
            "proven available without contacting its adapter. Confirm before import and relink if needed.")
    return Diagnostic("data_unavailable", "warning", message, path)


def _data_diagnostics(canvas: dict[str, Any]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for node in canvas["nodes"]:
        if node.get("type") != "source":
            continue
        config = node.get("data", {}).get("config", {})
        if not isinstance(config, dict):
            continue
        path = f"canvas.nodes.{node['id']}"
        ref = config.get("datasetRef")
        selected = ref.get("resolved") if isinstance(ref, dict) and ref.get("kind") == "as_of" else ref
        if isinstance(selected, dict) and selected.get("kind") == "exact":
            diagnostic = _exact_dataset_diagnostic(selected, path)
            if diagnostic is not None:
                result.append(diagnostic)
        uri = config.get("uri")
        if isinstance(uri, str):
            entry = metadb.catalog_get(uri)
            if entry is None or entry.get("missing"):
                result.append(Diagnostic(
                    "uri_unavailable", "warning",
                    f"Source '{node['id']}' URI intent is not available in this workspace. Relink it before running.",
                    path))
            elif not isinstance(selected, dict):
                result.append(Diagnostic(
                    "uri_availability_unproven", "warning",
                    f"Source '{node['id']}' URI is registered, but availability is not probed before warning acknowledgement.",
                    path))
        if any(key in config for key in (
                "providerResourceRef", "providerMountId", "providerName", "providerReadMode")):
            result.append(Diagnostic(
                "provider_availability_unproven", "warning",
                f"Source '{node['id']}' keeps provider intent, but this import does not contact that provider before warning acknowledgement.",
                path))
    for index, declaration in enumerate(canvas.get("parameters", [])):
        if not isinstance(declaration, dict) or declaration.get("type") != "dataset":
            continue
        default = declaration.get("default")
        path = f"canvas.parameters[{index}].default"
        if isinstance(default, dict) and default.get("kind") == "exact":
            diagnostic = _exact_dataset_diagnostic(default, path)
            if diagnostic is not None:
                result.append(diagnostic)
        elif isinstance(default, dict) and default.get("kind") == "latest":
            result.append(Diagnostic(
                "data_availability_unproven", "warning",
                f"Dataset parameter '{declaration.get('name', index)}' resolves latest only at execution; availability is not probed before warning acknowledgement.",
                path))
    return result


def _config_diagnostics(canvas: dict[str, Any], node_specs: dict[str, Any]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for node in canvas["nodes"]:
        if _is_annotation(node):
            continue
        config = node.get("data", {}).get("config", {})
        if not isinstance(config, dict):
            result.append(Diagnostic("invalid_config", "error", f"Node '{node['id']}' configuration must be an object."))
            continue
        spec = node_specs.get(node["type"])
        params = list(getattr(spec, "params", [])) if spec is not None else []
        declared = {str(param.name) for param in params}
        unknown = sorted(set(config) - _CORE_CONFIG_KEYS - declared)
        if unknown:
            result.append(Diagnostic("unsupported_config", "error", f"Node '{node['id']}' has execution-affecting config not declared by its active node descriptor: {', '.join(unknown[:5])}. Update the compatible plugin before importing."))
        for param in params:
            show_when = getattr(param, "show_when", None)
            if (isinstance(show_when, dict)
                    and config.get(show_when.get("param")) not in show_when.get("in", [])):
                continue
            value = config.get(param.name)
            if value is None:
                if getattr(param, "required", False):
                    result.append(Diagnostic(
                        "invalid_config", "error",
                        f"Node '{node['id']}' parameter '{param.name}' is required.",
                        f"canvas.nodes.{node['id']}.data.config.{param.name}"))
                continue
            param_type = getattr(param, "type", None)
            valid = True
            if param_type in {"string", "text", "code", "select"}:
                valid = isinstance(value, str)
            elif param_type == "bool":
                valid = type(value) is bool
            elif param_type == "int":
                valid = type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1
            elif param_type == "float":
                try:
                    valid = type(value) in {int, float} and math.isfinite(value)
                except OverflowError:
                    valid = False
            elif param_type == "columns":
                valid = (isinstance(value, list)
                         and all(isinstance(column, str) and column.strip()
                                 for column in value))
            if not valid:
                result.append(Diagnostic(
                    "invalid_config", "error",
                    f"Node '{node['id']}' parameter '{param.name}' must have type '{param_type}'.",
                    f"canvas.nodes.{node['id']}.data.config.{param.name}"))
                continue
            if (param_type == "select" and getattr(param, "options", None) is not None
                    and value not in param.options):
                result.append(Diagnostic(
                    "invalid_config", "error",
                    f"Node '{node['id']}' parameter '{param.name}' must be one of: "
                    f"{', '.join(param.options)}.",
                    f"canvas.nodes.{node['id']}.data.config.{param.name}"))
            if (getattr(param, "required", False)
                    and ((isinstance(value, str) and not value.strip())
                         or (isinstance(value, list) and not value))):
                result.append(Diagnostic(
                    "invalid_config", "error",
                    f"Node '{node['id']}' parameter '{param.name}' is required.",
                    f"canvas.nodes.{node['id']}.data.config.{param.name}"))
    return result


def _library_diagnostics(parsed: dict[str, Any], registry: Any) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    exported = {
        (item.get("nodeId"), item.get("processor"), item.get("version")): item
        for item in parsed["libraryProcessors"] if isinstance(item, dict)
    }
    expected: set[tuple[Any, Any, Any]] = set()
    for node in parsed["canvas"]["nodes"]:
        config = node.get("data", {}).get("config", {})
        if (node.get("type") != "transform" or not isinstance(config, dict)
                or config.get("source") != "library"):
            continue
        identity = (node.get("id"), config.get("processor"), config.get("version"))
        expected.add(identity)
        item = exported.get(identity)
        if item is None:
            continue
        processor, version = identity[1], identity[2]
        try:
            descriptor = registry.get(str(processor), str(version)).descriptor().model_dump(
                by_alias=True, exclude_none=True, mode="json")
        except KeyError:
            result.append(Diagnostic(
                "library_processor_unavailable", "warning",
                f"Library processor '{processor}' exact version '{version}' is unavailable. "
                "The imported Canvas remains truthful but cannot run this node until it is installed.",
                f"canvas.nodes.{identity[0]}"))
            continue
        if item.get("descriptor") != descriptor:
            result.append(Diagnostic(
                "library_processor_incompatible", "error",
                f"Library processor '{processor}' exact version '{version}' has a different descriptor in this workspace.",
                f"canvas.nodes.{identity[0]}"))
    if set(exported) != expected or len(exported) != len(parsed["libraryProcessors"]):
        result.append(Diagnostic(
            "library_processor_mismatch", "error",
            "The library processor identity list does not exactly match the graph's exact references."))
    return result


def diagnostics(parsed: dict[str, Any], deps: Any, uid: str) -> list[Diagnostic]:
    canvas = parsed["canvas"]
    result = _requirements_diagnostics(canvas.get("requirements", []))
    graph = _execution_graph(canvas)
    try:
        current_descriptors = execution_manifest.descriptor_snapshot(graph, deps)
    except execution_manifest.ExecutionManifestError as exc:
        result.append(Diagnostic("missing_node", "error", str(exc)))
    else:
        exported_descriptors = parsed["descriptors"]
        if exported_descriptors.get("core") != current_descriptors["core"]:
            result.append(Diagnostic(
                "incompatible_core", "error",
                "This native Canvas targets a different core API or package version."))
        if exported_descriptors.get("plugins") != current_descriptors["plugins"]:
            result.append(Diagnostic(
                "incompatible_plugin", "error",
                "The required plugin package, version, or source identities differ in this workspace."))
        if exported_descriptors.get("nodes") != current_descriptors["nodes"]:
            result.append(Diagnostic(
                "incompatible_node", "error",
                "One or more node execution descriptors differ in this workspace."))
        if set(exported_descriptors) != {"core", "nodes", "plugins"}:
            result.append(Diagnostic(
                "descriptor_mismatch", "error", "The descriptor snapshot has unsupported fields."))
    expected_refs = _data_references(canvas)
    if parsed["dataReferences"] != expected_refs:
        result.append(Diagnostic(
            "data_reference_mismatch", "error",
            "The data reference identity list does not exactly match the graph's source intent."))
    invalid = graph_mod.validation_error(graph, deps.node_specs, deps.node_builders)
    if invalid:
        result.append(Diagnostic("invalid_graph", "error", invalid[0]))
    result.extend(_config_diagnostics(canvas, deps.node_specs))
    result.extend(_library_diagnostics(parsed, deps.registry))
    try:
        metadb.require_promoted_transform_use(uid, canvas)
    except PermissionError as exc:
        result.append(Diagnostic(
            "promoted_transform_unavailable", "error", str(exc)))
    result.extend(_data_diagnostics(canvas))
    return result[:MAX_DIAGNOSTICS]


def summary(parsed: dict[str, Any], diagnostics_: list[Diagnostic]) -> dict[str, Any]:
    canvas = parsed["canvas"]
    return {
        "name": canvas.get("name") or "untitled",
        "nodeCount": len(canvas["nodes"]),
        "edgeCount": len(canvas["edges"]),
        "requirements": canvas.get("requirements", []),
        "parameters": canvas.get("parameters", []),
        "diagnostics": [item.wire() for item in diagnostics_],
        "canImport": not any(item.severity == "error" for item in diagnostics_),
        "requiresConfirmation": any(item.severity == "warning" for item in diagnostics_),
        "validationDigest": validation_digest(parsed, diagnostics_),
    }


def validation_digest(parsed: dict[str, Any], diagnostics_: list[Diagnostic]) -> str:
    canonical = json.dumps({
        "envelopeDigest": import_intent_digest(parsed),
        "diagnostics": [item.wire() for item in diagnostics_],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def import_intent_digest(parsed: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"format": FORMAT, "version": VERSION, **parsed},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def validation_digest_matches(
    supplied: str | None,
    parsed: dict[str, Any],
    diagnostics_: list[Diagnostic],
) -> bool:
    return bool(
        supplied is not None and _SHA256.fullmatch(supplied)
        and secrets.compare_digest(supplied, validation_digest(parsed, diagnostics_)))


def canonical_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
