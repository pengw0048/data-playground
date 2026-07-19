"""One synchronous, complete SparseOutput -> managed-local Parquet merge.

This is intentionally an internal reference path: it has no task, route, or provider contract.  The
only durable addition is the semantic replay record; the ordinary typed write still owns publication.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from hub import db, metadb
from hub.local_writes import write_managed_local_file
from hub.models import ColumnSchema, ExactDatasetRef, PlanDigest, Wire, WriteIntent, WriteReceipt
from hub.plugins.adapters import DuckDBAdapter, relation_columns
from hub.row_identity import (
    certify_row_identity_coverage, decode_row_identity_coverage,
)
from hub.sparse_outputs import reopen_sparse_output_context
from hub.storage import source_read_scope
from hub.sqlpolicy import identifier_key


class MergeColumnsError(RuntimeError):
    """Fail-closed merge boundary; messages intentionally do not disclose data values."""


class MergeColumnRuleV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    source: str
    target: str
    mode: Literal["add", "replace"]


class SparseOutputMergeEvidenceV1(Wire):
    """Frozen, non-physical SparseOutput facts consumed by one merge meaning."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    admission_intent_sha256: PlanDigest
    producer_sha256: PlanDigest
    config_sha256: PlanDigest
    provenance_sha256: PlanDigest
    admission_schema_sha256: PlanDigest
    row_identity_spec_sha256: PlanDigest
    materialized_content_sha256: PlanDigest
    materialized_schema_sha256: PlanDigest
    materialized_coverage_sha256: PlanDigest


class MergeColumnsIntentV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    sparse_output_id: str
    sparse_evidence: SparseOutputMergeEvidenceV1
    rules: list[MergeColumnRuleV1]
    policy: Literal["require_complete"] = "require_complete"
    output_schema: list[ColumnSchema]
    write_intent: WriteIntent
    merge_sha256: str = ""

    @model_validator(mode="after")
    def validate_merge_digest(self) -> "MergeColumnsIntentV1":
        payload = _merge_semantic_payload(self)
        expected = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        if self.merge_sha256 and self.merge_sha256 != expected:
            raise ValueError("merge intent digest is invalid")
        self.merge_sha256 = expected
        return self


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _merge_semantic_payload(intent: MergeColumnsIntentV1) -> dict:
    """Whitelist merge meaning while leaving physical/logical URI truth in the ordinary ledger."""
    write = WriteIntent.model_validate(intent.write_intent)
    write_doc = write.model_dump(by_alias=True, mode="json")
    return {
        "base": intent.base.model_dump(by_alias=True, mode="json"),
        "sparseOutputId": intent.sparse_output_id,
        "sparseEvidence": intent.sparse_evidence.model_dump(by_alias=True, mode="json"),
        "rules": [rule.model_dump(by_alias=True, mode="json") for rule in intent.rules],
        "policy": intent.policy,
        "outputSchema": [column.model_dump(by_alias=True, mode="json")
                         for column in intent.output_schema],
        "write": {
            "destination": {
                "provider": write.destination.provider,
                "name": write.destination.name,
                "datasetId": write.destination.dataset_id,
            },
            "mode": write.mode,
            "expectedSchema": [column.model_dump(by_alias=True, mode="json")
                               for column in write.expected_schema],
            "expectedHead": (write.expected_head.model_dump(by_alias=True, mode="json")
                             if write.expected_head is not None else None),
            "idempotencyKey": write.idempotency_key,
            # The ordinary revision ledger owns the full WriteIntent, including logical URI and
            # lineage parents.  This digest binds it without copying URI-shaped authority here.
            "intentSha256": hashlib.sha256(_canonical(write_doc).encode()).hexdigest(),
        },
    }


def _as_exact(value: ExactDatasetRef) -> ExactDatasetRef:
    if not isinstance(value, ExactDatasetRef) or value.kind != "exact":
        raise MergeColumnsError("merge exact base is invalid")
    return value


def _schema_columns(schema: pa.Schema) -> list[ColumnSchema]:
    with db.base_guard():
        empty = pa.Table.from_batches([], schema=schema)
        columns = relation_columns(db.conn().from_arrow(empty))
    return [ColumnSchema(name=column.name, type=column.type) for column in columns]


def side_fields_name(schema: pa.Schema, requested: str) -> str:
    """Resolve a V1 identifier through the same collision semantics as validation."""
    key = identifier_key(requested)
    matches = [field.name for field in schema if identifier_key(field.name) == key]
    if len(matches) != 1:  # validation has already rejected this; keep lazy Arrow indexing fail-closed
        raise MergeColumnsError("merge schema is ambiguous")
    return matches[0]


