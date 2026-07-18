"""Canonical execution definitions share the existing run admission lifecycle."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hub import metadb
from hub.execution_manifest import (
    ExecutionManifestError,
    build_execution_manifest,
    execution_manifest_accepts_graph_replay,
    validate_execution_manifest,
)
from hub.models import (
    DatasetRevision,
    Graph,
    LineagePublication,
    WorkspaceRunRecord,
    WriteDestination,
    WriteIntent,
    WritePublicationIdentity,
    WriteProvenance,
    WriteReceipt,
)
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.nodespecs import NodeSpec
from hub.run_controller import RunController


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, session_factory, url = metadb._engine, metadb._Session, settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (
        os.environ.get("DP_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'execution-manifest.db'}"
    )
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine, metadb._Session = engine, session_factory


def _deps():
    return SimpleNamespace(
        node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
        plugins=[],
    )


def _graph(*, canvas_id: str = "manifest-canvas", disabled: bool = False) -> Graph:
    return Graph.model_validate({
        "id": canvas_id,
        "version": 7,
        "nodes": [
            {
                "id": "source",
                "type": "source",
                "position": {"x": 10, "y": 20},
                "data": {
                    "title": "Readable source name",
                    "status": "latest",
                    "disabled": disabled,
                    "config": {"uri": "/private/research/input.parquet"},
                },
            },
            {
                "id": "filter",
                "type": "filter",
                "position": {"x": 30, "y": 40},
                "data": {
                    "title": "Keep useful rows",
                    "status": "stale",
                    "config": {"predicate": "score > 0"},
                },
            },
        ],
        "edges": [{
            "id": "display-edge-id",
            "source": "source",
            "target": "filter",
            "data": {"wire": "dataset"},
        }],
        "requirements": ["numpy==2.5.0"],
    })


def _inputs(*, revision: str = "revision-1", resolved_at: str = "2026-07-18T00:00:00Z"):
    return [{
        "node_id": "source",
        "dataset_id": "dataset-1",
        "revision_id": revision,
        "provider": "test-provider",
        "resolved_at": resolved_at,
    }]


def _write_intent(destination: str, *, run_id: str = "run-1") -> WriteIntent:
    publication = LineagePublication(
        idempotency_key="write-key",
        run_id=run_id,
        producer="manifest-canvas",
        producer_version=7,
        step_id="write",
        provenance="run",
    )
    return WriteIntent(
        destination=WriteDestination(
            logical_uri=destination,
            name="output.parquet",
            provider="managed-local-file",
        ),
        mode="create",
        expected_schema=[],
        idempotency_key="write-key",
        provenance=WriteProvenance(publication=publication, parents=[]),
    )


def _build(
    graph: Graph | None = None,
    *,
    inputs=None,
    target: str | None = "filter",
    port: str | None = None,
    write_intent: WriteIntent | None = None,
    deps=None,
):
    return build_execution_manifest(
        graph or _graph(),
        target_node_id=target,
        target_port_id=port,
        input_manifest=_inputs() if inputs is None else inputs,
        write_intent=write_intent,
        deps=deps or _deps(),
    )


def test_digest_ignores_nonsemantic_canvas_display_and_admission_time():
    digest, payload = _build()
    changed = _graph(canvas_id="renamed-canvas")
    changed.version = 99
    changed.nodes[0].position.x = 999
    changed.nodes[0].data["title"] = "Different display name"
    changed.nodes[0].data["status"] = "running"
    observed, observed_payload = _build(
        changed,
        inputs=_inputs(resolved_at="2030-01-01T12:34:56Z"),
    )

    assert observed == digest
    assert observed_payload == payload
    doc = validate_execution_manifest(digest, payload)
    encoded = json.dumps(doc)
    assert "/private/research/input.parquet" not in encoded
    assert "Readable source name" not in encoded
    assert "resolved_at" not in encoded
    assert doc["graph"]["nodes"][0]["data"]["config"]["datasetRef"] == {
        "kind": "exact", "datasetId": "dataset-1", "revisionId": "revision-1",
    }
    assert "parameters" not in doc

    reordered_requirements = _graph()
    reordered_requirements.requirements = ["polars==1.32.0", "numpy==2.5.0"]
    first_order, _ = _build(reordered_requirements)
    reordered_requirements.requirements.reverse()
    second_order, _ = _build(reordered_requirements)
    assert second_order == first_order


def test_digest_retains_only_titles_consumed_by_execution():
    baseline, _ = _build()

    metric = _graph()
    metric.nodes[1].type = "metric"
    metric.nodes[1].data["title"] = "Rows kept"
    first_metric, _ = _build(metric)
    metric.nodes[1].data["title"] = "Useful rows"
    second_metric, _ = _build(metric)
    assert second_metric != first_metric

    write = _graph()
    write.nodes[1].type = "write"
    write.nodes[1].data["config"] = {"format": "parquet"}
    write.nodes[1].data["title"] = "daily output"
    first_write, _ = _build(write)
    write.nodes[1].data["title"] = "weekly output"
    second_write, _ = _build(write)
    assert second_write != first_write

    # An explicit filename makes the Write title display-only again.
    write.nodes[1].data["config"]["filename"] = "fixed.parquet"
    explicit_write, _ = _build(write)
    write.nodes[1].data["title"] = "Display-only rename"
    renamed_explicit_write, _ = _build(write)
    assert renamed_explicit_write == explicit_write

    section_child = _graph()
    section_child.nodes[1].parent_id = "section"
    section_child.nodes[1].data["title"] = "clean rows"
    first_alias, _ = _build(section_child)
    section_child.nodes[1].data["title"] = "validated rows"
    second_alias, _ = _build(section_child)
    assert second_alias != first_alias

    assert baseline == _build()[0]


def test_manifest_replay_compares_graph_without_re_resolving_retained_inputs():
    digest, payload = _build()
    moved_source = _graph()
    moved_source.nodes[0].data["config"]["uri"] = "/new/provider/location.parquet"
    assert execution_manifest_accepts_graph_replay(
        digest, payload, moved_source,
        target_node_id="filter", target_port_id=None,
    )

    moved_source.nodes[1].data["config"]["predicate"] = "score >= 0"
    assert not execution_manifest_accepts_graph_replay(
        digest, payload, moved_source,
        target_node_id="filter", target_port_id=None,
    )


@pytest.mark.parametrize("change", ["config", "disabled", "target", "port", "input", "write"])
def test_execution_changes_produce_distinct_semantic_digests(change: str):
    baseline, _ = _build()
    graph = _graph()
    inputs = _inputs()
    target, port, write_intent = "filter", None, None
    if change == "config":
        graph.nodes[1].data["config"]["predicate"] = "score >= 0"
    elif change == "disabled":
        graph.nodes[0].data["disabled"] = True
    elif change == "target":
        target = "source"
    elif change == "port":
        port = "out"
    elif change == "input":
        inputs = _inputs(revision="revision-2")
    else:
        write_intent = _write_intent("file:///workspace/outputs/result.parquet")

    observed, _ = _build(
        graph, inputs=inputs, target=target, port=port, write_intent=write_intent)
    assert observed != baseline


def test_manifest_rejects_material_secrets_and_is_byte_bounded(monkeypatch):
    graph = _graph()
    graph.nodes[1].data["config"]["apiKey"] = "material-secret"
    with pytest.raises(ExecutionManifestError, match="sensitive field"):
        _build(graph)

    graph.nodes[1].data["config"].pop("apiKey")
    graph.nodes[1].data["config"]["accessToken"] = "material-secret"
    with pytest.raises(ExecutionManifestError, match="sensitive field"):
        _build(graph)

    graph.nodes[1].data["config"].pop("accessToken")
    graph.nodes[1].data["config"]["apiKeyRef"] = "renamed-material-secret"
    with pytest.raises(ExecutionManifestError, match="sensitive field"):
        _build(graph)

    graph.nodes[1].data["config"]["apiKeyRef"] = "env:MODEL_API_KEY"
    import hub.execution_manifest as contract
    monkeypatch.setattr(contract, "MAX_MANIFEST_BYTES", 128)
    with pytest.raises(ExecutionManifestError, match="encoded bytes"):
        _build(graph)


def test_manifest_snapshots_only_used_plugin_descriptors_and_versions():
    graph = _graph()
    graph.nodes[1].type = "plugin-filter"
    plugin_spec = NodeSpec(
        kind="plugin-filter", title="Plugin filter", category="compute",
        source="plugin:quality-pack",
    )
    node_specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    node_specs[plugin_spec.kind] = plugin_spec
    deps = SimpleNamespace(
        node_specs=node_specs,
        plugins=[{
            "name": "quality-pack", "package": "dp-quality-pack",
            "version": "1.2.3", "source": "entry-point",
        }],
    )

    digest, payload = _build(graph, deps=deps)
    doc = validate_execution_manifest(digest, payload)
    assert {item["kind"] for item in doc["descriptors"]["nodes"]} == {
        "source", "plugin-filter",
    }
    assert doc["descriptors"]["plugins"] == [{
        "name": "quality-pack", "package": "dp-quality-pack",
        "version": "1.2.3", "source": "entry-point",
    }]

    plugin_spec.title = "Renamed in the editor"
    plugin_spec.blurb = "Different help text"
    display_only, _ = _build(graph, deps=deps)
    assert display_only == digest

    deps.plugins[0]["version"] = "2.0.0"
    changed, _ = _build(graph, deps=deps)
    assert changed != digest


def _canvas(canvas_id: str) -> None:
    with metadb.session() as session:
        session.add(metadb.Canvas(id=canvas_id, owner_id="local", name=canvas_id))


def _admit(canvas_id: str, submission_id: str, digest: str, payload: str) -> str:
    run_id, created = metadb.admit_local_run_inputs(
        uid="local",
        canvas_id=canvas_id,
        submission_id=submission_id,
        target_node_id="filter",
        intent_sha256="a" * 64,
        manifest=_inputs(),
        execution_manifest_sha256=digest,
        execution_manifest_doc=payload,
    )
    assert created is True
    return run_id


def test_response_loss_replay_adopts_only_the_original_manifest():
    _canvas("manifest-canvas")
    submission_id = str(uuid.uuid4())
    digest, payload = _build()
    run_id = _admit("manifest-canvas", submission_id, digest, payload)

    replayed_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id="manifest-canvas", submission_id=submission_id,
        target_node_id="filter", intent_sha256="a" * 64, manifest=_inputs(),
        execution_manifest_sha256=digest, execution_manifest_doc=payload,
    )
    assert (replayed_id, created) == (run_id, False)

    changed_digest, changed_payload = _build(target="source")
    with pytest.raises(RuntimeError, match="does not match its persisted admission"):
        metadb.admit_local_run_inputs(
            uid="local", canvas_id="manifest-canvas", submission_id=submission_id,
            target_node_id="filter", intent_sha256="a" * 64, manifest=_inputs(),
            execution_manifest_sha256=changed_digest,
            execution_manifest_doc=changed_payload,
        )


def test_distributed_dispatch_fails_closed_without_a_durable_manifest_callback():
    graph = _graph()
    digest, payload = _build(graph)
    graph._execution_manifest_sha256 = digest
    graph._execution_manifest_doc = payload
    controller = RunController(_deps(), base=None, place_fn=None)
    regions = [SimpleNamespace(
        node_ids={"source", "filter"}, output_node="filter",
        backend="placed", cut_inputs=[],
    )]

    with pytest.raises(RuntimeError, match="no durable status callback"):
        controller.run(graph, "filter", regions=regions)
    assert controller.runs == {}


def test_admission_state_history_share_manifest_and_canvas_delete_reclaims_it():
    _canvas("manifest-canvas")
    digest, payload = _build()
    run_id = _admit("manifest-canvas", str(uuid.uuid4()), digest, payload)

    queued, dispatch = metadb.claim_local_run_dispatch(
        run_id=run_id, uid="local", auth_canvas_id=None, request_id="request-1")
    assert dispatch is True
    assert queued["status"] == "queued"
    metadb.record_run(
        canvas_id="manifest-canvas", target_node_id="filter", job_type="run",
        status="failed", error="expected test failure", run_id=run_id,
    )

    with metadb.session() as session:
        admission = session.get(metadb.RunInputAdmission, run_id)
        state = session.get(metadb.RunState, run_id)
        history = session.query(metadb.RunRecord).filter_by(run_id=run_id).one()
        assert admission is not None and state is not None
        assert {
            admission.execution_manifest_sha256,
            state.execution_manifest_sha256,
            history.execution_manifest_sha256,
        } == {digest}
    listed = metadb.list_runs("manifest-canvas")
    assert listed[0]["executionManifestSha256"] == digest
    assert metadb.execution_manifest(digest)["document"]["target"]["nodeId"] == "filter"

    metadb.delete_canvas_cascade("manifest-canvas")
    assert metadb.execution_manifest(digest) is None


def test_history_rejects_disagreeing_admission_and_state_manifest_owners():
    _canvas("manifest-canvas")
    digest, payload = _build()
    run_id = _admit("manifest-canvas", str(uuid.uuid4()), digest, payload)
    metadb.claim_local_run_dispatch(
        run_id=run_id, uid="local", auth_canvas_id=None, request_id="request-1")
    changed_digest, changed_payload = _build(target="source")
    with metadb.session() as session:
        metadb._persist_execution_manifest(session, changed_digest, changed_payload)
        state = session.get(metadb.RunState, run_id)
        assert state is not None
        state.execution_manifest_sha256 = changed_digest

    with pytest.raises(RuntimeError, match="owners disagree"):
        metadb.record_run(
            canvas_id="manifest-canvas", target_node_id="filter", job_type="run",
            status="failed", error="expected", run_id=run_id,
        )


def test_receipt_and_canvas_lineage_resolve_the_same_manifest_through_run_identity():
    _canvas("manifest-canvas")
    submission_id = str(uuid.uuid4())
    expected_run_id = metadb.local_run_submission_id(
        "local", "manifest-canvas", submission_id)
    intent = _write_intent(
        "file:///workspace/outputs/result.parquet", run_id=expected_run_id)
    digest, payload = _build(write_intent=intent)
    run_id = _admit("manifest-canvas", submission_id, digest, payload)
    assert run_id == expected_run_id
    metadb.record_run(
        canvas_id="manifest-canvas", target_node_id="filter", job_type="run",
        status="failed", error="expected", run_id=run_id,
    )

    receipt = WriteReceipt(
        dataset_id="dataset-output", revision_id="revision-output",
        head=DatasetRevision(
            dataset_id="dataset-output", revision_id="revision-output"),
        rows=1, bytes=8, schema=[],
        publication=WritePublicationIdentity(
            logical_uri="file:///workspace/outputs/result.parquet",
            artifact_uri="file:///workspace/outputs/result.parquet",
            publish_sequence=1, idempotency_key="write-key",
        ),
        provenance=intent.provenance,
    )
    with metadb.session() as session:
        lineage = metadb.CatalogLineageFact(
            fact_key="manifest-lineage-fact", publication_key="manifest-publication",
            fingerprint="manifest-fingerprint", source_key="source-key",
            destination_key="destination-key", source_uri="file:///source.parquet",
            destination_uri="file:///workspace/outputs/result.parquet",
            source_key_hash="1" * 64, destination_key_hash="2" * 64,
            source_uri_hash="3" * 64, destination_uri_hash="4" * 64,
            run_id=run_id, producer="manifest-canvas", producer_version=7,
            step_id="filter", provenance="run",
        )
        session.add(lineage)

    assert receipt.provenance.publication.run_id == run_id
    with metadb.session() as session:
        lineage_run_id = session.query(metadb.CatalogLineageFact.run_id).filter_by(
            fact_key="manifest-lineage-fact").scalar()
    assert lineage_run_id == run_id
    assert metadb.execution_manifest_sha256_for_run(run_id) == digest

    with metadb.session() as session:
        history = session.query(metadb.RunRecord).filter_by(run_id=run_id).one()
        history.execution_manifest_sha256 = "f" * 64
    with pytest.raises(RuntimeError, match="disagree"):
        metadb.execution_manifest_sha256_for_run(run_id)
    with metadb.session() as session:
        history = session.query(metadb.RunRecord).filter_by(run_id=run_id).one()
        history.execution_manifest_sha256 = digest


def test_shared_manifest_survives_one_owner_and_legacy_history_is_explicit():
    digest, payload = _build()
    for canvas_id in ("manifest-canvas", "manifest-canvas-2", "legacy-canvas"):
        _canvas(canvas_id)
    _admit("manifest-canvas", str(uuid.uuid4()), digest, payload)
    _admit("manifest-canvas-2", str(uuid.uuid4()), digest, payload)
    metadb.record_run(
        canvas_id="legacy-canvas", target_node_id=None, job_type="run",
        status="failed", error="legacy", run_id="legacy-run",
    )
    legacy = metadb.list_workspace_runs(
        uid="local", canvas_id="legacy-canvas", limit=10)
    assert legacy["items"][0]["executionManifestSha256"] is None
    assert legacy["items"][0]["executionManifestReconstructable"] is False
    legacy_model = WorkspaceRunRecord.model_validate(legacy["items"][0])
    assert legacy_model.execution_manifest_sha256 is None
    assert legacy_model.execution_manifest_reconstructable is False

    metadb.delete_canvas_cascade("manifest-canvas")
    assert metadb.execution_manifest(digest) is not None
    metadb.delete_canvas_cascade("manifest-canvas-2")
    assert metadb.execution_manifest(digest) is None


def test_profile_preallocation_and_history_retain_one_manifest_across_restart_projection():
    _canvas("manifest-canvas")
    digest, payload = _build(port="out")
    submission_id = str(uuid.uuid4())
    reservation = metadb.preallocate_or_adopt_profile_run_owner(
        submission_id, "local", None, "manifest-canvas", "filter", "out",
        "b" * 64,
        input_manifest=_inputs(),
        execution_manifest_sha256=digest,
        execution_manifest_doc=payload,
    )
    assert reservation.should_dispatch is True
    with metadb.session() as session:
        state = session.get(metadb.RunState, reservation.run_id)
        assert state is not None and state.execution_manifest_sha256 == digest

    changed_digest, changed_payload = _build(target="source", port="out")
    with pytest.raises(
            metadb.ProfileSubmissionConflict, match="different execution manifest"):
        metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "local", None, "manifest-canvas", "filter", "out",
            "b" * 64, input_manifest=_inputs(),
            execution_manifest_sha256=changed_digest,
            execution_manifest_doc=changed_payload,
        )
    metadb.record_run(
        canvas_id="manifest-canvas", target_node_id="filter", target_port_id="out",
        job_type="profile", status="failed", error="expected", run_id=reservation.run_id,
    )
    assert metadb.list_runs("manifest-canvas")[0]["executionManifestSha256"] == digest


def test_history_pruning_and_state_eviction_reclaim_unreferenced_manifests(monkeypatch):
    monkeypatch.setattr(metadb, "_RUN_HISTORY_MAX", 1)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
    _canvas("manifest-canvas")
    first_digest, first_payload = _build()
    changed_graph = _graph()
    changed_graph.nodes[1].data["config"]["predicate"] = "score > 10"
    second_digest, second_payload = _build(changed_graph)

    run_ids = []
    for digest, payload in (
        (first_digest, first_payload), (second_digest, second_payload),
    ):
        run_id = _admit("manifest-canvas", str(uuid.uuid4()), digest, payload)
        run_ids.append(run_id)
        metadb.claim_local_run_dispatch(
            run_id=run_id, uid="local", auth_canvas_id=None,
            request_id=f"request-{len(run_ids)}",
        )
        metadb.save_run_state(
            run_id, {"run_id": run_id, "status": "failed", "error": "expected"},
            canvas_id="manifest-canvas",
        )
        metadb.record_run(
            canvas_id="manifest-canvas", target_node_id="filter", job_type="run",
            status="failed", error="expected", run_id=run_id,
        )
        if len(run_ids) == 1:
            with metadb.session() as session:
                old = datetime(2020, 1, 1, tzinfo=timezone.utc)
                state = session.get(metadb.RunState, run_id)
                history = session.query(metadb.RunRecord).filter_by(run_id=run_id).one()
                assert state is not None
                state.updated_at = old
                history.created_at = old

    assert metadb.execution_manifest(first_digest) is None
    assert metadb.execution_manifest(second_digest) is not None
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_ids[0]) is None
        assert session.query(metadb.RunRecord).filter_by(run_id=run_ids[0]).count() == 0
