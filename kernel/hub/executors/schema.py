"""Shared Canvas output-schema and row-reference derivation.

DuckDB is authoritative for output names and physical types, but a relation does not retain
provider field metadata.  This module overlays one conservative row-reference contract on those
observed columns.  Schema inspection, preview, join analysis, graph validation, placement, and
persisted ``outputSchema`` declarations all consume these same derived facts.
"""

from __future__ import annotations

import re
import uuid
from collections import deque

from hub import db, graph as g
from hub.executors.engine import (
    BuildEngine,
    _bypassed,
    _disabled,
    declared_schema,
    normalize_how,
)
from hub.ir import resolve_config
from hub.models import (
    ColumnSchema,
    Graph,
    GraphNode,
    TypedRowReference,
    dataset_ref_identity,
    normalize_column_schemas,
)
from hub.plugins.adapters import relation_columns, revision_adapter_for_uri
from hub.sqlpolicy import identifier_key

# Kinds whose output columns require executing code. Pivot columns depend on data values, so its
# metadata-only output is also intentionally unknown.
_UNTYPED = {"transform", "section", "vector-search", "pivot"}
_REFERENCE_PASSTHROUGH = {"filter", "sample"}
_DIRECT_IDENTIFIER = re.compile(
    r'^\s*(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_]*))'
    r'(?:\s+AS\s+(?:"((?:""|[^"])*)"|([A-Za-z_][A-Za-z0-9_]*)))?\s*$',
    re.IGNORECASE,
)


def _columns(values: list[ColumnSchema | dict]) -> list[ColumnSchema]:
    return normalize_column_schemas(list(values))


def _wire(columns: list[ColumnSchema] | None) -> list[dict] | None:
    return (
        [column.model_dump(by_alias=True, mode="json") for column in columns]
        if columns is not None else None
    )


def _split_projection(value: str) -> list[str]:
    """Split a SQL projection/group list without treating nested commas as separators."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            current.append(char)
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    current.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _direct_projection(value: str) -> tuple[str, str] | None:
    """Return ``(source, output)`` only for a direct identifier or ``identifier AS alias``."""
    match = _DIRECT_IDENTIFIER.match(value)
    if match is None:
        return None
    source = (match.group(1) or match.group(2) or "").replace('""', '"')
    output = (match.group(3) or match.group(4) or source).replace('""', '"')
    return source, output


def _by_name(columns: list[ColumnSchema]) -> dict[str, list[ColumnSchema]]:
    out: dict[str, list[ColumnSchema]] = {}
    for column in columns:
        out.setdefault(identifier_key(column.name), []).append(column)
    return out


def _reference_copy(
        output: ColumnSchema, source: ColumnSchema | None) -> ColumnSchema:
    return output.model_copy(update={
        "row_reference": source.row_reference if source is not None else None,
    })


def _unknown_references(columns: list[ColumnSchema]) -> list[ColumnSchema]:
    return [_reference_copy(column, None) for column in columns]


def _matching_reference_columns(
        output: list[ColumnSchema], source: list[ColumnSchema]) -> list[ColumnSchema]:
    candidates = _by_name(source)
    output_names = _by_name(output)
    return [
        _reference_copy(
            column,
            matches[0] if (
                len(output_names[identifier_key(column.name)]) == 1
                and len(matches := candidates.get(identifier_key(column.name), [])) == 1
            )
            else None,
        )
        for column in output
    ]


def _reference_signature(reference: TypedRowReference) -> tuple:
    target = reference.target
    if target.kind == "exact":
        target_identity = ("exact", target.dataset_id, target.revision_id)
    else:
        target_identity = ("canonical", target.dataset_id)
    return (
        target_identity,
        tuple(reference.key_fields),
        reference.semantic_type,
    )


def _compatible_reference(
        candidates: list[ColumnSchema | None],
) -> TypedRowReference | None:
    """Merge complete compatible evidence without selecting whichever Union input ran first."""
    references = [
        column.row_reference if column is not None else None
        for column in candidates
    ]
    if not references or any(reference is None for reference in references):
        return None
    known = [reference for reference in references if reference is not None]
    if len({_reference_signature(reference) for reference in known}) != 1:
        return None
    documents = [reference.model_dump(by_alias=True, mode="json") for reference in known]
    if all(document == documents[0] for document in documents[1:]):
        return known[0]
    # Compatible targets with different evidence provenance are a derived fact, not evidence owned
    # by an arbitrarily selected Union input. Exact-target lastKnown display hints are likewise not
    # merged; the stable exact identity remains sufficient.
    merged = known[0].model_dump(by_alias=True, mode="json")
    merged["provenance"] = "lineage"
    if known[0].target.kind == "exact":
        merged["target"] = {
            "kind": "exact",
            "datasetId": known[0].target.dataset_id,
            "revisionId": known[0].target.revision_id,
        }
    return TypedRowReference.model_validate(merged)


def _source_evidence(
        node: GraphNode, resolve_adapter, *,
        allow_revision_detail: bool = True,
) -> list[ColumnSchema] | None:
    """Read the declaration or exact provider schema that owns a Source's field evidence."""
    declaration = declared_schema(node)
    if declaration is not None:
        try:
            return _columns(declaration)
        except ValueError:
            return None
    cfg = resolve_config(node)
    uri = cfg.get("uri")
    if not isinstance(uri, str) or not uri:
        return None
    provider_preview_uri = cfg.get("_input_provider_preview_uri")
    if uri.startswith("workspace-provider://") and isinstance(provider_preview_uri, str):
        uri = provider_preview_uri
    provider_uri = cfg.get("_input_provider_uri")
    if cfg.get("_input_revision_id") is not None and isinstance(provider_uri, str):
        uri = provider_uri
    try:
        dataset_ref = cfg.get("datasetRef")
        revision_id = cfg.get("_input_revision_id")
        if isinstance(dataset_ref, dict):
            _dataset_id, revision_id = dataset_ref_identity(dataset_ref)
        if revision_id is not None:
            if not allow_revision_detail:
                # A provider's revision-detail implementation may open the complete exact relation.
                # Interactive preview must retain its source bound rather than fetching extra evidence.
                return None
            detail = revision_adapter_for_uri(uri, resolve_adapter).revision_detail(
                uri, str(revision_id), preview_limit=1)
            return _columns(detail["columns"])
        return _columns(resolve_adapter(uri).schema(uri))
    except Exception:  # noqa: BLE001 - absent provider evidence is an explicit unknown
        return None


