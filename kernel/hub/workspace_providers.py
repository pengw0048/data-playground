"""Bounded composition of configured read-only catalog mounts into Workspace browse."""

from __future__ import annotations

import base64
import concurrent.futures
import functools
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Literal

from hub import metadb
from hub.catalog_provider import (
    CatalogMount,
    CatalogResource,
    ProviderPage,
    ReadOnlyCatalogProvider,
    bounded_ancestors,
    bounded_list_children,
    bounded_resolve,
)

_EXTERNAL_PREFIX = "external."
_CURSOR_VERSION = 1
_MAX_MOUNTS = 8
_MAX_CONFIG_BYTES = 1024 * 1024
_SOURCE_STATES = {
    "complete", "page", "pending", "partial", "unavailable", "unsupported",
}
_ERROR_STATES = {"partial", "unavailable", "unsupported"}


@dataclass(frozen=True)
class _MountedProvider:
    mount: CatalogMount
    container_id: str


@dataclass(frozen=True)
class _Source:
    kind: Literal["local", "provider", "configuration"]
    mounted: _MountedProvider | None = None


def _configured_mounts() -> tuple[list[_MountedProvider], bool]:
    """Parse operator-owned mount config while keeping malformed sources isolated from local data."""
    raw = (os.environ.get("DP_CATALOG_MOUNTS") or "").strip()
    if not raw:
        return [], False
    if len(raw.encode("utf-8")) > _MAX_CONFIG_BYTES:
        return [], True
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return [], True
    if not isinstance(document, list) or len(document) > _MAX_MOUNTS:
        return [], True

    mounts: list[_MountedProvider] = []
    seen: set[str] = set()
    invalid = False
    for item in document:
        try:
            if not isinstance(item, dict) or not set(item) <= {
                    "id", "provider", "containerId", "config"}:
                raise ValueError
            config = item.get("config", {})
            if not isinstance(config, dict):
                raise ValueError
            mount = CatalogMount.model_validate({
                "id": item["id"], "provider": item["provider"],
                "config": config,
            })
            container_id = item.get("containerId", metadb.LOCAL_WORKSPACE_ROOT_ID)
            if ("\x00" in mount.id or "\x00" in mount.provider
                    or not isinstance(container_id, str) or not container_id
                    or "\x00" in container_id):
                raise ValueError
            if mount.id in seen:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            invalid = True
            continue
        seen.add(mount.id)
        mounts.append(_MountedProvider(mount=mount, container_id=container_id))
    mounts.sort(key=lambda configured: configured.mount.id)
    return mounts, invalid


@functools.lru_cache(maxsize=64)
def _provider_factory(name: str) -> Callable[[], object]:
    entry = next((item for item in entry_points(group="dataplay.catalog_providers")
                  if item.name == name), None)
    if entry is None:
        raise LookupError("provider entry point is not installed")
    factory = entry.load()
    if not callable(factory):
        raise TypeError("provider entry point is not callable")
    return factory


def _load_provider(name: str) -> ReadOnlyCatalogProvider:
    # A factory invocation per mount/read keeps independent mounts from sharing mutable provider state.
    provider = _provider_factory(name)()
    if not isinstance(provider, ReadOnlyCatalogProvider):
        raise TypeError("entry point did not return a read-only catalog provider")
    return provider


def _mount_fingerprint(mounts: list[_MountedProvider], invalid: bool) -> str:
    # Configuration may contain credentials. Only its digest crosses the API cursor boundary.
    value = [{
        "id": item.mount.id,
        "provider": item.mount.provider,
        "containerId": item.container_id,
        "config": item.mount.config,
    } for item in mounts]
    payload = json.dumps([value, invalid], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:24]


def _external_identity(mount_id: str, resource_id: str) -> str:
    payload = json.dumps([mount_id, resource_id], separators=(",", ":")).encode()
    return _EXTERNAL_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_external_identity(identity: str) -> tuple[str, str]:
    if not identity.startswith(_EXTERNAL_PREFIX):
        raise KeyError("invalid external Workspace resource reference")
    try:
        encoded = identity.removeprefix(_EXTERNAL_PREFIX)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        mount_id, resource_id = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise KeyError("invalid external Workspace resource reference") from exc
    if (not isinstance(mount_id, str) or not mount_id or len(mount_id) > 128
            or not isinstance(resource_id, str) or not resource_id or len(resource_id) > 512):
        raise KeyError("invalid external Workspace resource reference")
    return mount_id, resource_id


