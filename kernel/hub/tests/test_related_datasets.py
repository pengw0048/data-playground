"""Bounded related-dataset ranking and atomic Join-with confirmation."""

from __future__ import annotations

import uuid
import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app
from hub.models import (
    CatalogPage,
    CatalogTable,
    ColumnSchema,
    Relationship,
)
from hub.related_datasets import related_datasets, source_identity_from_config

client = TestClient(app)


class _UnavailableAdapter:
    @staticmethod
    def scan(_uri, columns=None):
        raise ConnectionError("provider unavailable")


class _Catalog:
    def __init__(self, tables: list[CatalogTable], relationships: list[Relationship] | None = None):
        self.tables = tables
        self._relationships = relationships or []
        self.requested_limits: list[int] = []

    def get_table(self, token: str) -> CatalogTable:
        for table in self.tables:
            if token in {
                table.uri, table.id, table.name, table.registration_id,
            }:
                return table
        raise KeyError(token)

    def list_page(self, query):
        self.requested_limits.append(query.limit)
        items = [
            table for table in self.tables
            if (not query.q or query.q.casefold() in table.name.casefold())
            and (
                not query.folder
                or table.folder == query.folder
                or table.folder.startswith(f"{query.folder}/")
            )
        ]
        window = items[query.offset:query.offset + query.limit]
        return CatalogPage(
            items=window, total=len(items), offset=query.offset, limit=query.limit,
            has_more=query.offset + len(window) < len(items),
        )

    def incident_relationships(self, uri, *, limit=64):
        items = [relationship for relationship in self._relationships
                 if uri in (relationship.left_uri, relationship.right_uri)]
        return items[:limit], len(items) > limit


def _table(name: str, columns: list[ColumnSchema], *, folder: str = "") -> CatalogTable:
    return CatalogTable(
        id=f"tbl_{name}",
        registration_id=f"reg_{name}",
        name=name,
        uri=f"provider://{name}",
        version="v1",
        folder=folder,
        columns=columns,
    )


def test_declared_then_typed_ranking_excludes_conflicting_reference_and_stays_bounded():
    source = _table("events", [ColumnSchema.model_validate({
        "name": "owner_id",
        "type": "int",
        "rowReference": {
            "target": {"kind": "canonical", "datasetId": "reg_owners"},
            "keyFields": ["id"],
            "provenance": "provider",
        },
    })])
    owners = _table("owners", [ColumnSchema(name="id", type="int")])
    impostor = _table("impostor", [ColumnSchema(name="id", type="int")])
    declared = _table("declared", [ColumnSchema(name="event_id", type="int")])
    extras = [
        _table(f"extra-{index:03}", [ColumnSchema(name="value", type="string")])
        for index in range(80)
    ]
    catalog = _Catalog(
        [source, declared, owners, impostor, *extras],
        [Relationship(
            left_uri=source.uri,
            left_columns=["owner_id"],
            right_uri=declared.uri,
            right_columns=["event_id"],
            cardinality="unknown",
        )],
    )

    page = related_datasets(
        catalog, lambda _uri: _UnavailableAdapter(), None, source.registration_id,
        limit=10,
    )

    assert catalog.requested_limits == [50]
    assert [item.name for item in page.candidates[:1]] == ["owners"]
    assert page.candidates[0].evidence == "typed_reference"
    exclusion = next(item for item in page.excluded if item.name == "declared")
    assert "contradicts" in exclusion.reason
    assert page.truncated is True and page.refinement_required is True


def _parquet(path, values: list[int]) -> None:
    pq.write_table(pa.table({"id": values}), path)


def _register(path, name: str) -> dict:
    response = client.post("/api/catalog/register", json={"uri": str(path), "name": name})
    assert response.status_code == 200, response.text
    return response.json()