def _code_hash(value: str) -> str:
    """Match the UI's unsigned 32-bit djb2 hash over JavaScript UTF-16 code units."""
    result = 5381
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    for index in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[index:index + 2], "little")
        result = (result * 33 + code_unit) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if result == 0:
        return "0"
    out = ""
    while result:
        result, digit = divmod(result, 36)
        out = digits[digit] + out
    return out


def _declared_output(node: GraphNode) -> list[ColumnSchema] | None:
    declaration = declared_schema(node)
    if declaration is None:
        return None
    try:
        columns = _columns(declaration)
    except ValueError:
        return None
    raw_config = (
        node.data.get("config", {}) if isinstance(node.data, dict) else {}
    )
    contract_text = raw_config.get("code")
    if contract_text is None:
        contract_text = raw_config.get("sql")
    pinned_hash = raw_config.get("outputSchemaCodeHash")
    if (
        contract_text is not None
        and isinstance(pinned_hash, str)
        and pinned_hash
        and pinned_hash != _code_hash(str(contract_text))
    ):
        # The declaration still owns names/types and remains enforceable. Only its row-reference
        # evidence is stale: changed code may now populate the same-shaped field from another target.
        return _unknown_references(columns)
    return columns


def _same_names(left: list[ColumnSchema], right: list[ColumnSchema]) -> bool:
    return (
        len(left) == len(right)
        and all(identifier_key(a.name) == identifier_key(b.name)
                for a, b in zip(left, right, strict=True))
    )


def _derive_select(
        output: list[ColumnSchema], source: list[ColumnSchema], expression: str,
) -> list[ColumnSchema]:
    if not expression:
        return _matching_reference_columns(output, source)
    mapping: dict[str, list[str]] = {}
    for part in _split_projection(expression):
        if part == "*":
            for column in source:
                mapping.setdefault(identifier_key(column.name), []).append(column.name)
            continue
        direct = _direct_projection(part)
        if direct is not None:
            input_name, output_name = direct
            mapping.setdefault(identifier_key(output_name), []).append(input_name)
    candidates = _by_name(source)
    output_names = _by_name(output)
    derived: list[ColumnSchema] = []
    for column in output:
        output_key = identifier_key(column.name)
        source_names = mapping.get(output_key, [])
        matches = (
            candidates.get(identifier_key(source_names[0]), [])
            if len(source_names) == 1 else []
        )
        derived.append(_reference_copy(
            column,
            matches[0] if len(output_names[output_key]) == len(matches) == 1 else None,
        ))
    return derived


