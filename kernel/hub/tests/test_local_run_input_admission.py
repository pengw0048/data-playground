"""Local full-run admission binds Sources to one immutable provider revision."""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pyarrow as pa
import pytest
from sqlalchemy import event

from hub import db, metadb
from hub.models import Graph, RunEstimate, RunStatus
from hub.plugins.adapters import LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import runs


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, session, url = metadb._engine, metadb._Session, settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'admission.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine, metadb._Session = engine, session


def _graph(uri: str) -> Graph:
    return Graph.model_validate({
        "id": "local-admission", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }], "edges": [],
    })


def test_manifest_is_ordered_secret_free_and_reopens_the_original_lance_head(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "input.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="input", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _graph(uri)

    manifest = runs._resolve_local_run_manifest(graph, "source", deps)
    assert list(manifest[0]) == ["node_id", "dataset_id", "revision_id", "provider", "resolved_at"]
    assert "uri" not in manifest[0] and "secret" not in str(manifest[0]).lower()
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=str(uuid.uuid4()), target_node_id="source",
        intent_sha256="a" * 64, manifest=manifest,
    )
    assert created is True

    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    bound = runs._bind_local_run_manifest(graph, metadb.local_run_input_manifest(run_id) or [], deps)
    cfg = bound.nodes[0].data["config"]
    assert cfg["_input_revision_id"] == manifest[0]["revision_id"]
    with db.run_scope():
        assert LanceAdapter().open_revision(cfg["uri"], cfg["_input_revision_id"]).fetchall() == [(1,)]


def test_same_submission_adopts_its_original_manifest_after_the_lance_head_moves(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "retry.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="retry", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _graph(uri)
    submission = str(uuid.uuid4())
    first = runs._resolve_local_run_manifest(graph, "source", deps)
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=submission, target_node_id="source",
        intent_sha256="b" * 64, manifest=first,
    )
    assert created is True
    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    moved = runs._resolve_local_run_manifest(graph, "source", deps)
    adopted_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=submission, target_node_id="source",
        intent_sha256="b" * 64, manifest=moved,
    )
    assert (adopted_id, created) == (run_id, False)
    assert metadb.local_run_input_manifest(run_id) == first


def test_manifest_rejects_secret_or_noncanonical_fields():
    with pytest.raises(ValueError, match="manifest is invalid"):
        metadb.admit_local_run_inputs(
            uid="local", canvas_id=None, submission_id=str(uuid.uuid4()), target_node_id="source",
            intent_sha256="c" * 64,
            manifest=[{"node_id": "source", "dataset_id": "dataset", "revision_id": "1",
                       "provider": "lance", "resolved_at": "now", "secret": "nope"}],
        )


def test_concurrent_fresh_sqlite_admissions_converge_on_one_row():
    with metadb.session() as session:
        session.add(metadb.Canvas(id="admission-race", owner_id="local", name="race"))
    manifest = [{
        "node_id": "source", "dataset_id": "dataset", "revision_id": "revision",
        "provider": "lance", "resolved_at": "now",
    }]
    start = threading.Barrier(2)
    results: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def delay_new_admission(session, _flush_context, _instances) -> None:
        if any(isinstance(obj, metadb.RunInputAdmission) for obj in session.new):
            time.sleep(0.2)

    def submit() -> None:
        try:
            start.wait(timeout=5)
            results.append(metadb.admit_local_run_inputs(
                uid="local", canvas_id="admission-race", submission_id=str(submission_id),
                target_node_id="source", intent_sha256="d" * 64, manifest=manifest,
            ))
        except BaseException as exc:
            errors.append(exc)

    submission_id = uuid.uuid4()
    event.listen(metadb._Session.class_, "before_flush", delay_new_admission)
    try:
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        event.remove(metadb._Session.class_, "before_flush", delay_new_admission)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len({run_id for run_id, _created in results}) == 1
    assert sorted(created for _run_id, created in results) == [False, True]


