"""Canonical promoted Transform identities and immutable semantic definitions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from hub.models import ColumnSchema


PROMOTED_TRANSFORM_ID = re.compile(r"tr_[0-9a-f]{29}")
PROMOTED_TRANSFORM_VERSION = re.compile(r"v([1-9][0-9]*)")


def promoted_transform_version_number(value: Any) -> int | None:
    """Parse the one canonical wire spelling for an exact immutable version."""
    if not isinstance(value, str):
        return None
    match = PROMOTED_TRANSFORM_VERSION.fullmatch(value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number <= 2_147_483_647 else None


def promoted_transform_definition(
        *, title: str, blurb: str, category: str, mode: str, code: str,
        input_schema: list[ColumnSchema | dict], output_schema: list[ColumnSchema | dict],
        requirements: list[str]) -> tuple[str, dict]:
    """Return the server-derived digest and canonical semantic definition."""
    def schema_doc(values: list[ColumnSchema | dict]) -> list[dict]:
        return [
            (value.model_dump(by_alias=True, mode="json")
             if isinstance(value, ColumnSchema)
             else ColumnSchema.model_validate(value).model_dump(by_alias=True, mode="json"))
            for value in values
        ]

    doc = {
        "title": title,
        "blurb": blurb,
        "category": category,
        "mode": mode,
        "code": code,
        # Column order is semantic. Requirement declaration order is not.
        "inputSchema": schema_doc(input_schema),
        "outputSchema": schema_doc(output_schema),
        "requirements": sorted(set(requirements)),
    }
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), doc
