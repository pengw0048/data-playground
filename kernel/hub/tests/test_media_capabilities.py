from hub.models import ColumnSchema
from hub.plugins.capabilities import tag_columns


def _tag(name: str, value: object, *, type_: str = "bytes") -> ColumnSchema:
    return tag_columns([ColumnSchema(name=name, type=type_)], sample_rows=[{name: value}])[0]


def test_media_tagging_uses_bounded_byte_evidence() -> None:
    image = _tag("payload", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    video = _tag("payload", b"\x1aE\xdf\xa3webm")
    non_media = _tag("payload", b"not an image or video")

    assert image.capabilities == ["media"] and image.media_kind == "image"
    assert video.capabilities == ["media"] and video.media_kind == "video"
    assert non_media.capabilities == [] and non_media.media_kind is None


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
