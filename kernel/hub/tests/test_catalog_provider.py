"""Contract and installed-wheel coverage for read-only catalog providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from hub import catalog_provider_conformance
from hub.catalog_provider import (
    _PROVIDER_READ_CONCURRENCY, CatalogDatasetDetail, CatalogMount, CatalogResource,
    ProviderAncestors, ProviderCapabilities, ProviderCapabilitiesResult,
    ProviderDatasetDetailResult, ProviderLineageResult, ProviderPage,
    ProviderResourceResult, ProviderSearchPage,
    bounded_ancestors, bounded_capabilities, bounded_dataset_detail, bounded_list_children,
    bounded_lineage, bounded_resolve, bounded_search,
)


def test_mount_secret_references_resolve_at_the_provider_boundary_without_serializing_material(
        monkeypatch, tmp_path):
    """Mount SecretRefs resolve only in the provider call, never in Workspace's public projection."""
    from hub import workspace_providers
    from hub.execution_manifest import assert_secret_free

    env_secret = "mount-env-secret"
    file_secret = "mount-file-secret"
    secret_file = tmp_path / "mount-token"
    secret_file.write_text(file_secret + "\n", encoding="utf-8")
    monkeypatch.setenv("DP_MOUNT_TEST_TOKEN", env_secret)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": "secret-mount", "provider": "fixture", "config": {
            "apiKey": "env:DP_MOUNT_TEST_TOKEN", "password": f"file:{secret_file}",
            "endpoint": "https://catalog.example.test",
        },
    }]))

    mounts, invalid = workspace_providers._configured_mounts()
    assert invalid is False and len(mounts) == 1
    mount = mounts[0].mount
    assert mount.config == {
        "apiKey": env_secret, "password": file_secret,
        "endpoint": "https://catalog.example.test",
    }

    class Provider:
        def list_children(self, received, _parent, *, limit, cursor=None):
            assert received == mount
            return ProviderPage()

    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: Provider())
    reads = workspace_providers._prefetch_provider_pages(
        [workspace_providers._Source("provider", mounts[0])], 0, limit=1, source_cursor=None)
    assert reads[0] == ProviderPage()

    public = workspace_providers._source_status(
        workspace_providers._Source("provider", mounts[0]), "complete")
    cursor = workspace_providers._cursor_encode(
        "local-mounted", "workspace-local-root",
        workspace_providers._mount_fingerprint(mounts, False), 0, None, [])
    serialized = json.dumps({"api": public, "cursor": cursor})
    assert env_secret not in serialized and file_secret not in serialized
    monkeypatch.setenv("DP_MOUNT_TEST_TOKEN", "rotated-mount-env-secret")
    rotated, rotated_invalid = workspace_providers._configured_mounts()
    assert rotated_invalid is False
    assert workspace_providers._mount_fingerprint(rotated, False) == (
        workspace_providers._mount_fingerprint(mounts, False))
    # Source admissions use this secret-free manifest boundary; mount config is not part of it.
    assert_secret_free({"providerMountId": mount.id, "providerSourceBindingId": "a" * 32})


def test_mount_sensitive_inline_config_is_rejected_without_echoing_material(monkeypatch):
    from hub import workspace_providers

    inline = "inline-mount-secret"
    with pytest.raises(workspace_providers.MountConfigError, match="sensitive field 'apiKey'") as exc:
        workspace_providers._resolve_mount_config({"apiKey": inline})
    assert inline not in str(exc.value)

    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": "bad-secret-mount", "provider": "fixture", "config": {"apiKey": inline},
    }]))
    mounts, invalid = workspace_providers._configured_mounts()
    assert mounts == [] and invalid is True
    assert inline not in workspace_providers._configured_source_error()


def _write_catalog(root: Path, resources: list[dict]) -> None:
    root.mkdir()
    (root / "catalog.json").write_text(json.dumps({"resources": resources}))


def _resources(uri: str) -> list[dict]:
    return [
        {"placementId": "container-a", "kind": "container", "name": "shared"},
        {"placementId": "container-b", "kind": "container", "name": "shared"},
        {"placementId": "dataset-a", "kind": "dataset", "datasetId": "dataset-a",
         "name": "shared", "uri": uri, "columns": [{"name": "id", "type": "int64"}]},
        {"placementId": "nested-dataset", "parentPlacementId": "container-a",
         "kind": "dataset", "datasetId": "dataset-a", "name": "nested", "uri": uri,
         "columns": [{"name": "id", "type": "int64"}]},
        {"placementId": "dataset-a-under-b", "parentPlacementId": "container-b",
         "kind": "dataset", "datasetId": "dataset-a", "name": "moved shared", "uri": uri,
         "columns": [{"name": "id", "type": "int64"}]},
    ]


def _provider_root_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_file_provider_distinguishes_placements_from_canonical_dataset_identity(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repo / "examples" / "plugins"))
    from dp_file_catalog_provider import provider

    root = tmp_path / "catalog"
    _write_catalog(root, _resources("reference.csv"))
    catalog = provider()
    mount = CatalogMount(id="mount", provider="dp-file-catalog", config={"root": str(root)})

    roots = catalog.list_children(mount, None, limit=2)
    assert [item.placement_id for item in roots.items] == ["container-a", "container-b"]
    assert [item.name for item in roots.items] == ["shared", "shared"]
    first = catalog.list_children(mount, "container-a", limit=1).items[0]
    second = catalog.list_children(mount, "container-b", limit=1).items[0]
    assert first.placement_id != second.placement_id
    assert first.parent_placement_id == "container-a"
    assert second.parent_placement_id == "container-b"
    assert first.dataset_id == second.dataset_id == "dataset-a"

    first_detail = catalog.dataset_detail(mount, first.dataset_id or "")
    second_detail = catalog.dataset_detail(mount, second.dataset_id or "")
    assert first_detail.item is not None and second_detail.item is not None
    assert first_detail.item == second_detail.item
    assert first_detail.item.uri.startswith("dp-file-catalog-mutable://")
    assert [item.placement_id for item in catalog.ancestors(mount, first.placement_id).items] == [
        "container-a"
    ]
    assert [item.placement_id for item in catalog.ancestors(mount, second.placement_id).items] == [
        "container-b"
    ]

    searched = catalog.search(mount, "shared", limit=10)
    assert {(item.placement_id, item.parent_placement_id) for item in searched.items} == {
        ("container-a", None), ("container-b", None),
        ("dataset-a", None), ("dataset-a-under-b", "container-b"),
    }

    (root / "catalog.json").write_text(json.dumps({"resources": [
        {
            "placementId": "container-a", "parentPlacementId": "container-b",
            "kind": "container", "name": "a",
        },
        {
            "placementId": "container-b", "parentPlacementId": "container-a",
            "kind": "container", "name": "b",
        },
    ]}))
    cyclic = catalog.ancestors(mount, "container-a")
    assert cyclic.state == "partial" and cyclic.reason == "ancestor cycle detected"
    assert [item.placement_id for item in cyclic.items] == ["container-b"]


