from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from hub.models import (
    CompilePlan,
    Graph,
    GraphNode,
    PerNodeStatus,
    PlanStep,
    RunOutput,
    RunStatus,
)
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.plugins.runner import LocalRunner
from hub.run_controller import RunController, _RegionMaterialization
from hub.run_outputs import require_single_run_output, sole_committed_document_output
from hub.subprocess_runner import SubprocessRunner


NODE_SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}


class _LocalPathStorage:
    def output_uri(self, name: str, ext: str) -> str:
        return f"/tmp/{name}{ext}"


def _node(node_id: str, kind: str, config: dict | None = None) -> GraphNode:
    return GraphNode(
        id=node_id, type=kind,
        data={"title": node_id, "config": config or {}},
    )


def _plan(*steps: tuple[str, str], target: str | None = None) -> CompilePlan:
    return CompilePlan(
        target_node_id=target,
        steps=[PlanStep(node_id=node_id, kind=kind, label=node_id)
               for node_id, kind in steps],
    )


def _runner(tmp_path) -> SubprocessRunner:
    return SubprocessRunner(
        str(tmp_path), str(tmp_path), storage=_LocalPathStorage(),
        node_specs=NODE_SPECS,
    )


def _public_status_runner(kind: str, tmp_path):
    if kind == "local":
        return LocalRunner(
            resolve_adapter=lambda _uri: None,
            registry=None,
            catalog=None,
            workspace=str(tmp_path),
            node_specs=NODE_SPECS,
            storage=SimpleNamespace(),
        )
    if kind == "subprocess":
        return _runner(tmp_path)
    if kind == "controller":
        return RunController(
            SimpleNamespace(node_specs=NODE_SPECS),
            base=object(),
            place_fn=lambda *_a: None,
        )
    raise AssertionError(f"unknown runner kind: {kind}")


def _live_status(graph: Graph, *, run_id: str = "run") -> RunStatus:
    expected = require_single_run_output(graph, "source", NODE_SPECS)
    return RunStatus(
        run_id=run_id,
        status="running",
        target_node_id="source",
        outputs=[expected],
    )


def _committed_output(expected: RunOutput, uri: str, rows: int = 7) -> RunOutput:
    return RunOutput(
        node_id=expected.node_id,
        port_id=expected.port_id,
        port_label=expected.port_label,
        wire=expected.wire,
        publication_kind=expected.publication_kind,
        outcome="committed",
        uri=uri,
        rows=rows,
    )


def _status_shape(status: RunStatus) -> tuple:
    output = status.outputs[0]
    return (
        status.status,
        output.outcome,
        output.uri,
        output.rows,
        status.total_rows,
    )


def _publish_initial_status(runner, graph: Graph, status: RunStatus) -> None:
    with runner._lock:
        runner.runs[status.run_id] = status
    runner._emit(graph, status)


def test_subprocess_run_accepts_multi_output_before_claim_and_dispatch(
        tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("check", "assert")], edges=[])
    calls: list[str] = []
    monkeypatch.setattr(
        runner, "_claim_source_leases",
        lambda *_a: calls.append("source") or {
            "stack": SimpleNamespace(close=lambda: None), "guards": [],
            "attempts": {}, "local_sources": {}})
    monkeypatch.setattr(
        runner, "_claim_sink_contracts",
        lambda *_a: calls.append("sink") or ({}, {}, {}))
    monkeypatch.setattr(runner, "_spawn", lambda *_a: calls.append("spawn"))
    runner.on_status = lambda *_a: calls.append("emit")

    runner.run(_plan(("check", "assert"), target="check"), graph, "check", "local")

    assert calls == ["source", "sink", "spawn"]
    assert runner.runs == {}


def test_subprocess_run_unit_rejects_multi_output_before_plan_or_claim(
        tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("check", "assert")], edges=[])
    calls: list[str] = []
    monkeypatch.setattr("hub.compiler.compile_plan", lambda *_a, **_k: calls.append("plan"))
    monkeypatch.setattr(runner, "_claim_source_leases", lambda *_a: calls.append("source"))
    monkeypatch.setattr(runner, "_spawn", lambda *_a: calls.append("spawn"))

    with pytest.raises(ValueError, match="does not yet support multi-output"):
        runner.run_unit(graph, "check", str(tmp_path / "result.parquet"))

    assert calls == []
    assert runner.runs == {}


