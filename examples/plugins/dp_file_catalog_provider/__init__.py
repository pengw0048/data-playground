"""Reference read-only provider backed by a JSON catalog document.

The mount configuration must contain ``root`` pointing at a directory with ``catalog.json``.  The
document has ``resources`` entries with opaque ``id``, ``kind``, ``name``, optional ``parentId``, and
for datasets a ``uri`` plus optional ``columns``. The provider never creates, edits, or deletes files.
"""

from __future__ import annotations

import json
from pathlib import Path

from hub.catalog_provider import (
    CatalogMount, CatalogResource, ProviderAncestors, ProviderCapabilities, ProviderPage,
    ProviderResourceResult,
)


class FileCatalogProvider:
    def _resources(self, mount: CatalogMount) -> list[CatalogResource]:
        root = Path(mount.config["root"])
        document = json.loads((root / "catalog.json").read_text())
        resources = [CatalogResource.model_validate(raw) for raw in document.get("resources", [])]
        if len({item.id for item in resources}) != len(resources):
            raise ValueError("resource IDs must be unique")
        return resources

    def capabilities(self, mount: CatalogMount) -> ProviderCapabilities:
        return ProviderCapabilities()

    def list_children(self, mount: CatalogMount, parent_id: str | None, *, limit: int,
                      cursor: str | None = None) -> ProviderPage:
        resources = sorted((item for item in self._resources(mount) if item.parent_id == parent_id),
                           key=lambda item: (item.name.casefold(), item.id))
        start = int(cursor) if cursor is not None else 0
        if start < 0 or start > len(resources):
            raise ValueError("cursor is outside this collection")
        items = resources[start:start + limit]
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderPage(items=items, next_cursor=next_cursor)

    def resolve(self, mount: CatalogMount, resource_id: str) -> ProviderResourceResult:
        item = next((item for item in self._resources(mount) if item.id == resource_id), None)
        return ProviderResourceResult(item=item) if item else ProviderResourceResult(state="unavailable", reason="resource not found")

    def ancestors(self, mount: CatalogMount, resource_id: str) -> ProviderAncestors:
        by_id = {item.id: item for item in self._resources(mount)}
        current = by_id.get(resource_id)
        if current is None:
            return ProviderAncestors(state="unavailable", reason="resource not found")
        items: list[CatalogResource] = []
        seen = {resource_id}
        while current.parent_id is not None:
            parent_id = current.parent_id
            if parent_id in seen:
                return ProviderAncestors(
                    state="partial", items=list(reversed(items)), reason="ancestor cycle detected")
            current = by_id.get(parent_id)
            if current is None:
                return ProviderAncestors(
                    state="partial", items=list(reversed(items)), reason="ancestor is unavailable")
            items.append(current)
            seen.add(parent_id)
        return ProviderAncestors(items=list(reversed(items)))

    def dataset_detail(self, mount: CatalogMount, resource_id: str) -> ProviderResourceResult:
        result = self.resolve(mount, resource_id)
        if result.item is not None and result.item.kind != "dataset":
            return ProviderResourceResult(state="unsupported", reason="resource is not a dataset")
        return result


def provider() -> FileCatalogProvider:
    return FileCatalogProvider()