def test_confirm_is_one_canvas_cas_and_stale_canvas_or_dataset_changes_nothing(tmp_path):
    token = uuid.uuid4().hex
    left_path = tmp_path / f"left-{token}.parquet"
    right_path = tmp_path / f"right-{token}.parquet"
    _parquet(left_path, [1, 2])
    _parquet(right_path, [1, 2])
    left = _register(left_path, f"left-{token}")
    right = _register(right_path, f"right-{token}")
    canvas_id = f"related-{token}"
    original = {
        "id": canvas_id,
        "name": "Join with related",
        "version": 1,
        "nodes": [{
            "id": "left-source",
            "type": "source",
            "position": {"x": 10, "y": 20},
            "data": {
                "title": left["name"],
                "status": "draft",
                    "config": {"uri": left["uri"], "tableId": left["id"],
                               "registrationId": left["registrationId"]},
                "history": [],
            },
        }],
        "edges": [],
    }
    created = client.post("/api/canvas", json=original)
    assert created.status_code == 200 and created.json()["created"] is True
    page_response = client.post("/api/catalog/related-datasets", json={
        "source": {"kind": "local", "registrationId": left["registrationId"],
                   "revisionMode": "current"}, "q": right["name"], "limit": 10,
    })
    assert page_response.status_code == 200, page_response.text
    page = page_response.json()
    candidate = next(item for item in page["candidates"] if item["name"] == right["name"])
    body = {
        "expectedCanvasVersion": 1,
        "sourceNodeId": "left-source",
        "sourceIdentity": page["source"],
        "candidate": candidate,
        "q": right["name"],
        "how": "left",
    }

    confirmed = client.post(f"/api/canvas/{canvas_id}/join-with-related", json=body)
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["version"] == 2
    assert len(result["canvas"]["nodes"]) == 3
    assert len(result["canvas"]["edges"]) == 2
    join = next(node for node in result["canvas"]["nodes"] if node["type"] == "join")
    assert join["data"]["config"]["how"] == "left"
    assert join["data"]["config"]["on"] == "id"
    persisted = client.get(f"/api/canvas/{canvas_id}").json()
    assert persisted == result["canvas"]

    stale_canvas = client.post(f"/api/canvas/{canvas_id}/join-with-related", json=body)
    assert stale_canvas.status_code == 409
    assert client.get(f"/api/canvas/{canvas_id}").json() == persisted

    # The same atomic edit can fill an existing Join that has one unfilled input.
    existing_join_id = f"related-existing-join-{token}"
    existing_join = {
        **original,
        "id": existing_join_id,
        "nodes": [
            *original["nodes"],
            {
                "id": "empty-join",
                "type": "join",
                "position": {"x": 300, "y": 20},
                "data": {
                    "title": "Join",
                    "status": "draft",
                    "config": {},
                    "history": [],
                },
            },
        ],
        "edges": [{
            "id": "left-to-empty-join",
            "source": "left-source",
            "target": "empty-join",
            "sourceHandle": "out",
            "targetHandle": "a",
        }],
    }
    assert client.post("/api/canvas", json=existing_join).status_code == 200
    filled = client.post(
        f"/api/canvas/{existing_join_id}/join-with-related",
        json={**body, "joinNodeId": "empty-join", "how": "right"},
    )
    assert filled.status_code == 200, filled.text
    filled_result = filled.json()
    assert filled_result["joinNodeId"] == "empty-join"
    assert len(filled_result["canvas"]["nodes"]) == 3
    assert len(filled_result["canvas"]["edges"]) == 2
    filled_join = next(
        node for node in filled_result["canvas"]["nodes"] if node["id"] == "empty-join"
    )
    assert filled_join["data"]["config"]["how"] == "right"

    # A current/latest review is deliberately re-evaluated at confirmation; it must not pretend to
    # be an exact revision conflict merely because current catalog metadata advanced.
    second_id = f"related-stale-dataset-{token}"
    second = {**original, "id": second_id}
    assert client.post("/api/canvas", json=second).status_code == 200
    _parquet(right_path, [1, 2, 3])
    changed = _register(right_path, right["name"])
    assert changed["version"] != right["version"]
    stale_dataset = client.post(
        f"/api/canvas/{second_id}/join-with-related",
        json={**body, "expectedCanvasVersion": 1},
    )
    assert stale_dataset.status_code == 200


