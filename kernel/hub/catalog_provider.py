"""Public contract for independent, read-only external catalog mounts.

This module deliberately does not compose mounts into Workspace browse or persist mount settings.
Those are consumer concerns.  A mount is passed to the provider on each read, which keeps local
placement/configuration separate from a provider package and prevents this SPI from implying writes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from hub.models import ColumnSchema, Wire

ProviderState = Literal["ready", "partial", "unavailable", "unsupported"]
ProviderFailure = Literal["offline", "permission_lost", "not_found", "provider_error"]
_PROVIDER_READ_CONCURRENCY = 8
_provider_read_slots = threading.BoundedSemaphore(_PROVIDER_READ_CONCURRENCY)
_provider_read_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_PROVIDER_READ_CONCURRENCY,
    thread_name_prefix="dp-catalog-provider",
)
_R = TypeVar("_R")


class CatalogMount(Wire):
    """One local placement of an installed provider; ``id`` is not a provider resource ID."""

    id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=256)
    config: dict[str, str] = Field(default_factory=dict)


class CatalogResource(Wire):
    """One provider-owned browse/search occurrence.

    ``placement_id`` identifies this occurrence in its provider tree.  A dataset occurrence also
    identifies its canonical provider dataset with ``dataset_id``.  Neither display names nor
    presentation paths are identity.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    placement_id: str = Field(min_length=1, max_length=512)
    kind: Literal["container", "dataset"]
    name: str = Field(min_length=1, max_length=512)
    parent_placement_id: str | None = Field(default=None, max_length=512)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=512)
    uri: str | None = Field(default=None, max_length=8192)
    columns: list[ColumnSchema] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def _dataset_shape(self) -> "CatalogResource":
        if self.kind == "dataset" and (self.dataset_id is None or not self.uri):
            raise ValueError("a dataset occurrence requires a canonical dataset ID and URI")
        if self.kind == "container" and (
            self.dataset_id is not None or self.uri is not None or self.columns
        ):
            raise ValueError("a container resource cannot carry dataset details")
        return self


class CatalogDatasetDetail(Wire):
    """Canonical provider facts for one dataset, independent of any placement occurrence."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=512)
    uri: str = Field(min_length=1, max_length=8192)
    columns: list[ColumnSchema] = Field(default_factory=list, max_length=2048)


class ProviderCapabilities(Wire):
    list_children: bool = True
    resolve: bool = True
    ancestors: bool = True
    dataset_detail: bool = True
    search: bool = False


class ProviderCapabilitiesResult(Wire):
    state: Literal["ready", "unavailable"] = "ready"
    item: ProviderCapabilities | None = None
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _result_shape(self) -> "ProviderCapabilitiesResult":
        if self.state == "ready" and self.item is None:
            raise ValueError("ready provider capabilities require an item")
        if self.state != "ready" and self.item is not None:
            raise ValueError("unavailable provider capabilities cannot carry an item")
        return self


class ProviderPage(Wire):
    state: ProviderState = "ready"
    items: list[CatalogResource] = Field(default_factory=list, max_length=500)
    next_cursor: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _continuation_shape(self) -> "ProviderPage":
        if self.state != "ready" and self.next_cursor is not None:
            raise ValueError("non-ready provider results cannot continue")
        _validate_occurrences(self.items)
        return self


class ProviderSearchPage(ProviderPage):
    """One provider-owned lexical page plus the freshness of that provider's result set."""

    freshness: Literal["current", "stale", "unknown"] = "current"


class ProviderResourceResult(Wire):
    state: ProviderState = "ready"
    item: CatalogResource | None = None
    reason: str | None = Field(default=None, max_length=512)
    failure: ProviderFailure | None = None

    @model_validator(mode="after")
    def _failure_shape(self) -> "ProviderResourceResult":
        if self.state == "ready" and self.failure is not None:
            raise ValueError("a ready provider resource cannot carry a failure")
        if self.state != "ready" and self.item is not None:
            raise ValueError("a failed provider resource cannot carry an item")
        if self.state != "ready" and self.failure is None:
            raise ValueError("a failed provider resource must classify its failure")
        return self


class ProviderDatasetDetailResult(Wire):
    state: ProviderState = "ready"
    item: CatalogDatasetDetail | None = None
    reason: str | None = Field(default=None, max_length=512)
    failure: ProviderFailure | None = None

    @model_validator(mode="after")
    def _failure_shape(self) -> "ProviderDatasetDetailResult":
        if self.state == "ready" and self.failure is not None:
            raise ValueError("a ready provider dataset detail cannot carry a failure")
        if self.state != "ready" and self.item is not None:
            raise ValueError("a failed provider dataset detail cannot carry an item")
        if self.state != "ready" and self.failure is None:
            raise ValueError("a failed provider dataset detail must classify its failure")
        return self


