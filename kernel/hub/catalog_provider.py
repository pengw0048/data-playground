"""Public contract for independent, read-only external catalog mounts.

This module deliberately does not compose mounts into Workspace browse or persist mount settings.
Those are consumer concerns.  A mount is passed to the provider on each read, which keeps local
placement/configuration separate from a provider package and prevents this SPI from implying writes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from hub.models import ColumnSchema, Wire

ProviderState = Literal["ready", "partial", "unavailable", "unsupported"]


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


class ProviderResourceResult(Wire):
    state: ProviderState = "ready"
    item: CatalogResource | None = None
    reason: str | None = Field(default=None, max_length=512)


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


def _unavailable(reason: str) -> ProviderPage:
    return ProviderPage(state="unavailable", reason=reason)


def bounded_list_children(provider: ReadOnlyCatalogProvider, mount: CatalogMount,
                          parent_id: str | None, *, limit: int, cursor: str | None = None,
                          timeout: float = 1.0) -> ProviderPage:
    """Return promptly when a synchronous provider is slow, cancelled, or unavailable.

    The worker is intentionally not awaited after a deadline: provider I/O must never block local
    browsing. A provider may return ``partial`` itself when it has a truthful bounded subset.
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if timeout <= 0:
        return _unavailable("deadline exceeded")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(provider.list_children, mount, parent_id, limit=limit, cursor=cursor)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return _unavailable("deadline exceeded")
    except concurrent.futures.CancelledError:
        return _unavailable("request cancelled")
    except asyncio.CancelledError:
        return _unavailable("request cancelled")
    except NotImplementedError:
        return ProviderPage(state="unsupported", reason="list_children is unsupported")
    except OSError:
        return _unavailable("provider unavailable")
    except Exception:  # noqa: BLE001 -- provider failures must not take down local browse
        return _unavailable("provider read failed")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