def test_real_http_retained_revision_review_creates_an_exact_related_source(tmp_path, monkeypatch):
    """Exercise the picker/review/confirm HTTP path without replacing its routes."""
    from hub import related_datasets as related
    from hub.routers import catalog as catalog_router

    token = uuid.uuid4().hex
    left_path = tmp_path / f"exact-left-{token}.parquet"
    right_path = tmp_path / f"exact-right-{token}.parquet"
    _parquet(left_path, [1, 2])
    _parquet(right_path, [1, 2])
    left = _register(left_path, f"exact-left-{token}")
    right = _register(right_path, f"exact-right-{token}")

    class RetainedAdapter:
        retention_owner = "provider"

        def revision_history(self, uri, *, limit, cursor=None):
            assert uri == right["uri"] and limit == 20 and cursor is None
            return ([{"revision_id": "retained-v1", "committed_at": datetime.datetime(
                2026, 7, 24, tzinfo=datetime.timezone.utc)}], None)

        def revision_detail(self, uri, revision_id, *, preview_limit):
            assert uri == right["uri"] and revision_id == "retained-v1"
            return {
                "revision_id": revision_id,
                "committed_at": datetime.datetime(2026, 7, 24, tzinfo=datetime.timezone.utc),
                "columns": [{"name": "id", "type": "int"}],
                "preview_table": pa.table({"id": [1]}),
                "parent_revision_id": None, "producer_operation": "append",
            }

        def resolve_revision(self, uri, *, as_of=None):
            return {"revision_id": "retained-v1", "committed_at": datetime.datetime(
                2026, 7, 24, tzinfo=datetime.timezone.utc)}

        def open_revision(self, uri, revision_id):
            raise AssertionError("the bounded revision detail path is required")

    adapter = RetainedAdapter()
    original_related = related.revision_adapter_for_uri
    original_catalog = catalog_router.revision_adapter_for_uri
    monkeypatch.setattr(related, "revision_adapter_for_uri",
                        lambda uri, resolve: adapter if uri == right["uri"] else original_related(uri, resolve))
    monkeypatch.setattr(catalog_router, "revision_adapter_for_uri",
                        lambda uri, resolve: adapter if uri == right["uri"] else original_catalog(uri, resolve))

    source_identity = {"kind": "local", "registrationId": left["registrationId"], "revisionMode": "current"}
    listed = client.post("/api/catalog/related-datasets", json={
        "source": source_identity, "q": right["name"], "limit": 10,
    })
    assert listed.status_code == 200, listed.text
    current = next(item for item in listed.json()["candidates"] if item["name"] == right["name"])
    history = client.post("/api/catalog/related-datasets/revisions", json={
        "identity": current["identity"], "limit": 20,
    })
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["revisionId"] == "retained-v1"
    review = client.post("/api/catalog/related-datasets/revision-review", json={
        "source": source_identity, "candidate": current, "revisionId": "retained-v1", "q": right["name"],
    })
    assert review.status_code == 200, review.text
    exact = review.json()
    assert exact["identity"]["revisionMode"] == "exact"
    assert exact["exactRef"] == {"kind": "exact", "datasetId": right["registrationId"],
                                  "revisionId": "retained-v1", "lastKnown": {"committedAt": "2026-07-24T00:00:00Z"}}

    canvas_id = f"related-exact-{token}"
    created = client.post("/api/canvas", json={
        "id": canvas_id, "name": "Exact related join", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"title": left["name"], "status": "draft", "history": [],
                            "config": {"uri": left["uri"], "tableId": left["id"],
                                       "registrationId": left["registrationId"]}}}], "edges": [],
    })
    assert created.status_code == 200
    confirmed = client.post(f"/api/canvas/{canvas_id}/join-with-related", json={
        "expectedCanvasVersion": 1, "sourceNodeId": "source", "sourceIdentity": source_identity,
        "candidate": exact, "q": right["name"], "how": "inner",
    })
    assert confirmed.status_code == 200, confirmed.text
    added = next(node for node in confirmed.json()["canvas"]["nodes"]
                 if node["type"] == "source" and node["id"] != "source")
    assert added["data"]["config"]["datasetRef"]["revisionId"] == "retained-v1"


