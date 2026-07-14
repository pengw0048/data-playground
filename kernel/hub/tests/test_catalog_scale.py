"""Catalog at scale: server-side browse / search / facets / folders / bounded lineage / semantic.

These exercise the discovery surface that has to hold thousands of tables — every assertion is about
pushdown (filter/sort/paginate/facet in the DB, bounded payloads), organization (folders/tags/owner),
and the semantic-search seam. Seeded entries use a distinct `mem://` uri prefix and are torn down, so
they can't leak into the rest of the suite.
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from hub import metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import CatalogQuery

client = TestClient(app)

_SCALE = "mem://scale/"
_SEM = "mem://sem/"


def _doc(name, uri, *, folder="", tags=None, owner=None, description=None, rows=0, cols=None):
    """A catalog_bulk_seed entry: {uri, name, doc} where doc is the full CatalogTable-shaped dict."""
    return {"uri": uri, "name": name, "doc": {
        "id": f"tbl_{name}", "name": name, "uri": uri, "folder": folder, "tags": tags or [],
        "owner": owner, "description": description, "rowCount": rows,
        "columns": [{"name": c, "type": "VARCHAR"} for c in (cols or [])],
    }}


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


def test_pagination_headers_and_windowing(scale_catalog):
    n = scale_catalog
    r = client.get("/api/catalog/tables", params={"folder": "team0", "limit": 25, "offset": 0, "sort": "name"})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 25, "the page is bounded by limit, not the catalog size"
    total = int(r.headers["X-Total-Count"])
    assert total == n // 5, "team0 holds exactly a fifth of the seeded tables"
    assert r.headers["X-Has-More"] == "1"
    # a deterministic second page has no overlap with the first (stable sort + offset)
    r2 = client.get("/api/catalog/tables", params={"folder": "team0", "limit": 25, "offset": 25, "sort": "name"})
    first = {t["uri"] for t in items}
    second = {t["uri"] for t in r2.json()}
    assert first.isdisjoint(second)


def test_sort_by_rows_and_usage(scale_catalog):
    r = client.get("/api/catalog/tables", params={"folder": "team1", "sort": "rows", "order": "desc", "limit": 5})
    rows = [t["rowCount"] for t in r.json()]
    assert rows == sorted(rows, reverse=True) and rows[0] > rows[-1]


def test_folder_subtree_filter(scale_catalog):
    # a folder filter matches the folder AND its subtree
    total_top = int(client.get("/api/catalog/tables", params={"folder": "team2", "limit": 1}).headers["X-Total-Count"])
    total_sub = int(client.get("/api/catalog/tables", params={"folder": "team2/ds2", "limit": 1}).headers["X-Total-Count"])
    assert total_top > total_sub > 0


def test_tag_and_owner_and_column_filters(scale_catalog):
    # tags AND together; pii is a strict subset of the seeded set
    pii = int(client.get("/api/catalog/tables", params={"tags": "pii", "limit": 1}).headers["X-Total-Count"])
    pii_gold = int(client.get("/api/catalog/tables", params={"tags": "pii,gold", "limit": 1}).headers["X-Total-Count"])
    assert 0 < pii_gold <= pii
    # every pii table has the email column, so has-column agrees
    has_email = int(client.get("/api/catalog/tables", params={"hasColumns": "email", "limit": 1}).headers["X-Total-Count"])
    assert has_email == pii
    owned = int(client.get("/api/catalog/tables", params={"owner": "alice", "limit": 1}).headers["X-Total-Count"])
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
    assert int(client.get("/api/catalog/tables", params={"folder": "curated", "tags": "blessed"}).headers["X-Total-Count"]) == 1
    assert client.get("/api/catalog/search", params={"q": "hand-picked"}).json()[0]["name"] == "scale_00042"


def test_bounded_lineage_depth_and_maxnodes():
    # a long derivation chain a -> b -> c -> ... ; lineage is capped by depth AND max_nodes, and says so
    uris = [f"{_SCALE}chain/{i}" for i in range(12)]
    try:
        metadb.catalog_bulk_seed([_doc(f"chain_{i}", u) for i, u in enumerate(uris)])
        for a, b in zip(uris, uris[1:]):
            metadb.catalog_add_edge(a, b, pipeline="p")
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
        assert int(client.get("/api/catalog/tables", params={"folder": "sandbox"}).headers["X-Total-Count"]) >= 1
    finally:
        client.delete(f"/api/catalog/tables/{t['id']}")


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
    both = int(client.get("/api/catalog/tables", params={"q": "team0 gold", "limit": 1}).headers["X-Total-Count"])
    gold_in_team0 = int(client.get("/api/catalog/tables",
                                   params={"folder": "team0", "tags": "gold", "limit": 1}).headers["X-Total-Count"])
    assert both == gold_in_team0 > 0
    # a tag term matches through plain q too (the documented search contract)
    by_q = int(client.get("/api/catalog/tables", params={"q": "pii", "limit": 1}).headers["X-Total-Count"])
    by_tag = int(client.get("/api/catalog/tables", params={"tags": "pii", "limit": 1}).headers["X-Total-Count"])
    assert by_q >= by_tag > 0
    # LIKE metacharacters in q are literal, not wildcards
    assert int(client.get("/api/catalog/tables", params={"q": "%", "limit": 1}).headers["X-Total-Count"]) == 0


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


def test_unregister_cleans_edges_keys_relationships():
    """A deleted table must not haunt lineage/ER as a ghost node, and a NEW dataset re-registered at
    the same uri must not inherit the old declared key or parents."""
    a, b = f"{_SCALE}orph/a", f"{_SCALE}orph/b"
    try:
        metadb.catalog_bulk_seed([_doc("orph_a", a, cols=["id"]), _doc("orph_b", b, cols=["id"])])
        metadb.catalog_add_edge(a, b, pipeline="p")
        metadb.catalog_set_declared_key(b, ["id"])
        client.post("/api/catalog/relationships", json={
            "leftUri": a, "leftColumns": ["id"], "rightUri": b, "rightColumns": ["id"],
            "kind": "one_to_one"})
        assert client.delete("/api/catalog/tables/tbl_orph_b").json() == {"ok": True}
        lin = client.get("/api/catalog/lineage", params={"uri": a}).json()
        assert all(n["uri"] != b for n in lin["nodes"]) and not lin["edges"]
        assert metadb.catalog_declared_keys([b]) == {}
        assert all(b not in (r["leftUri"], r["rightUri"])
                   for r in client.get("/api/catalog/relationships").json())
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


def test_lineage_edges_export_page():
    uris = [f"{_SCALE}exp/{i}" for i in range(4)]
    try:
        metadb.catalog_bulk_seed([_doc(f"exp_{i}", u) for i, u in enumerate(uris)])
        for a, b in zip(uris, uris[1:]):
            metadb.catalog_add_edge(a, b, pipeline="exp")
        r = client.get("/api/catalog/edges", params={"limit": 2, "offset": 0})
        assert r.status_code == 200 and len(r.json()) == 2
        assert int(r.headers["X-Total-Count"]) >= 3
        page2 = client.get("/api/catalog/edges", params={"limit": 2, "offset": 2}).json()
        assert {(e["parent"], e["child"]) for e in r.json()}.isdisjoint(
            {(e["parent"], e["child"]) for e in page2})
    finally:
        metadb.catalog_delete_prefix(_SCALE)


def test_facets_advertise_semantic_availability():
    cat = get_deps().catalog
    assert client.get("/api/catalog/facets").json()["semanticAvailable"] is False
    try:
        cat._embedder = lambda texts: [[1.0] for _ in texts]
        assert client.get("/api/catalog/facets").json()["semanticAvailable"] is True
    finally:
        cat._embedder = None


def test_old_protocol_provider_still_works_via_compat():
    """A provider written against the PRE-scale protocol (only list_tables/get_table) keeps working
    behind the new discovery surface: reg.set_catalog wraps it in CatalogCompat."""
    from hub.models import CatalogTable
    from hub.plugins.catalog import CatalogCompat

    class OldProvider:
        name = "old"

        def __init__(self):
            self._tables = [
                CatalogTable(id="tbl_x", name="x", uri="mem://old/x", folder="f1", tags=["t1"],
                             owner="ann", columns=[], keys=[]),
                CatalogTable(id="tbl_y", name="y", uri="mem://old/y", folder="f1/sub", tags=[],
                             owner=None, columns=[], keys=[]),
                CatalogTable(id="tbl_z", name="z", uri="mem://old/z", folder="", tags=["t1"],
                             owner="ann", columns=[], keys=[]),
            ]

        def list_tables(self, q=None):
            return [t for t in self._tables if not q or q in t.name]

        def get_table(self, id_or_name):
            for t in self._tables:
                if id_or_name in (t.id, t.name, t.uri):
                    return t
            raise KeyError(id_or_name)

    class _FakeDeps:
        catalog = None
    from hub.deps import Registry
    reg = Registry.__new__(Registry)
    reg.deps = _FakeDeps()
    reg.set_catalog(OldProvider())
    cat = reg.deps.catalog
    assert isinstance(cat, CatalogCompat)
    page = cat.list_page(CatalogQuery(folder="f1", limit=10))
    assert {t.name for t in page.items} == {"x", "y"} and page.total == 2
    f = cat.facets(CatalogQuery())
    assert {v.value for v in f.owners} == {"ann"} and {v.value for v in f.tags} == {"t1"}
    tree = cat.browse("")
    assert {fo.name for fo in tree.folders} == {"f1"} and {t.name for t in tree.tables} == {"z"}
    assert [t.name for t in cat.search("x")] == ["x"]
    assert {t.name for t in cat.search("", query=CatalogQuery(folder="f1", owner="ann"))} == {"x"}
    assert cat.search_modes() == ["lexical"]
    assert cat.get_table("tbl_y").name == "y"  # passthrough still works


def test_migration_0017_backfills_pre_existing_docs(tmp_path, monkeypatch):
    """An UPGRADED install (catalog rows written before 0017) is immediately filterable: the backfill
    promotes folder/owner/rows out of each stored doc and populates the tag/column join tables. Run
    against a THROWAWAY sqlite db so the suite's own DB is untouched."""
    import json as _json

    import sqlalchemy as sa
    from alembic import command

    from hub.settings import settings as live_settings

    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setattr(live_settings, "database_url", url)
    cfg = metadb._alembic_cfg()
    command.upgrade(cfg, "0016_run_state_owner")
    eng = sa.create_engine(url)
    doc = {"id": "tbl_events", "name": "events", "uri": "/data/events.parquet",
           "folder": "prod/web", "tags": ["gold", "web"], "owner": "growth",
           "description": "click events", "rowCount": 123,
           "columns": [{"name": "user_id", "type": "BIGINT"}, {"name": "ts", "type": "TIMESTAMP"}]}
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO catalog_entries (uri, name, doc, updated_at) "
                          "VALUES (:u, :n, :d, CURRENT_TIMESTAMP)"),
                  {"u": doc["uri"], "n": "events", "d": _json.dumps(doc)})
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        row = c.execute(sa.text("SELECT tbl_id, folder, owner, row_count FROM catalog_entries")).one()
        assert tuple(row) == ("tbl_events", "prod/web", "growth", 123)
        tags = {t for (t,) in c.execute(sa.text("SELECT tag FROM catalog_tags"))}
        cols = {t for (t,) in c.execute(sa.text('SELECT "column" FROM catalog_columns'))}
        assert tags == {"gold", "web"} and cols == {"user_id", "ts"}
    eng.dispose()


