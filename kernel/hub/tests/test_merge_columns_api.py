"""Headless product admission for the certified local merge-columns path."""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from hub import metadb
from hub.api_errors import APIError
from hub.deps import Deps
from hub.models import Graph
from hub.routers import merge_columns as api


@pytest.fixture(autouse=True)
def _metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'metadata.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


def _request(
        tmp_path, monkeypatch, *, owner_id: str = "owner", editor_id: str = "editor",
        canvas_id: str = "canvas", submission_id: str = "merge-api-submission"):
    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"), maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    logical_uri = deps.storage.output_uri("base", ".parquet")
    artifact = deps.storage.begin_result("merge-api-fixture", "fixture")
    pq.write_table(pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"], "untouched": [7, 8],
    }), artifact)
    deps.storage.commit_result(artifact, "fixture")
    published = deps.catalog.publish_managed_local_file_output(
        name="base", logical_uri=logical_uri, artifact_uri=artifact)
    assert deps.storage.release_result(artifact, "fixture")
    binding = metadb.catalog_revision_binding(published["dataset_id"])
    assert binding is not None
    with metadb.session() as session:
        session.add(metadb.User(id=owner_id, name="Owner"))
        session.add(metadb.User(id=editor_id, name="Editor"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=owner_id, name="Canvas", doc="{}"))
        session.add(metadb.CanvasShare(canvas_id=canvas_id, user_id=editor_id, role="editor"))
    return api.MergeColumnsRequestV1(
        graph=Graph.model_validate({
            "id": canvas_id,
            "nodes": [
                {"id": "source", "type": "source", "data": {"config": {
                    "uri": binding["uri"], "datasetRef": {"kind": "exact",
                        "datasetId": published["dataset_id"], "revisionId": published["revision_id"]},
                }}},
                {"id": "select", "type": "select", "data": {"config": {
                    "select": "id, value AS replacement"}}},
                {"id": "write", "type": "write", "data": {"config": {
                    "filename": "base.parquet", "writeMode": "overwrite"}}},
            ],
            "edges": [
                {"id": "source-select", "source": "source", "target": "select"},
                {"id": "select-write", "source": "select", "target": "write"},
            ],
        }),
        submission_id=submission_id, identity_columns=["id"],
        rules=[{"source": "replacement", "target": "value", "mode": "replace"}],
    )