def test_subprocess_run_all_keeps_execution_target_none_with_one_write(
        tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[
        _node("write", "write", {"name": "result"}),
        _node("independent-check", "assert", {
            "predicate": "value > 10", "severity": "error"}),
    ], edges=[])
    plan = _plan(("write", "write"), ("independent-check", "assert"), target=None)
    seen: dict[str, object] = {}

    def claim(_graph, target, _run_id):
        seen["claim_target"] = target
        return {"stack": SimpleNamespace(close=lambda: None), "guards": [],
                "attempts": {}, "local_sources": {}}

    def spawn(status, _extra, _graph, target):
        seen["spawn_target"] = target
        seen["status"] = status
        return status

    monkeypatch.setattr(runner, "_claim_source_leases", claim)
    monkeypatch.setattr(runner, "_claim_sink_contracts", lambda *_a: ({}, {}, {}))
    monkeypatch.setattr(runner, "_spawn", spawn)

    status = runner.run(plan, graph, None, "local")

    assert seen["claim_target"] is None
    assert seen["spawn_target"] is None
    assert status.target_node_id == "write"
    assert [(output.node_id, output.outcome) for output in status.outputs] == [
        ("write", "pending")]


def test_subprocess_multiple_writes_fail_before_identity_or_claim(
        tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("a", "write"), _node("b", "write")], edges=[])
    calls: list[str] = []
    monkeypatch.setattr(runner, "_claim_source_leases", lambda *_a: calls.append("source"))
    monkeypatch.setattr(runner, "_claim_sink_contracts", lambda *_a: calls.append("sink"))
    monkeypatch.setattr(runner, "_spawn", lambda *_a: calls.append("spawn"))

    with pytest.raises(ValueError, match="multiple write outputs"):
        runner.run(_plan(("a", "write"), ("b", "write")), graph, None, "local")

    assert calls == []
    assert runner.runs == {}


def test_controller_falls_back_for_multi_output_intermediate_before_public_state(
        tmp_path, monkeypatch):
    deps = SimpleNamespace(node_specs=NODE_SPECS)
    controller = RunController(deps, base=object(), place_fn=lambda *_a: None)
    graph = Graph(id="g", nodes=[
        _node("split", "assert"),
        _node("final", "source", {"uri": str(tmp_path / "input.parquet")}),
    ], edges=[])
    regions = [
        SimpleNamespace(
            output_node="split", node_ids={"split"}, backend="placed", cut_inputs=[]),
        SimpleNamespace(
            output_node="final", node_ids={"final"}, backend="default",
            cut_inputs=[("split", "out", "in", "final")]),
    ]
    calls: list[str] = []
    monkeypatch.setattr(controller, "plan", lambda *_a, **_k: regions)
    controller.on_status = lambda *_a: calls.append("emit")
    monkeypatch.setattr(threading, "Thread", lambda *_a, **_k: calls.append("thread"))

    result = controller.run(graph, "final")

    assert result is None
    assert calls == []
    assert controller.runs == {}


def test_region_cache_requires_canonical_matching_output_with_known_rows(tmp_path):
    graph = Graph(id="g", nodes=[_node("source", "source")], edges=[])
    expected = require_single_run_output(graph, "source", NODE_SPECS)

    class Pin:
        closed = False

        def check(self):
            pass

        def close(self):
            self.closed = True

    pin = Pin()
    uri = str(tmp_path / "cached.parquet")
    document = RunController._region_cache_document(expected, uri=uri, rows=7)
    base = SimpleNamespace(_cache_acquire=lambda *_a: (document, pin))
    controller = RunController(
        SimpleNamespace(node_specs=NODE_SPECS), base=base, place_fn=lambda *_a: None)
    controller._region_output_exists = lambda candidate: candidate == uri

    cached = controller._acquire_region_cache(
        "key", owner="owner", expected_output=expected)

    assert str(cached) == uri
    assert cached.rows == 7
    assert cached.cache_pin is pin and pin.closed is False
    output = sole_committed_document_output(document)
    assert output is not None
    assert (output.node_id, output.port_id, output.rows) == ("source", "out", 7)
    assert output.publication_kind == "result" and output.table is None


