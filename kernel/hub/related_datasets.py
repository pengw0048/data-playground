"""Bounded, provenance-aware related-dataset discovery.

This module is deliberately the single policy boundary used by catalog HTTP, MCP and Canvas
confirmation. It has no display-name, URI, table-id or placement identity fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hub import metadb, relationships, workspace_providers
from hub.backends import DatasetRevisionAdapter
from hub.models import (
    CatalogQuery, CatalogTable, ColumnSchema, DatasetRevision, DatasetRevisionPage, ExactDatasetRef,
    RelatedDatasetCandidate, RelatedDatasetExclusion, RelatedDatasetIdentity, RelatedDatasetPage,
)
from hub.plugins.adapters import revision_adapter_for_uri
from hub.row_reference_diagnosis import diagnose_key_pairs, has_target_conflict, input_identity
from hub.plugins.capabilities import display_base_type
from hub.sqlpolicy import identifier_key
from hub.storage import source_read_scope

_SCOPE_LIMIT = 50
_RELATIONSHIP_LIMIT = 64
_REFERENCE_LIMIT = 64
_EXCLUSION_LIMIT = 20
MAX_RELATED_DATASETS = 20


@dataclass(frozen=True)
class _Source:
    identity: RelatedDatasetIdentity
    name: str
    uri: str
    columns: list[ColumnSchema]
    dataset_id: str
    revision_dataset_id: str
    table: CatalogTable | None = None


@dataclass
class _Seed:
    table: CatalogTable
    identity: RelatedDatasetIdentity
    rank: int
    score: float
    evidence: Literal["declared_relationship", "typed_reference", "schema_match"]
    evidence_status: Literal["declared", "proven", "inferred"]
    reason: str
    left_columns: list[str]
    right_columns: list[str]
    declared_cardinality: str = "unknown"
    exact_ref: ExactDatasetRef | None = None


def _local_identity(table: CatalogTable, *, revision_id: str | None = None) -> RelatedDatasetIdentity:
    if not table.registration_id:
        raise ValueError("catalog dataset has no stable registration identity")
    return RelatedDatasetIdentity(
        kind="local", registration_id=table.registration_id,
        revision_mode="exact" if revision_id else "current",
        revision_id=revision_id,
    )


def _revision_adapter(uri: str, resolve_adapter):
    """Return an adapter which can prove retained revision facts for this exact binding."""
    adapter = revision_adapter_for_uri(uri, resolve_adapter)
    supports_exact = (workspace_providers.provider_dataset_supports_exact(adapter)
                      if workspace_providers.is_provider_dataset_uri(uri)
                      else isinstance(adapter, DatasetRevisionAdapter))
    if not supports_exact:
        raise NotImplementedError("retained revisions are unavailable for this dataset")
    return adapter


def _exact_columns(uri: str, revision_id: str, resolve_adapter) -> list[ColumnSchema]:
    """Read schema from the selected immutable revision, never from a mutable catalog head."""
    adapter = _revision_adapter(uri, resolve_adapter)
    detail = adapter.revision_detail(uri, revision_id, preview_limit=1)
    return [ColumnSchema.model_validate(item) for item in detail["columns"]]


def related_dataset_revisions(catalog, resolve_adapter, identity: RelatedDatasetIdentity, *,
                              limit: int = 20, cursor: str | None = None) -> DatasetRevisionPage:
    """List one bounded retained-revision page for a stable related-data binding.

    This intentionally accepts the same identity that Canvas will fence.  It never discovers a
    provider dataset from a display field or substitutes a current schema for an exact selection.
    """
    source = _source_from_identity(catalog, identity, resolve_adapter)
    bounded = max(1, min(int(limit), MAX_RELATED_DATASETS))
    adapter = _revision_adapter(source.uri, resolve_adapter)
    rows, next_cursor = adapter.revision_history(source.uri, limit=bounded, cursor=cursor)
    return DatasetRevisionPage(
        items=[DatasetRevision(
            dataset_id=source.revision_dataset_id, revision_id=str(row["revision_id"]),
            committed_at=row.get("committed_at"),
            retention_owner=getattr(adapter, "retention_owner", "provider"),
        ) for row in rows],
        next_cursor=next_cursor, has_more=next_cursor is not None,
    )


def _stable_identity_matches(left: RelatedDatasetIdentity, right: RelatedDatasetIdentity) -> bool:
    """Compare the only identity fields that survive a retained-revision selection."""
    return (left.kind == right.kind
            and left.registration_id == right.registration_id
            and left.mount_id == right.mount_id
            and left.source_binding_id == right.source_binding_id)


def _key_columns_are_compatible(
        left_columns: list[ColumnSchema], right_columns: list[ColumnSchema],
        left_fields: list[str], right_fields: list[str]) -> bool:
    if not left_fields or len(left_fields) != len(right_fields):
        return False
    left_by_name = {identifier_key(column.name): column for column in left_columns}
    right_by_name = {identifier_key(column.name): column for column in right_columns}
    for left, right in zip(left_fields, right_fields, strict=True):
        left_column = left_by_name.get(identifier_key(left))
        right_column = right_by_name.get(identifier_key(right))
        if (left_column is None or right_column is None
                or display_base_type(left_column.type) != display_base_type(right_column.type)):
            return False
    return True


def review_related_dataset_revision(
        catalog, resolve_adapter, storage, source_identity: RelatedDatasetIdentity,
        candidate: RelatedDatasetCandidate, revision_id: str, *,
        q: str | None = None, folder: str | None = None,
) -> RelatedDatasetCandidate:
    """Re-review one selected retained candidate revision before Canvas can mutate.

    The initial candidate is replayed from the bounded discovery query.  Then the exact adapter
    detail is used for the selected revision's schema and typed-reference contradiction check; no
    current-head fields or cardinality measurement are retained in the result.
    """
    page = related_datasets(
        catalog, resolve_adapter, storage, source_identity, q=q, folder=folder,
        limit=MAX_RELATED_DATASETS)
    base = next((item for item in page.candidates
                 if _stable_identity_matches(item.identity, candidate.identity)
                 and item.evidence == candidate.evidence
                 and item.left_columns == candidate.left_columns
                 and item.right_columns == candidate.right_columns), None)
    if base is None:
        raise ValueError("the related dataset or join evidence changed after review")
    selected_identity = base.identity.model_copy(update={
        "revision_mode": "exact", "revision_id": revision_id,
    })
    source = _source_from_identity(catalog, source_identity, resolve_adapter)
    target = _source_from_identity(catalog, selected_identity, resolve_adapter)
    if not _key_columns_are_compatible(
            source.columns, target.columns, base.left_columns, base.right_columns):
        raise ValueError("the selected retained revision is not compatible with the reviewed join keys")
    diagnoses = diagnose_key_pairs(
        left_input=input_identity(dataset_id=source.dataset_id,
                                  revision_id=source.identity.revision_id),
        right_input=input_identity(dataset_id=target.dataset_id,
                                   revision_id=target.identity.revision_id),
        left_columns=source.columns, right_columns=target.columns,
        left_fields=base.left_columns, right_fields=base.right_columns)
    if has_target_conflict(diagnoses):
        raise ValueError("the selected retained revision contradicts typed row-reference evidence")
    detail = _revision_adapter(target.uri, resolve_adapter).revision_detail(
        target.uri, revision_id, preview_limit=1)
    exact_ref = ExactDatasetRef(
        kind="exact", dataset_id=target.revision_dataset_id, revision_id=str(detail["revision_id"]),
        last_known={"committedAt": detail.get("committed_at")},
    )
    return base.model_copy(update={
        "identity": selected_identity,
        "exact_ref": exact_ref,
        "cardinality": "unknown",
        "confidence": "inferred",
        "warning": None,
    })


def _source_from_identity(catalog, value: str | RelatedDatasetIdentity, resolve_adapter) -> _Source:
    """Resolve exactly one selected local registration or mount-scoped provider binding."""
    if isinstance(value, str):
        table = catalog.get_table(value)
        identity = _local_identity(table)
    else:
        identity = value
        if identity.kind == "local":
            if not identity.registration_id:
                raise ValueError("local Source has no registrationId")
            table = catalog.get_table(identity.registration_id)
            observed = _local_identity(table, revision_id=identity.revision_id)
            if observed.registration_id != identity.registration_id:
                raise ValueError("selected local dataset changed after review")
            identity = observed
        else:
            binding = metadb.workspace_provider_dataset_for_source_binding(
                mount_id=str(identity.mount_id), source_binding_id=str(identity.source_binding_id))
            if binding is None or binding.get("referenceState") != "current":
                raise ValueError("selected provider dataset binding is unavailable")
            uri = workspace_providers.provider_dataset_uri(
                str(identity.mount_id), str(identity.source_binding_id))
            dataset_id = workspace_providers.provider_dataset_identity(uri)
            if dataset_id is None:
                raise ValueError("selected provider dataset identity is unavailable")
            columns = ([ColumnSchema.model_validate(item) for item in binding["columns"]]
                       if identity.revision_mode == "current" else _exact_columns(
                           uri, str(identity.revision_id), resolve_adapter))
            return _Source(
                identity=identity, name=str(binding.get("providerDatasetId") or "provider dataset"),
                uri=uri, columns=columns,
                dataset_id=dataset_id, revision_dataset_id=dataset_id, table=None)
    if not table.registration_id:
        raise ValueError("selected local dataset has no registrationId")
    columns = (table.columns if identity.revision_mode == "current" else _exact_columns(
        table.uri, str(identity.revision_id), resolve_adapter))
    binding = metadb.catalog_revision_binding_for_uri(table.uri)
    revision_dataset_id = str(binding["dataset_id"]) if binding is not None else table.registration_id
    return _Source(identity=identity, name=table.name, uri=table.uri, columns=columns,
                   dataset_id=revision_dataset_id, revision_dataset_id=revision_dataset_id, table=table)


def _resolved_revision_id(config: dict) -> str | None:
    """Return only immutable revision evidence already persisted in a Source config."""
    ref = config.get("datasetRef")
    if not isinstance(ref, dict):
        return None
    if ref.get("kind") == "exact":
        revision_id = ref.get("revisionId")
        return revision_id if isinstance(revision_id, str) and revision_id else None
    if ref.get("kind") == "as_of":
        resolved = ref.get("resolved")
        revision_id = resolved.get("revisionId") if isinstance(resolved, dict) else None
        return revision_id if isinstance(revision_id, str) and revision_id else None
    return None


def source_identity_from_config(catalog, config: dict) -> RelatedDatasetIdentity:
    """Admit a Canvas Source by its persisted stable binding only.

    This is intentionally separate from catalog lookup so older URI/tableId-only Source documents
    do not silently gain the Join-with action.
    """
    registration_id = config.get("registrationId")
    if isinstance(registration_id, str) and registration_id:
        table = catalog.get_table(registration_id)
        return _local_identity(table, revision_id=_resolved_revision_id(config))
    mount_id = config.get("providerMountId")
    binding_id = config.get("providerSourceBindingId")
    if isinstance(mount_id, str) and isinstance(binding_id, str):
        revision_id = _resolved_revision_id(config)
        return RelatedDatasetIdentity(
            kind="provider", mount_id=mount_id, source_binding_id=binding_id,
            revision_mode="exact" if revision_id is not None else "current",
            revision_id=revision_id,
        )
    raise ValueError("Source has no supported stable dataset binding")


def _scope_page(catalog, *, q: str | None, folder: str | None):
    page = catalog.list_page(CatalogQuery(
        q=(q or "").strip() or None, folder=(folder or "").strip("/") or None,
        limit=_SCOPE_LIMIT, offset=0, sort="name", order="asc"))
    if page.offset != 0 or page.limit != _SCOPE_LIMIT or len(page.items) > _SCOPE_LIMIT:
        raise RuntimeError("catalog provider returned an invalid bounded candidate page")
    return page


def _put(seeds: dict[str, _Seed], seed: _Seed) -> None:
    key = seed.identity.registration_id or f"{seed.identity.mount_id}/{seed.identity.source_binding_id}"
    current = seeds.get(key)
    if current is None or (seed.rank, -seed.score, seed.table.name.casefold()) < (
            current.rank, -current.score, current.table.name.casefold()):
        seeds[key] = seed


def _semantic_dataset_id(table: CatalogTable) -> str | None:
    binding = metadb.catalog_revision_binding_for_uri(table.uri)
    return str(binding["dataset_id"]) if binding is not None else table.registration_id


def _target(catalog, reference, cache: dict[str, CatalogTable | None]) -> CatalogTable | None:
    """Resolve a typed reference by its retained dataset identity only."""
    dataset_id = reference.target.dataset_id
    if dataset_id not in cache:
        try:
            table = catalog.get_table(dataset_id)
            cache[dataset_id] = table if _semantic_dataset_id(table) == dataset_id else None
        except Exception:  # retained or unavailable target stays unavailable; never rebind it
            try:
                binding = metadb.catalog_revision_binding(dataset_id)
                table = catalog.get_table(binding["uri"]) if binding is not None else None
                cache[dataset_id] = (table if table is not None
                                     and _semantic_dataset_id(table) == dataset_id else None)
            except Exception:
                cache[dataset_id] = None
    return cache[dataset_id]


def _candidate_identity(table: CatalogTable, exact: ExactDatasetRef | None = None) -> RelatedDatasetIdentity:
    return _local_identity(table, revision_id=exact.revision_id if exact else None)


def _provider_candidate_identity(
        identity: RelatedDatasetIdentity, exact: ExactDatasetRef | None = None,
) -> RelatedDatasetIdentity:
    return identity.model_copy(update={
        "revision_mode": "exact" if exact is not None else "current",
        "revision_id": exact.revision_id if exact is not None else None,
    })


def _provider_typed_target(
        source: _Source, reference, *, q: str | None,
) -> tuple[CatalogTable, RelatedDatasetIdentity] | None:
    """Resolve a provider typed target by its opaque canonical identity alone.

    The bounded provider page is for inference only: a typed reference may point beyond its first
    page.  Decode only the canonical identity, keep it on the selected Source's mount, then use the
    mount-and-binding lookup as the sole authoritative target resolution.  Names, physical URIs,
    and provider browse placements never participate in this path.
    """
    if source.identity.kind != "provider":
        return None
    binding = workspace_providers.provider_dataset_binding_for_identity(
        reference.target.dataset_id)
    if binding is None:
        return None
    mount_id, binding_id = binding
    if mount_id != source.identity.mount_id or binding_id == source.identity.source_binding_id:
        return None
    row = metadb.workspace_provider_dataset_for_source_binding(
        mount_id=mount_id, source_binding_id=binding_id)
    if (row is None or row.get("referenceState") != "current"
            or row.get("mountId") != mount_id or row.get("sourceBindingId") != binding_id):
        return None
    normalized_q = " ".join((q or "").split()).casefold()
    provider_dataset_id = str(row.get("providerDatasetId") or "")
    if normalized_q and normalized_q not in provider_dataset_id.casefold():
        return None
    try:
        columns = [ColumnSchema.model_validate(item) for item in row["columns"]]
    except (KeyError, TypeError, ValueError):
        return None
    table = CatalogTable(
        id=provider_dataset_id or binding_id, name=provider_dataset_id or "provider dataset",
        uri=workspace_providers.provider_dataset_uri(mount_id, binding_id), columns=columns)
    return table, RelatedDatasetIdentity(
        kind="provider", mount_id=mount_id, source_binding_id=binding_id)


def _conflict(
        source: _Source, table: CatalogTable, left: list[str], right: list[str],
        target_identity: RelatedDatasetIdentity | None = None,
) -> str | None:
    if target_identity is not None and target_identity.kind == "provider":
        target_dataset_id = workspace_providers.provider_dataset_identity(table.uri)
        target_revision_id = target_identity.revision_id
    else:
        target_dataset_id = _semantic_dataset_id(table)
        target_revision_id = target_identity.revision_id if target_identity is not None else None
    diagnoses = diagnose_key_pairs(
        left_input=input_identity(dataset_id=source.dataset_id,
                                  revision_id=source.identity.revision_id),
        right_input=input_identity(dataset_id=target_dataset_id, revision_id=target_revision_id),
        left_columns=source.columns, right_columns=table.columns,
        left_fields=left, right_fields=right)
    if not has_target_conflict(diagnoses):
        return None
    reason = next(item.reason for item in diagnoses if item.status == "conflict")
    return f"Typed row-reference evidence contradicts this key pair ({reason})."


def _fanout(cardinality: str) -> str | None:
    if cardinality not in {"1:N", "N:1", "N:M"}:
        return None
    return "This join may multiply rows; inspect the resulting Join analysis before running."


def related_datasets(catalog, resolve_adapter, storage, dataset: str | RelatedDatasetIdentity, *,
                     q: str | None = None, folder: str | None = None, limit: int = 10) -> RelatedDatasetPage:
    """Return a bounded, ranked candidate page and measure only its visible candidates."""
    source = _source_from_identity(catalog, dataset, resolve_adapter)
    bounded = max(1, min(int(limit), MAX_RELATED_DATASETS))
    scope = _scope_page(catalog, q=q, folder=folder)
    # A provider Source may only discover candidates through its same-mount canonical records.
    # The local catalog page remains a bounded UI/search context, never a provider fallback.
    scoped = ([] if source.identity.kind == "provider" else [
        item for item in scope.items if item.registration_id != source.identity.registration_id])
    scoped_ids = {item.registration_id for item in scoped if item.registration_id}
    refined = bool((q or "").strip() or (folder or "").strip("/"))
    seeds: dict[str, _Seed] = {}
    exclusions: list[RelatedDatasetExclusion] = []
    cache: dict[str, CatalogTable | None] = {}
    relationship_truncated = False
    provider_identities: dict[str, RelatedDatasetIdentity] = {}
    provider_folder_unavailable = False

    if source.identity.kind == "provider":
        # Canonical provider records deliberately do not carry placement/folder facts. Returning
        # them for a folder filter would claim an unprovable scope, so fail closed to no provider
        # candidates and tell the caller that a trustworthy refinement was unavailable.
        provider_folder_unavailable = bool((folder or "").strip("/"))
        if not provider_folder_unavailable:
            provider_rows, provider_truncated = metadb.workspace_provider_dataset_page(
                mount_id=str(source.identity.mount_id), query=q, limit=_SCOPE_LIMIT)
            relationship_truncated = provider_truncated
            for row in provider_rows:
                binding_id = str(row.get("sourceBindingId") or "")
                if not binding_id or binding_id == source.identity.source_binding_id:
                    continue
                uri = workspace_providers.provider_dataset_uri(str(source.identity.mount_id), binding_id)
                identity = RelatedDatasetIdentity(
                    kind="provider", mount_id=str(source.identity.mount_id), source_binding_id=binding_id)
                table = CatalogTable(
                    id=str(row.get("providerDatasetId") or binding_id),
                    name=str(row.get("providerDatasetId") or "provider dataset"), uri=uri,
                    columns=[ColumnSchema.model_validate(item) for item in row["columns"]])
                provider_identities[uri] = identity
                scoped.append(table)

    if source.table is not None:
        # The built-in catalog implements this as an indexed endpoint lookup. Providers that do not
        # implement the capability simply expose no owner-declared relationship candidates.
        incident = getattr(catalog, "incident_relationships", None)
        declared, relationship_truncated = (incident(source.uri, limit=_RELATIONSHIP_LIMIT)
                                             if callable(incident) else ([], False))
        for relation in declared:
            if relation.left_uri == source.uri:
                other, left, right, card = (relation.right_uri, relation.left_columns,
                                             relation.right_columns, relation.cardinality)
            elif relation.right_uri == source.uri:
                other, left, right = relation.left_uri, relation.right_columns, relation.left_columns
                card = {"1:N": "N:1", "N:1": "1:N"}.get(relation.cardinality, relation.cardinality)
            else:
                continue
            try:
                table = catalog.get_table(other)
            except Exception:
                continue
            if not table.registration_id or (refined and table.registration_id not in scoped_ids):
                continue
            if not _key_columns_are_compatible(source.columns, table.columns, list(left), list(right)):
                continue
            conflict = _conflict(source, table, list(left), list(right))
            if conflict:
                if len(exclusions) < _EXCLUSION_LIMIT:
                    exclusions.append(RelatedDatasetExclusion(identity=_candidate_identity(table),
                        name=table.name, reason=conflict))
                continue
            _put(seeds, _Seed(table, _candidate_identity(table), 0, 10, "declared_relationship", "declared",
                               "Owner-declared relationship", list(left), list(right), card))

    references_seen = 0
    for column in source.columns:
        reference = column.row_reference
        if reference is None or len(reference.key_fields) != 1:
            continue
        references_seen += 1
        if references_seen > _REFERENCE_LIMIT:
            break
        provider_target = (None if provider_folder_unavailable
                           else _provider_typed_target(source, reference, q=q))
        if provider_target is not None:
            table, provider_identity = provider_target
            if table.uri == source.uri:
                continue
        else:
            table = _target(catalog, reference, cache)
            provider_identity = None
            if table is None or not table.registration_id or table.registration_id == source.identity.registration_id:
                continue
            if refined and table.registration_id not in scoped_ids:
                continue
        exact = reference.target if isinstance(reference.target, ExactDatasetRef) else None
        # A retained typed target must bring its retained schema. If the adapter cannot prove that
        # schema, omit the candidate rather than pairing current-head columns with an exact label.
        if exact is not None:
            try:
                table = table.model_copy(update={
                    "columns": _exact_columns(table.uri, exact.revision_id, resolve_adapter)})
            except Exception:
                continue
        if not _key_columns_are_compatible(
                source.columns, table.columns, [column.name], list(reference.key_fields)):
            continue
        identity = (_provider_candidate_identity(provider_identity, exact)
                    if provider_identity is not None else _candidate_identity(table, exact))
        conflict = _conflict(source, table, [column.name], list(reference.key_fields), identity)
        if conflict:
            if len(exclusions) < _EXCLUSION_LIMIT:
                exclusions.append(RelatedDatasetExclusion(identity=identity,
                    name=table.name, reason=conflict))
            continue
        _put(seeds, _Seed(table, identity, 1, 8, "typed_reference",
                           "declared" if reference.provenance == "declared" else "proven",
                           f"{column.name} has a typed reference to {table.name}",
                           [column.name], list(reference.key_fields), exact_ref=exact))

    for table in scoped:
        identity = provider_identities.get(table.uri)
        if identity is None and not table.registration_id:
            continue
        inferred = relationships.suggest_joins(source.columns, table.columns,
                                                lambda _columns: None, lambda _columns: None)
        if not inferred:
            continue
        best = inferred[0]
        candidate_identity = identity or _candidate_identity(table)
        conflict = _conflict(source, table, list(best.left_columns), list(best.right_columns),
                             candidate_identity)
        if conflict:
            if len(exclusions) < _EXCLUSION_LIMIT:
                exclusions.append(RelatedDatasetExclusion(identity=candidate_identity,
                    name=table.name, reason=conflict))
            continue
        _put(seeds, _Seed(table, candidate_identity, 2, best.score, "schema_match", "inferred", best.reason,
                           list(best.left_columns), list(best.right_columns)))

    ranked = sorted(seeds.values(), key=lambda item: (
        item.rank, -item.score, item.table.name.casefold(), item.identity.registration_id or item.identity.source_binding_id or ""))
    visible = ranked[:bounded]
    candidates: list[RelatedDatasetCandidate] = []
    with source_read_scope(storage, [source.uri, *(item.table.uri for item in visible)],
                           owner=f"related-datasets:{source.dataset_id}"):
        for seed in visible:
            cardinality = seed.declared_cardinality
            # A current-head measurement must never be presented as evidence for an exact review.
            # Adapters do not share a portable exact-cardinality capability yet, so exact means
            # unknown here rather than a misleading current-head fact.
            if source.identity.revision_mode == "exact" or seed.exact_ref is not None:
                cardinality = "unknown"
            elif cardinality == "unknown":
                try:
                    left = relationships.measure_unique(source.uri, seed.left_columns, resolve_adapter)[0]
                    right = relationships.measure_unique(seed.table.uri, seed.right_columns, resolve_adapter)[0]
                    cardinality = relationships.cardinality(left, right)
                except Exception:
                    cardinality = "unknown"
            candidates.append(RelatedDatasetCandidate(
                identity=seed.identity, name=seed.table.name,
                folder=seed.table.folder, reason=seed.reason, evidence=seed.evidence,
                evidence_status=seed.evidence_status, left_columns=seed.left_columns,
                right_columns=seed.right_columns, cardinality=cardinality,
                confidence=("declared" if seed.evidence == "declared_relationship"
                            else "verified" if cardinality != "unknown" else "inferred"),
                exact_ref=seed.exact_ref, warning=_fanout(cardinality)))
    catalog_scope_truncated = scope.has_more if source.identity.kind == "local" else False
    truncated = catalog_scope_truncated or relationship_truncated or provider_folder_unavailable or references_seen > _REFERENCE_LIMIT \
        or len(ranked) > bounded or len(exclusions) == _EXCLUSION_LIMIT
    return RelatedDatasetPage(source=source.identity, source_name=source.name, candidates=candidates,
                              excluded=exclusions, limit=bounded, inspected=len(scoped),
                              truncated=truncated, refinement_required=truncated,
                              scope_note=("Provider folder scope cannot be proven from canonical dataset records; "
                                          "no provider candidates were returned."
                                          if provider_folder_unavailable else None))


def current_identity(catalog, expected: RelatedDatasetIdentity) -> CatalogTable:
    """Resolve one local candidate review exactly; providers are not catalog candidates today."""
    if expected.kind != "local" or not expected.registration_id:
        raise ValueError("related candidate has no local registration identity")
    table = catalog.get_table(expected.registration_id)
    observed = _local_identity(table, revision_id=expected.revision_id)
    if observed != expected:
        raise ValueError("related dataset changed after review")
    return table
