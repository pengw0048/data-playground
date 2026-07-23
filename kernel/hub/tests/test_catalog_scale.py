"""Catalog at scale: server-side browse / search / facets / folders / bounded lineage / semantic.

These exercise the discovery surface that has to hold thousands of tables — every assertion is about
pushdown (filter/sort/paginate/facet in the DB, bounded payloads), organization (folders/tags/owner),
and the semantic-search seam. Seeded entries use a distinct `mem://` uri prefix and are torn down, so
they can't leak into the rest of the suite.
"""

from __future__ import annotations


import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, or_, select

from hub import metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import CatalogQuery

client = TestClient(app)

_SCALE = "mem://scale/"
_SEM = "mem://sem/"


@pytest.fixture(autouse=True)
def _remove_scale_publication_tombstones():
    """Keep this module's permanent retry fences out of persistent test databases."""
    yield
    with metadb.session() as session:
        session.execute(delete(metadb.CatalogPublicationEvent).where(
            metadb.CatalogPublicationEvent.effect_type == "lineage",
            or_(
                metadb.CatalogPublicationEvent.uri.like(f"{_SCALE}%"),
                metadb.CatalogPublicationEvent.uri.like(f"{_SEM}%"),
            ),
        ))


def _doc(
        name, uri, *, folder="", tags=None, owner=None, description=None,
        rows=0, cols=None, version=None):
    """A catalog_bulk_seed entry: {uri, name, doc} where doc is the full CatalogTable-shaped dict."""
    doc = {
        "id": f"tbl_{name}", "name": name, "uri": uri, "folder": folder, "tags": tags or [],
        "owner": owner, "description": description, "rowCount": rows,
        "columns": [{"name": c, "type": "VARCHAR"} for c in (cols or [])],
    }
    if version is not None:
        doc["version"] = str(version)
    return {"uri": uri, "name": name, "doc": doc}


def _lineage(
        *, key=None, run_id=None, attempt_id=None, producer=None,
        producer_version=None, step_id=None, provenance="manual", mappings=None):
    return {
        "idempotency_key": key or f"catalog-scale-{uuid.uuid4().hex}",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "producer": producer,
        "producer_version": producer_version,
        "step_id": step_id,
        "provenance": provenance,
        "field_mappings": mappings or [],
    }


def _record_lineage(parent, child, *, lineage=None):
    destination = metadb.catalog_get(child)
    assert destination is not None
    return metadb.catalog_record_lineage(
        destination["uri"], destination.get("version"), [parent],
        lineage or _lineage())


@pytest.fixture
def scale_catalog():
    """Seed a few thousand synthetic tables across a folder/tag/owner space, then tear them down."""
    metadb.catalog_delete_prefix(_SCALE)
    n = 2000
    owners = ["alice", "bob", "carol", "dave"]
    entries = []
    for i in range(n):
        top = f"team{i % 5}"
        sub = f"ds{i % 20}"
        folder = f"{top}/{sub}"
        tags = ["gold" if i % 3 == 0 else "silver"]
        if i % 7 == 0:
            tags.append("pii")
        entries.append(_doc(
            f"scale_{i:05d}", f"{_SCALE}{i}", folder=folder, tags=tags,
            owner=owners[i % len(owners)], rows=i * 10,
            cols=["id", "value"] + (["email"] if i % 7 == 0 else []),
        ))
    inserted = metadb.catalog_bulk_seed(entries)
    assert inserted == n
    try:
        yield n
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_pagination_body_and_windowing(scale_catalog):
    n = scale_catalog
    r = client.get("/api/catalog/tables", params={"folder": "team0", "limit": 25, "offset": 0, "sort": "name"})
    assert r.status_code == 200
    page = r.json()
    items = page["items"]
    assert len(items) == 25, "the page is bounded by limit, not the catalog size"
    assert page == {
        "items": items,
        "total": n // 5,
        "offset": 0,
        "limit": 25,
        "hasMore": True,
    }
    assert "X-Total-Count" not in r.headers and "X-Has-More" not in r.headers
    # a deterministic second page has no overlap with the first (stable sort + offset)
    r2 = client.get("/api/catalog/tables", params={"folder": "team0", "limit": 25, "offset": 25, "sort": "name"})
    first = {t["uri"] for t in items}
    second = {t["uri"] for t in r2.json()["items"]}
    assert first.isdisjoint(second)


