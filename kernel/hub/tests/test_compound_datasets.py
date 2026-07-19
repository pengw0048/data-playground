"""Focused public-contract checks for bounded compound revision manifests."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from hub.compound_datasets import (
    MAX_MANIFEST_BYTES,
    ClockMapping,
    CompoundManifestError,
    ObservationRef,
    map_tick,
    open_compound_manifest,
)


_SHA = "a" * 64


def _document() -> dict:
    return {
        "version": 1,
        "datasetId": "demo-data",
        "revisionId": "0" * 64,
        "members": [
            {"id": "state-index", "datasetId": "state-data", "revisionId": "state-v7",
             "schemaDigest": "b" * 64},
            {"id": "camera-index", "datasetId": "camera-data", "revisionId": "camera-v9",
             "schemaDigest": "c" * 64},
        ],
        "assets": [
            {"id": "thumbnail", "mediaType": "image/png", "byteLength": 4, "sha256": "e" * 64},
            {"id": "frames", "mediaType": "application/octet-stream", "byteLength": 12,
             "sha256": _SHA},
        ],
        "episodes": [{"id": "episode-2"}, {"id": "episode-1"}],
        "streams": [
            {"id": "state", "kind": "state", "observationSchema": [
                {"name": "observation_id", "type": "string", "nullable": False},
                {"name": "episode_id", "type": "string", "nullable": False},
                {"name": "tick", "type": "int64", "nullable": False},
                {"name": "value", "type": "float64", "nullable": False},
            ], "timing": "regular", "nominalRate": {"numerator": 30, "denominator": 1,
                                                         "physicalUnit": "second"},
             "clock": {"id": "state-clock", "timeDomain": "device", "tickUnit": {
                 "numerator": 1, "denominator": 1000, "physicalUnit": "second"}},
             "units": [{"field": "value", "unit": "meter"}],
             "missingData": "not-recorded", "providerCoverage": None,
             "transformChain": ["calibrated"]},
            {"id": "camera", "kind": "image", "observationSchema": [
                {"name": "observation_id", "type": "string", "nullable": False},
                {"name": "episode_id", "type": "string", "nullable": False},
                {"name": "tick", "type": "int64", "nullable": False},
                {"name": "frame", "type": "binary", "nullable": False},
            ], "timing": "irregular", "nominalRate": None,
             "clock": {"id": "camera-clock", "timeDomain": "device", "tickUnit": {
                 "numerator": 1, "denominator": 1000, "physicalUnit": "second"}},
             "units": [{"field": "frame", "unit": "encoded bytes"}],
             "missingData": "not-recorded", "providerCoverage": None,
             "transformChain": []},
        ],
        "bindings": [
            {"episodeId": "episode-2", "streamId": "state", "state": "absent", "memberId": None,
             "assetIds": [], "observationIndex": None},
            {"episodeId": "episode-1", "streamId": "camera", "state": "present",
             "memberId": "camera-index", "assetIds": ["thumbnail", "frames"], "observationIndex": {
                 "observationIdField": "observation_id", "episodeIdField": "episode_id",
                 "tickField": "tick", "startTickField": None, "endTickField": None,
                 "valueRefs": ["frame"]}},
            {"episodeId": "episode-1", "streamId": "state", "state": "absent", "memberId": None,
             "assetIds": [], "observationIndex": None},
            {"episodeId": "episode-2", "streamId": "camera", "state": "present",
             "memberId": "camera-index", "assetIds": ["thumbnail", "frames"], "observationIndex": {
                 "observationIdField": "observation_id", "episodeIdField": "episode_id",
                 "tickField": "tick", "startTickField": None, "endTickField": None,
                 "valueRefs": ["frame"]}},
        ],
        "clockMappings": [{"sourceClockId": "camera-clock", "targetClockId": "state-clock",
                           "scaleNumerator": 1, "scaleDenominator": 2, "offsetTick": 0}],
    }


def _canonical_digest(document: dict) -> str:
    document = copy.deepcopy(document)
    document.pop("revisionId")
    document["members"].sort(key=lambda item: item["id"])
    document["assets"].sort(key=lambda item: item["id"])
    document["episodes"].sort(key=lambda item: item["id"])
    document["streams"].sort(key=lambda item: item["id"])
    for stream in document["streams"]:
        stream["units"].sort(key=lambda item: item["field"])
    document["bindings"].sort(key=lambda item: (item["episodeId"], item["streamId"]))
    for binding in document["bindings"]:
        binding["assetIds"].sort()
    document["clockMappings"].sort(key=lambda item: (
        item["sourceClockId"], item["targetClockId"], item["scaleNumerator"],
        item["scaleDenominator"], item["offsetTick"]))
    return hashlib.sha256(json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _payload(document: dict | None = None) -> bytes:
    document = _document() if document is None else document
    document["revisionId"] = _canonical_digest(document)
    return json.dumps(document, separators=(",", ":")).encode()


def test_open_bounded_manifest_has_immutable_identity_and_declared_absence():
    manifest = open_compound_manifest(_payload())

    assert manifest.ref.revision_id == manifest.digest
    assert [(item.episode_id, item.stream_id, item.state) for item in manifest.bindings] == [
        ("episode-1", "camera", "present"), ("episode-1", "state", "absent"),
        ("episode-2", "camera", "present"), ("episode-2", "state", "absent"),
    ]
    assert "revision_id" in ObservationRef.__annotations__


def test_json_key_order_and_declared_unordered_collections_do_not_change_identity():
    first = _document()
    second = _document()
    second["members"].reverse()
    second["assets"].reverse()
    second["episodes"].reverse()
    second["streams"].reverse()
    second["bindings"].reverse()
    for binding in second["bindings"]:
        binding["assetIds"].reverse()
    first_payload = _payload(first)
    second_payload = _payload(second)

    assert open_compound_manifest(first_payload) == open_compound_manifest(second_payload)
    assert open_compound_manifest(json.dumps(json.loads(first_payload), indent=2, sort_keys=True).encode()).digest == (
        open_compound_manifest(first_payload).digest)


def test_exact_member_identity_accepts_provider_native_opaque_characters_and_type_tokens():
    document = _document()
    document["members"][0]["datasetId"] = "catalog/group/table name #状态"
    document["members"][0]["revisionId"] = "branch/main@v7+résumé"
    document["streams"][0]["observationSchema"][3]["type"] = "fixed_size_list<item: float>[3]"

    manifest = open_compound_manifest(_payload(document))

    assert manifest.members[1].dataset_id == "catalog/group/table name #状态"
    assert manifest.members[1].revision_id == "branch/main@v7+résumé"


@pytest.mark.parametrize(("field", "value"), [
    ("datasetId", "x" * 129),
    ("revisionId", "x" * 257),
])
def test_exact_member_identity_reuses_dataset_ref_bounds(field, value):
    document = _document()
    document["members"][0][field] = value
    with pytest.raises(CompoundManifestError, match="exact member identity"):
        open_compound_manifest(_payload(document))


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["members"].__setitem__(0, {**doc["members"][0], "revisionId": "state-v8"}),
    lambda doc: doc["assets"].__setitem__(0, {**doc["assets"][0], "sha256": "d" * 64}),
    lambda doc: doc["streams"][0]["observationSchema"].__setitem__(0, {
        **doc["streams"][0]["observationSchema"][0], "type": "binary"}),
    lambda doc: doc["streams"][0].__setitem__("missingData", "redacted"),
    lambda doc: doc["clockMappings"][0].__setitem__("offsetTick", 1),
])
def test_semantic_changes_require_another_revision(mutate):
    document = _document()
    original = _payload(document)
    mutate(document)
    document["revisionId"] = json.loads(original)["revisionId"]

    with pytest.raises(CompoundManifestError, match="revision does not match"):
        open_compound_manifest(json.dumps(document).encode())
    assert open_compound_manifest(_payload(document)).digest != open_compound_manifest(original).digest


def test_decoder_rejects_duplicate_keys_dangling_bindings_ambiguous_indices_and_mutable_assets():
    duplicate = b'{"version":1,"version":1}'
    with pytest.raises(CompoundManifestError, match="not valid JSON"):
        open_compound_manifest(duplicate)

    dangling = _document()
    dangling["bindings"][1]["memberId"] = "not-a-member"
    with pytest.raises(CompoundManifestError, match="dangling member"):
        open_compound_manifest(_payload(dangling))

    ambiguous = _document()
    ambiguous["bindings"][1]["observationIndex"]["startTickField"] = "start"
    ambiguous["bindings"][1]["observationIndex"]["endTickField"] = "end"
    with pytest.raises(CompoundManifestError, match="ambiguous time"):
        open_compound_manifest(_payload(ambiguous))

    unknown_field = _document()
    unknown_field["bindings"][1]["observationIndex"]["valueRefs"] = ["not_in_schema"]
    with pytest.raises(CompoundManifestError, match="not in the stream schema"):
        open_compound_manifest(_payload(unknown_field))

    mutable_asset = _document()
    mutable_asset["assets"][0].pop("sha256")
    mutable_asset["assets"][0]["path"] = "/private/bytes"
    with pytest.raises(CompoundManifestError, match="object shape"):
        open_compound_manifest(_payload(mutable_asset))


@pytest.mark.parametrize(("collection", "id_field"), [
    ("members", "id"), ("assets", "id"), ("episodes", "id"), ("streams", "id"),
])
def test_duplicate_top_level_id_fails_closed(collection, id_field):
    document = _document()
    duplicate = copy.deepcopy(document[collection][0])
    duplicate[id_field] = document[collection][0][id_field]
    document[collection].append(duplicate)

    with pytest.raises(CompoundManifestError, match="duplicate"):
        open_compound_manifest(_payload(document))


@pytest.mark.parametrize("change", ["duplicate", "missing"])
def test_episode_stream_matrix_must_have_exactly_one_binding(change):
    document = _document()
    if change == "duplicate":
        document["bindings"].append(copy.deepcopy(document["bindings"][0]))
    else:
        document["bindings"].pop()
    with pytest.raises(CompoundManifestError, match="incomplete or duplicate"):
        open_compound_manifest(_payload(document))


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["streams"][1]["observationSchema"][0].__setitem__("type", "int64"),
    lambda doc: doc["streams"][1]["observationSchema"][1].__setitem__("nullable", True),
    lambda doc: doc["streams"][1]["observationSchema"][2].__setitem__("type", "float64"),
    lambda doc: doc["streams"][1]["observationSchema"][2].__setitem__("nullable", True),
])
def test_observation_index_roles_require_non_null_string_ids_and_integer_ticks(mutate):
    document = _document()
    mutate(document)
    with pytest.raises(CompoundManifestError, match="identity fields|time fields"):
        open_compound_manifest(_payload(document))


def test_interval_endpoints_must_have_the_same_supported_integer_type():
    document = _document()
    stream = document["streams"][1]
    stream["observationSchema"][2] = {"name": "start", "type": "int32", "nullable": False}
    stream["observationSchema"].insert(
        3, {"name": "end", "type": "int64", "nullable": False})
    for binding in (document["bindings"][1], document["bindings"][3]):
        binding["observationIndex"].update({
            "tickField": None, "startTickField": "start", "endTickField": "end",
        })
    with pytest.raises(CompoundManifestError, match="time fields"):
        open_compound_manifest(_payload(document))


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["streams"][1]["units"].append({"field": "frame", "unit": "bytes"}),
    lambda doc: doc["streams"][1]["units"].__setitem__(
        0, {"field": "missing_field", "unit": "bytes"}),
    lambda doc: doc["bindings"][1]["observationIndex"].__setitem__("valueRefs", []),
])
def test_field_units_and_value_references_fail_closed_when_ambiguous(mutate):
    document = _document()
    mutate(document)
    with pytest.raises(CompoundManifestError, match="unit|value references"):
        open_compound_manifest(_payload(document))


def test_tick_mapping_never_rounds_and_manifest_limit_precedes_json_parse():
    mapping = ClockMapping("from", "to", 1, 2, -1)
    assert map_tick(mapping, 4) == 1
    with pytest.raises(CompoundManifestError, match="integral"):
        map_tick(mapping, 3)
    with pytest.raises(CompoundManifestError, match="byte limit"):
        open_compound_manifest(b"{" + b"x" * MAX_MANIFEST_BYTES)
    with pytest.raises(CompoundManifestError, match="hard cap"):
        open_compound_manifest(b"{}", max_bytes=MAX_MANIFEST_BYTES + 1)
    with pytest.raises(CompoundManifestError, match="target tick"):
        map_tick(ClockMapping("from", "to", 1, 1, (1 << 53) - 1), 1)


def test_deep_json_parse_failure_is_a_stable_manifest_error():
    payload = b"[" * 20_000 + b"]" * 20_000
    with pytest.raises(CompoundManifestError, match="not valid JSON"):
        open_compound_manifest(payload)