@pytest.mark.parametrize("document", [
    {"uri": "/tmp/legacy.parquet", "table": "region", "rows": 7},
    {"outputs": [{
        "node_id": "source", "port_id": "out", "port_label": None,
        "wire": "dataset", "publication_kind": "result", "outcome": "committed",
        "uri": "/tmp/unknown.parquet", "rows": None,
    }]},
    {"outputs": [{
        "node_id": "other", "port_id": "out", "port_label": None,
        "wire": "dataset", "publication_kind": "result", "outcome": "committed",
        "uri": "/tmp/wrong-node.parquet", "rows": 7,
    }]},
])
def test_region_cache_treats_legacy_unknown_or_wrong_identity_as_miss(
        tmp_path, document):
    graph = Graph(id="g", nodes=[_node("source", "source")], edges=[])
    expected = require_single_run_output(graph, "source", NODE_SPECS)

    class Pin:
        closed = False

        def close(self):
            self.closed = True

    pin = Pin()
    base = SimpleNamespace(_cache_acquire=lambda *_a: (document, pin))
    controller = RunController(
        SimpleNamespace(node_specs=NODE_SPECS), base=base, place_fn=lambda *_a: None)

    assert controller._acquire_region_cache(
        "key", owner="owner", expected_output=expected) is None
    assert pin.closed is True


def test_region_tier_copy_inherits_exact_cached_rows(tmp_path, monkeypatch):
    from hub.tiers import Tier

    graph = Graph(id="g", nodes=[_node("source", "source")], edges=[])
    expected = require_single_run_output(graph, "source", NODE_SPECS)
    destination = Tier("destination", str(tmp_path / "destination"), 0)
    source_tier = Tier("source-tier", str(tmp_path / "source-tier"), 1)
    writes: list[dict] = []
    base = SimpleNamespace(
        _plan_hash=lambda *_a: "hash",
        _cache_put=lambda _key, document: writes.append(document),
    )
    deps = SimpleNamespace(
        node_specs=NODE_SPECS, workspace=str(tmp_path),
    )
    controller = RunController(deps, base=base, place_fn=lambda *_a: None)
    controller._boundary_tier = lambda *_a: destination
    pin = SimpleNamespace(check=lambda: None, close=lambda: None)
    source = _RegionMaterialization(
        str(tmp_path / "source-tier" / "region.parquet"), pin, rows=11)
    acquisitions = iter([None, source])
    controller._acquire_region_cache = lambda *_a, **_k: next(acquisitions)
    controller._move_tier = lambda *_a, **_k: 11
    monkeypatch.setattr(
        "hub.tiers.tiers",
        lambda _workspace: {destination.name: destination, source_tier.name: source_tier},
    )
    region = SimpleNamespace(
        id="region", output_node="source", backend="default")

    materialized = controller._materialize_with_sources(
        "run", region, [region], None, graph, threading.Event(), [])

    assert materialized.rows == 11
    assert len(writes) == 1
    output = sole_committed_document_output(writes[0])
    assert output is not None
    assert RunController._region_output_identity(output) \
        == RunController._region_output_identity(expected)
    assert output.rows == 11


def test_internal_region_suppresses_callbacks_but_logical_controller_publishes(tmp_path):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("final", "source")], edges=[])
    expected = require_single_run_output(graph, "final", NODE_SPECS)
    committed = RunOutput(
        node_id="final", port_id=expected.port_id, port_label=expected.port_label,
        wire=expected.wire, publication_kind="result", outcome="committed",
        uri=str(tmp_path / "result.parquet"), rows=3,
    )
    internal = RunStatus(
        run_id="unit", status="done", target_node_id="final", total_rows=3,
        outputs=[committed],
    )
    callbacks: list[str] = []
    runner.on_status = lambda *_a: callbacks.append("internal-status")
    runner.on_complete = lambda *_a: callbacks.append("internal-complete")
    runner._internal_runs.add("unit")
    runner._emit(graph, internal)
    runner._complete(graph, "final", internal)
    assert callbacks == []

    deps = SimpleNamespace(node_specs=NODE_SPECS)
    controller = RunController(deps, base=object(), place_fn=lambda *_a: None)
    logical = RunStatus(
        run_id="logical", status="queued", placement="distributed",
        target_node_id="final", per_node=[
            PerNodeStatus(node_id="final", status="queued", label="source")],
        outputs=[expected],
    )
    controller.runs["logical"] = logical
    controller._cancel["logical"] = threading.Event()
    controller._stop["logical"] = threading.Event()
    region = SimpleNamespace(output_node="final", node_ids={"final"})
    controller._run_final = lambda *_a, **_k: internal.model_copy(
        update={"run_id": "child", "rows_processed": 3})
    controller.on_status = lambda _g, status: callbacks.append(f"status:{status.status}")
    controller.on_complete = lambda _g, _t, status: callbacks.append(
        f"complete:{status.status}")

    controller._orchestrate("logical", graph, "final", [region])

    assert controller.status("logical").status == "done"
    assert controller.status("logical").outputs[0].uri == str(tmp_path / "result.parquet")
    assert "status:done" in callbacks
    assert "complete:done" in callbacks