class ProviderAncestors(Wire):
    state: ProviderState = "ready"
    items: list[CatalogResource] = Field(default_factory=list, max_length=128)
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _occurrence_shape(self) -> "ProviderAncestors":
        _validate_occurrences(self.items)
        if any(item.kind != "container" for item in self.items):
            raise ValueError("provider ancestors must be container occurrences")
        for parent, child in zip(self.items, self.items[1:]):
            if child.parent_placement_id != parent.placement_id:
                raise ValueError("provider ancestor occurrences must have consistent parents")
        return self


def _validate_occurrences(items: list[CatalogResource]) -> None:
    placement_ids = [item.placement_id for item in items]
    if len(set(placement_ids)) != len(placement_ids):
        raise ValueError("provider placement IDs must be unique within one result")
    canonical: dict[str, tuple[str, list[ColumnSchema]]] = {}
    for item in items:
        if item.kind != "dataset":
            continue
        assert item.dataset_id is not None and item.uri is not None
        facts = (item.uri, item.columns)
        previous = canonical.setdefault(item.dataset_id, facts)
        if previous != facts:
            raise ValueError("one dataset ID cannot have conflicting canonical facts")


@runtime_checkable
class ReadOnlyCatalogProvider(Protocol):
    """Provider-side reads for one mount. Implementations must not mutate provider state."""

    def capabilities(self, mount: CatalogMount) -> ProviderCapabilities: ...
    def list_children(self, mount: CatalogMount, parent_placement_id: str | None, *, limit: int,
                      cursor: str | None = None) -> ProviderPage: ...
    def resolve(self, mount: CatalogMount, placement_id: str) -> ProviderResourceResult: ...
    def ancestors(self, mount: CatalogMount, placement_id: str) -> ProviderAncestors: ...
    def dataset_detail(self, mount: CatalogMount, dataset_id: str) -> ProviderDatasetDetailResult: ...


def _provider_read(function: Callable[[], _R]) -> _R:
    try:
        return function()
    finally:
        _provider_read_slots.release()


def _bounded_provider_read(
    function: Callable[[], _R],
    *,
    unavailable: Callable[[str], _R],
    unsupported: Callable[[], _R],
    timeout: float,
    failed: Callable[[], _R] | None = None,
) -> _R:
    if timeout <= 0:
        return unavailable("deadline exceeded")
    if not _provider_read_slots.acquire(blocking=False):
        return unavailable("provider busy")
    try:
        future = _provider_read_executor.submit(_provider_read, function)
    except Exception:  # noqa: BLE001 -- executor shutdown is an unavailable provider result
        _provider_read_slots.release()
        return unavailable("provider unavailable")
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        if future.cancel():
            _provider_read_slots.release()
        return unavailable("deadline exceeded")
    except concurrent.futures.CancelledError:
        return unavailable("request cancelled")
    except asyncio.CancelledError:
        return unavailable("request cancelled")
    except NotImplementedError:
        return unsupported()
    except OSError:
        return unavailable("provider unavailable")
    except Exception:  # noqa: BLE001 -- provider failures must not take down local browse
        return failed() if failed is not None else unavailable("provider read failed")