def test_existing_join_with_selected_source_on_b_swaps_heterogeneous_condition(tmp_path):
    token = uuid.uuid4().hex
    left_path, right_path = tmp_path / f"left-{token}.parquet", tmp_path / f"right-{token}.parquet"
    pq.write_table(pa.table({"user_id": [1, 2]}), left_path)
    pq.write_table(pa.table({"id": [1, 2]}), right_path)
    left, right = _register(left_path, f"condition-left-{token}"), _register(right_path, f"condition-right-{token}")
    canvas_id = f"related-b-{token}"
    assert client.post("/api/canvas", json={
        "id": canvas_id, "name": "Join source on b", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"title": left["name"], "status": "draft", "history": [], "config": {
                 "uri": left["uri"], "tableId": left["id"], "registrationId": left["registrationId"]}}},
            {"id": "join", "type": "join", "position": {"x": 200, "y": 0},
             "data": {"title": "Join", "status": "draft", "history": [], "config": {}}},
        ], "edges": [{"id": "source-b", "source": "source", "target": "join",
                        "sourceHandle": "out", "targetHandle": "b"}],
    }).status_code == 200
    source_identity = {"kind": "local", "registrationId": left["registrationId"], "revisionMode": "current"}
    page = client.post("/api/catalog/related-datasets", json={
        "source": source_identity, "q": right["name"], "limit": 10,
    }).json()
    candidate = next(item for item in page["candidates"] if item["name"] == right["name"])
    result = client.post(f"/api/canvas/{canvas_id}/join-with-related", json={
        "expectedCanvasVersion": 1, "sourceNodeId": "source", "joinNodeId": "join",
        "sourceIdentity": source_identity, "candidate": candidate, "q": right["name"], "how": "inner",
    })
    assert result.status_code == 200, result.text
    join = next(node for node in result.json()["canvas"]["nodes"] if node["id"] == "join")
    assert join["data"]["config"]["condition"] == 'a."id" = b."user_id"'


def test_mcp_lists_read_only_related_dataset_tool():
    from hub.mcp import _tool_specs, Playground

    names = {
        spec["name"]
        for spec in _tool_specs(Playground(get_deps(), "default", "http://127.0.0.1:8471"))
    }
    assert "related_datasets" in names