def _workspace_resource(item: CatalogResource, mounted: _MountedProvider) -> dict:
    identity = _external_identity(mounted.mount.id, item.id)
    parent_id = (
        f"container:{_external_identity(mounted.mount.id, item.parent_id)}"
        if item.parent_id is not None
        else f"container:{mounted.container_id}"
    )
    return {
        "id": f"{item.kind}:{identity}",
        "kind": item.kind,
        "name": item.name,
        "parentId": parent_id,
        "detached": False,
        "source": "provider",
        "mountId": mounted.mount.id,
        "provider": mounted.mount.provider,
        "resourceId": item.id,
    }


def _source_status(source: _Source, completeness: str, error: str | None = None) -> dict:
    if source.kind == "local":
        return {"id": "local", "kind": "local", "completeness": completeness,
                "error": error}
    if source.kind == "configuration":
        return {"id": "configuration", "kind": "configuration",
                "completeness": completeness, "error": error}
    assert source.mounted is not None
    return {
        "id": f"mount:{source.mounted.mount.id}",
        "kind": "provider",
        "mountId": source.mounted.mount.id,
        "provider": source.mounted.mount.provider,
        "completeness": completeness,
        "error": error,
    }


def _cursor_encode(container_id: str, fingerprint: str, source_index: int,
                   source_cursor: str | None, history: list[tuple[str, str | None]]) -> str:
    raw = json.dumps(
        [_CURSOR_VERSION, container_id, fingerprint, source_index, source_cursor, history],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(cursor: str | None, *, container_id: str, fingerprint: str,
                   source_count: int) -> tuple[int, str | None, list[tuple[str, str | None]]]:
    if cursor is None:
        return 0, None, []
    if len(cursor) > 16_384:
        raise ValueError("invalid Workspace cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        version, bound_container, bound_fingerprint, source_index, source_cursor, history = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Workspace cursor") from exc
    valid_history = (
        isinstance(history, list)
        and all(isinstance(item, list) and len(item) == 2
                and item[0] in _SOURCE_STATES
                and (item[1] is None or isinstance(item[1], str) and len(item[1]) <= 512)
                for item in history)
    )
    if (version != _CURSOR_VERSION or bound_container != container_id
            or bound_fingerprint != fingerprint
            or isinstance(source_index, bool) or not isinstance(source_index, int)
            or source_index < 0 or source_index >= source_count
            or source_cursor is not None and (
                not isinstance(source_cursor, str) or len(source_cursor) > 512)
            or not valid_history or len(history) != source_index):
        raise ValueError("invalid Workspace cursor")
    return source_index, source_cursor, [(item[0], item[1]) for item in history]


def _configured_source_error() -> str:
    return "catalog mount configuration is invalid"


def _activation_error() -> str:
    return "catalog provider activation failed"


def _prefetch_provider_pages(
    sources: list[_Source], start: int, *, limit: int, source_cursor: str | None,
) -> dict[int, ProviderPage | str]:
    """Start all remaining mount reads together so one slow mount cannot serialize healthy ones."""
    reads: dict[int, ProviderPage | str] = {}
    futures: dict[concurrent.futures.Future[ProviderPage], int] = {}
    provider_sources = [
        (index, source) for index, source in enumerate(sources[start:], start)
        if source.kind == "provider"
    ]
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(provider_sources)),
            thread_name_prefix="dp-workspace-mount") as executor:
        for index, source in provider_sources:
            assert source.mounted is not None
            try:
                provider = _load_provider(source.mounted.mount.provider)
            except Exception:  # noqa: BLE001 -- one activation failure must not block other mounts
                reads[index] = _activation_error()
                continue
            cursor = source_cursor if index == start else None
            future = executor.submit(
                bounded_list_children, provider, source.mounted.mount, None,
                limit=limit, cursor=cursor)
            futures[future] = index
        for future, index in futures.items():
            try:
                reads[index] = future.result()
            except Exception:  # noqa: BLE001 -- bounded wrapper should not leak provider failures
                reads[index] = "catalog provider read failed"
    return reads