def test_five_thousand_dataset_discovery_stays_bounded():
    prefix = f"{_SCALE}five-thousand/"
    metadb.catalog_delete_prefix(prefix)
    entries = [
        _doc(f"five_thousand_{index:04d}", f"{prefix}{index}", folder=f"large/{index % 10}")
        for index in range(5_000)
    ]
    try:
        assert metadb.catalog_bulk_seed(entries) == 5_000
        page = client.get("/api/catalog/tables", params={
            "q": "five_thousand_", "limit": 50, "offset": 0, "sort": "name",
        })
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["total"] == 5_000
        assert len(body["items"]) == 50
        assert body["hasMore"] is True
    finally:
        metadb.catalog_delete_prefix(prefix)


def test_sort_by_rows_and_usage(scale_catalog):
    r = client.get("/api/catalog/tables", params={"folder": "team1", "sort": "rows", "order": "desc", "limit": 5})
    rows = [t["rowCount"] for t in r.json()["items"]]
    assert rows == sorted(rows, reverse=True) and rows[0] > rows[-1]


def test_folder_subtree_filter(scale_catalog):
    # a folder filter matches the folder AND its subtree
    total_top = client.get("/api/catalog/tables", params={"folder": "team2", "limit": 1}).json()["total"]
    total_sub = client.get("/api/catalog/tables", params={"folder": "team2/ds2", "limit": 1}).json()["total"]
    assert total_top > total_sub > 0


def test_tag_and_owner_and_column_filters(scale_catalog):
    # tags AND together; pii is a strict subset of the seeded set
    pii = client.get("/api/catalog/tables", params={"tags": "pii", "limit": 1}).json()["total"]
    pii_gold = client.get("/api/catalog/tables", params={"tags": "pii,gold", "limit": 1}).json()["total"]
    assert 0 < pii_gold <= pii
    # every pii table has the email column, so has-column agrees
    has_email = client.get("/api/catalog/tables", params={"hasColumns": "email", "limit": 1}).json()["total"]
    assert has_email == pii
    owned = client.get("/api/catalog/tables", params={"owner": "alice", "limit": 1}).json()["total"]
    assert owned > 0


def test_facets_reflect_filter(scale_catalog):
    f = client.get("/api/catalog/facets", params={"folder": "team3"}).json()
    tags = {v["value"]: v["count"] for v in f["tags"]}
    assert set(tags) <= {"gold", "silver", "pii"} and sum(tags.values()) > 0
    owners = {v["value"] for v in f["owners"]}
    assert owners <= {"alice", "bob", "carol", "dave"}
    # every folder facet under the team3 filter is within the team3 subtree
    assert all(v["value"].startswith("team3") for v in f["folders"])


def test_browse_tree(scale_catalog):
    root = client.get("/api/catalog/tree").json()
    names = {fo["name"] for fo in root["folders"]}
    assert {"team0", "team1", "team2", "team3", "team4"} <= names
    team0 = next(fo for fo in root["folders"] if fo["name"] == "team0")
    assert team0["tableCount"] == 2000 // 5  # subtree count
    level = client.get("/api/catalog/tree", params={"prefix": "team0"}).json()
    assert all(fo["path"].startswith("team0/") for fo in level["folders"])


def test_search_lexical(scale_catalog):
    # lexical search hits name / folder / column substrings even with no embedder
    hits = client.get("/api/catalog/search", params={"q": "scale_00001", "mode": "lexical"}).json()
    assert any(t["name"] == "scale_00001" for t in hits)


def test_set_metadata_roundtrip(scale_catalog):
    tid = "tbl_scale_00042"
    r = client.put(f"/api/catalog/tables/{tid}/metadata",
                   json={"folder": "curated/featured", "tags": ["blessed"], "owner": "erin",
                         "description": "hand-picked"})
    assert r.status_code == 200
    body = r.json()
    assert body["folder"] == "curated/featured" and "blessed" in body["tags"] and body["owner"] == "erin"
    # it's now findable by the new folder + tag, and gone from its old team folder
    assert client.get(
        "/api/catalog/tables", params={"folder": "curated", "tags": "blessed"}
    ).json()["total"] == 1
    assert client.get("/api/catalog/search", params={"q": "hand-picked"}).json()[0]["name"] == "scale_00042"