def test_occurrence_contract_rejects_legacy_ids_and_conflicting_facts():
    with pytest.raises(ValueError, match="placementId"):
        CatalogResource.model_validate({
            "id": "legacy", "kind": "container", "name": "legacy",
        })
    with pytest.raises(ValueError, match="canonical dataset ID and URI"):
        CatalogResource(placement_id="dataset", kind="dataset", name="dataset", uri="file:///data")
    with pytest.raises(ValueError, match="cannot carry dataset details"):
        CatalogResource(placement_id="container", kind="container", name="container",
                        dataset_id="not-allowed")

    first = CatalogResource(
        placement_id="first", parent_placement_id="parent-a", kind="dataset", name="data",
        dataset_id="dataset", uri="file:///one", columns=[],
    )
    conflict = first.model_copy(update={"placement_id": "second", "uri": "file:///two"})
    with pytest.raises(ValueError, match="conflicting canonical facts"):
        ProviderPage(items=[first, conflict])
    with pytest.raises(ValueError, match="placement IDs must be unique"):
        ProviderPage(items=[first, first])


def test_occurrence_contract_represents_bounded_item_availability():
    unavailable = CatalogResource(
        placement_id="cold-dataset",
        dataset_id="canonical-cold",
        kind="dataset",
        name="Cold dataset",
        availability="unavailable",
        availability_reason="Metadata is still indexing",
    )
    unsupported = CatalogResource(
        placement_id="unsupported-folder",
        kind="container",
        name="Unsupported folder",
        availability="unsupported",
        availability_reason="This resource type cannot be browsed",
    )
    page = ProviderPage(items=[unavailable, unsupported], next_cursor="next")
    assert page.state == "ready"
    assert page.next_cursor == "next"
    assert unavailable.uri is None and unavailable.columns == []
    assert unavailable.model_dump(by_alias=True)["availabilityReason"] == (
        "Metadata is still indexing")

    class RootProvider:
        def list_children(self, _mount, parent_placement_id, *, limit, cursor=None):
            assert parent_placement_id is None and limit == 1 and cursor is None
            return ProviderPage(items=[unavailable])

    listed = bounded_list_children(
        RootProvider(), CatalogMount(id="root-items", provider="fixture"), None, limit=1)
    assert listed.state == "ready" and listed.items == [unavailable]

    with pytest.raises(ValueError, match="requires an availability reason"):
        CatalogResource(
            placement_id="missing-reason", kind="container", name="Missing reason",
            availability="unavailable",
        )
    with pytest.raises(ValueError, match="cannot carry an availability reason"):
        CatalogResource(
            placement_id="available-reason", kind="container", name="Available reason",
            availability_reason="not allowed",
        )
    with pytest.raises(ValueError, match="cannot carry URI or columns"):
        CatalogResource(
            placement_id="bad-dataset",
            parent_placement_id="folder",
            dataset_id="canonical-bad",
            kind="dataset",
            name="Bad dataset",
            uri="file:///fabricated",
            availability="unsupported",
            availability_reason="No dataset adapter",
        )
    with pytest.raises(ValueError, match="cannot continue"):
        ProviderPage(state="partial", next_cursor="next")


def test_bounded_helpers_enforce_requested_provider_identity():
    occurrence = CatalogResource(
        placement_id="other-placement", kind="container", name="Other",
    )

    class MismatchedProvider:
        def list_children(self, *_args, **_kwargs):
            return ProviderPage(items=[occurrence])

        def resolve(self, *_args, **_kwargs):
            return ProviderResourceResult(item=occurrence)

        def ancestors(self, *_args, **_kwargs):
            return ProviderAncestors(items=[])

        def dataset_detail(self, *_args, **_kwargs):
            return ProviderDatasetDetailResult(item=CatalogDatasetDetail(
                dataset_id="other-dataset", uri="file:///data", columns=[]))

    mount = CatalogMount(id="mount", provider="fixture")
    listed = bounded_list_children(MismatchedProvider(), mount, "requested-parent", limit=1)
    assert listed.state == "unavailable" and listed.reason == "provider list result is invalid"
    resolved = bounded_resolve(MismatchedProvider(), mount, "requested-placement")
    assert resolved.state == "unavailable" and resolved.failure == "provider_error"
    detailed = bounded_dataset_detail(MismatchedProvider(), mount, "requested-dataset")
    assert detailed.state == "unavailable" and detailed.failure == "provider_error"


