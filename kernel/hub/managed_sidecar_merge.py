"""Pure admission for merging one exact managed-local Parquet sidecar.

This module deliberately has no publication, task, or route authority.  Its only output is the
ordinary replace ``WriteIntent`` plus the semantic facts required to reproduce that admission.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from hub import db, metadb
from hub.merge_columns import (
    MergeColumnRuleV1, MergeColumnsError, merge_output_columns, schema_columns,
)
from hub.models import (
    ColumnSchema, ExactDatasetRef, LineagePublication, PlanDigest, Wire, WriteDestination,
    WriteIntent, WriteProvenance,
)
from hub.plugins.adapters import DuckDBAdapter
from hub.row_identity import (
    RowIdentityError, certify_row_identity_coverage, decode_row_identity_coverage,
    serialize_row_identity_coverage,
)
from hub.storage import source_read_scope


class ManagedSidecarMergeError(RuntimeError):
    """Fail-closed managed-sidecar admission boundary."""


class ManagedSidecarMergeRequestV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    sidecar: ExactDatasetRef
    expected_head: ExactDatasetRef
    identity_columns: list[str] = Field(min_length=1, max_length=16)
    rules: list[MergeColumnRuleV1] = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=2048)
    publication: LineagePublication

    @model_validator(mode="after")
    def validate_request(self) -> "ManagedSidecarMergeRequestV1":
        if self.idempotency_key != self.idempotency_key.strip():
            raise ValueError("merge idempotency key cannot contain surrounding whitespace")
        if len(set(self.identity_columns)) != len(self.identity_columns) or any(
                not name or name != name.strip() for name in self.identity_columns):
            raise ValueError("merge identity columns are invalid")
        if self.publication.idempotency_key != self.idempotency_key:
            raise ValueError("merge provenance identity must match the idempotency key")
        self.base = _canonical_exact(self.base)
        self.sidecar = _canonical_exact(self.sidecar)
        self.expected_head = _canonical_exact(self.expected_head)
        return self


class ManagedSidecarMergeIntentV1(Wire):
    """The complete semantic admission record; it intentionally excludes physical artifacts."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    sidecar: ExactDatasetRef
    expected_head: ExactDatasetRef
    row_identity_spec_sha256: PlanDigest
    coverage: dict
    rules: list[MergeColumnRuleV1]
    base_schema: list[ColumnSchema]
    sidecar_schema: list[ColumnSchema]
    output_schema: list[ColumnSchema]
    write_intent: WriteIntent
    merge_sha256: PlanDigest

    @model_validator(mode="after")
    def validate_digest(self) -> "ManagedSidecarMergeIntentV1":
        self.base = _canonical_exact(self.base)
        self.sidecar = _canonical_exact(self.sidecar)
        self.expected_head = _canonical_exact(self.expected_head)
        self.write_intent.expected_head = (
            _canonical_exact(self.write_intent.expected_head)
            if self.write_intent.expected_head is not None else None)
        try:
            # Reopenable intents must prove their serialized certificate remains bound to exactly
            # this base and spec digest; a digest over arbitrary JSON is not that proof.
            coverage = decode_row_identity_coverage(
                self.coverage, self.base, self.row_identity_spec_sha256)
        except RowIdentityError as exc:
            raise ValueError("managed sidecar merge coverage is invalid") from exc
        if coverage.status != "complete":
            raise ValueError("managed sidecar merge coverage is incomplete")
        if not _same_exact(self.base, self.expected_head):
            raise ValueError("managed sidecar merge expected head is invalid")
        write = self.write_intent
        if (write.mode != "replace" or write.destination.provider != "managed-local-file"
                or write.destination.dataset_id != self.base.dataset_id
                or write.expected_head is None
                or not _same_exact(write.expected_head, self.expected_head)
                or write.expected_schema != self.output_schema
                or write.provenance.parents != []):
            raise ValueError("managed sidecar merge write intent is invalid")
        try:
            output = merge_output_columns(
                self.base_schema, self.sidecar_schema,
                [field.name for field in coverage.spec.fields], self.rules)
        except MergeColumnsError as exc:
            raise ValueError("managed sidecar merge rules are invalid") from exc
        if output != self.output_schema:
            raise ValueError("managed sidecar merge output schema is invalid")
        expected = hashlib.sha256(_canonical(_semantic_payload(self)).encode()).hexdigest()
        if self.merge_sha256 != expected:
            raise ValueError("managed sidecar merge intent digest is invalid")
        return self


@dataclass(frozen=True)
class PreparedManagedSidecarMerge:
    """Read-only preflight facts; non-complete coverage is deliberately representable."""

    request: ManagedSidecarMergeRequestV1
    destination: WriteDestination
    base_schema: list[ColumnSchema]
    sidecar_schema: list[ColumnSchema]
    coverage: object
    output_schema: list[ColumnSchema]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _semantic_payload(intent: ManagedSidecarMergeIntentV1) -> dict:
    return {
        "base": intent.base.model_dump(by_alias=True, mode="json"),
        "sidecar": intent.sidecar.model_dump(by_alias=True, mode="json"),
        "expectedHead": intent.expected_head.model_dump(by_alias=True, mode="json"),
        "rowIdentitySpecSha256": intent.row_identity_spec_sha256,
        "coverage": intent.coverage,
        "rules": [rule.model_dump(by_alias=True, mode="json") for rule in intent.rules],
        "baseSchema": [column.model_dump(by_alias=True, mode="json")
                       for column in intent.base_schema],
        "sidecarSchema": [column.model_dump(by_alias=True, mode="json")
                          for column in intent.sidecar_schema],
        "outputSchema": [column.model_dump(by_alias=True, mode="json")
                         for column in intent.output_schema],
        "writeIntent": intent.write_intent.model_dump(by_alias=True, mode="json"),
    }