def test_bounded_lineage_depth_and_maxnodes():
    # a long derivation chain a -> b -> c -> ... ; lineage is capped by depth AND max_nodes, and says so
    uris = [f"{_SCALE}chain/{i}" for i in range(12)]
    try:
        metadb.catalog_bulk_seed([_doc(f"chain_{i}", u) for i, u in enumerate(uris)])
        for a, b in zip(uris, uris[1:]):
            _record_lineage(a, b)
        deep = client.get("/api/catalog/lineage", params={"uri": uris[0], "depth": 20, "maxNodes": 5000}).json()
        assert len(deep["nodes"]) == 12 and deep["truncated"] is False
        shallow = client.get("/api/catalog/lineage", params={"uri": uris[0], "depth": 2}).json()
        assert len(shallow["nodes"]) < 12 and shallow["truncated"] is True
        capped = client.get("/api/catalog/lineage", params={"uri": uris[0], "depth": 20, "maxNodes": 4}).json()
        assert len(capped["nodes"]) <= 4 and capped["truncated"] is True
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_register_with_folder_and_tags(tmp_path):
    import duckdb
    p = str(tmp_path / "widgets.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 id) TO '{p}' (FORMAT PARQUET)")
    r = client.post("/api/catalog/register", json={
        "uri": p, "name": "widgets", "folder": "sandbox/demo", "tags": ["temp", "demo"], "owner": "zoe"})
    assert r.status_code == 200
    t = r.json()
    assert t["folder"] == "sandbox/demo" and set(t["tags"]) == {"temp", "demo"} and t["owner"] == "zoe"
    try:
        assert client.get("/api/catalog/tables", params={"folder": "sandbox"}).json()["total"] >= 1
    finally:
        client.delete(f"/api/catalog/tables/{t['id']}", params={
            "expected_registration_id": t["registrationId"],
            "expected_revision": t["metadataRevision"],
        })


def test_semantic_and_hybrid_search():
    """Inject a deterministic hashing embedder and verify semantic ranking + hybrid fusion + the
    graceful lexical fallback. Torn down so the global catalog singleton keeps no embedder."""
    cat = get_deps().catalog
    metadb.catalog_delete_prefix(_SEM)

    def embed(texts):
        # a hashing bag-of-words vectorizer: deterministic, offline, cosine ~ word overlap
        out = []
        for text in texts:
            v = [0.0] * 64
            for w in text.lower().split():
                v[hash(w) % 64] += 1.0
            out.append(v)
        return out

    metadb.catalog_bulk_seed([
        _doc("customers", f"{_SEM}c", folder="semantic/sales", tags=["silver"], owner="crm",
             description="people who buy and purchase products", cols=["email"]),
        _doc("shipments", f"{_SEM}s", folder="semantic/ops",
             description="packages parcels in transit tracking delivery"),
        _doc("invoices", f"{_SEM}i", folder="semantic/finance", tags=["gold"], owner="finance",
             description="billing payments money owed amounts due", cols=["amount"]),
        # This is the unfiltered semantic winner. A filter applied only AFTER top-k would return an
        # empty page instead of continuing to the best candidate inside semantic/finance.
        _doc("billing_archive", f"{_SEM}a", folder="semantic/archive", tags=["gold"], owner="finance",
             description="billing payments money billing payments money", cols=["amount"]),
    ])
    try:
        cat.set_embedder(embed, "test")
        cat._reindex_embeddings()  # synchronous embed of the seeded docs (idempotent)
        sem = client.get("/api/catalog/search", params={"q": "billing payments money", "mode": "semantic"}).json()
        sem_uris = [t["uri"] for t in sem if t["uri"].startswith(_SEM)]
        assert sem_uris and sem_uris[0] == f"{_SEM}a"
        filtered = client.get("/api/catalog/search", params={
            "q": "billing payments money", "mode": "semantic", "limit": 1,
            "folder": "semantic/finance", "tags": "gold", "owner": "finance",
            "hasColumns": "amount",
        }).json()
        assert [t["uri"] for t in filtered] == [f"{_SEM}i"]
        assert client.get("/api/catalog/search", params={
            "q": "billing payments money", "mode": "semantic", "folder": "semantic/finance",
            "tags": "missing",
        }).json() == []
        # hybrid returns results and still surfaces the semantic winner among the top
        hyb = client.get("/api/catalog/search", params={
            "q": "billing payments", "mode": "hybrid", "folder": "semantic/finance",
        }).json()
        assert any(t["uri"] == f"{_SEM}i" for t in hyb)
    finally:
        cat._embedder = None
        cat._embed_model = ""
        metadb.catalog_delete_prefix(_SEM)


def test_list_page_provider_api(scale_catalog):
    # the provider-level primitive the SPI documents (used by external catalogs) is bounded + totals
    page = get_deps().catalog.list_page(CatalogQuery(folder="team4", sort="name", limit=10))
    assert len(page.items) == 10 and page.total == 2000 // 5 and page.has_more is True


