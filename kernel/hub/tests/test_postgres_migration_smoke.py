"""Real PostgreSQL release-contract smoke test (enabled only by DP_TEST_DATABASE_URL)."""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import os
import subprocess
import sys
import uuid

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text

from hub import metadb
from hub.models import ExactDatasetRef
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.sparse_outputs import (
    SparseOutputAdmissionRequest,
    SparseOutputSubmissionConflict,
    admit_sparse_output,
)
from hub.storage import LocalStorage
from hub.tests.task_manifest_helpers import with_task_manifest


def _reset_postgres(url: str):
    """Return a clean engine for this job's dedicated PostgreSQL database."""
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    metadb.engine().dispose()
    metadb._engine = metadb._Session = None
    return admin_engine


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_cli_migration_and_service_startup_contract(tmp_path):
    url = os.environ["DP_TEST_DATABASE_URL"]
    assert url.startswith("postgresql"), "DP_TEST_DATABASE_URL must name a dedicated Postgres database"

    # This job owns the dedicated database. Reset it so the CLI proves a genuinely fresh migration,
    # independent of conftest's normal test-database setup.
    admin_engine = _reset_postgres(url)
    admin_engine.dispose()

    base_env = os.environ.copy()
    base_env.update({
        "DP_DATABASE_URL": url,
        "DP_WORKSPACE": str(tmp_path),
        "DP_DATA_DIR": str(tmp_path / "data"),
        "DP_AUTH_SECRET": "0123456789abcdef0123456789abcdef",
    })
    base_env.pop("DP_AUTH_MODE", None)

    first_env = {**base_env, "DP_AUTH_PASSWORD": "postgres-bootstrap-test"}
    first = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=first_env, text=True, capture_output=True, timeout=60,
    )
    assert first.returncode == 0, first.stderr
    assert metadb.require_schema_at_head() == metadb.expected_schema_head()
    with metadb.engine().connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
        )
        assert compare_metadata(context, metadb.Base.metadata) == []
    with metadb.session() as session:
        admin = session.get(metadb.User, metadb.DEFAULT_USER_ID)
        assert admin is not None and admin.is_admin and admin.password_hash

    service_env = base_env.copy()
    service_env.pop("DP_AUTH_PASSWORD", None)
    ready_service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert ready_service.returncode == 0, ready_service.stderr

    with metadb.engine().connect() as connection:
        command.downgrade(metadb._alembic_cfg(connection), "0002_managed_file_revs")
    with metadb.engine().begin() as connection:
        connection.execute(text("DROP TABLE workspace_placements"))
        connection.execute(text("DROP TABLE workspace_containers"))
        connection.execute(text("ALTER TABLE run_records DROP COLUMN profile"))
    behind = metadb._current_schema_heads()
    assert behind == ("0002_managed_file_revs",)

    service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert service.returncode != 0, "service startup migrated a behind Postgres schema"
    assert "metadata schema is not at required Alembic head" in (service.stderr + service.stdout)
    assert metadb._current_schema_heads() == behind

    # The explicit migration command repairs the supported historical schema without service startup
    # silently mutating a behind production schema.
    restore = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=first_env, text=True, capture_output=True, timeout=60,
    )
    assert restore.returncode == 0, restore.stderr
    assert metadb.schema_at_head() is True
    with metadb.engine().connect() as connection:
        assert "profile" in {
            column.name for column in metadb.Base.metadata.tables["run_records"].columns
        }
        assert "profile" in {
            column["name"] for column in connection.dialect.get_columns(connection, "run_records")
        }
        assert connection.execute(text("""
            SELECT id FROM workspace_containers WHERE id = 'workspace-local-root'
        """)).scalar_one() == "workspace-local-root"


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_sparse_output_admission_replays_one_exact_base_reference(tmp_path):
    """Exercise the shared DB transaction boundary without allocating a sparse sidecar artifact."""
    suffix = uuid.uuid4().hex
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    owner, canvas = f"sparse-owner-{suffix}", f"sparse-canvas-{suffix}"
    logical_uri = str(tmp_path / "base.parquet")
    run_id = f"sparse-base-{suffix}"
    artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
    try:
        pq.write_table(pa.table({
            "id": pa.array([1, 2], type=pa.int32()), "payload": pa.array(["a", "b"]),
        }), artifact)
        storage.commit_result(artifact, run_id)
        published = catalog.publish_managed_local_file_output(
            name="sparse-base", logical_uri=logical_uri, artifact_uri=artifact)
        assert storage.release_result(artifact, run_id) is True
        with metadb.session() as session:
            session.add(metadb.User(id=owner, name="Sparse PostgreSQL owner"))
            session.add(metadb.Canvas(id=canvas, owner_id=owner, name="Sparse", doc="{}"))
        request = SparseOutputAdmissionRequest(
            owner_id=owner, canvas_id=canvas, submission_id="Postgres-Submission",
            dataset_ref=ExactDatasetRef(
                kind="exact", dataset_id=published["dataset_id"],
                revision_id=published["revision_id"]),
            select_config={"expr": "id, payload AS score"}, identity_columns=["id"],
            provenance={"idempotencyKey": f"sparse-{suffix}", "provenance": "manual"},
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replays = list(pool.map(lambda _ignored: admit_sparse_output(storage, request), range(4)))
        first = replays[0]
        assert {result.id for result in replays} == {first.id}
        assert sum(result.created for result in replays) == 1
        conflicting = SparseOutputAdmissionRequest(
            **{**request.__dict__, "submission_id": "Postgres-Conflict",
               "select_config": {"expr": "id, payload AS other_score"}})
        winning = SparseOutputAdmissionRequest(
            **{**request.__dict__, "submission_id": "Postgres-Conflict"})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(admit_sparse_output, storage, value) for value in (winning, conflicting)]
        outcomes = [future.exception() or future.result() for future in futures]
        assert sum(isinstance(result, SparseOutputSubmissionConflict) for result in outcomes) == 1
        assert sum(not isinstance(result, Exception) for result in outcomes) == 1
        with metadb.session() as session:
            refs = list(session.scalars(select(metadb.LocalResultReference).where(
                metadb.LocalResultReference.owner_kind == "sparse_output",
                metadb.LocalResultReference.owner_key == first.id,
            )))
            conflict_row = session.scalar(select(metadb.SparseOutput).where(
                metadb.SparseOutput.owner_id == owner,
                metadb.SparseOutput.canvas_id == canvas,
                metadb.SparseOutput.submission_id == "postgres-conflict",
            ))
            assert conflict_row is not None
            conflict_refs = list(session.scalars(select(metadb.LocalResultReference).where(
                metadb.LocalResultReference.owner_kind == "sparse_output",
                metadb.LocalResultReference.owner_key == conflict_row.id,
            )))
            artifacts = list(session.scalars(select(metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.uri == artifact)))
        assert len(refs) == 1 and refs[0].uri == artifact
        assert len(conflict_refs) == 1 and conflict_refs[0].uri == artifact
        assert len(artifacts) == 1
    finally:
        storage.close()


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_linear_checkpoint_db_time_fencing_and_reservation_race(tmp_path):
    suffix = uuid.uuid4().hex
    uid, canvas_id, submission = f"cp-user-{suffix}", f"cp-canvas-{suffix}", str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {"id": canvas_id, "version": 1, "nodes": [
        {"id": "checkpoint", "type": "write", "data": {
            "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
        {"id": "final", "type": "write", "data": {
            "title": "final", "config": {"filename": "final.parquet"}}},
    ], "edges": []}
    intent = {
        "destination": {"logicalUri": f"/tmp/{suffix}/final.parquet", "name": "final",
                        "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []}}
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Postgres checkpoint owner"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Checkpoint", doc="{}"))
    admission, _ = metadb.submit_linear_checkpoint_task(**with_task_manifest(dict(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        final_target_node_id="final", checkpoint_id=f"cp:{suffix}",
        checkpoint_node_id="checkpoint", output_port_id="out",
        task_intent_sha256="a" * 64, graph_prefix_sha256="b" * 64,
        input_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
        graph_doc=graph, input_manifest=[], write_intent=intent),
        target_key="final_target_node_id"))
    first = metadb.claim_linear_checkpoint_task(admission["task_id"], "expired-owner")
    old_attempt = first["attempts"][-1]
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, old_attempt["id"]).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    stale = {
        "task_id": admission["task_id"], "attempt_id": old_attempt["id"],
        "owner_token": "expired-owner", "namespace_id": uuid.uuid4().hex,
        "storage_root": str(tmp_path / ".dp-results"),
        "writer_token": uuid.uuid4().hex, "lock_token": uuid.uuid4().hex}
    with pytest.raises(RuntimeError, match="stale or fenced"):
        metadb.reserve_linear_checkpoint_candidate(**stale)

    current = metadb.claim_linear_checkpoint_task(admission["task_id"], "current-owner")
    attempt_id = current["attempts"][-1]["id"]
    namespace, lock_token = uuid.uuid4().hex, uuid.uuid4().hex

    def reserve(writer_token):
        return metadb.reserve_linear_checkpoint_candidate(
            task_id=admission["task_id"], attempt_id=attempt_id,
            owner_token="current-owner", namespace_id=namespace,
            storage_root=str(tmp_path / ".dp-results"),
            writer_token=writer_token, lock_token=lock_token)

    writers = [uuid.uuid4().hex for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve, writer) for writer in writers]
    winners, errors = [], []
    for future in futures:
        try:
            winners.append(future.result())
        except Exception as exc:
            errors.append(exc)
    assert len(winners) == 1
    assert len(errors) == 7
    assert all(
        isinstance(error, RuntimeError)
        and "changed exact authority" in str(error)
        for error in errors
    )
    assert metadb.linear_checkpoint_candidate(admission["task_id"]) == winners[0]


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_linear_checkpoint_commit_is_single_owner_and_db_time_fenced(tmp_path):
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import linear_checkpoint as lc
    from hub.storage import LocalStorage

    suffix = uuid.uuid4().hex
    uid, canvas_id, submission = f"cc-user-{suffix}", f"cc-canvas-{suffix}", str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {"id": canvas_id, "version": 1, "nodes": [
        {"id": "checkpoint", "type": "write", "data": {
            "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
        {"id": "final", "type": "write", "data": {
            "title": "final", "config": {"filename": "final.parquet"}}},
    ], "edges": []}
    intent = {
        "destination": {"logicalUri": f"/tmp/{suffix}/final.parquet", "name": "final",
                        "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []}}
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Postgres checkpoint committer"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Checkpoint", doc="{}"))
    admission, _ = metadb.submit_linear_checkpoint_task(**with_task_manifest(dict(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        final_target_node_id="final", checkpoint_id=f"cc:{suffix}",
        checkpoint_node_id="checkpoint", output_port_id="out",
        task_intent_sha256="a" * 64, graph_prefix_sha256="b" * 64,
        input_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
        graph_doc=graph, input_manifest=[], write_intent=intent),
        target_key="final_target_node_id"))

    store = LocalStorage(str(tmp_path / "outputs"))
    claim = metadb.claim_linear_checkpoint_task(admission["task_id"], "current-owner")
    attempt_id = claim["attempts"][-1]["id"]
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_id, owner_token="current-owner",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)

    sink = io.BytesIO()
    pq.write_table(pa.table({"id": pa.array([1, 2, 3], pa.int64())}), sink)
    content = sink.getvalue()
    writer = store.materialize_checkpoint(candidate)
    writer.write(content)
    writer.seal()
    proof = store.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
    evidence = proof.evidence

    def commit(owner_token):
        return metadb.commit_linear_checkpoint(
            task_id=admission["task_id"], attempt_id=attempt_id, owner_token=owner_token,
            namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
            lock_token=candidate["lock_token"], generation=candidate["generation"],
            rows=evidence["rows"], size_bytes=evidence["bytes"],
            content_sha256=evidence["content_sha256"], schema_sha256=evidence["schema_sha256"],
            dev=evidence["dev"], ino=evidence["ino"])

    # DB-time fencing: an expired lease cannot record the commit.
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, attempt_id).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="stale or fenced"):
        commit("current-owner")
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, attempt_id).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60))

    # Duplicate concurrent commits install exactly one owner and return identical evidence.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(commit, "current-owner") for _ in range(8)]
    docs = [future.result() for future in futures]
    proof.recheck()
    proof.close()
    writer.release()

    assert all(doc == docs[0] for doc in docs)
    assert docs[0]["phase"] == "committed"
    with metadb.session() as session:
        from sqlalchemy import func, select

        owners = session.scalars(select(metadb.LocalResultReference.owner_kind).where(
            metadb.LocalResultReference.uri == candidate["uri"])).all()
        assert owners == [metadb._LINEAR_CHECKPOINT_OWNER_KIND]
        assert session.scalar(select(func.count()).select_from(metadb.LocalResultArtifact).where(
            metadb.LocalResultArtifact.uri == candidate["uri"],
            metadb.LocalResultArtifact.state == "ready")) == 1
    guard, reopened = lc.reopen_checkpoint(LocalStorage(str(tmp_path / "outputs")),
                                           admission["task_id"])
    try:
        assert reopened.content_sha256 == evidence["content_sha256"]
    finally:
        guard.close()


