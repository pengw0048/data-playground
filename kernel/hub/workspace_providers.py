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
    ProviderSearchPage,
    ReadOnlyCatalogProvider,
    bounded_ancestors,
    bounded_dataset_detail,
    bounded_list_children,
    bounded_resolve,
    bounded_search,
)

_EXTERNAL_PREFIX = "external."
_PROVIDER_DATASET_URI_PREFIX = "workspace-provider://"
_CURSOR_VERSION = 2
_SEARCH_CURSOR_VERSION = 1
_MAX_MOUNTS = 8
_MAX_CONFIG_BYTES = 1024 * 1024
_SOURCE_STATES = {
    "complete", "page", "pending", "partial", "unavailable", "unsupported",
}
_ERROR_STATES = {"partial", "unavailable", "unsupported"}


class ProviderRelinkUnavailable(RuntimeError):
    """The explicitly selected replacement could not be resolved right now."""


class ProviderDatasetUnavailable(RuntimeError):
    """A stable provider dataset binding cannot currently authorize a physical read."""


class ProviderDatasetGone(ProviderDatasetUnavailable):
    """A stable provider dataset binding is terminally absent and must be explicitly relinked."""


class ProviderDatasetOffline(ProviderDatasetUnavailable):
    """A valid provider dataset binding could not be read because its provider is offline."""


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


def is_configured_mount_container(container_id: str) -> bool:
    """Whether operator configuration reserves this local Folder as a provider mount point."""
    mounts, _invalid = _configured_mounts()
    return any(mounted.container_id == container_id for mounted in mounts)


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


def _external_identity(mount_id: str, resource_id: str, binding_id: str) -> str:
    payload = json.dumps([mount_id, resource_id, binding_id], separators=(",", ":")).encode()
    return _EXTERNAL_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_external_identity(identity: str) -> tuple[str, str, str]:
    if not identity.startswith(_EXTERNAL_PREFIX):
        raise KeyError("invalid external Workspace resource reference")
    try:
        encoded = identity.removeprefix(_EXTERNAL_PREFIX)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        mount_id, resource_id, binding_id = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise KeyError("invalid external Workspace resource reference") from exc
    if (not isinstance(mount_id, str) or not mount_id or len(mount_id) > 128
            or not isinstance(resource_id, str) or not resource_id or len(resource_id) > 512):
        raise KeyError("invalid external Workspace resource reference")
    if (not isinstance(binding_id, str) or len(binding_id) != 32
            or any(char not in "0123456789abcdef" for char in binding_id)):
        raise KeyError("invalid external Workspace resource reference")
    return mount_id, resource_id, binding_id


def provider_dataset_uri(binding_id: str) -> str:
    """Return the only provider Source identity persisted in a Canvas document."""
    if (len(binding_id) != 32
            or any(char not in "0123456789abcdef" for char in binding_id)):
        raise ValueError("invalid Workspace provider dataset binding")
    return f"{_PROVIDER_DATASET_URI_PREFIX}{binding_id}"


def is_provider_dataset_uri(uri: str) -> bool:
    return uri.startswith(_PROVIDER_DATASET_URI_PREFIX)


def provider_dataset_identity(uri: str) -> str | None:
    """Map one synthetic Source URI to its ABA-fenced Workspace dataset identity."""
    if not is_provider_dataset_uri(uri):
        return None
    binding_id = uri.removeprefix(_PROVIDER_DATASET_URI_PREFIX)
    if (len(binding_id) != 32
            or any(char not in "0123456789abcdef" for char in binding_id)):
        raise ProviderDatasetUnavailable("provider dataset binding is invalid")
    binding = metadb.workspace_provider_binding(binding_id)
    if binding is None or binding["kind"] != "dataset":
        raise ProviderDatasetGone("provider dataset binding is unavailable")
    return f"workspace-provider:{binding_id}"


class _BoundProviderDatasetAdapter:
    """Translate one synthetic stable URI into a provider-owned physical adapter binding.

    Optional adapter capabilities remain feature-detected through ``__getattr__``: a mutable-only
    adapter does not accidentally satisfy ``DatasetRevisionAdapter`` or ``DatasetPreviewAdapter``.
    """

    _URI_METHODS = {
        "scan", "preview_scan", "schema", "count", "metadata_count", "fingerprint",
        "resolve_revision", "open_revision", "preview_revision",
    }

    def __init__(self, source_uri: str, physical_uri: str, adapter: object):
        self.source_uri = source_uri
        self.physical_uri = physical_uri
        self.adapter = adapter
        self.name = str(getattr(adapter, "name", "") or "")

    def matches(self, uri: str) -> bool:
        return uri == self.source_uri

    def __getattr__(self, name: str):
        if name in {"write", "revision_history", "revision_detail", "nearest"}:
            raise AttributeError(f"read-only provider dataset adapter does not expose {name}")
        value = getattr(self.adapter, name)
        if name not in self._URI_METHODS or not callable(value):
            return value

        def invoke(uri: str, *args, **kwargs):
            if uri != self.source_uri:
                raise ValueError("provider dataset adapter received a mismatched binding")
            return value(self.physical_uri, *args, **kwargs)

        return invoke


