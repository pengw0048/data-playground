"""Local full-run admission binds Sources to one immutable provider revision."""

from __future__ import annotations

import threading
import time
import uuid
import json
import os
from types import SimpleNamespace

import pyarrow as pa
import pytest
from sqlalchemy import event, func, select

from hub import db, metadb
from hub.api_errors import APIError, APIErrorCode
from hub.executors.engine import BuildEngine
from hub.models import Graph, ParameterBinding, ParameterDeclaration, RunEstimate, RunStatus
from hub.local_run_inputs import (
    LocalRunInputError, finalize_local_file_candidates, snapshot_local_file_input,
)
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import runs
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, session, url = metadb._engine, metadb._Session, settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'admission.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine, metadb._Session = engine, session


def _graph(uri: str, *, canvas_id: str = "local-admission") -> Graph:
    return Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }], "edges": [],
    })


def _write_ordinary_source(path, values: list[int]) -> None:
    suffix = path.suffix.lower()
    table = pa.table({"value": values})
    if suffix == ".parquet":
        import pyarrow.parquet as pq
        pq.write_table(table, path)
    elif suffix == ".csv":
        import pyarrow.csv as pacsv
        pacsv.write_csv(table, path)
    elif suffix == ".json":
        path.write_text("\n".join(
            json.dumps({"value": value}) for value in values) + "\n")
    elif suffix == ".arrow":
        import pyarrow.ipc as ipc
        with ipc.new_file(path, table.schema) as writer:
            writer.write_table(table)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(suffix)


def _admit_ordinary_source(*, source, canvas_id: str, storage, adapter, intent: str):
    graph = _graph(str(source), canvas_id=canvas_id)
    with metadb.session() as session:
        session.add(metadb.Canvas(id=canvas_id, owner_id="local", name=canvas_id))
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter, storage=storage)
    candidates: list[dict[str, str]] = []
    manifest = runs._resolve_local_run_manifest(
        graph, "source", deps, materialize_local_files=True,
        local_file_candidates=candidates)
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=canvas_id, submission_id=str(uuid.uuid4()),
        target_node_id="source", intent_sha256=intent, manifest=manifest,
        local_file_candidates=candidates)
    assert created is True
    finalize_local_file_candidates(storage, candidates, run_id)
    artifact = metadb.local_file_input_revision_artifact(
        manifest[0]["dataset_id"], manifest[0]["revision_id"])
    assert artifact is not None
    return graph, deps, manifest, run_id, artifact


