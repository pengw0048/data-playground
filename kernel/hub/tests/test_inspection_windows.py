"""Acceptance tests for the one bounded, redacted compound inspection read."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def inspection_workspace(tmp_path):
    from hub import deps as deps_module
    from hub import metadb
    from hub.deps import set_workspace

    previous = deps_module._deps
    root = tmp_path / "workspace"
    metadb.init_db()
    set_workspace(str(root), str(root / "data"))
    try:
        yield root
    finally:
        deps_module._deps = previous


def _request(client: TestClient, *, episode="episode-1", streams=None, revision=None):
    detail = client.get("/api/compound-datasets/reference").json()
    revision = revision or detail["revisionId"]
    payload = {
        "episodeId": episode,
        "startTick": "0" if episode == "episode-1" else "20000",
        "endTick": "10000" if episode == "episode-1" else "27000",
        "streamIds": streams or ["numeric-sensor", "interval-annotation", "video"],
        "gapThresholdTicks": "3000",
        "toleranceTicks": "1",
    }
    if len(payload["streamIds"]) > 1:
        payload["pair"] = {"leftStreamId": payload["streamIds"][0],
                           "rightStreamId": payload["streamIds"][-1]}
    response = client.post(
        f"/api/compound-datasets/{detail['datasetId']}/revisions/{revision}/inspection-window",
        json=payload,
    )
    return detail, payload, response


def test_inspection_window_returns_exact_mapped_points_intervals_and_asset_refs(inspection_workspace):
    from hub.main import app

    with TestClient(app) as client:
        detail, _payload, response = _request(client)
    assert response.status_code == 200
    document = response.json()
    assert document["identity"] == {
        "compoundDatasetId": detail["datasetId"], "compoundRevision": detail["revisionId"],
        "episodeId": "episode-1", "referenceClockId": "reference-ms", "startTick": 0,
        "endTick": 10000, "streamIds": ["numeric-sensor", "interval-annotation", "video"],
    }
    assert document["limits"] == {"maxRowsPerStream": 10_000, "maxRawBytesPerStream": 1_000_000}
    raw = {item["streamId"]: item for item in document["observations"]}
    assert raw["numeric-sensor"]["state"] == "present"
    assert [(column["name"], column["type"], column["provenance"]) for column in raw["numeric-sensor"]["columns"]] == [
        ("observation_id", "string", "declared"), ("episode_id", "string", "declared"),
        ("device_tick", "int64", "declared"), ("value", "float64", "declared"),
    ]
    assert [item["startTick"] for item in raw["numeric-sensor"]["observations"]] == [
        876, 1877, 2878, 6882, 7883, 8884,
    ]
    assert raw["interval-annotation"]["observations"] == [
        {"observationId": "episode-1-annotation-001", "kind": "interval", "startTick": 1000,
         "endTick": 2500, "values": {"fixture_phase": "protocol-a"}, "assets": []},
        {"observationId": "episode-1-annotation-002", "kind": "interval", "startTick": 6000,
         "endTick": 8000, "values": {"fixture_phase": "protocol-b"}, "assets": []},
    ]
    video = raw["video"]["observations"]
    assert video[0]["assets"] == [{
        "id": "flower-webm", "mediaType": "video/webm", "byteLength": 554058,
        "sha256": "c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda",
        "status": "available",
    }]
    sensor_evidence = next(item for item in document["evidence"]["streams"]
                           if item["streamId"] == "numeric-sensor")
    assert sensor_evidence["gaps"] == [{
        "afterObservationId": "episode-1-sensor-003", "beforeObservationId": "episode-1-sensor-004",
        "durationTicks": 4004, "thresholdTicks": 3000,
    }]
    assert document["evidence"]["pair"]["nearestDelta"]["tieBreak"] == "distance,startTick,endTick,observationId"
    rendered = json.dumps(document)
    assert str(inspection_workspace) not in rendered
    assert all(forbidden not in rendered for forbidden in ("manifestJson", "datasetView", "fixture://"))


def test_inspection_window_reports_absence_restarts_and_stale_revisions(inspection_workspace):
    from hub import deps as deps_module
    from hub.deps import set_workspace
    from hub.main import app

    with TestClient(app) as client:
        detail, payload, first = _request(client)
        absent_detail, _absent_payload, absent = _request(client, episode="episode-2", streams=["video"])
        stale_detail, _stale_payload, stale = _request(client, revision="0" * 64)
    assert first.status_code == 200
    assert absent_detail == detail == stale_detail
    assert absent.status_code == 200
    absent_stream = absent.json()["observations"][0]
    assert (absent_stream["streamId"], absent_stream["state"], absent_stream["complete"],
            absent_stream["observations"]) == ("video", "absent", True, [])
    assert [(column["name"], column["type"]) for column in absent_stream["columns"]] == [
        ("observation_id", "string"), ("episode_id", "string"), ("start_tick", "int64"),
        ("end_tick", "int64"), ("asset_id", "string"),
    ]
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"

    restarted = set_workspace(str(inspection_workspace), str(inspection_workspace / "data"))
    assert restarted is deps_module._deps
    with TestClient(app) as client:
        replay = client.post(
            f"/api/compound-datasets/{detail['datasetId']}/revisions/{detail['revisionId']}/inspection-window",
            json=payload,
        )
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_inspection_window_marks_byte_caps_asset_loss_and_member_failures_truthfully(
    inspection_workspace, monkeypatch,
):
    from hub.main import app
    from hub.routers import inspection_windows

    monkeypatch.setattr(inspection_windows, "_RAW_BYTES_PER_STREAM", 1)
    with TestClient(app) as client:
        _detail, _payload, capped = _request(client, streams=["numeric-sensor"])
    assert capped.status_code == 200
    assert capped.json()["limits"]["maxRawBytesPerStream"] == 1
    assert capped.json()["complete"] is False
    assert capped.json()["observations"][0]["state"] == "truncated"
    assert capped.json()["observations"][0]["observations"] == []

    monkeypatch.setattr(inspection_windows, "_RAW_BYTES_PER_STREAM", 1_000_000)
    with TestClient(app) as client:
        detail, _payload, _response = _request(client)
    (inspection_workspace / "data" / "compound-fixture-v1" / "flower.webm").unlink()
    with TestClient(app) as client:
        asset_lost = client.post(
            f"/api/compound-datasets/{detail['datasetId']}/revisions/{detail['revisionId']}/inspection-window",
            json={"episodeId": "episode-1", "startTick": "0", "endTick": "10000", "streamIds": ["video"],
                  "gapThresholdTicks": "3000"},
        )
    assert asset_lost.status_code == 200
    video = asset_lost.json()["observations"][0]
    assert (video["state"], video["complete"], video["observations"][0]["assets"][0]["status"]) == (
        "partial", False, "unavailable",
    )

    def unavailable(*_args, **_kwargs):
        raise ConnectionError

    monkeypatch.setattr(inspection_windows, "_rows", unavailable)
    with TestClient(app) as client:
        _detail, _payload, unavailable_response = _request(client, streams=["numeric-sensor"])
    assert unavailable_response.status_code == 200
    assert unavailable_response.json()["observations"][0]["state"] == "unavailable"


def test_inspection_window_marks_row_caps_corruption_and_permission_loss(inspection_workspace, monkeypatch):
    from hub.main import app
    from hub.routers import inspection_windows

    def too_many_rows(*_args, **_kwargs):
        for index in range(10_001):
            yield {"observation_id": f"synthetic-{index}", "episode_id": "episode-1",
                   "device_tick": (index + 1) * 1_000_000, "value": float(index)}

    monkeypatch.setattr(inspection_windows, "_rows", too_many_rows)
    with TestClient(app) as client:
        _detail, _payload, capped = _request(client, streams=["numeric-sensor"])
    assert capped.status_code == 200
    assert capped.json()["observations"][0]["state"] == "truncated"

    def corrupt_row(*_args, **_kwargs):
        yield {"observation_id": "bad", "episode_id": "episode-1", "device_tick": "not-a-tick", "value": 1.0}

    monkeypatch.setattr(inspection_windows, "_rows", corrupt_row)
    with TestClient(app) as client:
        _detail, _payload, corrupt = _request(client, streams=["numeric-sensor"])
    assert corrupt.status_code == 200
    assert corrupt.json()["observations"][0]["state"] == "corrupt"

    def permission_lost(*_args, **_kwargs):
        raise PermissionError

    monkeypatch.setattr(inspection_windows, "_rows", permission_lost)
    with TestClient(app) as client:
        _detail, _payload, permission = _request(client, streams=["numeric-sensor"])
    assert permission.status_code == 200
    assert permission.json()["observations"][0]["state"] == "permission"


def test_inspection_window_propagates_evidence_corruption_and_truncation(inspection_workspace, monkeypatch):
    from hub.main import app
    from hub.routers import inspection_windows

    original = inspection_windows._CatalogObservationReader.read

    def corrupt_evidence(self, **kwargs):
        if kwargs["stream_id"] == "numeric-sensor":
            return [{"observation_id": "bad", "episode_id": "episode-1", "device_tick": "not-a-tick"}]
        return original(self, **kwargs)

    monkeypatch.setattr(inspection_windows._CatalogObservationReader, "read", corrupt_evidence)
    with TestClient(app) as client:
        _detail, _payload, corrupt = _request(client, streams=["numeric-sensor"])
    corrupt_stream = corrupt.json()["observations"][0]
    assert (corrupt_stream["state"], corrupt_stream["complete"], corrupt_stream["reason"],
            bool(corrupt_stream["observations"])) == (
        "partial", False, "temporal evidence contains corrupt observations", True,
    )

    def truncated_evidence(self, **kwargs):
        if kwargs["stream_id"] == "numeric-sensor":
            return [{"observation_id": f"cap-{index}", "episode_id": "episode-1", "device_tick": 1_000_000}
                    for index in range(10_001)]
        return original(self, **kwargs)

    monkeypatch.setattr(inspection_windows._CatalogObservationReader, "read", truncated_evidence)
    with TestClient(app) as client:
        _detail, _payload, truncated = _request(client, streams=["numeric-sensor"])
    truncated_stream = truncated.json()["observations"][0]
    assert (truncated_stream["state"], truncated_stream["complete"], truncated_stream["reason"],
            truncated_stream["observations"]) == ("truncated", False, "observation cap reached", [])


def test_inspection_window_does_not_mask_programming_failures_as_corruption(inspection_workspace, monkeypatch):
    from hub.main import app
    from hub.routers import inspection_windows

    def assertion_failure(*_args, **_kwargs):
        raise AssertionError("programming failure")

    monkeypatch.setattr(inspection_windows, "_rows", assertion_failure)
    with TestClient(app, raise_server_exceptions=False) as client:
        _detail, _payload, response = _request(client, streams=["numeric-sensor"])
    assert response.status_code == 500
