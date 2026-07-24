"""Provider-neutral, bounded row-reference target diagnosis.

This module deliberately compares only retained identities and normalized field evidence.  It does
not inspect values, names, URIs, or provider metadata, so every admission and advisory surface can
share the same conservative answer.
"""
from __future__ import annotations

from hub.models import (
    CanonicalDatasetRef,
    ColumnSchema,
    ExactDatasetRef,
    RowReferenceDiagnosis,
    RowReferenceInputIdentity,
    TypedRowReference,
)
from hub.sqlpolicy import identifier_key


ROW_REFERENCE_TARGET_MISMATCH = "row_reference_target_mismatch"


def input_identity(*, dataset_id: str | None, revision_id: str | None = None) -> RowReferenceInputIdentity | None:
    """Build one bounded exact/canonical identity from already-resolved catalog facts."""
    if not dataset_id:
        return None
    return RowReferenceInputIdentity(
        kind="exact" if revision_id else "canonical",
        dataset_id=dataset_id, revision_id=revision_id,
    )


def _field(columns: list[ColumnSchema], name: str) -> ColumnSchema | None:
    matches = [column for column in columns if identifier_key(column.name) == identifier_key(name)]
    return matches[0] if len(matches) == 1 else None


def _target_outcome(target: ExactDatasetRef | CanonicalDatasetRef, peer: RowReferenceInputIdentity | None) -> tuple[str, str]:
    """Return status/reason for a positive target fact against one peer identity."""
    if peer is None:
        return "unknown", "peer_identity_unavailable"
    if target.kind == "exact":
        # Exact evidence is only comparable to another exact identity.  Dataset-id equality alone
        # must not silently retarget a reference across revisions.
        if peer.kind != "exact":
            return "unknown", "peer_exact_identity_unavailable"
        if (target.dataset_id, target.revision_id) == (peer.dataset_id, peer.revision_id):
            return "compatible", "exact_target_matches_peer"
        return "conflict", "exact_target_differs_from_peer"
    if target.dataset_id == peer.dataset_id:
        return "compatible", "canonical_target_matches_peer"
    return "conflict", "canonical_target_differs_from_peer"


def diagnose_key_pair(
        *, left_input: RowReferenceInputIdentity | None,
        right_input: RowReferenceInputIdentity | None,
        left_columns: list[ColumnSchema], right_columns: list[ColumnSchema],
        left_field: str, right_field: str,
        left_peer_fields: list[str] | None = None,
        right_peer_fields: list[str] | None = None,
) -> RowReferenceDiagnosis:
    """Diagnose one configured/suggested key pair without ever inventing compatibility.

    A known target contradiction wins over cardinality and over a compatible fact on the other key.
    Missing, stale, ambiguous, or unavailable field evidence stays ``unknown``.
    """
    left = _field(left_columns, left_field)
    right = _field(right_columns, right_field)
    left_ref: TypedRowReference | None = left.row_reference if left is not None else None
    right_ref: TypedRowReference | None = right.row_reference if right is not None else None
    outcomes: list[tuple[str, str]] = []
    expected_right = left_peer_fields if left_peer_fields is not None else [right_field]
    expected_left = right_peer_fields if right_peer_fields is not None else [left_field]
    if left_ref is not None:
        if _same_field_sequence(left_ref.key_fields, expected_right):
            outcomes.append(_target_outcome(left_ref.target, right_input))
        else:
            outcomes.append(("conflict", "declared_target_key_differs_from_join_key"))
    if right_ref is not None:
        if _same_field_sequence(right_ref.key_fields, expected_left):
            outcomes.append(_target_outcome(right_ref.target, left_input))
        else:
            outcomes.append(("conflict", "declared_target_key_differs_from_join_key"))
    if any(status == "conflict" for status, _reason in outcomes):
        status = "conflict"
        reason = next(reason for outcome, reason in outcomes if outcome == "conflict")
    elif any(status == "compatible" for status, _reason in outcomes):
        status = "compatible"
        reason = next(reason for outcome, reason in outcomes if outcome == "compatible")
    elif outcomes:
        status, reason = "unknown", outcomes[0][1]
    else:
        status, reason = "unknown", "no_row_reference_evidence"
    return RowReferenceDiagnosis(
        left_input=left_input, right_input=right_input,
        left_field=left_field, right_field=right_field,
        left_target=left_ref.target if left_ref is not None else None,
        right_target=right_ref.target if right_ref is not None else None,
        left_key_fields=left_ref.key_fields if left_ref is not None else [],
        right_key_fields=right_ref.key_fields if right_ref is not None else [],
        status=status, evidence_source="row_reference" if outcomes else "none", reason=reason,
    )