def test_source_admission_and_typed_targets_never_fall_back_to_display_identity():
    table = _table("canonical", [ColumnSchema(name="id", type="int")])
    catalog = _Catalog([table])
    # Legacy/hand-authored Sources remain readable but deliberately do not gain a misleading Join
    # action just because a URI, display name or transient table id happens to resolve today.
    for config in (
        {"uri": table.uri}, {"tableId": table.id}, {"uri": table.uri, "tableId": table.id},
        {"providerMountId": "mount-only"},
        {"providerResourceRef": "placement:copied", "providerMountId": "mount-only"},
    ):
        try:
            source_identity_from_config(catalog, config)
        except ValueError:
            pass
        else:  # pragma: no cover - makes every prohibited fallback explicit
            raise AssertionError(f"unexpected stable binding from {config}")
    assert source_identity_from_config(catalog, {"registrationId": table.registration_id}).registration_id == table.registration_id
    local_exact = source_identity_from_config(catalog, {
        "registrationId": table.registration_id,
        "datasetRef": {"kind": "exact", "datasetId": table.registration_id, "revisionId": "retained-1"},
    })
    assert (local_exact.revision_mode, local_exact.revision_id) == ("exact", "retained-1")
    local_as_of = source_identity_from_config(catalog, {
        "registrationId": table.registration_id,
        "datasetRef": {"kind": "as_of", "asOf": "2026-07-24T00:00:00Z", "resolved": {
            "datasetId": table.registration_id, "revisionId": "retained-as-of",
            "selector": "as_of", "retentionOwner": "provider",
        }},
    })
    assert (local_as_of.revision_mode, local_as_of.revision_id) == ("exact", "retained-as-of")
    provider = source_identity_from_config(catalog, {
        "providerMountId": "mount-a", "providerSourceBindingId": "a" * 32,
    })
    assert (provider.kind, provider.mount_id, provider.source_binding_id) == ("provider", "mount-a", "a" * 32)
    provider_as_of = source_identity_from_config(catalog, {
        "providerMountId": "mount-a", "providerSourceBindingId": "a" * 32,
        "datasetRef": {"kind": "as_of", "resolved": {"revisionId": "provider-as-of"}},
    })
    assert (provider_as_of.revision_mode, provider_as_of.revision_id) == ("exact", "provider-as-of")
    assert source_identity_from_config(catalog, {
        "providerMountId": "mount-a", "providerSourceBindingId": "a" * 32,
        "datasetRef": {"parameterRef": "revision"},
    }).revision_mode == "current"

    with pytest.raises(ValueError):
        from hub.models import RelatedDatasetIdentity
        RelatedDatasetIdentity.model_validate({
            "kind": "local", "registrationId": table.registration_id,
            "revisionMode": "current", "uri": table.uri,
        })
    with pytest.raises(ValueError):
        from hub.models import RelatedDatasetCandidate
        RelatedDatasetCandidate.model_validate({
            "identity": {"kind": "local", "registrationId": table.registration_id,
                         "revisionMode": "current"},
            "name": "candidate", "reason": "test", "evidence": "schema_match",
            "evidenceStatus": "inferred", "leftColumns": ["id"], "rightColumns": ["id"],
            "unexpected": True,
        })

    class CollidingCatalog(_Catalog):
        def get_table(self, _token):
            # Simulates a buggy provider that returns a colliding display-name row for a missing
            # retained typed target. The service must still reject it by registration identity.
            return table

    source = _table("events", [ColumnSchema.model_validate({
        "name": "owner_id", "type": "int",
        "rowReference": {"target": {"kind": "canonical", "datasetId": "retained-gone"},
                         "keyFields": ["id"], "provenance": "provider"},
    })])
    page = related_datasets(CollidingCatalog([source, table]), lambda _uri: _UnavailableAdapter(),
                            None, source.registration_id)
    assert all(item.name != table.name for item in page.candidates)


def test_provider_source_uses_bounded_mount_scope_not_a_local_catalog_fallback(monkeypatch):
    from hub import related_datasets as related

    source_binding = "a" * 32
    candidate_binding = "b" * 32
    uri_for = lambda mount, binding: f"workspace-provider://{mount}/{binding}"
    row = lambda binding, name: {  # noqa: E731 - compact provider fixture
        "mountId": "mount-a", "providerDatasetId": name, "sourceBindingId": binding,
        "referenceState": "current", "uri": f"physical://{name}",
        "columns": [{"name": "id", "type": "int"}],
    }
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_for_source_binding",
                        lambda *, mount_id, source_binding_id: row(source_binding_id, "source")
                        if source_binding_id == source_binding else None)
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_page",
                        lambda *, mount_id, query, limit: ([row(candidate_binding, "provider-related")], False))
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_uri", uri_for)
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_identity",
                        lambda uri: f"workspace-provider:{uri.rsplit('/', 1)[-1]}")
    # There is intentionally no local catalog candidate. The provider candidate still flows through
    # the same bounded schema/key and unknown-cardinality policy.
    page = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                            related.RelatedDatasetIdentity(
                                kind="provider", mount_id="mount-a", source_binding_id=source_binding),
                            limit=10)
    assert [(item.name, item.identity.kind, item.identity.source_binding_id, item.cardinality)
            for item in page.candidates] == [("provider-related", "provider", candidate_binding, "unknown")]


