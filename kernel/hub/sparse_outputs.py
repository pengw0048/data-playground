"""Admission-only SparseOutput contract for one exact managed-local Parquet revision.

This internal leaf freezes Source -> built-in Select metadata and exact row-identity evidence.  It
does not reserve a sidecar artifact, create a file, or claim materialization has happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from hub import db, metadb
from hub.models import ExactDatasetRef, LineagePublication
from hub.plugins.adapters import DuckDBAdapter
from hub.row_identity import (
    RowIdentityError,
    certify_row_identity_coverage,
    decode_row_identity_coverage,
    freeze_row_identity_spec,
    serialize_row_identity_coverage,
)
from hub.sqlpolicy import FragmentKind, SQLPolicyError, identifier_key, quote_identifier, validate_fragment
from hub.storage import ManagedSourceUnavailable, source_read_scope


class SparseOutputError(RuntimeError):
    """Stable fail-closed SparseOutput admission error."""


class SparseOutputValidationError(SparseOutputError):
    """The restricted Source -> Select immutable admission is invalid."""


class SparseOutputUnavailable(SparseOutputError):
    """The caller's exact core-managed revision is not available for admission."""


class SparseOutputSubmissionConflict(SparseOutputError):
    """One owner/canvas/submission identity has different immutable semantics."""


class SparseOutputMaterializationConflict(SparseOutputError):
    """The one internal sidecar owner is stale or disagrees with immutable admission facts."""


@dataclass(frozen=True)
class SparseOutputAdmissionRequest:
    """The only accepted V1 producer: exact Source followed by built-in Select projection."""

    owner_id: str
    canvas_id: str
    submission_id: str
    dataset_ref: ExactDatasetRef
    select_config: dict[str, object]
    identity_columns: Sequence[str]
    provenance: dict[str, object]


@dataclass(frozen=True)
class SparseOutputAdmission:
    id: str
    created: bool
    document: dict


@dataclass(frozen=True)
class SparseOutputMaterialization:
    id: str
    committed: bool
    evidence: dict | None


def admit_sparse_output(storage, request: SparseOutputAdmissionRequest) -> SparseOutputAdmission:
    """Atomically admit/replay a sparse output and retain exactly its ready exact base artifact.

    The outer guard intentionally spans projection binding, coverage, and metadata commit.  A base
    revision therefore cannot be released between evidence collection and its durable retention ref.
    """
    owner_id = _identity_text(request.owner_id, "owner", 512)
    canvas_id = _identity_text(request.canvas_id, "canvas", 512)
    submission_id = _submission_id(request.submission_id)
    dataset_ref = _exact_ref(request.dataset_ref)
    expression = _select_expression(request.select_config)
    identity_columns = _identity_columns(request.identity_columns)
    provenance = _lineage_provenance(request.provenance)
    artifact_uri = metadb.managed_local_file_revision_artifact(
        dataset_ref.dataset_id, dataset_ref.revision_id)
    if artifact_uri is None:
        raise SparseOutputUnavailable("SparseOutput exact base revision is unavailable")

    try:
        with db.base_guard(), source_read_scope(
                storage, [artifact_uri], owner=f"sparse-output-admission:{uuid.uuid4().hex}"):
            base = DuckDBAdapter().scan(artifact_uri)
            validated = validate_fragment(FragmentKind.PROJECTION, expression, con=db.conn())
            if any(function.name.lower() == "row_number" for function in validated.functions):
                raise SparseOutputValidationError("SparseOutput Select config is invalid")
            candidate = base.project(validated.sql)
            # Projection is lazy. Bind its schema while this exact read guard is definitely live; do
            # not inspect the relation again after coverage closes its nested guard.
            output_schema = candidate.limit(0).to_arrow_table().schema
            identity_schema, payload_schema = _output_schema(output_schema, identity_columns)
            frozen_spec = freeze_row_identity_spec(dataset_ref, identity_columns, base)
            coverage = certify_row_identity_coverage(
                storage, dataset_ref, identity_columns, candidate,
                owner=f"sparse-output-coverage:{uuid.uuid4().hex}", frozen_spec=frozen_spec)
            evidence = serialize_row_identity_coverage(
                coverage, dataset_ref, frozen_spec.digest)
            documents, digests = _immutable_documents(
                owner_id, canvas_id, submission_id, dataset_ref, validated.sql,
                identity_schema, payload_schema, provenance, evidence, frozen_spec.digest)
            sparse_id = _sparse_id(owner_id, canvas_id, submission_id)
            try:
                document, created = metadb.sparse_output_admit(
                    owner_id=owner_id, canvas_id=canvas_id, submission_id=submission_id,
                    sparse_id=sparse_id, input_dataset_id=dataset_ref.dataset_id,
                    input_revision_id=dataset_ref.revision_id,
                    documents=documents, digests=digests,
                    row_identity_spec_sha256=frozen_spec.digest)
            except metadb.SparseOutputSubmissionConflict as exc:
                raise SparseOutputSubmissionConflict(
                    "SparseOutput submission belongs to a different immutable admission") from exc
    except SparseOutputError:
        raise
    except (ManagedSourceUnavailable, RowIdentityError, SQLPolicyError, ValueError, duckdb.Error) as exc:
        raise SparseOutputValidationError("SparseOutput admission is invalid") from exc
    except OSError as exc:
        raise SparseOutputUnavailable("SparseOutput exact base revision is unavailable") from exc
    return SparseOutputAdmission(id=document["id"], created=created, document=document)