def provider_dataset_adapter(uri: str, resolve_physical: Callable[[str], object]) -> object:
    """Authorize and bind one stable Workspace identity to its installed DatasetAdapter."""
    dataset_id = provider_dataset_identity(uri)
    if dataset_id is None:
        raise LookupError("not a Workspace provider dataset URI")
    binding_id = dataset_id.removeprefix("workspace-provider:")
    binding = metadb.workspace_provider_binding(binding_id)
    assert binding is not None
    if binding["referenceState"] == "detached":
        raise ProviderDatasetGone("provider dataset was deleted; relink it explicitly")
    mounts, _invalid = _configured_mounts()
    mounted = next((item for item in mounts if item.mount.id == binding["mountId"]), None)
    if mounted is None or mounted.mount.provider != binding["provider"]:
        metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error", error="catalog mount is not configured")
        raise ProviderDatasetUnavailable("provider dataset mount is unavailable")
    try:
        provider = _load_provider(mounted.mount.provider)
    except Exception as exc:  # noqa: BLE001 -- activation details/configuration stay sanitized
        metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error", error=_activation_error())
        raise ProviderDatasetUnavailable(_activation_error()) from exc
    result = bounded_dataset_detail(provider, mounted.mount, binding["resourceId"])
    if result.state != "ready" or result.item is None:
        state = _reference_state(result.failure, result.state)
        metadb.workspace_provider_mark_binding(
            binding_id, state=state, error=result.reason)
        if state == "permission_lost":
            raise PermissionError("permission to read provider dataset was lost")
        if state == "detached":
            raise ProviderDatasetGone("provider dataset was deleted; relink it explicitly")
        if state == "offline":
            raise ProviderDatasetOffline("provider dataset is offline")
        raise ProviderDatasetUnavailable("provider dataset detail is invalid")
    item = result.item
    if item.id != binding["resourceId"] or item.kind != "dataset" or not item.uri:
        metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error",
            error="catalog provider returned a mismatched dataset binding")
        raise ProviderDatasetUnavailable("provider returned a mismatched dataset binding")
    cached = metadb.workspace_provider_cache_resource(
        mount_id=mounted.mount.id, provider=mounted.mount.provider,
        container_id=mounted.container_id, resource_id=item.id, kind=item.kind,
        name=item.name, parent_binding_id=binding.get("parentBindingId"),
    )
    if cached["bindingId"] != binding_id or cached["referenceState"] != "current":
        raise ProviderDatasetUnavailable("provider dataset binding is no longer current")
    try:
        adapter = resolve_physical(item.uri)
    except Exception as exc:
        raise ProviderDatasetUnavailable("provider dataset adapter is unavailable") from exc
    return _BoundProviderDatasetAdapter(uri, item.uri, adapter)


def provider_dataset_supports_exact(adapter: object) -> bool:
    """Feature-detect exact evidence on the physical adapter hidden by a read-only binding."""
    from hub.backends import DatasetRevisionAdapter

    physical = adapter.adapter if isinstance(adapter, _BoundProviderDatasetAdapter) else adapter
    return isinstance(physical, DatasetRevisionAdapter)


def provider_dataset_dispatch_uri(adapter: object, source_uri: str) -> str:
    """Return the request-local physical URI only from an already authorized stable binding."""
    if (not isinstance(adapter, _BoundProviderDatasetAdapter)
            or adapter.source_uri != source_uri):
        raise ProviderDatasetUnavailable("provider dataset dispatch binding is unavailable")
    return adapter.physical_uri


def provider_dataset_inspection_graph(
    graph, target_node_id: str | None, resolve_adapter: Callable[[str], object],
):
    """Bind mutable provider Sources only on a private preview/profile graph.

    The logical Workspace URI remains the visible identity. The physical URI is a one-request kernel
    capability, and disabling the warm cache prevents a mutable head from being reused after it changes.
    """
    from hub import graph as graph_mod

    bound = graph.model_copy(deep=True)
    cone = graph_mod.upstream_chain(bound, target_node_id) if target_node_id else bound.nodes
    for node in cone:
        if node.type != "source" or not isinstance(node.data, dict):
            continue
        config = node.data.get("config")
        if not isinstance(config, dict):
            continue
        config.pop("_input_provider_preview_uri", None)
        source_uri = str(config.get("uri") or "")
        if not is_provider_dataset_uri(source_uri):
            continue
        adapter = resolve_adapter(source_uri)
        config["_input_provider_preview_uri"] = provider_dataset_dispatch_uri(
            adapter, source_uri)
        config["cacheable"] = False
    return bound


