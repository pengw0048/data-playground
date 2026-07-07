"""Capability providers — predicate(schema) -> bool + the viewer tabs they add.

Capabilities live on columns, not wires. They add actions/viewer tabs to any node whose
data qualifies; they never change what connects.
"""

from __future__ import annotations

import re

from hub.models import ColumnSchema

_MEDIA_NAME = re.compile(r"(media|image|img|video|thumb|frame|photo|asset|clip|url|uri|path)", re.I)
_MEDIA_EXT = re.compile(r"\.(mp4|mov|mkv|webm|png|jpe?g|gif|webp|wav|mp3|flac)\b", re.I)
_VECTOR_NAME = re.compile(r"(embed|embedding|vector|feature)", re.I)
# an id-like column name: `id`, `uuid`, `pk`, or a *_id / *_key / *_uid suffix (the usual join keys).
_KEY_NAME = re.compile(r"^(id|uuid|guid|pk)$|_(id|uid|uuid|guid|key|pk)$", re.I)
_KEY_TYPES = {"int", "string", "bytes"}  # a plausible join-key type (not float/bool/vector/media)


def is_media_column(col: ColumnSchema) -> bool:
    if "media" in col.capabilities:
        return True
    t = col.type.lower()
    if t in {"varchar", "string", "text"} and (_MEDIA_NAME.search(col.name) or _MEDIA_EXT.search(col.name)):
        return True
    return False


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


def tag_columns(columns: list[ColumnSchema]) -> list[ColumnSchema]:
    """Annotate columns with detected capability tags (idempotent) — built-in media/vector/key plus any
    plugin-registered detectors."""
    for c in columns:
        caps = set(c.capabilities)
        if is_media_column(c):
            caps.add("media")
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