def materialize_sparse_output(
        storage, sparse_id: str, owner_token: str, *, lease_seconds: int = 300,
) -> SparseOutputMaterialization:
    """Materialize/replay one frozen Source -> Select sidecar under one DB-time fenced owner."""
    token = _token(owner_token)
    if not getattr(storage, "lock_supported", False):
        raise SparseOutputMaterializationConflict("SparseOutput materialization requires OS locks")
    lock_token = uuid.uuid4().hex
    try:
        candidate = metadb.reserve_sparse_output_materialization(
            sparse_id=str(sparse_id), owner_token=token, lease_seconds=lease_seconds,
            namespace_id=storage.namespace_id, storage_root=storage.result_root,
            lock_token=lock_token)
        if candidate.get("phase") == "committed":
            return _revalidated_materialization_result(storage, str(sparse_id), candidate)
        admission = metadb.sparse_output_materialization_input(str(sparse_id))
        frozen = decode_row_identity_coverage(
            admission["documents"]["evidence"],
            ExactDatasetRef(kind="exact", dataset_id=admission["inputDatasetId"],
                            revision_id=admission["inputRevisionId"]),
            admission["rowIdentitySpecSha256"],
        ).spec
        writer = None
        proof = None
        reattached_lock = None
        try:
            if candidate["dev"] is None:
                writer = storage.materialize_checkpoint(candidate)
                metadb.bind_sparse_output_materialization(
                    sparse_id=str(sparse_id), owner_token=token, uri=writer.uri,
                    dev=writer.identity()[0], ino=writer.identity()[1])
                with os.fdopen(os.dup(writer.fileno()), "wb") as output:
                    _write_frozen_select(storage, admission, output)
                writer.seal()
                proof = storage.prove_checkpoint(writer)
            else:
                reattached_lock, proof = storage.reattach_checkpoint(candidate)
            _validate_held_schema(proof.artifact_fileno(), admission)
            exact = ExactDatasetRef(kind="exact", dataset_id=admission["inputDatasetId"],
                                    revision_id=admission["inputRevisionId"])
            with _held_parquet_relation(proof.artifact_fileno()) as candidate_relation:
                coverage = certify_row_identity_coverage(
                    storage, exact, [field.name for field in frozen.fields], candidate_relation,
                    owner=f"sparse-output-materialization:{sparse_id}", frozen_spec=frozen)
            coverage_doc = _canonical_json(serialize_row_identity_coverage(
                coverage, exact, frozen.digest))
            proof.recheck()
            committed = metadb.commit_sparse_output_materialization(
                sparse_id=str(sparse_id), owner_token=token,
                namespace_id=candidate["namespace_id"], storage_root=candidate["storage_root"],
                lock_name=candidate["lock_name"], lock_token=candidate["lock_token"],
                lock_protected=candidate["lock_protected"],
                generation=candidate["generation"],
                rows=proof.evidence["rows"], size_bytes=proof.evidence["bytes"],
                content_sha256=proof.evidence["content_sha256"],
                schema_sha256=proof.evidence["schema_sha256"], dev=proof.evidence["dev"],
                ino=proof.evidence["ino"], coverage_doc=coverage_doc)
            return _materialization_result(str(sparse_id), committed)
        except Exception:
            reconciled = metadb.reconcile_sparse_output_materialization(str(sparse_id))
            if reconciled and reconciled.get("phase") == "committed":
                if proof is not None:
                    _revalidate_held_proof(storage, str(sparse_id), admission, proof, reconciled)
                    return _materialization_result(str(sparse_id), reconciled)
                return _revalidated_materialization_result(storage, str(sparse_id), reconciled)
            raise
        finally:
            if proof is not None:
                proof.close()
            if writer is not None:
                writer.release()
            if reattached_lock is not None:
                os.close(reattached_lock)
    except SparseOutputError:
        raise
    except (ManagedSourceUnavailable, RowIdentityError, ValueError, OSError, duckdb.Error) as exc:
        raise SparseOutputMaterializationConflict("SparseOutput materialization is invalid") from exc
    except RuntimeError as exc:
        raise SparseOutputMaterializationConflict("SparseOutput materialization is unavailable") from exc