def test_provider_lineage_is_bounded_and_hides_physical_uris(monkeypatch):
    from fastapi.testclient import TestClient

    from hub import metadb, workspace_providers
    from hub.main import app
    from hub.models import LineageEdge, LineageNode, LineageResult

    suffix = uuid.uuid4().hex
    mount_id = f"lineage-mount-{suffix}"
    source_binding_id = uuid.uuid4().hex
    provider_dataset_id = f"dataset-{suffix}"
    physical_root = f"s3://secret-bucket/{suffix}/raw_video_v2.lance"
    physical_parent = f"s3://secret-bucket/{suffix}/raw_video_v1.lance"

    class Provider:
        def capabilities(self, _mount):
            return ProviderCapabilities(lineage=True)

        def list_children(self, *_args, **_kwargs):
            return ProviderPage()

        def resolve(self, *_args, **_kwargs):
            return ProviderResourceResult(state="unavailable", failure="not_found")

        def ancestors(self, *_args, **_kwargs):
            return ProviderAncestors()

        def dataset_detail(self, *_args, **_kwargs):
            return ProviderDatasetDetailResult(state="unavailable", failure="not_found")

        def lineage(self, _mount, dataset_id, *, depth, max_nodes):
            assert dataset_id == provider_dataset_id
            assert depth == 2 and max_nodes == 10
            return LineageResult(
                root_uri=physical_root,
                nodes=[
                    LineageNode(id=physical_parent, name="raw_video_v1", uri=physical_parent),
                    LineageNode(id=physical_root, name="raw_video_v2", uri=physical_root),
                ],
                edges=[LineageEdge(
                    parent=physical_parent, child=physical_root, fact_count=2)],
            )

    provider = Provider()
    bounded = bounded_lineage(
        provider, CatalogMount(id=mount_id, provider="fixture"), provider_dataset_id,
        depth=2, max_nodes=10,
    )
    assert bounded == ProviderLineageResult(item=provider.lineage(
        CatalogMount(id=mount_id, provider="fixture"), provider_dataset_id,
        depth=2, max_nodes=10,
    ))

    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id,
        "provider": "fixture",
        "config": {},
    }]))
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    with metadb.session() as session:
        session.add(metadb.WorkspaceProviderDataset(
            mount_id=mount_id,
            provider_dataset_id=provider_dataset_id,
            provider="fixture",
            source_binding_id=source_binding_id,
            uri="fixture://hidden/provider-binding",
            columns_doc="[]",
            state="current",
        ))
    source_uri = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    graph = workspace_providers.provider_dataset_lineage(
        source_uri, depth=2, max_nodes=10)
    serialized = graph.model_dump_json()
    assert graph.root_uri == source_uri
    assert graph.nodes[1].name == "raw_video_v2"
    assert graph.edges[0].child == source_uri
    assert "secret-bucket" not in serialized and suffix not in graph.nodes[0].uri

    with TestClient(app) as client:
        response = client.get(
            "/api/catalog/lineage",
            params={"uri": source_uri, "depth": 2, "maxNodes": 10},
        )
    assert response.status_code == 200, response.text
    routed = response.json()
    assert routed["rootUri"] == source_uri
    assert {node["name"] for node in routed["nodes"]} >= {
        "raw_video_v1", "raw_video_v2",
    }
    assert "secret-bucket" not in response.text


def test_bounded_pages_reject_overlimit_and_nonadvancing_cursors():
    first = CatalogResource(placement_id="first", kind="container", name="First")
    second = CatalogResource(placement_id="second", kind="container", name="Second")

    class InvalidProvider:
        def list_children(self, *_args, **_kwargs):
            return ProviderPage(items=[first, second])

        def capabilities(self, _mount):
            return type("Capabilities", (), {"search": True})()

        def search(self, _mount, _query, *, limit, cursor=None):
            return ProviderSearchPage(items=[first], next_cursor=cursor)

    mount = CatalogMount(id="mount", provider="fixture")
    listed = bounded_list_children(InvalidProvider(), mount, None, limit=1)
    assert listed.state == "unavailable" and listed.reason == "provider list result is invalid"
    searched = bounded_search(InvalidProvider(), mount, "query", limit=1, cursor="same")
    assert searched.state == "unavailable" and searched.reason == "provider search result is invalid"


class _SlowProvider:
    def list_children(self, *_args, **_kwargs):
        time.sleep(0.2)
        return ProviderPage()


class _CancelledProvider:
    def list_children(self, *_args, **_kwargs):
        raise asyncio.CancelledError()


class _SlowCapabilitiesProvider:
    def capabilities(self, _mount):
        time.sleep(0.2)
        return ProviderCapabilities()


def test_bounded_listing_caps_background_work_and_normalizes_failures():
    mount = CatalogMount(id="local", provider="test")
    started = time.monotonic()
    timeouts = [
        bounded_list_children(_SlowProvider(), mount, None, limit=1, timeout=0.005)
        for _ in range(_PROVIDER_READ_CONCURRENCY)
    ]
    saturated = bounded_list_children(_SlowProvider(), mount, None, limit=1, timeout=0.005)
    assert time.monotonic() - started < 0.2
    assert all(item.state == "unavailable" and item.reason == "deadline exceeded" for item in timeouts)
    assert saturated.state == "unavailable" and saturated.reason == "provider busy"
    provider_threads = [
        thread for thread in threading.enumerate() if thread.name.startswith("dp-catalog-provider")
    ]
    assert len(provider_threads) <= _PROVIDER_READ_CONCURRENCY
    time.sleep(0.25)
    cancelled = bounded_list_children(_CancelledProvider(), mount, None, limit=1)
    assert cancelled.state == "unavailable" and cancelled.reason == "request cancelled"


def test_bounded_capabilities_returns_an_explicit_unavailable_result_at_its_deadline():
    started = time.monotonic()
    result = bounded_capabilities(
        _SlowCapabilitiesProvider(), CatalogMount(id="local", provider="test"), timeout=0.005)
    assert time.monotonic() - started < 0.2
    assert result == ProviderCapabilitiesResult(
        state="unavailable", reason="deadline exceeded")
    time.sleep(0.25)


def test_bounded_search_retains_placement_identity():
    occurrence = CatalogResource(
        placement_id="dataset-under-parent", parent_placement_id="parent", kind="dataset",
        dataset_id="dataset", name="Dataset", uri="file:///data", columns=[],
    )

    class SearchProvider:
        def capabilities(self, _mount):
            return ProviderCapabilities(search=True)

        def search(self, _mount, _query, *, limit, cursor=None):
            assert limit == 1 and cursor is None
            return ProviderSearchPage(items=[occurrence])

    result = bounded_search(SearchProvider(), CatalogMount(id="mount", provider="fixture"),
                            "dataset", limit=1)
    assert result.items == [occurrence]


