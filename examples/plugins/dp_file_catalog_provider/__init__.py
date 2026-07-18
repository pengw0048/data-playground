"""Reference read-only provider backed by a JSON catalog document.

The mount configuration must contain ``root`` pointing at a directory with ``catalog.json``.  The
document has ``resources`` entries with opaque ``id``, ``kind``, ``name``, optional ``parentId``, and
for datasets a ``uri`` plus optional ``columns``. An optional provider-owned immutable ``revisionId``
enables exact reads only when upstream guarantees the bytes for that token; absent means mutable
preview-only. The provider never creates, edits, or deletes files.
"""

from __future__ import annotations

import json
import base64
import datetime
import hashlib
from pathlib import Path

from hub.catalog_provider import (
    CatalogMount, CatalogResource, ProviderAncestors, ProviderCapabilities, ProviderPage,
    ProviderResourceResult, ProviderSearchPage,
)
from hub.plugins.adapters import DuckDBAdapter, RevisionUnavailable, relation_columns

_DATASET_SCHEME = "dp-file-catalog://"
_MUTABLE_DATASET_SCHEME = "dp-file-catalog-mutable://"


def _dataset_uri(
    root: Path, resource_id: str, physical_uri: str, revision_id: str | None,
) -> str:
    values = [str(root.resolve()), resource_id, physical_uri]
    if revision_id is not None:
        values.append(revision_id)
    document = json.dumps(
        values, separators=(",", ":"),
    ).encode()
    scheme = _DATASET_SCHEME if revision_id is not None else _MUTABLE_DATASET_SCHEME
    return scheme + base64.urlsafe_b64encode(document).decode().rstrip("=")


def _dataset_binding(uri: str) -> tuple[Path, str | None]:
    exact = uri.startswith(_DATASET_SCHEME)
    mutable = uri.startswith(_MUTABLE_DATASET_SCHEME)
    if not exact and not mutable:
        raise ValueError("not a file catalog dataset URI")
    try:
        scheme = _DATASET_SCHEME if exact else _MUTABLE_DATASET_SCHEME
        encoded = uri.removeprefix(scheme)
        values = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(values, list) or len(values) != (4 if exact else 3):
            raise ValueError
        root_value, _resource_id, physical_uri = values[:3]
        revision_id = values[3] if exact else None
        root = Path(root_value).resolve()
        value = str(physical_uri)
        path = Path(value.removeprefix("file://")) if value.startswith("file://") else root / value
        resolved = path.resolve()
        resolved.relative_to(root)
        if exact and (
            not isinstance(revision_id, str) or not revision_id or len(revision_id) > 256
        ):
            raise ValueError
        return resolved, revision_id
    except Exception as exc:
        raise ValueError("invalid file catalog dataset binding") from exc


class _FileCatalogReadAdapter:
    """Shared read surface for exact and explicitly mutable provider bindings."""

    scheme: str

    def matches(self, uri: str) -> bool:
        return uri.startswith(self.scheme)

    def scan(self, uri: str, columns=None, predicate=None, limit=None, options=None):
        return DuckDBAdapter().scan(
            str(_dataset_binding(uri)[0]), columns=columns, predicate=predicate,
            limit=limit, options=options)

    def preview_scan(self, uri: str, columns=None, limit: int = 2000, options=None):
        return DuckDBAdapter().preview_scan(
            str(_dataset_binding(uri)[0]), columns=columns, limit=limit, options=options)

    def schema(self, uri: str):
        return DuckDBAdapter().schema(str(_dataset_binding(uri)[0]))

    def count(self, uri: str):
        return DuckDBAdapter().count(str(_dataset_binding(uri)[0]))

    def metadata_count(self, uri: str):
        return DuckDBAdapter().metadata_count(str(_dataset_binding(uri)[0]))

    def fingerprint(self, uri: str) -> str:
        path, revision_id = _dataset_binding(uri)
        stat = path.stat()
        evidence = f"{revision_id or 'mutable'}:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha256(evidence.encode()).hexdigest()[:16]

    def write(self, uri: str, rel, mode: str = "overwrite") -> dict:
        del uri, rel, mode
        raise PermissionError("file catalog datasets are read-only")