def reopen_sparse_output(storage, sparse_id: str):
    """Return a held, fully revalidated read guard for committed sidecar truth only."""
    committed = metadb.reconcile_sparse_output_materialization(str(sparse_id))
    if not committed or committed.get("phase") != "committed":
        raise SparseOutputUnavailable("SparseOutput materialization is unavailable")
    guard = storage.reopen_checkpoint(committed)
    try:
        admission = metadb.sparse_output_materialization_input(str(sparse_id))
        _validate_held_schema(guard.artifact_fileno(), admission)
        exact = ExactDatasetRef(kind="exact", dataset_id=admission["inputDatasetId"],
                                revision_id=admission["inputRevisionId"])
        frozen = decode_row_identity_coverage(
            admission["documents"]["evidence"], exact,
            admission["rowIdentitySpecSha256"]).spec
        with _held_parquet_relation(guard.artifact_fileno()) as candidate_relation:
            coverage = certify_row_identity_coverage(
                storage, exact, [field.name for field in frozen.fields], candidate_relation,
                owner=f"sparse-output-reopen:{sparse_id}", frozen_spec=frozen)
        if serialize_row_identity_coverage(coverage, exact, frozen.digest) != committed["coverage"]:
            raise SparseOutputUnavailable("SparseOutput materialization is unavailable")
        guard.check()
        return guard
    except Exception:
        guard.close()
        raise


def _write_frozen_select(storage, admission: dict, output) -> None:
    exact = ExactDatasetRef(kind="exact", dataset_id=admission["inputDatasetId"],
                            revision_id=admission["inputRevisionId"])
    uri = metadb.managed_local_file_revision_artifact(exact.dataset_id, exact.revision_id)
    if uri is None:
        raise SparseOutputUnavailable("SparseOutput exact base revision is unavailable")
    expression = admission["documents"]["config"]["expr"]
    with db.base_guard(), source_read_scope(storage, [uri], owner="sparse-output-materialize"):
        validated = validate_fragment(FragmentKind.PROJECTION, expression, con=db.conn())
        reader = DuckDBAdapter().scan(uri).project(validated.sql).to_arrow_reader(65_536)
        identity, payload = _output_schema(reader.schema, tuple(
            field["name"] for field in admission["documents"]["schema"]["identity"]))
        if {"version": 1, "identity": identity, "payload": payload} != admission["documents"]["schema"]:
            raise SparseOutputMaterializationConflict("SparseOutput frozen schema is invalid")
        with pq.ParquetWriter(output, reader.schema) as parquet:
            for batch in reader:
                parquet.write_batch(batch)


