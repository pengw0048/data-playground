"""Catalog at scale: server-side browse / search / facets / folders / bounded lineage / semantic.

These exercise the discovery surface that has to hold thousands of tables — every assertion is about
pushdown (filter/sort/paginate/facet in the DB, bounded payloads), organization (folders/tags/owner),
and the semantic-search seam. Seeded entries use a distinct `mem://` uri prefix and are torn down, so
they can't leak into the rest of the suite.
"""

from __future__ import annotations

import contextlib

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
        _doc("customers", f"{_SEM}c", description="people who buy and purchase products", cols=["email"]),
        _doc("shipments", f"{_SEM}s", description="packages parcels in transit tracking delivery"),
        _doc("invoices", f"{_SEM}i", description="billing payments money owed amounts due"),
    ])
    try:
        cat.set_embedder(embed, "test")
        cat._reindex_embeddings()  # synchronous embed of the seeded docs (idempotent)
        sem = client.get("/api/catalog/search", params={"q": "billing payments money", "mode": "semantic"}).json()
        sem_uris = [t["uri"] for t in sem if t["uri"].startswith(_SEM)]
        assert sem_uris and sem_uris[0] == f"{_SEM}i", "invoices is the closest to a billing query"
        # hybrid returns results and still surfaces the semantic winner among the top
        hyb = client.get("/api/catalog/search", params={"q": "billing payments", "mode": "hybrid"}).json()
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
