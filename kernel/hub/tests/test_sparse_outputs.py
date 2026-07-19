"""Focused immutable SparseOutput admission contracts."""

from __future__ import annotations

import os
import uuid
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from hub import db, metadb
from hub.models import ExactDatasetRef
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import (
    RowIdentityFieldV1,
    RowIdentityValidationError,
    _spec_digest,
    certify_row_identity_coverage,
    decode_row_identity_coverage,
    serialize_row_identity_coverage,
)
from hub.sparse_outputs import (
    SparseOutputAdmissionRequest,
    SparseOutputSubmissionConflict,
    SparseOutputValidationError,
    admit_sparse_output,
)
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'sparse.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.fixture
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


def _publish(storage, catalog, logical_uri: str, table: pa.Table) -> dict:
    run_id = f"sparse-output-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="sparse", logical_uri=logical_uri, artifact_uri=artifact)
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return published


def _request(published: dict, *, owner: str, canvas: str, submission: str,
             projection: str = "id, payload AS score") -> SparseOutputAdmissionRequest:
    return SparseOutputAdmissionRequest(
        owner_id=owner, canvas_id=canvas, submission_id=submission,
        dataset_ref=ExactDatasetRef(
            kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"]),
        select_config={"expr": projection}, identity_columns=["id"],
        provenance={"idempotencyKey": f"sparse-{submission.lower()}", "provenance": "manual"},
    )


def _owner_canvas() -> tuple[str, str]:
    owner, canvas = f"owner-{uuid.uuid4().hex}", f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=owner, name="Sparse owner"))
        session.add(metadb.Canvas(id=canvas, owner_id=owner, name="Sparse canvas", doc="{}"))
    return owner, canvas


def _sparse_refs(sparse_id: str) -> list[metadb.LocalResultReference]:
    with metadb.session() as session:
        return list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "sparse_output",
            metadb.LocalResultReference.owner_key == sparse_id,
        )))


def test_admission_retains_only_existing_exact_base_and_replays_atomically(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "base.parquet"), pa.table({
        "id": pa.array([1, 2], type=pa.int32()),
        "payload": pa.array(["a", "b"]),
        "untouched": pa.array([10, 20], type=pa.int32()),
    }))
    owner, canvas = _owner_canvas()
    before_files = sorted(path.relative_to(storage.root) for path in Path(storage.root).rglob("*"))
    with metadb.session() as session:
        before_artifacts = session.scalar(select(func.count()).select_from(metadb.LocalResultArtifact))

    first = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="Submission-1"))
    replay = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="submission-1"))

    assert first.created is True
    assert replay.created is False and replay.id == first.id
    assert first.document["documents"]["schema"] == {
        "version": 1,
        "identity": [{"name": "id", "arrowType": "int32", "nullable": True}],
        "payload": [{"name": "score", "arrowType": "string", "nullable": True}],
    }
    assert "untouched" not in first.document["documents"]["schema"]
    assert all("uri" not in str(value).lower() for value in first.document.values())
    refs = _sparse_refs(first.id)
    assert len(refs) == 1
    assert refs[0].uri == metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert sorted(path.relative_to(storage.root) for path in Path(storage.root).rglob("*")) == before_files
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.LocalResultArtifact)) == before_artifacts

    with pytest.raises(SparseOutputSubmissionConflict):
        admit_sparse_output(storage, _request(
            published, owner=owner, canvas=canvas, submission="submission-1",
            projection="id, payload AS changed_score"))
    assert len(_sparse_refs(first.id)) == 1


@pytest.mark.parametrize(("projection", "status"), [
    ("id + 10 AS id, payload AS score", "partial"),
    ("1 AS id, payload AS score", "invalid"),
])
def test_partial_and_invalid_coverage_are_retained_as_truth(local_catalog, tmp_path, projection, status):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / f"{status}.parquet"), pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "payload": pa.array(["a", "b"]),
    }))
    owner, canvas = _owner_canvas()
    admitted = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission=f"submission-{status}", projection=projection))
    assert admitted.document["documents"]["evidence"]["status"] == status
    assert len(_sparse_refs(admitted.id)) == 1