def test_exact_admission_retries_a_transient_engine_error(tmp_path):
    """A transient scan/write failure under concurrent admission recovers on retry (the #523 area)."""
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "ordinary.parquet"
    _write_ordinary_source(source, [1, 2, 3])

    real_scan = adapter.scan_local_snapshot
    calls = {"n": 0}

    def flaky_scan(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient engine collision")
        return real_scan(*args, **kwargs)

    adapter.scan_local_snapshot = flaky_scan  # type: ignore[method-assign]
    revision_id, candidate = snapshot_local_file_input(
        uri=str(source), config={"uri": str(source)},
        dataset_id="ds-flaky", adapter=adapter, storage=storage)
    assert calls["n"] == 2 and candidate is not None and revision_id


def test_exact_admission_surfaces_a_persistent_engine_error_as_a_typed_contract_failure(tmp_path):
    """A persistent engine failure exhausts the retry and still fails typed, never a raw 500."""
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "ordinary.parquet"
    _write_ordinary_source(source, [1, 2, 3])

    def always_fail(*args, **kwargs):
        raise RuntimeError("persistent engine failure")

    adapter.scan_local_snapshot = always_fail  # type: ignore[method-assign]
    with pytest.raises(LocalRunInputError, match="could not be parsed into an immutable exact binding"):
        snapshot_local_file_input(
            uri=str(source), config={"uri": str(source)},
            dataset_id="ds-persist", adapter=adapter, storage=storage)


def test_ordinary_local_formats_keep_exact_rows_across_rename_restart_retry_and_cleanup(tmp_path):
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    nodes = []
    original_paths = []
    for index, suffix in enumerate((".parquet", ".csv", ".json", ".arrow")):
        source = tmp_path / f"ordinary-{index}{suffix}"
        _write_ordinary_source(source, [1, 2])
        catalog._add(name=f"ordinary-{index}", uri=str(source), strict_probe=True)
        nodes.append({
            "id": f"source-{index}", "type": "source", "position": {"x": 0, "y": index},
            "data": {"config": {"uri": str(source)}},
        })
        original_paths.append(source)
    nodes.append({
        "id": "source-repeat", "type": "source", "position": {"x": 0, "y": 4},
        "data": {"config": {"uri": str(original_paths[0])}},
    })
    graph = Graph.model_validate({
        "id": "ordinary-local-formats", "version": 1, "nodes": nodes, "edges": [],
    })
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=graph.id, owner_id="local", name="ordinary local formats"))
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter, storage=storage)

    candidates: list[dict[str, str]] = []
    manifest = runs._resolve_local_run_manifest(
        graph, None, deps, materialize_local_files=True,
        local_file_candidates=candidates)
    assert [item["node_id"] for item in manifest] == [node["id"] for node in nodes]
    assert {item["provider"] for item in manifest} == {"local-file-snapshot"}
    submission_id = str(uuid.uuid4())
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=graph.id, submission_id=submission_id,
        target_node_id=None, intent_sha256="e" * 64, manifest=manifest,
        local_file_candidates=candidates)
    assert created is True
    finalize_local_file_candidates(storage, candidates, run_id)

    retry_candidates: list[dict[str, str]] = []
    retry_manifest = runs._resolve_local_run_manifest(
        graph, None, deps, materialize_local_files=True,
        local_file_candidates=retry_candidates)
    assert retry_candidates == []
    assert [(item["node_id"], item["dataset_id"], item["revision_id"])
            for item in retry_manifest] == [
                (item["node_id"], item["dataset_id"], item["revision_id"])
                for item in manifest]
    adopted_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=graph.id, submission_id=submission_id,
        target_node_id=None, intent_sha256="e" * 64, manifest=retry_manifest)
    assert (adopted_id, created) == (run_id, False)
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalFileInputRevision)) == 4
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalResultArtifact)) == 4

    for source in original_paths:
        renamed = source.with_name(f"renamed-{source.name}")
        os.replace(source, renamed)
        _write_ordinary_source(renamed, [99])
    restarted_storage = LocalStorage(str(tmp_path / "outputs"))
    bound = runs._bind_local_run_manifest(graph, manifest, deps, None)
    with db.run_scope():
        exact_engine = BuildEngine(bound, deps.resolve_adapter, {}, full=True)
        assert [exact_engine.relation(node.id).fetchall()
                for node in bound.nodes] == [[(1,), (2,)]] * 5
    for artifact in bound._input_artifact_uris.values():
        with restarted_storage.acquire_result_read(artifact, "restart-test") as guard:
            guard.check()

    metadb.delete_canvas_cascade(graph.id)
    restarted_storage.prune_results(limit=50)
    for item in manifest:
        assert metadb.local_file_input_revision_artifact(
            item["dataset_id"], item["revision_id"]) is None


def test_ordinary_local_binding_can_be_recreated_after_cleanup(tmp_path):
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "recreated.parquet"
    _write_ordinary_source(source, [3, 4])
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    catalog._add(name="recreated", uri=str(source), strict_probe=True)

    _graph1, _deps1, manifest1, _run1, artifact1 = _admit_ordinary_source(
        source=source, canvas_id="cleanup-first", storage=storage,
        adapter=adapter, intent="1" * 64)
    metadb.delete_canvas_cascade("cleanup-first")
    storage.prune_results(limit=10)
    assert not os.path.exists(artifact1)
    assert metadb.local_file_input_revision_artifact(
        manifest1[0]["dataset_id"], manifest1[0]["revision_id"]) is None

    graph2, deps2, manifest2, _run2, artifact2 = _admit_ordinary_source(
        source=source, canvas_id="cleanup-second", storage=storage,
        adapter=adapter, intent="2" * 64)
    assert manifest2[0]["revision_id"] == manifest1[0]["revision_id"]
    assert artifact2 != artifact1
    bound = runs._bind_local_run_manifest(graph2, manifest2, deps2, "source")
    _write_ordinary_source(source, [99])
    with db.run_scope():
        assert BuildEngine(bound, deps2.resolve_adapter, {}, full=True).relation(
            "source").fetchall() == [(3,), (4,)]


