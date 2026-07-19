#!/usr/bin/env python3
"""Build deterministic data fixtures for UX acceptance runs.

The smoke profile keeps browser CI quick while retaining the product's starter datasets.
The full profile adds large-catalog, relationship-dense, temporal, and multimodal data for
scheduled and release-candidate acceptance runs. Failure scenarios are represented in the
manifest because they are injected at the HTTP or browser boundary, not by real credentials.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


MANIFEST = {
    "version": 1,
    "fixtures": {
        "small_happy_path": {
            "datasets": ["events", "movies", "images"],
            "purpose": "Three related starter datasets for discovery, preview, transform, and export.",
        },
        "medium_catalog": {
            "datasets": 120,
            "purpose": "Catalog paging, search, folders, and more-than-100-result behavior.",
        },
        "relationship_dense": {
            "datasets": 24,
            "purpose": "Dense join and lineage rendering with bounded result disclosure.",
        },
        "temporal_multimodal": {
            "datasets": ["episodes", "frames", "audio_windows"],
            "purpose": "Independent CSV discovery rows; they do not assert synchronized playback.",
        },
        "compound_timeline": {
            "files": ["compound/manifest.json", "compound/ground-truth.json"],
            "purpose": "Offline exact compound evidence with declared clocks, gaps, and modality absence.",
        },
        "fault_injection": {
            "scenarios": ["slow", "unavailable", "permission_denied", "stale_reference", "partial_failure", "recovery"],
            "purpose": "Deterministic route/browser injection; never requires a private service or credential.",
        },
    },
}


_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOUND_ASSET = _REPO_ROOT / "fixtures" / "compound" / "flower.webm"
_COMPOUND_PROVENANCE = _REPO_ROOT / "fixtures" / "compound" / "flower.webm.provenance.json"
_FLOWER_SOURCE = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.webm"
_FLOWER_SHA256 = "c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda"
_FLOWER_BYTES = 554_058
_COMPOUND_SIZE_BUDGET_BYTES = 600 * 1024


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_digest(fields: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode("ascii")).hexdigest()


def _vendored_asset_provenance() -> dict[str, object]:
    provenance = json.loads(_COMPOUND_PROVENANCE.read_text(encoding="utf-8"))
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


def _seed_starter_data(output: Path) -> None:
    """Reuse the product's starter-data builder so smoke tests exercise normal file formats."""
    kernel = Path(__file__).resolve().parents[1] / "kernel"
    sys.path.insert(0, str(kernel))
    from hub.seed import seed  # noqa: PLC0415 - script deliberately bootstraps the product builder

    seed(str(output))
    # Full runs use the default kernel transport, which admits only provider-native exact revisions.
    # Keep the starter schema and catalog name while making the golden full-result source reopenable.
    import lance  # noqa: PLC0415 - available through the kernel runtime used by this script
    import pyarrow.parquet as pq  # noqa: PLC0415 - available through the kernel runtime

    events_parquet = output / "events.parquet"
    lance.write_dataset(pq.read_table(events_parquet), str(output / "events.lance"))
    events_parquet.unlink()


def _build_temporal_multimodal(output: Path) -> None:
    _write_csv(
        output / "episodes.csv",
        ["episode_id", "subject_id", "start_ms", "end_ms", "label"],
        [
            {"episode_id": index, "subject_id": index % 4, "start_ms": index * 1_000,
             "end_ms": index * 1_000 + 900, "label": "walk" if index % 2 else "rest"}
            for index in range(16)
        ],
    )
    _write_csv(
        output / "frames.csv",
        ["episode_id", "frame_ms", "image_url", "camera"],
        [
            {"episode_id": episode, "frame_ms": episode * 1_000 + frame * 100,
             "image_url": f"https://picsum.photos/seed/ux-{episode}-{frame}/320/240",
             "camera": "front" if frame % 2 else "side"}
            for episode in range(16) for frame in range(5)
        ],
    )
    _write_csv(
        output / "audio_windows.csv",
        ["episode_id", "start_ms", "end_ms", "rms"],
        [
            {"episode_id": episode, "start_ms": episode * 1_000 + window * 100,
             "end_ms": episode * 1_000 + (window + 1) * 100,
             "rms": round((episode + window) / 100, 3)}
            for episode in range(16) for window in range(9)
        ],
    )


def _build_compound_timeline(output: Path) -> None:
    """Write a small, completely local fixture for immutable compound evidence tests.

    The rows deliberately describe fixture protocol phases, not labels inferred from the video.
    ``flower.webm`` remains byte-for-byte upstream; the video observation merely bounds the
    fixture timeline to its initial two seconds.
    """
    if not _COMPOUND_ASSET.is_file():
        raise FileNotFoundError(f"missing vendored compound asset: {_COMPOUND_ASSET}")
    provenance = _vendored_asset_provenance()
    if _COMPOUND_ASSET.stat().st_size != _FLOWER_BYTES or _sha256(_COMPOUND_ASSET) != _FLOWER_SHA256:
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
        _write_csv(path, fields, rows)
        revision = _sha256(path)
        members.append({
            "id": member_id, "datasetId": f"fixture.compound.{member_id}",
            "revisionId": revision, "schemaDigest": _schema_digest(schema),
        })
    asset_path = compound / "flower.webm"
    shutil.copyfile(_COMPOUND_ASSET, asset_path)
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

    kernel = _REPO_ROOT / "kernel"
    if str(kernel) not in sys.path:
        sys.path.insert(0, str(kernel))
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


def _build_full_catalog(output: Path) -> None:
    for index in range(120):
        _write_csv(
            output / f"catalog_{index:03}.csv",
            ["id", "group", "value"],
            [{"id": index, "group": f"folder-{index % 12:02}", "value": index * 10}],
        )
    for index in range(24):
        _write_csv(
            output / f"relationship_dense_{index:02}.csv",
            ["id", "left_id", "right_id", "weight"],
            [
                {"id": index * 10 + row, "left_id": row % 12, "right_id": (row + index) % 12,
                 "weight": index + row}
                for row in range(12)
            ],
        )


def build(output: Path, profile: str) -> Path:
    if profile not in {"smoke", "full"}:
        raise ValueError(f"unknown UX fixture profile: {profile}")
    output.mkdir(parents=True, exist_ok=True)
    _seed_starter_data(output)
    _build_temporal_multimodal(output)
    _build_compound_timeline(output)
    if profile == "full":
        _build_full_catalog(output)
    manifest_dir = output / "ux-fixtures"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({**MANIFEST, "profile": profile}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_dir / "manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="empty or disposable data directory")
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    args = parser.parse_args()
    manifest = build(args.output.resolve(), args.profile)
    print(f"built {args.profile} UX fixtures → {manifest}")


if __name__ == "__main__":
    main()