def _same_field_sequence(left: list[str], right: list[str]) -> bool:
    return len(left) == len(right) and all(
        identifier_key(a) == identifier_key(b) for a, b in zip(left, right, strict=True))


def diagnose_key_pairs(
        *, left_input: RowReferenceInputIdentity | None,
        right_input: RowReferenceInputIdentity | None,
        left_columns: list[ColumnSchema], right_columns: list[ColumnSchema],
        left_fields: list[str], right_fields: list[str],
) -> list[RowReferenceDiagnosis]:
    """Return one bounded diagnosis per aligned key pair; malformed pairs are unknown facts."""
    if len(left_fields) != len(right_fields):
        return [RowReferenceDiagnosis(
            left_input=left_input, right_input=right_input,
            left_field=left_fields[0] if left_fields else "",
            right_field=right_fields[0] if right_fields else "",
            status="unknown", reason="join_key_pair_malformed",
        )]
    pairs = zip(left_fields, right_fields, strict=True)
    return [diagnose_key_pair(
        left_input=left_input, right_input=right_input,
        left_columns=left_columns, right_columns=right_columns,
        left_field=left_field, right_field=right_field,
        left_peer_fields=right_fields, right_peer_fields=left_fields,
    ) for left_field, right_field in pairs]


def has_target_conflict(diagnoses: list[RowReferenceDiagnosis]) -> bool:
    return any(diagnosis.status == "conflict" for diagnosis in diagnoses)


def diagnose_durable_field_projections(
        *, sidecar: RowReferenceInputIdentity, base: RowReferenceInputIdentity,
        fields: list[str], projections: list[dict], state: str,
) -> list[RowReferenceDiagnosis]:
    """Interpret normalized exact field-lineage projections without provider metadata.

    Projection absence/truncation is intentionally unknown.  Managed identity admission is stricter
    than ordinary joins and decides how to reject that unknown evidence at its own boundary.
    """
    diagnoses: list[RowReferenceDiagnosis] = []
    for field in fields:
        matches = [
            item for item in projections
            if isinstance(item.get("destination_field"), str)
            and identifier_key(item["destination_field"]) == identifier_key(field)
        ]
        target = None
        status, reason = "unknown", "durable_projection_unavailable"
        if state == "available" and len(matches) == 1:
            projection = matches[0]
            source_id = projection.get("source_dataset_id")
            source_revision = projection.get("source_version")
            if isinstance(source_id, str) and isinstance(source_revision, str):
                target = ExactDatasetRef(
                    kind="exact", dataset_id=source_id, revision_id=source_revision)
                status, reason = _target_outcome(target, base)
            else:  # defensive: persisted projection DTO normally validates this
                reason = "durable_projection_unavailable"
            source_field = projection.get("source_field")
            if (isinstance(source_field, str)
                    and identifier_key(source_field) != identifier_key(field)):
                status, reason = "conflict", "durable_projection_source_field_differs_from_identity"
            elif not isinstance(source_field, str) and status != "conflict":
                status, reason = "unknown", "durable_projection_source_field_unavailable"
        elif state == "available" and not matches:
            reason = "durable_projection_missing"
        elif state == "available":
            reason = "durable_projection_ambiguous"
        diagnoses.append(RowReferenceDiagnosis(
            left_input=sidecar, right_input=base, left_field=field, right_field=field,
            left_target=target, left_key_fields=[field], status=status,
            evidence_source="row_reference" if target is not None else "none", reason=reason,
        ))
    return diagnoses
