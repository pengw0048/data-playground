"""Contract coverage for managed local Lance append writes submitted through a run.

A Lance append is a managed-local write: it is published by the same certified durable-Task owner as
create/replace, using the exact-head CAS + receipt reconciliation from ``write_managed_local_lance_append``.
Both the default per-canvas kernel and the explicit local-out-of-core backend route there, so a failed
attempt leaves zero committed effects and a retry converges on exactly one appended version.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pyarrow as pa
import pytest

from hub import durable_tasks, metadb
from hub.deps import Deps
from hub.models import Graph, WriteIntent
from hub.routers import runs
from hub.routers.runs import _write_admission_for_graph


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('lance-append-durable') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _select_backend(monkeypatch, execution: str) -> None:
    from hub.settings import settings
    monkeypatch.setattr(settings, "execution", execution)


def _await_durable_task(task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed is not None and observed["status"] in ("done", "failed", "cancelled"):
            return observed
        time.sleep(0.05)
    raise AssertionError(f"durable task {task_id} did not finish")


def _lance_version_count(uri: str) -> int:
    import lance
    return len(lance.dataset(uri).versions())


def _lance_append_context(tmp_path, existing: list[int] | None = None,
                          source_column: str = "value"):
    lance = pytest.importorskip("lance")
    token = uuid.uuid4().hex
    workspace = tmp_path / f"ws-{token}"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)
    deps = Deps(str(workspace), str(data_dir), maintain_storage=False)

    source = data_dir / "source.lance"
    lance.write_dataset(pa.table({source_column: [7, 8]}), str(source))
    deps.catalog._add(name=f"src-{token}", uri=str(source), strict_probe=True)

    dest_uri = deps.storage.output_uri("dest", ".lance")
    lance.write_dataset(pa.table({"value": existing if existing is not None else [1]}), dest_uri)
    deps.catalog._add(name="dest", uri=dest_uri, strict_probe=True)

    uid, canvas_id = f"user-{token}", f"canvas-{token}"
    graph = Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "data": {
                "title": "dest", "config": {"filename": "dest.lance", "writeMode": "append"}}},
        ],
        "edges": [{"id": "e", "source": "source", "target": "write"}],
    })
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Lance researcher"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=uid, name="Lance append",
            doc=json.dumps(graph.model_dump(by_alias=True, mode="json"))))
    return lance, deps, graph, dest_uri, uid


@pytest.mark.parametrize("execution", ("kernel", "local-out-of-core"))
def test_managed_lance_append_publishes_exactly_one_version_and_receipt(
        tmp_path, monkeypatch, execution):
    _select_backend(monkeypatch, execution)
    lance, deps, graph, dest_uri, uid = _lance_append_context(tmp_path, existing=[1])

    admission = _write_admission_for_graph(deps, graph, "write", uid, str(uuid.uuid4()))
    assert admission.managed and admission.provider == "managed-local-lance"
    assert admission.mode == "append"
    versions_before = _lance_version_count(dest_uri)

    real_dispatch = durable_tasks.dispatch
    monkeypatch.setattr(durable_tasks, "dispatch", lambda task_id, _deps: None)
    submission = str(uuid.uuid4())
    status, owner = runs.start_run(
        deps, graph.model_copy(deep=True), "write", uid, confirmed=True,
        submission_id=submission)
    assert owner is None
    task = metadb.durable_task(status.run_id)
    assert task is not None and task["task_kind"] == "managed_local_write"
    assert task["write_intent"]["mode"] == "append"

    # A replayed submission adopts the one task rather than opening a second.
    replay, _ = runs.start_run(
        deps, graph.model_copy(deep=True), "write", uid, confirmed=True,
        submission_id=submission)
    assert replay.run_id == status.run_id
    assert len(metadb.durable_task(status.run_id)["attempts"]) == 1

    real_dispatch(task["id"], deps)
    observed = _await_durable_task(task["id"])
    assert observed["status"] == "done", observed
    receipt = observed["output_receipt"]
    assert receipt["revisionId"] == "2"
    assert receipt["rows"] == 3  # 1 existing + 2 appended, the resulting version's total
    assert receipt["publication"]["provider"] == "managed-local-lance"
    assert _lance_version_count(dest_uri) == versions_before + 1
    assert lance.dataset(dest_uri).to_table()["value"].to_pylist() == [1, 7, 8]
    jobs = metadb.list_workspace_runs(uid, run_id=task["id"])
    assert jobs["items"][0]["outputReceipt"]["revisionId"] == "2"

    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_managed_lance_append_schema_mismatch_fails_before_any_version(
        tmp_path, monkeypatch):
    from fastapi import HTTPException

    _select_backend(monkeypatch, "kernel")
    # The source produces a `label` column; the destination head is `value` — an incompatible append.
    lance, deps, graph, dest_uri, uid = _lance_append_context(
        tmp_path, existing=[1], source_column="label")
    versions_before = _lance_version_count(dest_uri)

    monkeypatch.setattr(durable_tasks, "dispatch", lambda task_id, _deps: None)
    with pytest.raises(HTTPException) as excinfo:
        runs.start_run(
            deps, graph.model_copy(deep=True), "write", uid, confirmed=True,
            submission_id=str(uuid.uuid4()))
    assert excinfo.value.status_code == 409
    assert "schema" in str(excinfo.value.detail).lower()
    assert _lance_version_count(dest_uri) == versions_before
    assert lance.dataset(dest_uri).to_table()["value"].to_pylist() == [1]

    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_managed_lance_append_restart_after_commit_reconciles_one_version(
        tmp_path, monkeypatch):
    from hub.local_writes import write_managed_local_lance_append

    _select_backend(monkeypatch, "kernel")
    lance, deps, graph, dest_uri, uid = _lance_append_context(tmp_path, existing=[1])

    real_dispatch = durable_tasks.dispatch
    monkeypatch.setattr(durable_tasks, "dispatch", lambda task_id, _deps: None)
    submission = str(uuid.uuid4())
    status, _ = runs.start_run(
        deps, graph.model_copy(deep=True), "write", uid, confirmed=True,
        submission_id=submission)
    task = metadb.durable_task(status.run_id)
    assert task is not None and task["write_intent"]["mode"] == "append"
    intent = WriteIntent.model_validate(task["write_intent"])

    # A crashed attempt committed the version from the task's frozen intent; the response was lost.
    receipt = write_managed_local_lance_append(
        intent=intent, data=pa.table({"value": [7, 8]}))
    assert _lance_version_count(dest_uri) == 2

    # Restart recovery re-dispatches the same task; it reconciles onto the committed version.
    real_dispatch(task["id"], deps)
    observed = _await_durable_task(task["id"])
    assert observed["status"] == "done", observed
    assert observed["output_receipt"]["revisionId"] == receipt.revision_id
    assert _lance_version_count(dest_uri) == 2  # no duplicate append
    assert lance.dataset(dest_uri).to_table()["value"].to_pylist() == [1, 7, 8]

    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_expected_sink_uri_keeps_lance_uri_but_folds_parquet_directory_append():
    from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
    from hub.sinks import SinkSpec, expected_sink_uri

    lance_spec = SinkSpec.from_config({"filename": "dest.lance", "writeMode": "append"})
    lance_uri = "/tmp/dest.lance"
    # A Lance append commits in place; its published URI keeps the `.lance` suffix.
    assert expected_sink_uri(lance_spec, lance_uri, LanceAdapter()) == lance_uri
    assert expected_sink_uri(lance_spec, lance_uri, None) == lance_uri

    parquet_spec = SinkSpec.from_config({"filename": "out.parquet", "writeMode": "append"})
    # A parquet append still folds into an extension-stripped directory of part files.
    assert expected_sink_uri(parquet_spec, "/tmp/out.parquet", DuckDBAdapter()) == "/tmp/out"