def _local_start_context(monkeypatch):
    """Build the smallest default-local route seam around the admission boundary."""
    with metadb.session() as session:
        session.add(metadb.Canvas(id="local-admission", owner_id="local", name="admission"))

    class Runner:
        @staticmethod
        def estimate(*_args):
            return RunEstimate(rows=1, bytes=1, placement="local", needs_confirm=False)

    runner = Runner()
    controller = SimpleNamespace(
        plan_for_run=lambda *_args, **_kwargs: [],
        run=lambda *_args, **_kwargs: None,
    )
    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref), registry={}, node_specs={}, node_ir={},
        runner=runner, controller=controller, pick_runner=lambda *_args: runner,
        run_index={}, run_owner={},
    )
    manifest = [{
        "node_id": "source", "dataset_id": "dataset", "revision_id": "revision",
        "provider": "lance", "resolved_at": "now",
    }]
    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs.graph_mod, "resolve_source_refs", lambda *_args: None)
    monkeypatch.setattr(runs, "_reject_invalid", lambda *_args: None)
    monkeypatch.setattr(runs.compiler, "compile_plan", lambda *_args: SimpleNamespace(acyclic=True))
    monkeypatch.setattr(runs, "_run_output_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_route_by_capability", lambda *_args: runner)
    monkeypatch.setattr(runs, "_require_destination_credential_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_cone_size", lambda *_args: (1, 1, {}))
    monkeypatch.setattr(runs, "_resolve_local_run_manifest", lambda *_args: manifest)
    monkeypatch.setattr(runs, "_bind_local_run_manifest", lambda graph, *_args: graph)
    return deps, _graph("lance://admission")


def test_queued_response_loss_adopts_the_claimed_local_run(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    calls = []

    def dispatch(_runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
        calls.append(run_id)
        return RunStatus(run_id=run_id, status="queued")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    submission_id = str(uuid.uuid4())
    first, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    retry, owner = runs.start_run(deps, graph, "source", "local", confirmed=True,
                                  submission_id=submission_id)

    assert retry.run_id == first.run_id
    assert retry.status == "queued"
    assert owner is deps.runner
    assert calls == [first.run_id]


def test_concurrent_duplicate_submission_has_one_local_dispatch_owner(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    result: list[RunStatus] = []
    errors: list[BaseException] = []

    def dispatch(_runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
        calls.append(run_id)
        entered.set()
        assert release.wait(timeout=5)
        return RunStatus(run_id=run_id, status="queued")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    submission_id = str(uuid.uuid4())

    def first_submit() -> None:
        try:
            status, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                                       submission_id=submission_id)
            result.append(status)
        except BaseException as exc:  # surface worker-thread failures to this test
            errors.append(exc)

    thread = threading.Thread(target=first_submit)
    thread.start()
    assert entered.wait(timeout=5)
    retry, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert len(result) == 1
    assert retry.run_id == result[0].run_id
    assert calls == [result[0].run_id]


def test_dispatch_exception_after_backend_side_effect_is_adopted_not_retried(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    calls: list[str] = []

    def dispatch(_runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
        calls.append(run_id)  # the backend may already have created a worker before its response fails
        raise RuntimeError("response lost after dispatch")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    submission_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="response lost"):
        runs.start_run(deps, graph, "source", "local", confirmed=True,
                       submission_id=submission_id)

    retry, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    assert retry.status == "queued"
    assert calls == [retry.run_id]


def test_failure_before_dispatch_leaves_the_admission_unclaimed(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    submission_id = str(uuid.uuid4())
    bind_manifest = runs._bind_local_run_manifest
    monkeypatch.setattr(runs, "_bind_local_run_manifest",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("revision unavailable")))

    with pytest.raises(RuntimeError, match="revision unavailable"):
        runs.start_run(deps, graph, "source", "local", confirmed=True,
                       submission_id=submission_id)
    run_id = metadb.local_run_submission_id("local", "local-admission", submission_id)
    with metadb.session() as session:
        assert session.get(metadb.RunInputAdmission, run_id).dispatched_at is None
        assert session.get(metadb.RunState, run_id) is None

    calls: list[str] = []
    monkeypatch.setattr(runs, "_bind_local_run_manifest", bind_manifest)
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        calls.append(run_id) or RunStatus(run_id=run_id, status="queued"),
    )
    retry, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    assert calls == [retry.run_id]