def test_readmission_replaces_a_mapping_while_its_old_artifact_is_deleting(tmp_path):
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "deleting-window.parquet"
    _write_ordinary_source(source, [8, 9])
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    catalog._add(name="deleting-window", uri=str(source), strict_probe=True)

    _graph1, _deps1, manifest1, _run1, artifact1 = _admit_ordinary_source(
        source=source, canvas_id="deleting-first", storage=storage,
        adapter=adapter, intent="3" * 64)
    metadb.delete_canvas_cascade("deleting-first")
    claims = metadb.claim_local_result_reclaims(storage.namespace_id, limit=10)
    old_claim = next(claim for claim in claims if claim[0] == artifact1)
    with metadb.session() as session:
        old_mapping = session.get(metadb.LocalFileInputRevision, {
            "dataset_id": manifest1[0]["dataset_id"],
            "revision_id": manifest1[0]["revision_id"],
        })
        old_artifact = session.get(metadb.LocalResultArtifact, artifact1)
        assert old_mapping is not None and old_mapping.artifact_uri == artifact1
        assert old_artifact is not None and old_artifact.state == "deleting"
    assert metadb.local_file_input_revision_artifact(
        manifest1[0]["dataset_id"], manifest1[0]["revision_id"]) is None

    graph2, deps2, manifest2, _run2, artifact2 = _admit_ordinary_source(
        source=source, canvas_id="deleting-second", storage=storage,
        adapter=adapter, intent="4" * 64)
    assert manifest2[0]["revision_id"] == manifest1[0]["revision_id"]
    assert artifact2 != artifact1
    storage._delete_claimed_result(
        old_claim[0], old_claim[1], lock_token=old_claim[2])
    assert metadb.local_file_input_revision_artifact(
        manifest2[0]["dataset_id"], manifest2[0]["revision_id"]) == artifact2
    bound = runs._bind_local_run_manifest(graph2, manifest2, deps2, "source")
    _write_ordinary_source(source, [99])
    with db.run_scope():
        assert BuildEngine(bound, deps2.resolve_adapter, {}, full=True).relation(
            "source").fetchall() == [(8,), (9,)]


