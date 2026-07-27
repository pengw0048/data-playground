"""Prepared plugin-node exact native-row restriction conformance."""

from __future__ import annotations

import importlib.util
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub import db, graph as graph_mod
from hub.backends import ExactRowRestrictionUnsupported
from hub.deps import Registry
from hub.executors.engine import BuildEngine
from hub.models import Graph
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec, ParamSpec, PortSpec
from hub.sdk import (
    ExactSourceRowRestriction,
    NodePreparation,
    UnsupportedUpstreamError,
)


@pytest.fixture(autouse=True)
def _no_core_owned_revision_overrides(monkeypatch):
    """These conformance URIs are owned directly by their fixture adapters."""
    monkeypatch.setattr(
        "hub.plugins.adapters.managed_local_file_revision_adapter",
        lambda _uri: None)
    monkeypatch.setattr(
        "hub.plugins.adapters.local_file_input_revision_adapter",
        lambda _uri: None)


KIND = "prepared-exact-row-test"
SPEC = NodeSpec(
    kind=KIND,
    title="prepared exact row test",
    category="compute",
    inputs=[PortSpec(id="in", wire="dataset")],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[
        ParamSpec(name="query", type="string", required=True),
        ParamSpec(name="limit", type="int", default=7),
    ],
    previewable=False,
)


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": 0, "y": 0},
        "data": {"config": config or {}},
    }


def _source_config(**extra) -> dict:
    return {
        "uri": "fixture://dataset",
        "_input_dataset_id": "dataset-1",
        "_input_provider": "fixture-exact",
        "_input_revision_id": "r1",
        **extra,
    }


def _graph(*, second_consumer: bool = False, source_config: dict | None = None) -> Graph:
    nodes = [
        _node("source", "source", source_config or _source_config()),
        _node("prepared", KIND, {
            "query": "needle",
            "_privateNotAParam": "must-not-leak",
        }),
    ]
    edges = [{
        "id": "restricted-edge",
        "source": "source",
        "target": "prepared",
        "data": {"wire": "dataset"},
    }]
    if second_consumer:
        nodes.append(_node("ordinary", "select", {"select": "value"}))
        edges.append({
            "id": "ordinary-edge",
            "source": "source",
            "target": "ordinary",
            "data": {"wire": "dataset"},
        })
    return Graph(**{
        "id": "prepared-test",
        "version": 1,
        "nodes": nodes,
        "edges": edges,
    })


def _registries(build, prepare):
    specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    deps = SimpleNamespace(
        builtin_kinds=set(specs),
        node_specs=specs,
        node_builders={},
        node_ir={},
    )
    Registry(deps).add_node(SPEC, build, prepare=prepare)
    return deps.node_builders, deps.node_specs


class _ExactAdapter:
    name = "fixture-exact"

    def __init__(self, events: list):
        self.events = events
        self.revisions = {
            "r1": [(1, "one"), (2, "two"), (3, "three")],
            "r2": [(1, "new-head")],
        }
        self.head = "r2"

    def open_revision_native_rows(self, _uri, revision_id, *, native_row_ids):
        self.events.append(("restricted", revision_id, native_row_ids))
        wanted = set(native_row_ids)
        values = [
            {"_rowid": row_id, "value": value}
            for row_id, value in self.revisions[revision_id]
            if row_id in wanted
        ]
        import pyarrow as pa
        table = pa.table({
            "_rowid": pa.array([item["_rowid"] for item in values], type=pa.uint64()),
            "value": pa.array([item["value"] for item in values], type=pa.string()),
        })
        return db.conn().from_arrow(table)

    def open_revision(self, _uri, revision_id):
        self.events.append(("unrestricted", revision_id))
        values = self.revisions[revision_id]
        return db.conn().sql(
            "SELECT * FROM (VALUES "
            + ", ".join(f"({row_id}, '{value}')" for row_id, value in values)
            + ") AS t(_rowid, value)"
        )