def _validate_intent(intent: MergeColumnsIntentV1, admission: dict, base_schema: pa.Schema,
                     sidecar_schema: pa.Schema) -> tuple[ExactDatasetRef, list[str]]:
    exact = _as_exact(intent.base)
    if (admission["inputDatasetId"], admission["inputRevisionId"]) != (
            exact.dataset_id, exact.revision_id):
        raise MergeColumnsError("merge SparseOutput base is invalid")
    write = WriteIntent.model_validate(intent.write_intent)
    if (write.mode != "replace" or write.destination.provider != "managed-local-file"
            or write.destination.dataset_id != exact.dataset_id or write.expected_head != exact):
        raise MergeColumnsError("merge write intent is invalid")
    evidence = admission["documents"]["evidence"]
    frozen = decode_row_identity_coverage(
        evidence, exact, admission["rowIdentitySpecSha256"]).spec
    identities = [field.name for field in frozen.fields]
    base_fields = {identifier_key(field.name): field for field in base_schema}
    side_fields = {identifier_key(field.name): field for field in sidecar_schema}
    if len(base_fields) != len(base_schema) or len(side_fields) != len(sidecar_schema):
        raise MergeColumnsError("merge schema is ambiguous")
    if (not intent.rules
            or len(intent.rules) != len({identifier_key(rule.target) for rule in intent.rules})):
        raise MergeColumnsError("merge rules are invalid")
    if len(intent.rules) != len({identifier_key(rule.source) for rule in intent.rules}):
        raise MergeColumnsError("merge rules are invalid")
    for rule in intent.rules:
        source_key, target_key = identifier_key(rule.source), identifier_key(rule.target)
        if (source_key not in side_fields or target_key in {identifier_key(name) for name in identities}
                or source_key in {identifier_key(name) for name in identities}
                or not rule.source or not rule.target):
            raise MergeColumnsError("merge rules are invalid")
        target = base_fields.get(target_key)
        if rule.mode == "add":
            if target is not None:
                raise MergeColumnsError("merge add target already exists")
        elif target is None or target.type != side_fields[source_key].type:
            raise MergeColumnsError("merge replace type is invalid")
    payload = [field.name for field in sidecar_schema if identifier_key(field.name) not in {
        identifier_key(name) for name in identities}]
    if {identifier_key(rule.source) for rule in intent.rules} != {
            identifier_key(name) for name in payload}:
        raise MergeColumnsError("merge rules do not consume the certified sidecar payload")
    expected = _schema_columns(base_schema) + [
        ColumnSchema(name=rule.target, type=_schema_columns(
            pa.schema([side_fields[identifier_key(rule.source)]]))[0].type)
        for rule in intent.rules if rule.mode == "add"
    ]
    if intent.output_schema != expected or write.expected_schema != expected:
        raise MergeColumnsError("merge output schema is invalid")
    return exact, identities


@contextmanager
def _sidecar_table(guard):
    source = os.fdopen(os.dup(guard.artifact_fileno()), "rb")
    try:
        yield pq.read_table(source)
    finally:
        source.close()


