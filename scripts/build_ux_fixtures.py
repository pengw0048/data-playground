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
import json
import sys
from pathlib import Path


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


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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
    """Delegate the canonical compound fixture to the kernel-owned pure builder."""
    kernel = Path(__file__).resolve().parents[1] / "kernel"
    if str(kernel) not in sys.path:
        sys.path.insert(0, str(kernel))
    from hub.compound_fixture_definition import build_compound_timeline

    build_compound_timeline(output)

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