def test_default_catalog_is_loaded_as_a_plugin():
    """The built-in catalog is not a privileged core instantiation — it's registered FIRST through the
    public reg.set_catalog seam by a bundled plugin, so swapping in an external catalog is first-class."""
    from hub.backends import CatalogProvider
    from hub.plugins.catalog import InMemoryCatalog
    deps = get_deps()
    # it registered as a plugin (source=builtin), loaded before external plugins
    assert any(p.get("name") == "default-catalog" and p.get("source") == "builtin" for p in deps.plugins)
    # and it's the real provider, conforming to the protocol
    assert isinstance(deps.catalog, InMemoryCatalog)
    assert isinstance(deps.catalog, CatalogProvider)

    # the bundled register() is exactly what a third-party catalog plugin would call
    from hub.plugins import default_catalog

    class _Reg:
        def __init__(self, d):
            self.deps = d
            self.chosen = None

        def set_catalog(self, c):
            self.chosen = c

    reg = _Reg(deps)
    default_catalog.register(reg)
    assert isinstance(reg.chosen, InMemoryCatalog)


def test_search_multi_token_and_tags(scale_catalog):
    # every whitespace token must match SOMEWHERE — "team0 gold" means folder team0 AND tag gold,
    # even though the words never appear adjacent in any single field
    both = client.get("/api/catalog/tables", params={"q": "team0 gold", "limit": 1}).json()["total"]
    gold_in_team0 = client.get(
        "/api/catalog/tables", params={"folder": "team0", "tags": "gold", "limit": 1}
    ).json()["total"]
    assert both == gold_in_team0 > 0
    # a tag term matches through plain q too (the documented search contract)
    by_q = client.get("/api/catalog/tables", params={"q": "pii", "limit": 1}).json()["total"]
    by_tag = client.get("/api/catalog/tables", params={"tags": "pii", "limit": 1}).json()["total"]
    assert by_q >= by_tag > 0
    # LIKE metacharacters in q are literal, not wildcards
    assert client.get("/api/catalog/tables", params={"q": "%", "limit": 1}).json()["total"] == 0


def test_set_metadata_partial_and_clear(scale_catalog):
    tid = "tbl_scale_00043"
    base = client.put(f"/api/catalog/tables/{tid}/metadata",
                      json={"folder": "curated/x", "tags": ["keep"], "owner": "erin", "description": "d1"}).json()
    assert base["owner"] == "erin" and base["tags"] == ["keep"]
    # a partial body touches ONLY the fields present
    part = client.put(f"/api/catalog/tables/{tid}/metadata", json={"description": "d2"}).json()
    assert part["description"] == "d2"
    assert part["owner"] == "erin" and part["tags"] == ["keep"] and part["folder"] == "curated/x"
    # an explicit null clears
    cleared = client.put(f"/api/catalog/tables/{tid}/metadata", json={"owner": None, "description": None}).json()
    assert cleared["owner"] is None and cleared["description"] is None
    assert cleared["tags"] == ["keep"], "tags were absent from the body, so they survive"


def test_unregister_cleans_facts_keys_relationships_without_reregister_inheritance():
    """A deleted table must not haunt lineage/ER as a ghost node, and a NEW dataset re-registered at
    the same uri must not inherit the old declared key or parents."""
    a, b = f"{_SCALE}orph/a", f"{_SCALE}orph/b"
    try:
        metadb.catalog_bulk_seed([_doc("orph_a", a, cols=["id"]), _doc("orph_b", b, cols=["id"])])
        _record_lineage(a, b)
        metadb.catalog_set_declared_key(b, ["id"])
        client.post("/api/catalog/relationships", json={
            "leftUri": a, "leftColumns": ["id"], "rightUri": b, "rightColumns": ["id"],
            "kind": "one_to_one"})
        registered = client.get("/api/catalog/tables/tbl_orph_b").json()
        assert client.delete("/api/catalog/tables/tbl_orph_b", params={
            "expected_registration_id": registered["registrationId"],
            "expected_revision": registered["metadataRevision"],
        }).json() == {"ok": True}
        lin = client.get("/api/catalog/lineage", params={"uri": a}).json()
        assert all(n["uri"] != b for n in lin["nodes"]) and not lin["edges"]
        assert metadb.catalog_declared_keys([b]) == {}
        assert all(b not in (r["leftUri"], r["rightUri"])
                   for r in client.get("/api/catalog/relationships").json())
        with metadb.session() as session:
            assert session.scalar(select(metadb.CatalogLineageFact).where(
                (metadb.CatalogLineageFact.source_uri == b)
                | (metadb.CatalogLineageFact.destination_uri == b)
                | (metadb.CatalogLineageFact.source_key == b)
                | (metadb.CatalogLineageFact.destination_key == b)
            )) is None

        assert metadb.catalog_bulk_seed([_doc("orph_b_fresh", b, cols=["id"])]) == 1
        fresh = client.get("/api/catalog/lineage", params={"uri": b}).json()
        assert fresh["edges"] == []
        assert all(b not in (row["parent"], row["child"])
                   for row in metadb.catalog_lineage_pairs())
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_browse_tree_truncation_flag():
    folder = "trunctest/big"
    try:
        metadb.catalog_bulk_seed([_doc(f"tr_{i:03d}", f"{_SCALE}tr/{i}", folder=folder) for i in range(120)])
        level = client.get("/api/catalog/tree", params={"prefix": folder}).json()
        assert level["totalTables"] == 120 and level["truncated"] is True
        assert len(level["tables"]) < 120, "the tree returns a bounded sample, not the full folder"
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_legacy_lineage_edges_export_is_removed():
    assert client.get("/api/catalog/edges").status_code == 404


