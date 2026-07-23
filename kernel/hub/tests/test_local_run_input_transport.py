"""Exact admitted-input transport contracts for the built-in local execution paths."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pyarrow as pa
import pytest

from hub import db, metadb
from hub.executors import engine as engine_mod
from hub.executors.engine import BuildEngine, NotPreviewable
from hub.executors.preview import PREVIEW_SCAN
from hub.kernel import RunBody, _admitted_kernel_graph
from hub.kernel_backend import KernelBackend
from hub.local_run_inputs import LocalRunInputError, bind_manifest, validate_manifest
from hub.models import CompilePlan, Graph, GraphNode, RunStatus
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import runs
from hub.subprocess_runner import SubprocessRunner
from hub.subrun import _validate_admitted_input_manifest


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, session, url = metadb._engine, metadb._Session, settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'transport.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine, metadb._Session = engine, session


def _source_graph(uri: str, *, canvas_id: str = "transport-canvas") -> Graph:
    return Graph(
        id=canvas_id,
        nodes=[GraphNode(
            id="source", type="source",
            data={"config": {"uri": uri}},
        )],
        edges=[],
    )


def _admit(graph: Graph, deps) -> tuple[str, list[dict[str, str]], Graph]:
    with metadb.session() as session:
        session.add(metadb.Canvas(id=graph.id, owner_id="local", name="transport"))
    manifest = runs._resolve_local_run_manifest(graph, "source", deps)
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=graph.id, submission_id=str(uuid.uuid4()),
        target_node_id="source", intent_sha256=runs._local_run_intent_sha256(graph, "source"),
        manifest=manifest,
    )
    assert created is True
    return run_id, manifest, bind_manifest(graph, "source", manifest, deps.resolve_adapter)


def test_kernel_transport_reopens_the_admitted_lance_revision_after_head_move_and_db_restart(
        tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "input.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())._add(
        name="input", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _source_graph(uri)
    run_id, manifest, dispatch_graph = _admit(graph, deps)

    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    body = RunBody(
        run_id=run_id, graph=dispatch_graph.model_dump(), target="source",
        input_manifest=manifest,
    )

    for restart in (False, True):
        if restart:
            assert metadb._engine is not None
            metadb._engine.dispose()
            metadb._engine = metadb._Session = None
            metadb.init_db()
        reopened, carried = _admitted_kernel_graph(
            body, kernel_canvas=graph.id, deps=deps, metadata=metadb)
        assert carried == manifest
        config = reopened.nodes[0].data["config"]
        assert config["_input_dataset_id"] == manifest[0]["dataset_id"]
        assert config["_input_provider"] == manifest[0]["provider"]
        assert config["_input_revision_id"] == manifest[0]["revision_id"]
        with db.run_scope():
            assert LanceAdapter().open_revision(
                config["uri"], config["_input_revision_id"]).fetchall() == [(1,)]

    os.replace(uri, f"{uri}.unavailable")
    with pytest.raises(LocalRunInputError, match="revision is unavailable"):
        _admitted_kernel_graph(
            body, kernel_canvas=graph.id, deps=deps, metadata=metadb)


def test_manifest_binding_replaces_untrusted_preview_limit_and_keeps_full_run_exact(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "input.lance")
    lance.write_dataset(pa.table({"value": [1, 2, 3]}), uri)

    class SpyLanceAdapter(LanceAdapter):
        def __init__(self):
            self.open_calls: list[str] = []
            self.preview_calls: list[tuple[str, int]] = []

        def open_revision(self, uri: str, revision_id: str):
            self.open_calls.append(revision_id)
            return super().open_revision(uri, revision_id)

        def preview_revision(self, uri: str, revision_id: str, *, limit: int):
            self.preview_calls.append((revision_id, limit))
            return super().preview_revision(uri, revision_id, limit=limit)

    adapter = SpyLanceAdapter()
    InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)._add(
        name="input", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter)
    graph = _source_graph(uri)
    graph.nodes[0].data["config"]["_input_preview_limit"] = 1
    manifest = runs._resolve_local_run_manifest(graph, "source", deps)

    full_graph = bind_manifest(graph, "source", manifest, deps.resolve_adapter)
    assert "_input_preview_limit" not in full_graph.nodes[0].data["config"]
    with db.run_scope():
        assert BuildEngine(
            full_graph, deps.resolve_adapter, {}, full=True,
        ).relation("source").fetchall() == [(1,), (2,), (3,)]
    assert adapter.open_calls == [manifest[0]["revision_id"]] * 2
    assert adapter.preview_calls == []

    preview_graph = bind_manifest(
        graph, "source", manifest, deps.resolve_adapter, preview_limit=PREVIEW_SCAN)
    assert preview_graph.nodes[0].data["config"]["_input_preview_limit"] == PREVIEW_SCAN
    with db.run_scope():
        assert BuildEngine(
            preview_graph, deps.resolve_adapter, {}, sample_k=PREVIEW_SCAN, full=False,
        ).relation("source").fetchall() == [(1,), (2,), (3,)]
    assert adapter.preview_calls == [
        (manifest[0]["revision_id"], PREVIEW_SCAN),
        (manifest[0]["revision_id"], PREVIEW_SCAN),
    ]


def test_manifest_binding_rebuilds_dispatch_fields_before_opening_source(tmp_path):
    lance = pytest.importorskip("lance")
    admitted_uri = str(tmp_path / "admitted.lance")
    stale_uri = str(tmp_path / "stale.lance")
    lance.write_dataset(pa.table({"value": [1]}), admitted_uri)
    lance.write_dataset(pa.table({"value": [2]}), stale_uri)
    adapter = LanceAdapter()
    InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)._add(
        name="admitted", uri=admitted_uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter)
    graph = _source_graph(admitted_uri)
    config = graph.nodes[0].data["config"]
    config.update({
        "_input_artifact_uri": stale_uri,
        "_input_dataset_id": "stale-dataset",
        "_input_preview_limit": 1,
        "_input_provider": "stale-provider",
        "_input_provider_preview_uri": stale_uri,
        "_input_provider_uri": stale_uri,
        "_input_revision_id": "999",
    })
    manifest = runs._resolve_local_run_manifest(graph, "source", deps)

    bound = bind_manifest(graph, "source", manifest, deps.resolve_adapter)
    bound_config = bound.nodes[0].data["config"]

    assert graph.nodes[0].data["config"]["_input_provider_uri"] == stale_uri
    assert "_input_provider_uri" not in bound_config
    assert "_input_provider_preview_uri" not in bound_config
    assert "_input_preview_limit" not in bound_config
    assert "_input_artifact_uri" not in bound_config
    assert bound_config["_input_dataset_id"] == manifest[0]["dataset_id"]
    assert bound_config["_input_provider"] == manifest[0]["provider"]
    assert bound_config["_input_revision_id"] == manifest[0]["revision_id"]
    with db.run_scope():
        assert BuildEngine(
            bound, deps.resolve_adapter, {}, full=True,
        ).relation("source").fetchall() == [(1,)]


def test_kernel_rebinding_preserves_only_a_matching_prebound_provider_uri(tmp_path):
    lance = pytest.importorskip("lance")
    physical_uri = str(tmp_path / "provider.lance")
    lance.write_dataset(pa.table({"value": [7]}), physical_uri)
    adapter = LanceAdapter()
    binding_id = "a" * 32
    graph = _source_graph(f"workspace-provider://{binding_id}")
    revision_id = str(lance.dataset(physical_uri).version)
    manifest = [{
        "node_id": "source",
        "dataset_id": f"workspace-provider:{binding_id}",
        "revision_id": revision_id,
        "provider": adapter.name,
        "resolved_at": "2026-07-23T00:00:00Z",
    }]
    graph.nodes[0].data["config"].update({
        "_input_provider_uri": physical_uri,
        "_input_dataset_id": manifest[0]["dataset_id"],
        "_input_provider": manifest[0]["provider"],
        "_input_revision_id": revision_id,
        "_input_preview_limit": 1,
    })

    rebound = bind_manifest(
        graph,
        "source",
        manifest,
        lambda _uri: adapter,
        allow_prebound_provider=True,
    )
    config = rebound.nodes[0].data["config"]

    assert config["_input_provider_uri"] == physical_uri
    assert config["_input_dataset_id"] == manifest[0]["dataset_id"]
    assert config["_input_provider"] == manifest[0]["provider"]
    assert config["_input_revision_id"] == revision_id
    assert "_input_preview_limit" not in config
    with db.run_scope():
        assert BuildEngine(
            rebound, lambda _uri: adapter, {}, full=True,
        ).relation("source").fetchall() == [(7,)]


def test_kernel_transport_rejects_stale_and_secret_bearing_manifests():
    valid = [{
        "node_id": "source", "dataset_id": "dataset", "revision_id": "1",
        "provider": "lance", "resolved_at": "now",
    }]
    with pytest.raises(LocalRunInputError, match="malformed"):
        validate_manifest([{**valid[0], "secret": "must-not-cross"}])

    graph = _source_graph("/tmp/not-opened.lance")
    body = RunBody(
        run_id="run-stale", graph=graph.model_dump(), target="source",
        input_manifest=[{**valid[0], "revision_id": "2"}],
    )
    metadata = SimpleNamespace(local_run_input_admission=lambda _run_id: {
        "run_id": "run-stale", "canvas_id": graph.id, "target_node_id": "source",
        "manifest": valid,
    })
    with pytest.raises(LocalRunInputError, match="does not match"):
        _admitted_kernel_graph(
            body, kernel_canvas=graph.id,
            deps=SimpleNamespace(resolve_adapter=lambda _uri: None), metadata=metadata)


def test_kernel_backend_does_not_spawn_before_a_matching_admission(monkeypatch):
    backend = KernelBackend(SimpleNamespace(node_specs={}, workspace="/tmp"), SimpleNamespace())
    spawned: list[str] = []
    monkeypatch.setattr(backend, "_ensure_kernel", lambda canvas: spawned.append(canvas))

    with pytest.raises(RuntimeError, match="persisted local input admission"):
        backend.run(
            CompilePlan(target_node_id=None, steps=[]), Graph(id="missing", nodes=[], edges=[]),
            None, "local", run_id="run-missing", input_manifest=[],
        )
    assert spawned == []


def test_isolated_local_job_carries_and_revalidates_manifest_identity(tmp_path, monkeypatch):
    reservations: list[str] = []
    storage = SimpleNamespace(
        begin_result=lambda key, _run_id: reservations.append(key) or str(
            tmp_path / f"result-{len(reservations)}.parquet"),
        result_lock_fd=lambda *_args: None,
        _read_lock_token=lambda *_args: None,
        result_namespace_identity=lambda: (1, 2),
        namespace_id="transport-test",
        abort_result=lambda *_args: None,
    )
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), storage=storage)
    runner.on_status = lambda *_args: None
    graph = Graph(
        id="isolated", nodes=[GraphNode(id="check", type="assert", data={"config": {}})],
        edges=[],
    )
    calls: list[dict] = []
    monkeypatch.setattr(runner, "_claim_source_leases", lambda *_args: {
        "stack": SimpleNamespace(close=lambda: None), "guards": [],
        "attempts": {}, "local_sources": {},
    })
    monkeypatch.setattr(runner, "_claim_sink_contracts", lambda *_args: ({}, {}, {}))

    def capture(status, job_extra, submitted_graph, target):
        calls.append(job_extra)
        _validate_admitted_input_manifest({
            **job_extra, "graph": submitted_graph.model_dump(), "target": target,
        }, submitted_graph)
        return status

    monkeypatch.setattr(runner, "_spawn", capture)
    status = runner.run(
        CompilePlan(target_node_id="check", steps=[]), graph, "check", "local",
        run_id="run-isolated", input_manifest=[],
    )
    assert isinstance(status, RunStatus)
    assert reservations == [
        "run-isolated:check:pass:0", "run-isolated:check:out:1"]
    assert [result["portId"] for result in calls[0]["forcedResults"]] == ["pass", "out"]
    assert calls == [{
        "runId": "run-isolated",
        "inputManifest": [],
        "inputManifestIdentity": {
            "runId": "run-isolated", "canvasId": "isolated", "targetNodeId": "check",
        },
        "managedSourceAttempts": {}, "managedLocalSources": {},
        "sinkTargets": {}, "sinkAttempts": {},
        "forcedResults": [
            {"nodeId": "check", "portId": "pass",
             "uri": str(tmp_path / "result-1.parquet"),
             "lockFd": None, "lockToken": None},
            {"nodeId": "check", "portId": "out",
             "uri": str(tmp_path / "result-2.parquet"),
             "lockFd": None, "lockToken": None},
        ],
        "resultNamespaceId": "transport-test",
        "resultNamespaceIdentity": [1, 2],
    }]

    stale = {**calls[0], "inputManifestIdentity": {
        **calls[0]["inputManifestIdentity"], "runId": "run-other",
    }, "target": "check"}
    with pytest.raises(RuntimeError, match="identity is invalid"):
        _validate_admitted_input_manifest(stale, graph)


def test_metadata_isolated_engine_reads_only_the_parent_attested_exact_artifact(
        tmp_path, monkeypatch):
    artifact = str(tmp_path / "exact.parquet")
    DuckDBAdapter().write(artifact, db.conn().from_arrow(pa.table({"value": [1]})))
    graph = _source_graph(artifact)
    config = graph.nodes[0].data["config"]
    config["_input_revision_id"] = "content-revision"
    config["_input_artifact_uri"] = artifact
    revision_lookups: list[str] = []
    original_lookup = engine_mod.revision_adapter_for_uri

    def traced_lookup(uri, resolve_adapter):
        revision_lookups.append(uri)
        return original_lookup(uri, resolve_adapter)

    monkeypatch.setattr(engine_mod, "revision_adapter_for_uri", traced_lookup)

    with db.run_scope():
        with pytest.raises(NotPreviewable, match="persisted input revision is unavailable"):
            BuildEngine(
                graph, lambda _uri: DuckDBAdapter(), {}, full=True,
            ).relation("source")
        assert revision_lookups == [artifact]

        graph._input_artifact_uris["source"] = artifact
        assert BuildEngine(
            graph, lambda _uri: DuckDBAdapter(), {}, full=True,
        ).relation("source").fetchall() == [(1,)]
        assert revision_lookups == [artifact]


def test_isolated_local_rejects_malformed_manifest_before_claim_or_spawn(tmp_path, monkeypatch):
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), storage=SimpleNamespace())
    graph = Graph(
        id="isolated", nodes=[GraphNode(id="check", type="assert", data={"config": {}})],
        edges=[],
    )
    calls: list[str] = []
    monkeypatch.setattr(runner, "_claim_source_leases", lambda *_args: calls.append("claim"))
    monkeypatch.setattr(runner, "_spawn", lambda *_args: calls.append("spawn"))
    with pytest.raises(LocalRunInputError, match="malformed"):
        runner.run(
            CompilePlan(target_node_id="check", steps=[]), graph, "check", "local",
            run_id="run-isolated",
            input_manifest=[{
                "node_id": "source", "dataset_id": "dataset", "revision_id": "1",
                "provider": "lance", "resolved_at": "now", "secret": "nope",
            }],
        )
    assert calls == []