def bounded_list_children(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                          parent_placement_id: str | None, *, limit: int, cursor: str | None = None,
                          timeout: float = 1.0) -> ProviderPage:
    """Return promptly when a synchronous provider is slow, cancelled, or unavailable.

    The worker is intentionally not awaited after a deadline: provider I/O must never block local
    browsing. A provider may return ``partial`` itself when it has a truthful bounded subset.
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    def read() -> ProviderPage:
        raw = provider.list_children(mount, parent_placement_id, limit=limit, cursor=cursor)
        if isinstance(raw, ProviderPage):
            raw = raw.model_dump(mode="python")
        page = ProviderPage.model_validate(raw)
        if any(item.parent_placement_id != parent_placement_id for item in page.items):
            raise ValueError("provider children do not match the requested parent placement")
        if len(page.items) > limit:
            raise ValueError("provider children exceed the requested limit")
        if cursor is not None and page.next_cursor == cursor:
            raise ValueError("provider children returned a non-advancing cursor")
        return page

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderPage(state="unavailable", reason=reason),
        unsupported=lambda: ProviderPage(
            state="unsupported", reason="list_children is unsupported"),
        failed=lambda: ProviderPage(
            state="unavailable", reason="provider list result is invalid"),
        timeout=timeout,
    )


def bounded_capabilities(provider: ReadOnlyCatalogProvider, mount: CatalogMount, *,
                         timeout: float = 1.0) -> ProviderCapabilitiesResult:
    """Read and revalidate provider capabilities under the shared provider deadline."""

    def read() -> ProviderCapabilitiesResult:
        raw = provider.capabilities(mount)
        if isinstance(raw, ProviderCapabilities):
            raw = raw.model_dump(mode="python")
        return ProviderCapabilitiesResult(item=ProviderCapabilities.model_validate(raw))

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderCapabilitiesResult(
            state="unavailable", reason=reason),
        unsupported=lambda: ProviderCapabilitiesResult(
            state="unavailable", reason="capability discovery is unsupported"),
        failed=lambda: ProviderCapabilitiesResult(
            state="unavailable", reason="provider capabilities are invalid"),
        timeout=timeout,
    )


def bounded_resolve(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                    placement_id: str, *, timeout: float = 1.0) -> ProviderResourceResult:
    """Resolve one provider identity without letting synchronous provider I/O block Workspace."""

    def read() -> ProviderResourceResult:
        raw = provider.resolve(mount, placement_id)
        if isinstance(raw, ProviderResourceResult):
            raw = raw.model_dump(mode="python")
        result = ProviderResourceResult.model_validate(raw)
        if result.item is not None and result.item.placement_id != placement_id:
            raise ValueError("provider resolve result does not match the requested placement")
        return result

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderResourceResult(
            state="unavailable", reason=reason, failure="offline"),
        unsupported=lambda: ProviderResourceResult(
            state="unsupported", reason="resolve is unsupported", failure="provider_error"),
        failed=lambda: ProviderResourceResult(
            state="unavailable", reason="provider resolve result is invalid",
            failure="provider_error"),
        timeout=timeout,
    )


def bounded_dataset_detail(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                           dataset_id: str, *, timeout: float = 1.0) -> ProviderDatasetDetailResult:
    """Resolve one bounded dataset read binding without trusting an unvalidated plugin result."""

    def read() -> ProviderDatasetDetailResult:
        raw = provider.dataset_detail(mount, dataset_id)
        if isinstance(raw, ProviderDatasetDetailResult):
            # Pydantic trusts an existing instance by default. Flatten it first so plugins cannot use
            # model_copy/model_construct to bypass this public boundary's nested resource limits.
            raw = raw.model_dump(mode="python")
        result = ProviderDatasetDetailResult.model_validate(raw)
        if result.item is not None and result.item.dataset_id != dataset_id:
            raise ValueError("provider dataset detail does not match the requested dataset")
        return result

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderDatasetDetailResult(
            state="unavailable", reason=reason, failure="offline"),
        unsupported=lambda: ProviderDatasetDetailResult(
            state="unsupported", reason="dataset_detail is unsupported",
            failure="provider_error"),
        failed=lambda: ProviderDatasetDetailResult(
            state="unavailable", reason="provider dataset detail is invalid",
            failure="provider_error"),
        timeout=timeout,
    )


def bounded_ancestors(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                      placement_id: str, *, timeout: float = 1.0) -> ProviderAncestors:
    """Read one bounded ancestor chain without materializing a provider catalog."""

    def read() -> ProviderAncestors:
        raw = provider.ancestors(mount, placement_id)
        if isinstance(raw, ProviderAncestors):
            raw = raw.model_dump(mode="python")
        return ProviderAncestors.model_validate(raw)

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderAncestors(state="unavailable", reason=reason),
        unsupported=lambda: ProviderAncestors(
            state="unsupported", reason="ancestors is unsupported"),
        failed=lambda: ProviderAncestors(
            state="unavailable", reason="provider ancestor result is invalid"),
        timeout=timeout,
    )


def bounded_search(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                   query: str, *, limit: int, cursor: str | None = None,
                   timeout: float = 1.0) -> ProviderSearchPage:
    """Use only an explicitly declared lexical search capability under one source deadline."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    def read() -> ProviderSearchPage:
        raw_capabilities = provider.capabilities(mount)
        if isinstance(raw_capabilities, ProviderCapabilities):
            raw_capabilities = raw_capabilities.model_dump(mode="python")
        capabilities = ProviderCapabilities.model_validate(raw_capabilities)
        search = getattr(provider, "search", None)
        if not capabilities.search or not callable(search):
            raise NotImplementedError
        raw = search(mount, query, limit=limit, cursor=cursor)
        if isinstance(raw, ProviderSearchPage):
            raw = raw.model_dump(mode="python")
        page = ProviderSearchPage.model_validate(raw)
        if len(page.items) > limit:
            raise ValueError("provider search exceeds the requested limit")
        if cursor is not None and page.next_cursor == cursor:
            raise ValueError("provider search returned a non-advancing cursor")
        return page

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderSearchPage(state="unavailable", reason=reason),
        unsupported=lambda: ProviderSearchPage(
            state="unsupported", reason="search is unsupported", freshness="unknown"),
        failed=lambda: ProviderSearchPage(
            state="unavailable", reason="provider search result is invalid"),
        timeout=timeout,
    )
