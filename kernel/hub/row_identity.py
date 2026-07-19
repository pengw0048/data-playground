"""Revision-scoped logical row identity and exact coverage for managed local Parquet.

This is deliberately a small internal contract.  It has one source shape: a retained,
core-owned managed-local Parquet revision, and one candidate relation already owned by
the caller's DuckDB connection.  It does not create a durable sidecar or resolve a
catalog head; later leaves may consume the certificate it returns.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import pyarrow as pa

from hub import db, metadb
from hub.models import ExactDatasetRef
from hub.plugins.adapters import DuckDBAdapter
from hub.sqlpolicy import identifier, quote_identifier
from hub.storage import ManagedSourceUnavailable, source_read_scope


_ENCODING_VERSION = "row-identity-v1"
_NULL_POLICY = "reject"
_SUPPORTED_TYPES: dict[str, tuple[str, int | None, bool | None]] = {
    "int8": ("i8", 1, True), "int16": ("i16", 2, True),
    "int32": ("i32", 4, True), "int64": ("i64", 8, True),
    "uint8": ("u8", 1, False), "uint16": ("u16", 2, False),
    "uint32": ("u32", 4, False), "uint64": ("u64", 8, False),
    "string": ("utf8", None, None),
}


class RowIdentityError(RuntimeError):
    """Stable fail-closed error; its text intentionally contains no data identifiers."""


class RowIdentityUnavailable(RowIdentityError):
    """The retained exact artifact or its mandatory lifecycle guard is unavailable."""


class RowIdentityValidationError(RowIdentityError):
    """The declaration, schema, or evidence violates the V1 contract."""


@dataclass(frozen=True)
class RowIdentityFieldV1:
    """One ordered logical-key field, with its exact Arrow V1 type fact."""

    name: str
    arrow_type: str


@dataclass(frozen=True)
class RowIdentitySpecV1:
    """The revision-bound, canonical V1 identity declaration."""

    dataset_id: str
    revision_id: str
    fields: tuple[RowIdentityFieldV1, ...]
    schema_digest: str
    encoding_version: Literal["row-identity-v1"] = _ENCODING_VERSION
    null_policy: Literal["reject"] = _NULL_POLICY
    digest: str = ""


@dataclass(frozen=True)
class RowIdentityScanEvidenceV1:
    """Bounded facts from one whole-key scan; never carries a key or physical location."""

    rows: int
    unique_identities: int
    null_rows: int
    duplicate_groups: int
    duplicate_rows: int
    key_set_digest: str | None


@dataclass(frozen=True)
class RowIdentityCoverageV1:
    """The internal hand-off for one exact base revision and one candidate relation."""

    spec: RowIdentitySpecV1
    base: RowIdentityScanEvidenceV1
    candidate: RowIdentityScanEvidenceV1
    matched_identities: int
    missing_identities: int
    extra_identities: int
    status: Literal["complete", "partial", "invalid"]


def certify_row_identity_coverage(
        storage, dataset_ref: ExactDatasetRef, key_columns: Sequence[str], candidate,
        *, owner: str = "row-identity") -> RowIdentityCoverageV1:
    """Certify raw-key coverage for one *exact* core-owned managed-local revision.

    ``candidate`` must be a DuckDB relation built on the caller's current connection.  Keeping
    ownership there avoids silently copying a potentially large relation into Python or a new
    temporary artifact.  Both relations are compared with typed SQL equality; SHA-256 is evidence
    only and cannot establish equality.
    """
    exact = _exact_ref(dataset_ref)
    declared = _declared_keys(key_columns)
    artifact_uri = metadb.managed_local_file_revision_artifact(*exact)
    if artifact_uri is None:
        raise RowIdentityUnavailable("exact row identity source is unavailable")

    try:
        with db.base_guard(), source_read_scope(storage, [artifact_uri], owner=owner):
            base = DuckDBAdapter().scan(artifact_uri)
            base_schema = _relation_schema(base)
            fields = _key_fields(base_schema, declared)
            spec = _spec(exact, fields, base_schema)
            _require_candidate_schema(candidate, declared, fields)

            base_keys = _key_relation(base, declared)
            candidate_keys = _key_relation(candidate, declared)
            base_evidence = _scan_evidence(base_keys, fields)
            candidate_evidence = _scan_evidence(candidate_keys, fields)
            matched, missing, extra = _raw_coverage(base_keys, candidate_keys, declared)
    except ManagedSourceUnavailable as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc
    except RowIdentityError:
        raise
    except Exception as exc:
        # Relation reads are lazy.  Do not expose a filesystem/provider/SQL detail when one fails.
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc

    invalid = any((
        base_evidence.null_rows, base_evidence.duplicate_groups,
        candidate_evidence.null_rows, candidate_evidence.duplicate_groups,
    ))
    status: Literal["complete", "partial", "invalid"]
    if invalid:
        status = "invalid"
    elif missing or extra:
        status = "partial"
    else:
        status = "complete"
    certificate = RowIdentityCoverageV1(
        spec=spec, base=base_evidence, candidate=candidate_evidence,
        matched_identities=matched, missing_identities=missing,
        extra_identities=extra, status=status)
    validate_row_identity_coverage(certificate, dataset_ref, spec.digest)
    return certificate


def validate_row_identity_coverage(
        certificate: RowIdentityCoverageV1, expected_dataset_ref: ExactDatasetRef,
        expected_spec_digest: str) -> None:
    """Bind a certificate to caller-frozen authority before a later leaf consumes it."""
    if not isinstance(certificate, RowIdentityCoverageV1):
        raise RowIdentityValidationError("row identity evidence is invalid")
    try:
        expected_exact = _exact_ref(expected_dataset_ref)
    except RowIdentityValidationError as exc:
        raise RowIdentityValidationError("row identity evidence is invalid") from exc
    if not _digest(expected_spec_digest):
        raise RowIdentityValidationError("row identity evidence is invalid")
    spec = certificate.spec
    if (not isinstance(spec, RowIdentitySpecV1)
            or type(spec.dataset_id) is not str or type(spec.revision_id) is not str
            or not isinstance(spec.fields, tuple)
            or type(spec.schema_digest) is not str or type(spec.digest) is not str
            or not spec.dataset_id or not spec.revision_id or not spec.fields
            or spec.encoding_version != _ENCODING_VERSION or spec.null_policy != _NULL_POLICY
            or not _digest(spec.schema_digest)
            or any(not isinstance(field, RowIdentityFieldV1)
                   or type(field.name) is not str or type(field.arrow_type) is not str
                   or not field.name or field.arrow_type not in _SUPPORTED_TYPES
                   for field in spec.fields)
            or len({field.name for field in spec.fields}) != len(spec.fields)
            or spec.digest != _spec_digest(spec.dataset_id, spec.revision_id, spec.fields,
                                           spec.schema_digest)
            or (spec.dataset_id, spec.revision_id) != expected_exact
            or spec.digest != expected_spec_digest):
        raise RowIdentityValidationError("row identity evidence is invalid")
    for scan in (certificate.base, certificate.candidate):
        if (not isinstance(scan, RowIdentityScanEvidenceV1)
                or any(not _nonnegative_int(value) for value in (
                    scan.rows, scan.unique_identities, scan.null_rows,
                    scan.duplicate_groups, scan.duplicate_rows))
                or scan.unique_identities > scan.rows - scan.null_rows
                or scan.duplicate_groups > scan.unique_identities
                or scan.rows - scan.null_rows != (
                    scan.unique_identities - scan.duplicate_groups + scan.duplicate_rows)
                or (scan.duplicate_groups == 0) != (scan.duplicate_rows == 0)
                or (scan.null_rows and scan.key_set_digest is not None)
                or (not scan.null_rows and not _digest(scan.key_set_digest))):
            raise RowIdentityValidationError("row identity evidence is invalid")
    if (any(not _nonnegative_int(value) for value in (
            certificate.matched_identities, certificate.missing_identities,
            certificate.extra_identities))
            or type(certificate.status) is not str):
        raise RowIdentityValidationError("row identity evidence is invalid")
    if certificate.status not in {"complete", "partial", "invalid"}:
        raise RowIdentityValidationError("row identity evidence is invalid")
    if (certificate.matched_identities + certificate.missing_identities
            != certificate.base.unique_identities
            or certificate.matched_identities + certificate.extra_identities
            != certificate.candidate.unique_identities):
        raise RowIdentityValidationError("row identity evidence is invalid")
    if (certificate.base.key_set_digest is not None
            and certificate.candidate.key_set_digest is not None
            and ((certificate.base.key_set_digest == certificate.candidate.key_set_digest)
                 != (certificate.missing_identities == certificate.extra_identities == 0))):
        raise RowIdentityValidationError("row identity evidence is invalid")
    invalid = any((certificate.base.null_rows, certificate.base.duplicate_groups,
                   certificate.candidate.null_rows, certificate.candidate.duplicate_groups))
    expected = "invalid" if invalid else (
        "partial" if certificate.missing_identities or certificate.extra_identities else "complete")
    if certificate.status != expected:
        raise RowIdentityValidationError("row identity evidence is invalid")


def _exact_ref(dataset_ref: ExactDatasetRef) -> tuple[str, str]:
    if not isinstance(dataset_ref, ExactDatasetRef):
        raise RowIdentityValidationError("row identity declaration is invalid")
    if dataset_ref.kind != "exact" or not dataset_ref.dataset_id or not dataset_ref.revision_id:
        raise RowIdentityValidationError("row identity declaration is invalid")
    return dataset_ref.dataset_id, dataset_ref.revision_id


def _declared_keys(key_columns: Sequence[str]) -> tuple[str, ...]:
    if isinstance(key_columns, str) or not key_columns:
        raise RowIdentityValidationError("row identity declaration is invalid")
    keys = tuple(key_columns)
    if any(not isinstance(name, str) or not name for name in keys) or len(set(keys)) != len(keys):
        raise RowIdentityValidationError("row identity declaration is invalid")
    return keys


def _relation_schema(relation) -> pa.Schema:
    try:
        return relation.limit(0).to_arrow_table().schema
    except Exception as exc:
        raise RowIdentityValidationError("row identity relation is invalid") from exc


def _key_fields(schema: pa.Schema, keys: tuple[str, ...]) -> tuple[RowIdentityFieldV1, ...]:
    fields: list[RowIdentityFieldV1] = []
    for name in keys:
        try:
            field = schema.field(name)
        except (KeyError, IndexError) as exc:
            raise RowIdentityValidationError("row identity schema is invalid") from exc
        arrow_type = str(field.type)
        if arrow_type not in _SUPPORTED_TYPES:
            raise RowIdentityValidationError("row identity schema is invalid")
        fields.append(RowIdentityFieldV1(name=name, arrow_type=arrow_type))
    return tuple(fields)


def _require_candidate_schema(candidate, keys: tuple[str, ...],
                              fields: tuple[RowIdentityFieldV1, ...]) -> None:
    candidate_fields = _key_fields(_relation_schema(candidate), keys)
    if candidate_fields != fields:
        raise RowIdentityValidationError("row identity schema is invalid")


def _schema_digest(schema: pa.Schema) -> str:
    facts = [(field.name, str(field.type), bool(field.nullable)) for field in schema]
    return _sha256_json({"schema": facts})


def _spec(exact: tuple[str, str], fields: tuple[RowIdentityFieldV1, ...],
          schema: pa.Schema) -> RowIdentitySpecV1:
    schema_digest = _schema_digest(schema)
    digest = _spec_digest(exact[0], exact[1], fields, schema_digest)
    return RowIdentitySpecV1(
        dataset_id=exact[0], revision_id=exact[1], fields=fields,
        schema_digest=schema_digest, digest=digest)


def _spec_digest(dataset_id: str, revision_id: str,
                 fields: tuple[RowIdentityFieldV1, ...], schema_digest: str) -> str:
    return _sha256_json({
        "version": _ENCODING_VERSION, "datasetId": dataset_id, "revisionId": revision_id,
        "nullPolicy": _NULL_POLICY, "fields": [(field.name, field.arrow_type) for field in fields],
        "schemaDigest": schema_digest,
    })


def _key_relation(relation, keys: tuple[str, ...]):
    columns = relation.columns
    selected = [identifier(name, columns, label="row identity key") for name in keys]
    return relation.project(", ".join(quote_identifier(name) for name in selected))


def _scan_evidence(relation, fields: tuple[RowIdentityFieldV1, ...]) -> RowIdentityScanEvidenceV1:
    keys = tuple(field.name for field in fields)
    quoted = tuple(quote_identifier(name) for name in keys)
    null_condition = " OR ".join(f"{name} IS NULL" for name in quoted)
    rows, null_rows = relation.aggregate(
        f"count(*) AS rows, count(*) FILTER (WHERE {null_condition}) AS null_rows").fetchone()
    non_null = relation.filter(f"NOT ({null_condition})")
    groups = non_null.aggregate(
        f"{', '.join(quoted)}, count(*) AS n", ", ".join(quoted))
    unique_identities = int(groups.aggregate("count(*) AS groups").fetchone()[0])
    duplicates = groups.filter("n > 1")
    duplicate_groups, duplicate_rows = duplicates.aggregate(
        "count(*) AS groups, coalesce(sum(n), 0) AS rows").fetchone()
    rows, null_rows = int(rows), int(null_rows)
    duplicate_groups, duplicate_rows = int(duplicate_groups), int(duplicate_rows)
    return RowIdentityScanEvidenceV1(
        rows=rows,
        unique_identities=unique_identities,
        null_rows=null_rows,
        duplicate_groups=duplicate_groups,
        duplicate_rows=duplicate_rows,
        key_set_digest=None if null_rows else _key_set_digest(groups.project(
            ", ".join(quoted)), fields),
    )


def _raw_coverage(base, candidate, keys: tuple[str, ...]) -> tuple[int, int, int]:
    """Return distinct non-null raw-key SEMI/ANTI facts without hash-based equality."""
    quoted = tuple(quote_identifier(name) for name in keys)
    base_distinct = base.filter(" AND ".join(f"{name} IS NOT NULL" for name in quoted)).distinct()
    candidate_distinct = candidate.filter(
        " AND ".join(f"{name} IS NOT NULL" for name in quoted)).distinct()
    base_distinct = base_distinct.set_alias("base_identity")
    candidate_distinct = candidate_distinct.set_alias("candidate_identity")
    predicate = " AND ".join(
        f"base_identity.{name} = candidate_identity.{name}" for name in quoted)
    matched = base_distinct.join(candidate_distinct, predicate, "semi")
    missing = base_distinct.join(candidate_distinct, predicate, "anti")
    extra = candidate_distinct.join(base_distinct, predicate, "anti")
    return tuple(int(relation.aggregate("count(*) AS n").fetchone()[0])
                 for relation in (matched, missing, extra))


def _key_set_digest(relation, fields: tuple[RowIdentityFieldV1, ...]) -> str:
    ordered = relation.order(", ".join(quote_identifier(field.name) for field in fields))
    hasher = hashlib.sha256(b"row-identity-key-set-v1\\0")
    reader = ordered.to_arrow_reader(batch_size=65_536)
    for batch in reader:
        for row_index in range(batch.num_rows):
            values = tuple(batch.column(index)[row_index].as_py() for index in range(batch.num_columns))
            hasher.update(_encode_identity(fields, values))
    return hasher.hexdigest()


def _encode_identity(fields: tuple[RowIdentityFieldV1, ...], values: tuple[object, ...]) -> bytes:
    if len(fields) != len(values):  # defensive; this is never user-facing data
        raise RowIdentityValidationError("row identity evidence is invalid")
    encoded = bytearray(b"RI1" + len(fields).to_bytes(2, "big"))
    for field, value in zip(fields, values, strict=True):
        if value is None:
            raise RowIdentityValidationError("row identity evidence is invalid")
        tag, width, signed = _SUPPORTED_TYPES[field.arrow_type]
        if width is None:
            if not isinstance(value, str):
                raise RowIdentityValidationError("row identity evidence is invalid")
            payload = value.encode("utf-8")
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise RowIdentityValidationError("row identity evidence is invalid")
            try:
                payload = value.to_bytes(width, "big", signed=bool(signed))
            except OverflowError as exc:
                raise RowIdentityValidationError("row identity evidence is invalid") from exc
        tag_bytes = tag.encode("ascii")
        encoded.extend(len(tag_bytes).to_bytes(1, "big"))
        encoded.extend(tag_bytes)
        encoded.extend(len(payload).to_bytes(8, "big"))
        encoded.extend(payload)
    return bytes(encoded)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _digest(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0