def test_untrusted_nonterminal_child_cannot_expose_a_provisional_uri(tmp_path):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("final", "source")], edges=[])
    expected = require_single_run_output(graph, "final", NODE_SPECS)
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", target_node_id="final", outputs=[expected])
    runner._emit(graph, runner.runs["run"])
    forged = RunStatus(
        run_id="child", status="running", target_node_id="final", total_rows=2,
        outputs=[RunOutput(
            node_id="final", port_id=expected.port_id, port_label=expected.port_label,
            wire=expected.wire, publication_kind="result", outcome="committed",
            uri="/tmp/provisional.parquet", rows=2,
        )],
    )
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(forged.model_dump()))

    assert runner._read("run", str(status_file)) is None
    # Sanitizing an untrusted provisional binding must leave the live owner internally valid too;
    # the next progress emit must not fail on a stale scalar projection from that receipt.
    assert runner.runs["run"].total_rows is None
    RunStatus.model_validate(runner.runs["run"].model_dump())
    visible = runner.status("run")
    assert visible.outputs[0].outcome == "pending"
    assert visible.outputs[0].uri is None


@pytest.mark.parametrize("kind", ["local", "subprocess", "controller"])
def test_public_status_waits_for_a_coherent_emit_during_terminal_mutation(
        kind, tmp_path):
    runner = _public_status_runner(kind, tmp_path)
    graph = Graph(id="snapshot", nodes=[_node("source", "source")], edges=[])
    live = _live_status(graph)
    callbacks: list[RunStatus] = []
    runner.on_status = lambda _graph, status: callbacks.append(status)
    _publish_initial_status(runner, graph, live)

    uri = str(tmp_path / "result.parquet")
    committed = _committed_output(live.outputs[0], uri)
    torn = threading.Event()
    finish = threading.Event()

    def publish_terminal() -> None:
        # Reproduce the old terminal three-step window: status and output are terminal while totalRows
        # still belongs to the previous state.  No public reader may observe it before the emit boundary.
        live.outputs = [committed]
        live.status = "done"
        torn.set()
        assert finish.wait(timeout=2)
        live.total_rows = 7
        runner._emit(graph, live)

    writer = threading.Thread(target=publish_terminal)
    writer.start()
    assert torn.wait(timeout=2)
    try:
        for _ in range(100):
            assert _status_shape(runner.status("run")) == (
                "running", "pending", None, None, None)
    finally:
        finish.set()
        writer.join(timeout=2)

    assert not writer.is_alive()
    assert _status_shape(runner.status("run")) == (
        "done", "committed", uri, 7, 7)
    assert callbacks == [live, live]