def _canonical_exact(ref: ExactDatasetRef) -> ExactDatasetRef:
    if not isinstance(ref, ExactDatasetRef) or ref.kind != "exact":
        raise ValueError("managed sidecar merge exact reference is invalid")
    return ExactDatasetRef(kind="exact", dataset_id=ref.dataset_id, revision_id=ref.revision_id)


def _same_exact(left: ExactDatasetRef, right: ExactDatasetRef) -> bool:
    return (left.dataset_id, left.revision_id) == (right.dataset_id, right.revision_id)


def _exact(ref: ExactDatasetRef, field: str) -> ExactDatasetRef:
    if not isinstance(ref, ExactDatasetRef) or ref.kind != "exact":
        raise ManagedSidecarMergeError(f"managed sidecar merge {field} is invalid")
    return _canonical_exact(ref)


def _schema(relation):
    return relation.limit(0).to_arrow_table().schema


def prepare_managed_sidecar_merge(
        *, storage, request: ManagedSidecarMergeRequestV1) -> PreparedManagedSidecarMerge:
    """Read exact revisions and return coverage facts without admitting incomplete coverage."""
    request = ManagedSidecarMergeRequestV1.model_validate(request)
    base, sidecar, expected = (_exact(request.base, "base"), _exact(request.sidecar, "sidecar"),
                               _exact(request.expected_head, "expected head"))
    if not _same_exact(base, expected):
        raise ManagedSidecarMergeError("managed sidecar merge expected head is invalid")
    head = metadb.catalog_managed_local_head_for_dataset(base.dataset_id)
    if (head is None or head.get("state") != "active" or head.get("revision_id") != base.revision_id
            or head.get("dataset_id") != base.dataset_id or not head.get("logical_uri")
            or not head.get("name")):
        raise ManagedSidecarMergeError("managed sidecar merge base head moved")
    base_uri = metadb.managed_local_file_revision_artifact(base.dataset_id, base.revision_id)
    sidecar_uri = metadb.managed_local_file_revision_artifact(
        sidecar.dataset_id, sidecar.revision_id)
    if base_uri is None or sidecar_uri is None:
        raise ManagedSidecarMergeError("managed sidecar merge exact revision is unavailable")
    try:
        # The sidecar stays within this lifecycle scope while its lazy relation is inspected and
        # certified. ``certify_row_identity_coverage`` independently fences the exact base. The
        # ledger has no format field, so this protected scan is the evidence that it is readable
        # as the core-owned Parquet relation required by this admission.
        with db.base_guard(), source_read_scope(
                storage, [base_uri, sidecar_uri], owner="managed-sidecar-merge"):
            base_schema = _schema(DuckDBAdapter().scan(base_uri))
            side_relation = DuckDBAdapter().scan(sidecar_uri)
            sidecar_schema = _schema(side_relation)
            coverage = certify_row_identity_coverage(
                storage, base, request.identity_columns, side_relation,
                owner="managed-sidecar-merge")
    except ManagedSidecarMergeError:
        raise
    except Exception as exc:
        raise ManagedSidecarMergeError("managed sidecar merge revision is unavailable") from exc
    try:
        base_columns, sidecar_columns = schema_columns(base_schema), schema_columns(sidecar_schema)
        output_schema = merge_output_columns(
            base_columns, sidecar_columns, request.identity_columns, request.rules)
    except MergeColumnsError as exc:
        raise ManagedSidecarMergeError(str(exc)) from exc
    return PreparedManagedSidecarMerge(
        request=request,
        destination=WriteDestination(
            logical_uri=str(head["logical_uri"]), name=str(head["name"]),
            dataset_id=base.dataset_id),
        base_schema=base_columns, sidecar_schema=sidecar_columns,
        coverage=coverage, output_schema=output_schema)


def _build_intent(**values) -> ManagedSidecarMergeIntentV1:
    """Compute the digest from a validation-free first pass, then validate the final wire DTO."""
    draft = ManagedSidecarMergeIntentV1.model_construct(merge_sha256="0" * 64, **values)
    digest = hashlib.sha256(_canonical(_semantic_payload(draft)).encode()).hexdigest()
    return ManagedSidecarMergeIntentV1(merge_sha256=digest, **values)


def admit_managed_sidecar_merge(
        *, storage, request: ManagedSidecarMergeRequestV1) -> ManagedSidecarMergeIntentV1:
    """Consume complete preflight facts and return a frozen ordinary replace intent."""
    prepared = prepare_managed_sidecar_merge(storage=storage, request=request)
    if prepared.coverage.status != "complete":
        raise ManagedSidecarMergeError("managed sidecar merge requires complete identity coverage")
    base = prepared.request.base
    write = WriteIntent(
        destination=prepared.destination,
        mode="replace", expected_schema=prepared.output_schema, expected_head=base,
        idempotency_key=prepared.request.idempotency_key,
        provenance=WriteProvenance(publication=prepared.request.publication, parents=[]))
    coverage_doc = serialize_row_identity_coverage(
        prepared.coverage, base, prepared.coverage.spec.digest)
    return _build_intent(
        base=base, sidecar=prepared.request.sidecar, expected_head=base,
        row_identity_spec_sha256=prepared.coverage.spec.digest, coverage=coverage_doc,
        rules=prepared.request.rules, base_schema=prepared.base_schema,
        sidecar_schema=prepared.sidecar_schema, output_schema=prepared.output_schema,
        write_intent=write)