def test_start_run_retries_when_reclaim_wins_after_ready_snapshot_lookup(tmp_path, monkeypatch):
    resolve_manifest = runs._resolve_local_run_manifest
    bind_manifest = runs._bind_local_run_manifest
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "lookup-reclaim-race.parquet"
    _write_ordinary_source(source, [5, 6])
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    catalog._add(name="lookup-reclaim-race", uri=str(source), strict_probe=True)
    _graph1, _deps1, manifest1, _run1, artifact1 = _admit_ordinary_source(
        source=source, canvas_id="lookup-reclaim-first", storage=storage,
        adapter=adapter, intent="5" * 64)
    metadb.delete_canvas_cascade("lookup-reclaim-first")

    deps, graph = _local_start_context(monkeypatch)
    deps.catalog = catalog
    deps.resolve_adapter = lambda _uri: adapter
    deps.storage = storage
    graph = _graph(str(source))
    monkeypatch.setattr(runs, "_resolve_local_run_manifest", resolve_manifest)
    monkeypatch.setattr(runs, "_bind_local_run_manifest", bind_manifest)
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        RunStatus(run_id=run_id, status="queued"),
    )

    ready_observed = threading.Event()
    reclaim_finished = threading.Event()
    claims: list[tuple[str, str, str]] = []
    reclaim_errors: list[BaseException] = []
    lookup = metadb.local_file_input_revision_artifact

    def lookup_with_barrier(dataset_id: str, revision_id: str) -> str | None:
        artifact = lookup(dataset_id, revision_id)
        if artifact == artifact1 and not ready_observed.is_set():
            ready_observed.set()
            assert reclaim_finished.wait(timeout=5)
        return artifact

    def reclaim_after_lookup() -> None:
        try:
            assert ready_observed.wait(timeout=5)
            claims.extend(metadb.claim_local_result_reclaims(storage.namespace_id, limit=10))
        except BaseException as exc:
            reclaim_errors.append(exc)
        finally:
            reclaim_finished.set()

    monkeypatch.setattr(metadb, "local_file_input_revision_artifact", lookup_with_barrier)
    reclaimer = threading.Thread(target=reclaim_after_lookup)
    reclaimer.start()
    status, _owner = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=str(uuid.uuid4()))
    reclaimer.join(timeout=5)

    assert not reclaimer.is_alive()
    assert reclaim_errors == []
    old_claim = next(claim for claim in claims if claim[0] == artifact1)
    admitted = metadb.local_run_input_manifest(status.run_id)
    assert admitted is not None
    replacement = lookup(
        admitted[0]["dataset_id"], admitted[0]["revision_id"])
    assert replacement is not None and replacement != artifact1
    storage._delete_claimed_result(old_claim[0], old_claim[1], lock_token=old_claim[2])
    assert lookup(admitted[0]["dataset_id"], admitted[0]["revision_id"]) == replacement


def test_local_file_mutation_during_snapshot_fails_before_admission(tmp_path, monkeypatch):
    from hub import local_run_inputs

    source = tmp_path / "moving.csv"
    source.write_text("value\n" + "1\n" * (1024 * 1024))
    source_identity = os.stat(source)
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    catalog._add(name="moving", uri=str(source), strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter, storage=storage)
    original_read = os.read
    changed = False

    def mutate_after_first_source_chunk(fd: int, size: int) -> bytes:
        nonlocal changed
        data = original_read(fd, size)
        current = os.fstat(fd)
        if (data and not changed
                and (current.st_dev, current.st_ino) == (
                    source_identity.st_dev, source_identity.st_ino)):
            changed = True
            with source.open("ab") as stream:
                stream.write(b"2\n")
        return data

    monkeypatch.setattr(local_run_inputs.os, "read", mutate_after_first_source_chunk)
    with pytest.raises(APIError) as exc:
        runs._resolve_local_run_manifest(
            _graph(str(source)), "source", deps,
            materialize_local_files=True, local_file_candidates=[])
    assert exc.value.status_code == 409
    assert exc.value.code == APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED
    assert exc.value.detail == "ordinary local input changed while its exact binding was created"
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.RunInputAdmission)) == 0
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalResultArtifact)) == 0


def test_local_file_revision_digest_covers_bytes_format_and_parse_options(tmp_path):
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    source = tmp_path / "digest.csv"
    source.write_text("value\n1\n")
    catalog._add(name="digest-csv", uri=str(source), strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter, storage=storage)

    def resolve(path, **config):
        graph = _graph(str(path), canvas_id=f"digest-{uuid.uuid4().hex}")
        graph.nodes[0].data["config"].update(config)
        candidates: list[dict[str, str]] = []
        manifest = runs._resolve_local_run_manifest(
            graph, "source", deps, materialize_local_files=True,
            local_file_candidates=candidates)
        finalize_local_file_candidates(storage, candidates, "unadmitted-digest-probe")
        return manifest[0]["revision_id"]

    header_revision = resolve(source, header="yes")
    assert resolve(source, header="no") != header_revision

    source.write_text("value\n2\n")
    changed_bytes_revision = resolve(source, header="yes")
    assert changed_bytes_revision != header_revision

    same_bytes_other_format = tmp_path / "digest.tsv"
    same_bytes_other_format.write_bytes(source.read_bytes())
    catalog._add(name="digest-tsv", uri=str(same_bytes_other_format), strict_probe=True)
    assert resolve(same_bytes_other_format, header="yes") != changed_bytes_revision