def test_bounded_search_can_complete_with_an_explicit_deadline_or_stay_sanitized():
    class GateProvider:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def capabilities(self, _mount):
            return ProviderCapabilities(search=True)

        def search(self, _mount, _query, *, limit, cursor=None):
            del limit, cursor
            self.entered.set()
            assert self.release.wait(timeout=1), "test did not release provider search"
            return ProviderSearchPage()

    mount = CatalogMount(id="local", provider="test")
    timed_out_provider = GateProvider()
    timed_out = bounded_search(timed_out_provider, mount, "query", limit=1, timeout=0.001)
    assert timed_out_provider.entered.wait(timeout=1)
    assert timed_out.state == "unavailable" and timed_out.reason == "deadline exceeded"
    timed_out_provider.release.set()

    interactive_provider = GateProvider()
    result: list[ProviderSearchPage] = []
    worker = threading.Thread(target=lambda: result.append(
        bounded_search(interactive_provider, mount, "query", limit=1, timeout=0.1)))
    worker.start()
    assert interactive_provider.entered.wait(timeout=1)
    interactive_provider.release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert result[0].state == "ready"


def test_resource_failures_are_explicitly_classified():
    with pytest.raises(ValueError, match="must classify"):
        ProviderResourceResult(state="unavailable", reason="ambiguous failure")
    assert ProviderResourceResult(
        state="unavailable", reason="access revoked", failure="permission_lost",
    ).failure == "permission_lost"


def test_malformed_and_constructed_provider_results_are_revalidated():
    class MalformedProvider:
        def dataset_detail(self, *_args, **_kwargs):
            return {"state": "ready", "item": {
                "datasetId": "dataset", "uri": "fixture://data",
                "columns": [{"name": f"column-{index}", "type": "int"}
                            for index in range(2049)],
            }}

    malformed = bounded_dataset_detail(
        MalformedProvider(), CatalogMount(id="mount", provider="fixture"), "dataset")
    assert malformed.state == "unavailable"
    assert malformed.failure == "provider_error"
    assert malformed.reason == "provider dataset detail is invalid"

    invalid_occurrence = CatalogResource.model_construct(
        placement_id="dataset", dataset_id="dataset", kind="dataset", name="x" * 513,
        parent_placement_id=None, uri="fixture://data", columns=[])
    invalid_detail = CatalogDatasetDetail.model_construct(
        dataset_id="dataset", uri="x" * 8193, columns=[])

    class ConstructedProvider:
        def capabilities(self, _mount):
            return ProviderCapabilities(search=True)

        def list_children(self, *_args, **_kwargs):
            return ProviderPage.model_construct(
                state="ready", items=[invalid_occurrence], next_cursor=None, reason=None)

        def resolve(self, *_args, **_kwargs):
            return ProviderResourceResult.model_construct(
                state="ready", item=invalid_occurrence, reason=None, failure=None)

        def ancestors(self, *_args, **_kwargs):
            return ProviderAncestors.model_construct(
                state="ready", items=[invalid_occurrence], reason=None)

        def dataset_detail(self, *_args, **_kwargs):
            return ProviderDatasetDetailResult.model_construct(
                state="ready", item=invalid_detail, reason=None, failure=None)

        def search(self, *_args, **_kwargs):
            return ProviderSearchPage.model_construct(
                state="ready", items=[invalid_occurrence], next_cursor=None, reason=None,
                freshness="current")

    provider = ConstructedProvider()
    mount = CatalogMount(id="mount", provider="fixture")
    assert bounded_list_children(provider, mount, None, limit=10).state == "unavailable"
    assert bounded_resolve(provider, mount, "dataset").failure == "provider_error"
    assert bounded_ancestors(provider, mount, "dataset").state == "unavailable"
    assert bounded_dataset_detail(provider, mount, "dataset").failure == "provider_error"
    searched = bounded_search(provider, mount, "dataset", limit=10)
    assert searched.state == "unavailable"
    assert searched.reason == "provider search result is invalid"


class _ConformanceFixtureProvider:
    def __init__(
        self, *, second_uri: str = "fixture://dataset",
        second_columns: list[dict[str, str]] | None = None,
        detail_uri: str = "fixture://dataset",
        detail_columns: list[dict[str, str]] | None = None,
    ):
        columns = [{"name": "id", "type": "int64"}]
        self.roots = [
            CatalogResource(placement_id="container-a", kind="container", name="shared"),
            CatalogResource(placement_id="container-b", kind="container", name="shared"),
        ]
        self.children = {
            "container-a": CatalogResource(
                placement_id="dataset-under-a", parent_placement_id="container-a",
                dataset_id="dataset", kind="dataset", name="dataset",
                uri="fixture://dataset", columns=columns,
            ),
            "container-b": CatalogResource(
                placement_id="dataset-under-b", parent_placement_id="container-b",
                dataset_id="dataset", kind="dataset", name="dataset",
                uri=second_uri, columns=second_columns or columns,
            ),
        }
        self.detail = CatalogDatasetDetail(
            dataset_id="dataset", uri=detail_uri, columns=detail_columns or columns)

    def capabilities(self, _mount):
        return ProviderCapabilities()

    def list_children(self, _mount, parent_placement_id, *, limit, cursor=None):
        if parent_placement_id is None:
            start = int(cursor or 0)
            items = self.roots[start:start + limit]
            next_cursor = str(start + len(items)) if start + len(items) < len(self.roots) else None
            return ProviderPage(items=items, next_cursor=next_cursor)
        child = self.children.get(parent_placement_id)
        return ProviderPage(items=[child] if child is not None and limit else [])

    def resolve(self, _mount, placement_id):
        item = next(
            (item for item in [*self.roots, *self.children.values()]
             if item.placement_id == placement_id),
            None,
        )
        return ProviderResourceResult(item=item) if item is not None else ProviderResourceResult(
            state="unavailable", reason="not found", failure="not_found")

    def ancestors(self, _mount, placement_id):
        child = next(
            (item for item in self.children.values() if item.placement_id == placement_id), None)
        if child is None or child.parent_placement_id is None:
            return ProviderAncestors()
        parent = next(
            item for item in self.roots if item.placement_id == child.parent_placement_id)
        return ProviderAncestors(items=[parent])

    def dataset_detail(self, _mount, _dataset_id):
        return ProviderDatasetDetailResult(item=self.detail)


