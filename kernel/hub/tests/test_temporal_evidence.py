"""Ground-truth and fail-closed checks for bounded compound temporal evidence."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hub.compound_datasets import open_compound_manifest
from hub.api_errors import APIError
from hub.models import TemporalEvidenceRequestV1, TemporalEvidenceResponseV1
from hub.temporal_evidence import (
    MAX_OBSERVATIONS_PER_STREAM, EvidenceRequest, EvidenceWindow, TemporalEvidenceError,
    compute_temporal_evidence,
)


class _FixtureReader:
    def __init__(self, root):
        self.root = root
        self.calls: list[int] = []
        self.fail: Exception | None = None

    def read(self, *, dataset_id, revision_id, fields, stream_id, episode_id, episode_id_field,
             tick_field, start_tick_field, end_tick_field, source_start, source_end, limit):
        self.calls.append(limit)
        if self.fail:
            raise self.fail
        filename = dataset_id.removeprefix("fixture.compound.") + ".csv"
        with (self.root / "compound" / filename).open(newline="") as source:
            rows = list(csv.DictReader(source))
        result = []
        for row in rows:
            if row[episode_id_field] != episode_id:
                continue
            for field in (tick_field, start_tick_field, end_tick_field):
                if field:
                    row[field] = int(row[field])
            if tick_field and not (source_start <= row[tick_field] < source_end):
                continue
            if start_tick_field and (row[end_tick_field] <= source_start or row[start_tick_field] >= source_end):
                continue
            result.append({field: row[field] for field in fields})
        return result[:limit]


@pytest.fixture()
def fixture_compound(tmp_path):
    subprocess.run([sys.executable, "scripts/build_ux_fixtures.py", "--output", str(tmp_path)],
                   cwd=str(__import__("pathlib").Path(__file__).parents[3]), check=True)
    manifest = open_compound_manifest((tmp_path / "compound" / "manifest.json").read_bytes())
    return tmp_path, manifest, json.loads((tmp_path / "compound" / "ground-truth.json").read_text())


def _request(*, tolerance=1, gap_threshold=3_003):
    return EvidenceRequest(
        episode_id="episode-1", stream_ids=("numeric-sensor", "video"),
        pair=("numeric-sensor", "video"), tolerance_ticks=tolerance, gap_threshold_ticks=gap_threshold,
        window=EvidenceWindow("reference-ms", 0, 10_000),
    )


def test_fixture_ground_truth_is_recomputed_from_real_member_observations(fixture_compound):
    root, manifest, ground_truth = fixture_compound
    evidence = compute_temporal_evidence(manifest, _request(), _FixtureReader(root))
    sensor, video = evidence["streams"]
    expected = next(item for item in ground_truth["coverage"]
                    if item["episodeId"] == "episode-1" and item["streamId"] == "numeric-sensor")
    assert sensor["observedCount"] == expected["observationCount"]
    assert sensor["firstTick"] == expected["firstReferenceTick"]
    assert sensor["lastTick"] == expected["lastReferenceTick"]
    assert sensor["gaps"][0]["durationTicks"] == expected["gaps"][0]["durationReferenceTicks"]
    assert video["state"] == "available"
    assert evidence["pair"]["overlapTicks"] == ground_truth["overlap"]["durationReferenceTicks"]
    assert evidence["identity"]["compoundRevision"] == ground_truth["manifest"]["revisionId"]


def test_tolerance_and_explicit_gap_threshold_change_identity_and_keep_boundary_visible(fixture_compound):
    root, manifest, _ground_truth = fixture_compound
    first = compute_temporal_evidence(manifest, _request(tolerance=0), _FixtureReader(root))
    second = compute_temporal_evidence(manifest, _request(tolerance=1), _FixtureReader(root))
    assert first["identity"]["evidenceId"] != second["identity"]["evidenceId"]
    boundary = compute_temporal_evidence(manifest, _request(gap_threshold=4_004), _FixtureReader(root))
    assert first["identity"]["evidenceId"] != boundary["identity"]["evidenceId"]
    assert len(boundary["streams"][0]["coverageIntervals"]) == 2
    assert boundary["streams"][0]["gaps"][0]["thresholdTicks"] == 4_004


def test_absent_provider_failure_truncation_and_prescan_cap_are_distinct(fixture_compound):
    root, manifest, _ground_truth = fixture_compound
    absent = compute_temporal_evidence(
        manifest, EvidenceRequest("episode-2", ("video",), EvidenceWindow("reference-ms", 20_000, 27_000), 1),
        _FixtureReader(root))
    assert absent["streams"][0]["state"] == "absent"
    broken = _FixtureReader(root); broken.fail = ConnectionError()
    unavailable = compute_temporal_evidence(manifest, _request(), broken)
    assert unavailable["streams"][0]["state"] == "unavailable"
    assert broken.calls[0] == MAX_OBSERVATIONS_PER_STREAM + 1


def test_unknown_mapping_and_bounds_fail_before_any_member_read(fixture_compound):
    root, manifest, _ground_truth = fixture_compound
    reader = _FixtureReader(root)
    unknown = compute_temporal_evidence(
        manifest, EvidenceRequest("episode-1", ("numeric-sensor",), EvidenceWindow("other", 0, 1), 1), reader)
    assert unknown["streams"][0]["state"] == "unknown"
    assert reader.calls == []
    with pytest.raises(TemporalEvidenceError):
        compute_temporal_evidence(
            manifest, EvidenceRequest("episode-1", tuple(str(i) for i in range(9)),
                                      EvidenceWindow("reference-ms", 0, 1), 1), reader)
    assert reader.calls == []


def test_nearest_tie_breaks_by_timestamp_then_observation_id_not_input_order():
    from hub.temporal_evidence import _nearest
    matched, left, right, summary, comparisons = _nearest(
        [("left", 10, 10)], [("z-last", 9, 9), ("a-first", 11, 11)], 1)
    assert (matched, left, right, summary, comparisons) == (1, 0, 1, {
        "count": 1, "minimum": 1, "maximum": 1,
        "tieBreak": "distance,startTick,endTick,observationId",
        "rightReuse": True,
    }, 2)
    assert _nearest([("left", 10, 10)], [("a-first", 11, 11), ("z-last", 9, 9)], 1) == (
        matched, left, right, summary, comparisons)


def test_interval_evidence_is_clipped_and_nearest_is_half_open_interval_aware():
    from hub.temporal_evidence import _coverage, _nearest, _normalize_rows
    facts, corrupt = _normalize_rows(
        [{"id": "interval", "start": -3, "end": 12}], "id", None, "start", "end", None,
        EvidenceWindow("reference", 0, 10))
    assert (facts, corrupt) == ([("interval", 0, 10)], 0)
    matched, unmatched_left, unmatched_right, summary, _comparisons = _nearest(
        [("point", 5, 5)], [("interval", 0, 10)], 0)
    assert (matched, unmatched_left, unmatched_right, summary["minimum"]) == (1, 0, 0, 0)
    # End is exclusive: tick 10 is one discrete tick away from [0, 10).
    assert _nearest([("point", 10, 10)], [("interval", 0, 10)], 0)[0] == 0
    duplicate, duplicate_corrupt = _normalize_rows(
        [{"id": "same", "tick": 1}, {"id": "same", "tick": 2}], "id", "tick", None, None, None,
        EvidenceWindow("reference", 0, 10))
    reverse_duplicate, reverse_corrupt = _normalize_rows(
        [{"id": "same", "tick": 2}, {"id": "same", "tick": 1}], "id", "tick", None, None, None,
        EvidenceWindow("reference", 0, 10))
    assert (duplicate, duplicate_corrupt) == ([], 2)
    assert (reverse_duplicate, reverse_corrupt) == (duplicate, duplicate_corrupt)
    _intervals, gaps = _coverage([("outer", 0, 20), ("nested", 5, 10), ("next", 30, 31)], 5)
    assert gaps[0]["afterObservationId"] == "outer"


def test_fixture_named_mapping_alignment_matches_frozen_ground_truth(fixture_compound):
    from hub.temporal_evidence import _nearest, _normalize_rows
    root, manifest, ground_truth = fixture_compound
    binding = next(item for item in manifest.bindings
                   if item.episode_id == "episode-1" and item.stream_id == "numeric-sensor")
    member = next(item for item in manifest.members if item.id == binding.member_id)
    index = binding.observation_index
    assert index is not None
    facts, corrupt = [], 0
    for episode_id in ("episode-1", "episode-2"):
        rows = _FixtureReader(root).read(
            dataset_id=member.dataset_id, revision_id=member.revision_id,
            fields=(index.observation_id_field, index.tick_field), stream_id="numeric-sensor",
            episode_id=episode_id, episode_id_field=index.episode_id_field, tick_field=index.tick_field,
            start_tick_field=None, end_tick_field=None, source_start=0, source_end=10**12, limit=10_001)
        episode_facts, episode_corrupt = _normalize_rows(
            rows, index.observation_id_field, index.tick_field, None, None, manifest.clock_mappings[0],
            EvidenceWindow("reference-ms", 0, 27_001))
        facts.extend(episode_facts)
        corrupt += episode_corrupt
    facts.sort(key=lambda item: (item[1], item[2], item[0]))
    targets = [(f"reference-{tick}", tick, tick) for tick in ground_truth["alignment"]["referenceTicks"]]
    matched, unmatched_left, unmatched_right, summary, _comparisons = _nearest(facts, targets, 1)
    assert corrupt == 0
    assert (matched, unmatched_left, unmatched_right) == (
        ground_truth["alignment"]["matchedCount"], ground_truth["alignment"]["unmatchedSensorCount"],
        ground_truth["alignment"]["unmatchedReferenceCount"])
    assert summary["maximum"] == 0


def test_response_dto_redacts_raw_facts_and_rejects_uncontracted_fields(fixture_compound):
    root, manifest, _ground_truth = fixture_compound
    document = compute_temporal_evidence(manifest, _request(), _FixtureReader(root))
    response = TemporalEvidenceResponseV1.model_validate(document)
    assert response.identity.compound_dataset_id == manifest.ref.dataset_id
    assert "_facts" not in response.model_dump(by_alias=True)["streams"][0]
    with pytest.raises(ValueError):
        TemporalEvidenceResponseV1.model_validate({**document, "rawObservation": {}})


def test_corrupt_pair_member_returns_unknown_without_fabricated_counts(fixture_compound):
    root, manifest, _ground_truth = fixture_compound

    class CorruptReader(_FixtureReader):
        def read(self, **kwargs):
            rows = super().read(**kwargs)
            return rows + rows[:1] if kwargs["stream_id"] == "numeric-sensor" else rows

    evidence = compute_temporal_evidence(manifest, _request(), CorruptReader(root))
    assert evidence["streams"][0]["state"] == "corrupt"
    assert evidence["pair"] == {
        "state": "unknown", "complete": False, "reason": "pair member is incomplete",
        "unknownCount": None,
    }


def test_route_rejects_view_time_field_not_matching_manifest_index(fixture_compound, monkeypatch):
    from hub.routers import temporal_evidence as route
    root, manifest, _ground_truth = fixture_compound
    sensor = next(item for item in manifest.members if item.id == "sensor-observations")
    view = SimpleNamespace(
        id="view-1", dataset_ref=SimpleNamespace(dataset_id=sensor.dataset_id, revision_id=sensor.revision_id),
        sampling=SimpleNamespace(kind="all"),
        temporal_window=SimpleNamespace(time_field="wrong", time_domain="sensor-device-us",
                                        start_tick=0, end_tick=10_000_000),
        selected_columns=["observation_id", "episode_id", "device_tick"],
        definition_sha256="a" * 64, semantic_sha256="b" * 64,
    )
    monkeypatch.setattr(route, "_stored_definition", lambda _uid, _view_id: view)
    request = TemporalEvidenceRequestV1(
        manifestJson=(root / "compound" / "manifest.json").read_text(), episodeId="episode-1",
        streamIds=["numeric-sensor"], streamViews=[{"streamId": "numeric-sensor", "datasetViewId": "view-1"}],
        referenceViewId="view-1", gapThresholdTicks="1",
    )
    with pytest.raises(APIError, match="time field"):
        route.temporal_evidence(request, "user")


def test_route_rejects_view_window_that_cannot_cover_reference_evidence(fixture_compound, monkeypatch):
    from hub.routers import temporal_evidence as route
    root, manifest, _ground_truth = fixture_compound
    sensor = next(item for item in manifest.members if item.id == "sensor-observations")
    annotations = next(item for item in manifest.members if item.id == "interval-annotations")
    sensor_view = SimpleNamespace(
        id="sensor-view", dataset_ref=SimpleNamespace(dataset_id=sensor.dataset_id, revision_id=sensor.revision_id),
        sampling=SimpleNamespace(kind="all"),
        temporal_window=SimpleNamespace(time_field="device_tick", time_domain="sensor-device-us",
                                        start_tick=1_000_000, end_tick=2_000_000),
        selected_columns=["observation_id", "episode_id", "device_tick"],
        definition_sha256="a" * 64, semantic_sha256="b" * 64,
    )
    annotation_view = SimpleNamespace(
        id="annotation-view", dataset_ref=SimpleNamespace(dataset_id=annotations.dataset_id,
                                                             revision_id=annotations.revision_id),
        sampling=SimpleNamespace(kind="all"),
        temporal_window=SimpleNamespace(time_field="start_tick", time_domain="reference-ms",
                                        start_tick=0, end_tick=10_000),
        selected_columns=["observation_id", "episode_id", "start_tick", "end_tick"],
        definition_sha256="c" * 64, semantic_sha256="d" * 64,
    )
    monkeypatch.setattr(route, "_stored_definition", lambda _uid, view_id: {
        "sensor-view": sensor_view, "annotation-view": annotation_view}[view_id])
    request = TemporalEvidenceRequestV1(
        manifestJson=(root / "compound" / "manifest.json").read_text(), episodeId="episode-1",
        streamIds=["numeric-sensor", "interval-annotation"], streamViews=[
            {"streamId": "numeric-sensor", "datasetViewId": "sensor-view"},
            {"streamId": "interval-annotation", "datasetViewId": "annotation-view"},
        ], referenceViewId="annotation-view", gapThresholdTicks="1",
    )
    with pytest.raises(APIError, match="does not cover"):
        route.temporal_evidence(request, "user")


def test_route_allows_pair_with_declared_absent_stream_and_no_fake_view(fixture_compound, monkeypatch):
    from hub.routers import temporal_evidence as route
    root, manifest, _ground_truth = fixture_compound
    sensor = next(item for item in manifest.members if item.id == "sensor-observations")
    view = SimpleNamespace(
        id="sensor-view", dataset_ref=SimpleNamespace(dataset_id=sensor.dataset_id,
                                                         revision_id=sensor.revision_id),
        sampling=SimpleNamespace(kind="all"),
        temporal_window=SimpleNamespace(time_field="device_tick", time_domain="sensor-device-us",
                                        start_tick=20_000_000, end_tick=27_000_000),
        selected_columns=["observation_id", "episode_id", "device_tick"],
        definition_sha256="a" * 64, semantic_sha256="b" * 64,
    )
    monkeypatch.setattr(route, "_stored_definition", lambda _uid, _view_id: view)
    monkeypatch.setattr(route, "_CatalogObservationReader", lambda _views: _FixtureReader(root))
    request = TemporalEvidenceRequestV1(
        manifestJson=(root / "compound" / "manifest.json").read_text(), episodeId="episode-2",
        streamIds=["numeric-sensor", "video"],
        streamViews=[{"streamId": "numeric-sensor", "datasetViewId": "sensor-view"}],
        referenceViewId="sensor-view", gapThresholdTicks="1",
        pair={"leftStreamId": "numeric-sensor", "rightStreamId": "video"},
    )
    response = route.temporal_evidence(request, "user")
    assert response.streams[1].state == "absent"
    assert response.pair is not None and response.pair.state == "unknown"
    assert response.pair.matched_count is response.pair.unmatched_left_count is None


def test_tolerance_contract_is_nonnegative_int64_in_validation_schema_and_service(fixture_compound):
    base = {
        "manifestJson": "{}", "episodeId": "episode", "streamIds": ["stream"],
        "streamViews": [{"streamId": "stream", "datasetViewId": "view"}],
        "referenceViewId": "view", "gapThresholdTicks": "1",
    }
    with pytest.raises(ValueError):
        TemporalEvidenceRequestV1.model_validate({**base, "toleranceTicks": "-1"})
    tolerance = TemporalEvidenceRequestV1.model_json_schema(by_alias=True)["properties"]["toleranceTicks"]
    assert tolerance["pattern"] == r"^(?:0|[1-9][0-9]*)$"
    assert tolerance["description"] == "Canonical nonnegative signed-int64 decimal string."
    maximum = (1 << 63) - 1
    assert TemporalEvidenceRequestV1.model_validate(
        {**base, "toleranceTicks": str(maximum)}).tolerance_ticks == str(maximum)
    with pytest.raises(ValueError):
        TemporalEvidenceRequestV1.model_validate({**base, "toleranceTicks": str(maximum + 1)})
    root, manifest, _ground_truth = fixture_compound
    assert compute_temporal_evidence(
        manifest, _request(tolerance=maximum), _FixtureReader(root))["pair"]["toleranceTicks"] == maximum
    with pytest.raises(TemporalEvidenceError):
        compute_temporal_evidence(manifest, _request(tolerance=maximum + 1), _FixtureReader(root))


def test_pair_nearest_work_stays_linearish_without_truncating_normal_windows():
    from hub.temporal_evidence import _nearest
    left = [(f"left-{i}", i * 30, i * 30) for i in range(1_000)]
    right = [(f"right-{i}", i * 30 + 1, i * 30 + 1) for i in range(1_000)]
    result = _nearest(left, right, 1)
    assert result is not None
    assert result[-1] < 2_000
    assert _nearest(left, right, 1, budget=1) is None


def test_nearest_finds_nested_long_interval_and_reuses_deterministic_same_tick_target():
    from hub.temporal_evidence import _nearest
    nested = _nearest([("point", 90, 90)], [("long", 0, 100), ("nested", 50, 60)], 0)
    assert nested is not None and nested[:3] == (1, 0, 1)
    reused = _nearest(
        [("left-10", 10, 10), ("left-11", 11, 11)],
        [("z", 10, 10), ("a", 10, 10)], 1)
    assert reused is not None
    assert reused[:3] == (2, 0, 1)


def test_closed_tick_index_handles_empty_ties_and_ten_thousand_bounded_facts():
    from hub.temporal_evidence import _nearest
    assert _nearest([("left", 1, 1)], [], 0)[:3] == (0, 1, 0)
    # Same closed prior end and same following start use the original (start, end, id) ordering.
    prior = _nearest([("left", 10, 10)], [("z", 5, 10), ("a", 5, 10)], 1)
    following = _nearest([("left", 0, 0)], [("z", 1, 2), ("a", 1, 2)], 1)
    assert prior is not None and following is not None
    assert prior[:3] == (1, 0, 1)
    assert following[:3] == (1, 0, 1)
    left = [(f"left-{index}", index, index) for index in range(10_000)]
    right = [(f"right-{index}", index + 1, index + 1) for index in range(10_000)]
    result = _nearest(left, right, 1)
    assert result is not None and result[-1] <= 20_000


def test_candidate_budget_checks_exact_slot_boundary_before_materialization():
    from hub.temporal_evidence import _nearest, _pair_evidence
    left = [(f"left-{index}", 1, 1) for index in range(10_000)]
    right = [("prior", 0, 0), ("following", 2, 2)]
    exact = _nearest(left, right, 1, budget=20_000)
    assert exact is not None and exact[-1] == 20_000
    assert _nearest(left, right, 1, budget=19_999) is None
    items = {
        "left": {"state": "available", "complete": True, "_point": True,
                 "_facts": left, "firstTick": 1, "lastTick": 1, "coverageIntervals": [(1, 1)]},
        "right": {"state": "available", "complete": True, "_point": True,
                  "_facts": right, "firstTick": 0, "lastTick": 2, "coverageIntervals": [(0, 2)]},
    }
    request = EvidenceRequest("episode", ("left", "right"), EvidenceWindow("clock", 0, 3), 1,
                              pair=("left", "right"), tolerance_ticks=1)
    assert _pair_evidence(items, request)["state"] == "truncated"