def test_provider_typed_reference_is_ranked_before_its_inferred_match(monkeypatch):
    from hub import related_datasets as related

    source_binding, target_binding = "a" * 32, "b" * 32
    target_identity = f"workspace-provider:{related.workspace_providers._source_identity_token('mount-a', target_binding)}"
    row = lambda binding, name, columns: {  # noqa: E731 - compact provider fixture
        "mountId": "mount-a", "providerDatasetId": name, "sourceBindingId": binding,
        "referenceState": "current", "uri": f"physical://{name}", "columns": columns,
    }
    source_row = row(source_binding, "events", [{
        "name": "user_id", "type": "int", "rowReference": {
            "target": {"kind": "canonical", "datasetId": target_identity},
            "keyFields": ["id"], "provenance": "provider",
        },
    }])
    target_row = row(target_binding, "users", [{"name": "id", "type": "int"}])
    fillers = [row(f"{index + 10:032x}", f"filler-{index:03}", [
        {"name": "value", "type": "string"},
    ]) for index in range(50)]
    calls = []
    def lookup(*, mount_id, source_binding_id):
        calls.append((mount_id, source_binding_id))
        if (mount_id, source_binding_id) == ("mount-a", source_binding):
            return source_row
        if (mount_id, source_binding_id) == ("mount-a", target_binding):
            return target_row
        return None
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_for_source_binding", lookup)
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_page",
                        lambda *, mount_id, query, limit: (fillers if not query else [], not bool(query)))
    page = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                            related.RelatedDatasetIdentity(
                                kind="provider", mount_id="mount-a", source_binding_id=source_binding))
    assert [(item.name, item.evidence, item.identity.source_binding_id)
            for item in page.candidates] == [("users", "typed_reference", target_binding)]
    assert ("mount-a", target_binding) in calls
    matching = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                               related.RelatedDatasetIdentity(
                                   kind="provider", mount_id="mount-a", source_binding_id=source_binding),
                               q="users")
    assert [item.name for item in matching.candidates] == ["users"]
    excluded = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                               related.RelatedDatasetIdentity(
                                   kind="provider", mount_id="mount-a", source_binding_id=source_binding),
                               q="unrelated")
    assert excluded.candidates == []


@pytest.mark.parametrize("case", ["cross_mount", "invalid", "stale"])
def test_provider_typed_reference_never_rebinds_cross_mount_invalid_or_stale(monkeypatch, case):
    from hub import related_datasets as related

    source_binding, target_binding = "a" * 32, "b" * 32
    if case == "cross_mount":
        target = f"workspace-provider:{related.workspace_providers._source_identity_token('mount-b', target_binding)}"
    elif case == "invalid":
        target = "workspace-provider:not-a-canonical-token"
    else:
        target = f"workspace-provider:{related.workspace_providers._source_identity_token('mount-a', target_binding)}"
    source_row = {
        "mountId": "mount-a", "providerDatasetId": "source", "sourceBindingId": source_binding,
        "referenceState": "current", "uri": "physical://source", "columns": [{
            "name": "user_id", "type": "int", "rowReference": {
                "target": {"kind": "canonical", "datasetId": target},
                "keyFields": ["id"], "provenance": "provider",
            },
        }],
    }
    calls = []
    def lookup(*, mount_id, source_binding_id):
        calls.append((mount_id, source_binding_id))
        if (mount_id, source_binding_id) == ("mount-a", source_binding):
            return source_row
        if case == "stale" and (mount_id, source_binding_id) == ("mount-a", target_binding):
            return None
        return None
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_for_source_binding", lookup)
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_page",
                        lambda **_kwargs: ([], False))
    page = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                            related.RelatedDatasetIdentity(
                                kind="provider", mount_id="mount-a", source_binding_id=source_binding))
    assert page.candidates == []
    if case == "stale":
        assert ("mount-a", target_binding) in calls
    else:
        assert ("mount-a", target_binding) not in calls


