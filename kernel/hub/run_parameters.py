"""One bounded typed-parameter resolver shared by every Canvas execution surface."""

from __future__ import annotations

import datetime
import math
import re
from typing import Any

from hub import graph as graph_mod, metadb, workspace_providers
from hub.models import Graph, ParameterBinding, ParameterDeclaration
from hub.plugins.adapters import revision_adapter_for_uri
from hub.secrets import is_registered_secret_ref

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_MAX_REFERENCES = 4096


class ParameterResolutionError(ValueError):
    """The submitted public binding document cannot be admitted."""


def _secret_free(value: Any, name: str) -> None:
    if isinstance(value, str) and is_registered_secret_ref(value):
        raise ParameterResolutionError(
            f"parameter '{name}' cannot contain a SecretRef; use a credential binding")


def _scalar(decl: ParameterDeclaration, value: Any) -> Any:
    typ = decl.type
    if typ == "string":
        if not isinstance(value, str):
            raise ParameterResolutionError(f"parameter '{decl.name}' must be a string")
        _secret_free(value, decl.name)
        result: Any = value
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**53 - 1:
            raise ParameterResolutionError(f"parameter '{decl.name}' must be a safe integer")
        result = value
    elif typ == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterResolutionError(f"parameter '{decl.name}' must be a finite number")
        result = float(value)
        if not math.isfinite(result):
            raise ParameterResolutionError(f"parameter '{decl.name}' must be a finite number")
    elif typ == "boolean":
        if not isinstance(value, bool):
            raise ParameterResolutionError(f"parameter '{decl.name}' must be a boolean")
        result = value
    elif typ == "date":
        if not isinstance(value, str):
            raise ParameterResolutionError(f"parameter '{decl.name}' must be an ISO date")
        try:
            result = datetime.date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' must be an ISO date (YYYY-MM-DD)") from exc
    elif typ == "datetime":
        if not isinstance(value, str):
            raise ParameterResolutionError(
                f"parameter '{decl.name}' must be an ISO datetime with timezone")
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' must be an ISO datetime with timezone") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' must include an explicit timezone")
        result = parsed.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        raise ParameterResolutionError(f"parameter '{decl.name}' must be a DatasetRef")

    limits = decl.constraints
    if limits is None:
        return result
    if typ == "string":
        if limits.minimum is not None or limits.maximum is not None:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' has numeric constraints on a string")
        if limits.min_length is not None and len(result) < limits.min_length:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' is shorter than minLength {limits.min_length}")
        if limits.max_length is not None and len(result) > limits.max_length:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' is longer than maxLength {limits.max_length}")
    elif typ in ("integer", "float"):
        if limits.min_length is not None or limits.max_length is not None:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' has length constraints on a number")
        if limits.minimum is not None and result < limits.minimum:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' is below minimum {limits.minimum}")
        if limits.maximum is not None and result > limits.maximum:
            raise ParameterResolutionError(
                f"parameter '{decl.name}' is above maximum {limits.maximum}")
    elif any(item is not None for item in (
            limits.minimum, limits.maximum, limits.min_length, limits.max_length)):
        raise ParameterResolutionError(
            f"parameter '{decl.name}' type does not support constraints")
    return result


