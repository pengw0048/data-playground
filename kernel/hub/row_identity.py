"""Revision-scoped logical row identity and exact coverage for managed local data.

This is deliberately a small internal contract.  It has one source shape: a retained,
core-owned managed-local Parquet revision, and one candidate relation already owned by
the caller's DuckDB connection.  The Lance foundation below deliberately keeps its
registration-incarnation fence separate from that retained-file lifecycle.  Neither path creates
a durable sidecar or resolves a catalog head; later leaves may consume a certificate it returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq

from hub import db, metadb
from hub.models import ExactDatasetRef, MEDIA_CELL_IDENTITY_VALUE_MAX_LENGTH
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.sqlpolicy import identifier, quote_identifier
from hub.storage import ManagedSourceUnavailable, source_read_scope


_ENCODING_VERSION = "row-identity-v1"
_NULL_POLICY = "reject"
_CommitResult = TypeVar("_CommitResult")
PREVIEW_ROW_IDENTITIES_MAX_JSON_BYTES = 1024 * 1024
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


class RowIdentityRevisionMismatch(RowIdentityError):
    """The held artifact no longer matches the exact schema/spec admitted for this operation."""


class RowIdentityValidationError(RowIdentityError):
    """The declaration, schema, or evidence violates the V1 contract."""


class RowIdentityValueTooLarge(RowIdentityValidationError):
    """One identity value cannot fit the bounded public row-reference representation."""

    reason = "identity_value_over_limit"


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


@dataclass(frozen=True)
class ManagedLocalLanceRowIdentityFenceV1:
    """Private exact-Lance authority retained alongside one row-identity certificate."""

    dataset_id: str
    revision_id: str
    schema_sha256: str
    row_identity_spec_sha256: str
    physical_incarnation_sha256: str


def serialize_row_identity_coverage(
        certificate: RowIdentityCoverageV1, expected_dataset_ref: ExactDatasetRef,
        expected_spec_digest: str) -> dict:
    """Return the complete canonical V1 certificate document for durable admission.

    The document is intentionally an explicit whitelist rather than a dataclass dump: persisted
    evidence must not acquire unaudited fields as this hand-off moves between later sparse leaves.
    """
    validate_row_identity_coverage(certificate, expected_dataset_ref, expected_spec_digest)
    return {
        "version": 1,
        "spec": {
            "datasetId": certificate.spec.dataset_id,
            "revisionId": certificate.spec.revision_id,
            "fields": [{"name": field.name, "arrowType": field.arrow_type}
                       for field in certificate.spec.fields],
            "schemaDigest": certificate.spec.schema_digest,
            "encodingVersion": certificate.spec.encoding_version,
            "nullPolicy": certificate.spec.null_policy,
            "digest": certificate.spec.digest,
        },
        "base": _scan_document(certificate.base),
        "candidate": _scan_document(certificate.candidate),
        "matchedIdentities": certificate.matched_identities,
        "missingIdentities": certificate.missing_identities,
        "extraIdentities": certificate.extra_identities,
        "status": certificate.status,
    }


def decode_row_identity_coverage(
        document: object, expected_dataset_ref: ExactDatasetRef,
        expected_spec_digest: str) -> RowIdentityCoverageV1:
    """Decode only the exact V1 certificate shape and revalidate frozen authority.

    This is deliberately stricter than JSON schema coercion: booleans never become counts and an
    extra field is corruption, not a future-compatible extension of immutable evidence.
    """
    try:
        document = _exact_keys(document, {
            "version", "spec", "base", "candidate", "matchedIdentities",
            "missingIdentities", "extraIdentities", "status",
        })
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError
        spec_doc = _exact_keys(document["spec"], {
            "datasetId", "revisionId", "fields", "schemaDigest", "encodingVersion",
            "nullPolicy", "digest",
        })
        fields_doc = spec_doc["fields"]
        if not isinstance(fields_doc, list):
            raise ValueError
        fields: list[RowIdentityFieldV1] = []
        for field in fields_doc:
            field = _exact_keys(field, {"name", "arrowType"})
            fields.append(RowIdentityFieldV1(name=field["name"], arrow_type=field["arrowType"]))
        certificate = RowIdentityCoverageV1(
            spec=RowIdentitySpecV1(
                dataset_id=spec_doc["datasetId"], revision_id=spec_doc["revisionId"],
                fields=tuple(fields), schema_digest=spec_doc["schemaDigest"],
                encoding_version=spec_doc["encodingVersion"], null_policy=spec_doc["nullPolicy"],
                digest=spec_doc["digest"],
            ),
            base=_scan_from_document(document["base"]),
            candidate=_scan_from_document(document["candidate"]),
            matched_identities=document["matchedIdentities"],
            missing_identities=document["missingIdentities"],
            extra_identities=document["extraIdentities"],
            status=document["status"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RowIdentityValidationError("row identity evidence is invalid") from exc
    validate_row_identity_coverage(certificate, expected_dataset_ref, expected_spec_digest)
    return certificate


def _scan_document(scan: RowIdentityScanEvidenceV1) -> dict:
    return {
        "rows": scan.rows,
        "uniqueIdentities": scan.unique_identities,
        "nullRows": scan.null_rows,
        "duplicateGroups": scan.duplicate_groups,
        "duplicateRows": scan.duplicate_rows,
        "keySetDigest": scan.key_set_digest,
    }


def _scan_from_document(document: object) -> RowIdentityScanEvidenceV1:
    document = _exact_keys(document, {
        "rows", "uniqueIdentities", "nullRows", "duplicateGroups", "duplicateRows",
        "keySetDigest",
    })
    return RowIdentityScanEvidenceV1(
        rows=document["rows"], unique_identities=document["uniqueIdentities"],
        null_rows=document["nullRows"], duplicate_groups=document["duplicateGroups"],
        duplicate_rows=document["duplicateRows"], key_set_digest=document["keySetDigest"],
    )


def _exact_keys(document: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError
    return document


def certify_row_identity_coverage(
        storage, dataset_ref: ExactDatasetRef, key_columns: Sequence[str], candidate,
        *, owner: str = "row-identity", frozen_spec: RowIdentitySpecV1 | None = None,
) -> RowIdentityCoverageV1:
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
        with db.base_guard(), source_read_scope(
                storage, [artifact_uri], owner=owner) as guards:
            if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
                raise RowIdentityUnavailable("exact row identity source is unavailable")
            current_spec = freeze_row_identity_spec_from_parquet_fileno(
                dataset_ref, key_columns, guards[0].artifact_fileno())
            if frozen_spec is not None:
                _validate_frozen_spec(frozen_spec, dataset_ref)
                if current_spec != frozen_spec:
                    raise RowIdentityRevisionMismatch(
                        "exact row identity source no longer matches admission")
                spec = frozen_spec
            else:
                spec = current_spec
            base = DuckDBAdapter().scan(artifact_uri)
            base_schema = _relation_schema(base)
            fields = _key_fields(base_schema, declared)
            if fields != spec.fields:
                raise RowIdentityValidationError("row identity schema is invalid")
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


def certify_exact_row_identity(
        storage, dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        owner: str = "row-identity-certification",
        frozen_spec: RowIdentitySpecV1 | None = None,
) -> RowIdentityCoverageV1:
    """Certify exactly one managed-local revision's own logical key once.

    This is deliberately separate from coverage certification: there is no candidate relation and
    no interactive fallback.  The resulting complete certificate can be persisted by a caller for
    later row-addressed reads, while catalog detail remains a metadata-only lookup.
    """
    exact = _exact_ref(dataset_ref)
    declared = _declared_keys(key_columns)
    artifact_uri = metadb.managed_local_file_revision_artifact(*exact)
    if artifact_uri is None:
        raise RowIdentityUnavailable("exact row identity source is unavailable")

    try:
        with db.base_guard(), source_read_scope(
                storage, [artifact_uri], owner=owner) as guards:
            if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
                raise RowIdentityUnavailable("exact row identity source is unavailable")
            current_spec = freeze_row_identity_spec_from_parquet_fileno(
                dataset_ref, key_columns, guards[0].artifact_fileno())
            if frozen_spec is not None:
                _validate_frozen_spec(frozen_spec, dataset_ref)
                if current_spec != frozen_spec:
                    raise RowIdentityRevisionMismatch(
                        "exact row identity source no longer matches admission")
                spec = frozen_spec
            else:
                spec = current_spec
            base = DuckDBAdapter().scan(artifact_uri)
            base_schema = _relation_schema(base)
            fields = _key_fields(base_schema, declared)
            if fields != spec.fields:
                raise RowIdentityValidationError("row identity schema is invalid")
            evidence = _scan_evidence(_key_relation(base, declared), fields)
    except ManagedSourceUnavailable as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc
    except RowIdentityError:
        raise
    except Exception as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc

    status: Literal["complete", "invalid"] = (
        "invalid" if evidence.null_rows or evidence.duplicate_groups else "complete")
    certificate = RowIdentityCoverageV1(
        spec=spec, base=evidence, candidate=evidence,
        matched_identities=evidence.unique_identities, missing_identities=0,
        extra_identities=0, status=status)
    validate_row_identity_coverage(certificate, dataset_ref, spec.digest)
    return certificate


def certify_and_persist_exact_row_identity(
        storage, dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        owner: str = "row-identity-certification",
) -> dict:
    """Run the explicit whole-revision proof operation and retain its reusable descriptor."""
    def persist(
            certificate: RowIdentityCoverageV1, artifact_dev: int, artifact_ino: int) -> dict:
        if certificate.status != "complete":
            raise RowIdentityValidationError("row identity evidence is invalid")
        return metadb.managed_local_row_identity_certificate_store(
            dataset_ref.dataset_id, dataset_ref.revision_id,
            serialize_row_identity_coverage(
                certificate, dataset_ref, certificate.spec.digest),
            artifact_dev=artifact_dev, artifact_ino=artifact_ino)

    return certify_and_commit_exact_row_identity(
        storage, dataset_ref, key_columns, commit=persist, owner=owner,
    )


def certify_and_commit_exact_row_identity(
        storage, dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        commit: Callable[[RowIdentityCoverageV1, int, int], _CommitResult],
        owner: str = "row-identity-certification",
        expected_schema_sha256: str | None = None,
        expected_spec_sha256: str | None = None,
) -> _CommitResult:
    """Prove one revision and invoke its durable commit while the exact source guard is held.

    The callback is the worker's atomic metadata boundary.  Keeping it inside the outer lifecycle
    guard prevents the artifact from changing between whole-revision proof and certificate commit.
    """
    exact = _exact_ref(dataset_ref)
    artifact_uri = metadb.managed_local_file_revision_artifact(*exact)
    if artifact_uri is None:
        raise RowIdentityUnavailable("exact row identity source is unavailable")
    if (expected_schema_sha256 is None) != (expected_spec_sha256 is None):
        raise RowIdentityValidationError("row identity admission is invalid")
    try:
        with source_read_scope(storage, [artifact_uri], owner=f"{owner}:persistence") as guards:
            if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
                raise RowIdentityUnavailable("exact row identity source is unavailable")
            artifact_info = os.fstat(guards[0].artifact_fileno())
            frozen_spec = freeze_row_identity_spec_from_parquet_fileno(
                dataset_ref, key_columns, guards[0].artifact_fileno())
            if (expected_schema_sha256 is not None
                    and (frozen_spec.schema_digest != expected_schema_sha256
                         or frozen_spec.digest != expected_spec_sha256)):
                raise RowIdentityRevisionMismatch(
                    "exact row identity source no longer matches admission")
            certificate = certify_exact_row_identity(
                storage, dataset_ref, key_columns, owner=owner,
                frozen_spec=frozen_spec)
            return commit(certificate, int(artifact_info.st_dev), int(artifact_info.st_ino))
    except ManagedSourceUnavailable as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc


def freeze_managed_local_lance_row_identity_fence(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        owner: str = "managed-lance-row-identity",
) -> tuple[RowIdentitySpecV1, ManagedLocalLanceRowIdentityFenceV1]:
    """Freeze one registered Lance exact version without exposing its URI or tracked files.

    Catalog registration is the dataset incarnation fence.  The native tracked-file digest gives a
    second, physical-incarnation fence for the exact version; only that digest crosses into metadata.
    """
    exact = _exact_ref(dataset_ref)
    declared = _declared_keys(key_columns)
    binding = metadb.managed_local_lance_row_identity_binding(exact[0])
    if binding is None or binding.get("dataset_id") != exact[0]:
        raise RowIdentityUnavailable("exact row identity source is unavailable")
    try:
        with db.base_guard():
            adapter = LanceAdapter()
            schema, physical = adapter.exact_revision_incarnation(
                str(binding["uri"]), exact[1])
            if not isinstance(schema, pa.Schema):
                raise RowIdentityUnavailable("exact row identity source is unavailable")
            spec = freeze_row_identity_spec_from_schema(dataset_ref, declared, schema)
    except RowIdentityError:
        raise
    except Exception as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc
    if not isinstance(physical, str) or len(physical) != 64:
        raise RowIdentityUnavailable("exact row identity source is unavailable")
    return spec, ManagedLocalLanceRowIdentityFenceV1(
        dataset_id=exact[0], revision_id=exact[1], schema_sha256=spec.schema_digest,
        row_identity_spec_sha256=spec.digest, physical_incarnation_sha256=physical)


def certify_and_commit_managed_local_lance_row_identity(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        commit: Callable[[RowIdentityCoverageV1, ManagedLocalLanceRowIdentityFenceV1], _CommitResult],
        owner: str = "managed-lance-row-identity",
        expected_fence: ManagedLocalLanceRowIdentityFenceV1 | None = None,
) -> _CommitResult:
    """Scan one exact registered Lance key projection and commit only after a fresh fence recheck."""
    exact = _exact_ref(dataset_ref)
    declared = _declared_keys(key_columns)
    binding = metadb.managed_local_lance_row_identity_binding(exact[0])
    if binding is None or binding.get("dataset_id") != exact[0]:
        raise RowIdentityUnavailable("exact row identity source is unavailable")
    try:
        with db.base_guard():
            adapter = LanceAdapter()
            schema, physical = adapter.exact_revision_incarnation(
                str(binding["uri"]), exact[1])
            if not isinstance(schema, pa.Schema):
                raise RowIdentityUnavailable("exact row identity source is unavailable")
            spec = freeze_row_identity_spec_from_schema(dataset_ref, declared, schema)
            fence = ManagedLocalLanceRowIdentityFenceV1(
                dataset_id=exact[0], revision_id=exact[1], schema_sha256=spec.schema_digest,
                row_identity_spec_sha256=spec.digest, physical_incarnation_sha256=physical)
            if expected_fence is not None and fence != expected_fence:
                raise RowIdentityRevisionMismatch("exact row identity source no longer matches admission")
            if not row_identity_spec_is_supported(spec):
                raise RowIdentityValidationError("row identity schema is invalid")
            streamed_keys = adapter.open_revision_projection(
                str(binding["uri"]), exact[1], columns=list(declared))
            # Lance exposes an Arrow reader. Materialize only the declared key projection into a
            # temporary DuckDB table so the aggregate, duplicate check, and ordered digest can all
            # replay it; DuckDB's configured temp directory lets that relation spill to disk.
            temp_name = f"dp_lance_row_identity_{uuid.uuid4().hex}"
            try:
                streamed_keys.create(temp_name)
                base = db.conn().table(temp_name)
                fields = _key_fields(_relation_schema(base), declared)
                if fields != spec.fields:
                    raise RowIdentityRevisionMismatch(
                        "exact row identity source no longer matches admission")
                evidence = _scan_evidence(
                    _key_relation(base, declared), fields,
                    string_value_max_bytes=MEDIA_CELL_IDENTITY_VALUE_MAX_LENGTH)
                status: Literal["complete", "invalid"] = (
                    "invalid" if evidence.null_rows or evidence.duplicate_groups else "complete")
                certificate = RowIdentityCoverageV1(
                    spec=spec, base=evidence, candidate=evidence,
                    matched_identities=evidence.unique_identities, missing_identities=0,
                    extra_identities=0, status=status)
                validate_row_identity_coverage(certificate, dataset_ref, spec.digest)
                if certificate.status != "complete":
                    raise RowIdentityValidationError("row identity evidence is invalid")
            finally:
                db.conn().execute(f'DROP TABLE IF EXISTS "{temp_name}"')
            # The second exact metadata read is deliberately after all lazy relation evaluation. It
            # catches an in-place physical replacement before the metadata transaction can retain it.
            after_schema, after_physical = adapter.exact_revision_incarnation(
                str(binding["uri"]), exact[1])
            if (not isinstance(after_schema, pa.Schema)
                    or _schema_digest(after_schema) != fence.schema_sha256
                    or after_physical != fence.physical_incarnation_sha256):
                raise RowIdentityRevisionMismatch("exact row identity source no longer matches admission")
            return commit(certificate, fence)
    except (RowIdentityError, metadb.ManagedLocalLanceRowIdentityCertificateConflict):
        raise
    except Exception as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc


def certify_and_persist_managed_local_lance_row_identity(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        owner: str = "managed-lance-row-identity",
) -> dict:
    """Run the private whole-revision Lance proof and retain its exact registration fence."""
    exact = _exact_ref(dataset_ref)

    def persist(certificate: RowIdentityCoverageV1,
                fence: ManagedLocalLanceRowIdentityFenceV1) -> dict:
        return metadb.managed_local_lance_row_identity_certificate_store(
            exact[0], exact[1],
            serialize_row_identity_coverage(certificate, dataset_ref, fence.row_identity_spec_sha256),
            physical_incarnation_sha256=fence.physical_incarnation_sha256,
            schema_sha256=fence.schema_sha256,
            row_identity_spec_sha256=fence.row_identity_spec_sha256)

    return certify_and_commit_managed_local_lance_row_identity(
        dataset_ref, key_columns, commit=persist, owner=owner)


def managed_local_lance_row_identity_certificate(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], *,
        owner: str = "managed-lance-row-identity",
) -> RowIdentityCoverageV1 | None:
    """Load a retained Lance proof only after re-deriving its exact physical fence, without a scan."""
    spec, fence = freeze_managed_local_lance_row_identity_fence(
        dataset_ref, key_columns, owner=owner)
    if not row_identity_spec_is_supported(spec):
        return None
    return metadb.managed_local_lance_row_identity_certificate_for_fence(
        fence.dataset_id, fence.revision_id,
        physical_incarnation_sha256=fence.physical_incarnation_sha256,
        schema_sha256=fence.schema_sha256,
        row_identity_spec_sha256=fence.row_identity_spec_sha256)


def freeze_row_identity_spec_from_schema(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], schema: pa.Schema,
) -> RowIdentitySpecV1:
    """Freeze V1 identity from a bounded retained Parquet schema metadata read."""
    exact = _exact_ref(dataset_ref)
    declared = _declared_keys(key_columns)
    # Admission must retain a deterministic typed intent even when V1 cannot prove that Arrow
    # type.  The worker checks ``row_identity_spec_is_supported`` and terminalizes it without a
    # scan; executable proof paths continue to call the strict default below.
    return _spec(exact, _key_fields(schema, declared, require_supported=False), schema)


def freeze_row_identity_spec_from_parquet_fileno(
        dataset_ref: ExactDatasetRef, key_columns: Sequence[str], artifact_fileno: int,
) -> RowIdentitySpecV1:
    """Freeze the canonical Parquet footer schema through an already-held artifact handle."""
    try:
        with os.fdopen(os.dup(artifact_fileno), "rb") as artifact:
            schema = pq.ParquetFile(artifact).schema_arrow
    except (OSError, pa.ArrowException) as exc:
        raise RowIdentityUnavailable("exact row identity source is unavailable") from exc
    return freeze_row_identity_spec_from_schema(dataset_ref, key_columns, schema)


def row_identity_spec_is_supported(spec: RowIdentitySpecV1) -> bool:
    """Whether the frozen key types can be proven by the V1 whole-revision worker."""
    return all(field.arrow_type in _SUPPORTED_TYPES for field in spec.fields)


def canonicalize_preview_row_identities(
        table: pa.Table, certificate: RowIdentityCoverageV1,
) -> list[list[dict[str, str]] | None] | None:
    """Build #826-ready identities from one already-materialized exact Arrow preview.

    A schema/certificate mismatch fails the complete sidecar closed. Individual null or
    unrepresentable values retain their row position as ``None``. The budget is the exact compact
    JSON UTF-8 size of the array, including list punctuation and required null placeholders.
    """
    if not isinstance(table, pa.Table) or not isinstance(certificate, RowIdentityCoverageV1):
        return None
    try:
        expected = ExactDatasetRef(
            kind="exact", dataset_id=certificate.spec.dataset_id,
            revision_id=certificate.spec.revision_id)
        validate_row_identity_coverage(certificate, expected, certificate.spec.digest)
    except (TypeError, ValueError, RowIdentityValidationError):
        return None
    if (certificate.status != "complete"
            or certificate.spec.schema_digest != _schema_digest(table.schema)):
        return None

    columns: list[tuple[RowIdentityFieldV1, pa.ChunkedArray]] = []
    for field in certificate.spec.fields:
        indices = table.schema.get_all_field_indices(field.name)
        if len(indices) != 1 or str(table.schema.field(indices[0]).type) != field.arrow_type:
            return None
        columns.append((field, table.column(indices[0])))

    identities: list[list[dict[str, str]] | None] = [None] * table.num_rows
    used = len(json.dumps(
        identities, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    for row_index in range(table.num_rows):
        identity: list[dict[str, str]] = []
        for field, column in columns:
            scalar = column[row_index]
            if not scalar.is_valid:
                identity = []
                break
            try:
                value = scalar.as_py()
            except Exception:  # noqa: BLE001 - one unrepresentable row fails closed
                identity = []
                break
            if field.arrow_type == "string":
                canonical = (
                    value if type(value) is str
                    and len(value) <= MEDIA_CELL_IDENTITY_VALUE_MAX_LENGTH else None)
            else:
                canonical = str(value) if type(value) is int else None
            if canonical is None:
                identity = []
                break
            identity.append({
                "name": field.name,
                "arrowType": field.arrow_type,
                "value": canonical,
            })
        if not identity:
            continue
        encoded = json.dumps(
            identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        added = len(encoded) - len(b"null")
        if used + added <= PREVIEW_ROW_IDENTITIES_MAX_JSON_BYTES:
            identities[row_index] = identity
            used += added
    return identities


def _validate_frozen_spec(spec: RowIdentitySpecV1, dataset_ref: ExactDatasetRef) -> None:
    expected = _exact_ref(dataset_ref)
    if (not isinstance(spec, RowIdentitySpecV1) or spec.digest != _spec_digest(
            spec.dataset_id, spec.revision_id, spec.fields, spec.schema_digest)
            or (spec.dataset_id, spec.revision_id) != expected):
        raise RowIdentityValidationError("row identity evidence is invalid")


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


def _key_fields(
        schema: pa.Schema, keys: tuple[str, ...], *, require_supported: bool = True,
) -> tuple[RowIdentityFieldV1, ...]:
    fields: list[RowIdentityFieldV1] = []
    for name in keys:
        try:
            field = schema.field(name)
        except (KeyError, IndexError) as exc:
            raise RowIdentityValidationError("row identity schema is invalid") from exc
        arrow_type = str(field.type)
        if require_supported and arrow_type not in _SUPPORTED_TYPES:
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


def _scan_evidence(
        relation, fields: tuple[RowIdentityFieldV1, ...], *,
        string_value_max_bytes: int | None = None,
) -> RowIdentityScanEvidenceV1:
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
            ", ".join(quoted)), fields,
            string_value_max_bytes=string_value_max_bytes),
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


def _key_set_digest(
        relation, fields: tuple[RowIdentityFieldV1, ...], *,
        string_value_max_bytes: int | None = None,
) -> str:
    ordered = relation.order(", ".join(quote_identifier(field.name) for field in fields))
    hasher = hashlib.sha256(b"row-identity-key-set-v1\\0")
    reader = ordered.to_arrow_reader(batch_size=65_536)
    for batch in reader:
        for row_index in range(batch.num_rows):
            values = tuple(batch.column(index)[row_index].as_py() for index in range(batch.num_columns))
            hasher.update(_encode_identity(
                fields, values, string_value_max_bytes=string_value_max_bytes))
    return hasher.hexdigest()


def _encode_identity(
        fields: tuple[RowIdentityFieldV1, ...], values: tuple[object, ...], *,
        string_value_max_bytes: int | None = None,
) -> bytes:
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
            if string_value_max_bytes is not None and len(payload) > string_value_max_bytes:
                raise RowIdentityValueTooLarge("row identity value exceeds its bounded public limit")
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
