from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
from fractions import Fraction
from pathlib import Path

from hub.compound_datasets import map_tick, open_compound_manifest


def _load_gate():
    path = Path(__file__).resolve().parents[3] / "scripts" / "ux_release_gate.py"
    spec = importlib.util.spec_from_file_location("ux_release_gate_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_gate_blocks_open_p0_and_p1_ux_defects_but_not_tracking_or_prs():
    gate = _load_gate()
    issues = [
        {"number": 174, "title": "tracking", "labels": [{"name": "P1"}, {"name": "ux"}]},
        {"number": 160, "title": "data loss", "labels": [{"name": "P0"}, {"name": "ux"}]},
        {"number": 164, "title": "stale preview", "labels": [{"name": "P1"}, {"name": "ux"}]},
        {"number": 173, "title": "responsive", "labels": [{"name": "P2"}, {"name": "ux"}]},
        {"number": 123, "title": "unrelated P1", "labels": [{"name": "P1"}, {"name": "api"}]},
        {"number": 999, "title": "labeled PR", "labels": [{"name": "P1"}, {"name": "ux"}],
         "pull_request": {"url": "https://api.github.test/pulls/999"}},
    ]

    assert [issue["number"] for issue in gate.blockers(issues)] == [160, 164]


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_fixture_builder_is_deterministic_and_full_profile_has_the_catalog_matrix(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts" / "build_ux_fixtures.py"
    spec = importlib.util.spec_from_file_location("build_ux_fixtures_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first, second = tmp_path / "first", tmp_path / "second"
    first_manifest = module.build(first, "full")
    second_manifest = module.build(second, "full")

    # The new compound fixture is self-contained: an attempted network connection is a test
    # failure, while two clean roots must reproduce every generated path and byte, not merely its
    # manifest. The legacy seeded Lance member remains outside this bounded fixture contract.
    def no_network(*args, **kwargs):
        raise AssertionError("fixture build attempted network access")

    monkeypatch.setattr("socket.create_connection", no_network)
    monkeypatch.setattr("socket.socket.connect", no_network)
    compound_first, compound_second = tmp_path / "compound-first", tmp_path / "compound-second"
    module._build_compound_timeline(compound_first)
    module._build_compound_timeline(compound_second)
    assert _tree(compound_first) == _tree(compound_second)
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    assert len(list(first.glob("catalog_*.csv"))) == 120
    assert len(list(first.glob("relationship_dense_*.csv"))) == 24


def test_compound_fixture_uses_the_public_contract_and_exact_ground_truth(tmp_path):
    path = Path(__file__).resolve().parents[3] / "scripts" / "build_ux_fixtures.py"
    spec = importlib.util.spec_from_file_location("build_compound_fixture_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / "fixture"
    module.build(root, "smoke")
    compound = root / "compound"
    ground_truth = json.loads((compound / "ground-truth.json").read_text(encoding="utf-8"))
    manifest = open_compound_manifest((compound / "manifest.json").read_bytes())

    def parse_cell(cell: str, field_type: str) -> None:
        assert cell != ""
        if field_type == "string":
            return
        if field_type == "int64":
            value = int(cell)
            assert -(1 << 63) <= value < (1 << 63)
            return
        if field_type == "float64":
            assert math.isfinite(float(cell))
            return
        raise AssertionError(f"fixture schema type is unsupported: {field_type}")

    assert manifest.digest == ground_truth["manifest"]["revisionId"]
    member_truth = {member["id"]: member for member in ground_truth["memberChecksums"]}
    members = {member.id: member for member in manifest.members}
    assert members.keys() == member_truth.keys()
    member_rows: dict[str, list[dict[str, str]]] = {}
    for member_id, member in members.items():
        truth = member_truth[member_id]
        path = root / truth["path"]
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == truth["headers"]
            member_rows[member_id] = list(reader)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == truth["sha256"]
        assert member.revision_id == truth["revisionId"] == truth["sha256"]
        assert member.schema_digest == hashlib.sha256(json.dumps(
            truth["schema"], separators=(",", ":")).encode("ascii")).hexdigest()
        assert [field[0] for field in truth["schema"]] == truth["headers"]
        for row in member_rows[member_id]:
            for name, field_type in truth["schema"]:
                parse_cell(row[name], field_type)

    streams = {stream.id: stream for stream in manifest.streams}
    present = [binding for binding in manifest.bindings if binding.state == "present"]
    absent = [binding for binding in manifest.bindings if binding.state == "absent"]
    assert len(present) + len(absent) == len(manifest.episodes) * len(manifest.streams)
    assert all(binding.member_id is None and not binding.asset_ids and binding.observation_index is None
               for binding in absent)
    binding_rows: dict[tuple[str, str], list[dict[str, str]]] = {}
    member_stream_schemas: dict[str, tuple[tuple[str, str, bool], ...]] = {}
    for binding in present:
        assert binding.member_id in members and binding.observation_index is not None
        index = binding.observation_index
        truth_schema = tuple((name, field_type) for name, field_type in member_truth[
            binding.member_id]["schema"])
        stream_schema = tuple((field.name, field.type, field.nullable)
                              for field in streams[binding.stream_id].observation_schema)
        assert truth_schema == tuple((name, field_type) for name, field_type, _ in stream_schema)
        assert all(nullable is False for _, _, nullable in stream_schema)
        previous_schema = member_stream_schemas.setdefault(binding.member_id, stream_schema)
        assert previous_schema == stream_schema
        headers = member_truth[binding.member_id]["headers"]
        required = [index.observation_id_field, index.episode_id_field, *index.value_refs]
        required.extend(field for field in (index.tick_field, index.start_tick_field, index.end_tick_field)
                        if field is not None)
        assert all(field in headers for field in required)
        rows = [row for row in member_rows[binding.member_id]
                if row[index.episode_id_field] == binding.episode_id]
        assert rows
        assert len({row[index.observation_id_field] for row in rows}) == len(rows)
        for row in rows:
            for name, field_type, nullable in stream_schema:
                cell = row[name]
                assert nullable or cell != ""
                if cell != "":
                    parse_cell(cell, field_type)
        binding_rows[(binding.episode_id, binding.stream_id)] = rows

    asset = ground_truth["assetProvenance"]
    assert asset == {
        "byteLength": 554_058,
        "license": "CC0-1.0",
        "mediaType": "video/webm",
        "modifications": "Unmodified upstream bytes; no transcoding or extraction. The video observation declares only source timeline [0, 2000) ms.",
        "path": "compound/flower.webm",
        "repositorySizeBudgetBytes": 600 * 1024,
        "sha256": "c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda",
        "sourceUrl": "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm",
    }
    assert hashlib.sha256((root / asset["path"]).read_bytes()).hexdigest() == asset["sha256"]
    mapping = manifest.clock_mappings[0]
    alignment = ground_truth["alignment"]
    assert (mapping.source_clock_id, mapping.target_clock_id, mapping.scale_numerator,
            mapping.scale_denominator, mapping.offset_tick) == (
        alignment["sourceClockId"], alignment["targetClockId"], alignment["scaleNumerator"],
        alignment["scaleDenominator"], alignment["offsetTick"],
    )
    source = next(stream.clock.tick_unit for stream in streams.values()
                  if stream.clock.id == mapping.source_clock_id)
    target = next(stream.clock.tick_unit for stream in streams.values()
                  if stream.clock.id == mapping.target_clock_id)
    observed_scale = Fraction(mapping.scale_numerator, mapping.scale_denominator)
    nominal_scale = Fraction(source.numerator, source.denominator) / Fraction(
        target.numerator, target.denominator)
    assert int((observed_scale / nominal_scale - 1) * 1_000_000) == alignment["declaredDriftPpm"]

    calculated_coverage = []
    sensor_points: dict[str, list[tuple[str, int]]] = {}
    target_points: dict[str, list[tuple[str, int]]] = {}
    video_intervals: dict[str, list[tuple[int, int]]] = {}
    for binding in manifest.bindings:
        entry = {"episodeId": binding.episode_id, "streamId": binding.stream_id, "state": binding.state}
        if binding.state == "absent":
            entry.update(observationCount=0, observations=[], gaps=[])
            calculated_coverage.append(entry)
            continue
        index = binding.observation_index
        assert index is not None
        rows = binding_rows[(binding.episode_id, binding.stream_id)]
        if index.tick_field is not None:
            stream_mapping = mapping if streams[binding.stream_id].clock.id == mapping.source_clock_id else None
            points = [(row[index.observation_id_field], map_tick(stream_mapping, int(row[index.tick_field]))
                       if stream_mapping else int(row[index.tick_field]))
                      for row in rows]
            points.sort(key=lambda item: (item[1], item[0]))
            if binding.stream_id == "numeric-sensor":
                sensor_points[binding.episode_id] = points
            if binding.stream_id == "target-observations":
                target_points[binding.episode_id] = points
            intervals = [
                (before, after, after[1] - before[1]) for before, after in zip(points, points[1:])]
            baseline = min((duration for _, _, duration in intervals), default=0)
            multiplier = ground_truth["gapDetection"]["minimumMultiplierExclusive"]
            gaps = [{"afterObservationId": before[0], "beforeObservationId": after[0],
                     "durationReferenceTicks": duration}
                    for before, after, duration in intervals if duration > baseline * multiplier]
            entry.update(observationCount=len(points), firstReferenceTick=points[0][1],
                         lastReferenceTick=points[-1][1],
                         observations=[{"observationId": observation_id, "referenceTick": tick}
                                       for observation_id, tick in points], gaps=gaps)
        else:
            intervals = [(row[index.observation_id_field], int(row[index.start_tick_field]),
                          int(row[index.end_tick_field])) for row in rows]
            intervals.sort(key=lambda item: (item[1], item[2], item[0]))
            entry.update(observationCount=len(intervals), firstReferenceTick=min(item[1] for item in intervals),
                         lastReferenceTick=max(item[2] for item in intervals),
                         observations=[{"observationId": observation_id, "startTick": start, "endTick": end}
                                       for observation_id, start, end in intervals], gaps=[])
            if binding.asset_ids:
                assert {row[index.value_refs[0]] for row in rows} == set(binding.asset_ids)
                video_intervals[binding.episode_id] = [(start, end) for _, start, end in intervals]
        calculated_coverage.append(entry)

    expected_coverage = sorted(ground_truth["coverage"], key=lambda item: (item["episodeId"], item["streamId"]))
    assert sorted(calculated_coverage, key=lambda item: (item["episodeId"], item["streamId"])) == expected_coverage
    declared_gaps = [gap for coverage in calculated_coverage for gap in coverage["gaps"]]
    assert {(item["afterObservationId"], item["beforeObservationId"], item["durationReferenceTicks"])
            for item in declared_gaps} == {
        ("episode-1-sensor-003", "episode-1-sensor-004", 4004),
        ("episode-1-target-003", "episode-1-target-004", 4004),
    }

    overlap = ground_truth["overlap"]
    sensor_start, sensor_end = sensor_points[overlap["episodeId"]][0][1], sensor_points[overlap["episodeId"]][-1][1]
    duration = sum(max(0, min(sensor_end, end) - max(sensor_start, start))
                   for start, end in video_intervals[overlap["episodeId"]])
    assert duration == overlap["durationReferenceTicks"]

    tolerance = alignment["toleranceReferenceTicks"]
    target_facts = sorted((point for points in target_points.values() for point in points),
                          key=lambda item: (item[1], item[0]))
    assert [tick for _, tick in target_facts] == alignment["referenceTicks"]
    remaining = list(enumerate(target_facts))
    matched_ids = []
    for observation_id, tick in sorted(
            (point for points in sensor_points.values() for point in points), key=lambda item: (item[1], item[0])):
        candidates = [(abs(tick - reference_tick), reference_tick, index)
                      for index, (_target_id, reference_tick) in remaining
                      if abs(tick - reference_tick) <= tolerance]
        if not candidates:
            continue
        _, _, chosen = min(candidates)
        remaining = [(index, reference_tick) for index, reference_tick in remaining if index != chosen]
        matched_ids.append(observation_id)
    assert matched_ids == alignment["matchedObservationIds"]
    assert len(matched_ids) == alignment["matchedCount"]
    assert sum(1 for points in sensor_points.values() for point in points if point[0] not in matched_ids) == alignment["unmatchedSensorCount"]
    assert len(remaining) == alignment["unmatchedReferenceCount"]