def _dataset_value(decl: ParameterDeclaration, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ParameterResolutionError(
            f"parameter '{decl.name}' must be an exact or latest DatasetRef")
    if value.get("kind") == "exact" and set(value) == {"kind", "datasetId", "revisionId"}:
        dataset_id, revision_id = value.get("datasetId"), value.get("revisionId")
        if (not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 128
                or not isinstance(revision_id, str) or not revision_id or len(revision_id) > 256):
            raise ParameterResolutionError(f"parameter '{decl.name}' has an invalid exact DatasetRef")
        _secret_free(dataset_id, decl.name)
        _secret_free(revision_id, decl.name)
        return {"kind": "exact", "datasetId": dataset_id, "revisionId": revision_id}
    if value.get("kind") == "latest" and set(value) == {"kind", "datasetId"}:
        dataset_id = value.get("datasetId")
        if not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 128:
            raise ParameterResolutionError(f"parameter '{decl.name}' has an invalid latest DatasetRef")
        _secret_free(dataset_id, decl.name)
        return {"kind": "latest", "datasetId": dataset_id}
    raise ParameterResolutionError(
        f"parameter '{decl.name}' must be {{kind: exact, datasetId, revisionId}} "
        "or {kind: latest, datasetId}")


def parse_cli_bindings(graph: Graph, values: list[str]) -> list[ParameterBinding]:
    """Parse repeatable NAME=VALUE CLI text against the Canvas declaration types."""
    declarations = {item.name: item for item in graph.parameters}
    result: list[ParameterBinding] = []
    for item in values:
        name, sep, raw = item.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ParameterResolutionError(f"--param must be NAME=VALUE, got '{item}'")
        decl = declarations.get(name)
        if decl is None:
            raise ParameterResolutionError(f"unknown canvas parameter '{name}'")
        if decl.type == "integer":
            if not _INT.fullmatch(raw.strip()):
                raise ParameterResolutionError(f"parameter '{name}' must be a safe integer")
            value: Any = int(raw)
        elif decl.type == "float":
            if not _FLOAT.fullmatch(raw.strip()):
                raise ParameterResolutionError(f"parameter '{name}' must be a finite number")
            value = float(raw)
        elif decl.type == "boolean":
            if raw.strip().lower() not in ("true", "false"):
                raise ParameterResolutionError(f"parameter '{name}' must be true or false")
            value = raw.strip().lower() == "true"
        elif decl.type == "dataset":
            dataset_id, marker, revision = raw.partition("@")
            if not marker or not dataset_id or not revision:
                raise ParameterResolutionError(
                    f"parameter '{name}' must be DATASET_ID@REVISION_ID or DATASET_ID@latest")
            value = ({"kind": "latest", "datasetId": dataset_id}
                     if revision == "latest" else
                     {"kind": "exact", "datasetId": dataset_id, "revisionId": revision})
        else:
            value = raw
        result.append(ParameterBinding(name=name, value=value))
    return result


def resolve_graph_parameters(graph: Graph, bindings: list[ParameterBinding], target: str | None,
                             deps, *, freeze_latest: bool = True
                             ) -> tuple[Graph, list[dict[str, Any]]]:
    """Return a bound graph and canonical used bindings for one target cone.

    ``freeze_latest=False`` is reserved for comparing a response-loss retry with an already-retained
    manifest. It validates the same public binding contract but preserves latest intent without
    consulting mutable provider state. Fresh admissions always use the default and freeze an exact
    revision before allocation.
    """
    if not graph.parameters and not bindings:
        return graph, []
    declarations = {item.name: item for item in graph.parameters}
    supplied: dict[str, Any] = {}
    for item in bindings:
        if item.name in supplied:
            raise ParameterResolutionError(f"duplicate canvas parameter '{item.name}'")
        if item.name not in declarations:
            raise ParameterResolutionError(f"unknown canvas parameter '{item.name}'")
        decl = declarations[item.name]
        supplied[item.name] = (
            _dataset_value(decl, item.value)
            if decl.type == "dataset" else _scalar(decl, item.value)
        )

    bound = graph.model_copy(deep=True)
    try:
        roots = graph_mod.upstream_chain(bound, target) if target else bound.nodes
        cone = graph_mod.execution_nodes(bound, roots)
    except (KeyError, graph_mod.CycleError):
        return bound, []  # the shared graph validator reports the structural error
    referenced: set[str] = set()
    references = 0

    def replace(value: Any, *, node, config_key: str | None = None) -> Any:
        nonlocal references
        if isinstance(value, dict) and "parameterRef" in value:
            if set(value) != {"parameterRef"} or not isinstance(value["parameterRef"], str):
                raise ParameterResolutionError("parameterRef sentinel must contain only one string name")
            references += 1
            if references > _MAX_REFERENCES:
                raise ParameterResolutionError("canvas has too many parameter references")
            name = value["parameterRef"]
            decl = declarations.get(name)
            if decl is None:
                raise ParameterResolutionError(f"canvas config references undeclared parameter '{name}'")
            referenced.add(name)
            raw = supplied[name] if name in supplied else decl.default
            if raw is None:
                requirement = "required " if decl.required else ""
                raise ParameterResolutionError(
                    f"{requirement}parameter '{name}' has no binding or default")
            if decl.type != "dataset":
                return _scalar(decl, raw)
            if config_key != "datasetRef" or node.type != "source":
                raise ParameterResolutionError(
                    f"dataset parameter '{name}' must be the complete datasetRef of a Source")
            dataset = _dataset_value(decl, raw)
            if dataset["kind"] == "exact" or not freeze_latest:
                return dataset
            uri = node.data.get("config", {}).get("uri")
            if not isinstance(uri, str) or not uri:
                raise ParameterResolutionError(
                    f"latest dataset parameter '{name}' requires a Source uri")
            try:
                logical_uri = deps.catalog.resolve_ref(uri)
                provider_id = workspace_providers.provider_dataset_identity(logical_uri)
                adapter = (deps.resolve_adapter(logical_uri) if provider_id is not None
                           else revision_adapter_for_uri(logical_uri, deps.resolve_adapter))
                if (provider_id is not None
                        and not workspace_providers.provider_dataset_supports_exact(adapter)):
                    raise ParameterResolutionError(
                        f"latest dataset parameter '{name}' requires exact revision support")
                registration = (None if provider_id is not None
                                else metadb.catalog_revision_binding_for_uri(logical_uri))
                current_id = provider_id or (str(registration["dataset_id"]) if registration else None)
                if current_id != dataset["datasetId"]:
                    raise ParameterResolutionError(
                        f"latest dataset parameter '{name}' does not match the Source registration")
                resolved = adapter.resolve_revision(logical_uri)
                revision_id = str(resolved.get("revision_id") or "")
                if not revision_id:
                    raise RuntimeError("provider returned no revision identity")
            except ParameterResolutionError:
                raise
            except Exception as exc:
                raise ParameterResolutionError(
                    f"latest dataset parameter '{name}' could not resolve an exact revision") from exc
            return {"kind": "exact", "datasetId": dataset["datasetId"],
                    "revisionId": revision_id}
        if isinstance(value, dict):
            return {key: replace(child, node=node, config_key=key) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child, node=node) for child in value]
        return value

    for node in cone:
        config = node.data.get("config") if isinstance(node.data, dict) else None
        if isinstance(config, dict):
            node.data["config"] = replace(config, node=node)

    canonical: list[dict[str, Any]] = []
    for decl in graph.parameters:
        if decl.name not in referenced:
            continue
        raw = supplied[decl.name] if decl.name in supplied else decl.default
        value = _dataset_value(decl, raw) if decl.type == "dataset" else _scalar(decl, raw)
        if decl.type == "dataset" and value["kind"] == "latest" and freeze_latest:
            exacts = []
            for node in cone:
                config = node.data.get("config") if isinstance(node.data, dict) else None
                candidate = config.get("datasetRef") if isinstance(config, dict) else None
                if (isinstance(candidate, dict) and candidate.get("kind") == "exact"
                        and candidate.get("datasetId") == value["datasetId"]):
                    exacts.append(str(candidate.get("revisionId") or ""))
            if not exacts or len(set(exacts)) != 1:
                raise ParameterResolutionError(
                    f"latest dataset parameter '{decl.name}' did not resolve consistently")
            value = {**value, "resolvedRevisionId": exacts[0]}
        declaration = decl.model_dump(by_alias=True, mode="json", exclude_none=True)
        declaration.pop("label", None)
        declaration.pop("help", None)
        canonical.append({"name": decl.name, "type": decl.type,
                          "value": value, "declaration": declaration})
    bound._parameter_bindings = canonical
    return bound, canonical


