"""Execution-boundary coverage for durable lineage publication."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hub import metadb
from hub.models import Graph, GraphEdge, GraphNode, Position, RunOutput, RunStatus
from hub.plugins.catalog import lineage_for_output, record_cached_output_lineage
from hub.plugins.runner import LocalRunner, _catalog_publication_version
from hub.run_controller import RunController
from hub.subprocess_runner import SubprocessRunner


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _write_graph() -> Graph:
    return Graph(
        id="research-canvas",
        version=7,
        nodes=[
            GraphNode(
                id="source", type="source", position=Position(x=0, y=0),
                data={"config": {"uri": "/data/source.parquet"}},
            ),
            GraphNode(
                id="write", type="write", position=Position(x=1, y=0),
                data={"config": {"name": "derived"}},
            ),
        ],
        edges=[GraphEdge(id="edge", source="source", target="write")],
    )


def _pending_status(run_id: str) -> RunStatus:
    return RunStatus(
        run_id=run_id,
        status="running",
        target_node_id="write",
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )


@pytest.mark.parametrize(("publication", "expected"), [
    (SimpleNamespace(version="unmanaged-v1"), "unmanaged-v1"),
    ({"table": SimpleNamespace(version="managed-v2")}, "managed-v2"),
    ({"table": {"version": "managed-v3"}}, "managed-v3"),
    ({"table": SimpleNamespace(version=None)}, None),
])
def test_catalog_publication_version_uses_authoritative_table(publication, expected):
    assert _catalog_publication_version(publication) == expected


def test_local_catalog_cache_records_lineage_before_exposing_hit(tmp_path):
    events: list[str] = []

    class Catalog:
        @staticmethod
        def get_table(uri):
            events.append("readback")
            return SimpleNamespace(uri=uri, name="derived", version="v-exact")

        @staticmethod
        def record_lineage(**kwargs):
            events.append("record")
            assert kwargs["version"] == "v-exact"
            assert kwargs["parents"] == ["/data/source.parquet"]
            assert kwargs["lineage"].run_id == "run-cache"
            return 1

    runner = SimpleNamespace(
        workspace=str(tmp_path), catalog=Catalog(),
        forced_sink_targets=None,
        _output_exists=lambda _uri: True,
    )
    graph = _write_graph()
    status = _pending_status("run-cache")
    cached = {"outputs": [RunOutput(
        node_id="write", port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed",
        uri="/data/derived.parquet", table="derived", version="v-exact", rows=11,
    ).model_dump()]}

    class Cancel:
        @staticmethod
        def is_set():
            return False

    def pre_publish(*, check_cancel: bool) -> None:
        assert check_cancel is True
        events.append("fence")

    rows = LocalRunner._commit_write(
        runner, graph.nodes[1], graph, None, status, cached, Cancel(),
        pre_publish=pre_publish,
    )

    assert rows == 11
    assert events == ["readback", "fence", "record"]
    assert status.outputs[0].outcome == "committed"
    assert status.outputs[0].uri == "/data/derived.parquet"


def test_local_catalog_cache_lineage_failure_never_exposes_hit(tmp_path):
    events: list[str] = []

    class Catalog:
        @staticmethod
        def get_table(uri):
            events.append("readback")
            return SimpleNamespace(uri=uri, name="derived", version="v-exact")

        @staticmethod
        def record_lineage(**_kwargs):
            events.append("record")
            raise RuntimeError("catalog fact failed")

    runner = SimpleNamespace(
        workspace=str(tmp_path), catalog=Catalog(),
        forced_sink_targets=None,
        _output_exists=lambda _uri: True,
    )
    graph = _write_graph()
    status = _pending_status("run-cache-failure")
    cached = {"outputs": [RunOutput(
        node_id="write", port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed",
        uri="/data/derived.parquet", table="derived", version="v-exact", rows=11,
    ).model_dump()]}

    class Cancel:
        @staticmethod
        def is_set():
            return False

    def pre_publish(*, check_cancel: bool) -> None:
        assert check_cancel is True
        events.append("fence")

    with pytest.raises(RuntimeError, match="catalog fact failed"):
        LocalRunner._commit_write(
            runner, graph.nodes[1], graph, None, status, cached, Cancel(),
            pre_publish=pre_publish,
        )

    assert events == ["readback", "fence", "record"]
    assert status.outputs[0].outcome == "pending"
    assert status.outputs[0].uri is None


def test_local_rejects_invalid_lineage_parent_before_building_or_writing(tmp_path):
    graph = _write_graph()
    graph.nodes[0].data["config"]["uri"] = "s" * 8_193
    runner = SimpleNamespace(forced_sink_targets=None)

    class Engine:
        @staticmethod
        def relation(*_args):
            pytest.fail("invalid lineage reached relation construction")

    class Cancel:
        @staticmethod
        def is_set():
            return False

    with pytest.raises(ValueError, match="exceeds 8192 characters"):
        LocalRunner._commit_write(
            runner, graph.nodes[1], graph, Engine(),
            _pending_status("run-invalid-parent"), None, Cancel(),
        )


@pytest.mark.parametrize("receipt", [None, True, -1, "1"])
def test_cached_lineage_requires_a_durable_recorder_receipt(receipt):
    class Catalog:
        @staticmethod
        def get_table(uri):
            return SimpleNamespace(uri=uri, name="derived", version="v-exact")

        @staticmethod
        def record_lineage(**_kwargs):
            return receipt

    with pytest.raises(RuntimeError, match="invalid durable receipt"):
        record_cached_output_lineage(
            Catalog(), name="derived", uri="/data/derived.parquet", version="v-exact",
            parents=["/data/source.parquet"],
            lineage=lineage_for_output(_write_graph(), "run-cache", "write"),
        )


@pytest.mark.parametrize(("observed_uri", "observed_version"), [
    ("/data/new-generation.parquet", "v-new"),
    ("/data/old-generation.parquet", "v-new"),
])
def test_local_catalog_cache_rejects_stale_current_pointer_or_version(
        tmp_path, observed_uri, observed_version):
    class Catalog:
        @staticmethod
        def get_table(_uri):
            return SimpleNamespace(
                uri=observed_uri, name="derived", version=observed_version)

        @staticmethod
        def record_lineage(**_kwargs):  # pragma: no cover - a stale pointer cannot reach the recorder
            raise AssertionError("stale cache lineage was recorded")

    runner = SimpleNamespace(
        workspace=str(tmp_path), catalog=Catalog(),
        forced_sink_targets=None,
        _output_exists=lambda _uri: True,
    )
    graph = Graph(
        id="research-canvas", version=7,
        nodes=[GraphNode(
            id="write", type="write", position=Position(x=0, y=0),
            data={"config": {"name": "derived"}},
        )],
        edges=[],
    )
    status = _pending_status("run-stale")
    cached = {"outputs": [RunOutput(
        node_id="write", port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed",
        uri="/data/old-generation.parquet", table="derived", version="v-old", rows=11,
    ).model_dump()]}

    class Cancel:
        @staticmethod
        def is_set():
            return False

    rows = LocalRunner._commit_write(
        runner, graph.nodes[0], graph, None, status, cached, Cancel(),
        pre_publish=lambda **_kwargs: pytest.fail("stale cache crossed publication fence"),
    )

    assert rows == 0
    assert status.outputs[0].outcome == "pending"
    assert status.outputs[0].uri is None


def test_final_region_keeps_outer_run_and_original_producer(monkeypatch):
    graph = _write_graph()
    region = SimpleNamespace(
        id="final", node_ids={"source", "write"}, output_node="write", backend="local",
    )
    controller = object.__new__(RunController)
    controller.deps = SimpleNamespace(registry={}, node_specs={}, node_ir={})
    observed = {}

    class Backend:
        def run(self, _plan, subgraph, _target, _placement):
            observed["lineage"] = lineage_for_output(
                subgraph, "backend-subrun", "write")
            return RunStatus(run_id="backend-subrun", status="running")

    backend = Backend()
    controller._backend_runner = lambda _region, _uid: backend
    controller._track_sub = lambda *_args: None
    controller._untrack_sub = lambda *_args: None
    controller._await = lambda _backend, _run, *, cancel_run: RunStatus(
        run_id="backend-subrun", status="done")
    monkeypatch.setattr("hub.run_controller.compiler.compile_plan", lambda *_args: object())

    result = controller._run_final("outer-run", graph, region, {})

    assert result.run_id == "backend-subrun"
    publication = observed["lineage"]
    assert publication.run_id == "outer-run"
    assert publication.attempt_id == "backend-subrun"
    assert publication.producer == "research-canvas"
    assert publication.producer_version == 7


def test_region_preserves_explicit_zero_producer_version():
    graph = _write_graph()
    graph._publication_producer_id = "version-zero-canvas"
    graph._publication_producer_version = 0
    region = SimpleNamespace(node_ids={"source", "write"})

    subgraph = object.__new__(RunController)._subgraph(graph, region, {})

    assert subgraph._publication_producer_id == "version-zero-canvas"
    assert subgraph._publication_producer_version == 0


def test_subprocess_parent_lineage_sidecar_never_enters_child_job(tmp_path, monkeypatch):
    from hub import compiler

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            return None

        @staticmethod
        def get_table(_uri):
            return None

    runner = SubprocessRunner(
        str(tmp_path / "workspace"), str(tmp_path / "data"), catalog=Catalog())
    graph = _write_graph()
    graph._publication_run_id = "outer-run"
    captured = {}

    def reject_spawn(_run_id, argv, **_kwargs):
        with open(argv[-1]) as stream:
            captured["job"] = json.load(stream)
        captured["sidecar"] = runner._sink_contracts["backend-subrun"]["write"]
        raise RuntimeError("stop before child launch")

    monkeypatch.setattr(runner, "_spawn_process", reject_spawn)
    with pytest.raises(RuntimeError, match="stop before child launch"):
        runner.run(
            compiler.compile_plan(graph, "write"), graph, "write", "local",
            run_id="backend-subrun",
        )

    job = captured["job"]
    publication = captured["sidecar"]["lineage"]
    assert publication.run_id == "outer-run"
    assert publication.attempt_id == "backend-subrun"
    assert "outer-run" not in json.dumps(job, sort_keys=True)
    assert "lineage" not in json.dumps(job["graph"], sort_keys=True).lower()
    assert set(job).isdisjoint({"lineage", "publicationContext", "publication_context"})
    assert job["runId"] == "backend-subrun"


def test_subprocess_claim_freezes_outer_lineage_in_parent_contract(tmp_path):
    from hub import compiler

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            return None

        @staticmethod
        def get_table(_uri):
            return None

    runner = SubprocessRunner(
        str(tmp_path / "workspace"), str(tmp_path / "data"), catalog=Catalog())
    graph = _write_graph()
    graph._publication_run_id = "outer-run"
    graph._publication_producer_id = "original-canvas"
    graph._publication_producer_version = 12
    plan = compiler.compile_plan(graph, "write")

    _targets, attempts, contracts = runner._claim_sink_contracts(
        plan, graph, "backend-subrun", _pending_status("backend-subrun"))

    assert attempts == {}
    publication = contracts["write"]["lineage"]
    assert publication.run_id == "outer-run"
    assert publication.attempt_id == "backend-subrun"
    assert publication.producer == "original-canvas"
    assert publication.producer_version == 12


def test_subprocess_rejects_invalid_lineage_parent_before_sink_claim(tmp_path):
    from hub import compiler

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            return None

        @staticmethod
        def get_table(_uri):
            return None

    runner = SubprocessRunner(
        str(tmp_path / "workspace"), str(tmp_path / "data"), catalog=Catalog())
    graph = _write_graph()
    graph.nodes[0].data["config"]["uri"] = "s" * 8_193

    with pytest.raises(ValueError, match="exceeds 8192 characters"):
        runner._claim_sink_contracts(
            compiler.compile_plan(graph, "write"), graph, "backend-invalid-parent",
            _pending_status("backend-invalid-parent"),
        )
