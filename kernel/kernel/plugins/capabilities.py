"""Capability providers — predicate(schema) -> bool + the viewer tabs they add (PRD §5.4).

Capabilities live on columns, not wires. They add actions/viewer tabs to any node whose
data qualifies; they never change what connects.
"""

from __future__ import annotations

import re

from kernel.models import ColumnSchema

_MEDIA_NAME = re.compile(r"(media|image|img|video|thumb|frame|photo|asset|clip|url|uri|path)", re.I)
_MEDIA_EXT = re.compile(r"\.(mp4|mov|mkv|webm|png|jpe?g|gif|webp|wav|mp3|flac)\b", re.I)
_VECTOR_NAME = re.compile(r"(embed|embedding|vector|feature)", re.I)


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


def tag_columns(columns: list[ColumnSchema]) -> list[ColumnSchema]:
    """Annotate columns with detected capability tags (idempotent)."""
    for c in columns:
        caps = set(c.capabilities)
        if is_media_column(c):
            caps.add("media")
        if is_vector_column(c):
            caps.add("vector")
        c.capabilities = sorted(caps)
    return columns


class MediaCapability:
    id = "media"
    label = "Media"

    def predicate(self, columns: list[ColumnSchema]) -> bool:
        return any("media" in c.capabilities for c in columns)

    def columns(self, columns: list[ColumnSchema]) -> list[str]:
        return [c.name for c in columns if "media" in c.capabilities]


class VectorCapability:
    id = "vector"
    label = "Vectors"

    def predicate(self, columns: list[ColumnSchema]) -> bool:
        return any("vector" in c.capabilities for c in columns)

    def columns(self, columns: list[ColumnSchema]) -> list[str]:
        return [c.name for c in columns if "vector" in c.capabilities]


BUILTIN_CAPABILITIES = [MediaCapability(), VectorCapability()]