def _admit_checkpoint(suffix: str):
    uid, canvas_id, submission = f"lc-user-{suffix}", f"lc-canvas-{suffix}", str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {"id": canvas_id, "version": 1, "nodes": [
        {"id": "checkpoint", "type": "write", "data": {
            "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
        {"id": "final", "type": "write", "data": {
            "title": "final", "config": {"filename": "final.parquet"}}},
    ], "edges": []}
    intent = {
        "destination": {"logicalUri": f"/tmp/{suffix}/final.parquet", "name": "final",
                        "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []}}
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Postgres lifecycle owner"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Checkpoint", doc="{}"))
    admission, _ = metadb.submit_linear_checkpoint_task(**with_task_manifest(dict(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        final_target_node_id="final", checkpoint_id=f"lc:{suffix}",
        checkpoint_node_id="checkpoint", output_port_id="out",
        task_intent_sha256="a" * 64, graph_prefix_sha256="b" * 64,
        input_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
        graph_doc=graph, input_manifest=[], write_intent=intent),
        target_key="final_target_node_id"))
    return admission, canvas_id


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_linear_checkpoint_two_owner_retire_and_release(tmp_path):
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import linear_checkpoint as lc
    from hub.storage import LocalStorage

    admission, canvas_id = _admit_checkpoint(uuid.uuid4().hex)
    store = LocalStorage(str(tmp_path / "outputs"))

    # A producer materializes an uncommitted candidate, then its lease expires and a new attempt wins.
    first = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-a")
    attempt_a = first["attempts"][-1]["id"]
    old = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    w = store.materialize_checkpoint(old)
    w.write(b"stale-bytes")
    metadb.bind_linear_checkpoint_materialization(
        task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
        uri=old["uri"], dev=w.identity()[0], ino=w.identity()[1])
    w.seal()
    w.release()
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, attempt_a).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    current = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-b")
    attempt_b = current["attempts"][-1]["id"]

    # Race the fenced old owner against the current owner. Exactly one retire wins, the current-owner
    # replays are idempotent (reserve), and every stale old-owner call is fenced. The winner survives.
    def recover(attempt_id, owner):
        return metadb.reattach_or_retire_linear_checkpoint(admission["task_id"], attempt_id, owner)

    calls = [(attempt_b, "owner-b")] * 5 + [(attempt_a, "owner-a")] * 5
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(recover, attempt_id, owner) for attempt_id, owner in calls]
    outcomes, fenced = [], 0
    for future in futures:
        try:
            outcomes.append(future.result())
        except RuntimeError as exc:
            assert "stale or fenced" in str(exc)
            fenced += 1
    actions = [outcome["action"] for outcome in outcomes]
    assert fenced == 5  # every stale old-owner recovery is refused
    assert actions.count("retire") == 1  # exactly one superseded candidate is retired
    assert set(actions) <= {"retire", "reserve"}
    # Metadata retire only marks the exact artifact reclaimable; the winner deletes its file, as
    # lc.recover_checkpoint does in production.
    retire = next(outcome for outcome in outcomes if outcome["action"] == "retire")
    assert retire["uri"] == old["uri"]
    store.discard_checkpoint_artifact(retire["uri"], retire["delete_token"], retire["lock_token"])
    assert not os.path.exists(old["uri"])  # the superseded file is gone
    assert metadb.reconcile_linear_checkpoint(admission["task_id"])["phase"] == "pending"

    # The current attempt reserves and commits a fresh generation, installing exactly one owner.
    new = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    sink = io.BytesIO()
    pq.write_table(pa.table({"id": pa.array([1, 2], pa.int64())}), sink)
    evidence = lc.materialize_and_commit_checkpoint(
        store, task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b",
        candidate=new, content=sink.getvalue())
    assert evidence.generation == new["generation"] and new["generation"] != old["generation"]

    # Concurrent explicit releases remove the owner and lifecycle exactly once; truth survives cleanup.
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, attempt_b).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))

    def release():
        return metadb.release_linear_checkpoint(admission["task_id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = [future.result() for future in [pool.submit(release) for _ in range(6)]]
    assert sum(1 for r in results if r is not None) == 1
    with metadb.session() as session:
        from sqlalchemy import func, select

        assert session.get(metadb.DurableCheckpoint, admission["task_id"]) is None
        assert session.scalar(select(func.count()).select_from(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == new["uri"])) == 0
    store.prune_results()
    assert not os.path.exists(new["uri"])