def test_postgres_ordinary_local_admission_publishes_mapping_and_owner(tmp_path):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("PostgreSQL admission contract")
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    source = tmp_path / "postgres-input.parquet"
    _write_ordinary_source(source, [7])
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    catalog._add(name=f"postgres-{uuid.uuid4().hex}", uri=str(source), strict_probe=True)
    graph = Graph.model_validate({
        "id": f"postgres-local-input-{uuid.uuid4().hex}",
        "version": 1,
        "nodes": [
            {"id": node_id, "type": "source", "position": {"x": 0, "y": index},
             "data": {"config": {"uri": str(source)}}}
            for index, node_id in enumerate(("first", "repeat"))
        ],
        "edges": [],
    })
    with metadb.session() as session:
        session.add(metadb.Canvas(id=graph.id, owner_id="local", name="postgres local input"))
    deps = SimpleNamespace(resolve_adapter=lambda _uri: adapter, storage=storage)
    candidates: list[dict[str, str]] = []
    manifest = runs._resolve_local_run_manifest(
        graph, None, deps, materialize_local_files=True,
        local_file_candidates=candidates)
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=graph.id, submission_id=str(uuid.uuid4()),
        target_node_id=None, intent_sha256="f" * 64, manifest=manifest,
        local_file_candidates=candidates)
    assert created is True
    finalize_local_file_candidates(storage, candidates, run_id)
    artifact = metadb.local_file_input_revision_artifact(
        manifest[0]["dataset_id"], manifest[0]["revision_id"])
    assert artifact is not None
    with metadb.session() as session:
        assert session.get(metadb.LocalResultReference, {
            "uri": artifact, "owner_kind": "run_input_admission", "owner_key": run_id,
        }) is not None
    assert [row[0] for row in adapter.scan(artifact).fetchall()] == [7]

    metadb.delete_canvas_cascade(graph.id)
    metadb.catalog_delete_entry(str(source))
    storage.prune_results(limit=10)
    assert metadb.local_file_input_revision_artifact(
        manifest[0]["dataset_id"], manifest[0]["revision_id"]) is None


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
    assert cfg["_input_dataset_id"] == manifest[0]["dataset_id"]
    assert cfg["_input_provider"] == manifest[0]["provider"]
    assert cfg["_input_revision_id"] == manifest[0]["revision_id"]
    with db.run_scope():
        assert LanceAdapter().open_revision(cfg["uri"], cfg["_input_revision_id"]).fetchall() == [(1,)]