def _derive_join(
        output: list[ColumnSchema], inputs: list[list[ColumnSchema]],
        config: dict,
) -> list[ColumnSchema]:
    if len(inputs) < 2:
        return _matching_reference_columns(output, inputs[0]) if inputs else _unknown_references(output)
    left, right = inputs[:2]
    using_keys: set[str] = set()
    raw_on = str(config.get("on") or "").strip()
    how = normalize_how(str(config.get("how") or "inner"))
    if raw_on and how != "cross":
        direct = [_direct_projection(part) for part in _split_projection(raw_on)]
        if all(item is not None and item[0] == item[1] for item in direct):
            using_keys = {identifier_key(item[0]) for item in direct if item is not None}
    origins = [*left, *(column for column in right
                        if identifier_key(column.name) not in using_keys)]
    if len(origins) != len(output):
        return _unknown_references(output)
    left_by_name = _by_name(left)
    right_by_name = _by_name(right)
    derived: list[ColumnSchema] = []
    for column, origin in zip(output, origins, strict=True):
        key = identifier_key(origin.name)
        if key not in using_keys:
            derived.append(_reference_copy(column, origin))
            continue
        left_matches = left_by_name.get(key, [])
        right_matches = right_by_name.get(key, [])
        left_key = left_matches[0] if len(left_matches) == 1 else None
        right_key = right_matches[0] if len(right_matches) == 1 else None
        if how == "left":
            reference = left_key.row_reference if left_key is not None else None
        elif how == "right":
            reference = right_key.row_reference if right_key is not None else None
        else:
            # INNER values are equal and FULL values can originate on either side. In both cases the
            # merged key has a reference only when complete evidence identifies one compatible target.
            reference = _compatible_reference([left_key, right_key])
        derived.append(column.model_copy(update={"row_reference": reference}))
    return derived


def _derive_aggregate(
        output: list[ColumnSchema], source: list[ColumnSchema], group_by: str,
) -> list[ColumnSchema]:
    direct_keys = [_direct_projection(part) for part in _split_projection(group_by)]
    source_by_name = _by_name(source)
    derived = _unknown_references(output)
    for index, direct in enumerate(direct_keys):
        if direct is None or index >= len(derived):
            continue
        input_name, output_name = direct
        if identifier_key(derived[index].name) != identifier_key(output_name):
            continue
        matches = source_by_name.get(identifier_key(input_name), [])
        if len(matches) == 1:
            derived[index] = _reference_copy(derived[index], matches[0])
    return derived


def _derive_union(
        output: list[ColumnSchema], inputs: list[list[ColumnSchema]], align: str,
) -> list[ColumnSchema]:
    if not inputs:
        return _unknown_references(output)
    by_position = align.lower() == "position"
    input_maps = [_by_name(columns) for columns in inputs]
    derived: list[ColumnSchema] = []
    for index, column in enumerate(output):
        candidates: list[ColumnSchema | None] = []
        for input_index, columns in enumerate(inputs):
            if by_position:
                candidates.append(columns[index] if index < len(columns) else None)
            else:
                matches = input_maps[input_index].get(identifier_key(column.name), [])
                candidates.append(matches[0] if len(matches) == 1 else None)
        derived.append(column.model_copy(update={
            "row_reference": _compatible_reference(candidates),
        }))
    return derived


