"""Pure admission for merging one exact managed-local Parquet sidecar.

This module deliberately has no publication, task, or route authority.  Its only output is the
ordinary replace ``WriteIntent`` plus the semantic facts required to reproduce that admission.
"""
from __future__ import annotations

import hashlib
import json

from pydantic import ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from hub import db, metadb
from hub.merge_columns import MergeColumnRuleV1, MergeColumnsError, merge_output_schema
from hub.models import (
    ColumnSchema, ExactDatasetRef, Wire, WriteDestination, WriteIntent, WriteProvenance,
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
    provenance: WriteProvenance

    @model_validator(mode="after")
    def validate_request(self) -> "ManagedSidecarMergeRequestV1":
        if self.idempotency_key != self.idempotency_key.strip():
            raise ValueError("merge idempotency key cannot contain surrounding whitespace")
        if len(set(self.identity_columns)) != len(self.identity_columns) or any(
                not name or name != name.strip() for name in self.identity_columns):
            raise ValueError("merge identity columns are invalid")
        if self.provenance.publication.idempotency_key != self.idempotency_key:
            raise ValueError("merge provenance identity must match the idempotency key")
        return self


class ManagedSidecarMergeIntentV1(Wire):
    """The complete semantic admission record; it intentionally excludes physical artifacts."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    sidecar: ExactDatasetRef
    expected_head: ExactDatasetRef
    row_identity_spec_sha256: str
    coverage: dict
    rules: list[MergeColumnRuleV1]
    output_schema: list[ColumnSchema]
    write_intent: WriteIntent
    merge_sha256: str = ""

    @model_validator(mode="after")
    def validate_digest(self) -> "ManagedSidecarMergeIntentV1":
        try:
            # Reopenable intents must prove their serialized certificate remains bound to exactly
            # this base and spec digest; a digest over arbitrary JSON is not that proof.
            decode_row_identity_coverage(
                self.coverage, self.base, self.row_identity_spec_sha256)
        except RowIdentityError as exc:
            raise ValueError("managed sidecar merge coverage is invalid") from exc
        expected = hashlib.sha256(_canonical(_semantic_payload(self)).encode()).hexdigest()
        if self.merge_sha256 and self.merge_sha256 != expected:
            raise ValueError("managed sidecar merge intent digest is invalid")
        self.merge_sha256 = expected
        return self


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
        "outputSchema": [column.model_dump(by_alias=True, mode="json")
                         for column in intent.output_schema],
        "writeIntent": intent.write_intent.model_dump(by_alias=True, mode="json"),
    }


def _exact(ref: ExactDatasetRef, field: str) -> ExactDatasetRef:
    if not isinstance(ref, ExactDatasetRef) or ref.kind != "exact":
        raise ManagedSidecarMergeError(f"managed sidecar merge {field} is invalid")
    return ref


def _schema(relation):
    return relation.limit(0).to_arrow_table().schema


def admit_managed_sidecar_merge(
        *, storage, request: ManagedSidecarMergeRequestV1) -> ManagedSidecarMergeIntentV1:
    """Validate exact revisions and return a side-effect-free, frozen replacement intent."""
    request = ManagedSidecarMergeRequestV1.model_validate(request)
    base, sidecar, expected = (_exact(request.base, "base"), _exact(request.sidecar, "sidecar"),
                               _exact(request.expected_head, "expected head"))
    if base != expected:
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
    if coverage.status != "complete":
        raise ManagedSidecarMergeError("managed sidecar merge requires complete identity coverage")
    try:
        output_schema = merge_output_schema(
            base_schema, sidecar_schema, request.identity_columns, request.rules)
    except MergeColumnsError as exc:
        raise ManagedSidecarMergeError(str(exc)) from exc
    write = WriteIntent(
        destination=WriteDestination(
            logical_uri=str(head["logical_uri"]), name=str(head["name"]),
            dataset_id=base.dataset_id),
        mode="replace", expected_schema=output_schema, expected_head=base,
        idempotency_key=request.idempotency_key, provenance=request.provenance)
    coverage_doc = serialize_row_identity_coverage(coverage, base, coverage.spec.digest)
    return ManagedSidecarMergeIntentV1(
        base=base, sidecar=sidecar, expected_head=base,
        row_identity_spec_sha256=coverage.spec.digest, coverage=coverage_doc,
        rules=request.rules, output_schema=output_schema, write_intent=write)