class _PreAdmissionSpyAdapter:
    """Exact fixture whose ordinary row paths are forbidden before preparation."""

    name = "pre-admission-spy"

    def __init__(self, events: list, *, metadata_rows: int | None = 123):
        self.events = events
        self.metadata_rows = metadata_rows

    def matches(self, uri: str) -> bool:
        return str(uri).startswith("prepared-spy://")

    def fingerprint(self, uri: str) -> str:
        self.events.append(("fingerprint", uri))
        return "spy-fingerprint"

    def metadata_count(self, uri: str) -> int | None:
        self.events.append(("metadata-count", uri))
        return self.metadata_rows

    def resolve_revision(self, uri: str, *, as_of=None):
        assert as_of is None
        self.events.append(("resolve-revision", uri))
        return {"revision_id": "r1"}

    def revision_history(self, _uri, *, limit, cursor=None):
        del limit, cursor
        return [], None

    def revision_schema(self, uri: str, revision_id: str):
        self.events.append(("revision-schema", uri, revision_id))
        return [
            {"name": "_rowid", "type": "uint64"},
            {"name": "value", "type": "string"},
        ]

    def open_revision_native_rows(self, uri: str, revision_id: str, *, native_row_ids):
        self.events.append(("native-rows", uri, revision_id, native_row_ids))
        import pyarrow as pa
        selected = [value for value in native_row_ids if value == 1]
        return db.conn().from_arrow(pa.table({
            "_rowid": pa.array(selected, type=pa.uint64()),
            "value": pa.array(["one"] * len(selected), type=pa.string()),
        }))

    def _forbidden(self, operation: str):
        pytest.fail(f"pre-admission prepared Source used forbidden {operation}")

    def scan(self, *_args, **_kwargs):
        self._forbidden("scan")

    def preview_scan(self, *_args, **_kwargs):
        self._forbidden("preview_scan")

    def schema(self, *_args, **_kwargs):
        self._forbidden("schema")

    def count(self, *_args, **_kwargs):
        self._forbidden("count")

    def write(self, *_args, **_kwargs):
        self._forbidden("write")

    def open_revision(self, *_args, **_kwargs):
        self._forbidden("open_revision")

    def revision_detail(self, *_args, **_kwargs):
        self._forbidden("revision_detail")