@pytest.mark.parametrize("kind", ["local", "subprocess", "controller"])
def test_public_status_snapshot_stress_never_tears_output_and_total_rows(
        kind, tmp_path):
    runner = _public_status_runner(kind, tmp_path)
    graph = Graph(id="snapshot-stress", nodes=[_node("source", "source")], edges=[])
    live = _live_status(graph)
    _publish_initial_status(runner, graph, live)
    pending = live.outputs[0].model_copy(deep=True)
    uri = str(tmp_path / "result.parquet")
    committed = _committed_output(pending, uri)
    running_shape = ("running", "pending", None, None, None)
    done_shape = ("done", "committed", uri, 7, 7)
    finished = threading.Event()

    def publish_repeatedly() -> None:
        try:
            for _ in range(200):
                live.status = "running"
                time.sleep(0)
                live.outputs = [pending.model_copy(deep=True)]
                time.sleep(0)
                live.total_rows = None
                runner._emit(graph, live)
                time.sleep(0.0001)

                live.outputs = [committed.model_copy(deep=True)]
                time.sleep(0)
                live.status = "done"
                time.sleep(0)
                live.total_rows = 7
                runner._emit(graph, live)
                time.sleep(0.0001)
        finally:
            finished.set()

    writer = threading.Thread(target=publish_repeatedly)
    writer.start()
    observed = {_status_shape(runner.status("run"))}
    for _ in range(500):
        shape = _status_shape(runner.status("run"))
        assert shape in (running_shape, done_shape)
        observed.add(shape)
        time.sleep(0)
    writer.join(timeout=10)

    assert not writer.is_alive()
    assert finished.is_set()
    assert _status_shape(runner.status("run")) == done_shape


@pytest.mark.parametrize("kind", ["local", "subprocess", "controller"])
def test_public_status_and_cancel_return_detached_published_snapshots(
        kind, tmp_path):
    runner = _public_status_runner(kind, tmp_path)
    graph = Graph(id="snapshot-identity", nodes=[_node("source", "source")], edges=[])
    live = _live_status(graph)
    callbacks: list[RunStatus] = []
    runner.on_status = lambda _graph, status: callbacks.append(status)
    _publish_initial_status(runner, graph, live)
    if kind == "local":
        runner._cancel["run"] = SimpleNamespace(set=lambda: None)
    elif kind == "controller":
        runner._cancel["run"] = threading.Event()
        runner._stop["run"] = threading.Event()

    first = runner.status("run")
    cancelled = runner.cancel("run")

    assert first is not live and cancelled is not live and first is not cancelled
    assert first.outputs[0] is not live.outputs[0]
    assert cancelled.outputs[0] is not live.outputs[0]
    first.error = "caller-owned mutation"
    cancelled.outputs[0].error = "caller-owned mutation"
    current = runner.status("run")
    assert current.error is None and current.outputs[0].error is None
    assert callbacks == [live]


@pytest.mark.parametrize("kind", ["local", "subprocess", "controller"])
def test_run_returns_published_snapshot_when_worker_mutates_before_return(
        kind, tmp_path, monkeypatch):
    runner = _public_status_runner(kind, tmp_path)
    graph = Graph(id="start-snapshot", nodes=[_node("source", "source")], edges=[])
    plan = _plan(("source", "op"), target="source")
    uri = str(tmp_path / "result.parquet")

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.args = args

        def start(self):
            run_id = self.args[0]
            live = runner.runs[run_id]
            live.outputs = [_committed_output(live.outputs[0], uri)]
            live.total_rows = 7
            live.status = "done"
            runner._emit(graph, live)

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    if kind == "local":
        returned = runner.run(plan, graph, "source", "local")
    elif kind == "subprocess":
        runner.on_status = lambda *_a: None
        runner.storage = SimpleNamespace(
            begin_result=lambda *_a: uri,
            result_lock_fd=lambda *_a: None,
            _read_lock_token=lambda *_a: None,
            result_namespace_identity=lambda: (1, 2),
            namespace_id="test",
            abort_result=lambda *_a: None,
        )
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        monkeypatch.setattr(
            "hub.subprocess_runner.tempfile.mkdtemp", lambda **_kwargs: str(job_dir))
        monkeypatch.setattr(
            runner, "_spawn_process",
            lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: 0))
        returned = runner.run(plan, graph, "source", "local")
    else:
        regions = [
            SimpleNamespace(
                output_node="source", node_ids={"source"}, backend="placed",
                cut_inputs=[]),
            SimpleNamespace(
                output_node="source", node_ids={"source"}, backend="default",
                cut_inputs=[]),
        ]
        monkeypatch.setattr(runner, "plan", lambda *_a, **_k: regions)
        monkeypatch.setattr(runner, "_safe_to_split", lambda *_a, **_k: True)
        returned = runner.run(graph, "source")

    live = runner.runs[returned.run_id]
    assert returned is not live
    assert _status_shape(returned) == ("done", "committed", uri, 7, 7)
    live.error = "later owner mutation"
    assert returned.error is None