def test_migration_0018_preserves_legacy_run_history(tmp_path, monkeypatch):
    """Existing run rows migrate with null links; new code can distinguish legacy history safely."""
    import sqlalchemy as sa
    from alembic import command

    from hub.settings import settings as live_settings

    url = f"sqlite:///{tmp_path}/run-links.db"
    monkeypatch.setattr(live_settings, "database_url", url)
    cfg = metadb._alembic_cfg()
    command.upgrade(cfg, "0017_catalog_org")
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO users (id, name) VALUES ('legacy-user', 'Legacy')"))
        c.execute(sa.text("INSERT INTO canvases (id, owner_id, name, version, doc) "
                          "VALUES ('legacy-canvas', 'legacy-user', 'Legacy', 1, '{}')"))
        c.execute(sa.text("INSERT INTO run_records (id, canvas_id, status, rows) "
                          "VALUES ('history-row', 'legacy-canvas', 'done', 12)"))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        row = c.execute(sa.text("SELECT run_id, output_uri FROM run_records WHERE id='history-row'")).one()
        assert tuple(row) == (None, None)
        indexes = {r[1] for r in c.execute(sa.text("PRAGMA index_list('run_records')"))}
        assert "ix_run_records_run_id" in indexes
    eng.dispose()


def test_migration_0019_adds_stable_object_attempt_identity(tmp_path, monkeypatch):
    """The GC registry upgrade preserves existing pointers and seeds one durable deployment owner."""
    import sqlalchemy as sa
    from alembic import command

    from hub.settings import settings as live_settings

    url = f"sqlite:///{tmp_path}/object-attempts.db"
    monkeypatch.setattr(live_settings, "database_url", url)
    cfg = metadb._alembic_cfg()
    command.upgrade(cfg, "0018_run_result_links")
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text(
            "INSERT INTO result_cache (key, doc, created_at) "
            "VALUES ('legacy-result', '{\"uri\":\"s3://bucket/old\"}', CURRENT_TIMESTAMP)"
        ))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        owner = c.execute(sa.text("SELECT owner_token FROM installation_identity WHERE id=1")).scalar_one()
        assert len(owner) == 32
        assert c.execute(sa.text("SELECT doc FROM result_cache WHERE key='legacy-result'")).scalar_one() == \
            '{"uri":"s3://bucket/old"}'
        indexes = {row[1] for row in c.execute(sa.text("PRAGMA index_list('object_attempts')"))}
        assert {"ix_object_attempts_gc", "ix_object_attempts_sink_target"} <= indexes
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM installation_identity")).scalar_one() == 1
        assert c.execute(sa.text("SELECT owner_token FROM installation_identity WHERE id=1")).scalar_one() == owner
    eng.dispose()