def test_declared_and_typed_candidates_require_keys_in_the_selected_schema():
    source = _table("events", [ColumnSchema(name="user_id", type="int")])
    declared = _table("declared", [ColumnSchema(name="id", type="int")])
    typed = _table("typed", [ColumnSchema(name="id", type="string")])
    source = source.model_copy(update={"columns": [
        ColumnSchema.model_validate({"name": "user_id", "type": "int", "rowReference": {
            "target": {"kind": "canonical", "datasetId": typed.registration_id},
            "keyFields": ["id"], "provenance": "provider",
        }}),
    ]})
    page = related_datasets(_Catalog([source, declared, typed], [Relationship(
        left_uri=source.uri, left_columns=["missing"], right_uri=declared.uri,
        right_columns=["id"], cardinality="1:1",
    )]), lambda _uri: _UnavailableAdapter(), None, source.registration_id)
    assert all(item.name not in {"declared", "typed"} for item in page.candidates)


def test_managed_logical_typed_target_uses_logical_data_identity_not_registration(monkeypatch):
    from hub import related_datasets as related

    target = _table("managed-target", [ColumnSchema(name="id", type="int")])
    source = _table("managed-source", [ColumnSchema.model_validate({
        "name": "target_id", "type": "int", "rowReference": {
            "target": {"kind": "canonical", "datasetId": "logical-managed-target"},
            "keyFields": ["id"], "provenance": "provider",
        },
    })])
    bindings = {
        source.uri: {"dataset_id": "logical-managed-source", "uri": source.uri},
        target.uri: {"dataset_id": "logical-managed-target", "uri": target.uri},
    }
    monkeypatch.setattr(related.metadb, "catalog_revision_binding_for_uri", lambda uri: bindings.get(uri))
    monkeypatch.setattr(related.metadb, "catalog_revision_binding",
                        lambda dataset_id: bindings[target.uri] if dataset_id == "logical-managed-target" else None)
    page = related_datasets(_Catalog([source, target]), lambda _uri: _UnavailableAdapter(), None,
                            source.registration_id)
    assert [(item.name, item.evidence) for item in page.candidates] == [("managed-target", "typed_reference")]


def test_provider_folder_refinement_fails_closed_when_canonical_records_lack_folder(monkeypatch):
    from hub import related_datasets as related

    source_binding = "a" * 32
    calls = []
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_for_source_binding",
                        lambda *, mount_id, source_binding_id: {
                            "mountId": mount_id, "providerDatasetId": "source",
                            "sourceBindingId": source_binding_id, "referenceState": "current",
                            "uri": "physical://source", "columns": [{"name": "id", "type": "int"}],
                        })
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_page",
                        lambda **kwargs: (calls.append(kwargs), ([], False))[1])
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_uri",
                        lambda mount, binding: f"workspace-provider://{mount}/{binding}")
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_identity",
                        lambda uri: f"workspace-provider:{uri.rsplit('/', 1)[-1]}")
    page = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                            related.RelatedDatasetIdentity(
                                kind="provider", mount_id="mount-a", source_binding_id=source_binding),
                            folder="robotics")
    assert calls == [] and page.candidates == []
    assert page.refinement_required is True
    assert page.scope_note and "folder scope cannot be proven" in page.scope_note


