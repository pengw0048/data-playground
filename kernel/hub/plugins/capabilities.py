"""Capability providers — predicate(schema) -> bool + the viewer tabs they add.

Capabilities live on columns, not wires. They add actions/viewer tabs to any node whose
data qualifies; they never change what connects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import urlsplit

from hub.models import ColumnSchema

_VECTOR_NAME = re.compile(r"(embed|embedding|vector|feature)", re.I)
# an id-like column name: `id`, `uuid`, `pk`, or a *_id / *_key / *_uid suffix (the usual join keys).
_KEY_NAME = re.compile(r"^(id|uuid|guid|pk)$|_(id|uid|uuid|guid|key|pk)$", re.I)
_KEY_TYPES = {"int", "string", "bytes"}  # a plausible join-key type (not float/bool/vector/media)


MediaKind = Literal["image", "video", "unknown"]

_IMAGE_EXTENSIONS = {"avif", "bmp", "gif", "jpeg", "jpg", "png", "svg", "webp"}
_VIDEO_EXTENSIONS = {"m4v", "mkv", "mov", "mp4", "webm"}
_IMAGE_FTYP_BRANDS = {
    b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1",
}
_VIDEO_FTYP_BRANDS = {
    b"3g2a", b"3g2b", b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"avc1", b"M4V ", b"M4VH", b"M4VP",
    b"isom", b"mp41", b"mp42", b"mp71", b"qt  ",
}
_MAX_MEDIA_SAMPLE_ROWS = 256
_MAX_MEDIA_CELL_BYTES = 4096
_MAX_MEDIA_URL_LENGTH = 8192
_PRIVATE_MEDIA_OBJECT_SCHEMES = {"s3", "r2", "gs", "gcs"}


def is_media_column(col: ColumnSchema) -> bool:
    """Whether a producer explicitly declares this schema field as media.

    Value inference deliberately belongs in ``tag_columns`` because schemas alone cannot
    prove that a suggestive field name actually contains renderable media.
    """
    return "media" in col.capabilities


def media_content_type_from_bytes(value: object) -> tuple[MediaKind, str] | None:
    """Return a conservative media kind/MIME pair from a bounded byte prefix."""
    try:
        if isinstance(value, bytes):
            data = value[:_MAX_MEDIA_CELL_BYTES]
        elif isinstance(value, bytearray):
            data = bytes(value[:_MAX_MEDIA_CELL_BYTES])
        elif isinstance(value, memoryview):
            data = value.cast("B")[:_MAX_MEDIA_CELL_BYTES].tobytes()
        else:
            return None
    except (TypeError, ValueError, MemoryError):
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data.startswith(b"\x1aE\xdf\xa3"):
        if b"matroska" in data:
            return "video", "video/x-matroska"
        return "video", "video/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _IMAGE_FTYP_BRANDS:
            if brand in {b"avif", b"avis"}:
                return "image", "image/avif"
            return "image", ("image/heif" if brand in {b"mif1", b"msf1"} else "image/heic")
        if brand in _VIDEO_FTYP_BRANDS:
            return "video", ("video/quicktime" if brand == b"qt  " else "video/mp4")
    return None


def _kind_from_bytes(value: object) -> MediaKind | None:
    detected = media_content_type_from_bytes(value)
    return detected[0] if detected is not None else None


def _kind_from_extension(path: str) -> MediaKind | None:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = name.rsplit(".", 1)
    if len(suffix) != 2:
        return None
    extension = suffix[1].lower()
    if extension in _IMAGE_EXTENSIONS:
        return "image"
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    return None


def _kind_from_url(value: object) -> MediaKind | None:
    if not isinstance(value, str) or len(value) > _MAX_MEDIA_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return None
    if parsed.scheme == "data":
        mime = value[5:].split(";", 1)[0].lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return _kind_from_extension(parsed.path)


def private_media_kind_from_value(value: object) -> MediaKind | None:
    """Classify one core-supported private reference without opening or fetching it."""
    if not isinstance(value, str) or len(value) > _MAX_MEDIA_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.lower()
    if scheme in {"data", "http", "https"}:
        return None
    if scheme == "file":
        if (not value.lower().startswith("file://") or parsed.netloc
                or "?" in value or "#" in value):
            return None
        return _kind_from_extension(parsed.path)
    if scheme in _PRIVATE_MEDIA_OBJECT_SCHEMES:
        return (
            _kind_from_extension(parsed.path)
            if value[len(parsed.scheme):].startswith("://") and parsed.netloc
            else None
        )
    if scheme and value[len(parsed.scheme):].startswith("://"):
        return None
    # Core treats absolute, relative, tilde, and opaque-colon spellings without ``://`` as local
    # paths. Classification is extension-only and never stats, opens, or fetches the value.
    return _kind_from_extension(value)


def media_kind_from_value(value: object) -> MediaKind | None:
    """Classify one already-read preview cell without decoding or fetching it."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _kind_from_bytes(value)
    return _kind_from_url(value)


