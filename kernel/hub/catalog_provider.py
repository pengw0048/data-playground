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

from pydantic import Field, model_validator

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
    """Provider-owned opaque resource identity. Names and parent paths are presentation only."""

    id: str = Field(min_length=1, max_length=512)
    kind: Literal["container", "dataset"]
    name: str = Field(min_length=1, max_length=512)
    parent_id: str | None = Field(default=None, max_length=512)
    uri: str | None = Field(default=None, max_length=8192)
    columns: list[ColumnSchema] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def _dataset_shape(self) -> "CatalogResource":
        if self.kind == "dataset" and not self.uri:
            raise ValueError("a dataset resource requires a URI")
        if self.kind == "container" and (self.uri is not None or self.columns):
            raise ValueError("a container resource cannot carry dataset details")
        return self


class ProviderCapabilities(Wire):
    list_children: bool = True
    resolve: bool = True
    ancestors: bool = True
    dataset_detail: bool = True
    search: bool = False


class ProviderPage(Wire):
    state: ProviderState = "ready"
    items: list[CatalogResource] = Field(default_factory=list, max_length=500)
    next_cursor: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _continuation_shape(self) -> "ProviderPage":
        if self.state != "ready" and self.next_cursor is not None:
            raise ValueError("non-ready provider results cannot continue")
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


class ProviderAncestors(Wire):
    state: ProviderState = "ready"
    items: list[CatalogResource] = Field(default_factory=list, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


@runtime_checkable
class ReadOnlyCatalogProvider(Protocol):
    """Provider-side reads for one mount. Implementations must not mutate provider state."""

    def capabilities(self, mount: CatalogMount) -> ProviderCapabilities: ...
    def list_children(self, mount: CatalogMount, parent_id: str | None, *, limit: int,
                      cursor: str | None = None) -> ProviderPage: ...
    def resolve(self, mount: CatalogMount, resource_id: str) -> ProviderResourceResult: ...
    def ancestors(self, mount: CatalogMount, resource_id: str) -> ProviderAncestors: ...
    def dataset_detail(self, mount: CatalogMount, resource_id: str) -> ProviderResourceResult: ...


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
        return unavailable("provider read failed")


def bounded_list_children(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                          parent_id: str | None, *, limit: int, cursor: str | None = None,
                          timeout: float = 1.0) -> ProviderPage:
    """Return promptly when a synchronous provider is slow, cancelled, or unavailable.

    The worker is intentionally not awaited after a deadline: provider I/O must never block local
    browsing. A provider may return ``partial`` itself when it has a truthful bounded subset.
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    return _bounded_provider_read(
        lambda: provider.list_children(mount, parent_id, limit=limit, cursor=cursor),
        unavailable=lambda reason: ProviderPage(state="unavailable", reason=reason),
        unsupported=lambda: ProviderPage(
            state="unsupported", reason="list_children is unsupported"),
        timeout=timeout,
    )


def bounded_resolve(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                    resource_id: str, *, timeout: float = 1.0) -> ProviderResourceResult:
    """Resolve one provider identity without letting synchronous provider I/O block Workspace."""
    return _bounded_provider_read(
        lambda: provider.resolve(mount, resource_id),
        unavailable=lambda reason: ProviderResourceResult(
            state="unavailable", reason=reason, failure="offline"),
        unsupported=lambda: ProviderResourceResult(
            state="unsupported", reason="resolve is unsupported", failure="provider_error"),
        timeout=timeout,
    )


def bounded_ancestors(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                      resource_id: str, *, timeout: float = 1.0) -> ProviderAncestors:
    """Read one bounded ancestor chain without materializing a provider catalog."""
    return _bounded_provider_read(
        lambda: provider.ancestors(mount, resource_id),
        unavailable=lambda reason: ProviderAncestors(state="unavailable", reason=reason),
        unsupported=lambda: ProviderAncestors(
            state="unsupported", reason="ancestors is unsupported"),
        timeout=timeout,
    )


def bounded_search(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                   query: str, *, limit: int, cursor: str | None = None,
                   timeout: float = 1.0) -> ProviderSearchPage:
    """Use only an explicitly declared lexical search capability under one source deadline."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    def read() -> ProviderSearchPage:
        capabilities = provider.capabilities(mount)
        search = getattr(provider, "search", None)
        if not capabilities.search or not callable(search):
            raise NotImplementedError
        page = search(mount, query, limit=limit, cursor=cursor)
        return ProviderSearchPage.model_validate(page)

    return _bounded_provider_read(
        read,
        unavailable=lambda reason: ProviderSearchPage(state="unavailable", reason=reason),
        unsupported=lambda: ProviderSearchPage(
            state="unsupported", reason="search is unsupported", freshness="unknown"),
        timeout=timeout,
    )
