from __future__ import annotations

import importlib.util
import urllib.parse
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "prepare_demo_showcases.py"
_SPEC = importlib.util.spec_from_file_location("prepare_demo_showcases", _SCRIPT)
assert _SPEC and _SPEC.loader
showcases = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(showcases)


class CatalogClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def json(self, method: str, path: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert method == "GET"
        self.paths.append(path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        assert query["q"] == ["demo_video_assets"]
        if query["offset"] == ["0"]:
            return {
                "items": [{"name": "demo_video_assets_backup"}],
                "total": 2,
                "hasMore": True,
            }
        assert query["offset"] == ["1"]
        return {
            "items": [{"id": "expected", "name": "demo_video_assets"}],
            "total": 2,
            "hasMore": False,
        }


class CanvasClient:
    def __init__(self, existing: dict[str, Any]) -> None:
        self.document = existing
        self.puts: list[dict[str, Any]] = []

    def json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if method == "GET":
            return self.document
        assert method == "PUT"
        assert body is not None
        self.puts.append(body)
        self.document = {**body, "version": body["version"] + 1}
        return self.document


def canvas(uri: str, *, marker: bool = False) -> dict[str, Any]:
    nodes = [{
        "id": "source",
        "type": "source",
        "position": {"x": 0, "y": 0},
        "data": {
            "title": "Source",
            "status": "idle",
            "config": {"uri": uri, "tableId": f"table:{uri}", "registrationId": f"registration:{uri}"},
        },
    }]
    if marker:
        nodes.append({
            "id": "guide",
            "type": "note",
            "position": {"x": 0, "y": 100},
            "data": {
                "title": showcases.DEMO_CANVAS_MARKER,
                "status": "idle",
                "config": {"markdown": "Synthetic showcase"},
            },
        })
    return {
        "id": "demo-video-asset-curation",
        "name": "Demo 01 · Video asset curation",
        "version": 4,
        "requirements": [],
        "parameters": [],
        "resultRetention": {"history": "recent", "maxVersions": 5, "maxAgeDays": 30},
        "nodes": nodes,
        "edges": [],
    }


def test_exact_table_uses_the_supported_query_parameter_and_follows_pagination() -> None:
    client = CatalogClient()

    assert showcases.exact_table(client, "demo_video_assets")["id"] == "expected"
    assert len(client.paths) == 2
    assert all("query=" not in path for path in client.paths)


def test_replacing_a_legacy_showcase_canvas_rebinds_its_saved_source() -> None:
    old_uri = "file:///old-demo.parquet"
    new_uri = "file:///new-demo.parquet"
    client = CanvasClient(canvas(old_uri))

    action, saved = showcases.upsert_canvas(
        client,
        canvas(new_uri, marker=True),
        replace=True,
        owned_source_uris={old_uri, new_uri},
    )

    assert action == "replaced"
    assert showcases.canvas_source_bindings(saved)["source"][0] == new_uri
    assert showcases.DEMO_CANVAS_MARKER in {node["data"]["title"] for node in saved["nodes"]}
    assert len(client.puts) == 1


def test_replacement_refuses_unmarked_resources_outside_the_showcase_boundary() -> None:
    with pytest.raises(showcases.APIError, match="not marked as owned"):
        showcases.require_owned_table({
            "name": "demo_video_assets", "owner": None, "folder": "", "tags": [],
        }, name="demo_video_assets")

    foreign_uri = "file:///researcher-owned.parquet"
    client = CanvasClient(canvas(foreign_uri))
    with pytest.raises(showcases.APIError, match="not marked as owned"):
        showcases.upsert_canvas(
            client,
            canvas("file:///new-demo.parquet", marker=True),
            replace=True,
            owned_source_uris={"file:///new-demo.parquet"},
        )