@pytest.mark.parametrize("kind", ["local", "subprocess", "controller"])
def test_eviction_waits_for_a_published_terminal_checkpoint(
        kind, tmp_path, monkeypatch):
    monkeypatch.setattr("hub.plugins.runner._MAX_RUNS", 1)
    monkeypatch.setattr("hub.subprocess_runner._MAX_RUNS", 1)
    runner = _public_status_runner(kind, tmp_path)
    graph = Graph(id="eviction-snapshot", nodes=[], edges=[])
    victim = RunStatus(run_id="victim", status="running")
    newer = RunStatus(run_id="newer", status="running")
    with runner._lock:
        runner.runs[victim.run_id] = victim
        runner.runs[newer.run_id] = newer
        runner._published_statuses[victim.run_id] = victim.model_copy(deep=True)
        runner._published_statuses[newer.run_id] = newer.model_copy(deep=True)

    # The live owner enters terminal state before its coherent emit. Eviction must retain it because
    # the last public checkpoint is still running; otherwise the terminal emit can never publish.
    victim.status = "done"
    with runner._lock:
        runner._evict()
    assert victim.run_id in runner.runs
    assert runner.status(victim.run_id).status == "running"

    runner._emit(graph, victim)
    with runner._lock:
        runner._evict()
    assert victim.run_id not in runner.runs
    assert victim.run_id not in runner._published_statuses


def test_child_cannot_change_parent_job_type_or_placement(tmp_path):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("final", "source")], edges=[])
    expected = require_single_run_output(graph, "final", NODE_SPECS)
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", job_type="run", placement="local",
        target_node_id="final", outputs=[expected])
    forged = RunStatus(
        run_id="child", status="running", job_type="profile", placement="distributed",
        target_node_id="final", outputs=[])

    fenced = runner._fence_child_outputs("run", forged)
    assert fenced.job_type == "run"
    assert fenced.placement == "local"
    assert fenced.outputs[0].outcome == "pending"


@pytest.mark.parametrize(("state", "error"), [
    ("failed", "child setup failed"),
    ("cancelled", None),
])
def test_early_child_terminal_without_a_complete_contract_is_rejected(
        tmp_path, state, error):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("final", "source")], edges=[])
    expected = require_single_run_output(graph, "final", NODE_SPECS)
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", target_node_id="final", outputs=[expected])
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({
        "run_id": "child", "status": state, "placement": "local",
        "per_node": [], "error": error,
    }))

    terminal = runner._read("run", str(status_file))

    assert terminal is None


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_negative_child_receipt_cannot_publish_a_committed_output(tmp_path, state):
    runner = _runner(tmp_path)
    graph = Graph(id="g", nodes=[_node("final", "source")], edges=[])
    expected = require_single_run_output(graph, "final", NODE_SPECS)
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", target_node_id="final", outputs=[expected])
    forged = RunStatus(
        run_id="child", status=state, target_node_id="final", total_rows=4,
        error="child failed" if state == "failed" else None,
        outputs=[_committed_output(expected, "/tmp/forged.parquet", rows=4)],
    )

    fenced = runner._fence_child_outputs("run", forged)

    assert fenced.status == state
    assert fenced.total_rows is None
    if state == "failed":
        assert fenced.outputs == forged.outputs
    else:
        assert fenced.outputs[0].outcome == "cancelled"
        assert fenced.outputs[0].uri is None


def test_subprocess_local_full_result_commits_through_durable_named_output_receipt(
        tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import metadb

    metadb.migrate_db()
    source = tmp_path / "input.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SubprocessRunner(
        str(workspace), str(tmp_path / "data"), node_specs=NODE_SPECS,
    )
    runner.on_status = lambda _graph, status: metadb.save_run_state(
        status.run_id, status.model_dump())
    graph = Graph(id="receipt", nodes=[
        _node("source", "source", {"uri": str(source)})], edges=[])
    status = runner.run(
        _plan(("source", "op"), target="source"), graph, "source", "local")
    deadline = time.monotonic() + 15
    try:
        while (runner.status(status.run_id).status not in ("done", "failed", "cancelled")
               and time.monotonic() < deadline):
            time.sleep(0.02)
        final = runner.status(status.run_id)
        assert final.status == "done", final.error
        assert final.total_rows == 3
        assert len(final.outputs) == 1
        assert final.outputs[0].outcome == "committed"
        assert final.outputs[0].rows == 3
        durable = RunStatus.model_validate(metadb.get_run_state(status.run_id))
        assert durable.outputs[0].uri == final.outputs[0].uri
    finally:
        runner._terminate_all()