def _pre_admission_graph(*, indirect: bool) -> Graph:
    token = uuid.uuid4().hex
    declared = [
        {"name": "_rowid", "type": "uint64"},
        {"name": "value", "type": "string"},
    ]
    nodes = [
        _node("source", "source", {
            "uri": f"prepared-spy://input-{token}",
            "outputSchema": declared,
        }),
        _node("prepared", KIND, {
            "query": "needle",
            "outputSchema": declared,
        }),
        _node("outside", "source", {
            "uri": f"prepared-spy://out-of-cone-{token}",
        }),
    ]
    edges = [{
        "id": "source-prepared",
        "source": "source",
        "target": "prepared",
        "data": {"wire": "dataset"},
    }]
    if indirect:
        nodes.insert(1, _node("middle", "select", {"select": "*"}))
        edges = [
            {
                "id": "source-middle",
                "source": "source",
                "target": "middle",
                "data": {"wire": "dataset"},
            },
            {
                "id": "middle-prepared",
                "source": "middle",
                "target": "prepared",
                "data": {"wire": "dataset"},
            },
        ]
    return Graph.model_validate({
        "id": f"prepared-admission-{uuid.uuid4().hex}",
        "version": 1,
        "nodes": nodes,
        "edges": edges,
    })


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "indirect"])
def test_real_start_run_never_constructs_a_pre_admission_source_relation(
        tmp_path, monkeypatch, indirect):
    from hub import metadb
    from hub.deps import Deps
    from hub.models import CatalogTable, RunStatus
    from hub.routers import runs

    events = []
    adapter = _PreAdmissionSpyAdapter(events)
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    workspace.mkdir()
    data_dir.mkdir()
    metadb.init_db()
    deps = Deps(
        str(workspace), str(data_dir), maintain_storage=False)
    deps.adapters.insert(0, adapter)

    def prepare(_params, _immediate_inputs):
        events.append(("prepare",))
        return NodePreparation(
            state="prepared",
            restriction=ExactSourceRowRestriction("in", (1,)),
        )

    Registry(deps).add_node(
        SPEC,
        lambda _engine, _node, inputs, _state: inputs[0],
        prepare=prepare,
    )
    graph = _pre_admission_graph(indirect=indirect)
    source_uri = str(graph.nodes[0].data["config"]["uri"])
    outside_uri = str(next(
        node for node in graph.nodes if node.id == "outside"
    ).data["config"]["uri"])
    deps.catalog._persist(CatalogTable(
        id=f"tbl_prepared_spy_{uuid.uuid4().hex}",
        name="prepared admission spy",
        uri=source_uri,
    ))
    captured = {}
    monkeypatch.setattr(deps.controller, "plan_for_run", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(deps.controller, "run", lambda *_args, **_kwargs: None)

    def dispatch(
            _runner, _plan, dispatch_graph, _target, placement, *,
            run_id, **_kwargs):
        captured["graph"] = dispatch_graph
        return RunStatus(run_id=run_id, status="queued", placement=placement)

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    try:
        status, _owner = runs.start_run(
            deps, graph, "prepared", "local", confirmed=True,
            submission_id=str(uuid.uuid4()),
        )
        assert status.status == "queued"
        assert not any(
            len(event) > 1 and event[1] == outside_uri
            for event in events
        )
        assert ("metadata-count", source_uri) in events
        assert ("resolve-revision", source_uri) in events

        admitted = captured["graph"]
        engine = BuildEngine(
            admitted, deps.resolve_adapter, deps.registry, full=True,
            node_builders=deps.node_builders, node_specs=deps.node_specs,
            output_node="prepared",
        )
        if indirect:
            with pytest.raises(UnsupportedUpstreamError, match="directly wired"):
                engine.prepare()
            assert not any(
                event[0] == "native-rows" and event[-1] == (1,)
                for event in events
            )
        else:
            with db.run_scope():
                engine.prepare()
                assert engine.relation("prepared").fetchall() == [(1, "one")]
            assert any(
                event[0] == "native-rows" and event[-1] == (1,)
                for event in events
            )
    finally:
        deps.storage.close()


def test_unknown_prepared_source_cost_requires_confirmation_without_row_probes(
        tmp_path):
    from hub import metadb
    from hub.deps import Deps
    from hub.routers import runs

    events = []
    adapter = _PreAdmissionSpyAdapter(events, metadata_rows=None)
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    workspace.mkdir()
    data_dir.mkdir()
    metadb.init_db()
    deps = Deps(
        str(workspace), str(data_dir), maintain_storage=False)
    deps.adapters.insert(0, adapter)
    Registry(deps).add_node(
        SPEC,
        lambda _engine, _node, inputs, _state: inputs[0],
        prepare=lambda _params, _inputs: NodePreparation(),
    )
    graph = _pre_admission_graph(indirect=False)
    source_uri = str(graph.nodes[0].data["config"]["uri"])
    try:
        rows, byts, sizes = runs._cone_size(graph, "prepared", deps)
        estimate = runs._explain_unknown_byte_size(
            deps.runner.estimate(
                runs.compiler.compile_plan(
                    graph, "prepared", deps.registry, deps.node_specs, deps.node_ir),
                rows,
                byts,
            ),
            sizes,
        )
    finally:
        deps.storage.close()

    assert rows is None and byts is None
    assert estimate.needs_confirm is True
    assert "Prepared input may require an unrestricted full read" in estimate.breakdown
    assert ("metadata-count", source_uri) in events


def test_preparation_opens_restricted_edge_before_any_unrestricted_source_and_hands_state_once():
    events: list = []
    state = object()
    seen_params = []
    seen_inputs = []

    def prepare(params, immediate_inputs):
        events.append(("prepare",))
        seen_params.append(params)
        seen_inputs.append(immediate_inputs)
        with pytest.raises(TypeError):
            params["query"] = "mutated"
        return NodePreparation(
            state=state,
            restriction=ExactSourceRowRestriction("in", (3, 1, 999)),
        )

    builder_states = []

    def build(_engine, _node, inputs, prepared_state):
        events.append(("build",))
        builder_states.append(prepared_state)
        return inputs[0]

    builders, specs = _registries(build, prepare)
    adapter = _ExactAdapter(events)
    graph = _graph()
    engine = BuildEngine(
        graph, lambda _uri: adapter, {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with db.run_scope():
        engine.prepare()
        assert engine.source_is_fully_restricted("source")
        # This is the LocalRunner contract: a fully restricted Source step is not ordinarily lowered.
        rows = engine.relation("prepared").fetchall()
        assert engine.relation("prepared").fetchall() == rows

    assert events == [
        ("prepare",),
        ("restricted", "r1", (3, 1, 999)),
        ("build",),
    ]
    assert rows == [(1, "one"), (3, "three")]
    assert builder_states == [state]
    assert dict(seen_params[0]) == {"query": "needle", "limit": 7}
    assert seen_inputs[0].port("in").inputs[0].dataset.revision_id == "r1"


def test_restricted_and_unrestricted_consumers_of_one_source_are_isolated_per_edge():
    events: list = []

    def prepare(_params, _immediate_inputs):
        events.append(("prepare",))
        return NodePreparation(
            state="once",
            restriction=ExactSourceRowRestriction("in", (2,)),
        )

    def build(_engine, _node, inputs, prepared_state):
        assert prepared_state == "once"
        events.append(("build",))
        return inputs[0]

    builders, specs = _registries(build, prepare)
    adapter = _ExactAdapter(events)
    engine = BuildEngine(
        _graph(second_consumer=True), lambda _uri: adapter, {}, full=True,
        node_builders=builders, node_specs=specs,
    )
    with db.run_scope():
        engine.prepare()
        assert not engine.source_is_fully_restricted("source")
        engine.build("source")
        restricted = engine.relation("prepared").fetchall()
        ordinary = engine.relation("ordinary").fetchall()

    assert events == [
        ("prepare",),
        ("restricted", "r1", (2,)),
        ("unrestricted", "r1"),
        ("build",),
    ]
    assert restricted == [(2, "two")]
    assert ordinary == [("one",), ("two",), ("three",)]


@pytest.mark.parametrize(
    ("disable_other_consumer", "output_node"),
    [
        pytest.param(False, "prepared", id="other-consumer-outside-output-cone"),
        pytest.param(True, None, id="other-consumer-disabled"),
    ],
)
def test_nonexecuting_source_edges_do_not_force_an_unrestricted_open(
        disable_other_consumer, output_node):
    events = []

    def prepare(_params, _immediate_inputs):
        events.append(("prepare",))
        return NodePreparation(
            state="once",
            restriction=ExactSourceRowRestriction("in", (2,)),
        )

    def build(_engine, _node, inputs, prepared_state):
        assert prepared_state == "once"
        events.append(("build",))
        return inputs[0]

    graph = _graph(second_consumer=True)
    if disable_other_consumer:
        graph.nodes[2].data["disabled"] = True
    builders, specs = _registries(build, prepare)
    engine = BuildEngine(
        graph, lambda _uri: _ExactAdapter(events), {}, full=True,
        node_builders=builders, node_specs=specs, output_node=output_node,
    )
    with db.run_scope():
        engine.prepare()
        assert engine.source_is_fully_restricted("source")
        rows = engine.relation("prepared").fetchall()

    assert rows == [(2, "two")]
    assert events == [
        ("prepare",),
        ("restricted", "r1", (2,)),
        ("build",),
    ]


def test_disabled_source_and_disabled_prepared_node_do_no_preparation_or_adapter_io():
    events = []

    def prepare(_params, _immediate_inputs):
        events.append("prepare")
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)

    disabled_source = _graph()
    disabled_source.nodes[0].data["disabled"] = True
    source_resolutions = []
    source_engine = BuildEngine(
        disabled_source, lambda _uri: source_resolutions.append("adapter"), {},
        full=True, node_builders=builders, node_specs=specs,
        output_node="prepared",
    )
    with pytest.raises(UnsupportedUpstreamError, match="disabled execution ancestor"):
        source_engine.prepare()
    assert not source_engine.source_is_fully_restricted("source")

    disabled_intermediate = _graph()
    disabled_intermediate.nodes.insert(1, type(disabled_intermediate.nodes[0]).model_validate(
        _node("middle", "transform", {
            "mode": "map",
            "code": "def fn(row):\n    return row",
        })))
    disabled_intermediate.nodes[1].data["disabled"] = True
    disabled_intermediate.edges[0].source = "middle"
    disabled_intermediate.edges.append(type(disabled_intermediate.edges[0]).model_validate({
        "id": "source-middle",
        "source": "source",
        "target": "middle",
        "data": {"wire": "dataset"},
    }))
    intermediate_resolutions = []
    intermediate_engine = BuildEngine(
        disabled_intermediate,
        lambda _uri: intermediate_resolutions.append("adapter"), {},
        full=True, node_builders=builders, node_specs=specs,
        output_node="prepared",
    )
    with pytest.raises(UnsupportedUpstreamError, match="disabled execution ancestor"):
        intermediate_engine.prepare()

    from hub.executors.schema import schema_for_graph
    schema_events = []

    class SchemaOnlyAdapter:
        name = "fixture-exact"

        def revision_schema(self, _uri, _revision_id):
            schema_events.append("revision-schema")
            from hub.models import ColumnSchema
            return [ColumnSchema(name="value", type="string")]

        def open_revision(self, *_args, **_kwargs):
            pytest.fail("disabled schema branch must not unrestricted-open rows")

        def scan(self, *_args, **_kwargs):
            pytest.fail("disabled schema branch must not scan rows")

    inferred = schema_for_graph(
        disabled_intermediate, lambda _uri: SchemaOnlyAdapter(), {},
        node_builders=builders, node_specs=specs,
    )
    assert inferred["prepared"] is None
    assert schema_events

    disabled_prepared = _graph()
    disabled_prepared.nodes[1].data["disabled"] = True
    prepared_resolutions = []
    prepared_engine = BuildEngine(
        disabled_prepared, lambda _uri: prepared_resolutions.append("adapter"), {},
        full=True, node_builders=builders, node_specs=specs,
        output_node="prepared",
    )
    prepared_engine.prepare()
    assert not prepared_engine.source_is_fully_restricted("source")
    with pytest.raises(Exception, match="node is disabled"):
        prepared_engine.relation("prepared")

    assert events == []
    assert source_resolutions == []
    assert intermediate_resolutions == []
    assert prepared_resolutions == []


def test_local_runner_prepares_and_skips_a_fully_restricted_source_step(tmp_path):
    from hub import metadb
    from hub.compiler import compile_plan
    from hub.plugins.adapters import DuckDBAdapter
    from hub.plugins.runner import LocalRunner

    events = []

    def prepare(_params, _immediate_inputs):
        events.append(("prepare",))
        return NodePreparation(
            state="runner-state",
            restriction=ExactSourceRowRestriction("in", (1,)),
        )

    def build(_engine, _node, inputs, prepared_state):
        assert prepared_state == "runner-state"
        events.append(("build",))
        return inputs[0]

    builders, specs = _registries(build, prepare)
    exact = _ExactAdapter(events)
    exact.fingerprint = lambda _uri: "exact-r1"
    fallback = DuckDBAdapter()

    def resolve(uri):
        return exact if str(uri).startswith("fixture://") else fallback

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadb.init_db()
    runner = LocalRunner(
        resolve, {}, SimpleNamespace(), str(workspace),
        node_builders=builders, node_specs=specs,
    )
    # Normal in-process runs have a durable RunState callback. A no-op callback is sufficient for
    # this focused execution-order test because the result artifact itself remains local and disposable.
    runner.on_status = lambda _graph, _status: None
    graph = _graph()
    plan = compile_plan(graph, "prepared", {}, specs)
    started = runner.run(plan, graph, "prepared", "local")
    runner._worker_threads[started.run_id].join(timeout=10)
    status = runner.status(started.run_id)

    assert status.status == "done", status.error
    assert events == [
        ("prepare",),
        ("restricted", "r1", (1,)),
        ("build",),
    ]


def test_local_runner_cancellation_after_blocking_service_fences_exact_row_open(tmp_path):
    from hub.compiler import compile_plan
    from hub.plugins.adapters import DuckDBAdapter
    from hub.plugins.runner import LocalRunner

    entered = threading.Event()
    release = threading.Event()
    events = []

    def prepare(_params, _immediate_inputs):
        events.append("prepare-start")
        entered.set()
        assert release.wait(timeout=10)
        events.append("prepare-return")
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    exact = _ExactAdapter(events)
    exact.fingerprint = lambda _uri: "exact-r1"
    fallback = DuckDBAdapter()
    runner = LocalRunner(
        lambda uri: exact if str(uri).startswith("fixture://") else fallback,
        {}, SimpleNamespace(), str(tmp_path / "workspace"),
        node_builders=builders, node_specs=specs,
    )
    runner.on_status = lambda _graph, _status: None
    graph = _graph()
    plan = compile_plan(graph, "prepared", {}, specs)
    started = runner.run(plan, graph, "prepared", "local")
    assert entered.wait(timeout=10)
    live = runner.status(started.run_id)
    prepared_live = next(
        item for item in live.per_node if item.node_id == "prepared")
    assert prepared_live.status == "running"

    runner.cancel(started.run_id)
    release.set()
    assert runner.wait_for_worker(started.run_id, timeout=10)
    finished = runner.status(started.run_id)
    prepared_finished = next(
        item for item in finished.per_node if item.node_id == "prepared")

    assert finished.status == "cancelled"
    assert prepared_finished.status == "cancelled"
    assert prepared_finished.error is None
    assert events == ["prepare-start", "prepare-return"]


def test_preparation_failure_is_timed_and_attributed_to_intermediate_prepared_node(
        tmp_path):
    from hub.compiler import compile_plan
    from hub.plugins.adapters import DuckDBAdapter
    from hub.plugins.runner import LocalRunner

    entered = threading.Event()
    release = threading.Event()

    def prepare(_params, _immediate_inputs):
        entered.set()
        assert release.wait(timeout=10)
        raise RuntimeError("prepared service failed")

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    graph = _graph()
    graph.nodes.append(type(graph.nodes[0]).model_validate(
        _node("target", "select", {"select": "value"})))
    graph.edges.append(type(graph.edges[0]).model_validate({
        "id": "prepared-target",
        "source": "prepared",
        "target": "target",
        "data": {"wire": "dataset"},
    }))
    exact = _ExactAdapter([])
    exact.fingerprint = lambda _uri: "exact-r1"
    fallback = DuckDBAdapter()
    runner = LocalRunner(
        lambda uri: exact if str(uri).startswith("fixture://") else fallback,
        {}, SimpleNamespace(), str(tmp_path / "workspace"),
        node_builders=builders, node_specs=specs,
    )
    runner.on_status = lambda _graph, _status: None
    plan = compile_plan(graph, "target", {}, specs)
    started = runner.run(plan, graph, "target", "local")
    assert entered.wait(timeout=10)
    live = runner.status(started.run_id)
    assert next(
        item for item in live.per_node if item.node_id == "prepared"
    ).status == "running"
    assert next(
        item for item in live.per_node if item.node_id == "target"
    ).status == "queued"

    time.sleep(0.02)
    release.set()
    assert runner.wait_for_worker(started.run_id, timeout=10)
    finished = runner.status(started.run_id)
    prepared = next(
        item for item in finished.per_node if item.node_id == "prepared")
    target = next(item for item in finished.per_node if item.node_id == "target")

    assert finished.status == "failed"
    assert prepared.status == "failed"
    assert prepared.ms is not None and prepared.ms >= 10
    assert "prepared service failed" in str(prepared.error)
    assert "prepared-exact-row-test" in str(finished.error)
    assert target.error is None


@pytest.mark.parametrize(
    ("row_ids", "error"),
    [
        ((1, 1), "deduplicated"),
        (tuple(range(51)), "at most 50"),
        ((-1,), "uint64"),
        (((1 << 64),), "uint64"),
        ((True,), "uint64"),
        ((1.5,), "uint64"),
    ],
)
def test_invalid_native_row_restrictions_fail_before_adapter_resolution(row_ids, error):
    calls = []

    def prepare(_params, _immediate_inputs):
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", row_ids))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    engine = BuildEngine(
        _graph(), lambda _uri: calls.append("resolved"), {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with pytest.raises((TypeError, ValueError), match=error):
        engine.prepare()
    assert calls == []


def test_missing_native_row_capability_fails_closed_without_opening_exact_or_head():
    events = []

    class NoCapability:
        name = "fixture-exact"

        def open_revision(self, *_args, **_kwargs):
            events.append("unrestricted")
            pytest.fail("must not fall back to open_revision")

        def scan(self, *_args, **_kwargs):
            events.append("head")
            pytest.fail("must not fall back to scan")

    def prepare(_params, _immediate_inputs):
        events.append("prepare")
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    engine = BuildEngine(
        _graph(), lambda _uri: NoCapability(), {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with pytest.raises(ExactRowRestrictionUnsupported, match="no exact native-row capability"):
        engine.prepare()
    assert events == ["prepare"]


def test_enforced_source_schema_fails_before_any_adapter_row_read():
    events = []

    class SpyAdapter:
        name = "fixture-exact"

        def open_revision_native_rows(self, *_args, **_kwargs):
            events.append("restricted")
            pytest.fail("enforced schema must fail before the exact row read")

        def open_revision(self, *_args, **_kwargs):
            events.append("unrestricted")
            pytest.fail("must not fall back to the unrestricted exact revision")

        def scan(self, *_args, **_kwargs):
            events.append("head")
            pytest.fail("must not fall back to the mutable head")

    def prepare(_params, _immediate_inputs):
        events.append("prepare")
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    engine = BuildEngine(
        _graph(source_config=_source_config(enforceSchema=True)),
        lambda _uri: SpyAdapter(), {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with pytest.raises(
            UnsupportedUpstreamError,
            match="does not support enforceSchema"):
        engine.prepare()
    assert events == ["prepare"]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("unavailable", id="unavailable"),
        pytest.param("permission", id="permission-lost"),
        pytest.param("offline", id="provider-offline"),
    ],
)
def test_exact_native_row_capability_preserves_revision_failure_taxonomy(failure):
    from hub.plugins.adapters import (
        RevisionPermissionLost,
        RevisionProviderOffline,
        RevisionUnavailable,
    )

    errors = {
        "unavailable": RevisionUnavailable("revision_unavailable"),
        "permission": RevisionPermissionLost("revision_permission_lost"),
        "offline": RevisionProviderOffline("revision_provider_offline"),
    }
    events = []

    class FailingCapability:
        name = "fixture-exact"

        def open_revision_native_rows(self, _uri, _revision_id, *, native_row_ids):
            events.append(("restricted", native_row_ids))
            raise errors[failure]

        def open_revision(self, *_args, **_kwargs):
            pytest.fail("must not fall back to unrestricted exact open")

        def scan(self, *_args, **_kwargs):
            pytest.fail("must not fall back to mutable head")

    def prepare(_params, _immediate_inputs):
        events.append(("prepare",))
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    engine = BuildEngine(
        _graph(), lambda _uri: FailingCapability(), {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with pytest.raises(type(errors[failure]), match=str(errors[failure])):
        engine.prepare()
    assert events == [("prepare",), ("restricted", (1,))]


def test_isolated_exact_artifact_without_native_capability_fails_closed(tmp_path):
    events = []

    class ArtifactAdapter:
        name = "artifact"

        def open_revision(self, *_args, **_kwargs):
            pytest.fail("must not open the artifact without the exact native-row capability")

        def scan(self, *_args, **_kwargs):
            pytest.fail("must not scan the artifact")

    def prepare(_params, _immediate_inputs):
        events.append("prepare")
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    graph = _graph(source_config=_source_config(
        _input_artifact_uri=str(tmp_path / "exact.parquet")))
    graph._input_artifact_uris["source"] = str(tmp_path / "exact.parquet")
    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    engine = BuildEngine(
        graph, lambda _uri: ArtifactAdapter(), {}, full=True,
        node_builders=builders, node_specs=specs, output_node="prepared",
    )
    with pytest.raises(ExactRowRestrictionUnsupported, match="no exact native-row capability"):
        engine.prepare()
    assert events == ["prepare"]


def test_mutable_wrong_topology_and_generated_inputs_fail_before_adapter_resolution():
    def prepare(_params, _immediate_inputs):
        return NodePreparation(
            restriction=ExactSourceRowRestriction("in", (1,)))

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)

    mutable = _graph(source_config={"uri": "fixture://mutable"})
    resolver_calls = []
    with pytest.raises(UnsupportedUpstreamError, match="admitted immutable"):
        BuildEngine(
            mutable, lambda _uri: resolver_calls.append("mutable"), {}, full=True,
            node_builders=builders, node_specs=specs, output_node="prepared",
        ).prepare()

    indirect = _graph()
    indirect.nodes.insert(1, type(indirect.nodes[0]).model_validate(
        _node("filter", "filter", {"predicate": "value <> ''"})))
    indirect.edges[0].source = "filter"
    indirect.edges.append(type(indirect.edges[0]).model_validate({
        "id": "source-filter",
        "source": "source",
        "target": "filter",
        "data": {"wire": "dataset"},
    }))
    assert graph_mod.structural_errors(indirect, specs) == []
    with pytest.raises(UnsupportedUpstreamError, match="directly wired"):
        BuildEngine(
            indirect, lambda _uri: resolver_calls.append("indirect"), {}, full=True,
            node_builders=builders, node_specs=specs, output_node="prepared",
        ).prepare()

    generated = _graph()
    generated._controller_generated_source_ids = {"source"}
    with pytest.raises(UnsupportedUpstreamError, match="directly wired"):
        BuildEngine(
            generated, lambda _uri: resolver_calls.append("generated"), {}, full=True,
            node_builders=builders, node_specs=specs, output_node="prepared",
        ).prepare()
    assert resolver_calls == []


def test_schema_only_and_preview_preparation_entrypoints_do_no_io():
    calls = []

    def prepare(_params, _immediate_inputs):
        calls.append("prepare")
        raise AssertionError("preparation I/O is full-runtime only")

    builders, specs = _registries(
        lambda _engine, _node, inputs, _state: inputs[0], prepare)
    graph = _graph()
    graph.nodes[1].data["config"]["outputSchema"] = [{"name": "value", "type": "string"}]

    schema = BuildEngine(
        graph, lambda _uri: pytest.fail("declared schema must not open a Source"), {},
        full=True, schema_only=True, node_builders=builders, node_specs=specs,
        output_node="prepared",
    )
    preview = BuildEngine(
        graph, lambda _uri: pytest.fail("prepare() must be a no-op for preview"), {},
        full=False, sample_k=10, node_builders=builders, node_specs=specs,
        output_node="prepared",
    )
    with db.run_scope():
        assert schema.relation("prepared").columns == ["value"]
        preview.prepare()
    assert calls == []


def test_prepared_registration_rejects_distributed_ir_and_preserves_legacy_builder_shape():
    specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    deps = SimpleNamespace(
        builtin_kinds=set(specs),
        node_specs=specs,
        node_builders={},
        node_ir={},
    )
    registry = Registry(deps)
    legacy = NodeSpec(
        kind="legacy-shape",
        title="legacy shape",
        category="compute",
        inputs=[PortSpec(id="in")],
        outputs=[PortSpec(id="out")],
    )
    legacy_build = lambda _engine, _node, inputs: inputs[0]
    registry.add_node(legacy, legacy_build)
    assert deps.node_builders["legacy-shape"] is legacy_build

    prepared = SPEC.model_copy(update={"kind": "prepared-with-ir"})
    with pytest.raises(ValueError, match="cannot also register distributed IR"):
        registry.add_node(
            prepared,
            lambda _engine, _node, inputs, _state: inputs[0],
            ir=lambda _node: {"op": "map", "config": {}},
            prepare=lambda _params, _inputs: NodePreparation(),
        )


def test_reference_plugin_manifest_and_registration_match_public_prepared_api():
    plugin_dir = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "dp_exact_row_fixture"
    )
    import tomllib

    manifest = tomllib.loads((plugin_dir / "dataplay.toml").read_text())
    assert manifest["min_core_api"] == 2

    module_spec = importlib.util.spec_from_file_location(
        "dp_exact_row_fixture_reference", plugin_dir / "__init__.py")
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    deps = SimpleNamespace(
        builtin_kinds=set(specs),
        node_specs=specs,
        node_builders={},
        node_ir={},
    )
    module.register(Registry(deps))
    assert "exact-row-fixture" in deps.node_builders


def test_lance_exact_native_rows_are_native_range_bounded_and_revision_pinned(
        tmp_path, monkeypatch):
    lance = pytest.importorskip("lance")
    pa = pytest.importorskip("pyarrow")
    from lance.dataset import LanceDataset
    from hub.plugins.adapters import LanceAdapter

    path = str(tmp_path / "multi-fragment.lance")
    schema = pa.schema([
        pa.field("value", pa.string()),
        pa.field("nested", pa.struct([
            pa.field("tags", pa.list_(pa.string())),
            pa.field("score", pa.int64()),
        ])),
    ])
    lance.write_dataset(
        pa.Table.from_pylist([
            {"value": "v1-a", "nested": {"tags": ["first"], "score": 1}},
            {"value": "v1-b", "nested": {"tags": ["second"], "score": 2}},
        ], schema=schema),
        path,
        enable_stable_row_ids=True,
    )
    lance.write_dataset(pa.Table.from_pylist([
        {"value": "v2-a", "nested": {"tags": ["third"], "score": 3}},
        {"value": "v2-b", "nested": {"tags": ["fourth"], "score": 4}},
    ], schema=schema), path, mode="append")
    exact = lance.dataset(path, version=2)
    assert exact.has_stable_row_ids is True
    assert len(exact.get_fragments()) >= 2
    exact_rows = exact.scanner(with_row_id=True).to_table().to_pylist()
    first_id = exact_rows[0]["_rowid"]
    last_id = exact_rows[-1]["_rowid"]

    # Move mutable head after admission. Its row id must be absent from the exact-v2 result.
    lance.write_dataset(pa.table({"value": ["head-only"]}), path, mode="append")
    head_only_id = lance.dataset(path).scanner(
        with_row_id=True).to_table().to_pylist()[-1]["_rowid"]

    scan_calls = []
    statistics = []
    real_scanner = LanceDataset.scanner

    reject_scanner = False

    def tracked_scanner(self, *args, **kwargs):
        if reject_scanner:
            pytest.fail("an empty native-row probe must not create a Lance scanner")
        scan_calls.append(dict(kwargs))
        if kwargs.get("filter") is not None:
            kwargs["scan_stats_callback"] = statistics.append
        return real_scanner(self, *args, **kwargs)

    monkeypatch.setattr(LanceDataset, "scanner", tracked_scanner)
    monkeypatch.setattr(
        LanceDataset, "take",
        lambda *_args, **_kwargs: pytest.fail("positional take() is forbidden"))
    monkeypatch.setattr(
        LanceDataset, "_take_rows",
        lambda *_args, **_kwargs: pytest.fail("the adapter uses the native range planner"))

    adapter = LanceAdapter()
    candidates = (last_id, first_id, head_only_id, 99, (1 << 64) - 1)
    with db.run_scope():
        relation = adapter.open_revision_native_rows(
            path, "2", native_row_ids=candidates)
        rows = relation.order("_rowid").to_arrow_table().to_pylist()
        reject_scanner = True
        empty = adapter.open_revision_native_rows(
            path, "2", native_row_ids=()).to_arrow_table()

    assert rows == [
        {"value": "v1-a", "nested": {"tags": ["first"], "score": 1}, "_rowid": first_id},
        {"value": "v2-b", "nested": {"tags": ["fourth"], "score": 4}, "_rowid": last_id},
    ]
    assert empty.num_rows == 0
    assert empty.schema.names == ["value", "nested", "_rowid"]
    assert empty.schema.field("nested").type == schema.field("nested").type
    assert empty.schema.field("_rowid").type == pa.uint64()
    assert all(call.get("filter") is not None for call in scan_calls)
    assert statistics
    assert sum(item.all_counts.get("rows_scanned", 0) for item in statistics) <= len(candidates)
    assert sum(item.all_counts.get("ranges_scanned", 0) for item in statistics) <= len(candidates)
