"""Pure canonical builder for the offline public compound timeline fixture."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

_FLOWER_SOURCE = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm"
_FLOWER_SHA256 = "c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda"
_FLOWER_BYTES = 554_058
_COMPOUND_SIZE_BUDGET_BYTES = 600 * 1024


def _fixture_resource(name: str):
    resource = files("hub").joinpath("_fixtures", "compound", name)
    # Hatch places these in hub/_fixtures for wheels; retain a source-tree fallback for editable tests.
    return resource if resource.is_file() else Path(__file__).parents[2] / "fixtures" / "compound" / name


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_digest(fields: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode("ascii")).hexdigest()


def _vendored_asset_provenance() -> dict[str, object]:
    provenance = json.loads(_fixture_resource("flower.webm.provenance.json").read_text(encoding="utf-8"))
    expected = {
        "assetFile": "flower.webm", "byteLength": _FLOWER_BYTES, "license": "CC0-1.0",
        "mediaType": "video/webm", "repositorySizeBudgetBytes": _COMPOUND_SIZE_BUDGET_BYTES,
        "sha256": _FLOWER_SHA256, "sourceUrl": _FLOWER_SOURCE,
    }
    if not isinstance(provenance, dict) or any(provenance.get(key) != value for key, value in expected.items()):
        raise ValueError("vendored compound asset provenance is incomplete or does not match its asset")
    modifications = provenance.get("modifications")
    if not isinstance(modifications, str) or not modifications:
        raise ValueError("vendored compound asset provenance omits modifications")
    return provenance


def _compound_manifest_revision(document: dict[str, Any]) -> str:
    """Match the #439 public canonicalization before asking its loader to validate it."""
    canonical = json.loads(json.dumps(document))
    canonical.pop("revisionId")
    canonical["members"].sort(key=lambda item: item["id"])
    canonical["assets"].sort(key=lambda item: item["id"])
    canonical["episodes"].sort(key=lambda item: item["id"])
    canonical["streams"].sort(key=lambda item: item["id"])
    for stream in canonical["streams"]:
        stream["units"].sort(key=lambda item: item["field"])
    canonical["bindings"].sort(key=lambda item: (item["episodeId"], item["streamId"]))
    for binding in canonical["bindings"]:
        binding["assetIds"].sort()
    canonical["clockMappings"].sort(key=lambda item: (
        item["sourceClockId"], item["targetClockId"], item["scaleNumerator"],
        item["scaleDenominator"], item["offsetTick"],
    ))
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def build_compound_timeline(output: Path) -> None:
    """Write a small, completely local fixture for immutable compound evidence tests.

    The rows deliberately describe fixture protocol phases, not labels inferred from the video.
    ``flower.webm`` remains byte-for-byte upstream; the video observation merely bounds the
    fixture timeline to its initial two seconds.
    """
    asset = _fixture_resource("flower.webm")
    if not asset.is_file():
        raise FileNotFoundError("missing vendored compound asset")
    provenance = _vendored_asset_provenance()
    asset_bytes = asset.read_bytes()
    if len(asset_bytes) != _FLOWER_BYTES or hashlib.sha256(asset_bytes).hexdigest() != _FLOWER_SHA256:
        raise ValueError("vendored compound asset does not match the recorded MDN CC0 revision")

    compound = output / "compound"
    compound.mkdir(parents=True, exist_ok=True)
    episodes = [
        {"episode_id": "episode-1", "reference_start_tick": 0, "reference_end_tick": 10_000},
        {"episode_id": "episode-2", "reference_start_tick": 20_000, "reference_end_tick": 27_000},
    ]
    sensor_rows = [
        {"observation_id": "episode-1-sensor-001", "episode_id": "episode-1", "device_tick": 1_000_000, "value": "0.125"},
        {"observation_id": "episode-1-sensor-002", "episode_id": "episode-1", "device_tick": 2_000_000, "value": "0.250"},
        {"observation_id": "episode-1-sensor-003", "episode_id": "episode-1", "device_tick": 3_000_000, "value": "0.375"},
        {"observation_id": "episode-1-sensor-004", "episode_id": "episode-1", "device_tick": 7_000_000, "value": "0.625"},
        {"observation_id": "episode-1-sensor-005", "episode_id": "episode-1", "device_tick": 8_000_000, "value": "0.750"},
        {"observation_id": "episode-1-sensor-006", "episode_id": "episode-1", "device_tick": 9_000_000, "value": "0.875"},
        {"observation_id": "episode-2-sensor-001", "episode_id": "episode-2", "device_tick": 21_000_000, "value": "1.125"},
        {"observation_id": "episode-2-sensor-002", "episode_id": "episode-2", "device_tick": 22_000_000, "value": "1.250"},
        {"observation_id": "episode-2-sensor-003", "episode_id": "episode-2", "device_tick": 25_000_000, "value": "1.625"},
        {"observation_id": "episode-2-sensor-004", "episode_id": "episode-2", "device_tick": 26_000_000, "value": "1.750"},
    ]
    annotation_rows = [
        {"observation_id": "episode-1-annotation-001", "episode_id": "episode-1", "start_tick": 1_000, "end_tick": 2_500, "fixture_phase": "protocol-a"},
        {"observation_id": "episode-1-annotation-002", "episode_id": "episode-1", "start_tick": 6_000, "end_tick": 8_000, "fixture_phase": "protocol-b"},
        {"observation_id": "episode-2-annotation-001", "episode_id": "episode-2", "start_tick": 21_000, "end_tick": 24_000, "fixture_phase": "protocol-c"},
    ]
    video_rows = [
        {"observation_id": "episode-1-video-001", "episode_id": "episode-1", "start_tick": 1_000, "end_tick": 3_000, "asset_id": "flower-webm"},
    ]
    tables = {
        "episodes": (["episode_id", "reference_start_tick", "reference_end_tick"], episodes,
                     [("episode_id", "string"), ("reference_start_tick", "int64"), ("reference_end_tick", "int64")]),
        "sensor-observations": (["observation_id", "episode_id", "device_tick", "value"], sensor_rows,
                                [("observation_id", "string"), ("episode_id", "string"), ("device_tick", "int64"), ("value", "float64")]),
        "interval-annotations": (["observation_id", "episode_id", "start_tick", "end_tick", "fixture_phase"], annotation_rows,
                                 [("observation_id", "string"), ("episode_id", "string"), ("start_tick", "int64"), ("end_tick", "int64"), ("fixture_phase", "string")]),
        "video-observations": (["observation_id", "episode_id", "start_tick", "end_tick", "asset_id"], video_rows,
                              [("observation_id", "string"), ("episode_id", "string"), ("start_tick", "int64"), ("end_tick", "int64"), ("asset_id", "string")]),
    }
    members = []
    for member_id, (fields, rows, schema) in tables.items():
        path = compound / f"{member_id}.csv"
        payload = _fixture_resource(f"{member_id}.csv").read_bytes()
        path.write_bytes(payload)
        revision = _sha256(path)
        members.append({
            "id": member_id, "datasetId": f"fixture.compound.{member_id}",
            "revisionId": revision, "schemaDigest": _schema_digest(schema),
        })
    asset_path = compound / "flower.webm"
    asset_path.write_bytes(asset_bytes)
    if asset_path.stat().st_size != _FLOWER_BYTES or _sha256(asset_path) != _FLOWER_SHA256:
        raise ValueError("compound asset copy was not byte-identical")

    reference_clock = {"id": "reference-ms", "timeDomain": "reference", "tickUnit": {
        "numerator": 1, "denominator": 1_000, "physicalUnit": "second"}}
    sensor_clock = {"id": "sensor-device-us", "timeDomain": "device", "tickUnit": {
        "numerator": 1, "denominator": 1_000_000, "physicalUnit": "second"}}
    point_index = {"observationIdField": "observation_id", "episodeIdField": "episode_id",
                   "tickField": "device_tick", "startTickField": None, "endTickField": None,
                   "valueRefs": ["value"]}
    interval_index = {"observationIdField": "observation_id", "episodeIdField": "episode_id",
                      "tickField": None, "startTickField": "start_tick", "endTickField": "end_tick",
                      "valueRefs": ["fixture_phase"]}
    video_index = {**interval_index, "valueRefs": ["asset_id"]}
    document: dict[str, Any] = {
        "version": 1, "datasetId": "fixture-compound-timeline", "revisionId": "0" * 64,
        "members": members,
        "assets": [{"id": "flower-webm", "mediaType": "video/webm", "byteLength": _FLOWER_BYTES,
                    "sha256": _FLOWER_SHA256}],
        "episodes": [{"id": "episode-1"}, {"id": "episode-2"}],
        "streams": [
            {"id": "numeric-sensor", "kind": "numeric-sensor", "observationSchema": [
                {"name": "observation_id", "type": "string", "nullable": False},
                {"name": "episode_id", "type": "string", "nullable": False},
                {"name": "device_tick", "type": "int64", "nullable": False},
                {"name": "value", "type": "float64", "nullable": False},
            ], "timing": "irregular", "nominalRate": None, "clock": sensor_clock,
             "units": [{"field": "value", "unit": "arbitrary fixture units"}],
             "missingData": "not-recorded", "providerCoverage": None, "transformChain": []},
            {"id": "interval-annotation", "kind": "interval-annotation", "observationSchema": [
                {"name": "observation_id", "type": "string", "nullable": False},
                {"name": "episode_id", "type": "string", "nullable": False},
                {"name": "start_tick", "type": "int64", "nullable": False},
                {"name": "end_tick", "type": "int64", "nullable": False},
                {"name": "fixture_phase", "type": "string", "nullable": False},
            ], "timing": "irregular", "nominalRate": None, "clock": reference_clock,
             "units": [{"field": "fixture_phase", "unit": "fixture protocol marker"}],
             "missingData": "not-recorded", "providerCoverage": None, "transformChain": ["fixture-authored"]},
            {"id": "video", "kind": "video", "observationSchema": [
                {"name": "observation_id", "type": "string", "nullable": False},
                {"name": "episode_id", "type": "string", "nullable": False},
                {"name": "start_tick", "type": "int64", "nullable": False},
                {"name": "end_tick", "type": "int64", "nullable": False},
                {"name": "asset_id", "type": "string", "nullable": False},
            ], "timing": "irregular", "nominalRate": None, "clock": reference_clock,
             "units": [{"field": "asset_id", "unit": "immutable asset identifier"}],
             "missingData": "not-recorded", "providerCoverage": None, "transformChain": []},
        ],
        "bindings": [
            {"episodeId": "episode-1", "streamId": "numeric-sensor", "state": "present", "memberId": "sensor-observations", "assetIds": [], "observationIndex": point_index},
            {"episodeId": "episode-2", "streamId": "numeric-sensor", "state": "present", "memberId": "sensor-observations", "assetIds": [], "observationIndex": point_index},
            {"episodeId": "episode-1", "streamId": "interval-annotation", "state": "present", "memberId": "interval-annotations", "assetIds": [], "observationIndex": interval_index},
            {"episodeId": "episode-2", "streamId": "interval-annotation", "state": "present", "memberId": "interval-annotations", "assetIds": [], "observationIndex": interval_index},
            {"episodeId": "episode-1", "streamId": "video", "state": "present", "memberId": "video-observations", "assetIds": ["flower-webm"], "observationIndex": video_index},
            {"episodeId": "episode-2", "streamId": "video", "state": "absent", "memberId": None, "assetIds": [], "observationIndex": None},
        ],
        "clockMappings": [{"sourceClockId": "sensor-device-us", "targetClockId": "reference-ms",
                           "scaleNumerator": 1001, "scaleDenominator": 1_000_000, "offsetTick": -125}],
    }
    document["revisionId"] = _compound_manifest_revision(document)
    _write_json(compound / "manifest.json", document)

    from hub.compound_datasets import open_compound_manifest  # noqa: PLC0415
    manifest = open_compound_manifest((compound / "manifest.json").read_bytes())
    if manifest.digest != document["revisionId"]:
        raise ValueError("compound manifest did not validate to its generated revision")

    mapped_ticks = [876, 1877, 2878, 6882, 7883, 8884, 20896, 21897, 24900, 25901]
    _write_json(compound / "ground-truth.json", {
        "version": 1,
        "manifest": {"path": "compound/manifest.json", "revisionId": manifest.digest},
        "assetProvenance": {"path": "compound/flower.webm", **{
            key: value for key, value in provenance.items() if key != "assetFile"}},
        "memberChecksums": [{
            "id": member["id"], "path": f"compound/{member['id']}.csv",
            "revisionId": member["revisionId"], "sha256": member["revisionId"],
            "headers": tables[member["id"]][0], "schema": tables[member["id"]][2],
        } for member in sorted(members, key=lambda item: item["id"])],
        "gapDetection": {"minimumMultiplierExclusive": 3},
        "coverage": [
            {"episodeId": "episode-1", "streamId": "numeric-sensor", "state": "present", "observationCount": 6, "firstReferenceTick": 876, "lastReferenceTick": 8884, "observations": [{"observationId": "episode-1-sensor-001", "referenceTick": 876}, {"observationId": "episode-1-sensor-002", "referenceTick": 1877}, {"observationId": "episode-1-sensor-003", "referenceTick": 2878}, {"observationId": "episode-1-sensor-004", "referenceTick": 6882}, {"observationId": "episode-1-sensor-005", "referenceTick": 7883}, {"observationId": "episode-1-sensor-006", "referenceTick": 8884}], "gaps": [{"afterObservationId": "episode-1-sensor-003", "beforeObservationId": "episode-1-sensor-004", "durationReferenceTicks": 4004}]},
            {"episodeId": "episode-2", "streamId": "numeric-sensor", "state": "present", "observationCount": 4, "firstReferenceTick": 20896, "lastReferenceTick": 25901, "observations": [{"observationId": "episode-2-sensor-001", "referenceTick": 20896}, {"observationId": "episode-2-sensor-002", "referenceTick": 21897}, {"observationId": "episode-2-sensor-003", "referenceTick": 24900}, {"observationId": "episode-2-sensor-004", "referenceTick": 25901}], "gaps": []},
            {"episodeId": "episode-1", "streamId": "interval-annotation", "state": "present", "observationCount": 2, "firstReferenceTick": 1000, "lastReferenceTick": 8000, "observations": [{"observationId": "episode-1-annotation-001", "startTick": 1000, "endTick": 2500}, {"observationId": "episode-1-annotation-002", "startTick": 6000, "endTick": 8000}], "gaps": []},
            {"episodeId": "episode-2", "streamId": "interval-annotation", "state": "present", "observationCount": 1, "firstReferenceTick": 21000, "lastReferenceTick": 24000, "observations": [{"observationId": "episode-2-annotation-001", "startTick": 21000, "endTick": 24000}], "gaps": []},
            {"episodeId": "episode-1", "streamId": "video", "state": "present", "observationCount": 1, "firstReferenceTick": 1000, "lastReferenceTick": 3000, "observations": [{"observationId": "episode-1-video-001", "startTick": 1000, "endTick": 3000}], "gaps": []},
            {"episodeId": "episode-2", "streamId": "video", "state": "absent", "observationCount": 0, "observations": [], "gaps": []},
        ],
        "overlap": {"leftStreamId": "numeric-sensor", "rightStreamId": "video", "episodeId": "episode-1", "durationReferenceTicks": 2000},
        "alignment": {
            "sourceClockId": "sensor-device-us", "targetClockId": "reference-ms",
            "scaleNumerator": 1001, "scaleDenominator": 1_000_000, "offsetTick": -125,
            "declaredDriftPpm": 1000, "toleranceReferenceTicks": 1,
            "matchedObservationIds": [
                "episode-1-sensor-001", "episode-1-sensor-002", "episode-1-sensor-003",
                "episode-1-sensor-004", "episode-1-sensor-005", "episode-1-sensor-006",
                "episode-2-sensor-001", "episode-2-sensor-002", "episode-2-sensor-003",
                "episode-2-sensor-004",
            ],
            "referenceTicks": [*mapped_ticks, 27_000], "matchedCount": 10,
            "unmatchedSensorCount": 0, "unmatchedReferenceCount": 1,
        },
        "annotationProvenance": "Fixture-authored protocol markers; no semantic label is inferred from pixels.",
    })