def test_subprocess_local_multi_output_commits_in_declaration_order(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import compiler, metadb

    metadb.migrate_db()
    source = tmp_path / "input.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SubprocessRunner(
        str(workspace), str(tmp_path / "data"), node_specs=NODE_SPECS,
    )
    runner.on_status = lambda _graph, status: metadb.save_run_state(
        status.run_id, status.model_dump())
    graph = Graph(**{"id": "multi-receipt", "nodes": [
        {"id": "source", "type": "source", "data": {
            "title": "source", "config": {"uri": str(source)}}},
        {"id": "branches", "type": "section", "data": {"title": "branches", "config": {
            "script": (
                "emit('first', inputs['in'])\n"
                "emit('second', inputs['in'])\n"),
            "outputs": ["first", "second"], "params": {}, "maxRuns": 10}}},
    ], "edges": [{"id": "source-to-branches", "source": "source", "target": "branches"}]})
    status = runner.run(
        compiler.compile_plan(graph, "branches"), graph, "branches", "local")
    deadline = time.monotonic() + 15
    try:
        while (runner.status(status.run_id).status not in ("done", "failed", "cancelled")
               and time.monotonic() < deadline):
            time.sleep(0.02)
        final = runner.status(status.run_id)
        assert final.status == "done", final.error
        assert [(output.port_id, output.outcome, output.rows) for output in final.outputs] == [
            ("first", "committed", 3), ("second", "committed", 3)]
        durable = RunStatus.model_validate(metadb.get_run_state(status.run_id))
        assert [output.uri for output in durable.outputs] == [
            output.uri for output in final.outputs]
    finally:
        runner._terminate_all()


def test_subprocess_local_second_port_commit_failure_retains_first_publication(
        tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import compiler, metadb

    metadb.migrate_db()
    source = tmp_path / "input.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SubprocessRunner(
        str(workspace), str(tmp_path / "data"), node_specs=NODE_SPECS,
    )
    runner.on_status = lambda _graph, status: metadb.save_run_state(
        status.run_id, status.model_dump())
    graph = Graph(**{"id": "partial-receipt", "nodes": [
        {"id": "source", "type": "source", "data": {
            "title": "source", "config": {"uri": str(source)}}},
        {"id": "branches", "type": "section", "data": {"title": "branches", "config": {
            "script": (
                "emit('first', inputs['in'])\n"
                "emit('second', inputs['in'])\n"),
            "outputs": ["first", "second"], "params": {}, "maxRuns": 10}}},
    ], "edges": [{"id": "source-to-branches", "source": "source", "target": "branches"}]})
    commit_result = runner.storage.commit_result
    commits: list[str] = []

    def fail_second_commit(uri, run_id):
        commits.append(uri)
        if len(commits) == 2:
            raise RuntimeError("second port commit rejected")
        return commit_result(uri, run_id)

    monkeypatch.setattr(runner.storage, "commit_result", fail_second_commit)
    status = runner.run(
        compiler.compile_plan(graph, "branches"), graph, "branches", "local")
    deadline = time.monotonic() + 15
    try:
        while (runner.status(status.run_id).status not in ("done", "failed", "cancelled")
               and time.monotonic() < deadline):
            time.sleep(0.02)
        final = runner.status(status.run_id)
        assert final.status == "failed", final.error
        assert [(output.port_id, output.outcome, output.rows) for output in final.outputs] == [
            ("first", "committed", 3), ("second", "failed", None)]
        assert len(commits) == 2
        durable = RunStatus.model_validate(metadb.get_run_state(status.run_id))
        assert [(output.port_id, output.outcome) for output in durable.outputs] == [
            ("first", "committed"), ("second", "failed")]
    finally:
        runner._terminate_all()