def test_caller_manifest_cannot_retarget_a_source_to_another_dataset(tmp_path):
    lance = pytest.importorskip("lance")
    first_uri = str(tmp_path / "first.lance")
    second_uri = str(tmp_path / "second.lance")
    lance.write_dataset(pa.table({"value": [1]}), first_uri)
    lance.write_dataset(pa.table({"value": [2]}), second_uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="first", uri=first_uri, strict_probe=True)
    catalog._add(name="second", uri=second_uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    second_manifest = runs._resolve_local_run_manifest(_graph(second_uri), "source", deps)

    with pytest.raises(APIError) as exc:
        runs._bind_local_run_manifest(_graph(first_uri), second_manifest, deps, "source")

    assert getattr(exc.value, "status_code", None) == 409
    assert getattr(exc.value, "detail", None) == "local_run_input_manifest_does_not_match_graph"


def test_caller_local_file_manifest_cannot_retarget_another_registered_source(tmp_path):
    adapter = DuckDBAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    first = tmp_path / "retarget-first.parquet"
    second = tmp_path / "retarget-second.parquet"
    _write_ordinary_source(first, [1])
    _write_ordinary_source(second, [2])
    catalog._add(name="retarget-first", uri=str(first), strict_probe=True)
    catalog._add(name="retarget-second", uri=str(second), strict_probe=True)
    first_graph, deps, _first_manifest, _first_run, _first_artifact = _admit_ordinary_source(
        source=first, canvas_id="retarget-first", storage=storage,
        adapter=adapter, intent="6" * 64)
    _second_graph, _deps, second_manifest, _second_run, _second_artifact = (
        _admit_ordinary_source(
            source=second, canvas_id="retarget-second", storage=storage,
            adapter=adapter, intent="7" * 64))

    with pytest.raises(APIError) as exc:
        runs._bind_local_run_manifest(first_graph, second_manifest, deps, "source")

    assert exc.value.status_code == 409
    assert exc.value.detail == "local_run_input_manifest_does_not_match_graph"


def test_pinned_source_admission_uses_selected_revision_instead_of_current_head(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "pinned-input.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    table = catalog._add(name="pinned-input", uri=uri, strict_probe=True)
    binding = metadb.catalog_revision_binding_for_uri(uri)
    assert binding is not None
    selected = LanceAdapter().resolve_revision(uri)["revision_id"]
    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    graph = _graph(uri)
    graph.nodes[0].data["config"] |= {
        "tableId": table.id,
        "datasetRef": {"kind": "exact", "datasetId": binding["dataset_id"],
                       "revisionId": selected},
    }
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())

    manifest = runs._resolve_local_run_manifest(graph, "source", deps)

    assert manifest[0]["dataset_id"] == binding["dataset_id"]
    assert manifest[0]["revision_id"] == selected
    bound = runs._bind_local_run_manifest(graph, manifest, deps, "source")
    dispatch_config = bound.nodes[0].data["config"]
    assert dispatch_config["_input_dataset_id"] == binding["dataset_id"]
    assert dispatch_config["_input_provider"] == manifest[0]["provider"]
    assert dispatch_config["_input_revision_id"] == selected
    assert LanceAdapter().open_revision(uri, dispatch_config["_input_revision_id"]).fetchall() == [(1,)]


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


def test_input_drift_reports_latest_revision_and_schema_compatibility(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "drift.lance")
    lance.write_dataset(pa.table({"value": pa.array([1], type=pa.int32())}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="drift", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _graph(uri)
    preview_manifest = runs._resolve_local_run_manifest(graph, "source", deps)

    lance.write_dataset(
        pa.table({"value": pa.array([2], type=pa.int32())}), uri, mode="append")
    drift = runs._input_drift(graph, "source", preview_manifest, deps)

    assert drift.drifted is True
    assert len(drift.sources) == 1
    source = drift.sources[0]
    assert source.preview_revision_id == preview_manifest[0]["revision_id"]
    assert source.latest_revision_id != source.preview_revision_id
    assert source.old_revision_readable is True
    assert source.compatibility is not None
    assert source.compatibility.status in {"compatible", "unknown"}


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


def _local_start_context(monkeypatch, *, saved: bool = True):
    """Build the smallest default-local route seam around the admission boundary."""
    if saved:
        with metadb.session() as session:
            session.add(metadb.Canvas(id="local-admission", owner_id="local", name="admission"))

    class Runner:
        def __init__(self):
            self.receipts: dict[str, RunStatus] = {}

        @staticmethod
        def supports_admitted_input_manifests() -> bool:
            return True

        @staticmethod
        def estimate(*_args):
            return RunEstimate(rows=1, bytes=1, placement="local", needs_confirm=False)

        def status(self, run_id: str) -> RunStatus:
            return self.receipts[run_id]

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
    monkeypatch.setattr(runs, "_reject_row_reference_target_mismatch", lambda *_args: None)
    monkeypatch.setattr(runs.compiler, "compile_plan", lambda *_args: SimpleNamespace(acyclic=True))
    monkeypatch.setattr(runs, "_run_output_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_route_by_capability", lambda *_args: runner)
    monkeypatch.setattr(runs, "_require_destination_credential_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_cone_size", lambda *_args: (1, 1, {}))
    monkeypatch.setattr(runs, "_resolve_local_run_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(runs, "_bind_local_run_manifest", lambda graph, *_args: graph)
    return deps, _graph("lance://admission")


def test_unsaved_open_mode_graph_uses_a_canvasless_exact_admission(monkeypatch):
    deps, graph = _local_start_context(monkeypatch, saved=False)
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        RunStatus(run_id=run_id, status="queued"),
    )

    status, _owner = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=str(uuid.uuid4()))

    admission = metadb.local_run_input_admission(status.run_id)
    assert admission is not None
    assert admission["canvas_id"] is None


def test_queued_response_loss_adopts_the_claimed_local_run(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    calls = []

    def dispatch(_runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
        assert deps.run_index[run_id] is deps.runner
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


def test_typed_latest_retry_uses_retained_manifest_without_mutable_head(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    deps.resolve_adapter = lambda _uri: object()
    graph.parameters = [ParameterDeclaration(
        name="input", type="dataset", required=True)]
    graph.nodes[0].data["config"]["datasetRef"] = {"parameterRef": "input"}
    binding = ParameterBinding(
        name="input", value={"kind": "latest", "datasetId": "dataset"})

    class InitialAdapter:
        @staticmethod
        def resolve_revision(_uri):
            return {"revision_id": "revision"}

    monkeypatch.setattr(
        "hub.run_parameters.revision_adapter_for_uri", lambda *_args: InitialAdapter())
    monkeypatch.setattr(
        "hub.run_parameters.metadb.catalog_revision_binding_for_uri",
        lambda _uri: {"dataset_id": "dataset"},
    )
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        RunStatus(run_id=run_id, status="queued"),
    )
    submission = str(uuid.uuid4())
    first, _ = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=submission, parameter_bindings=[binding])

    actual_owner = SimpleNamespace(status=lambda run_id: RunStatus(
        run_id=run_id, status="queued"))
    deps.kernel_backend = lambda: actual_owner
    metadb.save_run_state(
        first.run_id,
        RunStatus(run_id=first.run_id, status="queued").model_dump(),
        canvas_id="local-admission",
        kernel_id="kernel-after-hub-restart",
    )
    deps.run_index.clear()

    def mutable_head_access_is_a_bug(*_args, **_kwargs):
        raise AssertionError("response-loss retry consulted the mutable provider head")

    monkeypatch.setattr(
        "hub.run_parameters.revision_adapter_for_uri", mutable_head_access_is_a_bug)
    monkeypatch.setattr(
        "hub.run_parameters.metadb.catalog_revision_binding_for_uri",
        mutable_head_access_is_a_bug,
    )
    retry, owner = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=submission, parameter_bindings=[binding])

    assert retry.run_id == first.run_id
    assert owner is actual_owner
    assert deps.run_index[first.run_id] is actual_owner

    changed_binding = ParameterBinding(
        name="input", value={"kind": "latest", "datasetId": "other-dataset"})
    with pytest.raises(Exception) as conflict:
        runs.start_run(
            deps, graph, "source", "local", confirmed=True,
            submission_id=submission, parameter_bindings=[changed_binding])
    assert getattr(conflict.value, "status_code", None) == 409

    changed_declaration = graph.model_copy(deep=True)
    changed_declaration.parameters[0].required = False
    with pytest.raises(Exception) as conflict:
        runs.start_run(
            deps, changed_declaration, "source", "local", confirmed=True,
            submission_id=submission, parameter_bindings=[binding])
    assert getattr(conflict.value, "status_code", None) == 409

    with pytest.raises(Exception) as conflict:
        runs.start_run(
            deps, graph, None, "local", confirmed=True,
            submission_id=submission, parameter_bindings=[binding])
    assert getattr(conflict.value, "status_code", None) == 409


def test_start_run_admits_the_caller_preview_manifest_without_resolving_latest(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    preview_manifest = [{
        "node_id": "source", "dataset_id": "dataset", "revision_id": "preview-revision",
        "provider": "lance", "resolved_at": "preview-time",
    }]
    bound: list[list[dict[str, str]]] = []
    monkeypatch.setattr(
        runs, "_resolve_local_run_manifest",
        lambda *_args: (_ for _ in ()).throw(AssertionError("latest must not be resolved")),
    )
    monkeypatch.setattr(
        runs, "_bind_local_run_manifest",
        lambda current_graph, manifest, *_args: bound.append(manifest) or current_graph,
    )
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        RunStatus(run_id=run_id, status="queued"),
    )

    status, _ = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=str(uuid.uuid4()), input_manifest=preview_manifest,
    )

    assert bound == [preview_manifest, preview_manifest]
    assert metadb.local_run_input_manifest(status.run_id) == preview_manifest


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
        deps.runner.receipts[run_id] = RunStatus(run_id=run_id, status="queued")
        raise RuntimeError("response lost after dispatch")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    submission_id = str(uuid.uuid4())
    first, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)

    retry, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    assert retry.status == "queued"
    assert retry.run_id == first.run_id
    assert calls == [first.run_id]