def derive_reference_schema(
        node: GraphNode,
        observed: list[ColumnSchema],
        inputs: list[list[ColumnSchema] | None],
        *,
        source_evidence: list[ColumnSchema] | None = None,
) -> list[ColumnSchema]:
    """Apply the one provider-neutral reference-propagation contract for a Canvas node."""
    if _disabled(node):
        return _unknown_references(observed)
    known_inputs = [schema for schema in inputs if schema is not None]
    if _bypassed(node):
        return (
            _matching_reference_columns(observed, known_inputs[0])
            if len(known_inputs) == 1 else _unknown_references(observed)
        )
    config = resolve_config(node)
    if node.type == "source":
        return (
            [_reference_copy(output, evidence)
             for output, evidence in zip(observed, source_evidence, strict=True)]
            if source_evidence is not None
            and _same_names(observed, source_evidence)
            else _unknown_references(observed)
        )
    declaration = _declared_output(node)
    if declaration is not None:
        return _matching_reference_columns(observed, declaration)
    if node.type in _UNTYPED or node.type == "sql":
        return _unknown_references(observed)
    if not known_inputs or len(known_inputs) != len(inputs):
        return _unknown_references(observed)
    if node.type in _REFERENCE_PASSTHROUGH:
        return (
            _matching_reference_columns(observed, known_inputs[0])
            if len(known_inputs) == 1 else _unknown_references(observed)
        )
    if node.type == "select" and len(known_inputs) == 1:
        return _derive_select(observed, known_inputs[0], str(config.get("expr") or ""))
    if node.type == "join":
        return _derive_join(observed, known_inputs, config)
    if node.type == "aggregate" and len(known_inputs) == 1:
        return _derive_aggregate(
            observed, known_inputs[0], str(config.get("groupBy") or ""))
    if node.type == "union":
        return _derive_union(
            observed, known_inputs, str(config.get("align") or "name"))
    # Operations outside #787's explicit contract never inherit a plausible-looking reference.
    return _unknown_references(observed)


def apply_derived_references(
        observed: list[ColumnSchema], derived: list[ColumnSchema] | None,
) -> list[ColumnSchema]:
    """Put shared references on runtime preview columns without masking actual runtime types."""
    if derived is None or not _same_names(observed, derived):
        return _unknown_references(observed)
    return [
        _reference_copy(runtime, metadata)
        for runtime, metadata in zip(observed, derived, strict=True)
    ]


def _derive_graph_schemas(
        graph: Graph,
        observed: dict[str, list[ColumnSchema] | None],
        source_evidence: dict[str, list[ColumnSchema] | None],
        node_ids: set[str] | None = None,
) -> dict[str, list[ColumnSchema] | None]:
    selected = (
        graph.nodes
        if node_ids is None else [node for node in graph.nodes if node.id in node_ids]
    )
    nodes = {node.id: node for node in selected}
    incoming = {node_id: g.incoming(graph, node_id) for node_id in nodes}
    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    indegree = {node_id: 0 for node_id in nodes}
    for node_id, edges in incoming.items():
        for edge in edges:
            if edge.source in nodes:
                children[edge.source].append(node_id)
                indegree[node_id] += 1
    ready = deque(node_id for node_id in nodes if indegree[node_id] == 0)
    derived: dict[str, list[ColumnSchema] | None] = {}
    while ready:
        node_id = ready.popleft()
        node = nodes[node_id]
        raw = observed.get(node_id)
        inputs = [derived.get(edge.source) for edge in incoming[node_id]]
        derived[node_id] = (
            derive_reference_schema(
                node, raw, inputs, source_evidence=source_evidence.get(node_id))
            if raw is not None else None
        )
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return {node.id: derived.get(node.id) for node in selected}