def provider_dataset_source(resource_ref: str, *, uid: str,
                            resolve_physical: Callable[[str], object]) -> dict:
    """Create one minimal Source config from a live stable provider dataset reference."""
    resolution = resolve(resource_ref, uid=uid)
    resource = resolution.get("resource")
    source = resolution.get("source") or {}
    if (not isinstance(resource, dict) or resource.get("kind") != "dataset"
            or resource.get("source") != "provider"):
        raise ValueError("only a provider dataset can be used as a Source")
    if source.get("completeness") != "complete" or resource.get("lastKnown"):
        state = resource.get("referenceState") or source.get("referenceState")
        if state == "permission_lost":
            raise PermissionError("permission to read provider dataset was lost")
        if state == "detached":
            raise ProviderDatasetGone("provider dataset was deleted; relink it explicitly")
        if state == "provider_error":
            raise ProviderDatasetUnavailable("provider dataset metadata is invalid")
        raise ProviderDatasetOffline("provider dataset is offline")
    binding_id = str(resource.get("bindingId") or "")
    uri = provider_dataset_uri(binding_id)
    adapter = provider_dataset_adapter(uri, resolve_physical)
    dataset_id = provider_dataset_identity(uri)
    assert dataset_id is not None
    config: dict[str, object] = {
        "uri": uri,
        "providerResourceRef": resource_ref,
        "providerMountId": resource.get("mountId"),
        "providerName": resource.get("provider"),
    }
    from hub.models import ExactDatasetRef
    from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline
    read_mode = "mutable"
    if provider_dataset_supports_exact(adapter):
        try:
            evidence = adapter.resolve_revision(uri)
            revision_id = str(evidence.get("revision_id") or "")
            config["datasetRef"] = ExactDatasetRef(
                kind="exact", dataset_id=dataset_id, revision_id=revision_id,
                last_known={"committedAt": evidence.get("committed_at")},
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
        except (PermissionError, RevisionPermissionLost) as exc:
            raise PermissionError(
                "permission to read the provider dataset was lost") from exc
        except (ConnectionError, TimeoutError, OSError, RevisionProviderOffline) as exc:
            raise ProviderDatasetOffline("provider dataset is offline") from exc
        except Exception as exc:
            raise ProviderDatasetUnavailable(
                "provider could not prove one readable exact dataset revision") from exc
        read_mode = "exact"
    config["providerReadMode"] = read_mode
    return {
        "id": f"source-{os.urandom(16).hex()}",
        "type": "source",
        "position": {"x": 160, "y": 160},
        "data": {
            "title": resource["name"], "status": "draft", "config": config,
        },
    }


def _binding_resource(binding: dict, mounted: _MountedProvider) -> dict:
    identity = _external_identity(
        binding["mountId"], binding["resourceId"], binding["bindingId"])
    parent = (
        metadb.workspace_provider_binding(binding["parentBindingId"])
        if binding.get("parentBindingId") else None
    )
    parent_id = (
        f"container:{_external_identity(parent['mountId'], parent['resourceId'], parent['bindingId'])}"
        if parent is not None else f"container:{binding['containerId']}"
    )
    state = binding["referenceState"]
    anchor = (metadb.workspace_provider_overlay_anchor(binding["bindingId"])
              if binding["kind"] == "container" else None)
    if anchor is None and binding["kind"] == "container":
        # The normal path is a pure metadata read.  A legacy 0033 binding has no anchor until its
        # first display, including when its provider is currently unavailable, so only that miss
        # takes the narrow Workspace write lock to install the durable local capability.
        anchor = metadb.workspace_provider_ensure_overlay_anchor(binding["bindingId"])
    local_placement = None
    if anchor is not None:
        local_placement = {
            "writable": True,
            "canCreateCanvas": True,
            "canMoveCanvas": True,
            "containerId": anchor["containerId"],
            "containerVersion": anchor["containerVersion"],
            "recoveryState": anchor["recoveryState"],
        }
    return {
        "id": f"{binding['kind']}:{identity}",
        "kind": binding["kind"],
        "name": binding["name"],
        "parentId": parent_id,
        "detached": state == "detached",
        "source": "provider",
        "mountId": binding["mountId"],
        "provider": binding["provider"],
        "resourceId": binding["resourceId"],
        "bindingId": binding["bindingId"],
        "referenceState": state,
        "lastKnown": state != "current",
        "lastResolvedAt": binding["lastResolvedAt"],
        "localPlacement": local_placement,
        "providerMutation": False,
        "canCreateFolder": False,
        "canRenameFolder": False,
        "canDeleteFolder": False,
        "folderMutationUnavailableReason": "This provider location does not support local Folder changes.",
    }


def _workspace_resource(
    item: CatalogResource, mounted: _MountedProvider, *, parent_binding_id: str | None = None,
) -> dict:
    parent_is_known = item.parent_id is None or parent_binding_id is not None
    if parent_binding_id is None and item.parent_id is not None:
        parent = metadb.workspace_provider_binding_for_resource(
            mount_id=mounted.mount.id,
            provider=mounted.mount.provider,
            resource_id=item.parent_id,
        )
        parent_binding_id = parent["bindingId"] if parent is not None else None
        parent_is_known = parent is not None
    binding = metadb.workspace_provider_cache_resource(
        mount_id=mounted.mount.id,
        provider=mounted.mount.provider,
        container_id=mounted.container_id,
        resource_id=item.id,
        kind=item.kind,
        name=item.name,
        parent_binding_id=parent_binding_id,
        parent_is_known=parent_is_known,
    )
    return _binding_resource(binding, mounted)


def _source_status(
    source: _Source, completeness: str, error: str | None = None,
    reference_state: str | None = None,
) -> dict:
    if source.kind == "local":
        return {"id": "local", "kind": "local", "completeness": completeness,
                "error": error, "referenceState": reference_state}
    if source.kind == "configuration":
        return {"id": "configuration", "kind": "configuration",
                "completeness": completeness, "error": error,
                "referenceState": reference_state}
    assert source.mounted is not None
    return {
        "id": f"mount:{source.mounted.mount.id}",
        "kind": "provider",
        "mountId": source.mounted.mount.id,
        "provider": source.mounted.mount.provider,
        "completeness": completeness,
        "error": error,
        "referenceState": reference_state,
    }


def _cursor_encode(layout: str, container_id: str, fingerprint: str, source_index: int,
                   source_cursor: str | None, history: list[tuple[str, str | None]]) -> str:
    raw = json.dumps(
        [_CURSOR_VERSION, layout, container_id, fingerprint, source_index, source_cursor, history],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(cursor: str | None, *, layout: str, container_id: str, fingerprint: str,
                   source_count: int) -> tuple[int, str | None, list[tuple[str, str | None]]]:
    if cursor is None:
        return 0, None, []
    if len(cursor) > 16_384:
        raise ValueError("invalid Workspace cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        version, bound_layout, bound_container, bound_fingerprint, source_index, source_cursor, history = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Workspace cursor") from exc
    valid_history = (
        isinstance(history, list)
        and all(isinstance(item, list) and len(item) == 2
                and item[0] in _SOURCE_STATES
                and (item[1] is None or isinstance(item[1], str) and len(item[1]) <= 512)
                for item in history)
    )
    if (version != _CURSOR_VERSION or bound_layout != layout or bound_container != container_id
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
        cursor, layout="local-mounted", container_id=container_id, fingerprint=fingerprint,
        source_count=len(sources))
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
        _cursor_encode("local-mounted", container_id, fingerprint, *next_state)
        if next_state is not None else None
    )
    partial = any(status["completeness"] in _ERROR_STATES for status in statuses)
    if local_mounts:
        container = {
            **container,
            "canDeleteFolder": False,
            "folderMutationUnavailableReason": (
                "This Folder is configured as a provider mount point and cannot be deleted."
            ),
        }
    return {
        "container": container,
        "items": items,
        "nextCursor": next_cursor,
        "hasMore": next_cursor is not None,
        "completeness": "partial" if partial else "page" if next_cursor else "complete",
        "sources": statuses,
    }


def _remote_page(identity: str, *, uid: str, limit: int, cursor: str | None,
                 mounts: list[_MountedProvider], invalid: bool) -> dict:
    mount_id, resource_id, binding_id = _decode_external_identity(identity)
    cached = metadb.workspace_provider_binding(
        binding_id, mount_id=mount_id, resource_id=resource_id)
    if cached is None:
        raise KeyError("Workspace provider binding not found")
    # A pre-0034 cached container has no anchor until it is first exposed.  Direct external
    # browsing must take the same one-time recovery path as root browse; subsequent reads stay
    # metadata-only and never acquire the Workspace write lock.
    if metadb.workspace_provider_overlay_anchor(binding_id) is None:
        metadb.workspace_provider_ensure_overlay_anchor(binding_id)
    mounted = next((item for item in mounts if item.mount.id == mount_id), None)
    cached_mount = _MountedProvider(
        CatalogMount(id=mount_id, provider=cached["provider"], config={}), cached["containerId"])
    provider_source = _Source("provider", mounted) if mounted is not None else _Source("configuration")
    sources = [_Source("local"), provider_source]
    fingerprint = _mount_fingerprint([mounted], invalid) if mounted is not None else _mount_fingerprint([], invalid)
    source_index, source_cursor, history = _cursor_decode(
        cursor, layout="external-overlay", container_id=identity, fingerprint=fingerprint,
        source_count=len(sources))
    statuses = [
        _source_status(source, *(history[index] if index < len(history) else ("pending", None)))
        for index, source in enumerate(sources)
    ]
    items: list[dict] = []
    public_container = _binding_resource(cached, cached_mount)
    next_state: tuple[int, str | None, list[tuple[str, str | None]]] | None = None
    current = source_index

    while current < len(sources) and len(items) < limit:
        source = sources[current]
        remaining = limit - len(items)
        if source.kind == "local":
            overlay = metadb.workspace_provider_overlay_browse(
                binding_id, uid=uid, limit=remaining, cursor=source_cursor)
            items.extend(overlay["items"])
            if overlay["nextCursor"] is not None:
                statuses[current] = _source_status(source, "page")
                next_state = (current, overlay["nextCursor"], history)
                break
            statuses[current] = _source_status(source, "complete")
        elif mounted is None:
            cached = metadb.workspace_provider_mark_binding(
                binding_id, state="provider_error", error="catalog mount is not configured")
            public_container = _binding_resource(cached, cached_mount)
            statuses[current] = _source_status(
                source, "unavailable", "catalog mount is not configured", "provider_error")
        elif cached["provider"] != mounted.mount.provider:
            cached = metadb.workspace_provider_mark_binding(
                binding_id, state="provider_error", error="catalog mount provider changed")
            public_container = _binding_resource(cached, mounted)
            statuses[current] = _source_status(
                source, "unavailable", "catalog mount provider changed", "provider_error")
        elif cached["referenceState"] == "detached":
            statuses[current] = _source_status(
                source, "unavailable", cached.get("lastError") or "resource is detached", "detached")
        else:
            try:
                provider = _load_provider(mounted.mount.provider)
            except Exception:  # noqa: BLE001 -- activation failure is an honest source result
                cached = metadb.workspace_provider_mark_binding(
                    binding_id, state="offline", error=_activation_error())
                public_container = _binding_resource(cached, mounted)
                statuses[current] = _source_status(source, "unavailable", _activation_error(), "offline")
            else:
                resolved = bounded_resolve(provider, mounted.mount, resource_id)
                if resolved.state != "ready" or resolved.item is None:
                    state = _reference_state(resolved.failure, resolved.state)
                    cached = metadb.workspace_provider_mark_binding(
                        binding_id, state=state, error=resolved.reason)
                    public_container = _binding_resource(cached, mounted)
                    statuses[current] = _source_status(source, resolved.state, resolved.reason, state)
                elif resolved.item.id != resource_id or resolved.item.kind != "container":
                    raise KeyError(f"Workspace resource 'container:{identity}' is not a container")
                else:
                    ancestry = bounded_ancestors(provider, mounted.mount, resource_id)
                    provider_ancestors = [item for item in ancestry.items if item.kind == "container"]
                    dropped = len(provider_ancestors) != len(ancestry.items)
                    parent_binding_id: str | None = None
                    ancestry_completeness = "complete"
                    ancestry_error: str | None = None
                    if ancestry.state == "ready" and not dropped:
                        for ancestor in provider_ancestors:
                            parent = _workspace_resource(
                                ancestor, mounted, parent_binding_id=parent_binding_id)
                            parent_binding_id = parent["bindingId"]
                        container = _workspace_resource(
                            resolved.item, mounted, parent_binding_id=parent_binding_id)
                    else:
                        # A bounded ancestor failure must not rewrite a last-known parent to root.
                        # Refresh the target display facts only and preserve its cached ancestry.
                        binding = metadb.workspace_provider_cache_resource(
                            mount_id=mounted.mount.id, provider=mounted.mount.provider,
                            container_id=mounted.container_id, resource_id=resolved.item.id,
                            kind=resolved.item.kind, name=resolved.item.name,
                            parent_is_known=False)
                        container = _binding_resource(binding, mounted)
                        ancestry_completeness = (
                            "partial" if ancestry.state == "ready" else ancestry.state)
                        ancestry_error = (
                            "catalog provider returned a non-container ancestor"
                            if dropped else ancestry.reason)
                        container = {**container, "lastKnown": True}
                    public_container = container
                    page = bounded_list_children(
                        provider, mounted.mount, resource_id, limit=remaining, cursor=source_cursor)
                    if len(page.items) > remaining:
                        statuses[current] = _source_status(
                            source, "unavailable", "catalog provider exceeded the requested browse limit")
                    else:
                        items.extend(
                            _workspace_resource(item, mounted, parent_binding_id=container["bindingId"])
                            for item in page.items)
                        if page.state == "ready" and page.next_cursor is not None:
                            if not page.items:
                                statuses[current] = _source_status(
                                    source, "unavailable", "catalog provider returned a non-advancing page")
                            else:
                                statuses[current] = _source_status(
                                    source,
                                    "page" if ancestry_completeness == "complete"
                                    else ancestry_completeness,
                                    ancestry_error,
                                )
                                next_state = (current, page.next_cursor, history)
                                break
                        else:
                            completeness = "complete" if page.state == "ready" else page.state
                            statuses[current] = _source_status(
                                source,
                                ancestry_completeness if completeness == "complete"
                                else completeness,
                                ancestry_error if completeness == "complete" else page.reason,
                            )

        current += 1
        source_cursor = None
        history = [(status["completeness"], status.get("error")) for status in statuses[:current]]
        if len(items) >= limit and current < len(sources):
            next_state = (current, None, history)
            break

    next_cursor = (
        _cursor_encode("external-overlay", identity, fingerprint, *next_state)
        if next_state is not None else None
    )
    partial = any(status["completeness"] in _ERROR_STATES for status in statuses)
    return {
        "container": public_container, "items": items,
        "nextCursor": next_cursor, "hasMore": next_cursor is not None,
        "completeness": "partial" if partial else "page" if next_cursor else "complete",
        "sources": statuses,
    }


def browse(container_id: str, *, uid: str, limit: int = 50,
           cursor: str | None = None) -> dict:
    """Browse local and mounted providers with one bounded, source-stable cursor."""
    limit = max(1, min(int(limit), metadb._WORKSPACE_BROWSE_MAX_LIMIT))
    mounts, invalid = _configured_mounts()
    if container_id.startswith(_EXTERNAL_PREFIX):
        return _remote_page(container_id, uid=uid, limit=limit, cursor=cursor,
                            mounts=mounts, invalid=invalid)
    return _mixed_page(container_id, uid=uid, limit=limit, cursor=cursor,
                       mounts=mounts, invalid=invalid)


def _unavailable_resolution(source: _Source, completeness: str, error: str | None) -> dict:
    return {"resource": None, "ancestors": [],
            "source": _source_status(source, completeness, error)}


def _reference_state(failure: str | None, provider_state: str) -> str:
    if failure == "permission_lost":
        return "permission_lost"
    if failure == "not_found":
        return "detached"
    if failure == "offline":
        return "offline"
    if failure == "provider_error" or provider_state == "unsupported":
        return "provider_error"
    return "provider_error"


def _cached_resolution(
    binding: dict, mounted: _MountedProvider, source: _Source, *, uid: str, completeness: str,
    error: str | None,
) -> dict:
    try:
        local_parent = metadb.workspace_resolve(
            f"container:{binding['containerId']}", uid=uid)
        local_ancestors = [*local_parent["ancestors"], local_parent["resource"]]
    except KeyError:
        local_ancestors = []
    cached_ancestors = [
        _binding_resource(item, mounted)
        for item in metadb.workspace_provider_binding_ancestors(binding["bindingId"])
    ]
    return {
        "resource": _binding_resource(binding, mounted),
        "ancestors": [*local_ancestors, *cached_ancestors],
        "source": _source_status(
            source, completeness, error, binding["referenceState"]),
    }


def _overlay_cached_resolution(
    resource: dict, binding: dict, mounted: _MountedProvider, source: _Source, *, uid: str,
    completeness: str, error: str | None,
) -> dict:
    """Resolve a local Canvas through cached external ancestry without reading its provider."""
    try:
        local_parent = metadb.workspace_resolve(
            f"container:{binding['containerId']}", uid=uid)
        local_ancestors = [*local_parent["ancestors"], local_parent["resource"]]
    except KeyError:
        local_ancestors = []
    cached_ancestors = [
        _binding_resource(item, mounted)
        for item in metadb.workspace_provider_binding_ancestors(binding["bindingId"])
    ]
    return {
        "resource": resource,
        "ancestors": [*local_ancestors, *cached_ancestors, _binding_resource(binding, mounted)],
        "source": _source_status(
            source, completeness, error, binding["referenceState"]),
    }


def resolve(resource_ref: str, *, uid: str) -> dict:
    """Resolve local or provider identity and bounded ancestors without catalog materialization."""
    try:
        kind, identity = resource_ref.split(":", 1)
    except ValueError as exc:
        raise KeyError("invalid Workspace resource reference") from exc
    if kind not in {"container", "dataset"} or not identity.startswith(_EXTERNAL_PREFIX):
        try:
            return metadb.workspace_resolve(resource_ref, uid=uid)
        except KeyError:
            overlay = metadb.workspace_provider_overlay_resolve(resource_ref, uid=uid)
            binding = overlay["binding"]
            mounts, _invalid = _configured_mounts()
            mounted = next((item for item in mounts if item.mount.id == binding["mountId"]), None)
            if mounted is None:
                binding = metadb.workspace_provider_mark_binding(
                    binding["bindingId"], state="provider_error",
                    error="catalog mount is not configured")
                cached_mount = _MountedProvider(
                    CatalogMount(id=binding["mountId"], provider=binding["provider"], config={}),
                    binding["containerId"],
                )
                return _overlay_cached_resolution(
                    overlay["resource"], binding, cached_mount, _Source("configuration"), uid=uid,
                    completeness="unavailable", error="catalog mount is not configured")
            source = _Source("provider", mounted)
            if binding["provider"] != mounted.mount.provider:
                binding = metadb.workspace_provider_mark_binding(
                    binding["bindingId"], state="provider_error", error="catalog mount provider changed")
                return _overlay_cached_resolution(
                    overlay["resource"], binding, mounted, source, uid=uid,
                    completeness="unavailable", error="catalog mount provider changed")
            # Reuse the external-container resolver rather than parallel its provider/cache state
            # machine.  The parent is external, so this recursive call takes the branch below.
            parent = resolve(overlay["resource"]["parentId"], uid=uid)
            if parent["resource"] is not None:
                return {
                    "resource": overlay["resource"],
                    "ancestors": [*parent["ancestors"], parent["resource"]],
                    "source": parent["source"],
                }
            latest = metadb.workspace_provider_binding(binding["bindingId"])
            if latest is None:  # pragma: no cover - FK-protected anchor binding
                raise RuntimeError("Workspace provider overlay binding is missing")
            return _overlay_cached_resolution(
                overlay["resource"], latest, mounted, source, uid=uid,
                completeness=parent["source"]["completeness"],
                error=parent["source"].get("error"),
            )

    mount_id, resource_id, binding_id = _decode_external_identity(identity)
    cached = metadb.workspace_provider_binding(
        binding_id, mount_id=mount_id, resource_id=resource_id)
    if cached is None or cached["kind"] != kind:
        raise KeyError("Workspace provider binding not found")
    mounts, _invalid = _configured_mounts()
    mounted = next((item for item in mounts if item.mount.id == mount_id), None)
    if mounted is None:
        cached = metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error", error="catalog mount is not configured")
        cached_mount = _MountedProvider(
            CatalogMount(id=mount_id, provider=cached["provider"], config={}),
            cached["containerId"],
        )
        return _cached_resolution(
            cached, cached_mount, _Source("configuration"), uid=uid,
            completeness="unavailable", error="catalog mount is not configured")
    source = _Source("provider", mounted)
    if cached["provider"] != mounted.mount.provider:
        cached = metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error", error="catalog mount provider changed")
        return _cached_resolution(
            cached, mounted, source, uid=uid, completeness="unavailable",
            error="catalog mount provider changed")
    if cached["referenceState"] == "detached":
        return _cached_resolution(
            cached, mounted, source, uid=uid, completeness="unavailable",
            error=cached.get("lastError") or "resource is detached")
    try:
        provider = _load_provider(mounted.mount.provider)
    except Exception:  # noqa: BLE001 -- activation failure is isolated from local Workspace reads
        cached = metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error", error=_activation_error())
        return _cached_resolution(
            cached, mounted, source, uid=uid, completeness="unavailable",
            error=_activation_error())

    resolved = bounded_resolve(provider, mounted.mount, resource_id)
    if resolved.state != "ready" or resolved.item is None:
        state = _reference_state(resolved.failure, resolved.state)
        cached = metadb.workspace_provider_mark_binding(
            binding_id, state=state, error=resolved.reason)
        return _cached_resolution(
            cached, mounted, source, uid=uid, completeness=resolved.state,
            error=resolved.reason)
    if resolved.item.id != resource_id or resolved.item.kind != kind:
        cached = metadb.workspace_provider_mark_binding(
            binding_id, state="provider_error",
            error="catalog provider returned a mismatched resource identity")
        return _cached_resolution(
            cached, mounted, source, uid=uid, completeness="unavailable",
            error="catalog provider returned a mismatched resource identity")

    try:
        local_parent = metadb.workspace_resolve(
            f"container:{mounted.container_id}", uid=uid)
    except KeyError:
        return _unavailable_resolution(
            source, "unavailable", "catalog mount container is unavailable")
    ancestors = bounded_ancestors(provider, mounted.mount, resource_id)
    provider_ancestors = [item for item in ancestors.items if item.kind == "container"]
    dropped = len(provider_ancestors) != len(ancestors.items)
    provider_resources: list[dict] = []
    parent_binding_id: str | None = None
    for item in provider_ancestors:
        resource = _workspace_resource(
            item, mounted, parent_binding_id=parent_binding_id)
        provider_resources.append(resource)
        parent_binding_id = resource["bindingId"]
    current = _workspace_resource(
        resolved.item, mounted, parent_binding_id=parent_binding_id)
    completeness = "complete" if ancestors.state == "ready" and not dropped else (
        "partial" if ancestors.state == "ready" else ancestors.state)
    error = (
        "catalog provider returned a non-container ancestor" if dropped else ancestors.reason
    )
    if completeness != "complete":
        # A partial ancestor read must not erase the last-known path.  The target facts are current,
        # but the explanatory navigation context is explicitly stale until Retry converges.
        provider_resources = [
            _binding_resource(item, mounted)
            for item in metadb.workspace_provider_binding_ancestors(current["bindingId"])
        ]
        current = {**current, "lastKnown": True}
    combined = [
        *local_parent["ancestors"], local_parent["resource"], *provider_resources,
    ]
    return {
        "resource": current,
        "ancestors": combined,
        "source": _source_status(source, completeness, error, current["referenceState"]),
    }


def relink(
    resource_ref: str, *, uid: str, mount_id: str, resource_id: str,
) -> dict:
    """Resolve an explicit replacement and mint a new binding; never repair by display name."""
    try:
        kind, identity = resource_ref.split(":", 1)
    except ValueError as exc:
        raise KeyError("invalid Workspace resource reference") from exc
    if kind not in {"container", "dataset"} or not identity.startswith(_EXTERNAL_PREFIX):
        raise ValueError("only external Workspace resources can be relinked")
    old_mount_id, old_resource_id, old_binding_id = _decode_external_identity(identity)
    old = metadb.workspace_provider_binding(
        old_binding_id, mount_id=old_mount_id, resource_id=old_resource_id)
    if old is None or old["kind"] != kind:
        raise KeyError("Workspace provider binding not found")

    mounts, _invalid = _configured_mounts()
    mounted = next((item for item in mounts if item.mount.id == mount_id), None)
    if mounted is None:
        raise KeyError("replacement catalog mount is not configured")
    try:
        provider = _load_provider(mounted.mount.provider)
    except Exception as exc:  # noqa: BLE001 -- activation failure is an unavailable replacement
        raise ProviderRelinkUnavailable(_activation_error()) from exc
    resolved = bounded_resolve(provider, mounted.mount, resource_id)
    if resolved.state != "ready" or resolved.item is None:
        if resolved.failure == "permission_lost":
            raise PermissionError(resolved.reason or "replacement permission was lost")
        if resolved.failure == "not_found":
            raise KeyError(resolved.reason or "replacement resource was not found")
        raise ProviderRelinkUnavailable(
            resolved.reason or "replacement provider is unavailable")
    if resolved.item.id != resource_id:
        raise ProviderRelinkUnavailable(
            "catalog provider returned a mismatched replacement identity")
    if resolved.item.kind != kind:
        raise ValueError("replacement resource kind does not match the detached reference")

    ancestors = bounded_ancestors(provider, mounted.mount, resource_id)
    provider_ancestors = [item for item in ancestors.items if item.kind == "container"]
    parent_binding_id: str | None = None
    for item in provider_ancestors:
        parent = _workspace_resource(
            item, mounted, parent_binding_id=parent_binding_id)
        parent_binding_id = parent["bindingId"]
    previous, fresh = metadb.workspace_provider_relink_binding(
        old_binding_id,
        mount_id=mounted.mount.id,
        provider=mounted.mount.provider,
        container_id=mounted.container_id,
        resource_id=resolved.item.id,
        kind=resolved.item.kind,
        name=resolved.item.name,
        parent_binding_id=parent_binding_id,
    )
    return {
        "ok": True,
        "resource": _binding_resource(fresh, mounted),
        "previousResource": _binding_resource(previous, _MountedProvider(
            CatalogMount(
                id=previous["mountId"], provider=previous["provider"], config={}),
            previous["containerId"],
        )),
    }


def _search_source_status(source: _Source, completeness: str, *,
                          error: str | None = None, freshness: str = "unknown",
                          search_mode: str = "native") -> dict:
    return {
        **_source_status(source, completeness, error),
        "freshness": freshness,
        "searchMode": search_mode,
    }


def _search_cursor_encode(query: str, fingerprint: str, states: list[dict]) -> str:
    raw = json.dumps(
        [_SEARCH_CURSOR_VERSION, query, fingerprint, states],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _search_cursor_decode(cursor: str | None, *, query: str, fingerprint: str,
                          source_ids: list[str]) -> list[dict] | None:
    if cursor is None:
        return None
    if len(cursor) > 32_768:
        raise ValueError("invalid Workspace search cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        version, bound_query, bound_fingerprint, states = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Workspace search cursor") from exc
    valid = (
        version == _SEARCH_CURSOR_VERSION
        and bound_query == query
        and bound_fingerprint == fingerprint
        and isinstance(states, list)
        and len(states) == len(source_ids)
    )
    if not valid:
        raise ValueError("invalid Workspace search cursor")
    for expected_id, state in zip(source_ids, states, strict=True):
        if (not isinstance(state, dict) or set(state) != {
                "id", "active", "cursor", "completeness", "error", "freshness", "searchMode"}
                or state["id"] != expected_id
                or not isinstance(state["active"], bool)
                or state["cursor"] is not None and (
                    not isinstance(state["cursor"], str) or len(state["cursor"]) > 4096)
                or state["active"] != (state["cursor"] is not None)
                or state["active"] != (state["completeness"] == "page")
                or state["completeness"] not in _SOURCE_STATES
                or state["completeness"] == "pending"
                or state["error"] is not None and (
                    not isinstance(state["error"], str) or len(state["error"]) > 512)
                or state["freshness"] not in {"current", "stale", "unknown"}
                or state["searchMode"] not in {"native", "fallback", "unsupported"}):
            raise ValueError("invalid Workspace search cursor")
    return states


def _provider_searches(sources: list[_Source], states: list[dict], *,
                       query: str, limit: int) -> dict[int, ProviderSearchPage | str]:
    """Fan out only active mount searches; every read keeps its own bounded provider deadline."""
    reads: dict[int, ProviderSearchPage | str] = {}
    futures: dict[concurrent.futures.Future[ProviderSearchPage], int] = {}
    active = [
        (index, source) for index, source in enumerate(sources)
        if source.kind == "provider" and states[index]["active"]
    ]
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(active)),
            thread_name_prefix="dp-workspace-search") as executor:
        for index, source in active:
            assert source.mounted is not None
            try:
                provider = _load_provider(source.mounted.mount.provider)
            except Exception:  # noqa: BLE001 -- one activation failure is isolated
                reads[index] = _activation_error()
                continue
            future = executor.submit(
                bounded_search, provider, source.mounted.mount, query,
                limit=limit, cursor=states[index]["cursor"],
            )
            futures[future] = index
        for future, index in futures.items():
            try:
                reads[index] = future.result()
            except Exception:  # noqa: BLE001 -- bounded wrapper should not leak failures
                reads[index] = "catalog provider search failed"
    return reads