def test_dispatch_exception_before_worker_terminalizes_the_claim(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    calls: list[str] = []

    def dispatch(_runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
        calls.append(run_id)
        raise RuntimeError("runner rejected before creating a worker")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    submission_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="runner rejected"):
        runs.start_run(deps, graph, "source", "local", confirmed=True,
                       submission_id=submission_id)

    run_id = metadb.local_run_submission_id("local", "local-admission", submission_id)
    failed = metadb.get_run_state(run_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError: runner rejected before creating a worker"
    assert metadb.terminal_run_status(run_id) == "failed"
    history = metadb.list_runs("local-admission")
    assert len(history) == 1
    assert history[0]["runId"] == run_id
    assert history[0]["status"] == "failed"
    assert history[0]["inputManifest"] == [{
        "dataset_id": "dataset", "node_id": "source", "provider": "lance",
        "resolved_at": "now", "revision_id": "revision",
    }]
    retry, _ = runs.start_run(deps, graph, "source", "local", confirmed=True,
                              submission_id=submission_id)
    assert retry.status == "failed"
    assert calls == [run_id]


def test_unstarted_claim_failures_follow_terminal_and_history_retention(monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
    monkeypatch.setattr(metadb, "_RUN_HISTORY_MAX", 1)

    def dispatch(*_args, **_kwargs):
        raise RuntimeError("runner rejected before creating a worker")

    monkeypatch.setattr("hub.observability.invoke_backend_run", dispatch)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="runner rejected"):
            runs.start_run(deps, graph, "source", "local", confirmed=True,
                           submission_id=str(uuid.uuid4()))

    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.RunState)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.RunRecord)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.RunInputAdmission)) == 1


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


