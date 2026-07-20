#!/usr/bin/env python3
"""Build deterministic data fixtures for UX acceptance runs.

The smoke profile keeps browser CI quick while retaining the product's starter datasets.
The full profile adds large-catalog and relationship-dense data for scheduled and
release-candidate acceptance runs. Failure scenarios are represented in the manifest because they
are injected at the HTTP or browser boundary, not by real credentials.
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
        "fault_injection": {
            "scenarios": ["slow", "unavailable", "permission_denied", "stale_reference", "partial_failure", "recovery"],
            "purpose": "Deterministic route/browser injection; never requires a private service or credential.",
        },
        "lance_append_target": {
            "datasets": ["lance-append-target"],
            "purpose": "A pre-existing registerable Lance dataset in the outputs destination so the "
                       "default-journey acceptance can certify a managed-local Lance append and its idempotent retry.",
        },
    },
}


LANCE_APPEND_TARGET = "lance-append-target"


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


def _build_lance_append_target(workspace: Path) -> None:
    """Seed a registerable Lance dataset in the workspace outputs destination.

    The default Write node writes into the ``outputs`` destination root, so the acceptance journey can
    append to this dataset and prove the managed-local Lance append + idempotent retry from #633. Its
    schema matches a ``select id, event AS label`` projection over the starter ``events`` dataset.
    """
    import lance  # noqa: PLC0415 - available through the kernel runtime used by this script
    import pyarrow as pa  # noqa: PLC0415 - available through the kernel runtime

    outputs = workspace / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64()), "label": ["seed-a", "seed-b"]}),
        str(outputs / f"{LANCE_APPEND_TARGET}.lance"),
    )


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
    if profile == "full":
        _build_full_catalog(output)
        _build_lance_append_target(output.parent)
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