def test_concurrent_same_submission_converges_on_one_row_and_one_reference(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "concurrent.parquet"), pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "payload": pa.array(["a", "b"]),
    }))
    owner, canvas = _owner_canvas()

    def admit():
        return admit_sparse_output(storage, _request(
            published, owner=owner, canvas=canvas, submission="same-submission"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _ignored: admit(), range(4)))
    assert {result.id for result in results} == {results[0].id}
    assert sum(result.created for result in results) == 1
    assert len(_sparse_refs(results[0].id)) == 1


def test_admission_rolls_back_on_retention_failure_and_never_repairs_half_state(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "rollback.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "payload": pa.array(["a"]),
    }))
    owner, canvas = _owner_canvas()
    request = _request(published, owner=owner, canvas=canvas, submission="rollback")
    monkeypatch.setattr(metadb, "sync_local_result_owner", lambda *args: (_ for _ in ()).throw(
        RuntimeError("retention unavailable")))
    with pytest.raises(RuntimeError, match="retention unavailable"):
        admit_sparse_output(storage, request)
    with metadb.session() as session:
        assert session.scalar(select(metadb.SparseOutput)) is None
    assert not _sparse_refs("not-an-admission")


def test_readback_fails_closed_for_missing_retention_and_corrupt_document(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "half-state.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "payload": pa.array(["a"]),
    }))
    owner, canvas = _owner_canvas()
    admitted = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="half-state"))
    with metadb.session() as session:
        ref = session.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "sparse_output",
            metadb.LocalResultReference.owner_key == admitted.id))
        assert ref is not None
        session.delete(ref)
    with pytest.raises(RuntimeError, match="retention is incomplete"):
        metadb.sparse_output_get(owner, admitted.id)
    assert metadb.sparse_output_get("different-owner", admitted.id) is None

    # This separately proves the read path rejects immutable-document corruption before revealing it.
    second = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="corrupt"))
    with metadb.session() as session:
        row = session.get(metadb.SparseOutput, second.id)
        assert row is not None
        row.config_doc = "{}"
    with pytest.raises(RuntimeError, match="immutable admission is corrupt"):
        metadb.sparse_output_get(owner, second.id)

    third = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="schema-corrupt"))
    with metadb.session() as session:
        row = session.get(metadb.SparseOutput, third.id)
        assert row is not None
        schema = json.loads(row.schema_doc)
        schema["identity"][0]["nullable"] = 1  # coherent digest changes still cannot coerce bool.
        row.schema_doc = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.schema_sha256 = hashlib.sha256(row.schema_doc.encode()).hexdigest()
        intent = json.loads(row.intent_doc)
        intent["schemaSha256"] = row.schema_sha256
        row.intent_doc = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.intent_sha256 = hashlib.sha256(row.intent_doc.encode()).hexdigest()
    with pytest.raises(RuntimeError, match="immutable admission is corrupt"):
        metadb.sparse_output_get(owner, third.id)

    fourth = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="evidence-spec-corrupt"))
    with metadb.session() as session:
        row = session.get(metadb.SparseOutput, fourth.id)
        assert row is not None
        evidence = json.loads(row.evidence_doc)
        evidence["spec"]["schemaDigest"] = "f" * 64
        evidence["spec"]["digest"] = _spec_digest(
            evidence["spec"]["datasetId"], evidence["spec"]["revisionId"],
            tuple(RowIdentityFieldV1(field["name"], field["arrowType"])
                  for field in evidence["spec"]["fields"]), evidence["spec"]["schemaDigest"])
        row.evidence_doc = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.evidence_sha256 = hashlib.sha256(row.evidence_doc.encode()).hexdigest()
        intent = json.loads(row.intent_doc)
        intent["evidenceSha256"] = row.evidence_sha256
        row.intent_doc = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.intent_sha256 = hashlib.sha256(row.intent_doc.encode()).hexdigest()
    with pytest.raises(RuntimeError):
        metadb.sparse_output_get(owner, fourth.id)

    fifth = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="producer-version-corrupt"))
    with metadb.session() as session:
        row = session.get(metadb.SparseOutput, fifth.id)
        assert row is not None
        producer = json.loads(row.producer_doc)
        producer["select"]["version"] = True
        row.producer_doc = json.dumps(producer, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.producer_sha256 = hashlib.sha256(row.producer_doc.encode()).hexdigest()
        intent = json.loads(row.intent_doc)
        intent["producerSha256"] = row.producer_sha256
        row.intent_doc = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row.intent_sha256 = hashlib.sha256(row.intent_doc.encode()).hexdigest()
    with pytest.raises(RuntimeError, match="immutable admission is corrupt"):
        metadb.sparse_output_get(owner, fifth.id)


def test_non_exact_and_physical_identity_shapes_are_rejected_before_admission(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "reject.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "payload": pa.array(["a"]),
    }))
    owner, canvas = _owner_canvas()
    with pytest.raises(SparseOutputValidationError):
        admit_sparse_output(storage, _request(
            published, owner=owner, canvas=canvas, submission="physical",
            projection="rowid AS id, payload AS score"))