def sparse_output_merge_evidence(admission: dict, committed: dict) -> SparseOutputMergeEvidenceV1:
    """Freeze the exact redacted facts a caller must bind before merge execution."""
    digests = admission.get("digests") or {}
    coverage_doc = _canonical(committed.get("coverage"))
    try:
        return SparseOutputMergeEvidenceV1(
            admission_intent_sha256=digests["intent"], producer_sha256=digests["producer"],
            config_sha256=digests["config"], provenance_sha256=digests["provenance"],
            admission_schema_sha256=digests["schema"],
            row_identity_spec_sha256=admission["rowIdentitySpecSha256"],
            materialized_content_sha256=committed["contentSha256"],
            materialized_schema_sha256=committed["schemaSha256"],
            materialized_coverage_sha256=hashlib.sha256(coverage_doc.encode()).hexdigest(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MergeColumnsError("merge SparseOutput evidence is invalid") from exc


def merge_columns_document(intent: MergeColumnsIntentV1) -> str:
    # The frozen intent contains only semantic evidence; no URI, lock, descriptor, inode, token, or
    # lease can enter this document.  Its own digest is validated by the model above.
    payload = _merge_semantic_payload(intent)
    payload["mergeSha256"] = intent.merge_sha256
    return _canonical({"version": 1, "intent": payload})


def merge_columns_publication_context(
        intent: MergeColumnsIntentV1, *, task_id: str | None = None,
        attempt_id: str | None = None,
        owner_token: str | None = None) -> metadb.MergeColumnsPublicationContext:
    """Return the private semantic companion required by the ordinary typed write."""
    frozen = MergeColumnsIntentV1.model_validate(intent)
    document = merge_columns_document(frozen)
    return metadb.MergeColumnsPublicationContext(
        merge_doc=document, merge_sha256=hashlib.sha256(document.encode()).hexdigest(),
        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)


def merge_sparse_output_candidate(*, storage, intent: MergeColumnsIntentV1) -> pa.Table:
    """Build one complete Parquet candidate without publication.

    The durable merge Task uses this only before its candidate checkpoint commits.  Keeping this
    sidecar/base reopening and identity proof separate from publication makes the post-commit restart
    boundary explicit: publication consumes checkpoint bytes and cannot re-run this function.
    """
    frozen = MergeColumnsIntentV1.model_validate(intent)
    # SparseOutput reopen performs a bounded DuckDB coverage scan before returning the held guard.
    # Serialize that scan on the process-local connection just like the base and merge scans below.
    with db.base_guard():
        held_sidecar = reopen_sparse_output_context(storage, frozen.sparse_output_id)
    with held_sidecar as held:
        sidecar_guard, admission, committed = held.guard, held.admission, held.committed
        if sparse_output_merge_evidence(admission, committed) != frozen.sparse_evidence:
            raise MergeColumnsError("merge SparseOutput evidence changed")
        base_uri = metadb.managed_local_file_revision_artifact(
            frozen.base.dataset_id, frozen.base.revision_id)
        if base_uri is None:
            raise MergeColumnsError("merge exact base is unavailable")
        with _sidecar_table(sidecar_guard) as sidecar:
            with db.base_guard(), source_read_scope(storage, [base_uri], owner="merge-columns"):
                base = DuckDBAdapter().scan(base_uri).to_arrow_table()
            exact, identities = _validate_intent(frozen, admission, base.schema, sidecar.schema)
            with db.base_guard():
                side_relation = db.conn().from_arrow(sidecar)
                coverage = certify_row_identity_coverage(
                    storage, exact, identities, side_relation, owner="merge-columns",
                    frozen_spec=decode_row_identity_coverage(
                        admission["documents"]["evidence"], exact,
                        admission["rowIdentitySpecSha256"]).spec)
            if coverage.status != "complete":
                raise MergeColumnsError("merge requires complete logical identity coverage")
            # Build by immutable logical keys, never positions.  Coverage has already ruled out null
            # and duplicates, so this map is a total one-to-one correspondence.
            source_index = {tuple(sidecar[column][row].as_py() for column in identities): row
                            for row in range(sidecar.num_rows)}
            arrays: list[pa.Array] = []
            rule_by_target = {identifier_key(rule.target): rule for rule in frozen.rules}
            for field, column in zip(base.schema, base.columns, strict=True):
                rule = rule_by_target.get(identifier_key(field.name))
                if rule is None:
                    arrays.append(column)
                    continue
                source = sidecar[side_fields_name(sidecar.schema, rule.source)]
                arrays.append(pa.array([source[source_index[tuple(base[key][row].as_py() for key in identities)]].as_py()
                                        for row in range(base.num_rows)], type=field.type))
            for rule in frozen.rules:
                if rule.mode == "add":
                    source = sidecar[side_fields_name(sidecar.schema, rule.source)]
                    arrays.append(pa.array([source[source_index[tuple(base[key][row].as_py() for key in identities)]].as_py()
                                            for row in range(base.num_rows)], type=source.type))
            output = pa.Table.from_arrays(arrays, names=[field.name for field in base.schema] + [
                rule.target for rule in frozen.rules if rule.mode == "add"])
    return output


def merge_sparse_output_columns(*, storage, catalog, intent: MergeColumnsIntentV1) -> WriteReceipt:
    """Merge one certified sidecar with full identity coverage and publish one typed replacement."""
    frozen = MergeColumnsIntentV1.model_validate(intent)
    write = WriteIntent.model_validate(frozen.write_intent)
    publication = merge_columns_publication_context(frozen)
    prior = metadb.catalog_managed_local_write_receipt(
        write.model_dump(by_alias=True, mode="json"),
        merge_publication=publication)
    if prior is not None:
        return WriteReceipt.model_validate(prior)
    output = merge_sparse_output_candidate(storage=storage, intent=frozen)

    def writer(uri: str) -> None:
        pq.write_table(output, uri)

    try:
        return write_managed_local_file(
            storage=storage, catalog=catalog, intent=write, write_artifact=writer,
            merge_publication=publication)
    except Exception:
        # Generic receipt recovery is only acceptable when its companion semantic row committed in
        # the same transaction.  If it did, the next call returns it before reopening the sidecar.
        prior = metadb.catalog_managed_local_write_receipt(
            write.model_dump(by_alias=True, mode="json"),
            merge_publication=publication)
        if prior is not None:
            return WriteReceipt.model_validate(prior)
        raise