def test_conformance_routes_capability_discovery_through_the_bounded_boundary(
        monkeypatch, capsys):
    provider = _ConformanceFixtureProvider()
    calls: list[tuple[object, CatalogMount]] = []
    monkeypatch.setattr(catalog_provider_conformance, "_provider", lambda _name: provider)
    monkeypatch.setattr(
        catalog_provider_conformance, "bounded_capabilities",
        lambda item, mount: (
            calls.append((item, mount))
            or ProviderCapabilitiesResult(state="unavailable", reason="deadline exceeded")
        ),
    )

    result = catalog_provider_conformance.main(["fixture", "--mount-id", "mount"])

    assert result == 1
    assert calls == [(provider, CatalogMount(id="mount", provider="fixture"))]
    assert capsys.readouterr().err.strip() == (
        "capability: provider capability discovery was unavailable")


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (
            _ConformanceFixtureProvider(second_uri="fixture://other"),
            "capability: provider did not preserve canonical dataset identity",
        ),
        (
            _ConformanceFixtureProvider(
                second_columns=[{"name": "other", "type": "string"}]),
            "capability: provider did not preserve canonical dataset identity",
        ),
        (
            _ConformanceFixtureProvider(detail_uri="fixture://other"),
            "capability: provider could not return dataset detail and schema",
        ),
        (
            _ConformanceFixtureProvider(
                detail_columns=[{"name": "other", "type": "string"}]),
            "capability: provider could not return dataset detail and schema",
        ),
    ],
)
def test_conformance_rejects_conflicting_occurrence_and_detail_canonical_facts(
        monkeypatch, capsys, provider, message):
    monkeypatch.setattr(catalog_provider_conformance, "_provider", lambda _name: provider)

    result = catalog_provider_conformance.main(["fixture", "--mount-id", "mount"])

    assert result == 1
    assert capsys.readouterr().err.strip() == message


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=False, text=True, capture_output=True)


