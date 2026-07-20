"""One synchronous, complete keyed upsert into a managed-local Parquet dataset (issue #636).

A directly-invocable, durably-publishable service: match a payload revision's rows against a base
revision's declared certified keys, update matched keys and insert new keys, and publish ONE exact
child revision plus an idempotent WriteReceipt with matched/inserted/unchanged/rejected/duplicate/
conflict evidence.

It invents no scheduler and no provider contract. The ordinary typed managed-local write
(``write_managed_local_file``) owns publication, compare-and-swap on the expected head, and
response-loss reconciliation; row-identity coverage (#310) owns key validation and the counts. Both
the base and the payload are immutable exact revisions, so the evidence is a pure function of them and
is recomputed — never re-published — on a replay.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from hub import db, metadb
from hub.local_writes import write_managed_local_file
from hub.models import (
    ColumnSchema, ExactDatasetRef, LineagePublication, Wire, WriteDestination, WriteIntent,
    WriteProvenance, WriteReceipt,
)
from hub.plugins.adapters import DuckDBAdapter, relation_columns
from hub.row_identity import RowIdentityCoverageV1, RowIdentityError, certify_row_identity_coverage
from hub.sqlpolicy import identifier_key
from hub.storage import source_read_scope


class KeyedUpsertError(RuntimeError):
    """Fail-closed upsert boundary; messages intentionally disclose no data values."""


class UpsertEvidenceV1(Wire):
    """Exact per-outcome counts for one keyed upsert. On a published upsert the last three are 0;
    a non-zero rejected/duplicate/conflict is a fail-closed preflight error, never a publication."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    matched: int
    inserted: int
    unchanged: int
    rejected: int
    duplicate: int
    conflict: int


class UpsertIntentV1(Wire):
    """Frozen pre-1.0 contract for one keyed upsert into a managed-local-file dataset."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    head: ExactDatasetRef
    keys: list[str]
    match_policy: Literal["update"] = "update"
    insert_policy: Literal["insert"] = "insert"
    schema_policy: Literal["exact"] = "exact"
    conflict_policy: Literal["reject"] = "reject"
    output_schema: list[ColumnSchema]
    write_intent: WriteIntent
    upsert_sha256: str = ""

    @model_validator(mode="after")
    def validate_intent(self) -> "UpsertIntentV1":
        if self.base.kind != "exact" or self.head.kind != "exact":
            raise ValueError("upsert base and head must be exact revisions")
        if not self.keys or len(self.keys) != len({identifier_key(key) for key in self.keys}):
            raise ValueError("upsert keys must be a non-empty set of unambiguous columns")
        expected = _upsert_digest(self)
        if self.upsert_sha256 and self.upsert_sha256 != expected:
            raise ValueError("upsert intent digest is invalid")
        self.upsert_sha256 = expected
        # Bind every semantic input (base, head, keys, policy, schema, destination) into the
        # idempotency key so a different payload can never dedup against this one, and a replay with
        # the same payload converges on the same receipt.
        if self.write_intent.idempotency_key != idempotency_key(expected):
            raise ValueError("upsert idempotency key must bind the upsert digest")
        return self


class UpsertOutcomeV1(Wire):
    """The published exact revision receipt plus its exact evidence counts."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    receipt: WriteReceipt
    evidence: UpsertEvidenceV1