def _validate_held_schema(artifact_fd: int, admission: dict) -> None:
    """Bind every frozen identity and payload field to the same held Parquet descriptor."""
    with os.fdopen(os.dup(artifact_fd), "rb") as source:
        schema = pq.ParquetFile(source).schema_arrow
    identity, payload = _output_schema(schema, tuple(
        field["name"] for field in admission["documents"]["schema"]["identity"]))
    if {"version": 1, "identity": identity, "payload": payload} != admission["documents"]["schema"]:
        raise SparseOutputMaterializationConflict("SparseOutput frozen schema is invalid")


def _revalidate_held_proof(storage, sparse_id: str, admission: dict, proof, committed: dict) -> None:
    """Reject a post-commit response loss if the still-held descriptor no longer proves DB truth."""
    _validate_held_schema(proof.artifact_fileno(), admission)
    evidence = proof.storage._result_artifact_evidence(proof.uri, proof.artifact_fileno())
    if (evidence["dev"], evidence["ino"], evidence["rows"], evidence["bytes"],
            evidence["content_sha256"], evidence["schema_sha256"]) != (
                committed["dev"], committed["ino"], committed["rows"], committed["bytes"],
                committed["content_sha256"], committed["schema_sha256"]):
        raise SparseOutputMaterializationConflict("SparseOutput materialization is unavailable")
    exact = ExactDatasetRef(kind="exact", dataset_id=admission["inputDatasetId"],
                            revision_id=admission["inputRevisionId"])
    frozen = decode_row_identity_coverage(
        admission["documents"]["evidence"], exact, admission["rowIdentitySpecSha256"]).spec
    with _held_parquet_relation(proof.artifact_fileno()) as candidate_relation:
        coverage = certify_row_identity_coverage(
            storage, exact, [field.name for field in frozen.fields], candidate_relation,
            owner=f"sparse-output-response-loss:{sparse_id}", frozen_spec=frozen)
    if serialize_row_identity_coverage(coverage, exact, frozen.digest) != committed["coverage"]:
        raise SparseOutputMaterializationConflict("SparseOutput materialization is unavailable")


def _revalidated_materialization_result(storage, sparse_id: str, authority: dict) -> SparseOutputMaterialization:
    with reopen_sparse_output(storage, sparse_id):
        pass
    return _materialization_result(sparse_id, authority)


@contextlib.contextmanager
def _held_parquet_relation(artifact_fd: int):
    """Read a candidate only through the caller-held descriptor, never its pathname."""
    source = os.fdopen(os.dup(artifact_fd), "rb")
    table_name = f"__sparse_candidate_{uuid.uuid4().hex}"
    view_name = f"{table_name}_source"
    try:
        parquet = pq.ParquetFile(source)
        reader = pa.RecordBatchReader.from_batches(
            parquet.schema_arrow, parquet.iter_batches(batch_size=65_536))
        connection = db.conn()
        relation = connection.from_arrow(reader)
        relation.create_view(view_name, replace=True)
        connection.execute(
            f"CREATE TEMP TABLE {quote_identifier(table_name)} AS SELECT * FROM {quote_identifier(view_name)}")
        yield connection.table(table_name)
    finally:
        with contextlib.suppress(Exception):
            db.conn().execute(f"DROP TABLE IF EXISTS {quote_identifier(table_name)}")
        with contextlib.suppress(Exception):
            db.conn().execute(f"DROP VIEW IF EXISTS {quote_identifier(view_name)}")
        source.close()


def _materialization_result(sparse_id: str, authority: dict) -> SparseOutputMaterialization:
    # Deliberately exclude all storage, lock, descriptor, inode, token, and lease authority.
    return SparseOutputMaterialization(
        id=sparse_id, committed=True,
        evidence={key: authority[key] for key in (
            "rows", "bytes", "contentSha256", "schemaSha256", "coverage")})