def test_file_provider_wheel_passes_public_conformance(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    plugin = repo / "examples" / "plugins" / "dp_file_catalog_provider"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist, plugin_dist = tmp_path / "core-dist", tmp_path / "plugin-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    assert _run([uv, "build", "--wheel", "--out-dir", str(plugin_dist)], cwd=plugin).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")
    plugin_wheel, = plugin_dist.glob("dp_file_catalog_provider-*.whl")
    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    install = _run([
        uv, "pip", "install", "--python", str(python),
        str(core_wheel), str(plugin_wheel), "httpx2>=2.5",
    ], cwd=tmp_path)
    assert install.returncode == 0, install.stderr

    root = tmp_path / "catalog"
    resources = _resources("reference.csv")
    for resource in resources:
        if resource.get("datasetId") == "dataset-a":
            resource["revisionId"] = "provider-dataset-a-v1"
    _write_catalog(root, resources)
    (root / "reference.csv").write_text("id\n1\n2\n")
    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    checked = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", "dp-file-catalog",
         "--mount-id", "reference-mount", "--config", f"root={root}"], cwd=tmp_path, env=clean_env)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "catalog provider conformance passed"

    second_root = tmp_path / "catalog-two"
    _write_catalog(second_root, _resources("reference-two.csv"))
    (second_root / "reference-two.csv").write_text("id\n3\n4\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mixed_env = {
        **clean_env,
        "DP_WORKSPACE": str(workspace),
        "DP_DATA_DIR": str(workspace / "data"),
        "DP_DATABASE_URL": f"sqlite:///{workspace / 'dataplay.db'}",
        "DP_EXECUTION": "local-out-of-core",
        "DP_CATALOG_MOUNTS": json.dumps([
            {"id": "wheel-a", "provider": "dp-file-catalog", "config": {"root": str(root)}},
            {"id": "wheel-b", "provider": "dp-file-catalog", "config": {"root": str(second_root)}},
        ]),
    }
    acceptance_state = tmp_path / "provider-acceptance.json"
    provider_root_before_actions = _provider_root_snapshot(root)
    journey = _run([str(python), "-c", r'''
import json
import os
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hub import metadb
from hub.main import app

with TestClient(app) as client:
    root = client.get(
        f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}", params={"limit": 100})
    assert root.status_code == 200, root.text
    page = root.json()
    assert all(source["completeness"] == "complete" for source in page["sources"])
    duplicates = [item for item in page["items"]
                  if item.get("resourceId") == "dataset-a" and item.get("name") == "shared"]
    assert {item["mountId"] for item in duplicates} == {"wheel-a", "wheel-b"}
    assert len({item["id"] for item in duplicates}) == 2
    remote = next(item for item in page["items"]
                  if item.get("mountId") == "wheel-a" and item.get("resourceId") == "container-a")
    assert remote["providerMutation"] is False
    assert remote["localPlacement"]["recoveryState"] == "ready"
    nested = client.get(
        f"/api/workspace/containers/{remote['id'].removeprefix('container:')}", params={"limit": 100})
    assert nested.status_code == 200, nested.text
    nested_resource = next(
        item for item in nested.json()["items"]
        if item.get("resourceId") == "nested-dataset")
    resource = next(item for item in page["items"]
                    if item.get("mountId") == "wheel-a" and item.get("resourceId") == "dataset-a")
    root_resolution = client.get(f"/api/workspace/resources/{resource['id']}")
    nested_resolution = client.get(f"/api/workspace/resources/{nested_resource['id']}")
    assert root_resolution.status_code == nested_resolution.status_code == 200
    source_binding = root_resolution.json()["canonicalSourceBinding"]
    assert source_binding == nested_resolution.json()["canonicalSourceBinding"]
    assert source_binding is not None
    assert source_binding["mountId"] == "wheel-a"
    assert len(source_binding["sourceBindingId"]) == 32
    serialized_binding = json.dumps(source_binding, sort_keys=True)
    for forbidden in (
        "dataset-a", "nested-dataset", "container-a",
        "reference.csv", str(Path(os.environ["DP_WORKSPACE"]).parent),
    ):
        assert forbidden not in serialized_binding
    root_context_response = client.get(
        f"/api/workspace/resources/{resource['id']}/canonical-dataset")
    nested_context_response = client.get(
        f"/api/workspace/resources/{nested_resource['id']}/canonical-dataset")
    assert root_context_response.status_code == nested_context_response.status_code == 200
    canonical_context = root_context_response.json()
    assert canonical_context == nested_context_response.json()
    assert canonical_context["mountId"] == source_binding["mountId"]
    assert canonical_context["sourceBindingId"] == source_binding["sourceBindingId"]
    assert canonical_context["providerDatasetId"] == "dataset-a"
    assert canonical_context["datasetIdentity"].startswith("workspace-provider:")
    assert canonical_context["sourceUri"].startswith("workspace-provider://")
    assert canonical_context["readMode"] == "exact"
    assert canonical_context["revisionId"] == "provider-dataset-a-v1"
    assert isinstance(canonical_context["committedAt"], str)
    assert [(column["name"], column["type"])
            for column in canonical_context["columns"]] == [("id", "int64")]
    serialized_context = json.dumps(canonical_context, sort_keys=True)
    for forbidden in ("reference.csv", str(Path(os.environ["DP_WORKSPACE"]).parent)):
        assert forbidden not in serialized_context
    replacement = client.get(f"/api/workspace/resources/{nested_resource['id']}/source")
    assert replacement.status_code == 200, replacement.text
    replacement_config = replacement.json()["config"]
    assert replacement.json()["name"] == "nested"
    assert replacement_config["uri"].startswith("workspace-provider://")
    assert replacement_config["providerSourceBindingId"] == source_binding["sourceBindingId"]
    assert replacement_config["datasetRef"]["datasetId"] == canonical_context["datasetIdentity"]
    assert replacement_config["datasetRef"]["revisionId"] == canonical_context["revisionId"]
    assert replacement_config["providerReadMode"] == "exact"
    assert "reference.csv" not in json.dumps(replacement_config)
    body = {
        "requestId": "00000000-0000-4000-8000-000000000791",
        "containerId": remote["localPlacement"]["containerId"],
        "expectedContainerVersion": remote["localPlacement"]["containerVersion"],
        "name": "Installed provider identity journey", "providerDatasetRefs": [resource["id"]],
    }
    created = client.post("/api/workspace/canvases", json=body)
    assert created.status_code == 200, created.text
    created_doc = created.json()
    replay = client.post("/api/workspace/canvases", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == created_doc
    canvas_id = created_doc["id"]
    assert created_doc["resource"]["parentId"] == remote["id"]
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.Canvas).where(
            metadb.Canvas.owner_id == metadb.DEFAULT_USER_ID,
            metadb.Canvas.name == body["name"],
        )) == 1
        assert session.scalar(select(func.count()).select_from(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "canvas",
            metadb.WorkspacePlacement.target_id == canvas_id,
        )) == 1
        assert session.scalar(select(func.count()).select_from(
            metadb.WorkspaceCanvasCreateReplay).where(
                metadb.WorkspaceCanvasCreateReplay.owner_id == metadb.DEFAULT_USER_ID,
                metadb.WorkspaceCanvasCreateReplay.request_id == body["requestId"],
            )) == 1
    graph = client.get(f"/api/canvas/{canvas_id}").json()
    source = graph["nodes"][0]
    config = source["data"]["config"]
    assert config["uri"].startswith("workspace-provider://")
    from hub import workspace_providers
    assert workspace_providers.provider_dataset_identity(config["uri"]) == (
        canonical_context["datasetIdentity"])
    assert config["datasetRef"]["revisionId"] == "provider-dataset-a-v1"
    assert config["datasetRef"]["revisionId"] == canonical_context["revisionId"]
    assert config["datasetRef"]["lastKnown"]["committedAt"] == canonical_context["committedAt"]
    assert os.environ["DP_CATALOG_MOUNTS"] not in json.dumps(graph)
    assert str(Path(os.environ["DP_WORKSPACE"]).parent / "catalog") not in json.dumps(graph)

    destination = metadb.workspace_create_container(
        metadb.LOCAL_WORKSPACE_ROOT_ID, "Installed wheel stale marker")
    current_destination = metadb.workspace_update_container(
        destination["id"], expected_version=destination["version"],
        name="Installed wheel move destination")
    stale_move = client.put(
        f"/api/workspace/placements/{created_doc['resource']['placementId']}/canvas", json={
            "containerId": destination["id"],
            "expectedContainerVersion": destination["version"],
            "expectedVersion": created_doc["resource"]["version"],
        })
    assert stale_move.status_code == 409, stale_move.text
    unchanged = client.get(f"/api/workspace/resources/canvas:{canvas_id}")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["resource"]["parentId"] == remote["id"]
    moved = client.put(
        f"/api/workspace/placements/{created_doc['resource']['placementId']}/canvas", json={
            "containerId": destination["id"],
            "expectedContainerVersion": current_destination["version"],
            "expectedVersion": unchanged.json()["resource"]["version"],
        })
    assert moved.status_code == 200, moved.text
    assert moved.json()["resource"]["parentId"] == f"container:{destination['id']}"
    placement_destination = metadb.workspace_create_container(
        metadb.LOCAL_WORKSPACE_ROOT_ID, "Installed wheel placement CAS target")
    stale_placement = client.put(
        f"/api/workspace/placements/{created_doc['resource']['placementId']}/canvas", json={
            "containerId": placement_destination["id"],
            "expectedContainerVersion": placement_destination["version"],
            "expectedVersion": created_doc["resource"]["version"],
        })
    assert stale_placement.status_code == 409, stale_placement.text
    after_stale_placement = client.get(f"/api/workspace/resources/canvas:{canvas_id}")
    assert after_stale_placement.status_code == 200, after_stale_placement.text
    assert after_stale_placement.json()["resource"]["parentId"] == f"container:{destination['id']}"
    assert after_stale_placement.json()["resource"]["version"] == moved.json()["resource"]["version"]
    with metadb.session() as session:
        placement = session.get(metadb.WorkspacePlacement, created_doc["resource"]["placementId"])
        assert placement is not None
        assert placement.container_id == destination["id"]
        assert placement.version == moved.json()["resource"]["version"]
    undo = client.put(
        f"/api/workspace/placements/{created_doc['resource']['placementId']}/canvas", json={
            "containerId": remote["localPlacement"]["containerId"],
            "expectedContainerVersion": remote["localPlacement"]["containerVersion"],
            "expectedVersion": moved.json()["resource"]["version"],
        })
    assert undo.status_code == 200, undo.text
    assert undo.json()["resource"]["parentId"] == remote["id"]

    from hub.deps import get_deps
    physical = get_deps().resolve_adapter(config["uri"])
    exact_adapter = physical.adapter
    preview_calls = []
    original_preview = exact_adapter.preview_revision
    original_open = exact_adapter.open_revision
    def record_preview(uri, revision_id, *, limit):
        preview_calls.append((uri, revision_id, limit))
        return original_preview(uri, revision_id, limit=limit)
    def full_open_is_a_bug(*_args, **_kwargs):
        raise AssertionError("exact preview called the full-run revision path")
    exact_adapter.preview_revision = record_preview
    exact_adapter.open_revision = full_open_is_a_bug
    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
    })
    exact_adapter.open_revision = original_open
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [{"id": 1}, {"id": 2}]
    assert preview_calls == [
        (physical.physical_uri, "provider-dataset-a-v1", 2000),
        (physical.physical_uri, "provider-dataset-a-v1", 2000),
        (physical.physical_uri, "provider-dataset-a-v1", 2000),
    ]
    inputs = preview.json()["inputManifest"]
    assert len(inputs) == 1
    assert inputs[0]["revision_id"] == "provider-dataset-a-v1"
    assert inputs[0]["provider"] == "dp-file-catalog-exact"
    started = client.post("/api/run", json={
        "graph": graph, "targetNodeId": source["id"], "confirmed": True,
        "submissionId": str(uuid.uuid4()), "inputManifest": inputs,
    })
    assert started.status_code == 200, started.text
    run_id = started.json()["runId"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/run/{run_id}")
        assert run.status_code == 200, run.text
        if run.json()["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert run.json()["status"] == "done", run.json()
    assert run.json()["totalRows"] == 2
    history = None
    history_deadline = time.monotonic() + 5
    while time.monotonic() < history_deadline:
        history_response = client.get(f"/api/canvas/{canvas_id}/runs")
        assert history_response.status_code == 200, history_response.text
        history = next((item for item in history_response.json() if item.get("runId") == run_id), None)
        if history is not None:
            break
        time.sleep(0.05)
    assert history is not None, "terminal run was not projected into Canvas history"
    assert history["status"] == "done" and history["rows"] == 2
    assert history["inputManifest"] == inputs
    assert history["executionManifestAvailability"] == "available"
    manifest_response = client.get(f"/api/canvas/{canvas_id}/runs/{history['id']}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()
    assert manifest["availability"] == "available"
    assert manifest["document"]["admittedInputs"] == [{
        "nodeId": inputs[0]["node_id"], "datasetId": inputs[0]["dataset_id"],
        "revisionId": inputs[0]["revision_id"], "provider": inputs[0]["provider"],
    }]
    assert "dp-file-catalog://" not in json.dumps(graph)
    assert "dp-file-catalog://" not in json.dumps(history)
    assert "dp-file-catalog://" not in json.dumps(manifest)

    mutable = next(item for item in page["items"]
                   if item.get("mountId") == "wheel-b" and item.get("resourceId") == "dataset-a")
    mutable_replacement = client.get(
        f"/api/workspace/resources/{mutable['id']}/source")
    assert mutable_replacement.status_code == 409, mutable_replacement.text
    mutable_replacement_error = mutable_replacement.json()
    assert mutable_replacement_error["detail"] == (
        "This dataset cannot be pinned to a version, so it cannot replace a runnable Source."
    )
    assert mutable_replacement_error["code"] == "local_run_input_binding_failed"
    assert mutable_replacement_error["retryable"] is False
    mutable_canvas = client.post("/api/workspace/canvases", json={
        "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
        "expectedContainerVersion": page["container"]["version"],
        "name": "Installed provider mutable journey", "providerDatasetRefs": [mutable["id"]],
    })
    assert mutable_canvas.status_code == 200, mutable_canvas.text
    mutable_graph = client.get(f"/api/canvas/{mutable_canvas.json()['id']}").json()
    mutable_source = mutable_graph["nodes"][0]
    assert mutable_source["data"]["config"]["providerReadMode"] == "mutable"
    assert "datasetRef" not in mutable_source["data"]["config"]
    first = client.post("/api/run/preview", json={
        "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 10,
    })
    assert first.status_code == 200, first.text
    assert first.json()["rows"] == [{"id": 3}, {"id": 4}]
    Path(os.environ["MUTABLE_FILE"]).write_text("id\n30\n40\n")
    second = client.post("/api/run/preview", json={
        "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 10,
    })
    assert second.status_code == 200, second.text
    assert second.json()["rows"] == [{"id": 30}, {"id": 40}]
    rejected = client.post("/api/run", json={
        "graph": mutable_graph, "targetNodeId": mutable_source["id"], "confirmed": True,
    })
    assert rejected.status_code == 409, rejected.text
    assert "mutable-only" in rejected.json()["detail"]
    assert os.environ["DP_CATALOG_MOUNTS"] not in json.dumps(graph)
    Path(os.environ["ACCEPTANCE_STATE"]).write_text(json.dumps({
        "canvas_id": canvas_id, "run_id": run_id, "source_id": source["id"],
        "input_manifest": inputs, "remote_resource_id": remote["id"],
        "remote_binding_id": remote["bindingId"], "anchor": remote["localPlacement"],
        "canonical_resource_id": resource["id"],
        "source_binding_mount_id": source_binding["mountId"],
        "source_binding_id": source_binding["sourceBindingId"],
    }))
print("installed provider placement journey passed")
'''], cwd=tmp_path, env={
        **mixed_env, "ACCEPTANCE_STATE": str(acceptance_state),
        "MUTABLE_FILE": str(second_root / "reference-two.csv"),
    })
    assert journey.returncode == 0, journey.stderr
    assert journey.stdout.strip().endswith("installed provider placement journey passed")
    assert "dp-file-catalog://" not in journey.stdout + journey.stderr
    assert _provider_root_snapshot(root) == provider_root_before_actions

    restarted = _run([str(python), "-c", r'''
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app

state = json.loads(Path(os.environ["ACCEPTANCE_STATE"]).read_text())
with TestClient(app) as client:
    graph_response = client.get(f"/api/canvas/{state['canvas_id']}")
    assert graph_response.status_code == 200, graph_response.text
    graph = graph_response.json()
    source = next(node for node in graph["nodes"] if node["id"] == state["source_id"])
    assert source["data"]["config"]["datasetRef"]["revisionId"] == "provider-dataset-a-v1"
    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
        "inputManifest": state["input_manifest"],
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [{"id": 1}, {"id": 2}]
    history_response = client.get(f"/api/canvas/{state['canvas_id']}/runs")
    assert history_response.status_code == 200, history_response.text
    history = next(item for item in history_response.json() if item.get("runId") == state["run_id"])
    assert history["status"] == "done"
    assert history["inputManifest"] == state["input_manifest"]
    manifest = client.get(f"/api/canvas/{state['canvas_id']}/runs/{history['id']}/manifest")
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["availability"] == "available"
    reopened = client.get(f"/api/workspace/resources/canvas:{state['canvas_id']}")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["resource"]["parentId"] == state["remote_resource_id"]
    assert reopened.json()["ancestors"][-1]["id"] == state["remote_resource_id"]
    remote = client.get(f"/api/workspace/resources/{state['remote_resource_id']}")
    assert remote.status_code == 200, remote.text
    assert remote.json()["resource"]["bindingId"] == state["remote_binding_id"]
    assert remote.json()["resource"]["localPlacement"] == state["anchor"]
    anchor = metadb.workspace_provider_overlay_anchor(state["remote_binding_id"])
    assert anchor is not None
    assert {key: anchor[key] for key in ("containerId", "containerVersion", "recoveryState")} == {
        key: state["anchor"][key] for key in ("containerId", "containerVersion", "recoveryState")
    }
    canonical = client.get(f"/api/workspace/resources/{state['canonical_resource_id']}")
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["canonicalSourceBinding"] == {
        "mountId": state["source_binding_mount_id"],
        "sourceBindingId": state["source_binding_id"],
    }
print("installed provider restart evidence passed")
'''], cwd=tmp_path, env={**mixed_env, "ACCEPTANCE_STATE": str(acceptance_state)})
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.strip().endswith("installed provider restart evidence passed")
    assert _provider_root_snapshot(root) == provider_root_before_actions

    relink = _run([str(python), "-c", r'''
import json
import os
from pathlib import Path
import hashlib

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app

state = json.loads(Path(os.environ["ACCEPTANCE_STATE"]).read_text())
catalog = Path(os.environ["PROVIDER_CATALOG"])
def provider_snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }
with TestClient(app) as client:
    catalog.write_text(json.dumps({"resources": []}))
    empty_baseline = provider_snapshot(catalog.parent)
    detached = client.get(f"/api/workspace/resources/{state['remote_resource_id']}")
    assert detached.status_code == 200, detached.text
    assert detached.json()["resource"]["referenceState"] == "detached"
    assert detached.json()["resource"]["localPlacement"] == state["anchor"]
    assert provider_snapshot(catalog.parent) == empty_baseline
    catalog.write_text(json.dumps({"resources": [{
        "placementId": "container-a", "kind": "container", "name": "shared",
    }]}))
    recreated_baseline = provider_snapshot(catalog.parent)
    still = client.get(f"/api/workspace/resources/{state['remote_resource_id']}")
    assert still.json()["resource"]["referenceState"] == "detached"
    assert still.json()["resource"]["bindingId"] == state["remote_binding_id"]
    relinked = client.post(f"/api/workspace/resources/{state['remote_resource_id']}/relink", json={
        "mountId": "wheel-a", "resourceId": "container-a",
    })
    assert relinked.status_code == 200, relinked.text
    replacement = relinked.json()["resource"]
    assert replacement["bindingId"] != state["remote_binding_id"]
    assert replacement["localPlacement"]["containerId"] != state["anchor"]["containerId"]
    replacement_binding = metadb.workspace_provider_binding(replacement["bindingId"])
    assert replacement_binding is not None
    assert replacement_binding["relinkedFromId"] == state["remote_binding_id"]
    assert provider_snapshot(catalog.parent) == recreated_baseline
print("installed provider explicit relink passed")
'''], cwd=tmp_path, env={
        **mixed_env, "ACCEPTANCE_STATE": str(acceptance_state),
        "PROVIDER_CATALOG": str(root / "catalog.json"),
    })
    assert relink.returncode == 0, relink.stderr
    assert relink.stdout.strip().endswith("installed provider explicit relink passed")

    unique_names = _resources("reference.csv")
    unique_names[1]["name"] = "different"
    (root / "catalog.json").write_text(json.dumps({"resources": unique_names}))
    invalid_fixture = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", "dp-file-catalog",
         "--mount-id", "reference-mount", "--config", f"root={root}"],
        cwd=tmp_path, env=clean_env,
    )
    assert invalid_fixture.returncode == 1
    assert invalid_fixture.stderr.strip() == (
        "capability: provider did not preserve duplicate display names")

    secret = "config-should-not-leak"
    rejected = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", secret,
         "--mount-id", "reference-mount", "--config", f"root={root / secret}"],
        cwd=tmp_path, env=clean_env,
    )
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == (
        "activation: entry point did not provide a read-only catalog provider")
    assert secret not in rejected.stdout + rejected.stderr
