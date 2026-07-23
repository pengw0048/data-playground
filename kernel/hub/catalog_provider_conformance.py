"""Installed-wheel conformance check for :mod:`hub.catalog_provider`."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from importlib.metadata import entry_points

from hub.catalog_provider import (
    CatalogMount, ReadOnlyCatalogProvider, bounded_ancestors, bounded_capabilities,
    bounded_dataset_detail, bounded_list_children, bounded_resolve, bounded_search,
)


def _failure(stage: str, message: str) -> int:
    print(f"{stage}: {message}", file=sys.stderr)
    return 1


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m hub.catalog_provider_conformance",
        description="Verify an installed read-only catalog-provider wheel.",
    )
    parser.add_argument("provider", help="installed dataplay.catalog_providers entry-point name")
    parser.add_argument("--mount-id", required=True, help="local opaque mount identity for this check")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE",
                        help="provider configuration (repeatable)")
    return parser.parse_args(argv)


def _config(values: list[str]) -> dict[str, str] | None:
    config: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or key in config:
            return None
        config[key] = item
    return config


def _provider(name: str):
    entry = next((item for item in entry_points(group="dataplay.catalog_providers") if item.name == name), None)
    if entry is None:
        return None
    with contextlib.redirect_stdout(io.StringIO()):
        factory = entry.load()
        return factory()


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    config = _config(args.config)
    if config is None:
        return _failure("activation", "mount configuration must use unique KEY=VALUE pairs")
    try:
        provider = _provider(args.provider)
    except Exception:  # noqa: BLE001 -- do not reveal provider configuration or load errors
        return _failure("activation", "entry point did not activate")
    if provider is None or not isinstance(provider, ReadOnlyCatalogProvider):
        return _failure("activation", "entry point did not provide a read-only catalog provider")
    mount = CatalogMount(id=args.mount_id, provider=args.provider, config=config)
    try:
        capability_result = bounded_capabilities(provider, mount)
        if capability_result.state != "ready" or capability_result.item is None:
            return _failure("capability", "provider capability discovery was unavailable")
        capabilities = capability_result.item
        if not all((capabilities.list_children, capabilities.resolve, capabilities.ancestors,
                    capabilities.dataset_detail)):
            return _failure("capability", "provider omitted a required read capability")
        first = bounded_list_children(provider, mount, None, limit=1)
        if first.state != "ready" or len(first.items) != 1 or first.next_cursor is None:
            return _failure("capability", "provider did not return a bounded root page")
        second = bounded_list_children(provider, mount, None, limit=1, cursor=first.next_cursor)
        if (second.state != "ready" or len(second.items) != 1
                or second.items[0].placement_id == first.items[0].placement_id):
            return _failure("capability", "provider pagination was not stable")
        if first.items[0].name != second.items[0].name:
            return _failure("capability", "provider did not preserve duplicate display names")
        if capabilities.search:
            searched = bounded_search(provider, mount, first.items[0].name, limit=1)
            if searched.state != "ready" or len(searched.items) != 1:
                return _failure("capability", "provider search was not bounded and stable")
        resolved = bounded_resolve(provider, mount, first.items[0].placement_id)
        if (resolved.state != "ready" or resolved.item is None
                or resolved.item.placement_id != first.items[0].placement_id):
            return _failure("capability", "provider could not resolve its opaque placement ID")
        child = bounded_list_children(provider, mount, first.items[0].placement_id, limit=1)
        if child.state != "ready" or len(child.items) != 1 or child.items[0].kind != "dataset":
            return _failure("capability", "provider did not expose the conformance dataset child")
        first_dataset = child.items[0]
        assert first_dataset.dataset_id is not None
        detail = bounded_dataset_detail(provider, mount, first_dataset.dataset_id)
        if (detail.state != "ready" or detail.item is None or
                detail.item.dataset_id != first_dataset.dataset_id or not detail.item.columns
                or detail.item.uri != first_dataset.uri
                or detail.item.columns != first_dataset.columns):
            return _failure("capability", "provider could not return dataset detail and schema")
        second_child = bounded_list_children(provider, mount, second.items[0].placement_id, limit=1)
        if (second_child.state != "ready" or len(second_child.items) != 1
                or second_child.items[0].kind != "dataset"
                or second_child.items[0].dataset_id != first_dataset.dataset_id
                or second_child.items[0].placement_id == first_dataset.placement_id
                or second_child.items[0].uri != first_dataset.uri
                or second_child.items[0].columns != first_dataset.columns):
            return _failure("capability", "provider did not preserve canonical dataset identity")
        second_detail = bounded_dataset_detail(provider, mount, second_child.items[0].dataset_id or "")
        if second_detail.state != "ready" or second_detail.item != detail.item:
            return _failure("capability", "provider returned conflicting canonical dataset facts")
        ancestors = bounded_ancestors(provider, mount, first_dataset.placement_id)
        if (ancestors.state != "ready" or not ancestors.items or
                ancestors.items[-1].placement_id != first.items[0].placement_id):
            return _failure("capability", "provider could not return placement-specific ancestors")
        second_ancestors = bounded_ancestors(provider, mount, second_child.items[0].placement_id)
        if (second_ancestors.state != "ready" or not second_ancestors.items
                or second_ancestors.items[-1].placement_id != second.items[0].placement_id):
            return _failure("capability", "provider did not preserve moved presentation paths")
        restarted = _provider(args.provider)
        repeated = bounded_list_children(restarted, mount, None, limit=1)
        if repeated.state != "ready" or repeated.items != first.items:
            return _failure("capability", "provider identities changed after restart")
    except Exception:  # noqa: BLE001 -- provider exceptions are configuration-bearing
        return _failure("capability", "read-only provider check failed")
    print("catalog provider conformance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
