"""Acceptance contracts for the synchronous complete SparseOutput merge."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from hub import db, linear_checkpoint as lc, merge_columns as merge_columns_module, metadb
from hub import merge_columns_tasks
from hub.local_writes import write_managed_local_file
from hub.merge_columns import (
    MergeColumnRuleV1,
    MergeColumnsError,
    MergeColumnsIntentV1,
    merge_sparse_output_candidate,
    merge_sparse_output_columns,
    sparse_output_merge_evidence,
)
from hub.models import (
    ColumnSchema,
    DatasetRevision,
    ExactDatasetRef,
    LineagePublication,
    WriteDestination,
    WriteIntent,
    WriteProvenance,
)
from hub.plugins.adapters import DuckDBAdapter, relation_columns
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import (
    certify_row_identity_coverage,
    decode_row_identity_coverage,
    serialize_row_identity_coverage,
)
from hub.sparse_outputs import (
    SparseOutputAdmissionRequest,
    admit_sparse_output,
    materialize_sparse_output,
    release_sparse_output,
)
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = (
        os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'merge.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


@pytest.fixture
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


@dataclass
class MergeCase:
    storage: LocalStorage
    catalog: InMemoryCatalog
    logical_uri: str
    name: str
    exact: ExactDatasetRef
    sparse_id: str
    admission: dict
    identity_columns: list[str]
    base_schema: pa.Schema


def _publish(storage, catalog, logical_uri: str, name: str, table: pa.Table) -> dict:
    run_id = f"merge-fixture:{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"merge-fixture:{logical_uri}", run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    published = catalog.publish_managed_local_file_output(
        name=name, logical_uri=logical_uri, artifact_uri=artifact)
    assert storage.release_result(artifact, run_id) is True
    return published


def _case(local_catalog, tmp_path, table: pa.Table, *, projection: str,
          identity_columns: list[str], name: str = "base") -> MergeCase:
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / f"{name}-{uuid.uuid4().hex}.parquet")
    published = _publish(storage, catalog, logical_uri, name, table)
    owner, canvas = f"u-{uuid.uuid4().hex}", f"c-{uuid.uuid4().hex}"
    with metadb.session() as s:
        s.add(metadb.User(id=owner, name=owner))
        s.add(metadb.Canvas(id=canvas, owner_id=owner, name=canvas, doc="{}"))
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])
    admitted = admit_sparse_output(storage, SparseOutputAdmissionRequest(
        owner_id=owner, canvas_id=canvas, submission_id="submission", dataset_ref=exact,
        select_config={"expr": projection}, identity_columns=identity_columns,
        provenance={"idempotencyKey": f"sparse:{uuid.uuid4().hex}", "provenance": "manual"},
    ))
    materialize_sparse_output(storage, admitted.id, uuid.uuid4().hex)
    return MergeCase(storage, catalog, logical_uri, name, exact, admitted.id,
                     admitted.document, identity_columns, table.schema)


def _logical_schema(schema: pa.Schema) -> list[ColumnSchema]:
    with db.base_guard():
        columns = relation_columns(db.conn().from_arrow(pa.Table.from_batches([], schema=schema)))
    return [ColumnSchema(name=column.name, type=column.type) for column in columns]


def _intent(case: MergeCase, rules: list[MergeColumnRuleV1], *, key: str = "merge-key",
            output_schema: list[ColumnSchema] | None = None) -> MergeColumnsIntentV1:
    committed = metadb.reconcile_sparse_output_materialization(case.sparse_id)
    assert committed is not None and committed["phase"] == "committed"
    if output_schema is None:
        fields = list(case.base_schema)
        with os.fdopen(os.open(committed["uri"], os.O_RDONLY), "rb") as source:
            side_schema = pq.ParquetFile(source).schema_arrow
        for rule in rules:
            if rule.mode == "add":
                source_field = next(field for field in side_schema if field.name == rule.source)
                fields.append(pa.field(rule.target, source_field.type))
        output_schema = _logical_schema(pa.schema(fields))
    write = WriteIntent(
        destination=WriteDestination(
            logical_uri=case.logical_uri, name=case.name, dataset_id=case.exact.dataset_id),
        mode="replace", expected_schema=output_schema, expected_head=case.exact,
        idempotency_key=key,
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )
    return MergeColumnsIntentV1(
        base=case.exact, sparse_output_id=case.sparse_id,
        sparse_evidence=sparse_output_merge_evidence(case.admission, committed),
        rules=rules, output_schema=output_schema, write_intent=write,
    )


def _rewrite_sidecar(case: MergeCase, table: pa.Table) -> None:
    """Install a test-only internally consistent candidate to exercise merge revalidation."""
    committed = metadb.reconcile_sparse_output_materialization(case.sparse_id)
    assert committed is not None and committed["phase"] == "committed"
    pq.write_table(table, committed["uri"])
    fd = os.open(committed["uri"], os.O_RDONLY)
    try:
        physical = case.storage._result_artifact_evidence(committed["uri"], fd)
    finally:
        os.close(fd)
    frozen = decode_row_identity_coverage(
        case.admission["documents"]["evidence"], case.exact,
        case.admission["rowIdentitySpecSha256"]).spec
    with db.base_guard():
        coverage = certify_row_identity_coverage(
            case.storage, case.exact, case.identity_columns, db.conn().from_arrow(table),
            owner="merge-test-rewrite", frozen_spec=frozen)
    coverage_doc = json.dumps(
        serialize_row_identity_coverage(coverage, case.exact, frozen.digest),
        sort_keys=True, separators=(",", ":"))
    with metadb.session() as s:
        row = s.get(metadb.SparseOutputMaterialization, case.sparse_id)
        row.candidate_dev, row.candidate_ino = physical["dev"], physical["ino"]
        row.committed_rows, row.committed_bytes = physical["rows"], physical["bytes"]
        row.content_sha256, row.schema_sha256 = (
            physical["content_sha256"], physical["schema_sha256"])
        row.coverage_doc = coverage_doc
        row.coverage_sha256 = hashlib.sha256(coverage_doc.encode()).hexdigest()


def _revision_count(dataset_id: str) -> int:
    with metadb.session() as s:
        return int(s.scalar(select(func.count()).select_from(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.logical_id == dataset_id)) or 0)


def _merge_publication_count() -> int:
    with metadb.session() as s:
        return int(s.scalar(select(func.count()).select_from(metadb.MergeColumnsPublication)) or 0)


def _read_revision(ref: ExactDatasetRef | DatasetRevision) -> pa.Table:
    uri = metadb.managed_local_file_revision_artifact(ref.dataset_id, ref.revision_id)
    assert uri is not None
    return pq.read_table(uri)


def _ordinary_replace(case: MergeCase, intent: WriteIntent, table: pa.Table):
    return write_managed_local_file(
        storage=case.storage, catalog=case.catalog, intent=intent,
        write_artifact=lambda uri: pq.write_table(table, uri))


def _retarget_write(intent: WriteIntent, *, key: str,
                    head: ExactDatasetRef | DatasetRevision) -> WriteIntent:
    return WriteIntent(
        destination=intent.destination, mode="replace", expected_schema=intent.expected_schema,
        expected_head=ExactDatasetRef(
            kind="exact", dataset_id=head.dataset_id, revision_id=head.revision_id),
        idempotency_key=key,
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )


def test_int64_replace_composite_string_keys_reordered_and_replay(local_catalog, tmp_path):
    base = pa.table({
        "label": ["left", "right"], "id": pa.array([10, 20], type=pa.int64()),
        "value": pa.array([100, 200], type=pa.int64()), "untouched": ["a", "b"],
    })
    case = _case(local_catalog, tmp_path, base,
                 projection="label, id, value AS replacement",
                 identity_columns=["label", "id"])
    _rewrite_sidecar(case, pa.table({
        "label": ["right", "left"], "id": pa.array([20, 10], type=pa.int64()),
        "replacement": pa.array([222, 111], type=pa.int64()),
    }))
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    first = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert _read_revision(first.head).to_pydict() == {
        "label": ["left", "right"], "id": [10, 20], "value": [111, 222],
        "untouched": ["a", "b"],
    }
    assert _read_revision(case.exact).to_pydict() == base.to_pydict()
    assert first.parent_head == case.exact and first.rows == 2
    assert release_sparse_output(case.sparse_id) is True
    replay = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert replay.revision_id == first.revision_id and _revision_count(case.exact.dataset_id) == 2


def test_numeric_add_preserves_base_order_and_values(local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()),
                     "raw": pa.array([1.5, 2.5], type=pa.float64()), "keep": ["x", "y"]})
    case = _case(local_catalog, tmp_path, base, projection="id, raw AS score",
                 identity_columns=["id"])
    _rewrite_sidecar(case, pa.table({"id": pa.array([2, 1], type=pa.int32()),
                                     "score": pa.array([25.0, 15.0], type=pa.float64())}))
    intent = _intent(case, [MergeColumnRuleV1(source="score", target="derived", mode="add")])
    receipt = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert _read_revision(receipt.head).to_pydict() == {
        "id": [1, 2], "raw": [1.5, 2.5], "keep": ["x", "y"], "derived": [15.0, 25.0],
    }


@pytest.mark.parametrize("keys", [[1, 3], [1, 1], [1, None], [1], [1, 2, 3]],
                         ids=["different", "duplicate", "null", "missing", "extra"])
def test_non_complete_identity_cases_publish_nothing(local_catalog, tmp_path, keys):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    sidecar = pa.table({
        "id": pa.array(keys, type=pa.int32()),
        "replacement": pa.array([f"v-{index}" for index in range(len(keys))], type=pa.string()),
    })
    _rewrite_sidecar(case, sidecar)
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    before = _revision_count(case.exact.dataset_id)
    with pytest.raises(MergeColumnsError, match="complete logical identity"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert _revision_count(case.exact.dataset_id) == before
    assert _merge_publication_count() == 0


def test_generic_write_cannot_claim_merge_semantics(local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    ordinary = _ordinary_replace(case, intent.write_intent, base)
    assert ordinary.revision_id != case.exact.revision_id and _merge_publication_count() == 0
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="non-merge"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert _merge_publication_count() == 0


def test_response_loss_recovers_only_atomic_merge_receipt(local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    original = case.catalog.publish_managed_local_write

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated response loss")

    monkeypatch.setattr(case.catalog, "publish_managed_local_write", lose_response)
    receipt = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert receipt.parent_head == case.exact
    assert _revision_count(case.exact.dataset_id) == 2 and _merge_publication_count() == 1
    assert merge_sparse_output_columns(
        storage=case.storage, catalog=case.catalog, intent=intent).revision_id == receipt.revision_id


def test_concurrent_semantic_collision_has_one_cas_winner(local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()),
                     "value": ["a", "b"], "other": ["x", "y"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intents = [
        _intent(case, [MergeColumnRuleV1(
            source="replacement", target=target, mode="replace")], key="contended")
        for target in ("value", "other")
    ]

    def execute(candidate):
        try:
            return merge_sparse_output_columns(
                storage=case.storage, catalog=case.catalog, intent=candidate)
        except Exception as exc:  # noqa: BLE001 - the assertion below checks the exact loser class
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, intents))
    receipts = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    conflicts = [outcome for outcome in outcomes
                 if isinstance(outcome, metadb.ManagedLocalWriteConflict)]
    assert len(receipts) == len(conflicts) == 1, outcomes
    assert _revision_count(case.exact.dataset_id) == 2 and _merge_publication_count() == 1


def test_release_after_held_guard_does_not_cancel_started_merge(
        local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    original = merge_columns_module.reopen_sparse_output_context

    def reopen_then_release(storage, sparse_id):
        guard = original(storage, sparse_id)
        assert release_sparse_output(sparse_id) is True
        return guard

    monkeypatch.setattr(
        merge_columns_module, "reopen_sparse_output_context", reopen_then_release)
    receipt = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert receipt.parent_head == case.exact and _merge_publication_count() == 1


def test_release_and_rebuild_during_reopen_cannot_mix_guard_and_frozen_truth(
        local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    original_reopen = case.storage.reopen_checkpoint
    raced = False

    def replace_before_guard(_old_authority):
        nonlocal raced
        assert raced is False
        raced = True
        assert release_sparse_output(case.sparse_id) is True
        rebuilt = admit_sparse_output(case.storage, SparseOutputAdmissionRequest(
            owner_id=case.admission["ownerId"], canvas_id=case.admission["canvasId"],
            submission_id=case.admission["submissionId"], dataset_ref=case.exact,
            select_config={"expr": "id, value AS changed"}, identity_columns=["id"],
            provenance={"idempotencyKey": "raced-rebuild", "provenance": "manual"},
        ))
        assert rebuilt.id == case.sparse_id
        materialize_sparse_output(case.storage, rebuilt.id, uuid.uuid4().hex)
        current = metadb.reconcile_sparse_output_materialization(case.sparse_id)
        assert current is not None and current["phase"] == "committed"
        return original_reopen(current)

    monkeypatch.setattr(case.storage, "reopen_checkpoint", replace_before_guard)
    with pytest.raises(RuntimeError, match="generation changed"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert raced is True
    assert _revision_count(case.exact.dataset_id) == 1 and _merge_publication_count() == 0


def test_rebuilt_deterministic_sparse_identity_cannot_change_merge_meaning(
        local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert release_sparse_output(case.sparse_id) is True
    rebuilt = admit_sparse_output(case.storage, SparseOutputAdmissionRequest(
        owner_id=case.admission["ownerId"], canvas_id=case.admission["canvasId"],
        submission_id=case.admission["submissionId"], dataset_ref=case.exact,
        select_config={"expr": "id, value AS changed"}, identity_columns=["id"],
        provenance={"idempotencyKey": "rebuilt-sparse", "provenance": "manual"},
    ))
    assert rebuilt.id == case.sparse_id
    materialize_sparse_output(case.storage, rebuilt.id, uuid.uuid4().hex)
    case.admission = rebuilt.document
    changed = _intent(case, [MergeColumnRuleV1(
        source="changed", target="value", mode="replace")])
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="immutable meaning"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=changed)


def test_rule_schema_and_stale_head_conflicts_publish_no_merge(local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    valid = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    bad_rule = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="add")], key="bad-rule")
    with pytest.raises(MergeColumnsError, match="already exists"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=bad_rule)
    wrong_schema = [ColumnSchema(name="id", type="int"), ColumnSchema(name="value", type="int")]
    bad_schema = _intent(case, valid.rules, key="bad-schema", output_schema=wrong_schema)
    with pytest.raises(MergeColumnsError, match="output schema"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=bad_schema)
    advanced_intent = _retarget_write(valid.write_intent, key="advance", head=case.exact)
    _ordinary_replace(case, advanced_intent, base)
    before = _revision_count(case.exact.dataset_id)
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="stale"):
        merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=valid)
    assert _revision_count(case.exact.dataset_id) == before and _merge_publication_count() == 0


def test_merge_revision_is_gc_retained_for_replay(local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    merged = merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    next_intent = _retarget_write(intent.write_intent, key="next-head", head=merged.head)
    _ordinary_replace(case, next_intent, base)
    assert release_sparse_output(case.sparse_id) is True
    metadb.managed_local_file_revision_gc_batch(0, limit=50)
    assert metadb.managed_local_file_revision_artifact(
        merged.dataset_id, merged.revision_id) is not None
    assert merge_sparse_output_columns(
        storage=case.storage, catalog=case.catalog, intent=intent).revision_id == merged.revision_id


def test_private_merge_row_contains_no_receipt_or_uri_authority(local_catalog, tmp_path):
    from sqlalchemy import inspect

    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    merge_sparse_output_columns(storage=case.storage, catalog=case.catalog, intent=intent)
    assert {column["name"] for column in inspect(metadb.engine()).get_columns(
        "merge_columns_publications")} == {
            "idempotency_key", "merge_doc", "merge_sha256", "revision_id", "created_at",
        }
    with metadb.session() as s:
        row = s.get(metadb.MergeColumnsPublication, intent.write_intent.idempotency_key)
        assert row is not None
        merge_doc = row.merge_doc
        document = json.loads(merge_doc)
    assert case.logical_uri not in merge_doc
    assert case.storage.result_root not in merge_doc

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key).casefold()
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    forbidden = ("uri", "artifact", "lock", "descriptor", "inode", "device", "token", "lease")
    assert not any(term in key for key in keys(document) for term in forbidden)


def _wait_task(task_id: str) -> dict:
    import time

    deadline = time.time() + 15
    while time.time() < deadline:
        task = metadb.durable_task(task_id, include_admission=False)
        assert task is not None
        if task["status"] in ("done", "failed", "cancelled"):
            return task
        time.sleep(0.02)
    raise AssertionError("durable merge task did not reach a terminal state")


def _admit_durable_merge(case: MergeCase, intent: MergeColumnsIntentV1, submission: str) -> dict:
    task, created = metadb.submit_merge_columns_task(
        uid=case.admission["ownerId"], canvas_id=case.admission["canvasId"],
        submission_id=submission, intent=intent)
    assert created
    return task


def _commit_durable_merge_candidate(task: dict, case: MergeCase, intent: MergeColumnsIntentV1):
    task_id = task["id"]
    owner = "candidate-owner"
    claimed = metadb.claim_merge_columns_task(task_id, owner)
    assert claimed is not None
    attempt_id = claimed["attempts"][-1]["id"]
    table = merge_sparse_output_candidate(storage=case.storage, intent=intent)
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=task_id, attempt_id=attempt_id, owner_token=owner,
        namespace_id=case.storage.namespace_id, storage_root=case.storage.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    lc.materialize_and_commit_checkpoint(
        case.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner,
        candidate=candidate, content=sink.getvalue().to_pybytes())
    return attempt_id, owner


def _expire_attempt(attempt_id: str) -> None:
    with metadb.session() as s:
        attempt = s.get(metadb.DurableTaskAttempt, attempt_id, with_for_update=True)
        assert attempt is not None
        attempt.lease_until = metadb._durable_task_db_now(s) - timedelta(seconds=1)


def _publish_checkpoint_as_owner(
        case: MergeCase, intent: MergeColumnsIntentV1, task_id: str,
        attempt_id: str, owner_token: str, artifact_uris: dict[str, str] | None = None):
    committed = metadb.linear_checkpoint_committed(task_id)
    assert committed is not None

    def copy_candidate(uri: str) -> None:
        if artifact_uris is not None:
            artifact_uris[owner_token] = uri
        with open(committed["uri"], "rb") as source, open(uri, "wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)

    return write_managed_local_file(
        storage=case.storage, catalog=case.catalog, intent=intent.write_intent,
        write_artifact=copy_candidate,
        merge_publication=merge_columns_module.merge_columns_publication_context(
            intent, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token))


def test_durable_merge_reuses_only_committed_candidate_and_redacts_jobs(local_catalog, tmp_path,
                                                                         monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")])
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    attempt_id, owner = _commit_durable_merge_candidate(task, case, intent)
    with metadb.session() as s:
        envelope = s.get(metadb.MergeColumnsTaskEnvelope, task["id"])
        checkpoint = s.get(metadb.DurableCheckpoint, task["id"])
        assert envelope is not None and envelope.phase == "candidate_committed"
        assert checkpoint is not None and checkpoint.phase == "committed"
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt_id, owner,
        {"run_id": task["id"], "status": "cancelled", "target_node_id": "merge-columns"})
    retried = metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    assert retried["status"] == "queued"

    def no_second_merge(*_args, **_kwargs):
        raise AssertionError("candidate-committed retry reopened SparseOutput")

    monkeypatch.setattr(merge_columns_tasks, "merge_sparse_output_candidate", no_second_merge)
    original_publish = case.catalog.publish_managed_local_write

    def lose_publication_response(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise RuntimeError("publication response lost")

    monkeypatch.setattr(case.catalog, "publish_managed_local_write", lose_publication_response)
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    done = _wait_task(task["id"])
    assert done["status"] == "done"
    assert done["output_receipt"] is not None
    assert _revision_count(case.exact.dataset_id) == 2
    inbox = metadb.list_durable_task_inbox_items(case.admission["ownerId"])
    outcomes = [item["outcome"] for item in inbox["items"] if item["task_id"] == task["id"]]
    assert sorted(outcomes) == ["cancelled", "completed"]
    jobs = metadb.list_workspace_runs(case.admission["ownerId"])
    job = next(item for item in jobs["items"] if item["taskId"] == task["id"])
    assert job["mergeColumns"]["phase"] == "done"
    assert job["mergeColumns"]["candidate"] == "committed"
    assert job["writeIntent"] is None and job["inputManifest"] == []
    assert case.logical_uri not in json.dumps(job["mergeColumns"])


def test_durable_merge_corrupt_committed_candidate_fails_closed(local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="corrupt-candidate")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    attempt_id, owner = _commit_durable_merge_candidate(task, case, intent)
    checkpoint = metadb.linear_checkpoint_committed(task["id"])
    assert checkpoint is not None
    with open(checkpoint["uri"], "wb") as corrupted:
        corrupted.write(b"not a parquet candidate")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt_id, owner,
        {"run_id": task["id"], "status": "cancelled", "target_node_id": "merge-columns"})
    metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    monkeypatch.setattr(merge_columns_tasks, "merge_sparse_output_candidate",
                        lambda *_args, **_kwargs: pytest.fail("corrupt candidate recomputed merge"))
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    failed = _wait_task(task["id"])
    assert failed["status"] == "failed"
    assert _revision_count(case.exact.dataset_id) == 1


def test_durable_merge_stale_publication_owner_cannot_cross_replacement_claim(
        local_catalog, tmp_path):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="publication-fence")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    stale_attempt, stale_owner = _commit_durable_merge_candidate(task, case, intent)
    assert metadb.update_merge_columns_task_phase(
        task["id"], stale_attempt, stale_owner, phase="publishing",
        status_doc=merge_columns_tasks._status(task["id"], phase="publishing", progress=0.8))
    _expire_attempt(stale_attempt)
    replacement_owner = "replacement-publication-owner"
    replacement = metadb.claim_merge_columns_task(task["id"], replacement_owner)
    assert replacement is not None
    replacement_attempt = replacement["attempts"][-1]["id"]

    with pytest.raises(metadb.ManagedLocalWriteConflict, match="owner is stale or fenced"):
        _publish_checkpoint_as_owner(
            case, intent, task["id"], stale_attempt, stale_owner)
    assert _revision_count(case.exact.dataset_id) == 1
    assert metadb.update_merge_columns_task_phase(
        task["id"], replacement_attempt, replacement_owner, phase="publishing",
        status_doc=merge_columns_tasks._status(task["id"], phase="publishing", progress=0.8))
    receipt = _publish_checkpoint_as_owner(
        case, intent, task["id"], replacement_attempt, replacement_owner)
    assert metadb.finish_durable_task_attempt(
        task["id"], replacement_attempt, replacement_owner,
        merge_columns_tasks._done(task["id"], intent.write_intent, receipt).model_dump())
    assert _revision_count(case.exact.dataset_id) == 2
    assert _merge_publication_count() == 1


def test_durable_merge_cancel_after_reservation_retires_exact_authority(
        local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="cancel-reserved")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    reserved: dict = {}

    def cancel_reserved(_storage, *, candidate, **_kwargs):
        reserved.update(candidate)
        metadb.request_durable_task_cancel(task["id"])
        raise RuntimeError("cancel after reservation")

    monkeypatch.setattr(
        merge_columns_tasks.lc, "materialize_and_commit_checkpoint", cancel_reserved)
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    assert _wait_task(task["id"])["status"] == "cancelled"
    assert reserved["uri"]
    assert metadb.linear_checkpoint_candidate(task["id"]) is None
    with metadb.session() as s:
        checkpoint = s.get(metadb.DurableCheckpoint, task["id"])
        artifact = s.get(metadb.LocalResultArtifact, reserved["uri"])
        assert checkpoint is not None and checkpoint.phase == "pending"
        assert (checkpoint.candidate_uri, checkpoint.candidate_generation,
                checkpoint.candidate_attempt_id, checkpoint.candidate_dev,
                checkpoint.candidate_ino) == (None, None, None, None, None)
        assert artifact is None or (
            artifact.state == "deleting" and artifact.writer_run_id is None
            and artifact.writer_token is None)


def test_durable_merge_reconciles_candidate_commit_response_loss(
        local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="commit-response-loss")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    original = metadb.commit_linear_checkpoint
    calls = 0

    def lose_commit_response(**kwargs):
        nonlocal calls
        calls += 1
        original(**kwargs)
        raise RuntimeError("candidate commit response lost")

    monkeypatch.setattr(metadb, "commit_linear_checkpoint", lose_commit_response)
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    assert _wait_task(task["id"])["status"] == "done"
    assert calls == 1
    assert metadb.linear_checkpoint_committed(task["id"]) is not None
    assert _revision_count(case.exact.dataset_id) == 2
    assert _merge_publication_count() == 1


@pytest.mark.parametrize(
    "restart_phase,expected_merges",
    [("validating", 1), ("merging", 1), ("reserved", 1),
     ("candidate_committed", 0), ("publishing", 0)],
)
def test_durable_merge_restart_from_each_authoritative_phase(
        local_catalog, tmp_path, monkeypatch, restart_phase, expected_merges):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")],
        key=f"restart-{restart_phase}")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    owner = f"expired-{restart_phase}"
    claimed = metadb.claim_merge_columns_task(task["id"], owner)
    assert claimed is not None
    attempt_id = claimed["attempts"][-1]["id"]
    if restart_phase in ("validating", "merging", "reserved"):
        visible_phase = "merging" if restart_phase == "reserved" else restart_phase
        assert metadb.update_merge_columns_task_phase(
            task["id"], attempt_id, owner, phase=visible_phase,
            status_doc=merge_columns_tasks._status(
                task["id"], phase=visible_phase, progress=0.2))
        if restart_phase == "reserved":
            metadb.reserve_linear_checkpoint_candidate(
                task_id=task["id"], attempt_id=attempt_id, owner_token=owner,
                namespace_id=case.storage.namespace_id,
                storage_root=case.storage.result_root,
                writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    else:
        table = merge_sparse_output_candidate(storage=case.storage, intent=intent)
        candidate = metadb.reserve_linear_checkpoint_candidate(
            task_id=task["id"], attempt_id=attempt_id, owner_token=owner,
            namespace_id=case.storage.namespace_id,
            storage_root=case.storage.result_root,
            writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink)
        lc.materialize_and_commit_checkpoint(
            case.storage, task_id=task["id"], attempt_id=attempt_id,
            owner_token=owner, candidate=candidate, content=sink.getvalue().to_pybytes())
        if restart_phase == "publishing":
            assert metadb.update_merge_columns_task_phase(
                task["id"], attempt_id, owner, phase="publishing",
                status_doc=merge_columns_tasks._status(
                    task["id"], phase="publishing", progress=0.8))
    _expire_attempt(attempt_id)
    original_merge = merge_columns_tasks.merge_sparse_output_candidate
    merge_calls = 0

    def count_merge(*args, **kwargs):
        nonlocal merge_calls
        merge_calls += 1
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(merge_columns_tasks, "merge_sparse_output_candidate", count_merge)
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    assert _wait_task(task["id"])["status"] == "done"
    assert merge_calls == expected_merges
    assert metadb.linear_checkpoint_committed(task["id"]) is not None
    assert _revision_count(case.exact.dataset_id) == 2


def test_durable_merge_cancel_and_stale_head_are_deterministic(local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="stale-durable")
    cancelled = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    metadb.request_durable_task_cancel(cancelled["id"])
    merge_columns_tasks.dispatch(cancelled["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    assert _wait_task(cancelled["id"])["status"] == "cancelled"
    assert metadb.linear_checkpoint_candidate(cancelled["id"]) is None
    assert _revision_count(case.exact.dataset_id) == 1

    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    attempt_id, owner = _commit_durable_merge_candidate(task, case, intent)
    _ordinary_replace(case, _retarget_write(intent.write_intent, key="advance-durable",
                                             head=case.exact), base)
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt_id, owner,
        {"run_id": task["id"], "status": "cancelled", "target_node_id": "merge-columns"})
    metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    monkeypatch.setattr(merge_columns_tasks, "merge_sparse_output_candidate",
                        lambda *_args, **_kwargs: pytest.fail("stale head recomputed merge"))
    merge_columns_tasks.dispatch(task["id"], SimpleNamespace(
        storage=case.storage, catalog=case.catalog))
    failed = _wait_task(task["id"])
    assert failed["status"] == "failed" and failed["error"] == "stale_expected_head"
    assert _revision_count(case.exact.dataset_id) == 2
    jobs = metadb.list_workspace_runs(case.admission["ownerId"])
    job = next(item for item in jobs["items"] if item["taskId"] == task["id"])
    assert job["error"] == "stale_expected_head"
    assert job["canRetry"] is False
    assert job["mergeColumns"]["canRetry"] is False
    assert job["mergeColumns"]["diagnosticCode"] == "stale_expected_head"
    assert "replace expected head is stale" not in json.dumps(job)
    with pytest.raises(ValueError, match="requires a new submission"):
        metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    inbox = metadb.list_durable_task_inbox_items(case.admission["ownerId"])
    terminal = next(item for item in inbox["items"]
                    if item["task_id"] == task["id"] and item["outcome"] == "failed")
    assert terminal["diagnostic_code"] == "stale_expected_head"


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"),
                    reason="requires dedicated PostgreSQL")
def test_postgres_two_owners_have_one_durable_merge_candidate_and_publication(
        local_catalog, tmp_path, monkeypatch):
    base = pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]})
    case = _case(local_catalog, tmp_path, base, projection="id, value AS replacement",
                 identity_columns=["id"])
    intent = _intent(case, [MergeColumnRuleV1(
        source="replacement", target="value", mode="replace")], key="postgres-owners")
    task = _admit_durable_merge(case, intent, str(uuid.uuid4()))
    stale_attempt, stale_owner = _commit_durable_merge_candidate(task, case, intent)
    assert metadb.update_merge_columns_task_phase(
        task["id"], stale_attempt, stale_owner, phase="publishing",
        status_doc=merge_columns_tasks._status(task["id"], phase="publishing", progress=0.8))
    _expire_attempt(stale_attempt)
    replacement_owner = "postgres-replacement-owner"
    replacement = metadb.claim_merge_columns_task(task["id"], replacement_owner)
    assert replacement is not None
    replacement_attempt = replacement["attempts"][-1]["id"]
    assert replacement_attempt != stale_attempt
    assert metadb.update_merge_columns_task_phase(
        task["id"], replacement_attempt, replacement_owner, phase="publishing",
        status_doc=merge_columns_tasks._status(task["id"], phase="publishing", progress=0.8))

    # Both owners enter the real catalog publication transaction from independent sessions. Hold the
    # replacement's Task lock until the stale transaction is trying to acquire that same row: the old
    # owner must block through the new commit and can then only recover the new owner's receipt.
    replacement_locked = threading.Event()
    stale_waiting = threading.Event()
    original_fence = metadb._lock_merge_columns_publication_fence

    def race_fence(s, context):
        if context.owner_token == replacement_owner:
            original_fence(s, context)
            replacement_locked.set()
            assert stale_waiting.wait(timeout=10)
            return
        assert context.owner_token == stale_owner
        assert replacement_locked.wait(timeout=10)
        stale_waiting.set()
        original_fence(s, context)

    monkeypatch.setattr(metadb, "_lock_merge_columns_publication_fence", race_fence)
    artifact_uris: dict[str, str] = {}

    def publish(owner_attempt):
        owner, attempt_id = owner_attempt
        return _publish_checkpoint_as_owner(
            case, intent, task["id"], attempt_id, owner, artifact_uris)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (
            (stale_owner, stale_attempt),
            (replacement_owner, replacement_attempt),
        )))
    assert len({outcome.revision_id for outcome in outcomes}) == 1
    receipt = outcomes[0]
    assert receipt.publication.artifact_uri == artifact_uris[replacement_owner]
    assert receipt.publication.artifact_uri != artifact_uris[stale_owner]
    assert metadb.finish_durable_task_attempt(
        task["id"], replacement_attempt, replacement_owner,
        merge_columns_tasks._done(task["id"], intent.write_intent, receipt).model_dump())
    with metadb.session() as s:
        attempts = list(s.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task["id"]).order_by(
                metadb.DurableTaskAttempt.attempt_number)))
        assert [attempt.status for attempt in attempts] == ["fenced", "done"]
    assert _revision_count(case.exact.dataset_id) == 2
    assert _merge_publication_count() == 1