def detect_media_kind(column: str, sample_rows: Sequence[Mapping[str, object]]) -> MediaKind | None:
    """Return evidence from a bounded preview page, never reading outside it."""
    kinds = {kind for row in sample_rows[:_MAX_MEDIA_SAMPLE_ROWS]
             if (kind := media_kind_from_value(row.get(column))) is not None}
    if not kinds:
        return None
    return next(iter(kinds)) if len(kinds) == 1 else "unknown"


def is_vector_column(col: ColumnSchema) -> bool:
    if "vector" in col.capabilities:
        return True
    t = col.type.lower()
    is_list = t.endswith("[]") or "list" in t or "array" in t
    return is_list and bool(_VECTOR_NAME.search(col.name))


def is_key_column(col: ColumnSchema) -> bool:
    """An id-like column — a likely join key. Name heuristic + a scalar key-able type (a media/
    vector column is never a key even if it matches the name pattern, e.g. `image_id` is a key but
    `image_url` is media). Whether it's ACTUALLY unique is measured separately (see relationships)."""
    if "key" in col.capabilities:
        return True
    if is_media_column(col) or is_vector_column(col):
        return False
    return bool(_KEY_NAME.search(col.name)) and display_base_type(col.type) in _KEY_TYPES


def display_base_type(t: str) -> str:
    """The generic base type ('int'/'string'/...), stripping a '[]' list suffix — matches the
    display types adapters emit (adapters.display_type)."""
    t = t.lower()
    return t[:-2] if t.endswith("[]") else t


# Plugin-registered column detectors: a capability object with a `detect(col)->bool` (wired via
# reg.add_capability → register_detector) gets its tag applied by tag_columns alongside the built-in
# media/vector/key — so add_capability is a REAL seam (a plugin can tag columns) without editing core.
_EXTRA_DETECTORS: list[tuple[str, object]] = []


def register_detector(cap_id: str, detect) -> None:
    """Register a plugin capability's column detector (idempotent per id)."""
    if callable(detect) and not any(cid == cap_id for cid, _ in _EXTRA_DETECTORS):
        _EXTRA_DETECTORS.append((cap_id, detect))


def tag_columns(columns: list[ColumnSchema], *, sample_rows: Sequence[Mapping[str, object]] | None = None) -> list[ColumnSchema]:
    """Annotate columns with explicit or bounded-preview capability evidence.

    Schema-only callers retain declared media capabilities; callers that already have preview
    rows may add media only when a sampled cell proves a supported image/video value.
    """
    for c in columns:
        caps = set(c.capabilities)
        kind = detect_media_kind(c.name, sample_rows) if sample_rows is not None else None
        if is_media_column(c) or kind is not None:
            caps.add("media")
            c.media_kind = kind or c.media_kind or "unknown"
        # Key detection must see media inferred from this preview page too, not just a
        # pre-existing producer declaration.
        c.capabilities = sorted(caps)
        if is_vector_column(c):
            caps.add("vector")
        if is_key_column(c):
            caps.add("key")
        for cap_id, detect in _EXTRA_DETECTORS:
            try:
                if detect(c):
                    caps.add(cap_id)
            except Exception:  # noqa: BLE001 — a plugin detector must never break column tagging
                pass
        c.capabilities = sorted(caps)
    return columns


# A registered capability contributes its id + label to KernelInfo (Deps.info / GET /api/kernel). It may
# ALSO carry an optional `detect(col)->bool` — if present, reg.add_capability registers it (via
# register_detector) so tag_columns tags matching columns with the capability id, no core edit needed.
# (The per-capability viewer UI is still a separate FRONTEND registration in web/src/nodes/capabilities.tsx.)
# The built-in media/vector below have no detect attr — their detection is the hardcoded heuristics above.
class MediaCapability:
    id = "media"
    label = "Media"


class VectorCapability:
    id = "vector"
    label = "Vectors"


BUILTIN_CAPABILITIES = [MediaCapability(), VectorCapability()]
