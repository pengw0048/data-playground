"""One-way Canvas copies from a persisted document or retained execution manifest."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from hub import execution_manifest, native_canvas
from hub.models import Graph
from hub.secrets import is_registered_secret_ref


class CanvasCopyError(ValueError):
    pass


def canvas_id(uid: str, copy_id: str) -> str:
    try:
        normalized = str(UUID(copy_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CanvasCopyError("copyId must be a UUID") from exc
    digest = hashlib.sha256(f"canvas-copy-v1\0{uid}\0{normalized}".encode()).hexdigest()
    return f"canvas-copy-{digest[:32]}"


def _strip_secret_refs(value: Any) -> tuple[Any, int]:
    if is_registered_secret_ref(value):
        return None, 1
    if isinstance(value, list):
        stripped, removed = [], 0
        for item in value:
            child, count = _strip_secret_refs(item)
            removed += count
            if count == 0 or child is not None:
                stripped.append(child)
        return stripped, removed
    if isinstance(value, dict):
        stripped, removed = {}, 0
        for key, item in value.items():
            reference = execution_manifest._REFERENCE_KEY.search(str(key))
            sensitive_key = str(key)[:reference.start()] if reference is not None else str(key)
            if item not in (None, "") and execution_manifest._SENSITIVE_KEY.search(sensitive_key):
                removed += 1
                continue
            child, count = _strip_secret_refs(item)
            removed += count
            if count == 0 or child is not None:
                stripped[key] = child
        return stripped, removed
    return value, 0


def _manifest_canvas(document: dict[str, Any], name: str) -> tuple[dict[str, Any], int]:
    graph = document.get("graph")
    if not isinstance(graph, dict):
        raise CanvasCopyError("retained execution manifest has no graph definition")
    raw_nodes, raw_edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise CanvasCopyError("retained execution manifest graph is invalid")
    nodes = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise CanvasCopyError("retained execution manifest has an invalid node")
        node = copy.deepcopy(raw)
        node.setdefault("position", {"x": 160 + (index % 5) * 220, "y": 160 + (index // 5) * 140})
        data = node.setdefault("data", {})
        if not isinstance(data, dict):
            raise CanvasCopyError("retained execution manifest has invalid node data")
        data["status"] = "draft"
        nodes.append(node)
    edges = []
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise CanvasCopyError("retained execution manifest has an invalid edge")
        edges.append({"id": f"manifest-edge-{index}", **copy.deepcopy(raw)})
    canvas, removed = _strip_secret_refs({
        "name": name, "nodes": nodes, "edges": edges,
        "requirements": copy.deepcopy(graph.get("requirements", [])), "parameters": [],
    })
    return canvas, removed


def prepare_current(document: dict[str, Any], name: str) -> tuple[dict[str, Any], int]:
    sanitized, removed = _strip_secret_refs(document)
    try:
        canvas = native_canvas._portable_canvas(sanitized)
    except native_canvas.NativeCanvasError as exc:
        raise CanvasCopyError(str(exc)) from exc
    canvas["name"] = name
    return canvas, removed


def prepare_manifest(document: dict[str, Any], name: str) -> tuple[dict[str, Any], int]:
    return _manifest_canvas(document, name)


def diagnostics(canvas: dict[str, Any], deps: Any, uid: str,
                *, descriptors: dict[str, Any] | None = None,
                stripped_credentials: int = 0,
                retained_parameters: bool = False) -> list[native_canvas.Diagnostic]:
    result: list[native_canvas.Diagnostic] = []
    try:
        native_canvas._assert_secret_free(canvas)
        graph = Graph.model_validate({"id": "canvas-copy", "version": 1, **canvas})
    except (native_canvas.NativeCanvasError, ValidationError, ValueError) as exc:
        return [native_canvas.Diagnostic("invalid_copy", "error", str(exc))]
    if stripped_credentials:
        result.append(native_canvas.Diagnostic(
            "credentials_removed", "warning",
            "Credential references were removed. Select credentials you are authorized to use before running."))
    if retained_parameters:
        result.append(native_canvas.Diagnostic(
            "parameters_applied", "warning",
            "Recorded parameter bindings are already applied to the cloned node configuration."))
    result.extend(native_canvas._requirements_diagnostics(canvas.get("requirements", [])))
    executable = [node for node in graph.nodes if node.type not in {"note", "code"}]
    missing = sorted({node.type for node in executable if node.type not in deps.node_specs})
    for kind in missing:
        result.append(native_canvas.Diagnostic(
            "node_unavailable", "warning",
            f"Node kind '{kind}' is unavailable. Install its compatible plugin before running."))
    available = {node.id for node in executable if node.type in deps.node_specs}
    subset = {**canvas, "nodes": [node for node in canvas["nodes"] if node.get("id") in available]}
    for item in native_canvas._config_diagnostics(subset, deps.node_specs):
        result.append(native_canvas.Diagnostic(item.code, "warning", item.message, item.path))
    if descriptors and isinstance(descriptors.get("plugins"), list):
        installed = {str(item.get("name")): item for item in getattr(deps, "plugins", [])}
        for required in descriptors["plugins"]:
            if not isinstance(required, dict):
                continue
            active = installed.get(str(required.get("name")))
            if active is None or any(active.get(key) != required.get(key) for key in ("version", "source")):
                result.append(native_canvas.Diagnostic(
                    "plugin_unavailable", "warning",
                    f"Plugin '{required.get('name')}' does not match the retained version/source. Install a compatible version before running."))
    for node in canvas["nodes"]:
        config = node.get("data", {}).get("config", {})
        if (node.get("type") == "transform" and isinstance(config, dict)
                and config.get("source") == "library"):
            try:
                deps.registry.get(str(config.get("processor")), str(config.get("version")))
            except KeyError:
                result.append(native_canvas.Diagnostic(
                    "transform_unavailable", "warning",
                    f"Transform '{config.get('processor')}' exact version '{config.get('version')}' is unavailable.",
                    f"canvas.nodes.{node.get('id')}"))
    result.extend(native_canvas._data_diagnostics(canvas))
    return result[:native_canvas.MAX_DIAGNOSTICS]


def intent_digest(source: dict[str, Any], destination: dict[str, Any], canvas: dict[str, Any]) -> str:
    payload = json.dumps({"source": source, "destination": destination, "canvas": canvas},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def request_digest(*, source_canvas_id: str, source_canvas_version: int | None,
                   source_subject_id: str | None, container_id: str,
                   container_version: int, name: str) -> str:
    payload = json.dumps({
        "sourceCanvasId": source_canvas_id,
        "sourceCanvasVersion": source_canvas_version,
        "sourceSubjectId": source_subject_id,
        "containerId": container_id,
        "containerVersion": container_version,
        "name": name.strip(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def validation_digest(intent: str, items: list[native_canvas.Diagnostic]) -> str:
    payload = json.dumps({"intent": intent, "diagnostics": [item.wire() for item in items]},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def validation_matches(supplied: str, intent: str,
                       items: list[native_canvas.Diagnostic]) -> bool:
    return bool(supplied and secrets.compare_digest(supplied, validation_digest(intent, items)))