def test_editor_preflights_without_side_effect_then_submits_and_replays(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
    preflight = api.preflight(request, "editor")
    assert preflight.coverage.status == "complete"
    assert preflight.coverage.matched_identities == 2
    assert preflight.output_schema[-1].name == "untouched"
    assert "sha" not in preflight.model_dump_json().lower()
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0

    submitted = api.submit(request, "editor")
    durable = metadb.durable_task(submitted.task_id, include_admission=False)
    assert durable is not None and durable["target_node_id"] == "write"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.status(submitted.task_id, "editor")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done"
    assert "sparseOutputId" not in current.model_dump_json(by_alias=True)
    durable = metadb.durable_task(submitted.task_id, include_admission=False)
    assert durable is not None
    assert durable["status_doc"]["target_node_id"] == "write"
    assert durable["status_doc"]["outputs"][0]["node_id"] == "write"
    job = next(item for item in metadb.list_workspace_runs(
        "editor", run_id=submitted.task_id)["items"] if item["taskId"] == submitted.task_id)
    assert job["outputs"][0]["node_id"] == "write"
    replayed = api.submit(request, "editor")
    assert replayed.task_id == submitted.task_id
    durable = metadb.durable_task(replayed.task_id, include_admission=False)
    assert durable is not None and durable["target_node_id"] == "write"

    presentation_replay = request.model_copy(deep=True)
    presentation_replay.graph.version = 99
    presentation_replay.graph.nodes[0].position.x = 400
    presentation_replay.graph.nodes[0].data["title"] = "Moved source"
    assert api.submit(presentation_replay, "editor").task_id == submitted.task_id

    moved_head = request.model_copy(update={"submission_id": "after-real-head-move"})
    with pytest.raises(APIError) as moved_preflight:
        api.preflight(moved_head, "editor")
    assert moved_preflight.value.status_code == 409
    with pytest.raises(APIError) as moved_submit:
        api.submit(moved_head, "editor")
    assert moved_submit.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 1

    changed_select = request.model_copy(deep=True)
    changed_select.graph.nodes[1].data["config"]["select"] = "id, value AS changed"
    with pytest.raises(APIError) as select_error:
        api.submit(changed_select, "editor")
    assert select_error.value.status_code == 409
    changed_rules = request.model_copy(deep=True)
    changed_rules.rules[0].target = "untouched"
    with pytest.raises(APIError) as rules_error:
        api.submit(changed_rules, "editor")
    assert rules_error.value.status_code == 409
    changed_source = request.model_copy(deep=True)
    changed_source.graph.nodes[0].data["config"]["uri"] = "file:///different.parquet"
    with pytest.raises(APIError) as source_error:
        api.submit(changed_source, "editor")
    assert source_error.value.status_code == 409
    changed_write = request.model_copy(deep=True)
    changed_write.graph.nodes[2].data["config"]["filename"] = "different.parquet"
    with pytest.raises(APIError) as write_error:
        api.submit(changed_write, "editor")
    assert write_error.value.status_code == 409
    changed_target = request.model_copy(deep=True)
    changed_target.graph.nodes[2].id = "other-write"
    changed_target.graph.edges[1].target = "other-write"
    with pytest.raises(APIError) as target_error:
        api.submit(changed_target, "editor")
    assert target_error.value.status_code == 409


def test_add_mapping_preflights_and_publishes_full_schema(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    request.graph.nodes[1].data["config"]["select"] = "id, value AS added"
    request.rules[0].source = "added"
    request.rules[0].target = "derived"
    request.rules[0].mode = "add"
    preflight = api.preflight(request, "editor")
    assert [column.name for column in preflight.output_schema] == ["id", "value", "untouched", "derived"]

    submitted = api.submit(request, "editor")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.status(submitted.task_id, "editor")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done"
    dataset_id = request.graph.nodes[0].data["config"]["datasetRef"]["datasetId"]
    with metadb.session() as session:
        logical = session.get(metadb.CatalogLogicalDataset, dataset_id)
        assert logical is not None
        logical_uri = logical.logical_uri
    head = metadb.catalog_managed_local_write_head(logical_uri)
    assert head is not None
    artifact = metadb.managed_local_file_revision_artifact(dataset_id, head["revision_id"])
    assert artifact is not None
    table = pq.read_table(artifact)
    assert table.schema.names == ["id", "value", "untouched", "derived"]
    assert table.column("derived").to_pylist() == ["a", "b"]


def test_partial_or_invalid_admission_never_creates_merge_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    partial = request.model_copy(deep=True)
    partial.graph.nodes[1].data["config"]["select"] = "id + 1 AS id, value AS replacement"
    assert api.preflight(partial, "editor").eligible is False
    with pytest.raises(APIError) as partial_error:
        api.submit(partial, "editor")
    assert partial_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0

    invalid = request.model_copy(deep=True)
    invalid.graph.nodes[1].data["config"]["select"] = "1 AS id, value AS replacement"
    assert api.preflight(invalid, "editor").eligible is False
    with pytest.raises(APIError) as invalid_error:
        api.submit(invalid, "editor")
    assert invalid_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_submission_conflict_releases_only_unclaimed_merge_sidecars(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    real_submit = metadb.submit_merge_columns_task

    def unclaimed_conflict(**_kwargs):
        raise metadb.DurableTaskSubmissionConflict("stale expected head")

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", unclaimed_conflict)
    with pytest.raises(APIError) as unclaimed_error:
        api.submit(request, "editor")
    assert unclaimed_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0

    def claimed_conflict(**kwargs):
        real_submit(**kwargs)
        raise metadb.DurableTaskSubmissionConflict("concurrent admission")

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", claimed_conflict)
    reconciled = api.submit(request, "editor")
    assert reconciled.status == "queued"
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 1


def test_unsupported_destination_is_a_conflict_without_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    request.graph.nodes[2].data["config"]["destId"] = "missing-destination"
    with pytest.raises(APIError) as error:
        api.preflight(request, "editor")
    assert error.value.status_code == 409
    with pytest.raises(APIError) as submit_error:
        api.submit(request, "editor")
    assert submit_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


@pytest.mark.parametrize("blocker", [
    "latest-source", "mutable-source", "wrong-shape", "multiple-inputs", "non-default-port",
    "csv-destination", "lance-destination", "arrow-destination", "provider-destination",
    "object-destination",
    "append", "upsert",
])
def test_documented_router_blockers_reject_before_side_effects(tmp_path, monkeypatch, blocker):
    request = _request(tmp_path, monkeypatch)
    if blocker == "latest-source":
        request.graph.nodes[0].data["config"]["datasetRef"] = {
            "kind": "latest",
            "datasetId": request.graph.nodes[0].data["config"]["datasetRef"]["datasetId"],
        }
    elif blocker == "mutable-source":
        request.graph.nodes[0].data["config"]["uri"] = "file:///mutable-source.parquet"
    elif blocker == "wrong-shape":
        request.graph.edges.pop()
    elif blocker == "multiple-inputs":
        request.graph.edges.append(request.graph.edges[0].model_copy(update={
            "id": "extra-input", "target": "write"}))
    elif blocker == "non-default-port":
        request.graph.edges[0].source_handle = "alternate"
    elif blocker in ("csv-destination", "lance-destination", "arrow-destination"):
        request.graph.nodes[2].data["config"]["filename"] = f"base.{blocker.split('-')[0]}"
    elif blocker == "provider-destination":
        request.graph.nodes[2].data["config"]["destId"] = "provider-destination"
    elif blocker == "object-destination":
        from hub import destinations
        monkeypatch.setattr(destinations, "presets", lambda _workspace: [{
            "id": "object", "name": "Object", "backend": "s3", "root": "s3://bucket",
        }])
        request.graph.nodes[2].data["config"]["destId"] = "object"
    else:
        request.graph.nodes[2].data["config"]["writeMode"] = blocker

    with pytest.raises(APIError) as preflight_error:
        api.preflight(request, "editor")
    assert preflight_error.value.status_code == 409
    with pytest.raises(APIError) as submit_error:
        api.submit(request, "editor")
    assert submit_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_unauthorized_preflight_and_submit_leave_no_merge_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add(metadb.User(id="intruder", name="Intruder"))
    for endpoint in (api.preflight, api.submit):
        with pytest.raises(APIError) as error:
            endpoint(request, "intruder")
        assert error.value.status_code == 404
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_revoked_editor_replay_cannot_dispatch_existing_task(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: dispatched.append(task_id))
    task = api.submit(request, "editor")
    assert dispatched == [task.task_id]
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
    with pytest.raises(APIError) as error:
        api.submit(request, "editor")
    assert error.value.status_code == 404
    assert dispatched == [task.task_id]
    with metadb.session() as session:
        durable = session.get(metadb.DurableTask, task.task_id)
        assert durable is not None and durable.status == "queued"


def _queued(request, uid, monkeypatch):
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    return api.submit(request, uid)


def _cancelled(task_id):
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        assert task is not None
        task.status = "cancelled"
        task.cancel_requested = False


def test_owner_and_editor_can_cancel_and_idempotently_retry_their_own_merge_tasks(tmp_path, monkeypatch):
    editor_request = _request(tmp_path, monkeypatch)
    editor_task = _queued(editor_request, "editor", monkeypatch)
    owner_request = editor_request.model_copy(update={"submission_id": "owner-merge-submission"})
    owner_task = _queued(owner_request, "owner", monkeypatch)

    assert api.cancel(editor_task.task_id, "editor").can_cancel is True
    assert api.cancel(owner_task.task_id, "owner").can_cancel is True
    _cancelled(editor_task.task_id)
    _cancelled(owner_task.task_id)

    first = api.retry(editor_task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor")
    second = api.retry(editor_task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor")
    assert first.task_id == second.task_id == editor_task.task_id
    assert api.retry(owner_task.task_id, api._RetryRequest(retry_request_id="owner-retry"), "owner").task_id == owner_task.task_id
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == editor_task.task_id)) == 2


def test_merge_task_observation_is_shared_but_actions_remain_with_original_owner(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add_all((
            metadb.User(id="viewer", name="Viewer"),
            metadb.User(id="stranger", name="Stranger"),
            metadb.CanvasShare(canvas_id="canvas", user_id="viewer", role="viewer"),
        ))

    task = _queued(request, "editor", monkeypatch)
    assert metadb.durable_task(task.task_id, include_admission=False)["target_node_id"] == "write"

    owner_view = api.status(task.task_id, "owner")
    editor_view = api.status(task.task_id, "editor")
    viewer_view = api.status(task.task_id, "viewer")
    assert owner_view.task_id == editor_view.task_id == viewer_view.task_id == task.task_id
    assert (editor_view.can_cancel, editor_view.can_retry) == (True, False)
    assert (owner_view.can_cancel, owner_view.can_retry) == (False, False)
    assert (viewer_view.can_cancel, viewer_view.can_retry) == (False, False)
    assert editor_view.merge_columns is not None
    assert owner_view.merge_columns is not None
    assert viewer_view.merge_columns is not None
    assert editor_view.merge_columns.can_cancel is True
    assert owner_view.merge_columns.can_cancel is False
    assert viewer_view.merge_columns.can_cancel is False

    for uid, expected in (("editor", True), ("owner", False), ("viewer", False)):
        job = next(item for item in metadb.list_workspace_runs(uid, run_id=task.task_id)["items"]
                   if item["taskId"] == task.task_id)
        assert job["targetNodeId"] == "write"
        assert job["canCancel"] is expected and job["canRetry"] is False
        assert job["mergeColumns"]["canCancel"] is expected
        assert job["mergeColumns"]["canRetry"] is False

    for uid in ("owner", "viewer", "stranger"):
        with pytest.raises(APIError) as cancel_error:
            api.cancel(task.task_id, uid)
        assert cancel_error.value.status_code == 404
    _cancelled(task.task_id)
    for uid in ("owner", "viewer", "stranger"):
        with pytest.raises(APIError) as retry_error:
            api.retry(task.task_id, api._RetryRequest(retry_request_id=f"{uid}-retry"), uid)
        assert retry_error.value.status_code == 404
    assert api.retry(task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor").task_id == task.task_id

    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
    for uid in ("editor", "stranger"):
        with pytest.raises(APIError) as status_error:
            api.status(task.task_id, uid)
        assert status_error.value.status_code == 404
        assert not metadb.list_workspace_runs(uid, run_id=task.task_id)["items"]


def test_revoked_editor_cannot_cancel_or_retry_and_leaves_task_state_unchanged(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    queued = _queued(request, "editor", monkeypatch)
    _cancelled(queued.task_id)
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)

    with pytest.raises(APIError) as retry_error:
        api.retry(queued.task_id, api._RetryRequest(retry_request_id="revoked-retry"), "editor")
    assert retry_error.value.status_code == 404
    with pytest.raises(APIError) as cancel_error:
        api.cancel(queued.task_id, "editor")
    assert cancel_error.value.status_code == 404
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, queued.task_id)
        assert task is not None
        assert (task.status, task.cancel_requested, task.retry_count) == ("cancelled", False, 0)
        assert session.scalar(select(func.count()).select_from(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == queued.task_id)) == 1


def test_workspace_editor_can_submit_cancel_and_retry_merge_task(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
        canvas = session.get(metadb.Canvas, "canvas")
        assert canvas is not None
        canvas.visibility = "workspace"

    queued = _queued(request, "editor", monkeypatch)
    assert api.cancel(queued.task_id, "editor").task_id == queued.task_id
    _cancelled(queued.task_id)
    assert api.retry(queued.task_id, api._RetryRequest(retry_request_id="workspace-retry"), "editor").task_id == queued.task_id


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"),
                    reason="requires dedicated PostgreSQL")
def test_postgres_merge_api_revoke_serializes_admission_and_blocks_later_dispatch(
        tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex
    owner, editor, canvas = (f"owner-{suffix}", f"editor-{suffix}", f"canvas-{suffix}")
    request = _request(
        tmp_path, monkeypatch, owner_id=owner, editor_id=editor, canvas_id=canvas,
        submission_id=f"submission-{suffix}")
    dispatched: list[str] = []
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: dispatched.append(task_id))
    about_to_admit = threading.Event()
    revoke_locked = threading.Event()
    release_revoke = threading.Event()
    original_submit = metadb.submit_merge_columns_task

    def gate_admission(**kwargs):
        about_to_admit.set()
        assert revoke_locked.wait(timeout=10)
        return original_submit(**kwargs)

    def revoke_editor():
        assert about_to_admit.wait(timeout=10)
        with metadb.session() as session:
            locked_canvas = session.get(metadb.Canvas, canvas, with_for_update=True)
            assert locked_canvas is not None
            share = session.scalar(select(metadb.CanvasShare).where(
                metadb.CanvasShare.canvas_id == canvas,
                metadb.CanvasShare.user_id == editor,
            ).with_for_update())
            assert share is not None
            session.delete(share)
            revoke_locked.set()
            assert release_revoke.wait(timeout=10)

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", gate_admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = pool.submit(api.submit, request, editor)
        revoked = pool.submit(revoke_editor)
        assert revoke_locked.wait(timeout=10)
        release_revoke.set()
        revoked.result(timeout=10)
        with pytest.raises(APIError) as admission_error:
            admitted.result(timeout=10)
    assert admission_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask).where(
            metadb.DurableTask.owner_id == editor,
            metadb.DurableTask.canvas_id == canvas,
        )) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput).where(
            metadb.SparseOutput.canvas_id == canvas,
        )) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization).join(
            metadb.SparseOutput,
            metadb.SparseOutput.id == metadb.SparseOutputMaterialization.sparse_id,
        ).where(metadb.SparseOutput.canvas_id == canvas)) == 0
    with pytest.raises(APIError) as replay_error:
        api.submit(request, editor)
    assert replay_error.value.status_code == 404
    assert dispatched == []
