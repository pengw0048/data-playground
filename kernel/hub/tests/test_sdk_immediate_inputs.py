"""Public plugin builder context conformance for bounded immediate-input inspection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hub import db, metadb, workspace_providers
from hub.executors.engine import BuildEngine
from hub.models import Graph, GraphNode
from hub.nodespecs import NodeSpec, PortSpec
from hub.sdk import UnsupportedUpstreamError, ctx


class _Adapter:
    name = "fixture"

    def scan(self, _uri, **_kwargs):
        return db.conn().sql("SELECT 1 AS id, 4 AS signal")

    def open_revision(self, _uri, _revision_id):
        return db.conn().sql("SELECT 1 AS id, 4 AS signal")


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {"id": node_id, "type": kind, "position": {"x": 0, "y": 0},
            "data": {"config": config or {}}}


def _engine(deps, graph: Graph) -> BuildEngine:
    return BuildEngine(
        graph, lambda _uri: _Adapter(), deps.registry, full=True,
        node_builders=deps.node_builders, node_specs=deps.node_specs,
    )


def _fixture_deps(tmp_path):
    import shutil
    from pathlib import Path

    from hub import metadb
    from hub.deps import Deps

    source = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_sidecar_fixture"
    workspace = tmp_path / "workspace"
    (workspace / "plugins").mkdir(parents=True)
    shutil.copytree(source, workspace / "plugins" / "dp_sidecar_fixture")
    metadb.init_db()
    return Deps(str(workspace), str(tmp_path / "data"))


def test_sidecar_fixture_checks_direct_single_source_and_identity_before_its_build(tmp_path, monkeypatch):
    deps = _fixture_deps(tmp_path)
    exact = {"kind": "exact", "datasetId": "base-dataset", "revisionId": "r1"}
    monkeypatch.setattr(
        metadb, "catalog_revision_binding_for_uri", lambda _uri: {"dataset_id": "base-dataset"})
    config = {
        "identity": "id", "value": "signal", "output": "derived",
        "sourceDatasetId": "base-dataset",
    }
    sql_calls: list[str] = []
    real_sql = ctx.sql

    def record_sql(*args, **kwargs):
        sql_calls.append("sidecar")
        return real_sql(*args, **kwargs)

    monkeypatch.setattr(ctx, "sql", record_sql)

    correct = Graph(**{"id": "correct", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base", "datasetRef": exact}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [{"id": "edge", "source": "source", "target": "sidecar",
                    "data": {"wire": "dataset"}}]})
    with db.run_scope():
        assert _engine(deps, correct).relation("sidecar").fetchall() == [(1, 8.0)]
    assert sql_calls == ["sidecar"]
    sql_calls.clear()

    multi = correct.model_copy(deep=True)
    multi.nodes.append(GraphNode.model_validate(_node("other", "source", {
        "uri": "fixture://other", "datasetRef": exact,
    })))
    multi.edges.append(type(multi.edges[0]).model_validate({
        "id": "second", "source": "other", "target": "sidecar", "data": {"wire": "dataset"},
    }))
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="exactly one"):
        _engine(deps, multi).relation("sidecar")
    assert not sql_calls

    wrong_kind = Graph(**{"id": "wrong-kind", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base", "datasetRef": exact}),
        _node("filter", "filter", {"predicate": "id > 0"}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [
        {"id": "first", "source": "source", "target": "filter", "data": {"wire": "dataset"}},
        {"id": "second", "source": "filter", "target": "sidecar", "data": {"wire": "dataset"}},
    ]})
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="direct Source"):
        _engine(deps, wrong_kind).relation("sidecar")
    assert not sql_calls

    ambiguous = Graph(**{"id": "ambiguous", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://unbound"}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [{"id": "edge", "source": "source", "target": "sidecar",
                    "data": {"wire": "dataset"}}]})
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="proved dataset identity"):
        _engine(deps, ambiguous).relation("sidecar")
    assert not sql_calls


def test_immediate_inputs_reports_only_canonical_provider_binding(monkeypatch):
    provider_uri = "workspace-provider://encoded-binding"
    provider_identity = "workspace-provider:encoded-binding"
    source = GraphNode.model_validate(_node("source", "source", {
        "uri": provider_uri,
        "datasetRef": {"kind": "exact", "datasetId": provider_identity, "revisionId": "r1"},
    }))
    consumer = GraphNode.model_validate(_node("consumer", "consumer"))
    engine = SimpleNamespace(
        graph=Graph(**{"id": "snapshot", "version": 1, "nodes": [source, consumer], "edges": [
            {"id": "edge", "source": "source", "target": "consumer", "data": {"wire": "dataset"}},
        ]}),
        _nodes={"source": source, "consumer": consumer},
        node_specs={"consumer": NodeSpec(kind="consumer", title="consumer", category="compute",
                                           inputs=[PortSpec(id="in")])},
    )
    monkeypatch.setattr(workspace_providers, "provider_dataset_identity", lambda _uri: provider_identity)

    port = ctx.immediate_inputs(engine, consumer).port("in")
    assert port.count == 1
    assert port.inputs[0].kind == "source"
    assert port.inputs[0].dataset is not None
    assert port.inputs[0].dataset.dataset_id == provider_identity
    assert port.inputs[0].dataset.revision_id == "r1"

    source.data["config"]["datasetRef"]["datasetId"] = "placement-or-uri-is-not-an-identity"
    assert ctx.immediate_inputs(engine, consumer).port("in").inputs[0].dataset is None