def test_migration_0020_quarantines_unsafe_legacy_and_removes_managed_visibility(
        tmp_path, monkeypatch):
    import json

    import sqlalchemy as sa
    from alembic import command

    from hub.settings import settings as live_settings

    url = f"sqlite:///{tmp_path}/object-lifecycle.db"
    monkeypatch.setattr(live_settings, "database_url", url)
    cfg = metadb._alembic_cfg()
    command.upgrade(cfg, "0019_object_attempts")
    eng = sa.create_engine(url)
    published = "s3://bucket/root/result.attempt-published"
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO users (id, name) VALUES ('u20', 'U20')"))
        c.execute(sa.text(
            "INSERT INTO canvases (id, owner_id, name, version, doc) "
            "VALUES ('c20', 'u20', 'C20', 1, '{}')"))
        for state in ("published", "writing", "retiring", "retired", "discarding"):
            uri = published if state == "published" else f"s3://bucket/root/result.attempt-{state}"
            c.execute(sa.text("""
                INSERT INTO object_attempts(uri, logical_uri, kind, run_id, state, created_at)
                VALUES (:uri, 's3://bucket/root/result.parquet', 'region', :run, :state,
                        CURRENT_TIMESTAMP)
            """), {"uri": uri, "run": f"run-{state}", "state": state})
        doc = json.dumps({"uri": published})
        c.execute(sa.text(
            "INSERT INTO result_cache(key, doc, created_at) VALUES ('cache20', :doc, CURRENT_TIMESTAMP)"),
            {"doc": doc})
        c.execute(sa.text("""
            INSERT INTO run_records(id, canvas_id, run_id, status, output_uri, created_at)
            VALUES ('record20', 'c20', 'run-published', 'done', :uri, CURRENT_TIMESTAMP)
        """), {"uri": published})
        c.execute(sa.text("""
            INSERT INTO run_states(run_id, canvas_id, status, doc, updated_at)
            VALUES ('run-published', 'c20', 'done', :doc, CURRENT_TIMESTAMP)
        """), {"doc": json.dumps({"status": "done", "output_uri": published})})
        c.execute(sa.text("""
            INSERT INTO catalog_entries(uri, name, doc, tbl_id, folder, usage, updated_at)
            VALUES (:uri, 'result', :doc, 'tbl_result20', 'gold', 0, CURRENT_TIMESTAMP)
        """), {"uri": published, "doc": json.dumps({
            "id": "tbl_result20", "name": "result", "uri": published,
            "folder": "gold", "tags": ["curated"],
        })})
        c.execute(sa.text(
            "INSERT INTO catalog_embeddings(uri, model, dim, vec, updated_at) "
            "VALUES (:uri, 'legacy-model', 1, :vec, CURRENT_TIMESTAMP)"
        ), {"uri": published, "vec": b"\x00\x00\x80?"})
        c.execute(sa.text(
            "INSERT INTO catalog_declared_keys(uri, columns) VALUES (:uri, '[\"id\"]')"
        ), {"uri": published})
        c.execute(sa.text(
            "INSERT INTO catalog_tags(uri, tag) VALUES (:uri, 'curated')"
        ), {"uri": published})
        c.execute(sa.text(
            'INSERT INTO catalog_columns(uri, "column") VALUES (:uri, \'id\')'
        ), {"uri": published})
        c.execute(sa.text(
            "INSERT INTO catalog_edges(parent, child, pipeline) "
            "VALUES (:uri, 's3://bucket/source', 'legacy-pipeline')"
        ), {"uri": published})
        c.execute(sa.text(
            "INSERT INTO catalog_relationships(rel_key, doc) VALUES ('legacy-rel', :doc)"
        ), {"doc": json.dumps({
            "leftUri": published,
            "rightUri": "s3://bucket/source",
            "leftColumns": ["id"],
            "rightColumns": ["id"],
        })})
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        states = dict(c.execute(sa.text("SELECT uri, state FROM object_attempts")).all())
        assert set(states.values()) == {"quarantined"}
        assert c.execute(sa.text(
            "SELECT count(*) FROM object_attempts WHERE quarantine_reason IS NOT NULL"
        )).scalar_one() == 5
        assert c.execute(sa.text("""
            SELECT count(*)
              FROM object_attempt_refs AS ref
              JOIN object_attempts AS attempt ON attempt.uri=ref.attempt_uri
             WHERE attempt.state != 'published'
        """)).scalar_one() == 0
        assert c.execute(sa.text("SELECT count(*) FROM object_attempt_refs")).scalar_one() == 0
        assert c.execute(sa.text(
            "SELECT count(*) FROM result_cache WHERE key='cache20'"
        )).scalar_one() == 0
        assert c.execute(sa.text(
            "SELECT count(*) FROM catalog_entries WHERE uri=:uri"
        ), {"uri": published}).scalar_one() == 0
        assert c.execute(sa.text(
            "SELECT count(*) FROM catalog_logical_datasets WHERE current_uri=:uri"
        ), {"uri": published}).scalar_one() == 0
        for table in (
            "catalog_embeddings", "catalog_declared_keys", "catalog_tags", "catalog_columns",
            "catalog_edges", "catalog_relationships",
        ):
            assert c.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        # Run history remains diagnostic-only and deliberately has no ownership reference.
        assert c.execute(sa.text(
            "SELECT output_uri FROM run_records WHERE id='record20'"
        )).scalar_one() == published
    with pytest.raises(RuntimeError, match="cannot downgrade.*managed object attempts"):
        command.downgrade(cfg, "0019_object_attempts")
    eng.dispose()


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
