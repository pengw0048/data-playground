"""LocalRunner integration coverage for complete named-output publication sets."""

from __future__ import annotations

import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from hub import metadb
from hub.models import CompilePlan, Graph, GraphNode, PlanStep, RunStatus
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.plugins import runner as runner_module
from hub.plugins.runner import LocalRunner
from hub.storage import LocalStorage


SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    """Keep lifecycle assertions independent from the process-wide test database."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'local-named-outputs.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@dataclass
class _Relation:
    port_id: str
    rows: int

    @property
    def columns(self):
        return ["value"]

    @property
    def types(self):
        return ["BIGINT"]


class _Engine:
    spill_files: list[str] = []

    def __init__(self, relations: dict[str, _Relation]):
        self._relations = relations

    def build(self, _node_id: str):
        return self._relations

    def relations(self, _node_id: str):
        return dict(self._relations)


class _Adapter:
    def __init__(self):
        self.writes: list[str] = []
        self.fail_port: str | None = None
        self.block_port: str | None = None
        self.block_started = threading.Event()
        self.block_release = threading.Event()

    def write(self, uri, relation: _Relation, _mode, cancelled=None):
        self.writes.append(relation.port_id)
        if relation.port_id == self.block_port:
            self.block_started.set()
            assert self.block_release.wait(timeout=5)
        if callable(cancelled) and cancelled():
            raise RuntimeError("run cancelled before output commit")
        if relation.port_id == self.fail_port:
            raise RuntimeError(f"{relation.port_id} publication failed")
        pathlib.Path(uri).write_bytes(
            f"{relation.port_id}:{relation.rows}".encode("utf-8"))
        return {"uri": str(uri), "rows": relation.rows}


def _canvas_graph() -> tuple[Graph, CompilePlan, str]:
    token = uuid.uuid4().hex
    user_id, canvas_id = f"user-{token}", f"canvas-{token}"
    with metadb.session() as session:
        session.add(metadb.User(id=user_id, name="Named output test"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id,
            owner_id=user_id,
            name="Named output test",
            version=1,
            doc="{}",
        ))
    graph = Graph(
        id=canvas_id,
        nodes=[GraphNode(
            id="branches",
            type="section",
            data={"config": {"outputs": ["left", "right"]}},
        )],
    )
    plan = CompilePlan(
        target_node_id="branches",
        steps=[PlanStep(node_id="branches", kind="section", label="Branches")],
    )
    return graph, plan, canvas_id


def _runner(tmp_path, monkeypatch, relations: dict[str, _Relation]):
    storage = LocalStorage(str(tmp_path / "outputs"))
    adapter = _Adapter()
    runner = LocalRunner(
        lambda _uri: adapter,
        {},
        object(),
        str(tmp_path),
        node_specs=SPECS,
        storage=storage,
    )
    monkeypatch.setattr(
        runner_module,
        "BuildEngine",
        lambda *_args, **_kwargs: _Engine(relations),
    )
    runner.on_status = lambda graph, status: metadb.save_run_state(
        status.run_id,
        status.model_dump(),
        canvas_id=graph.id,
        publish_region=status.status in ("done", "failed"),
    )
    return runner, storage, adapter


def _wait_terminal(runner: LocalRunner, run_id: str) -> RunStatus:
    deadline = time.monotonic() + 5
    while True:
        status = runner.status(run_id)
        if status.status in ("done", "failed", "cancelled"):
            return status
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _start(runner: LocalRunner, plan: CompilePlan, graph: Graph) -> RunStatus:
    return runner.run(
        plan,
        graph,
        "branches",
        "local",
        run_id=f"run-{uuid.uuid4().hex}",
    )


def _local_refs(run_id: str) -> list[str]:
    with metadb.session() as session:
        return list(session.scalars(select(metadb.LocalResultReference.uri).where(
            metadb.LocalResultReference.owner_kind == "run_state",
            metadb.LocalResultReference.owner_key == run_id,
        ).order_by(metadb.LocalResultReference.uri)))


def test_local_runner_publishes_and_recovers_complete_named_output_set(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    try:
        started = _start(runner, plan, graph)
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "done"
        assert [output.port_id for output in final.outputs] == ["left", "right"]
        assert [output.rows for output in final.outputs] == [2, 3]
        assert all(output.outcome == "committed" for output in final.outputs)
        assert final.total_rows is None
        assert adapter.writes == ["left", "right"]
        uris = [str(output.uri) for output in final.outputs]
        assert len(set(uris)) == 2 and all(pathlib.Path(uri).exists() for uri in uris)
        assert _local_refs(final.run_id) == sorted(uris)
        assert final.run_id not in runner._owned_result_uris

        recovered = RunStatus.model_validate(metadb.get_run_state(final.run_id))
        assert recovered.outputs == final.outputs and recovered.total_rows is None
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_retains_committed_prefix_when_later_output_fails(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    adapter.fail_port = "right"
    original_write = adapter.write

    def delayed_write(*args, **kwargs):
        time.sleep(0.01)
        return original_write(*args, **kwargs)

    adapter.write = delayed_write
    cache_writes: list[dict] = []
    runner.result_put = lambda _key, doc: cache_writes.append(doc)
    try:
        started = _start(runner, plan, graph)
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "failed" and final.total_rows is None
        assert [output.outcome for output in final.outputs] == ["committed", "failed"]
        assert final.outputs[0].rows == 2 and final.outputs[0].uri
        assert final.outputs[1].uri is None
        assert "right publication failed" in (final.outputs[1].error or "")
        assert pathlib.Path(str(final.outputs[0].uri)).exists()
        assert _local_refs(final.run_id) == [str(final.outputs[0].uri)]
        assert cache_writes == []
        assert final.ms >= 10

        recovered = RunStatus.model_validate(metadb.get_run_state(final.run_id))
        assert recovered.outputs == final.outputs and recovered.ms == final.ms
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_cancellation_before_terminal_publication_exposes_no_outputs(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    adapter.block_port = "right"
    try:
        started = _start(runner, plan, graph)
        assert adapter.block_started.wait(timeout=5)
        runner.cancel(started.run_id)
        adapter.block_release.set()
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "cancelled" and final.total_rows is None
        assert [output.outcome for output in final.outputs] == ["cancelled", "cancelled"]
        assert all(output.uri is None for output in final.outputs)
        assert _local_refs(final.run_id) == []
        with metadb.session() as session:
            artifacts = list(session.scalars(select(metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.writer_run_id == final.run_id,
            )))
        assert artifacts == []
    finally:
        adapter.block_release.set()
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_cancel_wins_before_managed_terminal_publication_begins(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    adapter.block_port = "right"
    publication_entered = threading.Event()
    publication_release = threading.Event()
    try:
        started = _start(runner, plan, graph)
        assert adapter.block_started.wait(timeout=5)
        gate = runner._terminal_publication_gates[started.run_id]
        original_begin = gate.begin_publication

        def blocked_begin():
            publication_entered.set()
            assert publication_release.wait(timeout=5)
            return original_begin()

        gate.begin_publication = blocked_begin
        adapter.block_release.set()
        assert publication_entered.wait(timeout=5)
        runner.cancel(started.run_id)
        publication_release.set()
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "cancelled"
        assert [output.outcome for output in final.outputs] == ["cancelled", "cancelled"]
        assert all(output.uri is None for output in final.outputs)
        assert _local_refs(final.run_id) == []
    finally:
        adapter.block_release.set()
        publication_release.set()
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_cancel_wins_before_partial_failure_publication_begins(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    adapter.block_port = adapter.fail_port = "right"
    publication_entered = threading.Event()
    publication_release = threading.Event()
    try:
        started = _start(runner, plan, graph)
        assert adapter.block_started.wait(timeout=5)
        gate = runner._terminal_publication_gates[started.run_id]
        original_begin = gate.begin_publication

        def blocked_begin():
            publication_entered.set()
            assert publication_release.wait(timeout=5)
            return original_begin()

        gate.begin_publication = blocked_begin
        adapter.block_release.set()
        assert publication_entered.wait(timeout=5)
        observed = runner.cancel(started.run_id)
        assert observed.status == "running"
        publication_release.set()
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "cancelled" and final.total_rows is None
        assert [output.outcome for output in final.outputs] == ["cancelled", "cancelled"]
        assert all(output.uri is None for output in final.outputs)
        assert _local_refs(final.run_id) == []
        assert final.run_id not in runner._owned_result_uris
    finally:
        adapter.block_release.set()
        publication_release.set()
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_late_cancel_does_not_interrupt_started_terminal_publication(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, _adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    original_on_status = runner.on_status
    publication_entered = threading.Event()
    publication_release = threading.Event()

    def blocked_terminal_save(observed_graph, status):
        if status.status == "done":
            publication_entered.set()
            assert publication_release.wait(timeout=5)
        assert original_on_status is not None
        original_on_status(observed_graph, status)

    runner.on_status = blocked_terminal_save
    try:
        started = _start(runner, plan, graph)
        assert publication_entered.wait(timeout=5)
        observed = runner.cancel(started.run_id)
        assert observed.status == "running"
        publication_release.set()
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "done"
        assert all(output.outcome == "committed" for output in final.outputs)
        assert _local_refs(final.run_id) == sorted(str(output.uri) for output in final.outputs)
    finally:
        publication_release.set()
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_receipt_publishes_terminal_snapshot_after_response_loss(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, _adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    original_on_status = runner.on_status
    terminal_attempts = 0

    def commit_then_raise(observed_graph, status):
        nonlocal terminal_attempts
        assert original_on_status is not None
        original_on_status(observed_graph, status)
        if status.status == "done":
            terminal_attempts += 1
            raise RuntimeError("database response was lost after commit")

    runner.on_status = commit_then_raise
    runner.publication_retry_wait = lambda _delay: None
    try:
        started = _start(runner, plan, graph)
        final = _wait_terminal(runner, started.run_id)

        assert terminal_attempts == 1
        assert final.status == "done"
        recovered = RunStatus.model_validate(metadb.get_run_state(final.run_id))
        assert recovered == final
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_owner_rejection_aborts_new_named_outputs(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, _adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    original_on_status = runner.on_status

    def reject_terminal_owner(observed_graph, status):
        if status.status == "done":
            with metadb.session() as session:
                existing = session.get(metadb.RunState, status.run_id, with_for_update=True)
                assert existing is not None
                session.delete(existing)
            raise metadb.RunStatePublicationRejected("run owner was deleted")
        assert original_on_status is not None
        original_on_status(observed_graph, status)

    runner.on_status = reject_terminal_owner
    try:
        started = _start(runner, plan, graph)
        final = _wait_terminal(runner, started.run_id)

        assert final.status == "failed"
        assert all(output.uri is None for output in final.outputs)
        assert [output.outcome for output in final.outputs] == ["failed", "failed"]
        assert metadb.get_run_state(final.run_id) is None
        assert _local_refs(final.run_id) == []
        with metadb.session() as session:
            artifacts = list(session.scalars(select(metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.writer_run_id == final.run_id,
            )))
        assert artifacts == []
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_partial_failure_owner_rejection_aborts_committed_prefix(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    adapter.fail_port = "right"
    original_on_status = runner.on_status
    terminal_attempts = 0

    def reject_failed_owner(observed_graph, status):
        nonlocal terminal_attempts
        if status.status == "failed":
            terminal_attempts += 1
            with metadb.session() as session:
                existing = session.get(metadb.RunState, status.run_id, with_for_update=True)
                assert existing is not None
                session.delete(existing)
            raise metadb.RunStatePublicationRejected("run owner was deleted")
        assert original_on_status is not None
        original_on_status(observed_graph, status)

    runner.on_status = reject_failed_owner
    try:
        started = _start(runner, plan, graph)
        final = _wait_terminal(runner, started.run_id)

        assert terminal_attempts == 1
        assert final.status == "failed" and final.total_rows is None
        assert all(output.uri is None for output in final.outputs)
        assert [output.outcome for output in final.outputs] == ["failed", "failed"]
        assert "run owner was deleted" in (final.error or "")
        assert metadb.get_run_state(final.run_id) is None
        assert _local_refs(final.run_id) == []
        assert final.run_id not in runner._owned_result_uris
        with metadb.session() as session:
            artifacts = list(session.scalars(select(metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.writer_run_id == final.run_id,
            )))
        assert artifacts == []
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)


def test_local_runner_cache_is_complete_set_or_recomputes_every_output(
        tmp_path, monkeypatch):
    graph, plan, canvas_id = _canvas_graph()
    runner, storage, adapter = _runner(tmp_path, monkeypatch, {
        "left": _Relation("left", 2),
        "right": _Relation("right", 3),
    })
    runner.result_acquire = metadb.acquire_result_cache_pin
    runner.result_put = metadb.put_result
    try:
        first = _wait_terminal(runner, _start(runner, plan, graph).run_id)
        first_uris = [str(output.uri) for output in first.outputs]
        assert adapter.writes == ["left", "right"]

        pathlib.Path(first_uris[1]).unlink()
        second = _wait_terminal(runner, _start(runner, plan, graph).run_id)
        second_uris = [str(output.uri) for output in second.outputs]
        assert adapter.writes == ["left", "right", "left", "right"]
        assert set(second_uris).isdisjoint(first_uris)

        third = _wait_terminal(runner, _start(runner, plan, graph).run_id)
        assert adapter.writes == ["left", "right", "left", "right"]
        assert [str(output.uri) for output in third.outputs] == second_uris
        assert all(output.outcome == "committed" for output in third.outputs)
    finally:
        storage.close()
        metadb.delete_canvas_cascade(canvas_id)