def derived_schemas_for_engine(
        graph: Graph, engine: BuildEngine, resolve_adapter, *,
        node_builders=None, node_specs=None, runtime: bool = False,
        source_evidence: dict[str, list[ColumnSchema] | None] | None = None,
        allow_revision_detail: bool = True,
        target_node_id: str | None = None,
) -> dict[str, list[ColumnSchema] | None]:
    """Derive schemas from one already-scoped engine.

    Metadata inspection sets ``runtime=False`` so an undeclared code node remains untyped.
    Preview sets ``runtime=True``: actual runtime columns remain visible, but opaque outputs still
    carry no inherited reference.
    """
    untyped = _UNTYPED | set(node_builders or {})

    def blocks(column: GraphNode) -> bool:
        return (
            (column.type in untyped and declared_schema(column) is None)
            or _disabled(column)
        )

    nodes = (
        g.upstream_chain(graph, target_node_id)
        if target_node_id is not None else graph.nodes
    )
    evidence = (
        source_evidence
        if source_evidence is not None
        else {
            node.id: _source_evidence(
                node, resolve_adapter,
                allow_revision_detail=allow_revision_detail,
            )
            for node in nodes if node.type == "source"
        }
    )
    observed: dict[str, list[ColumnSchema] | None] = {}
    for node in nodes:
        if node_specs and node.type in node_specs:
            try:
                ports = g.effective_output_ports_for_node(
                    node, node_specs[node.type])
            except ValueError:
                observed[node.id] = None
                continue
            if len(ports) > 1:
                observed[node.id] = None
                continue
        declaration = declared_schema(node)
        if (
            not runtime
            and node.type in untyped
            and declaration is not None
            and not _disabled(node)
            and not _bypassed(node)
        ):
            try:
                observed[node.id] = _columns(declaration)
            except ValueError:
                observed[node.id] = None
            continue
        if (
            not runtime
            and any(blocks(column)
                    for column in g.upstream_chain(graph, node.id))
        ):
            observed[node.id] = None
            continue
        try:
            observed[node.id] = relation_columns(engine.relation(node.id))
        except Exception:  # noqa: BLE001 - unavailable nodes are explicit unknowns
            observed[node.id] = None
    return _derive_graph_schemas(
        graph, observed, evidence, {node.id for node in nodes})


def schema_for_graph(
        graph: Graph, resolve_adapter, registry,
        node_builders=None, node_specs=None, storage=None,
) -> dict[str, list | None]:
    if not g.is_acyclic(graph):
        return {}
    engine = BuildEngine(
        graph, resolve_adapter, registry, sample_k=None, full=True,
        node_builders=node_builders, node_specs=node_specs, schema_only=True,
    )
    from hub.storage import source_read_scope
    with source_read_scope(
            storage, g.execution_source_uris(graph, None),
            owner=f"schema:{uuid.uuid4().hex}"):
        evidence = {
            node.id: _source_evidence(node, resolve_adapter)
            for node in graph.nodes if node.type == "source"
        }
        with db.run_scope():
            derived = derived_schemas_for_engine(
                graph, engine, resolve_adapter,
                node_builders=node_builders, node_specs=node_specs,
                source_evidence=evidence,
            )
    return {
        node_id: _wire(columns)
        for node_id, columns in derived.items()
    }


def schema_for_graph_ports(
        graph: Graph, resolve_adapter, registry,
        node_builders=None, node_specs=None, storage=None,
) -> dict[str, dict[str, list | None]]:
    """Return public per-port schemas without attributing one declaration to sibling ports."""
    if not g.is_acyclic(graph):
        return {}
    node_schemas = schema_for_graph(
        graph, resolve_adapter, registry, node_builders, node_specs, storage)
    out: dict[str, dict[str, list | None]] = {}
    multi_nodes: list[tuple[GraphNode, list]] = []
    for node in graph.nodes:
        try:
            ports = (
                g.effective_output_ports_for_node(node, node_specs[node.type])
                if node_specs else [g.EffectiveOutputPort("out", None, "dataset")]
            )
        except (KeyError, ValueError):
            out[node.id] = {}
            continue
        if len(ports) == 1:
            out[node.id] = {ports[0].id: node_schemas.get(node.id)}
        else:
            multi_nodes.append((node, ports))
            out[node.id] = {port.id: None for port in ports}

    # Preserve existing honest per-port inspection for executable multi-output nodes. Their
    # node-wide declaration is intentionally not projected onto any sibling port.
    if multi_nodes:
        untyped = _UNTYPED | set(node_builders or {})

        def blocks(column: GraphNode) -> bool:
            return (
                (column.type in untyped and declared_schema(column) is None)
                or _disabled(column)
            )

        engine = BuildEngine(
            graph, resolve_adapter, registry, sample_k=None, full=True,
            node_builders=node_builders, node_specs=node_specs, schema_only=True,
        )
        from hub.storage import source_read_scope
        with source_read_scope(
                storage, g.execution_source_uris(graph, None),
                owner=f"schema:{uuid.uuid4().hex}"):
            with db.run_scope():
                for node, ports in multi_nodes:
                    if any(blocks(column)
                           for column in g.upstream_chain(graph, node.id)):
                        continue
                    for port in ports:
                        try:
                            out[node.id][port.id] = _wire(
                                relation_columns(engine.relation(node.id, port.id)))
                        except Exception:  # noqa: BLE001
                            out[node.id][port.id] = None
    return out