def test_unclaimed_retained_admission_replays_without_live_row_reference_dependencies(
        monkeypatch):
    deps, graph = _local_start_context(monkeypatch)
    submission_id = str(uuid.uuid4())
    monkeypatch.setattr(
        runs, "_bind_local_run_manifest",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("crash before dispatch claim")))
    with pytest.raises(RuntimeError, match="crash before dispatch claim"):
        runs.start_run(
            deps, graph, "source", "local", confirmed=True,
            submission_id=submission_id)

    run_id = metadb.local_run_submission_id(
        "local", "local-admission", submission_id)
    assert metadb.local_run_input_admission(run_id) is not None
    assert metadb.get_run_state(run_id) is None

    def unavailable(*_args, **_kwargs):
        raise AssertionError("retained replay consulted live catalog/schema facts")

    deps.catalog = SimpleNamespace(resolve_ref=unavailable)
    deps.resolve_adapter = unavailable
    monkeypatch.setattr(runs.graph_mod, "resolve_source_refs", unavailable)
    monkeypatch.setattr(runs, "_reject_row_reference_target_mismatch", unavailable)
    monkeypatch.setattr(
        runs, "_bind_local_run_manifest",
        lambda current_graph, *_args, **_kwargs: current_graph)
    monkeypatch.setattr(
        "hub.observability.invoke_backend_run",
        lambda _runner, _plan, _graph, _target, _placement, *, run_id, **_kwargs:
        RunStatus(run_id=run_id, status="queued"),
    )
    retry, owner = runs.start_run(
        deps, graph, "source", "local", confirmed=True,
        submission_id=submission_id)
    assert retry.run_id == run_id
    assert owner is deps.runner
