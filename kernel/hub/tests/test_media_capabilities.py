import gc
import tracemalloc

import pyarrow as pa

from hub import db
from hub.executors.preview import preview_node
from hub.models import ColumnSchema, Graph
from hub.plugins.capabilities import media_kind_from_value, tag_columns


def _tag(name: str, value: object, *, type_: str = "bytes") -> ColumnSchema:
    return tag_columns([ColumnSchema(name=name, type=type_)], sample_rows=[{name: value}])[0]


def test_media_tagging_uses_bounded_byte_evidence() -> None:
    image = _tag("payload", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    video = _tag("payload", b"\x1aE\xdf\xa3webm")
    non_media = _tag("payload", b"not an image or video")

    assert image.capabilities == ["media"] and image.media_kind == "image"
    assert video.capabilities == ["media"] and video.media_kind == "video"
    assert non_media.capabilities == [] and non_media.media_kind is None


def test_iso_base_media_requires_a_known_major_brand() -> None:
    def ftyp(brand: bytes) -> bytes:
        return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"

    assert media_kind_from_value(ftyp(b"mif1")) == "image"
    assert media_kind_from_value(ftyp(b"mp42")) == "video"
    assert media_kind_from_value(ftyp(b"qt  ")) == "video"
    assert media_kind_from_value(ftyp(b"isom")) is None
    assert media_kind_from_value(ftyp(b"M4A ")) is None
    assert media_kind_from_value(ftyp(b"zzzz")) is None


def test_large_buffer_detection_copies_only_the_bounded_prefix() -> None:
    payload = bytearray(8 * 1024 * 1024)
    payload[:8] = b"\x89PNG\r\n\x1a\n"
    for value in (payload, memoryview(payload)):
        gc.collect()
        tracemalloc.start()
        assert media_kind_from_value(value) == "image"
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 1024 * 1024


def test_media_tagging_rejects_misleading_names_and_tolerates_corrupt_cells() -> None:
    asset = _tag("asset", "not-a-url", type_="string")
    corrupt = _tag("image_url", b"\x89PNG", type_="bytes")

    assert asset.capabilities == [] and asset.media_kind is None
    assert corrupt.capabilities == [] and corrupt.media_kind is None


def test_media_tagging_accepts_supported_url_and_data_evidence() -> None:
    image = _tag("value", "https://cdn.example.test/frame.png?size=small", type_="string")
    video = _tag("value", "data:video/webm;base64,GkXfoQ", type_="string")

    assert image.capabilities == ["media"] and image.media_kind == "image"
    assert video.capabilities == ["media"] and video.media_kind == "video"


def test_media_tagging_reports_unknown_for_mixed_supported_kinds() -> None:
    column = tag_columns([ColumnSchema(name="value", type="bytes")], sample_rows=[
        {"value": b"\x89PNG\r\n\x1a\n"}, {"value": b"\x1aE\xdf\xa3webm"},
    ])[0]

    assert column.capabilities == ["media"] and column.media_kind == "unknown"


def test_schema_only_tagging_requires_an_explicit_media_capability() -> None:
    implied = tag_columns([ColumnSchema(name="image_url", type="string")])[0]
    declared = tag_columns([ColumnSchema(name="payload", type="bytes", capabilities=["media"])])[0]

    assert implied.capabilities == []
    assert declared.capabilities == ["media"] and declared.media_kind == "unknown"


def test_preview_preserves_declared_and_sampled_media_kinds_through_filter() -> None:
    class PrivateMediaAdapter:
        name = "private-media"

        @staticmethod
        def schema(_uri: str) -> list[ColumnSchema]:
            return [
                ColumnSchema(
                    name="asset", type="string", provenance="provider",
                    capabilities=["media", "provider-private"], media_kind="unknown",
                ),
                ColumnSchema(
                    name="image", type="bytes", provenance="provider",
                    capabilities=["media"], media_kind="unknown",
                ),
            ]

        @staticmethod
        def preview_scan(_uri: str, *, limit: int = 2000, **_kwargs):
            table = pa.table({
                "asset": ["private-object-key"],
                "image": [b"\x89PNG\r\n\x1a\n"],
            })
            return db.conn().from_arrow(table).limit(limit)

        scan = preview_scan

        @staticmethod
        def fingerprint(uri: str) -> str:
            return f"private:{uri}"

    graph = Graph.model_validate({
        "id": "declared-media", "version": 1,
        "nodes": [
            {
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"title": "source", "config": {"uri": "private://media"}},
            },
            {
                "id": "filter", "type": "filter", "position": {"x": 100, "y": 0},
                "data": {"title": "filter", "config": {"predicate": "asset IS NOT NULL"}},
            },
        ],
        "edges": [{
            "id": "source-filter", "source": "source", "target": "filter",
            "data": {"wire": "dataset"},
        }],
    })
    adapter = PrivateMediaAdapter()

    for target in ("source", "filter"):
        result = preview_node(
            graph, target, 5, lambda _uri: adapter, {}, storage=None,
        )
        assert not result.error
        columns = {column.name: column for column in result.columns}
        assert columns["asset"].capabilities == ["media"]
        assert columns["asset"].media_kind == "unknown"
        assert columns["image"].capabilities == ["media"]
        assert columns["image"].media_kind == "image"