def _token(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise SparseOutputMaterializationConflict("SparseOutput materialization owner is invalid")
    return value


def _exact_ref(value: object) -> ExactDatasetRef:
    if not isinstance(value, ExactDatasetRef) or value.kind != "exact":
        raise SparseOutputValidationError("SparseOutput exact base revision is invalid")
    return value


def _identity_text(value: object, label: str, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value or len(value) > maximum:
        raise SparseOutputValidationError(f"SparseOutput {label} is invalid")
    return value


def _submission_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value or len(value) > 128:
        raise SparseOutputValidationError("SparseOutput submission is invalid")
    return value.lower()


def _select_expression(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"expr"} or type(value["expr"]) is not str:
        raise SparseOutputValidationError("SparseOutput Select config is invalid")
    expression = value["expr"].strip()
    if not expression or len(expression) > 65_536:
        raise SparseOutputValidationError("SparseOutput Select config is invalid")
    return expression


def _identity_columns(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise SparseOutputValidationError("SparseOutput identity schema is invalid")
    columns = tuple(value)
    if (any(type(column) is not str or not column or column != column.strip() or "\x00" in column
            for column in columns) or len({identifier_key(column) for column in columns}) != len(columns)):
        raise SparseOutputValidationError("SparseOutput identity schema is invalid")
    if {identifier_key(column) for column in columns} & {
            "rowid", "row_number", "row_group", "row_offset", "fragment", "offset"}:
        raise SparseOutputValidationError("SparseOutput identity schema is invalid")
    return columns


def _output_schema(schema: pa.Schema, identity_columns: tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    if len(schema.names) != len({identifier_key(name) for name in schema.names}):
        raise SparseOutputValidationError("SparseOutput Select output names are invalid")
    by_name = {identifier_key(field.name): field for field in schema}
    identity_keys = {identifier_key(column) for column in identity_columns}
    if any(identifier_key(column) not in by_name for column in identity_columns):
        raise SparseOutputValidationError("SparseOutput identity columns are absent from Select output")
    identity = [_field_document(by_name[identifier_key(column)]) for column in identity_columns]
    payload = [_field_document(field) for field in schema if identifier_key(field.name) not in identity_keys]
    if not payload:
        raise SparseOutputValidationError("SparseOutput requires one non-identity payload column")
    return identity, payload


def _field_document(field: pa.Field) -> dict:
    return {"name": field.name, "arrowType": str(field.type), "nullable": bool(field.nullable)}


def _immutable_documents(
        owner_id: str, canvas_id: str, submission_id: str, dataset_ref: ExactDatasetRef,
        expression: str, identity_schema: list[dict], payload_schema: list[dict],
        provenance: dict[str, object], evidence: dict,
        row_identity_spec_sha256: str) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, object] = {
        "input": {"kind": "exact", "datasetId": dataset_ref.dataset_id,
                  "revisionId": dataset_ref.revision_id},
        "producer": {"kind": "source_to_select", "source": "exact",
                     "select": {"kind": "builtin", "version": 1}},
        "config": {"expr": expression},
        "schema": {"version": 1, "identity": identity_schema, "payload": payload_schema},
        "provenance": provenance,
        "evidence": evidence,
    }
    documents = {name: _canonical_json(value) for name, value in values.items()}
    digests = {name: _sha256(document) for name, document in documents.items()}
    intent = {
        "version": 1, "ownerId": owner_id, "canvasId": canvas_id,
        "submissionId": submission_id,
        "rowIdentitySpecSha256": row_identity_spec_sha256,
        **{f"{name}Sha256": digest for name, digest in sorted(digests.items())},
    }
    documents["intent"] = _canonical_json(intent)
    digests["intent"] = _sha256(documents["intent"])
    return documents, digests


def _sparse_id(owner_id: str, canvas_id: str, submission_id: str) -> str:
    return hashlib.sha256(_canonical_json({
        "version": 1, "ownerId": owner_id, "canvasId": canvas_id,
        "submissionId": submission_id,
    }).encode()).hexdigest()[:32]


def _canonical_json(value: object) -> str:
    document = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(document.encode()) > 65_536:
        raise SparseOutputValidationError("SparseOutput immutable document is too large")
    return document


def _sha256(document: str) -> str:
    return hashlib.sha256(document.encode()).hexdigest()


def _lineage_provenance(value: object) -> dict:
    try:
        return LineagePublication.model_validate(value).model_dump(by_alias=True, mode="json")
    except ValueError as exc:
        raise SparseOutputValidationError("SparseOutput provenance is invalid") from exc
