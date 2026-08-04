#!/usr/bin/env python3
"""Prepare deterministic, runnable showcase data and Canvases through the public API.

Run with the kernel environment so PyArrow is available::

    cd kernel
    uv run python ../scripts/prepare_demo_showcases.py --base-url http://127.0.0.1:8472

The command is idempotent: matching datasets and Canvases are reused. ``--replace-data`` also updates
all three saved Canvas Source bindings. Replacement is refused unless the existing resources carry
the showcase ownership metadata (or the legacy Canvases reference only showcase-owned datasets).
Verification compiles every terminal branch against the document read back from the server; it does
not publish Write outputs or otherwise run the graph.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


ASSET_COUNT = 2_400
SHOTS_PER_ASSET = 4
MODEL_VERSIONS = ("baseline-v1", "balanced-v2", "quality-v3")
DATASETS = {
    "demo_video_assets": ASSET_COUNT,
    "demo_shot_annotations": ASSET_COUNT * SHOTS_PER_ASSET,
    "demo_model_predictions": ASSET_COUNT * SHOTS_PER_ASSET * len(MODEL_VERSIONS),
}
CANVAS_IDS = (
    "demo-video-asset-curation",
    "demo-model-evaluation",
    "demo-visual-similarity-review",
)
DEMO_OWNER = "Data Playground demo"
DEMO_FOLDER = "Demo data"
DEMO_TAGS = frozenset({"demo", "research", "showcase"})
DEMO_CANVAS_MARKER = "data-playground-demo-showcase:v1"


class APIError(RuntimeError):
    """A bounded, user-readable API failure."""


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def json(
        self,
        method: str,
        path: str,
        body: object | bytes | None = None,
        *,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        request_headers = dict(headers or {})
        data: bytes | None
        if isinstance(body, bytes):
            data = body
        elif body is None:
            data = None
        else:
            data = json.dumps(body, separators=(",", ":")).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"{self.base_url}/api{path}", data=data, headers=request_headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if allow_not_found and exc.code == 404:
                return None
            try:
                detail = json.loads(payload).get("detail")
            except (json.JSONDecodeError, AttributeError):
                detail = payload.decode(errors="replace") or exc.reason
            raise APIError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise APIError(f"{method} {path}: {exc.reason}") from exc
        return json.loads(payload) if payload else None


def _embedding(asset_id: int) -> list[float]:
    values = [
        math.sin(asset_id * (dimension + 1) * 0.017)
        + math.cos((asset_id + 11) * (dimension + 2) * 0.013)
        for dimension in range(8)
    ]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 7) for value in values]


def build_assets(path: Path) -> None:
    codecs = ("h264", "hevc", "av1")
    splits = ("train", "train", "train", "validation", "test")
    widths = (1280, 1920, 1080, 3840, 720)
    heights = (720, 1080, 1920, 2160, 1280)
    ingested = datetime(2026, 7, 1, tzinfo=UTC)
    ids = list(range(1, ASSET_COUNT + 1))
    duration = [round(8.0 + (asset_id * 17 % 5400) / 10, 1) for asset_id in ids]
    table = pa.table({
        "asset_id": pa.array(ids, type=pa.int64()),
        "creator_id": pa.array([1 + asset_id % 160 for asset_id in ids], type=pa.int64()),
        "file_uri": [f"demo://synthetic-video/{asset_id:06}.mp4" for asset_id in ids],
        "split": [splits[asset_id % len(splits)] for asset_id in ids],
        "duration_s": pa.array(duration, type=pa.float64()),
        "width": pa.array([widths[asset_id % len(widths)] for asset_id in ids], type=pa.int64()),
        "height": pa.array([heights[asset_id % len(heights)] for asset_id in ids], type=pa.int64()),
        "fps": pa.array([24.0 + 6.0 * (asset_id % 3) for asset_id in ids], type=pa.float64()),
        "codec": [codecs[asset_id % len(codecs)] for asset_id in ids],
        "container": ["mp4" if asset_id % 7 else "mov" for asset_id in ids],
        "bytes": pa.array(
            [int((2_000_000 + (asset_id * 131_071) % 80_000_000) * max(seconds, 1) / 30)
             for asset_id, seconds in zip(ids, duration, strict=True)],
            type=pa.int64(),
        ),
        "has_audio": [asset_id % 9 != 0 for asset_id in ids],
        "ingested_at": pa.array(
            [ingested + timedelta(minutes=asset_id * 3) for asset_id in ids],
            type=pa.timestamp("us", tz="UTC"),
        ),
        "embedding": pa.array([_embedding(asset_id) for asset_id in ids], type=pa.list_(pa.float32(), 8)),
    })
    pq.write_table(table, path, compression="zstd")


def build_annotations(path: Path) -> None:
    labels = ("dialogue", "action", "establishing", "product", "reaction")
    asset_ids: list[int] = []
    shot_ids: list[int] = []
    actual_labels: list[str] = []
    start_frames: list[int] = []
    end_frames: list[int] = []
    confidence: list[float] = []
    review_state: list[str] = []
    annotator: list[str] = []
    for asset_id in range(1, ASSET_COUNT + 1):
        for shot_id in range(SHOTS_PER_ASSET):
            key = asset_id * SHOTS_PER_ASSET + shot_id
            start = shot_id * (90 + asset_id % 30)
            asset_ids.append(asset_id)
            shot_ids.append(shot_id)
            actual_labels.append(labels[key % len(labels)])
            start_frames.append(start)
            end_frames.append(start + 60 + key % 150)
            confidence.append(round(0.78 + (key % 210) / 1000, 3))
            review_state.append("needs review" if key % 13 == 0 else "approved")
            annotator.append(f"reviewer-{1 + key % 12:02}")
    pq.write_table(pa.table({
        "asset_id": pa.array(asset_ids, type=pa.int64()),
        "shot_id": pa.array(shot_ids, type=pa.int64()),
        "actual_label": actual_labels,
        "start_frame": pa.array(start_frames, type=pa.int64()),
        "end_frame": pa.array(end_frames, type=pa.int64()),
        "annotation_confidence": pa.array(confidence, type=pa.float64()),
        "review_state": review_state,
        "annotator": annotator,
    }), path, compression="zstd")


def build_predictions(path: Path) -> None:
    labels = ("dialogue", "action", "establishing", "product", "reaction")
    target_accuracy = {"baseline-v1": 0.725, "balanced-v2": 0.841, "quality-v3": 0.916}
    base_latency = {"baseline-v1": 76.0, "balanced-v2": 63.0, "quality-v3": 54.0}
    columns: dict[str, list[Any]] = {
        "asset_id": [], "shot_id": [], "model_version": [], "predicted_label": [],
        "probability": [], "latency_ms": [], "gpu_seconds": [], "inference_at": [],
    }
    started = datetime(2026, 7, 20, tzinfo=UTC)
    for asset_id in range(1, ASSET_COUNT + 1):
        for shot_id in range(SHOTS_PER_ASSET):
            key = asset_id * SHOTS_PER_ASSET + shot_id
            actual_index = key % len(labels)
            for model_index, model_version in enumerate(MODEL_VERSIONS):
                score = ((key * 37 + model_index * 101) % 1000) / 1000
                correct = score < target_accuracy[model_version]
                predicted_index = actual_index if correct else (actual_index + 1 + key % 3) % len(labels)
                columns["asset_id"].append(asset_id)
                columns["shot_id"].append(shot_id)
                columns["model_version"].append(model_version)
                columns["predicted_label"].append(labels[predicted_index])
                columns["probability"].append(round(0.55 + ((key * 19 + model_index * 43) % 430) / 1000, 3))
                latency = base_latency[model_version] + ((key * 7 + model_index * 3) % 21) - 4
                columns["latency_ms"].append(round(latency, 2))
                columns["gpu_seconds"].append(round(latency / 1000 * (1.0 + model_index * 0.15), 5))
                columns["inference_at"].append(started + timedelta(seconds=key * 3 + model_index))
    pq.write_table(pa.table({
        "asset_id": pa.array(columns["asset_id"], type=pa.int64()),
        "shot_id": pa.array(columns["shot_id"], type=pa.int64()),
        "model_version": columns["model_version"],
        "predicted_label": columns["predicted_label"],
        "probability": pa.array(columns["probability"], type=pa.float64()),
        "latency_ms": pa.array(columns["latency_ms"], type=pa.float64()),
        "gpu_seconds": pa.array(columns["gpu_seconds"], type=pa.float64()),
        "inference_at": pa.array(columns["inference_at"], type=pa.timestamp("us", tz="UTC")),
    }), path, compression="zstd")


def exact_table(client: Client, name: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({"q": name, "limit": 100, "offset": offset})
        response = client.json("GET", f"/catalog/tables?{query}")
        items = response["items"]
        matches.extend(item for item in items if item["name"] == name)
        if not response.get("hasMore"):
            break
        if not items:
            raise APIError(f"catalog pagination stopped making progress while looking for {name!r}")
        offset += len(items)
    if len(matches) > 1:
        raise APIError(f"catalog contains {len(matches)} exact datasets named {name!r}")
    return matches[0] if matches else None


def require_owned_table(table: dict[str, Any], *, name: str) -> None:
    tags = set(table.get("tags") or [])
    if (
        table.get("name") != name
        or table.get("owner") != DEMO_OWNER
        or table.get("folder") != DEMO_FOLDER
        or not DEMO_TAGS.issubset(tags)
    ):
        raise APIError(
            f"dataset {name!r} already exists but is not marked as owned by this showcase; "
            "refusing to reuse or replace it",
        )


def require_matching_table(
    table: dict[str, Any],
    *,
    name: str,
    row_count: int,
    column_names: list[str],
    key: list[str],
) -> None:
    require_owned_table(table, name=name)
    actual_columns = [column.get("name") for column in table.get("columns") or []]
    declared_keys = {
        tuple(candidate.get("columns") or [])
        for candidate in table.get("keys") or []
        if candidate.get("confidence") == "declared"
    }
    mismatches: list[str] = []
    if table.get("rowCount") != row_count:
        mismatches.append(f"{table.get('rowCount')} rows instead of {row_count}")
    if actual_columns != column_names:
        mismatches.append("a different column layout")
    if tuple(key) not in declared_keys:
        mismatches.append(f"no declared key {key!r}")
    if mismatches:
        raise APIError(
            f"showcase dataset {name!r} has {'; '.join(mismatches)}; rerun with --replace-data",
        )


def delete_table(client: Client, table: dict[str, Any]) -> None:
    query = urllib.parse.urlencode({
        "expected_registration_id": table["registrationId"],
        "expected_revision": table["metadataRevision"],
        "delete_source": "true",
    })
    client.json("DELETE", f"/catalog/tables/{urllib.parse.quote(table['id'], safe='')}?{query}")


def upload_dataset(
    client: Client,
    path: Path,
    *,
    name: str,
    row_count: int,
    key: list[str],
    description: str,
    replace: bool,
) -> dict[str, Any]:
    table = exact_table(client, name)
    column_names = pq.read_schema(path).names
    if table:
        require_owned_table(table, name=name)
    if table and replace:
        delete_table(client, table)
        table = None
    if table:
        require_matching_table(
            table, name=name, row_count=row_count, column_names=column_names, key=key,
        )
        return table
    uploaded = client.json(
        "POST", "/catalog/upload", path.read_bytes(),
        headers={"Content-Type": "application/octet-stream", "X-Upload-Filename": path.name},
    )
    edited = client.json("PUT", f"/catalog/tables/{urllib.parse.quote(uploaded['id'], safe='')}/edit", {
        "expectedRevision": uploaded["metadataRevision"],
        "name": name,
        "description": description,
        "folder": DEMO_FOLDER,
        "tags": sorted(DEMO_TAGS),
        "owner": DEMO_OWNER,
        "declaredKey": key,
    })
    require_matching_table(
        edited, name=name, row_count=row_count, column_names=column_names, key=key,
    )
    return edited


def node(node_id: str, kind: str, x: int, y: int, title: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": x, "y": y},
        "data": {"title": title, "status": "idle", "config": config},
    }


def demo_note(node_id: str, x: int, y: int, heading: str, body: str) -> dict[str, Any]:
    markdown = (
        f"## {heading}\n\n{body}\n\n"
        "The dataset is deterministic synthetic demo data."
    )
    return node(node_id, "note", x, y, DEMO_CANVAS_MARKER, {"markdown": markdown})


def edge(
    source: str,
    target: str,
    *,
    source_handle: str = "out",
    target_handle: str = "in",
) -> dict[str, Any]:
    return {
        "id": f"{source}-{target}", "source": source, "target": target,
        "sourceHandle": source_handle, "targetHandle": target_handle, "data": {"wire": "dataset"},
    }


def source_config(table: dict[str, Any]) -> dict[str, Any]:
    return {
        "uri": table["uri"], "tableId": table["id"], "registrationId": table["registrationId"],
    }


def canvas_documents(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    assets = source_config(tables["demo_video_assets"])
    annotations = source_config(tables["demo_shot_annotations"])
    predictions = source_config(tables["demo_model_predictions"])
    retention = {"history": "recent", "maxVersions": 5, "maxAgeDays": 30}
    curation = {
        "id": CANVAS_IDS[0], "name": "Demo 01 · Video asset curation", "version": 1,
        "requirements": [], "parameters": [], "resultRetention": retention,
        "nodes": [
            demo_note(
                "guide", 60, 40, "Video asset curation",
                "Run all to validate media, derive useful features, publish a curated dataset, and compare the library by split.",
            ),
            node("assets", "source", 60, 260, "Video assets", assets),
            node("valid", "filter", 340, 260, "Keep usable media", {
                "predicate": "width >= 320 AND height >= 240 AND duration_s BETWEEN 1 AND 600 AND bytes > 0",
            }),
            node("core", "select", 620, 260, "Choose curation fields", {
                "select": "asset_id, creator_id, file_uri, split, duration_s, width, height, fps, codec, container, bytes, has_audio",
            }),
            node("features", "transform", 900, 260, "Derive media features", {
                "source": "adhoc", "mode": "map", "onError": "raise",
                "outputSchemaSource": "declared",
                "outputSchema": [
                    {"name": "asset_id", "type": "int"},
                    {"name": "creator_id", "type": "int"},
                    {"name": "file_uri", "type": "string"},
                    {"name": "split", "type": "string"},
                    {"name": "duration_s", "type": "float"},
                    {"name": "width", "type": "int"},
                    {"name": "height", "type": "int"},
                    {"name": "fps", "type": "float"},
                    {"name": "codec", "type": "string"},
                    {"name": "container", "type": "string"},
                    {"name": "bytes", "type": "int"},
                    {"name": "has_audio", "type": "bool"},
                    {"name": "pixel_count", "type": "int"},
                    {"name": "megapixels", "type": "float"},
                    {"name": "aspect_ratio", "type": "float"},
                    {"name": "orientation", "type": "string"},
                    {"name": "mb_per_second", "type": "float"},
                ],
                "code": "def fn(row):\n    width = float(row['width'])\n    height = float(row['height'])\n    seconds = max(float(row['duration_s']), 0.001)\n    return {**row, 'pixel_count': int(width * height), 'megapixels': round(width * height / 1_000_000, 3), 'aspect_ratio': round(width / height, 3), 'orientation': 'portrait' if height > width else 'landscape', 'mb_per_second': round(float(row['bytes']) / 1_000_000 / seconds, 3)}",
            }),
            node("quality", "assert", 1190, 130, "Validate curated assets", {
                "predicate": "asset_id IS NOT NULL AND megapixels > 0 AND duration_s > 0", "severity": "error",
            }),
            node("publish", "write", 1490, 130, "Publish curated assets", {
                "filename": "demo_curated_video_assets.parquet", "writeMode": "overwrite",
            }),
            node("summary", "aggregate", 1190, 430, "Summarize the library", {
                "groupBy": "split, codec, orientation",
                "aggs": "count(*) AS assets, avg(megapixels) AS avg_megapixels, avg(duration_s) AS avg_duration_s",
            }),
            node("chart", "chart", 1490, 430, "Assets by split", {
                "chartType": "bar", "xMode": "column", "x": "split",
                "yMode": "column", "y": "assets", "agg": "sum",
            }),
        ],
        "edges": [
            edge("assets", "valid"), edge("valid", "core"), edge("core", "features"),
            edge("features", "quality"), edge("quality", "publish", source_handle="pass"),
            edge("features", "summary"), edge("summary", "chart"),
        ],
    }
    evaluation = {
        "id": CANVAS_IDS[1], "name": "Demo 02 · Model evaluation", "version": 1,
        "requirements": [], "parameters": [], "resultRetention": retention,
        "nodes": [
            demo_note(
                "guide", 370, 40, "Model evaluation",
                "Run all to join reviewed labels with three model versions, calculate quality and latency, then publish the comparison.",
            ),
            node("predictions", "source", 60, 90, "Model predictions", predictions),
            node("annotations", "source", 60, 410, "Reviewed shot labels", annotations),
            node("join", "join", 370, 250, "Match predictions to labels", {
                "on": "asset_id, shot_id", "how": "inner",
            }),
            node("approved", "filter", 670, 250, "Use approved annotations", {
                "predicate": "review_state = 'approved'",
            }),
            node("scored", "sql", 960, 250, "Score each prediction", {
                "sql": "SELECT model_version, actual_label, predicted_label, probability, latency_ms, gpu_seconds, CAST(predicted_label = actual_label AS DOUBLE) AS is_correct FROM input",
            }),
            node("metrics", "aggregate", 1260, 250, "Compare model versions", {
                "groupBy": "model_version",
                "aggs": "count(*) AS evaluated_shots, avg(is_correct) AS accuracy, avg(probability) AS avg_confidence, quantile_cont(latency_ms, 0.95) AS p95_latency_ms, sum(gpu_seconds) AS gpu_seconds",
            }),
            node("ranked", "sort", 1550, 90, "Rank model candidates", {"by": "accuracy DESC, model_version"}),
            node("gate", "assert", 1840, 90, "Check release thresholds", {
                "predicate": "accuracy >= 0.70 AND p95_latency_ms <= 100", "severity": "error",
            }),
            node("publish", "write", 2130, 90, "Publish evaluation metrics", {
                "filename": "demo_model_evaluation.parquet", "writeMode": "overwrite",
            }),
            node("chart", "chart", 1550, 440, "Accuracy by model", {
                "chartType": "line", "xMode": "column", "x": "model_version",
                "yMode": "column", "y": "accuracy", "agg": "none",
            }),
        ],
        "edges": [
            edge("predictions", "join", target_handle="a"), edge("annotations", "join", target_handle="b"),
            edge("join", "approved"), edge("approved", "scored"), edge("scored", "metrics"),
            edge("metrics", "ranked"), edge("ranked", "gate"),
            edge("gate", "publish", source_handle="pass"), edge("metrics", "chart"),
        ],
    }
    similarity = {
        "id": CANVAS_IDS[2], "name": "Demo 03 · Visual similarity review", "version": 1,
        "requirements": [], "parameters": [], "resultRetention": retention,
        "nodes": [
            demo_note(
                "guide", 60, 40, "Visual similarity review",
                "Run all to find nearest synthetic embeddings, create review bands, inspect the best score, and chart the candidate set.",
            ),
            node("assets", "source", 60, 260, "Video embeddings", assets),
            node("train", "filter", 340, 260, "Search the training split", {"predicate": "split = 'train'"}),
            node("nearest", "vector-search", 620, 260, "Find nearest assets", {
                "column": "embedding", "queryRow": 42, "k": 32,
            }),
            node("columns", "select", 900, 260, "Review candidate fields", {
                "select": "asset_id, creator_id, file_uri, split, codec, duration_s, width, height, _score",
            }),
            node("review", "transform", 1180, 260, "Create review bands", {
                "source": "adhoc", "mode": "map", "onError": "raise",
                "outputSchemaSource": "declared",
                "outputSchema": [
                    {"name": "asset_id", "type": "int"},
                    {"name": "creator_id", "type": "int"},
                    {"name": "file_uri", "type": "string"},
                    {"name": "split", "type": "string"},
                    {"name": "codec", "type": "string"},
                    {"name": "duration_s", "type": "float"},
                    {"name": "width", "type": "int"},
                    {"name": "height", "type": "int"},
                    {"name": "_score", "type": "float"},
                    {"name": "similarity_pct", "type": "float"},
                    {"name": "review_bucket", "type": "string"},
                ],
                "code": "def fn(row):\n    score = float(row['_score'])\n    bucket = 'near duplicate' if score >= 0.95 else ('strong match' if score >= 0.80 else 'explore')\n    return {**row, 'similarity_pct': round(score * 100, 2), 'review_bucket': bucket}",
            }),
            node("ranked", "sort", 1470, 260, "Rank review candidates", {"by": "_score DESC"}),
            node("best", "metric", 1760, 100, "Best similarity score", {
                "agg": "max", "column": "_score",
            }),
            node("bands", "aggregate", 1760, 430, "Summarize review bands", {
                "groupBy": "review_bucket", "aggs": "count(*) AS candidates, avg(_score) AS avg_similarity",
            }),
            node("chart", "chart", 1760, 650, "Candidates by review band", {
                "chartType": "bar", "xMode": "column", "x": "review_bucket",
                "yMode": "column", "y": "", "agg": "count",
            }),
        ],
        "edges": [
            edge("assets", "train"), edge("train", "nearest"), edge("nearest", "columns"),
            edge("columns", "review"), edge("review", "ranked"),
            edge("ranked", "best"), edge("ranked", "bands"), edge("review", "chart"),
        ],
    }
    return [curation, evaluation, similarity]


def graph_document(canvas: dict[str, Any]) -> dict[str, Any]:
    data_ids = {item["id"] for item in canvas["nodes"] if item["type"] not in {"note", "code"}}
    return {
        "id": canvas["id"], "version": canvas["version"],
        "executionBackend": canvas.get("executionBackend"),
        "resultRetention": canvas["resultRetention"],
        "requirements": canvas["requirements"], "parameters": canvas["parameters"],
        "nodes": [{
            "id": item["id"], "type": item["type"], "position": item["position"],
            "parentId": item.get("parentId"),
            "data": {
                "title": item["data"]["title"], "config": item["data"]["config"],
                "bypassed": item["data"].get("bypassed"), "disabled": item["data"].get("disabled"),
                "status": item["data"].get("status"),
            },
        } for item in canvas["nodes"] if item["id"] in data_ids],
        "edges": [item for item in canvas["edges"] if item["source"] in data_ids and item["target"] in data_ids],
    }


def canvas_source_bindings(canvas: dict[str, Any]) -> dict[str, tuple[Any, Any, Any]]:
    return {
        item["id"]: (
            item.get("data", {}).get("config", {}).get("uri"),
            item.get("data", {}).get("config", {}).get("tableId"),
            item.get("data", {}).get("config", {}).get("registrationId"),
        )
        for item in canvas.get("nodes") or []
        if item.get("type") == "source"
    }


def require_owned_canvas(
    canvas: dict[str, Any],
    *,
    expected_name: str,
    owned_source_uris: set[str],
) -> None:
    if canvas.get("name") != expected_name:
        raise APIError(
            f"Canvas {canvas.get('id')!r} already exists with another name; refusing to reuse or replace it",
        )
    marked = any(
        item.get("type") == "note"
        and (
            item.get("data", {}).get("title") == DEMO_CANVAS_MARKER
            or DEMO_CANVAS_MARKER
            in str(item.get("data", {}).get("config", {}).get("markdown") or "")
        )
        for item in canvas.get("nodes") or []
    )
    source_uris = {binding[0] for binding in canvas_source_bindings(canvas).values() if binding[0]}
    legacy_owned = bool(source_uris) and source_uris.issubset(owned_source_uris)
    if not marked and not legacy_owned:
        raise APIError(
            f"Canvas {canvas.get('id')!r} is not marked as owned by this showcase; "
            "refusing to reuse or replace it",
        )


def require_saved_canvas(saved: dict[str, Any], expected: dict[str, Any]) -> None:
    if saved.get("id") != expected["id"] or saved.get("name") != expected["name"]:
        raise APIError(f"saved Canvas {expected['id']!r} did not preserve its identity")
    expected_sources = canvas_source_bindings(expected)
    saved_sources = canvas_source_bindings(saved)
    if saved_sources != expected_sources:
        raise APIError(
            f"saved Canvas {expected['id']!r} has stale or unexpected Source bindings; "
            "rerun with --replace-canvases",
        )


def upsert_canvas(
    client: Client,
    canvas: dict[str, Any],
    *,
    replace: bool,
    owned_source_uris: set[str],
) -> tuple[str, dict[str, Any]]:
    existing = client.json("GET", f"/canvas/{urllib.parse.quote(canvas['id'], safe='')}", allow_not_found=True)
    if existing:
        require_owned_canvas(
            existing, expected_name=canvas["name"], owned_source_uris=owned_source_uris,
        )
    if existing and not replace:
        require_saved_canvas(existing, canvas)
        return "reused", existing
    if existing:
        canvas = {**canvas, "version": existing["version"]}
        client.json(
            "PUT",
            f"/canvas/{urllib.parse.quote(canvas['id'], safe='')}?expectedVersion={existing['version']}",
            canvas,
        )
        action = "replaced"
    else:
        client.json("POST", "/canvas", canvas)
        action = "created"
    saved = client.json("GET", f"/canvas/{urllib.parse.quote(canvas['id'], safe='')}")
    require_saved_canvas(saved, canvas)
    return action, saved


def declare_relationships(client: Client, tables: dict[str, dict[str, Any]]) -> None:
    assets = tables["demo_video_assets"]
    annotations = tables["demo_shot_annotations"]
    predictions = tables["demo_model_predictions"]
    for relationship in (
        {
            "leftUri": assets["uri"], "leftColumns": ["asset_id"],
            "rightUri": annotations["uri"], "rightColumns": ["asset_id"],
            "cardinality": "1:N", "confidence": "declared",
        },
        {
            "leftUri": annotations["uri"], "leftColumns": ["asset_id", "shot_id"],
            "rightUri": predictions["uri"], "rightColumns": ["asset_id", "shot_id"],
            "cardinality": "1:N", "confidence": "declared",
        },
    ):
        client.json("POST", "/catalog/relationships", relationship)


def verify_canvas(client: Client, canvas: dict[str, Any], target: str) -> dict[str, Any]:
    graph = graph_document(canvas)
    compiled = client.json("POST", "/graph/compile", {"graph": graph, "targetNodeId": target})
    if compiled.get("error") or not compiled.get("acyclic"):
        raise APIError(f"compile of {canvas['name']!r} at {target!r} failed: {compiled.get('error')}")
    result = client.json("POST", "/run/preview", {
        "graph": graph, "nodeId": target, "k": 8, "offset": 0,
    })
    rows = result.get("rows") or []
    if rows:
        return {"mode": "preview", "rows": len(rows), "steps": len(compiled["steps"])}
    if result.get("notPreviewable") and result.get("suggestedAction") == "run":
        schemas = client.json("POST", "/graph/schema", {"graph": graph, "targetNodeId": target})
        target_schema = (schemas.get(target) or {}).get("out")
        return {
            "mode": "full-run-required",
            "columns": len(target_schema) if target_schema is not None else None,
            "steps": len(compiled["steps"]), "reason": result.get("reason"),
        }
    raise APIError(
        f"preview of {canvas['name']!r} at {target!r} returned no rows: {result.get('reason')}",
    )


def build_files(directory: Path) -> dict[str, Path]:
    builders = {
        "demo_video_assets": build_assets,
        "demo_shot_annotations": build_annotations,
        "demo_model_predictions": build_predictions,
    }
    paths: dict[str, Path] = {}
    for name, builder in builders.items():
        path = directory / f"{name}.parquet"
        builder(path)
        paths[name] = path
    return paths


def canvas_urls(base_url: str, canvases: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {
        canvas["name"]: f"{base_url.rstrip('/')}/#/canvas/{urllib.parse.quote(canvas['id'], safe='')}"
        for canvas in canvases
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8471")
    parser.add_argument("--replace-data", action="store_true")
    parser.add_argument("--replace-canvases", action="store_true")
    parser.add_argument("--canvas-id", action="append", choices=CANVAS_IDS)
    parser.add_argument("--skip-preview-verification", action="store_true")
    args = parser.parse_args()
    if args.replace_data and args.canvas_id:
        parser.error("--replace-data updates every showcase Source binding and cannot be limited with --canvas-id")
    client = Client(args.base_url)
    version = client.json("GET", "/version")
    previous_tables = {name: exact_table(client, name) for name in DATASETS}
    for name, table in previous_tables.items():
        if table:
            require_owned_table(table, name=name)
    previous_owned_uris = {
        table["uri"] for table in previous_tables.values() if table is not None
    }
    with tempfile.TemporaryDirectory(prefix="data-playground-showcase-") as temp:
        paths = build_files(Path(temp))
        tables = {
            "demo_video_assets": upload_dataset(
                client, paths["demo_video_assets"], name="demo_video_assets",
                row_count=DATASETS["demo_video_assets"], key=["asset_id"],
                description="Synthetic deterministic video metadata and embeddings for the Data Playground demo.",
                replace=args.replace_data,
            ),
            "demo_shot_annotations": upload_dataset(
                client, paths["demo_shot_annotations"], name="demo_shot_annotations",
                row_count=DATASETS["demo_shot_annotations"], key=["asset_id", "shot_id"],
                description="Reviewed shot-level labels for model evaluation demos.",
                replace=args.replace_data,
            ),
            "demo_model_predictions": upload_dataset(
                client, paths["demo_model_predictions"], name="demo_model_predictions",
                row_count=DATASETS["demo_model_predictions"], key=["asset_id", "shot_id", "model_version"],
                description="Three deterministic model versions with accuracy, confidence, and latency trade-offs.",
                replace=args.replace_data,
            ),
        }
    declare_relationships(client, tables)
    canvases = canvas_documents(tables)
    selected_canvases = [canvas for canvas in canvases if not args.canvas_id or canvas["id"] in args.canvas_id]
    owned_source_uris = previous_owned_uris | {table["uri"] for table in tables.values()}
    actions: dict[str, str] = {}
    saved_canvases: dict[str, dict[str, Any]] = {}
    for canvas in selected_canvases:
        action, saved = upsert_canvas(
            client,
            canvas,
            replace=args.replace_canvases or args.replace_data,
            owned_source_uris=owned_source_uris,
        )
        actions[canvas["id"]] = action
        saved_canvases[canvas["id"]] = saved
    previews: dict[str, dict[str, Any]] = {}
    if not args.skip_preview_verification:
        verification_targets = dict(zip(CANVAS_IDS, (
            ("publish", "chart"),
            ("publish", "chart"),
            ("best", "bands", "chart"),
        ), strict=True))
        for canvas in selected_canvases:
            saved = saved_canvases[canvas["id"]]
            previews[canvas["id"]] = {
                target: verify_canvas(client, saved, target)
                for target in verification_targets[canvas["id"]]
            }
    print(json.dumps({
        "version": version,
        "datasets": {
            name: {"id": table["id"], "rows": table["rowCount"], "uri": table["uri"]}
            for name, table in tables.items()
        },
        "canvases": actions,
        "verification": previews,
        "urls": canvas_urls(args.base_url, selected_canvases),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