def _mixed_page(container_id: str, *, uid: str, limit: int,
                cursor: str | None, mounts: list[_MountedProvider], invalid: bool) -> dict:
    local_mounts = [item for item in mounts if item.container_id == container_id]
    sources = [_Source("local"), *(_Source("provider", item) for item in local_mounts)]
    if invalid:
        sources.append(_Source("configuration"))
    fingerprint = _mount_fingerprint(local_mounts, invalid)
    source_index, source_cursor, history = _cursor_decode(
        cursor, container_id=container_id, fingerprint=fingerprint, source_count=len(sources))
    statuses = [
        _source_status(source, *(history[index] if index < len(history) else ("pending", None)))
        for index, source in enumerate(sources)
    ]
    items: list[dict] = []
    container = metadb.workspace_resolve(
        f"container:{container_id}", uid=uid)["resource"]
    next_state: tuple[int, str | None, list[tuple[str, str | None]]] | None = None
    current = source_index
    provider_pages: dict[int, ProviderPage | str] | None = None

    while current < len(sources) and len(items) < limit:
        source = sources[current]
        remaining = limit - len(items)
        if source.kind == "local":
            page = metadb.workspace_browse(
                container_id, uid=uid, limit=remaining, cursor=source_cursor)
            container = page["container"]
            items.extend(page["items"])
            if page["nextCursor"] is not None:
                statuses[current] = _source_status(source, "page")
                next_state = (current, page["nextCursor"], history)
                break
            statuses[current] = _source_status(source, "complete")
        elif source.kind == "configuration":
            statuses[current] = _source_status(
                source, "unavailable", _configured_source_error())
        else:
            assert source.mounted is not None
            if provider_pages is None:
                provider_pages = _prefetch_provider_pages(
                    sources, current, limit=remaining, source_cursor=source_cursor)
            provider_page = provider_pages[current]
            if isinstance(provider_page, str):
                statuses[current] = _source_status(
                    source, "unavailable", provider_page)
            elif len(provider_page.items) > remaining:
                # The prefetched page used the capacity available before earlier sources contributed.
                # Defer it intact and retry from the same opaque cursor; never drop a provider item.
                statuses[current] = _source_status(source, "pending")
                next_state = (current, source_cursor, history)
                break
            else:
                page = provider_page
                if page.items:
                    items.extend(_workspace_resource(item, source.mounted) for item in page.items)
                if page.state == "ready" and page.next_cursor is not None:
                    if not page.items:
                        statuses[current] = _source_status(
                            source, "unavailable", "catalog provider returned a non-advancing page")
                    else:
                        statuses[current] = _source_status(source, "page")
                        next_state = (current, page.next_cursor, history)
                        break
                else:
                    completeness = "complete" if page.state == "ready" else page.state
                    statuses[current] = _source_status(source, completeness, page.reason)

        current += 1
        source_cursor = None
        history = [(status["completeness"], status.get("error"))
                   for status in statuses[:current]]
        if len(items) >= limit and current < len(sources):
            next_state = (current, None, history)
            break

    next_cursor = (
        _cursor_encode(container_id, fingerprint, *next_state)
        if next_state is not None else None
    )
    partial = any(status["completeness"] in _ERROR_STATES for status in statuses)
    return {
        "container": container,
        "items": items,
        "nextCursor": next_cursor,
        "hasMore": next_cursor is not None,
        "completeness": "partial" if partial else "page" if next_cursor else "complete",
        "sources": statuses,
    }


def _remote_page(identity: str, *, limit: int, cursor: str | None,
                 mounts: list[_MountedProvider], invalid: bool) -> dict:
    mount_id, resource_id = _decode_external_identity(identity)
    mounted = next((item for item in mounts if item.mount.id == mount_id), None)
    if mounted is None:
        source = _Source("configuration")
        return {
            "container": None, "items": [], "nextCursor": None, "hasMore": False,
            "completeness": "partial",
            "sources": [_source_status(source, "unavailable", "catalog mount is not configured")],
        }
    source = _Source("provider", mounted)
    fingerprint = _mount_fingerprint([mounted], invalid)
    source_index, source_cursor, history = _cursor_decode(
        cursor, container_id=identity, fingerprint=fingerprint, source_count=1)
    assert source_index == 0 and not history
    try:
        provider = _load_provider(mounted.mount.provider)
    except Exception:  # noqa: BLE001 -- activation failure is an honest source result
        status = _source_status(source, "unavailable", _activation_error())
        return {"container": None, "items": [], "nextCursor": None, "hasMore": False,
                "completeness": "partial", "sources": [status]}

    resolved = bounded_resolve(provider, mounted.mount, resource_id)
    if resolved.state != "ready" or resolved.item is None:
        status = _source_status(source, resolved.state, resolved.reason)
        return {"container": None, "items": [], "nextCursor": None, "hasMore": False,
                "completeness": "partial", "sources": [status]}
    if resolved.item.id != resource_id or resolved.item.kind != "container":
        raise KeyError(f"Workspace resource 'container:{identity}' is not a container")
    container = _workspace_resource(resolved.item, mounted)
    page = bounded_list_children(
        provider, mounted.mount, resource_id, limit=limit, cursor=source_cursor)
    items = [_workspace_resource(item, mounted) for item in page.items]
    next_cursor = None
    if page.state == "ready" and page.next_cursor is not None:
        if not page.items:
            status = _source_status(
                source, "unavailable", "catalog provider returned a non-advancing page")
        else:
            status = _source_status(source, "page")
            next_cursor = _cursor_encode(identity, fingerprint, 0, page.next_cursor, [])
    else:
        completeness = "complete" if page.state == "ready" else page.state
        status = _source_status(source, completeness, page.reason)
    partial = status["completeness"] in _ERROR_STATES
    return {
        "container": container, "items": items, "nextCursor": next_cursor,
        "hasMore": next_cursor is not None,
        "completeness": "partial" if partial else "page" if next_cursor else "complete",
        "sources": [status],
    }