class FileCatalogMutableDatasetAdapter(_FileCatalogReadAdapter):
    """Read-only latest-value adapter for resources without provider revision evidence."""

    name = "dp-file-catalog-mutable"
    scheme = _MUTABLE_DATASET_SCHEME


class FileCatalogDatasetAdapter(_FileCatalogReadAdapter):
    """Read-only, provider-versioned adapter for explicit immutable provider tokens."""

    name = "dp-file-catalog-exact"
    scheme = _DATASET_SCHEME

    def revision_history(self, uri: str, *, limit: int, cursor: str | None = None):
        if cursor is not None or limit < 1:
            return [], None
        resolved = self.resolve_revision(uri)
        return [{**resolved, "retention_owner": "provider"}], None

    def resolve_revision(self, uri: str, *, as_of=None) -> dict:
        path, revision_id = _dataset_binding(uri)
        assert revision_id is not None
        committed_at = datetime.datetime.fromtimestamp(
            path.stat().st_mtime, tz=datetime.timezone.utc)
        if as_of is not None and committed_at > as_of:
            raise RevisionUnavailable("revision_unavailable")
        return {"revision_id": revision_id, "committed_at": committed_at}

    def open_revision(self, uri: str, revision_id: str):
        path, bound_revision = _dataset_binding(uri)
        if bound_revision != revision_id or not path.is_file():
            raise RevisionUnavailable("revision_unavailable")
        return self.scan(uri)

    def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int) -> dict:
        relation = self.open_revision(uri, revision_id)
        bounded = max(1, min(int(preview_limit), 100))
        preview = relation.limit(bounded + 1).arrow()
        return {
            "revision_id": revision_id,
            "committed_at": self.resolve_revision(uri)["committed_at"],
            "parent_revision_id": None,
            "producer_operation": "external",
            "columns": relation_columns(relation),
            "row_count": self.count(uri),
            "data_file_count": 1,
            "total_bytes": _dataset_binding(uri)[0].stat().st_size,
            "fragment_count": None,
            "preview_table": preview,
        }


class FileCatalogProvider:
    def _resources(self, mount: CatalogMount) -> list[CatalogResource]:
        root = Path(mount.config["root"])
        document = json.loads((root / "catalog.json").read_text())
        resources = []
        for raw in document.get("resources", []):
            item = CatalogResource.model_validate(raw)
            if item.kind == "dataset":
                raw_revision = raw.get("revisionId")
                if raw_revision is not None and (
                    not isinstance(raw_revision, str) or not raw_revision
                    or len(raw_revision) > 256
                ):
                    raise ValueError("revisionId must be a non-empty provider token")
                # Exactness is opt-in: only an explicit provider-owned immutable token can enable the
                # revision adapter. Ordinary file entries remain mutable and preview-only.
                revision_id = raw_revision if isinstance(raw_revision, str) else None
                item = item.model_copy(update={
                    "uri": _dataset_uri(root, item.id, str(item.uri), revision_id),
                })
            resources.append(item)
        if len({item.id for item in resources}) != len(resources):
            raise ValueError("resource IDs must be unique")
        return resources

    def capabilities(self, mount: CatalogMount) -> ProviderCapabilities:
        return ProviderCapabilities(search=True)

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
        return ProviderResourceResult(item=item) if item else ProviderResourceResult(
            state="unavailable", reason="resource not found", failure="not_found")

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
            return ProviderResourceResult(
                state="unsupported", reason="resource is not a dataset",
                failure="provider_error")
        return result

    def search(self, mount: CatalogMount, query: str, *, limit: int,
               cursor: str | None = None) -> ProviderSearchPage:
        tokens = [token.casefold() for token in query.split() if token]
        resources = sorted(
            (item for item in self._resources(mount)
             if all(token in item.name.casefold() for token in tokens)),
            key=lambda item: (item.name.casefold(), item.kind, item.id),
        )
        start = int(cursor) if cursor is not None else 0
        if start < 0 or start > len(resources):
            raise ValueError("cursor is outside this search")
        items = resources[start:start + limit]
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderSearchPage(items=items, next_cursor=next_cursor)


def provider() -> FileCatalogProvider:
    return FileCatalogProvider()


def register(reg) -> None:
    reg.add_adapter(FileCatalogDatasetAdapter())
    reg.add_adapter(FileCatalogMutableDatasetAdapter())
