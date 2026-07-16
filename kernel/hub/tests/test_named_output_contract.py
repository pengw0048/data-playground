"""Focused contract tests for atomic named-output publication (#263)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hub import metadb
from hub.api_errors import APIError, APIErrorCode
from hub.backends import backend_supports_named_multi_output_runs
from hub.executors.schema import schema_for_graph, schema_for_graph_ports
from hub.kernel_backend import KernelBackend
from hub.models import (
    CompilePlan, EstimateRequest, Graph, GraphEdge, GraphNode, PlanStep, RunEstimate,
    RunHistoryRecord, RunOutput, RunStatus,
)
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.profile_jobs import ProfileProcessRunner
from hub.plugins.runner import LocalRunner, _CancelToken
from hub.run_outputs import apply_cached_output, require_single_run_output
from hub.routers import runs as run_routes
from hub.routers import workspace as workspace_routes
from hub.security import current_user


SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}


def _multi_graph() -> Graph:
    return Graph(
        id="named-output-contract",
        nodes=[GraphNode(
            id="branches",
            type="section",
            data={"config": {"outputs": ["left", "right"], "outputSchema": [
                {"name": "value", "type": "int", "capabilities": []},
            ]}},
        )],
    )


def _multi_plan() -> CompilePlan:
    return CompilePlan(
        target_node_id="branches",
        steps=[PlanStep(node_id="branches", kind="section", label="branches")],
    )


def _prepare_full_run_route(monkeypatch, deps, plan: CompilePlan, events: list[str]) -> None:
    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)
    monkeypatch.setattr(
        run_routes.graph_mod, "resolve_source_refs", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_reject_invalid", lambda *_args: None)
    monkeypatch.setattr(
        run_routes.compiler, "compile_plan", lambda *_args, **_kwargs: plan)

    def route(_deps, chosen, _graph, _target):
        events.append("route")
        return chosen

    monkeypatch.setattr(run_routes, "_route_by_capability", route)


def _invoke_full_run_surface(surface: str, deps, graph: Graph):
    if surface == "estimate":
        return run_routes.run_estimate(
            EstimateRequest(graph=graph, target_node_id="branches"), uid="user")
    return run_routes.start_run(
        deps, graph, "branches", "user", confirmed=True)


def test_named_multi_output_backend_capability_is_explicit_and_fails_closed(tmp_path):
    local = LocalRunner(
        lambda _uri: None, {}, object(), str(tmp_path), node_specs=SPECS)
    assert backend_supports_named_multi_output_runs(local)

    local.forced_results = [{
        "nodeId": "branches", "portId": "left", "uri": str(tmp_path / "forced.parquet"),
    }]
    assert backend_supports_named_multi_output_runs(local)
    assert not backend_supports_named_multi_output_runs(SimpleNamespace(name="legacy"))

    class BrokenProbe:
        @staticmethod
        def supports_named_multi_output_runs():
            raise RuntimeError("capability unavailable")

    assert not backend_supports_named_multi_output_runs(BrokenProbe())


@pytest.mark.parametrize("surface", ["estimate", "start"])
def test_full_run_surfaces_reject_unsupported_selected_backend_before_work(
        surface, tmp_path, monkeypatch):
    events: list[str] = []

    class UnsupportedBackend:
        name = "isolated"

        @staticmethod
        def estimate(*_args):
            events.append("estimate")
            return RunEstimate(placement="local", needs_confirm=False)

        @staticmethod
        def run(*_args, **_kwargs):
            events.append("run")
            raise AssertionError("unsupported backend must not start")

    backend = UnsupportedBackend()

    def pick_runner(*_args):
        events.append("pick")
        return backend

    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref),
        node_specs=SPECS,
        node_builders={},
        registry={},
        node_ir={},
        workspace=str(tmp_path),
        runners=[backend],
        controller=SimpleNamespace(name="run-controller"),
        pick_runner=pick_runner,
    )
    _prepare_full_run_route(monkeypatch, deps, _multi_plan(), events)
    monkeypatch.setattr(
        run_routes, "_cone_size",
        lambda *_args: events.append("size") or (1, 1, {}),
    )

    with pytest.raises(APIError) as caught:
        _invoke_full_run_surface(surface, deps, _multi_graph())

    assert caught.value.code == APIErrorCode.MULTI_OUTPUT_UNSUPPORTED
    assert "isolated" in str(caught.value.detail)
    assert events == ["pick", "route"]


@pytest.mark.parametrize("surface", ["estimate", "start"])
def test_full_run_surfaces_reject_multi_output_when_controller_will_split(
        surface, tmp_path, monkeypatch):
    events: list[str] = []
    runner = LocalRunner(
        lambda _uri: None, {}, object(), str(tmp_path), node_specs=SPECS)

    def estimate(*_args):
        events.append("estimate")
        return RunEstimate(placement="local", needs_confirm=False)

    runner.estimate = estimate

    class Controller:
        name = "run-controller"

        @staticmethod
        def plan_for_run(*_args, **_kwargs):
            events.append("plan")
            return [
                SimpleNamespace(backend="placed"),
                SimpleNamespace(backend="default"),
            ]

        @staticmethod
        def run(*_args, **_kwargs):
            events.append("controller-run")
            raise AssertionError("unsupported controller must not allocate a run")

    controller = Controller()

    def pick_runner(*_args):
        events.append("pick")
        return runner

    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref),
        node_specs=SPECS,
        node_builders={},
        registry={},
        node_ir={},
        workspace=str(tmp_path),
        runners=[runner],
        controller=controller,
        pick_runner=pick_runner,
    )
    _prepare_full_run_route(monkeypatch, deps, _multi_plan(), events)
    monkeypatch.setattr(
        run_routes, "_require_destination_credential_preflight", lambda *_args: None)
    monkeypatch.setattr(
        run_routes, "_cone_size",
        lambda *_args: events.append("size") or (1, 1, {"branches": object()}),
    )

    with pytest.raises(APIError) as caught:
        _invoke_full_run_surface(surface, deps, _multi_graph())

    assert caught.value.code == APIErrorCode.MULTI_OUTPUT_UNSUPPORTED
    assert "run-controller" in str(caught.value.detail)
    assert events == ["pick", "route", "size", "plan"]


def test_collapsed_controller_plan_keeps_local_multi_output_estimate_admitted(
        tmp_path, monkeypatch):
    events: list[str] = []
    runner = LocalRunner(
        lambda _uri: None, {}, object(), str(tmp_path), node_specs=SPECS)
    runner.estimate = lambda *_args: RunEstimate(
        rows=1, bytes=1, placement="local", needs_confirm=False)
    controller = SimpleNamespace(
        name="run-controller",
        plan_for_run=lambda *_args, **_kwargs: [],
    )
    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref),
        node_specs=SPECS,
        node_builders={},
        registry={},
        node_ir={},
        workspace=str(tmp_path),
        runners=[runner],
        controller=controller,
        pick_runner=lambda *_args: runner,
    )
    _prepare_full_run_route(monkeypatch, deps, _multi_plan(), events)
    monkeypatch.setattr(
        run_routes, "_require_destination_credential_preflight", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_cone_size", lambda *_args: (1, 1, {}))

    estimate = _invoke_full_run_surface("estimate", deps, _multi_graph())

    assert estimate.rows == 1


def test_controller_admission_keeps_full_graph_execution_target_separate_from_output():
    planned_targets: list[str | None] = []

    class Controller:
        name = "run-controller"

        @staticmethod
        def supports_named_multi_output_runs():
            return True

        @staticmethod
        def plan_for_run(_graph, target, **_kwargs):
            planned_targets.append(target)
            return [SimpleNamespace(backend="placed")]

    deps = SimpleNamespace(controller=Controller(), node_specs=SPECS)

    regions = run_routes._controller_regions_for_run(
        deps, _multi_graph(), None, "branches", {}, multi_output=True)

    assert planned_targets == [None]
    assert len(regions) == 1


def _committed(*, rows: int = 3) -> RunOutput:
    return RunOutput(
        node_id="target",
        port_id="out",
        port_label="Result",
        wire="dataset",
        publication_kind="result",
        outcome="committed",
        uri="/tmp/result.parquet",
        rows=rows,
    )


def test_run_models_enforce_collection_row_and_terminal_invariants():
    with pytest.raises(ValidationError, match="singular run output fields"):
        RunStatus.model_validate({
            "runId": "legacy", "status": "done", "outputUri": "/tmp/legacy.parquet",
        })
    with pytest.raises(ValidationError, match="must equal its row count"):
        RunStatus(
            run_id="mismatch", status="done", target_node_id="target",
            outputs=[_committed(rows=2)], total_rows=3)
    with pytest.raises(ValidationError, match="cannot retain pending outputs"):
        RunStatus(
            run_id="pending", status="failed", target_node_id="target",
            outputs=[RunOutput(
                node_id="target", port_id="out", wire="dataset",
                publication_kind="result", outcome="pending")])
    with pytest.raises(ValidationError, match="profile.rowCount"):
        RunStatus(run_id="profile", status="done", job_type="profile", total_rows=3)
    profile = RunStatus.model_validate({
        "runId": "profile-good",
        "status": "done",
        "jobType": "profile",
        "targetNodeId": "target",
        "rowsProcessed": 3,
        "totalRows": None,
        "outputs": [],
        "profile": {"columns": [], "rowCount": 3, "sampled": False,
                    "completeness": "complete"},
    })
    assert profile.total_rows is None and profile.profile and profile.profile.row_count == 3

    status = RunStatus(
        run_id="good", status="done", target_node_id="target",
        outputs=[_committed(rows=3)], total_rows=3)
    assert status.total_rows == status.outputs[0].rows == 3


def test_graph_node_identity_is_bounded_at_graph_ingress():
    with pytest.raises(ValidationError, match="256"):
        GraphNode(id="n" * 257, type="source")


def test_long_local_failure_publishes_a_bounded_terminal_output_snapshot(
        tmp_path, monkeypatch):
    graph = Graph(
        id="bounded-failure",
        nodes=[GraphNode(id="source", type="source")],
    )
    pending = require_single_run_output(graph, "source", SPECS)
    live = RunStatus(
        run_id="long-error",
        status="running",
        target_node_id="source",
        outputs=[pending],
    )
    runner = LocalRunner(
        lambda _uri: None, {}, object(), str(tmp_path), node_specs=SPECS)
    runner.runs[live.run_id] = live
    runner._published_statuses[live.run_id] = live.model_copy(deep=True)
    detail = "execution failed: " + "x" * 5000

    def fail(*_args):
        raise RuntimeError(detail)

    monkeypatch.setattr(runner, "_execute", fail)
    runner._execute_guarded(
        live.run_id, CompilePlan(target_node_id="source"), graph, "source")

    final = runner.status(live.run_id)
    assert final.status == "failed"
    assert final.error == f"RuntimeError: {detail}"
    assert final.outputs[0].outcome == "failed"
    assert final.outputs[0].error == final.error[:4096]
    assert len(final.outputs[0].error or "") == 4096


def _write_contract_graph(filename: str) -> Graph:
    return Graph(
        id="sink-snapshot-preflight",
        nodes=[
            GraphNode(
                id="source", type="source",
                data={"config": {"uri": "/tmp/source.parquet"}},
            ),
            GraphNode(
                id="write", type="write",
                data={"config": {"filename": filename}},
            ),
        ],
        edges=[GraphEdge(id="source-write", source="source", target="write")],
    )


def _pending_write_status() -> RunStatus:
    return RunStatus(
        run_id="sink-preflight",
        status="running",
        target_node_id="write",
        outputs=[RunOutput(
            node_id="write",
            port_id="out",
            wire="dataset",
            publication_kind="catalog",
            outcome="pending",
        )],
    )


def test_invalid_sink_table_snapshot_fails_before_relation_or_write(tmp_path):
    graph = _write_contract_graph(f"{'t' * 513}.csv")
    writes: list[str] = []
    relation_reads: list[tuple[str, str | None]] = []

    class Adapter:
        @staticmethod
        def write(uri, _relation, _mode):
            writes.append(uri)
            return {"uri": uri, "rows": 1}

    class Storage:
        @staticmethod
        def output_uri(_name, _extension):
            return str(tmp_path / "output.csv")

    class Engine:
        @staticmethod
        def relation(node_id, source_handle=None):
            relation_reads.append((node_id, source_handle))
            return object()

    runner = LocalRunner(
        lambda _uri: Adapter(), {}, object(), str(tmp_path), storage=Storage())
    write_node = next(node for node in graph.nodes if node.id == "write")

    with pytest.raises(ValidationError, match="512"):
        runner._commit_write(
            write_node, graph, Engine(), _pending_write_status(), None, _CancelToken())

    assert relation_reads == []
    assert writes == []


def test_invalid_allocated_sink_uri_is_discarded_before_writer_starts(
        tmp_path, monkeypatch):
    from hub import handoff
    from hub.plugins import catalog as catalog_mod

    graph = _write_contract_graph("output.parquet")
    writes: list[str] = []
    discarded: list[str] = []
    invalid_attempt = "s3://bucket/" + "x" * 8192

    class CoreAdapter:
        @staticmethod
        def write(uri, _relation, _mode):
            writes.append(uri)
            return {"uri": uri, "rows": 1}

    CoreAdapter.__module__ = "hub.plugins.adapters"

    class Storage:
        @staticmethod
        def output_uri(_name, _extension):
            return "s3://bucket/output.parquet"

    class Engine:
        @staticmethod
        def relation(_node_id, _source_handle=None):
            return object()

    monkeypatch.setattr(
        handoff, "allocate_attempt", lambda **_kwargs: {"uri": invalid_attempt})
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    monkeypatch.setattr(
        catalog_mod, "core_managed_publisher", lambda _catalog: lambda **_kwargs: None)
    runner = LocalRunner(
        lambda _uri: CoreAdapter(), {}, object(), str(tmp_path), storage=Storage())
    write_node = next(node for node in graph.nodes if node.id == "write")

    with pytest.raises(ValidationError, match="8192"):
        runner._commit_write(
            write_node, graph, Engine(), _pending_write_status(), None, _CancelToken())

    assert writes == []
    assert discarded == [invalid_attempt]


def test_invalid_adapter_uri_snapshot_never_reaches_catalog_publication(tmp_path):
    graph = _write_contract_graph("output.csv")
    writes: list[str] = []
    publications: list[dict] = []
    fences: list[bool] = []
    invalid_uri = "x" * 8193

    class Adapter:
        @staticmethod
        def write(uri, _relation, _mode):
            writes.append(uri)
            return {"uri": invalid_uri, "rows": 1}

    class Storage:
        @staticmethod
        def output_uri(_name, _extension):
            return str(tmp_path / "output.csv")

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            publications.append(kwargs)
            return kwargs

        @staticmethod
        def get_table(_uri):
            raise AssertionError("invalid output URI must not reach catalog read-back")

    class Engine:
        @staticmethod
        def relation(_node_id, _source_handle=None):
            return object()

    status = _pending_write_status()
    runner = LocalRunner(
        lambda _uri: Adapter(), {}, Catalog(), str(tmp_path), storage=Storage())
    write_node = next(node for node in graph.nodes if node.id == "write")

    with pytest.raises(ValidationError, match="8192"):
        runner._commit_write(
            write_node, graph, Engine(), status, None, _CancelToken(),
            pre_publish=lambda **kwargs: fences.append(bool(kwargs)))

    assert writes == [str(tmp_path / "output.csv")]
    assert fences == []
    assert publications == []
    assert status.outputs[0].outcome == "pending"


def test_finished_history_requires_job_type_and_rejects_active_or_pending_state():
    base = {"id": "history", "jobType": "run", "outputs": []}
    with pytest.raises(ValidationError, match="Input should be 'done', 'failed' or 'cancelled'"):
        RunHistoryRecord.model_validate({**base, "status": "running"})
    with pytest.raises(ValidationError, match="cannot retain pending outputs"):
        RunHistoryRecord.model_validate({
            **base,
            "status": "failed",
            "outputs": [{
                "nodeId": "target", "portId": "out", "wire": "dataset",
                "publicationKind": "result", "outcome": "pending",
            }],
        })
    with pytest.raises(ValidationError, match="stores result rows only in the profile status"):
        RunHistoryRecord.model_validate({
            "id": "profile", "jobType": "profile", "status": "done", "rows": 3,
            "outputs": [],
        })


def test_durable_boundaries_revalidate_rows_and_reject_singular_fields():
    with pytest.raises(ValidationError, match="must equal its row count"):
        metadb.save_run_state("bad-state", {
            "runId": "bad-state", "status": "done", "totalRows": 4,
            "targetNodeId": "target",
            "outputs": [_committed(rows=3).model_dump(by_alias=True)],
        })
    with pytest.raises(ValidationError, match="singular run output fields"):
        metadb.save_run_state("legacy-state", {
            "run_id": "legacy-state", "status": "done", "output_uri": "/tmp/old",
        })


def test_result_cache_distinguishes_unknown_row_count_from_real_zero():
    pending = RunStatus(
        run_id="cache", status="running", target_node_id="target",
        outputs=[RunOutput(
            node_id="target", port_id="out", port_label="Result", wire="dataset",
            publication_kind="result", outcome="pending")],
    )
    unknown = _committed(rows=0).model_copy(update={"rows": None})
    assert apply_cached_output(pending, {"outputs": [unknown.model_dump()]}) is None
    zero = _committed(rows=0)
    applied = apply_cached_output(pending, {"outputs": [zero.model_dump()]})
    assert applied is not None and applied.rows == 0


def test_result_cache_rejects_dual_write_and_mismatched_port_snapshots():
    pending = RunStatus(
        run_id="cache", status="running", target_node_id="target",
        outputs=[RunOutput(
            node_id="target", port_id="out", port_label="Result", wire="dataset",
            publication_kind="result", outcome="pending")],
    )
    committed = _committed(rows=3)
    assert apply_cached_output(
        pending, {"outputs": [committed.model_dump()], "uri": committed.uri}) is None
    assert apply_cached_output(
        pending, {"outputs": [committed.model_copy(
            update={"port_label": "Stale label"}).model_dump()]}) is None
    assert apply_cached_output(
        pending, {"outputs": [committed.model_copy(
            update={"wire": "scalar"}).model_dump()]}) is None


def test_assert_quality_gate_selects_the_declared_violations_port():
    selected = []

    class Relation:
        def aggregate(self, _sql):
            return self

        def fetchone(self):
            return (0,)

    class Engine:
        def relation(self, node_id, port_id):
            selected.append((node_id, port_id))
            return Relation()

    node = SimpleNamespace(id="quality", data={"config": {"severity": "error"}})
    assert LocalRunner._check_assert(SimpleNamespace(), node, Engine()) == 0
    assert selected == [("quality", "out")]


def test_multi_output_declared_schema_is_not_misattributed_to_every_port():
    result = schema_for_graph(
        _multi_graph(), lambda _uri: None, {}, node_specs=SPECS, storage=None)
    assert result == {"branches": None}

    port_result = schema_for_graph_ports(
        _multi_graph(), lambda _uri: None, {}, node_specs=SPECS, storage=None)
    assert port_result == {"branches": {"left": None, "right": None}}


def test_kernel_backend_rejects_multi_output_before_kernel_claim(monkeypatch):
    base = SimpleNamespace(node_specs=SPECS, workspace="/tmp")
    backend = KernelBackend(base, SimpleNamespace())
    ensured = []
    monkeypatch.setattr(backend, "_ensure_kernel", lambda canvas_id: ensured.append(canvas_id))
    plan = CompilePlan(target_node_id="branches", steps=[])

    with pytest.raises(ValueError, match="does not yet support multi-output"):
        backend.run(plan, _multi_graph(), "branches", "local")
    assert ensured == []


def test_kernel_backend_preserves_none_execution_target_for_full_graph(monkeypatch):
    base = SimpleNamespace(node_specs=SPECS, workspace="/tmp")
    backend = KernelBackend(base, SimpleNamespace())
    graph = Graph(id="run-all", nodes=[
        GraphNode(id="write", type="write", data={"config": {"name": "result"}}),
        GraphNode(id="quality", type="assert", data={"config": {"severity": "error"}}),
    ])
    plan = CompilePlan(steps=[
        PlanStep(node_id="write", kind="write", label="write"),
        PlanStep(node_id="quality", kind="assert", label="quality"),
    ])
    submitted = []
    monkeypatch.setattr(backend, "_ensure_kernel", lambda _canvas: ("endpoint", "token"))

    def post(_endpoint, _path, _token, body):
        submitted.append(body)
        return {"runId": body["run_id"], "status": "failed", "outputs": []}

    monkeypatch.setattr("hub.kernel_backend._post", post)
    backend.run(plan, graph, None, "local", run_id="kernel-run-all")
    assert submitted[0]["target"] is None


def test_kernel_backend_rejects_ambiguous_run_all_before_kernel_claim(monkeypatch):
    base = SimpleNamespace(node_specs=SPECS, workspace="/tmp")
    backend = KernelBackend(base, SimpleNamespace())
    graph = Graph(id="ambiguous", nodes=[
        GraphNode(id="left", type="write", data={"config": {"name": "left"}}),
        GraphNode(id="right", type="write", data={"config": {"name": "right"}}),
    ])
    plan = CompilePlan(steps=[
        PlanStep(node_id="left", kind="write", label="left"),
        PlanStep(node_id="right", kind="write", label="right"),
    ])
    ensured = []
    monkeypatch.setattr(backend, "_ensure_kernel", lambda canvas: ensured.append(canvas))
    with pytest.raises(ValueError, match="multiple write outputs"):
        backend.run(plan, graph, None, "local")
    assert ensured == []


def test_profile_process_runner_rejects_multi_output_before_lease_or_spawn(monkeypatch, tmp_path):
    runner = ProfileProcessRunner(
        str(tmp_path), str(tmp_path), storage=object(), node_specs=SPECS)
    calls = []
    monkeypatch.setattr(
        runner, "_claim_source_leases", lambda *args: calls.append("lease"))
    monkeypatch.setattr(runner, "_spawn", lambda *args: calls.append("spawn"))

    with pytest.raises(ValueError, match="does not yet support multi-output"):
        runner.run(
            _multi_graph(), "branches", plan_digest="a" * 64,
            profile_attempt_order=1, run_id="profile-multi")
    assert calls == []
    assert "profile-multi" not in runner._profile_identities


def test_run_history_route_serializes_camel_case_snapshot_and_job_type(monkeypatch):
    monkeypatch.setattr(workspace_routes.metadb, "canvas_role", lambda *_args: "owner")
    monkeypatch.setattr(workspace_routes.metadb, "list_runs", lambda _canvas_id: [
        {
            "id": "history-row",
            "runId": "run-1",
            "jobType": "run",
            "status": "done",
            "targetNodeId": "target",
            "rows": 3,
            "outputs": [_committed(rows=3).model_dump()],
            "perNode": [{"node_id": "target", "status": "done", "rows": 3}],
        },
        {
            "id": "profile-history-row",
            "runId": "profile-1",
            "jobType": "profile",
            "status": "done",
            "targetNodeId": "target",
            "rows": None,
            "outputs": [],
            "perNode": [{"node_id": "target", "status": "done", "rows": 3}],
        },
    ])
    app = FastAPI()
    app.include_router(workspace_routes.router)
    app.dependency_overrides[current_user] = lambda: "user"

    response = TestClient(app).get("/canvas/canvas-1/runs")
    assert response.status_code == 200
    record = response.json()[0]
    assert record["jobType"] == "run"
    assert record["outputs"][0]["nodeId"] == "target"
    assert record["perNode"][0]["nodeId"] == "target"
    assert "node_id" not in record["perNode"][0]
    assert not ({"outputUri", "outputTable", "output_uri", "output_table"} & record.keys())
    profile_record = response.json()[1]
    assert profile_record["jobType"] == "profile"
    assert profile_record["rows"] is None and profile_record["outputs"] == []