def test_payload_offset_and_fragment_words_are_not_projection_wide_bans(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "words.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "offset": pa.array([3], type=pa.int32()),
        "fragment": pa.array(["piece"]),
    }))
    owner, canvas = _owner_canvas()
    admitted = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="words",
        projection="id, \"offset\" AS score, fragment || ' fragment offset' AS description"))
    assert admitted.document["documents"]["evidence"]["status"] == "complete"


def test_certificate_serialization_uses_frozen_exact_authority_and_rejects_extras(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "cert.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "payload": pa.array(["a"]),
    }))
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])
    certificate = certify_row_identity_coverage(
        storage, exact, ["id"], db.conn().from_arrow(pa.table({"id": pa.array([1], type=pa.int32())})))
    document = serialize_row_identity_coverage(certificate, exact, certificate.spec.digest)
    assert decode_row_identity_coverage(document, exact, certificate.spec.digest) == certificate
    altered_spec = replace(
        certificate.spec, revision_id="other-revision",
        digest=_spec_digest(certificate.spec.dataset_id, "other-revision", certificate.spec.fields,
                            certificate.spec.schema_digest))
    with pytest.raises(RowIdentityValidationError):
        serialize_row_identity_coverage(replace(certificate, spec=altered_spec), exact, certificate.spec.digest)
    document["extra"] = True
    with pytest.raises(RowIdentityValidationError):
        decode_row_identity_coverage(document, exact, certificate.spec.digest)


@pytest.mark.parametrize("candidate", [
    pa.table({"id": pa.array([1, 2], type=pa.int32())}),
    pa.table({"id": pa.array([1], type=pa.int32())}),
    pa.table({"id": pa.array([1, 2, 3], type=pa.int32())}),
    pa.table({"id": pa.array([1, 1], type=pa.int32())}),
    pa.table({"id": pa.array([1, None], type=pa.int32())}),
])
def test_certificate_round_trip_preserves_real_coverage_matrix(local_catalog, tmp_path, candidate):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "matrix.parquet"), pa.table({
        "id": pa.array([1, 2], type=pa.int32()),
    }))
    exact = ExactDatasetRef(kind="exact", dataset_id=published["dataset_id"],
                            revision_id=published["revision_id"])
    certificate = certify_row_identity_coverage(storage, exact, ["id"], db.conn().from_arrow(candidate))
    assert decode_row_identity_coverage(
        serialize_row_identity_coverage(certificate, exact, certificate.spec.digest),
        exact, certificate.spec.digest) == certificate


def test_direct_metadb_malformed_canonical_admission_rolls_back(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "direct.parquet"), pa.table({
        "id": pa.array([1], type=pa.int32()), "payload": pa.array(["a"]),
    }))
    owner, canvas = _owner_canvas()
    admitted = admit_sparse_output(storage, _request(
        published, owner=owner, canvas=canvas, submission="template"))
    documents = {name: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                 for name, value in admitted.document["documents"].items()}
    documents["config"] = json.dumps({"wrong": "shape"}, separators=(",", ":"))
    digests = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in documents.items()}
    intent = json.loads(documents["intent"])
    intent["submissionId"] = "bad"
    intent["configSha256"] = digests["config"]
    documents["intent"] = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digests["intent"] = hashlib.sha256(documents["intent"].encode()).hexdigest()
    sparse_id = f"bad{uuid.uuid4().hex[:29]}"
    with pytest.raises(RuntimeError, match="immutable admission is corrupt"):
        metadb.sparse_output_admit(
            owner_id=owner, canvas_id=canvas, submission_id="bad", sparse_id=sparse_id,
            input_dataset_id=published["dataset_id"], input_revision_id=published["revision_id"],
            documents=documents, digests=digests,
            row_identity_spec_sha256=admitted.document["rowIdentitySpecSha256"])
    with metadb.session() as session:
        assert session.get(metadb.SparseOutput, sparse_id) is None
    assert not _sparse_refs(sparse_id)