def search(query: str, *, uid: str, limit: int = 25,
           cursor: str | None = None) -> dict:
    """Search local metadata and declared provider search surfaces in source-grouped pages."""
    normalized = " ".join(query.split()).lower()
    if not normalized:
        raise ValueError("Workspace search query must not be blank")
    if len(normalized.encode("utf-8")) > 512:
        raise ValueError("Workspace search query must be at most 512 UTF-8 bytes")
    limit = max(1, min(int(limit), metadb._WORKSPACE_BROWSE_MAX_LIMIT))
    mounts, invalid = _configured_mounts()
    sources = [_Source("local"), *(_Source("provider", item) for item in mounts)]
    if invalid:
        sources.append(_Source("configuration"))
    source_ids = [_source_status(source, "pending")["id"] for source in sources]
    fingerprint = _mount_fingerprint(mounts, invalid)
    states = _search_cursor_decode(
        cursor, query=normalized, fingerprint=fingerprint, source_ids=source_ids)
    if states is None:
        states = [{
            "id": source_id, "active": True, "cursor": None,
            "completeness": "pending", "error": None,
            "freshness": "unknown", "searchMode": "native",
        } for source_id in source_ids]

    provider_pages = _provider_searches(
        sources, states, query=normalized, limit=limit)
    groups: list[dict] = []
    next_states: list[dict] = []
    for index, source in enumerate(sources):
        previous = states[index]
        items: list[dict] = []
        if not previous["active"]:
            status = _search_source_status(
                source, previous["completeness"], error=previous["error"],
                freshness=previous["freshness"], search_mode=previous["searchMode"])
            next_state = dict(previous)
        elif source.kind == "local":
            page = metadb.workspace_search(
                normalized, uid=uid, limit=limit, cursor=previous["cursor"])
            items = page["items"]
            completeness = "page" if page["nextCursor"] is not None else "complete"
            status = _search_source_status(
                source, completeness, freshness="current", search_mode="native")
            next_state = {
                "id": previous["id"], "active": page["nextCursor"] is not None,
                "cursor": page["nextCursor"], "completeness": completeness,
                "error": None, "freshness": "current", "searchMode": "native",
            }
        elif source.kind == "configuration":
            status = _search_source_status(
                source, "unavailable", error=_configured_source_error(),
                freshness="unknown", search_mode="unsupported")
            next_state = {
                "id": previous["id"], "active": False, "cursor": None,
                "completeness": "unavailable", "error": _configured_source_error(),
                "freshness": "unknown", "searchMode": "unsupported",
            }
        else:
            provider_page = provider_pages[index]
            if isinstance(provider_page, str):
                completeness, error, freshness, search_mode = (
                    "unavailable", provider_page, "unknown", "native")
                next_cursor = None
            elif len(provider_page.items) > limit:
                completeness, error, freshness, search_mode = (
                    "unavailable", "catalog provider exceeded the requested search limit",
                    provider_page.freshness, "native")
                next_cursor = None
            else:
                seen: set[str] = set()
                for item in provider_page.items:
                    resource = _workspace_resource(item, source.mounted)  # type: ignore[arg-type]
                    if resource["id"] not in seen:
                        seen.add(resource["id"])
                        items.append(resource)
                next_cursor = (
                    provider_page.next_cursor if provider_page.state == "ready" else None)
                completeness = (
                    "page" if provider_page.state == "ready" and next_cursor is not None
                    else "complete" if provider_page.state == "ready" else provider_page.state)
                error = provider_page.reason
                freshness = provider_page.freshness
                search_mode = "unsupported" if provider_page.state == "unsupported" else "native"
                if next_cursor is not None and (
                        not items or next_cursor == previous["cursor"]):
                    completeness, error, next_cursor = (
                        "unavailable", "catalog provider returned a non-advancing search page", None)
                    items = []
            status = _search_source_status(
                source, completeness, error=error,
                freshness=freshness, search_mode=search_mode)
            next_state = {
                "id": previous["id"], "active": next_cursor is not None,
                "cursor": next_cursor, "completeness": completeness,
                "error": error, "freshness": freshness, "searchMode": search_mode,
            }
        groups.append({"source": status, "items": items})
        next_states.append(next_state)

    has_more = any(state["active"] for state in next_states)
    partial = any(
        group["source"]["completeness"] in _ERROR_STATES for group in groups)
    next_cursor = (
        _search_cursor_encode(normalized, fingerprint, next_states) if has_more else None)
    return {
        "query": normalized,
        "groups": groups,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "completeness": "partial" if partial else "page" if has_more else "complete",
    }