@pytest.mark.parametrize("after_id", ["", "-1", "01", "abc", str(2**63)])
def test_lineage_facts_reject_invalid_or_overflow_cursor(after_id):
    response = client.get("/api/catalog/lineage/facts", params={"afterId": after_id})
    assert response.status_code == 422


def test_lineage_facts_wire_contract_preserves_bigint_ids(monkeypatch):
    fact_id = 2**53 + 1
    created_at = datetime.datetime(2026, 7, 16, 8, 30, tzinfo=datetime.UTC)
    row = {
        "id": fact_id,
        "fact_key": "lineage:v1:test-fact",
        "publication_key": "lineage-publication:v1:test-publication",
        "source_key": "tbl_raw_orders",
        "source_uri": "s3://warehouse/raw/orders.lance",
        "source_version": "17",
        "destination_key": "tbl_curated_orders",
        "destination_uri": "s3://warehouse/curated/orders.lance",
        "destination_version": "23",
        "run_id": "run-123",
        "attempt_id": "attempt-2",
        "producer": "canvas-orders",
        "producer_version": 7,
        "step_id": "write-orders",
        "provenance": "run",
        "field_mappings": [{
            "source_field": "raw_order_id",
            "destination_field": "order_id",
        }],
        "created_at": created_at,
    }
    monkeypatch.setattr(
        metadb,
        "catalog_lineage_facts_page",
        lambda *, limit, after_id: ([row], fact_id, True),
    )

    response = client.get(
        "/api/catalog/lineage/facts", params={"limit": 1, "afterId": "0"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [{
            "id": str(fact_id),
            "factKey": "lineage:v1:test-fact",
            "publicationKey": "lineage-publication:v1:test-publication",
            "sourceKey": "tbl_raw_orders",
            "sourceUri": "s3://warehouse/raw/orders.lance",
            "sourceVersion": "17",
            "destinationKey": "tbl_curated_orders",
            "destinationUri": "s3://warehouse/curated/orders.lance",
            "destinationVersion": "23",
            "runId": "run-123",
            "executionManifestSha256": None,
            "attemptId": "attempt-2",
            "producer": "canvas-orders",
            "producerVersion": 7,
            "stepId": "write-orders",
            "provenance": "run",
            "fieldMappings": [{
                "sourceDatasetId": None,
                "sourceVersion": None,
                "sourceField": "raw_order_id",
                "sourceFieldId": None,
                "destinationField": "order_id",
            }],
            "createdAt": "2026-07-16T08:30:00Z",
        }],
        "nextAfterId": str(fact_id),
        "hasMore": True,
    }


def test_lineage_facts_export_uses_provider_capability_or_fails_explicitly(monkeypatch):
    from hub.models import LineageFact, LineageFactsPage

    calls = []
    fact = LineageFact(
        id="11", fact_key="fact-11", publication_key="publication-11",
        source_key="source", source_uri="mem://source",
        destination_key="destination", destination_uri="mem://destination",
        provenance="manual", created_at=datetime.datetime.now(datetime.UTC),
    )

    class Exporter:
        def lineage_facts_page(self, *, limit, after_id):
            calls.append((limit, after_id))
            return LineageFactsPage(items=[fact], next_after_id="11", has_more=True)

    def local_side_store_must_not_run(**_kwargs):
        raise AssertionError("external catalog export fell back to the built-in side-store")

    deps = get_deps()
    monkeypatch.setattr(metadb, "catalog_lineage_facts_page", local_side_store_must_not_run)
    monkeypatch.setattr(deps, "catalog", Exporter())
    response = client.get(
        "/api/catalog/lineage/facts", params={"limit": 7, "afterId": "9"})
    assert response.status_code == 200
    assert response.json() == {
        "items": [fact.model_dump(by_alias=True, mode="json")],
        "nextAfterId": "11", "hasMore": True,
    }
    assert calls == [(7, 9)]

    monkeypatch.setattr(deps, "catalog", object())
    unsupported = client.get("/api/catalog/lineage/facts")
    assert unsupported.status_code == 501
    assert unsupported.json() == {
        "detail": "catalog provider does not support lineage fact export",
        "code": "not_implemented",
        "retryable": False,
    }


def test_field_lineage_lookup_uses_provider_capability_and_explicit_states(monkeypatch):
    from hub.models import FieldLineagePage, FieldLineageProjection

    calls = []
    projection = FieldLineageProjection(
        id="17",
        fact_key="fact-17",
        publication_key="publication-17",
        source_dataset_id="source-dataset",
        source_version="source-v1",
        source_field="raw_id",
        source_field_id="field-raw-id",
        destination_dataset_id="destination-dataset",
        destination_revision_id="destination-v2",
        destination_field="id",
        created_at=datetime.datetime.now(datetime.UTC),
    )

    class Exporter:
        def field_lineage_page(self, **kwargs):
            calls.append(kwargs)
            return FieldLineagePage(state="available", items=[projection])

    deps = get_deps()
    monkeypatch.setattr(deps, "catalog", Exporter())
    response = client.get("/api/catalog/lineage/fields", params={
        "datasetId": "destination-dataset",
        "revisionId": "destination-v2",
        "destinationFields": "id",
        "limit": 7,
        "afterId": "9",
    })
    assert response.status_code == 200
    assert response.json() == {
        "state": "available",
        "items": [projection.model_dump(by_alias=True, mode="json")],
        "nextAfterId": None,
    }
    assert calls == [{
        "dataset_id": "destination-dataset",
        "revision_id": "destination-v2",
        "destination_fields": ["id"],
        "limit": 7,
        "after_id": 9,
    }]

    monkeypatch.setattr(deps, "catalog", object())
    unsupported = client.get("/api/catalog/lineage/fields", params={
        "datasetId": "destination-dataset",
        "revisionId": "destination-v2",
        "destinationFields": "id",
    })
    assert unsupported.status_code == 200
    assert unsupported.json() == {
        "state": "unsupported", "items": [], "nextAfterId": None,
    }


def test_field_lineage_lookup_rejects_invalid_provider_window(monkeypatch):
    class Exporter:
        @staticmethod
        def field_lineage_page(**_kwargs):
            return {
                "state": "available",
                "items": [{
                    "id": "11",
                    "factKey": "fact-11",
                    "publicationKey": "publication-11",
                    "sourceDatasetId": "source",
                    "sourceVersion": "v1",
                    "sourceField": "raw_id",
                    "destinationDatasetId": "wrong-destination",
                    "destinationRevisionId": "v2",
                    "destinationField": "id",
                    "createdAt": "2026-07-16T08:30:00Z",
                }],
                "nextAfterId": None,
            }

    monkeypatch.setattr(get_deps(), "catalog", Exporter())
    response = client.get("/api/catalog/lineage/fields", params={
        "datasetId": "destination",
        "revisionId": "v2",
        "destinationFields": "id",
        "afterId": "9",
    })
    assert response.status_code == 502
    assert response.json()["detail"] == \
        "catalog provider returned an invalid field lineage page"


def test_lineage_fact_export_rejects_provider_cursor_contract_violations(monkeypatch):
    def fact(fact_id: int) -> dict:
        return {
            "id": str(fact_id), "factKey": f"fact-{fact_id}",
            "publicationKey": f"publication-{fact_id}",
            "sourceKey": "source", "sourceUri": "mem://source",
            "destinationKey": "destination", "destinationUri": "mem://destination",
            "provenance": "manual", "createdAt": "2026-07-16T08:30:00Z",
        }

    cases = [
        ({"items": [], "nextAfterId": "11", "hasMore": True}, 1),
        ({"items": [fact(9)], "nextAfterId": "9", "hasMore": True}, 1),
        ({"items": [fact(11), fact(10)], "nextAfterId": "10", "hasMore": True}, 2),
        ({"items": [fact(11)], "nextAfterId": "12", "hasMore": True}, 1),
        ({"items": [fact(11), fact(12)], "nextAfterId": "12", "hasMore": True}, 1),
    ]
    deps = get_deps()
    for page, limit in cases:
        class Exporter:
            @staticmethod
            def lineage_facts_page(*, limit: int, after_id: int):
                assert after_id == 9
                return page

        monkeypatch.setattr(deps, "catalog", Exporter())
        response = client.get(
            "/api/catalog/lineage/facts",
            params={"limit": limit, "afterId": "9"},
        )
        assert response.status_code == 502
        assert response.json()["detail"] == \
            "catalog provider returned an invalid lineage fact page"


def test_lineage_facts_export_uses_real_keyset_pagination():
    uris = [f"{_SCALE}facts/{i}" for i in range(4)]
    run_id = f"run-facts-export-{uuid.uuid4().hex}"
    try:
        metadb.catalog_bulk_seed([
            _doc(f"facts_{i}", uri, version=10 + i)
            for i, uri in enumerate(uris)
        ])
        for i, (source, destination) in enumerate(zip(uris, uris[1:])):
            source_table = metadb.catalog_get(source)
            assert source_table is not None
            assert _record_lineage(
                source,
                destination,
                lineage=_lineage(
                    key=f"facts-export-{uuid.uuid4().hex}",
                    run_id=run_id,
                    attempt_id=f"attempt-{i}",
                    producer="canvas-facts-export",
                    producer_version=7,
                    step_id=f"write-{i}",
                    provenance="run",
                    mappings=[{
                        "source_dataset_id": source_table["registrationId"],
                        "source_version": source_table["version"],
                        "source_field": "raw_id",
                        "source_field_id": None,
                        "destination_field": "id",
                    }],
                ),
            ) == 1
        with metadb.session() as session:
            ids = list(session.scalars(select(metadb.CatalogLineageFact.id).where(
                metadb.CatalogLineageFact.run_id == run_id,
            ).order_by(metadb.CatalogLineageFact.id)))
        assert len(ids) == 3
        assert ids == sorted(ids) and all(isinstance(value, int) for value in ids)

        first = client.get(
            "/api/catalog/lineage/facts",
            params={"limit": 2, "afterId": str(ids[0] - 1)},
        )
        assert first.status_code == 200
        first_page = first.json()
        assert [item["id"] for item in first_page["items"]] == [str(ids[0]), str(ids[1])]
        assert first_page["nextAfterId"] == str(ids[1])
        assert first_page["hasMore"] is True
        first_fact = dict(first_page["items"][0])
        fact_key = first_fact.pop("factKey")
        publication_key = first_fact.pop("publicationKey")
        created_at = first_fact.pop("createdAt")
        assert first_fact == {
            "id": str(ids[0]),
            "sourceKey": uris[0],
            "sourceUri": uris[0],
            "sourceVersion": "10",
            "destinationKey": uris[1],
            "destinationUri": uris[1],
            "destinationVersion": "11",
            "runId": run_id,
            "executionManifestSha256": None,
            "attemptId": "attempt-0",
            "producer": "canvas-facts-export",
            "producerVersion": 7,
            "stepId": "write-0",
            "provenance": "run",
            "fieldMappings": [{
                "sourceDatasetId": metadb.catalog_get(uris[0])["registrationId"],
                "sourceVersion": "10",
                "sourceField": "raw_id",
                "sourceFieldId": None,
                "destinationField": "id",
            }],
        }
        assert fact_key.startswith("lineage-fact:v1:sha256:")
        assert publication_key.startswith("lineage-publication:v1:sha256:")
        assert created_at.endswith("Z")

        # Deleting a row before the keyset cursor must not shift or duplicate the next page.
        with metadb.session() as session:
            obsolete = session.get(metadb.CatalogLineageFact, ids[0])
            assert obsolete is not None
            session.execute(delete(metadb.CatalogFieldLineageProjection).where(
                metadb.CatalogFieldLineageProjection.fact_id == ids[0]))
            session.delete(obsolete)

        second = client.get(
            "/api/catalog/lineage/facts",
            params={"limit": 2, "afterId": first_page["nextAfterId"]},
        )
        assert second.status_code == 200
        second_page = second.json()
        assert [item["id"] for item in second_page["items"]] == [str(ids[2])]
        assert second_page["nextAfterId"] is None
        assert second_page["hasMore"] is False
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_lineage_pair_limit_counts_pairs_and_graph_aggregates_facts():
    root = f"{_SCALE}aggregate/root"
    children = [f"{_SCALE}aggregate/child-{i}" for i in range(2)]
    try:
        metadb.catalog_bulk_seed([
            _doc("aggregate_root", root),
            *[_doc(f"aggregate_child_{i}", uri) for i, uri in enumerate(children)],
        ])
        for _ in range(4):
            _record_lineage(root, children[0])
        _record_lineage(root, children[1])

        one_pair, truncated = metadb.catalog_lineage_pairs_touching([root], limit=1)
        assert len(one_pair) == 1 and truncated is True
        two_pairs, truncated = metadb.catalog_lineage_pairs_touching([root], limit=2)
        assert truncated is False
        assert {(row["child"], row["fact_count"]) for row in two_pairs} == {
            (children[0], 4),
            (children[1], 1),
        }

        graph = client.get("/api/catalog/lineage", params={"uri": root}).json()
        assert {(edge["child"], edge["factCount"]) for edge in graph["edges"]} == {
            (children[0], 4),
            (children[1], 1),
        }
        assert graph["truncated"] is False
        _record_lineage(children[0], children[1])
        complete_at_boundary = client.get(
            "/api/catalog/lineage",
            params={"uri": root, "depth": 1, "maxNodes": 10},
        ).json()
        assert {(edge["parent"], edge["child"]) for edge in complete_at_boundary["edges"]} == {
            (root, children[0]),
            (root, children[1]),
            (children[0], children[1]),
        }
        assert complete_at_boundary["truncated"] is False

        grandchild = f"{_SCALE}aggregate/grandchild"
        metadb.catalog_bulk_seed([_doc("aggregate_grandchild", grandchild)])
        _record_lineage(children[0], grandchild)
        deeper_than_boundary = client.get(
            "/api/catalog/lineage",
            params={"uri": root, "depth": 1, "maxNodes": 10},
        ).json()
        assert deeper_than_boundary["truncated"] is True
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_lineage_graph_reports_fact_count_and_pair_truncation(monkeypatch):
    root = f"{_SCALE}lineage-root"
    child = f"{_SCALE}lineage-child"

    def touching(frontier, *, limit):
        assert frontier == [root]
        assert limit >= 1
        return ([{
            "parent": root,
            "child": child,
            "fact_count": 4,
        }], True)

    monkeypatch.setattr(metadb, "catalog_lineage_key_pairs_touching", touching)

    response = client.get(
        "/api/catalog/lineage",
        params={"uri": root, "depth": 1, "maxNodes": 10},
    )

    assert response.status_code == 200
    assert response.json()["edges"] == [{
        "parent": root,
        "child": child,
        "factCount": 4,
    }]
    assert response.json()["truncated"] is True


@pytest.mark.parametrize("uri", ["/", "x" * 8_193])
def test_lineage_rejects_invalid_root_uri_at_the_request_boundary(uri):
    response = client.get("/api/catalog/lineage", params={"uri": uri})

    assert response.status_code == 422
    assert response.json().get("detail")


def test_facets_advertise_semantic_availability():
    cat = get_deps().catalog
    assert client.get("/api/catalog/facets").json()["semanticAvailable"] is False
    try:
        cat._embedder = lambda texts: [[1.0] for _ in texts]
        assert client.get("/api/catalog/facets").json()["semanticAvailable"] is True
    finally:
        cat._embedder = None


def test_incomplete_catalog_provider_fails_during_registration():
    class IncompleteProvider:
        def get_table(self, id_or_name):
            raise KeyError(id_or_name)

    original = object()

    class _FakeDeps:
        catalog = original

    from hub.deps import Registry
    reg = Registry.__new__(Registry)
    reg.deps = _FakeDeps()
    with pytest.raises(TypeError) as error:
        reg.set_catalog(IncompleteProvider())
    message = str(error.value)
    assert message.startswith("catalog provider does not satisfy CatalogProvider; missing methods: ")
    assert all(name in message for name in ("browse", "facets", "list_page", "search"))
    assert "list_tables" not in message
    assert reg.deps.catalog is original


def test_search_pushes_structured_query_into_provider(monkeypatch):
    catalog = get_deps().catalog
    real_search = catalog.search
    calls = []

    def search(q, mode="hybrid", limit=50, *, query=None):
        calls.append((q, mode, limit, query))
        return real_search(q, mode=mode, limit=limit, query=query)

    monkeypatch.setattr(catalog, "search", search)
    response = client.get("/api/catalog/search", params={
        "q": "orders", "mode": "lexical", "limit": 17, "folder": "team/finance",
        "tags": "gold,curated", "owner": "ann", "hasColumns": "order_id",
    })
    assert response.status_code == 200
    q, mode, limit, query = calls.pop()
    assert (q, mode, limit) == ("orders", "lexical", 17)
    assert query == CatalogQuery(
        q="orders", folder="team/finance", tags=["gold", "curated"], owner="ann",
        has_columns=["order_id"], limit=17,
    )


def test_semantic_plugin_registers_embedder():
    """The shipped dp_semantic_catalog plugin wires an embedder through reg.add_embedder when enabled,
    and is a no-op when disabled — without importing the heavy model (the embedder fn is lazy)."""
    import importlib.util
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_semantic_catalog" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_semantic_ref", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Reg:
        def __init__(self, cfg):
            self._cfg = cfg
            self.embedder = None
            self.model = None

        def config(self, key, default=None):
            return self._cfg.get(key, default)

        def add_embedder(self, fn, model="custom"):
            self.embedder, self.model = fn, model

    on = _Reg({"enabled": True, "model": "m"})
    mod.register(on)
    assert callable(on.embedder) and on.model == "m"

    off = _Reg({"enabled": False})
    mod.register(off)
    assert off.embedder is None