def validate_canonical_parameter_binding(item: Any) -> dict[str, Any]:
    """Validate one persisted manifest binding against the resolver's canonical representation."""
    if not isinstance(item, dict) or set(item) != {"name", "type", "value", "declaration"}:
        raise ParameterResolutionError("canonical parameter binding has an invalid shape")
    try:
        declaration = ParameterDeclaration.model_validate(item["declaration"])
    except (TypeError, ValueError) as exc:
        raise ParameterResolutionError("canonical parameter declaration is invalid") from exc
    if item.get("name") != declaration.name or item.get("type") != declaration.type:
        raise ParameterResolutionError("canonical parameter identity does not match its declaration")
    expected_declaration = declaration.model_dump(
        by_alias=True, mode="json", exclude_none=True)
    expected_declaration.pop("label", None)
    expected_declaration.pop("help", None)
    if _canonical_json(item["declaration"]) != _canonical_json(expected_declaration):
        raise ParameterResolutionError("canonical parameter declaration is not normalized")

    raw = item["value"]
    if declaration.type != "dataset":
        expected_value = _scalar(declaration, raw)
    elif isinstance(raw, dict) and raw.get("kind") == "latest":
        if set(raw) != {"kind", "datasetId", "resolvedRevisionId"}:
            raise ParameterResolutionError("canonical latest DatasetRef is invalid")
        intent = _dataset_value(
            declaration, {"kind": "latest", "datasetId": raw.get("datasetId")})
        revision_id = raw.get("resolvedRevisionId")
        if (not isinstance(revision_id, str) or not revision_id or len(revision_id) > 256):
            raise ParameterResolutionError("canonical latest DatasetRef is invalid")
        _secret_free(revision_id, declaration.name)
        expected_value = {**intent, "resolvedRevisionId": revision_id}
    else:
        expected_value = _dataset_value(declaration, raw)
    if _canonical_json(raw) != _canonical_json(expected_value):
        raise ParameterResolutionError("canonical parameter value is not normalized")
    return {
        "name": declaration.name,
        "type": declaration.type,
        "value": expected_value,
        "declaration": expected_declaration,
    }


def _canonical_json(value: Any) -> str:
    import json
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ParameterResolutionError("canonical parameter value is not JSON") from exc