def idempotency_key(upsert_sha256: str) -> str:
    return f"keyed-upsert:{upsert_sha256}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _upsert_digest(intent: "UpsertIntentV1") -> str:
    """Digest the stable upsert meaning. The idempotency key is derived from this, never part of it,
    and the ordinary revision ledger still owns the full WriteIntent."""
    write = intent.write_intent
    payload = {
        "base": intent.base.model_dump(by_alias=True, mode="json"),
        "head": intent.head.model_dump(by_alias=True, mode="json"),
        "keys": list(intent.keys),
        "matchPolicy": intent.match_policy,
        "insertPolicy": intent.insert_policy,
        "schemaPolicy": intent.schema_policy,
        "conflictPolicy": intent.conflict_policy,
        "outputSchema": [column.model_dump(by_alias=True, mode="json")
                         for column in intent.output_schema],
        "destination": {
            "provider": write.destination.provider,
            "name": write.destination.name,
            "datasetId": write.destination.dataset_id,
        },
        "mode": write.mode,
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def build_upsert_intent(
        *, base: ExactDatasetRef, head: ExactDatasetRef, keys: list[str],
        destination: WriteDestination, output_schema: list[ColumnSchema],
        producer: str = "keyed-upsert", producer_version: int = 1) -> UpsertIntentV1:
    """Assemble one valid UpsertIntentV1 with the idempotency key bound to the upsert digest.

    A two-pass construction: the digest is computed from a provisional intent whose idempotency key is
    a placeholder, then the real WriteIntent is rebuilt with the bound key. Later leaves (HTTP
    admission) construct the intent through this same seam so the binding lives in one place.
    """
    def _write(idempotency: str) -> WriteIntent:
        return WriteIntent(
            destination=destination, mode="replace", expected_schema=output_schema,
            expected_head=base, idempotency_key=idempotency,
            provenance=WriteProvenance(publication=LineagePublication(
                idempotency_key=idempotency, provenance="manual", producer=producer,
                producer_version=producer_version, step_id=f"keyed-upsert:{head.revision_id}"),
                parents=[]))

    probe = UpsertIntentV1.model_construct(
        base=base, head=head, keys=list(keys), match_policy="update", insert_policy="insert",
        schema_policy="exact", conflict_policy="reject", output_schema=list(output_schema),
        write_intent=_write("keyed-upsert:pending"))
    digest = _upsert_digest(probe)
    return UpsertIntentV1(
        base=base, head=head, keys=list(keys), output_schema=list(output_schema),
        write_intent=_write(idempotency_key(digest)))


def _schema_columns(schema: pa.Schema) -> list[ColumnSchema]:
    with db.base_guard():
        empty = pa.Table.from_batches([], schema=schema)
        columns = relation_columns(db.conn().from_arrow(empty))
    return [ColumnSchema(name=column.name, type=column.type) for column in columns]


def _revision_artifact(ref: ExactDatasetRef) -> str:
    uri = metadb.managed_local_file_revision_artifact(ref.dataset_id, ref.revision_id)
    if uri is None:
        raise KeyedUpsertError("upsert revision is unavailable")
    return uri


def _read_table(storage, uri: str) -> pa.Table:
    with db.base_guard(), source_read_scope(storage, [uri], owner="keyed-upsert"):
        return DuckDBAdapter().scan(uri).to_arrow_table()


def _coverage(storage, intent: UpsertIntentV1, head: pa.Table) -> RowIdentityCoverageV1:
    with db.base_guard():
        candidate = db.conn().from_arrow(head)
        return certify_row_identity_coverage(
            storage, intent.base, intent.keys, candidate, owner="keyed-upsert")


def _evidence(coverage: RowIdentityCoverageV1) -> UpsertEvidenceV1:
    return UpsertEvidenceV1(
        matched=coverage.matched_identities,
        inserted=coverage.extra_identities,
        unchanged=coverage.missing_identities,
        rejected=coverage.base.null_rows + coverage.candidate.null_rows,
        duplicate=coverage.candidate.duplicate_groups,
        conflict=coverage.base.duplicate_groups,
    )


def _build_result(base: pa.Table, head: pa.Table, keys: list[str]) -> pa.Table:
    """Head rows win on matched keys; base rows with no payload key are kept unchanged."""
    head_key_set = set(zip(*[head.column(key).to_pylist() for key in keys], strict=True))
    base_key_rows = zip(*[base.column(key).to_pylist() for key in keys], strict=True)
    keep = pa.array([row not in head_key_set for row in base_key_rows], type=pa.bool_())
    return pa.concat_tables([head, base.filter(keep)])


def _validate_write_binds_base(intent: UpsertIntentV1, base_columns: list[ColumnSchema]) -> None:
    write = intent.write_intent
    if (write.mode != "replace" or write.destination.provider != "managed-local-file"
            or write.destination.dataset_id != intent.base.dataset_id
            or write.expected_head != intent.base):
        raise KeyedUpsertError("upsert write intent is invalid")
    if intent.output_schema != base_columns or list(write.expected_schema) != base_columns:
        raise KeyedUpsertError("upsert output schema is invalid")


def _preflight_and_build(storage, intent: UpsertIntentV1) -> tuple[pa.Table, UpsertEvidenceV1]:
    head_now = metadb.catalog_managed_local_head_for_dataset(intent.base.dataset_id)
    if head_now is None or head_now.get("revision_id") != intent.base.revision_id:
        raise KeyedUpsertError("upsert expected head is stale")
    base_table = _read_table(storage, _revision_artifact(intent.base))
    head_table = _read_table(storage, _revision_artifact(intent.head))
    base_columns = _schema_columns(base_table.schema)
    if not base_table.schema.equals(head_table.schema, check_metadata=False):
        raise KeyedUpsertError("upsert payload schema does not match the base schema")
    _validate_write_binds_base(intent, base_columns)
    try:
        coverage = _coverage(storage, intent, head_table)
    except RowIdentityError as exc:
        raise KeyedUpsertError("upsert key coverage is invalid") from exc
    evidence = _evidence(coverage)
    if coverage.status == "invalid":
        raise KeyedUpsertError(
            "upsert rejected null or duplicate keys "
            f"(rejected={evidence.rejected}, duplicate={evidence.duplicate}, "
            f"conflict={evidence.conflict})")
    return _build_result(base_table, head_table, intent.keys), evidence


def upsert_managed_local_file(*, storage, catalog, intent: UpsertIntentV1) -> UpsertOutcomeV1:
    """Upsert one payload revision into a base revision and publish one exact replacement."""
    frozen = UpsertIntentV1.model_validate(intent)
    write = WriteIntent.model_validate(frozen.write_intent)
    write_doc = write.model_dump(by_alias=True, mode="json")

    prior = metadb.catalog_managed_local_write_receipt(write_doc)
    if prior is not None:
        # Lost response: both revisions are immutable, so recompute the identical evidence and return
        # the original receipt without moving the head again.
        head_table = _read_table(storage, _revision_artifact(frozen.head))
        return UpsertOutcomeV1(
            receipt=WriteReceipt.model_validate(prior),
            evidence=_evidence(_coverage(storage, frozen, head_table)))

    result, evidence = _preflight_and_build(storage, frozen)

    def writer(uri: str) -> None:
        pq.write_table(result, uri)

    try:
        receipt = write_managed_local_file(
            storage=storage, catalog=catalog, intent=write, write_artifact=writer)
    except Exception:
        prior = metadb.catalog_managed_local_write_receipt(write_doc)
        if prior is not None:
            return UpsertOutcomeV1(
                receipt=WriteReceipt.model_validate(prior), evidence=evidence)
        raise
    return UpsertOutcomeV1(receipt=receipt, evidence=evidence)
