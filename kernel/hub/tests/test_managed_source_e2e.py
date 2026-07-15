from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from hub import handoff, metadb


def _wait_for_reaped_terminal(runner, run_id: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while True:
        status = runner.status(run_id)
        with runner._lock:
            reaped = run_id not in runner._procs
        if status.status in ("done", "failed", "cancelled") and reaped:
            return status
        assert time.monotonic() < deadline, f"run {run_id} did not stop: {status}"
        time.sleep(0.05)


def _assert_disposable_child_metadata(job_dir, endpoint: str) -> None:
    child_db = job_dir / "workload-metadata.db"
    assert child_db.is_file()
    with sqlite3.connect(child_db) as connection:
        assert connection.execute("SELECT count(*) FROM object_attempts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM catalog_entries").fetchone() == (0,)
        assert connection.execute(
            "SELECT value FROM settings WHERE scope = 'global' AND key = 'objectStore'"
        ).fetchone() is None
        row = connection.execute(
            "SELECT value FROM settings WHERE scope = 'global' "
            "AND key = 'defaultObjectStoreCredId'"
        ).fetchone()
        creds = connection.execute("SELECT id, kind, fields_json FROM creds").fetchall()
    assert row is not None and len(creds) == 1
    default_id = json.loads(row[0])
    assert creds[0][0] == default_id and creds[0][1] == "object_store"
    object_store = json.loads(creds[0][2])
    assert object_store["endpoint"] == endpoint
    assert object_store["accessKeyId"] == "env:DP_S3_KEY"
    assert object_store["secretAccessKey"] == "env:DP_S3_SECRET"
    assert "k" not in json.dumps(object_store)  # material credential must not land in worker metadata


@pytest.mark.parametrize("backend", ["local-subprocess", "local-pool"])
def test_moto_isolated_backends_read_parent_published_managed_source(
        tmp_path, monkeypatch, backend, object_store_cred):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from moto.server import ThreadedMotoServer

    from hub import compiler
    from hub.deps import Deps
    from hub.models import Graph
    from hub.settings import settings
    from hub import subprocess_runner

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    parent_db = tmp_path / "parent-metadata.db"
    server = ThreadedMotoServer(port=0)
    server.start()
    runner = None
    attempt_uri = None
    job_dirs = []
    real_rmtree = shutil.rmtree
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        bucket = f"managed-source-{backend.replace('local-', '')}-{uuid.uuid4().hex[:8]}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = f"sqlite:///{parent_db}"
        metadb._engine = metadb._Session = None
        metadb.init_db()
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
        monkeypatch.setenv("DP_S3_ENDPOINT", endpoint)
        monkeypatch.setenv("DP_S3_KEY", "k")
        monkeypatch.setenv("DP_S3_SECRET", "s")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("DP_STORAGE_URL", str(tmp_path / "outputs"))
        if backend == "local-pool":
            monkeypatch.setenv("DP_POOL_WORKERS", '[{"name":"worker","cpu":2}]')
        else:
            monkeypatch.delenv("DP_POOL_WORKERS", raising=False)

        workspace = tmp_path / "workspace"
        data_dir = tmp_path / "data"
        workspace.mkdir()
        data_dir.mkdir()
        deps = Deps(str(workspace), str(data_dir))

        logical_uri = f"s3://{bucket}/catalog/source.parquet"
        source_run_id = f"managed-source-{uuid.uuid4().hex}"
        handle = handoff.allocate_attempt(
            logical_uri=logical_uri, kind="sink", run_id=source_run_id,
            allocation_key=f"managed-source:{source_run_id}",
            catalog_key_base="managed_source",
            uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
                logical_uri, namespace, generation, attempt_id),
        )
        attempt_uri = handle["uri"]
        table = pa.table({"value": [3, 1, 4], "label": ["three", "one", "four"]})
        parquet = pa.BufferOutputStream()
        pq.write_table(table, parquet)
        parsed = urlsplit(attempt_uri)
        client.put_object(
            Bucket=parsed.netloc,
            Key=parsed.path.lstrip("/").rstrip("/") + "/part-00000.parquet",
            Body=parquet.getvalue().to_pybytes(),
        )
        handoff.write_manifest(
            attempt_uri, run_id=source_run_id, rows=table.num_rows, schema=table.schema)
        handoff.prepare_attempt_commit(attempt_uri)
        metadb.catalog_upsert_entry(attempt_uri, "managed_source", {
            "id": "ignored", "name": "managed_source", "uri": attempt_uri,
            "format": "parquet", "row_count": table.num_rows,
            "columns": [
                {"name": "value", "type": "INT64"},
                {"name": "label", "type": "STRING"},
            ],
        })
        assert metadb.catalog_get(logical_uri)["uri"] == attempt_uri
        with metadb.session() as session:
            source_attempt = session.get(metadb.ObjectAttempt, attempt_uri)
            assert source_attempt is not None and source_attempt.state == "published"
            assert session.get(metadb.ObjectAttemptRef, {
                "ref_type": "catalog", "ref_key": source_attempt.logical_id,
            }).attempt_uri == attempt_uri

        def retained_job_dir(prefix="dp-run-"):
            job_dir = tmp_path / f"{backend}-job-{len(job_dirs) + 1}"
            job_dir.mkdir()
            job_dirs.append(job_dir)
            return str(job_dir)

        monkeypatch.setattr(
            subprocess_runner, "tempfile", SimpleNamespace(mkdtemp=retained_job_dir))
        monkeypatch.setattr(
            subprocess_runner, "shutil", SimpleNamespace(rmtree=lambda *_args, **_kwargs: None))

        runner = next(candidate for candidate in deps.runners if candidate.name == backend)
        graph = Graph.model_validate({
            "id": f"{backend}-managed-source", "version": 1,
            "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": attempt_uri}},
            }],
            "edges": [],
        })
        plan = compiler.compile_plan(
            graph, "source", deps.registry, deps.node_specs, deps.node_ir)

        started = runner.run(plan, graph, "source", "local")
        completed = _wait_for_reaped_terminal(runner, started.run_id)
        assert completed.status == "done", completed.error
        assert completed.total_rows == table.num_rows
        assert pq.read_table(completed.output_uri).to_pydict() == table.to_pydict()

        unit_output = tmp_path / f"{backend}-unit.parquet"
        unit_started = runner.run_unit(graph, "source", str(unit_output))
        unit_completed = _wait_for_reaped_terminal(runner, unit_started.run_id)
        assert unit_completed.status == "done", unit_completed.error
        assert unit_completed.total_rows == table.num_rows
        assert unit_completed.output_uri == str(unit_output)
        assert pq.read_table(unit_output).to_pydict() == table.to_pydict()

        assert len(job_dirs) == 2
        expected_attestation = {
            "attemptId": handle["attempt_id"],
            "generation": handle["generation"],
            "storageNamespace": handle["namespace"],
            "logicalUri": logical_uri,
            "kind": "sink",
        }
        for job_dir in job_dirs:
            job = json.loads((job_dir / "job.json").read_text())
            assert job["managedSourceAttempts"] == {attempt_uri: expected_attestation}
            assert job["graph"]["nodes"][0]["data"]["config"]["uri"] == attempt_uri
            _assert_disposable_child_metadata(job_dir, endpoint)

        with metadb.session() as session:
            assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == attempt_uri,
                metadb.ObjectAttemptLease.lease_type == "read",
            ))) == []
    finally:
        if runner is not None:
            runner._terminate_all()
        if attempt_uri is not None:
            with contextlib.suppress(Exception):
                metadb.catalog_delete_entry(attempt_uri)
            with contextlib.suppress(Exception):
                metadb.quarantine_object_attempt(attempt_uri, "test cleanup")
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        server.stop()
        for job_dir in job_dirs:
            real_rmtree(job_dir, ignore_errors=True)