def browse(container_id: str, *, uid: str, limit: int = 50,
           cursor: str | None = None) -> dict:
    """Browse local and mounted providers with one bounded, source-stable cursor."""
    limit = max(1, min(int(limit), metadb._WORKSPACE_BROWSE_MAX_LIMIT))
    mounts, invalid = _configured_mounts()
    if container_id.startswith(_EXTERNAL_PREFIX):
        return _remote_page(container_id, limit=limit, cursor=cursor,
                            mounts=mounts, invalid=invalid)
    return _mixed_page(container_id, uid=uid, limit=limit, cursor=cursor,
                       mounts=mounts, invalid=invalid)


def _unavailable_resolution(source: _Source, completeness: str, error: str | None) -> dict:
    return {"resource": None, "ancestors": [],
            "source": _source_status(source, completeness, error)}


def resolve(resource_ref: str, *, uid: str) -> dict:
    """Resolve local or provider identity and bounded ancestors without catalog materialization."""
    try:
        kind, identity = resource_ref.split(":", 1)
    except ValueError as exc:
        raise KeyError("invalid Workspace resource reference") from exc
    if kind not in {"container", "dataset"} or not identity.startswith(_EXTERNAL_PREFIX):
        return metadb.workspace_resolve(resource_ref, uid=uid)

    mount_id, resource_id = _decode_external_identity(identity)
    mounts, _invalid = _configured_mounts()
    mounted = next((item for item in mounts if item.mount.id == mount_id), None)
    if mounted is None:
        return _unavailable_resolution(
            _Source("configuration"), "unavailable", "catalog mount is not configured")
    source = _Source("provider", mounted)
    try:
        provider = _load_provider(mounted.mount.provider)
    except Exception:  # noqa: BLE001 -- activation failure is isolated from local Workspace reads
        return _unavailable_resolution(source, "unavailable", _activation_error())

    resolved = bounded_resolve(provider, mounted.mount, resource_id)
    if resolved.state != "ready" or resolved.item is None:
        return _unavailable_resolution(source, resolved.state, resolved.reason)
    if resolved.item.id != resource_id or resolved.item.kind != kind:
        return _unavailable_resolution(
            source, "unavailable", "catalog provider returned a mismatched resource identity")

    try:
        local_parent = metadb.workspace_resolve(
            f"container:{mounted.container_id}", uid=uid)
    except KeyError:
        return _unavailable_resolution(
            source, "unavailable", "catalog mount container is unavailable")
    ancestors = bounded_ancestors(provider, mounted.mount, resource_id)
    provider_ancestors = [item for item in ancestors.items if item.kind == "container"]
    dropped = len(provider_ancestors) != len(ancestors.items)
    combined = [
        *local_parent["ancestors"], local_parent["resource"],
        *(_workspace_resource(item, mounted) for item in provider_ancestors),
    ]
    completeness = "complete" if ancestors.state == "ready" and not dropped else (
        "partial" if ancestors.state == "ready" else ancestors.state)
    error = (
        "catalog provider returned a non-container ancestor" if dropped else ancestors.reason
    )
    return {
        "resource": _workspace_resource(resolved.item, mounted),
        "ancestors": combined,
        "source": _source_status(source, completeness, error),
    }
