"""Golden and adversarial checks for the private #588 candidate contract."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace

import pyarrow as pa
import pytest

from hub.compound_datasets import (
    ClockDescriptor,
    ObservationIndexDescriptor,
    ObservationSchemaField,
    RationalUnit,
    StreamDescriptor,
    TabularMemberRef,
    open_compound_manifest,
)
from hub.temporal_resample import (
    INT64_MAX,
    INT64_MIN,
    DatasetViewIdentity,
    FieldSelection,
    ManagedOutputRevision,
    PointObservation,
    ResampleWindow,
    TemporalResampleError,
    TemporalResampleSpecV1,
    build_resample_candidate,
    compose_child_manifest,
)


def _canonical_manifest(manifest, mutate=None):
    from hub.temporal_resample import _manifest_digest, _manifest_document
    document = _manifest_document(manifest)
    if mutate is not None:
        mutate(document)
    document["members"].sort(key=lambda item: item["id"])
    document["streams"].sort(key=lambda item: item["id"])
    document["bindings"].sort(key=lambda item: (item["episodeId"], item["streamId"]))
    document["revisionId"] = _manifest_digest(document)
    return open_compound_manifest(json.dumps(document, separators=(",", ":")).encode())


def _fixture_manifest(tmp_path):
    # This calls the existing #440 writer only; its public fixture is never edited.
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).parents[3]
    subprocess.run([sys.executable, "scripts/build_ux_fixtures.py", "--output", str(tmp_path)],
                   cwd=root, check=True)
    source = open_compound_manifest((tmp_path / "compound" / "manifest.json").read_bytes())
    target_member = TabularMemberRef("test-target-points", "test.target", "test-target-r1", "b" * 64)
    target_stream = StreamDescriptor(
        id="test-target", kind="test-point", observation_schema=(
            ObservationSchemaField("observation_id", "string", False),
            ObservationSchemaField("episode_id", "string", False),
            ObservationSchemaField("reference_tick", "int64", False),
        ), timing="irregular", nominal_rate=None,
        clock=ClockDescriptor("reference-ms", "reference", RationalUnit(1, 1000, "second")),
        units=(), missing_data="not-recorded", provider_coverage=None, transform_chain=(),
    )
    index = ObservationIndexDescriptor("observation_id", "episode_id", "reference_tick", None, None, ())
    bindings = tuple(list(source.bindings) + [
        replace(item, stream_id="test-target", member_id="test-target-points", observation_index=index)
        for item in source.bindings if item.stream_id == "numeric-sensor"
    ])
    synthetic = replace(source, members=tuple(list(source.members) + [target_member]),
                        streams=tuple(list(source.streams) + [target_stream]), bindings=bindings)
    return _canonical_manifest(synthetic)


def _source_type_manifest(manifest, field_type, *, nullable=False):
    def mutate(document):
        stream = next(item for item in document["streams"] if item["id"] == "numeric-sensor")
        stream["observationSchema"][-1]["type"] = field_type
        stream["observationSchema"][-1]["nullable"] = nullable
        fields = [(item["name"], item["type"]) for item in stream["observationSchema"]]
        member = next(item for item in document["members"] if item["id"] == "sensor-observations")
        member["schemaDigest"] = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()
    return _canonical_manifest(manifest, mutate)


def _spec(manifest, *, tolerance=1, candidate_cap=10, output_cap=10):
    source_member = next(item for item in manifest.members if item.id == "sensor-observations")
    target_member = next(item for item in manifest.members if item.id == "test-target-points")
    mapping = next(item for item in manifest.clock_mappings
                   if item.source_clock_id == "sensor-device-us")
    return TemporalResampleSpecV1(
        compound_dataset_id=manifest.ref.dataset_id, compound_revision_id=manifest.ref.revision_id,
        episode_id="episode-1", source_stream_id="numeric-sensor", target_stream_id="test-target",
        output_stream_id="derived-resample",
        source_view=DatasetViewIdentity(source_member.dataset_id, source_member.revision_id, "source-view",
                                        "a" * 64, "c" * 64),
        target_view=DatasetViewIdentity(target_member.dataset_id, target_member.revision_id, "target-view",
                                        "d" * 64, "e" * 64),
        mapping=mapping, window=ResampleWindow("reference", 0, 10_000), tolerance_ticks=tolerance,
        selected_fields=(FieldSelection("value", "arbitrary fixture units"),), candidate_cap=candidate_cap,
        output_cap=output_cap,
    )


def _source_points():
    # Exact #440 source observations; the target timeline below is deliberately test-only.
    return [
        PointObservation("episode-1-sensor-003", 3_000_000, {"value": 0.375}),
        PointObservation("episode-1-sensor-001", 1_000_000, {"value": 0.125}),
        PointObservation("episode-1-sensor-002", 2_000_000, {"value": 0.250}),
    ]


def _target_points():
    return [PointObservation("target-gap", 5_000, {}), PointObservation("target-early", 876, {}),
            PointObservation("target-tie", 1_376, {})]


def test_fixture_source_facts_produce_sorted_nearest_rows_and_null_gap(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    first = build_resample_candidate(manifest, _spec(manifest, tolerance=500), _source_points(), _target_points())
    second = build_resample_candidate(manifest, _spec(manifest, tolerance=500),
                                      list(reversed(_source_points())), list(reversed(_target_points())))

    assert first == second
    assert first.spec.idempotency_digest == second.spec.idempotency_digest
    assert [(row.target_observation_id, row.source_observation_id, row.signed_delta_ticks,
             row.absolute_delta_ticks, dict(row.values)) for row in first.rows] == [
        ("target-early", "episode-1-sensor-001", 0, 0, {"value": 0.125}),
        ("target-tie", "episode-1-sensor-001", 500, 500, {"value": 0.125}),
        ("target-gap", None, None, None, {"value": None}),
    ]
    assert first.evidence["gapTargetObservationIds"] == ["target-gap"]
    assert first.evidence["signedDeltaTicks"] == {"count": 2, "minimum": 0, "maximum": 500}
    assert first.evidence["complete"] is True


def test_invalid_contract_fails_before_any_candidate_and_caps_are_strict(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    spec = _spec(manifest, candidate_cap=2)
    with pytest.raises(TemporalResampleError, match="cap"):
        build_resample_candidate(manifest, spec, _source_points(), _target_points())
    with pytest.raises(TemporalResampleError, match="duplicate"):
        build_resample_candidate(manifest, _spec(manifest), _source_points() + _source_points()[:1], [])
    with pytest.raises(TemporalResampleError, match="values"):
        build_resample_candidate(manifest, _spec(manifest),
                                 [PointObservation("x", 1_000_000, {})], [])
    with pytest.raises(TemporalResampleError, match="integral"):
        build_resample_candidate(manifest, _spec(manifest),
                                 [PointObservation("x", 1, {"value": 1.0})], [])
    with pytest.raises(TemporalResampleError, match="outside"):
        build_resample_candidate(manifest, _spec(manifest), [], [PointObservation("x", 10_000, {})])
    with pytest.raises(TemporalResampleError, match="signed-int64"):
        build_resample_candidate(manifest, _spec(manifest),
                                 [PointObservation("x", None, {"value": "x"})], [])  # type: ignore[arg-type]
    with pytest.raises(TemporalResampleError, match="field or unit"):
        build_resample_candidate(manifest, replace(
            _spec(manifest), selected_fields=(FieldSelection("value", "wrong"),)), [], [])
    with pytest.raises(TemporalResampleError, match="output stream identity"):
        build_resample_candidate(manifest, replace(_spec(manifest), output_stream_id="bad/id"), [], [])
    with pytest.raises(TemporalResampleError, match="Arrow scalar type"):
        build_resample_candidate(manifest, _spec(manifest),
                                 [PointObservation("typed", 1_000_000, {"value": "0.125"})], [])
    integer_manifest = _source_type_manifest(manifest, "int64")
    for invalid in (True, 1.2):
        with pytest.raises(TemporalResampleError, match="Arrow scalar type"):
            build_resample_candidate(integer_manifest, _spec(integer_manifest),
                                     [PointObservation("typed", 1_000_000, {"value": invalid})], [])
    for invalid in (None, float("nan"), float("inf")):
        with pytest.raises(TemporalResampleError, match="null selected|must be finite"):
            build_resample_candidate(manifest, _spec(manifest),
                                     [PointObservation("typed", 1_000_000, {"value": invalid})], [])
    complex_manifest = _source_type_manifest(manifest, "list<float64>")
    with pytest.raises(TemporalResampleError, match="unsupported"):
        build_resample_candidate(complex_manifest, _spec(complex_manifest), [], [])
    uint_manifest = _source_type_manifest(manifest, "uint64")
    uint_candidate = build_resample_candidate(uint_manifest, _spec(uint_manifest), [PointObservation(
        "uint-max", 1_000_000, {"value": (1 << 64) - 1})], [])
    assert uint_candidate.source_points[0].values["value"] == (1 << 64) - 1
    nullable_manifest = _source_type_manifest(manifest, "float64", nullable=True)
    nullable_candidate = build_resample_candidate(nullable_manifest, _spec(nullable_manifest), [PointObservation(
        "nullable", 1_000_000, {"value": None})], [])
    assert nullable_candidate.source_points[0].values["value"] is None
    video_member = next(item for item in manifest.members if item.id == "video-observations")
    interval_target = replace(
        _spec(manifest), target_stream_id="video",
        target_view=DatasetViewIdentity(video_member.dataset_id, video_member.revision_id, "video-view",
                                        "d" * 64, "e" * 64),
    )
    with pytest.raises(TemporalResampleError, match="point streams"):
        build_resample_candidate(manifest, interval_target, [], [])


def test_child_composition_is_pure_and_binds_exact_output_and_evidence(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    candidate = build_resample_candidate(manifest, _spec(manifest, tolerance=500), _source_points(), _target_points())
    from hub.temporal_resample import _materialization_row
    output = ManagedOutputRevision("resampled-output", "managed.resample", "output-r7")
    parent_before = repr(manifest)
    child = compose_child_manifest(manifest, candidate, output)
    parsed = open_compound_manifest(json.dumps(child, separators=(",", ":")).encode())

    assert parsed.digest == child["revisionId"] and repr(manifest) == parent_before
    expected_members = [
        {"id": item.id, "datasetId": item.dataset_id, "revisionId": item.revision_id,
         "schemaDigest": item.schema_digest} for item in sorted(manifest.members, key=lambda item: item.id)
    ]
    derived = next(item for item in child["streams"] if item["id"] == "derived-resample")
    expected_schema_digest = hashlib.sha256(json.dumps(
        [(item["name"], item["type"]) for item in derived["observationSchema"]],
        separators=(",", ":")).encode()).hexdigest()
    expected_members.append({"id": "resampled-output", "datasetId": "managed.resample",
                             "revisionId": "output-r7", "schemaDigest": expected_schema_digest})
    assert child["members"] == sorted(expected_members, key=lambda item: item["id"])
    assert child["assets"] == [
        {"id": item.id, "mediaType": item.media_type, "byteLength": item.byte_length,
         "sha256": item.sha256} for item in manifest.assets
    ]
    from hub.temporal_resample import _manifest_document
    parent_document = _manifest_document(manifest)
    assert [item for item in child["streams"] if item["id"] != "derived-resample"] == parent_document["streams"]
    assert [item for item in child["bindings"] if item["streamId"] != "derived-resample"] == parent_document["bindings"]
    assert child["clockMappings"] == parent_document["clockMappings"]
    assert derived["transformChain"][-1] == f"candidate-sha256:{candidate.digest}"
    assert len([item for item in child["bindings"] if item["streamId"] == "derived-resample"]) == 2
    arrow_schema = pa.schema([pa.field(item["name"], pa.type_for_alias(item["type"]), item["nullable"])
                              for item in derived["observationSchema"]])
    table = pa.Table.from_pylist([_materialization_row(candidate.spec, row) for row in candidate.rows],
                                 schema=arrow_schema)
    assert table["observation_id"].to_pylist() == [row.target_observation_id for row in candidate.rows]
    assert table["episode_id"].to_pylist() == [candidate.spec.episode_id] * len(candidate.rows)
    assert compose_child_manifest(manifest, candidate, output) == child


def test_reviewer_p1_counterexamples_fail_before_work_or_child_composition(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    spec = _spec(manifest, tolerance=500)
    with pytest.raises(TemporalResampleError, match="time domain"):
        build_resample_candidate(manifest, replace(spec, window=ResampleWindow("wrong", 0, 10_000)), [], [])
    forged_parent = replace(manifest, clock_mappings=(replace(spec.mapping, offset_tick=0),))
    with pytest.raises(TemporalResampleError, match="parent manifest"):
        build_resample_candidate(forged_parent, spec, [], [])
    noncanonical_parent = replace(manifest, members=tuple(reversed(manifest.members)))
    with pytest.raises(TemporalResampleError, match="semantics do not match"):
        build_resample_candidate(noncanonical_parent, spec, [], [])
    from hub.temporal_resample import MAX_POINTS
    with pytest.raises(TemporalResampleError, match="point cap"):
        build_resample_candidate(manifest, spec, [object()] * (MAX_POINTS + 1), [])  # type: ignore[list-item]
    with pytest.raises(TemporalResampleError, match="cumulative byte cap"):
        build_resample_candidate(manifest, spec, [PointObservation(
            "bounded-first", 1_000_000, {"value": "x" * (8 * 1024 * 1024)})], [])
    string_manifest = _source_type_manifest(manifest, "string")
    string_spec = _spec(string_manifest, tolerance=500)
    reused_value = "x" * (600 * 1024)
    reused_source = [PointObservation("reused", 1_000_000, {"value": reused_value})]
    single = build_resample_candidate(
        string_manifest, string_spec, reused_source, [PointObservation("one", 876, {})])
    assert single.rows[0].values == (("value", reused_value),)
    with pytest.raises(TemporalResampleError, match="projected selected values"):
        build_resample_candidate(string_manifest, string_spec, reused_source, [
            PointObservation("one", 876, {}), PointObservation("two", 877, {})])
    many = [PointObservation(f"many-{index}", 1_000_000, {"value": "x" * 128})
            for index in range(10_000)]
    with pytest.raises(TemporalResampleError, match="cumulative byte cap"):
        build_resample_candidate(string_manifest, string_spec, many, [])
    with pytest.raises(TemporalResampleError, match="supported scalar"):
        build_resample_candidate(manifest, spec, [PointObservation(
            "nested", 1_000_000, {"value": [1.0]})], [])
    candidate = build_resample_candidate(manifest, spec, _source_points(), _target_points())
    output = ManagedOutputRevision("resampled-output", "managed.resample", "output-r7")
    candidate.evidence["complete"] = False
    with pytest.raises(TemporalResampleError, match="canonical rows and evidence"):
        compose_child_manifest(manifest, candidate, output)
    forged = build_resample_candidate(manifest, spec, _source_points(), _target_points())
    with pytest.raises(TemporalResampleError, match="canonical rows and evidence"):
        compose_child_manifest(manifest, replace(forged, digest="0" * 64), output)
    with pytest.raises(TemporalResampleError, match="canonical rows and evidence"):
        compose_child_manifest(manifest, replace(
            forged, source_points=tuple(reversed(forged.source_points))), output)


def test_randomized_bruteforce_matching_and_signed_int64_mapping_boundaries(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    spec = _spec(manifest, tolerance=7, candidate_cap=16, output_cap=16)
    tie_mapping = replace(spec.mapping, scale_numerator=1, scale_denominator=1, offset_tick=0)
    tie_manifest = _canonical_manifest(manifest, lambda doc: doc["clockMappings"].__setitem__(slice(None), [{
        "sourceClockId": tie_mapping.source_clock_id, "targetClockId": tie_mapping.target_clock_id,
        "scaleNumerator": 1, "scaleDenominator": 1, "offsetTick": 0}]))
    tie_spec = replace(_spec(tie_manifest, tolerance=7, candidate_cap=16, output_cap=16), mapping=tie_mapping)
    tie = build_resample_candidate(
        tie_manifest, tie_spec,
        [PointObservation("later", 12, {"value": 12}), PointObservation("earlier", 10, {"value": 10})],
        [PointObservation("middle", 11, {})],
    )
    assert tie.rows[0].source_observation_id == "earlier"
    rng = random.Random(588)
    for _ in range(100):
        source_ticks = [tick * 1_000_000 for tick in rng.sample(range(1, 10), 8)]
        source = [PointObservation(f"s-{index}", tick, {"value": index})
                  for index, tick in enumerate(source_ticks)]
        targets = [PointObservation(f"t-{index}", tick, {})
                   for index, tick in enumerate(rng.sample(range(0, 10_000), 12))]
        candidate = build_resample_candidate(manifest, spec, source, targets)
        mapping = spec.mapping
        for row in candidate.rows:
            options = []
            for point in source:
                mapped = point.tick * mapping.scale_numerator // mapping.scale_denominator + mapping.offset_tick
                if point.tick * mapping.scale_numerator % mapping.scale_denominator == 0:
                    delta = row.target_tick - mapped
                    if abs(delta) <= spec.tolerance_ticks:
                        options.append((abs(delta), mapped, point.observation_id, point, delta))
            if options:
                _, mapped, point_id, point, delta = min(options)
                assert (row.source_observation_id, row.mapped_source_tick, row.signed_delta_ticks) == (
                    point_id, mapped, delta)
            else:
                assert row.source_observation_id is None and dict(row.values) == {"value": None}
    from hub.temporal_resample import _map_tick
    mapping = replace(spec.mapping, scale_numerator=1, scale_denominator=1, offset_tick=0)
    assert _map_tick(mapping, INT64_MIN) == INT64_MIN
    assert _map_tick(mapping, INT64_MAX) == INT64_MAX
    with pytest.raises(TemporalResampleError, match="signed-int64"):
        _map_tick(mapping, INT64_MAX + 1)