def test_retained_revision_picker_uses_bounded_history_and_rechecks_exact_schema(monkeypatch):
    from hub import related_datasets as related
    from hub.models import RelatedDatasetIdentity

    source = _table("events", [ColumnSchema(name="user_id", type="int")])
    target = _table("users", [ColumnSchema(name="id", type="int")])

    class Revisions:
        retention_owner = "provider"

        def revision_history(self, uri, *, limit, cursor=None):
            assert uri == target.uri and limit == 20 and cursor is None
            return ([{"revision_id": "v2", "committed_at": datetime.datetime(2026, 7, 24,
                                                                                tzinfo=datetime.timezone.utc)}], None)

        def revision_detail(self, uri, revision_id, *, preview_limit):
            assert uri == target.uri and revision_id == "v2" and preview_limit == 1
            return {"revision_id": "v2", "committed_at": datetime.datetime(2026, 7, 24,
                                                                                tzinfo=datetime.timezone.utc),
                    "columns": [{"name": "id", "type": "int"}]}

        def resolve_revision(self, *_args, **_kwargs):
            raise AssertionError("not used by the picker")

        def open_revision(self, *_args, **_kwargs):
            raise AssertionError("not used by the picker")

    adapter = Revisions()
    monkeypatch.setattr(related, "revision_adapter_for_uri", lambda uri, _resolve: adapter if uri == target.uri else _UnavailableAdapter())
    monkeypatch.setattr(related.metadb, "catalog_revision_binding_for_uri",
                        lambda uri: {"dataset_id": "logical-users"} if uri == target.uri else None)
    catalog = _Catalog([source, target])
    page = related_datasets(catalog, lambda _uri: _UnavailableAdapter(), None, source.registration_id)
    candidate = next(item for item in page.candidates if item.name == "users")
    revisions = related.related_dataset_revisions(
        catalog, lambda _uri: _UnavailableAdapter(),
        RelatedDatasetIdentity(kind="local", registration_id=target.registration_id), limit=20)
    assert [(item.dataset_id, item.revision_id) for item in revisions.items] == [("logical-users", "v2")]
    exact = related.review_related_dataset_revision(
        catalog, lambda _uri: _UnavailableAdapter(), None, page.source, candidate, "v2")
    assert exact.identity.revision_mode == "exact" and exact.identity.revision_id == "v2"
    assert exact.exact_ref is not None and exact.exact_ref.dataset_id == "logical-users"
    assert exact.cardinality == "unknown"


def test_provider_row_reference_conflict_uses_canonical_provider_identity(monkeypatch):
    from hub import related_datasets as related

    source_binding = "a" * 32
    candidate_binding = "b" * 32
    uri_for = lambda mount, binding: f"workspace-provider://{mount}/{binding}"
    wrong_target = f"workspace-provider:{'c' * 32}"
    row = lambda binding, name, columns: {  # noqa: E731 - compact provider fixture
        "mountId": "mount-a", "providerDatasetId": name, "sourceBindingId": binding,
        "referenceState": "current", "uri": f"physical://{name}", "columns": columns,
    }
    source_row = row(source_binding, "source", [{
        "name": "id", "type": "int", "rowReference": {
            "target": {"kind": "canonical", "datasetId": wrong_target},
            "keyFields": ["id"], "provenance": "provider",
        },
    }])
    candidate_row = row(candidate_binding, "candidate", [{"name": "id", "type": "int"}])
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_for_source_binding",
                        lambda *, mount_id, source_binding_id: source_row
                        if source_binding_id == source_binding else None)
    monkeypatch.setattr(related.metadb, "workspace_provider_dataset_page",
                        lambda *, mount_id, query, limit: ([candidate_row], False))
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_uri", uri_for)
    monkeypatch.setattr(related.workspace_providers, "provider_dataset_identity",
                        lambda uri: f"workspace-provider:{uri.rsplit('/', 1)[-1]}")
    page = related_datasets(_Catalog([]), lambda _uri: _UnavailableAdapter(), None,
                            related.RelatedDatasetIdentity(
                                kind="provider", mount_id="mount-a", source_binding_id=source_binding))
    assert page.candidates == []
    assert page.excluded[0].identity.source_binding_id == candidate_binding
    assert "contradicts" in page.excluded[0].reason
