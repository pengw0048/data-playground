"""End-to-end kernel tests — the real out-of-core build engine on real files."""

from __future__ import annotations

import datetime
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hub.deps import get_deps
from hub.main import app
from hub.models import CatalogQuery

client = TestClient(app)


def test_observability_livez_readyz_version(monkeypatch):
    # ARC8 observability: livez (pure liveness), readyz (REAL dep checks → 200/503), version (redacted
    # deployment identity). All are pre-auth probes (app-level, outside the /api auth gate).
    from hub import db, metadb
    object_store_primes = []
    monkeypatch.setattr(
        db, "_prime_object_store_before_scope", lambda: object_store_primes.append(True))
    assert client.get("/api/livez").json()["ok"] is True
    assert client.get("/api/health").status_code == 404
    r = client.get("/api/readyz")
    body = r.json()
    assert r.status_code == 200 and body["ready"] is True
    assert body["checks"] == {"db": True, "schema": True, "engine": True}  # real checks, not static ok
    v = client.get("/api/version").json()
    assert v["auth"] in ("enabled", "open") and v["spawner"] and v["duckdb"] and v["python"]
    assert v["db"] and "://" not in v["db"], "DB reported as dialect only — no creds leaked"
    # REL-01 / issue #114: package version is part of release identity (existing fields unchanged).
    assert isinstance(v.get("version"), str) and v["version"], "package version must be present"
    from importlib.metadata import version as pkg_version
    assert v["version"] == pkg_version("data-playground")
    assert set(v) >= {"version", "sha", "spawner", "db", "storage", "auth", "python", "duckdb", "pyarrow"}
    # a BARE-PATH storage override must NOT echo the internal path to an unauthenticated caller
    monkeypatch.setenv("DP_STORAGE_URL", "/mnt/secret-internal/customer-data")
    assert client.get("/api/version").json()["storage"] == "local", "bare storage path leaked"
    monkeypatch.setenv("DP_STORAGE_URL", "s3://bucket/prefix")
    assert client.get("/api/version").json()["storage"] == "s3"  # a scheme'd url → its scheme
    monkeypatch.setattr(metadb, "schema_at_head", lambda: False)
    r = client.get("/api/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["schema"] is False
    assert r.json()["code"] == "service_unavailable" and r.json()["retryable"] is True
    monkeypatch.setattr(metadb, "ping", lambda: False)
    monkeypatch.setattr(metadb, "schema_at_head", lambda: pytest.fail("schema check followed a DB timeout"))
    r = client.get("/api/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"db": False, "schema": False, "engine": True}
    assert r.json()["detail"] == "service is not ready"
    assert object_store_primes == []


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def N(nid, t, cfg):
    return {"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"title": nid, "config": cfg}}


class _FakeSpawner:  # a plugin KernelSpawner loaded via the DP_KERNEL_SPAWNER dotted path
    name = "fake"

    def __init__(self, workspace, data_dir):
        self.args = (workspace, data_dir)

    def spawn(self, canvas_id, kernel_id, token):
        pass

    def kill(self, canvas_id, kernel_id):
        pass


class _FakeStorage:  # a plugin Storage loaded via the DP_STORAGE dotted path
    def __init__(self, workspace):
        self.ws = workspace

    def output_uri(self, name, ext):
        return f"mem://{name}{ext}"

    def list_outputs(self):
        return []


def E(s, tg, sh=None, th=None):
    return {"id": f"{s}-{tg}", "source": s, "target": tg, "sourceHandle": sh, "targetHandle": th,
            "data": {"wire": "dataset"}}


def _poll(run_id, tries=150):
    for _ in range(tries):
        st = client.get(f"/api/run/{run_id}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.1)
    return st


def _sole_output(status, *, outcome: str | None = None):
    outputs = status.get("outputs") if isinstance(status, dict) else status.outputs
    assert len(outputs) == 1, outputs
    output = outputs[0]
    actual = output.get("outcome") if isinstance(output, dict) else output.outcome
    if outcome is not None:
        assert actual == outcome
    return output


def _output_field(status, name: str, *, outcome: str | None = None):
    output = _sole_output(status, outcome=outcome)
    return output.get(name) if isinstance(output, dict) else getattr(output, name)


def _full_result(graph: dict, target_node_id: str, k: int = 100) -> tuple[dict, dict]:
    """Run an exact, durable non-write target and reopen its materialized result."""
    started = client.post(
        "/api/run",
        json={"graph": graph, "targetNodeId": target_node_id, "confirmed": True},
    )
    assert started.status_code == 200, started.text
    status = _poll(started.json()["runId"])
    assert status["status"] == "done", status.get("error")
    output = _sole_output(status, outcome="committed")
    sampled = client.post(
        f"/api/run/{status['runId']}/sample",
        json={
            "nodeId": output["nodeId"],
            "portId": output["portId"],
            "k": k,
            "offset": 0,
        },
    )
    assert sampled.status_code == 200, sampled.text
    return status, sampled.json()


def test_seeded_example_run_admits_an_exact_ordinary_local_file_binding():
    """Normal CI exercises the fresh-seed path that regressed in issue #467."""
    from hub import metadb

    canvas_id = f"seeded-exact-local-{uuid.uuid4().hex}"
    graph = {
        "id": canvas_id,
        "name": "Purchases per user exact admission",
        "version": 1,
        "nodes": [
            N("src", "source", {"uri": "events"}),
            N("flt", "filter", {"predicate": "event = 'purchase'"}),
            N("agg", "aggregate", {
                "groupBy": "user_id", "aggs": "sum(amount) AS total, count(*) AS n",
            }),
        ],
        "edges": [E("src", "flt"), E("flt", "agg")],
    }
    created = client.post("/api/canvas", json=graph)
    assert created.status_code == 200, created.text

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "agg",
        "confirmed": True,
    })
    assert started.status_code == 200, started.text
    status = _poll(started.json()["runId"])
    assert status["status"] == "done", status
    assert 1 <= status["totalRows"] <= 200

    manifest = metadb.local_run_input_manifest(started.json()["runId"])
    assert manifest is not None and len(manifest) == 1
    assert manifest[0]["node_id"] == "src"
    assert manifest[0]["provider"] == "local-file-snapshot"
    artifact = metadb.local_file_input_revision_artifact(
        manifest[0]["dataset_id"], manifest[0]["revision_id"])
    assert artifact is not None and get_deps().storage.is_managed_result_uri(artifact)


def test_kernel_info():
    info = client.get("/api/kernel").json()
    assert info["backend"] == "duckdb+polars+arrow"
    assert "duckdb" in info["adapters"] and "lance" in info["adapters"]
    assert info["runners"] == ["local-out-of-core", "local-subprocess", "kernel"]
    assert {"media", "vector"} <= set(info["capabilities"])


def test_nodes_endpoint():
    specs = {s["kind"]: s for s in client.get("/api/nodes").json()}
    assert {"source", "filter", "select", "transform", "sql", "join", "aggregate", "sort",
            "dedup", "write", "metric", "vector-search"} <= set(specs)
    assert specs["aggregate"]["previewable"] is False
    assert specs["filter"]["source"] == "builtin"
    assert specs["filter"]["params"][0]["name"] == "predicate"
    assert specs["union"]["inputs"] == [{
        "id": "in", "label": None, "wire": "dataset",
        "accepts": ["dataset", "sample"], "multi": True,
    }]


def test_catalog_and_capabilities():
    # The catalog is persistent and paginated. Query each fixture by name so unrelated local or
    # earlier-suite entries cannot push a fixture off the first page.
    tabs = {}
    for name in ("images", "movies", "events"):
        matches = client.get("/api/catalog/tables", params={"q": name}).json()["items"]
        tabs[name] = next(t for t in matches if t["name"] == name)
    caps = {c["name"]: c["capabilities"] for c in tabs["images"]["columns"]}
    assert "media" in caps["image_url"] and "vector" in caps["embedding"]
    assert tabs["images"]["rowCount"] == 500


def test_sample():
    r = client.post("/api/data/sample", json={"uri": _uri("images"), "k": 5}).json()
    assert len(r["rows"]) == 5 and r["rowCount"] == 500 and r["truncated"] is True
    provenance = r["sampleProvenance"]
    assert provenance["strategy"] == "prefix"
    assert provenance["seed"] is None and provenance["requestedRows"] == 5
    assert provenance["returnedRows"] == 5 and provenance["datasetIdentity"] == _uri("images")
    assert "not representative or random" in " ".join(provenance["limitations"])


def test_hardlinked_local_source_is_readable_across_interactive_and_full_paths(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source = tmp_path / "hardlinked-source.parquet"
    alias = tmp_path / "hardlinked-snapshot.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), source)
    os.link(source, alias)
    assert source.stat().st_nlink == alias.stat().st_nlink == 2

    graph = {
        "id": "hardlinked-local-source",
        "version": 1,
        "nodes": [N("src", "source", {"uri": str(alias)})],
        "edges": [],
    }
    preview = client.post(
        "/api/run/preview", json={"graph": graph, "nodeId": "src", "k": 10})
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [{"value": 1}, {"value": 2}, {"value": 3}]

    profile = client.post("/api/run/profile", json={"graph": graph, "nodeId": "src"})
    assert profile.status_code == 200, profile.text
    assert profile.json()["rowCount"] == 3

    schema = client.post(
        "/api/graph/schema", json={"graph": graph, "targetNodeId": "src"})
    assert schema.status_code == 200, schema.text
    assert [column["name"] for column in schema.json()["src"]["out"]] == ["value"]

    estimate = client.post(
        "/api/run/estimate", json={"graph": graph, "targetNodeId": "src"})
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["rows"] == 3

    status, result = _full_result(graph, "src", 10)
    assert status["totalRows"] == 3
    assert result["rows"] == [{"value": 1}, {"value": 2}, {"value": 3}]


def test_sort_requires_a_full_run_and_preserves_exact_order():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
        N("srt", "sort", {"by": "area DESC"}),
    ], "edges": [E("src", "flt"), E("flt", "sel"), E("sel", "srt")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "srt", "k": 10}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")
    _, result = _full_result(g, "srt", 10)
    assert "area" in [c["name"] for c in result["columns"]]
    areas = [row["area"] for row in result["rows"]]
    assert areas == sorted(areas, reverse=True)


def test_profile_returns_column_stats():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
    ], "edges": [E("src", "sel")]}
    r = client.post("/api/run/profile", json={"graph": g, "nodeId": "sel"}).json()
    assert not r["error"] and not r["notPreviewable"]
    assert r["sampled"] is True and r["rowCount"] > 0
    assert r["completeness"] == "sample"
    cols = {c["name"]: c for c in r["columns"]}
    assert "area" in cols
    area = cols["area"]
    assert area["nonNull"] + area["nulls"] == r["rowCount"]  # every row is null or not
    assert area["distinct"] is not None
    assert area["mean"] is not None                          # numeric → has a mean
    assert area["min"] is not None and area["max"] is not None


def test_full_profile_uses_cancellable_job_lifecycle():
    from hub import metadb

    canvas_id = "profile-kernel-lifecycle"
    with metadb.session() as session:
        if session.get(metadb.Canvas, canvas_id) is None:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id="local", name="Profile kernel lifecycle",
                version=1, doc="{}",
            ))
    g = {"id": canvas_id, "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
    ], "edges": [E("src", "sel")]}
    # The retired synchronous full-mode field is no longer part of the request contract.
    legacy = client.post("/api/run/profile", json={"graph": g, "nodeId": "sel", "full": True})
    assert legacy.status_code == 422
    error = legacy.json()
    assert error["code"] == "validation_error" and error["retryable"] is False
    assert any(
        item["loc"] == ["body", "full"] and item["type"] == "extra_forbidden"
        for item in error["detail"]
    )

    preflight = client.post("/api/run/profile-estimate", json={
        "graph": g, "nodeId": "sel",
    })
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["needsConfirm"] is True
    plan_digest = preflight.json()["planDigest"]
    submit = client.post("/api/run/profile-job", json={
        "graph": g, "nodeId": "sel", "planDigest": plan_digest,
        "submissionId": "00000000-0000-4000-8000-000000000014",
        "confirmed": True,
    }, headers={"X-Request-Id": "req_profile_kernel_01"})
    assert submit.status_code == 200, submit.text
    started = submit.json()
    assert started["jobType"] == "profile"
    assert started["planDigest"] == plan_digest
    assert started["requestId"] == "req_profile_kernel_01"

    deadline = time.monotonic() + 5
    status = started
    while status["status"] not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
        response = client.get(f"/api/run/{started['runId']}")
        assert response.status_code == 200, response.text
        status = response.json()
    assert status["status"] == "done", status
    assert status["profile"]["sampled"] is False
    assert status["profile"]["completeness"] == "complete"
    assert status["profile"]["rowCount"] > 0


def test_profile_over_transform_upstream_of_faithful_op_is_honest():
    # a sort over a transformed input can't be previewed on a sample → profile must refuse honestly,
    # not fabricate stats from a truncated prefix
    code = "def fn(row):\n    row['area'] = row['width'] * row['height']\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
        N("srt", "sort", {"by": "area DESC"}),
    ], "edges": [E("src", "xf"), E("xf", "srt")]}
    r = client.post("/api/run/profile", json={"graph": g, "nodeId": "srt"}).json()
    assert r["notPreviewable"] is True


def test_warm_relation_cache_reuses_and_invalidates():
    # Phase 2 step 2: the kernel's warm preview cache must (a) return identical data on a repeat
    # preview (a hit doesn't corrupt), and CRITICALLY (b) invalidate on an edit — a changed plan_hash
    # must never serve the stale cached relation (the #1 hazard of caching intermediates).
    from hub.deps import get_deps
    from hub.executors.preview import preview_node
    from hub.models import Graph
    from hub.relation_cache import RelationCache
    d = get_deps()
    cache = RelationCache()

    def prev(sel):
        gr = Graph(**{"id": "cvW", "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            N("sel", "select", {"select": sel}),
        ], "edges": [E("src", "sel")]})
        return preview_node(gr, "sel", 10, d.resolve_adapter, d.registry, d.node_builders, d.node_specs, cache=cache)

    r1 = prev("user_id, amount")
    r2 = prev("user_id, amount")                     # same plan → cache HIT
    assert not r1.error and not r2.error
    assert [c.name for c in r1.columns] == ["user_id", "amount"]
    assert r1.rows == r2.rows                        # a hit returns the identical materialized data
    r3 = prev("user_id, amount, amount * 2 AS dbl")  # edited select → new plan_hash → must NOT be stale
    assert [c.name for c in r3.columns] == ["user_id", "amount", "dbl"]
    assert all(row["dbl"] == row["amount"] * 2 for row in r3.rows)


def test_warm_relation_cache_hit_crosses_run_scopes_without_rerunning_scan():
    import pyarrow as pa

    from hub import db
    from hub.executors.preview import preview_node
    from hub.models import Graph
    from hub.relation_cache import RelationCache

    calls = {"preview": 0}

    class Adapter:
        def fingerprint(self, _uri):
            return "stable"

        def preview_scan(self, _uri, **_kwargs):
            calls["preview"] += 1
            return db.conn().from_arrow(pa.table({"value": [1, 2, 3]}))

        def scan(self, _uri, **_kwargs):
            raise AssertionError("interactive cache fill must use the bounded preview capability")

    adapter = Adapter()
    graph = Graph(**{"id": "warm-cross-scope", "version": 1, "nodes": [
        N("src", "source", {"uri": "mock://warm-cache"}),
        N("sel", "select", {"select": "value, value * 2 AS doubled"}),
    ], "edges": [E("src", "sel")]})
    cache = RelationCache()

    first = preview_node(graph, "sel", 10, lambda _uri: adapter, object(), cache=cache)
    second = preview_node(graph, "sel", 10, lambda _uri: adapter, object(), cache=cache)

    assert not first.error and not second.error
    assert first.rows == second.rows
    assert calls["preview"] == 1, "the second run_scope must hit Arrow cache, not rebuild the source"


def test_relation_cache_no_deadlock_and_eviction_safe():
    # regressions: (a) put() of an already-cached key must NOT self-deadlock (it re-entered a non-reentrant
    # lock via get()); (b) a relation handed out by get()/put() must survive a concurrent LRU eviction of
    # its backing table — it is materialized into an independent Arrow-backed relation, not a lazy view.
    from hub import db
    from hub.relation_cache import RelationCache
    c = RelationCache(cap_rows=100, max_entries=2)  # tiny LRU → easy to force eviction
    with db.lock():
        con = db.conn()
        con.execute("CREATE OR REPLACE VIEW _rc_test_src AS SELECT * FROM range(5) t(v)")
        assert c.put("kA", "_rc_test_src") is not None
        assert c.put("kA", "_rc_test_src") is not None   # repeat same key — must NOT deadlock
        held = c.get("kA")                                # a hit → an independent relation
        assert held is not None and held.aggregate("count(*) AS n").fetchone()[0] == 5
        con.execute("CREATE OR REPLACE VIEW _rc_test_src2 AS SELECT * FROM range(3) t(v)")
        c.put("kB", "_rc_test_src2"); c.put("kC", "_rc_test_src2")  # evicts kA (max_entries=2) + drops its table
        assert held.aggregate("count(*) AS n").fetchone()[0] == 5   # still scannable — arrow-backed


def test_aggregate_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("agg", "aggregate", {"groupBy": "format", "aggs": "count(*) AS n"}),
    ], "edges": [E("src", "agg")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "agg", "k": 10}).json()
    assert r["notPreviewable"] is True and "full pass" in r["reason"]


def test_full_run_of_a_non_write_target_materializes_an_inspectable_result():
    # P0-UX-01: a full pass over a non-write target (an aggregate the sample refuses to preview) must
    # produce a DURABLE, inspectable result artifact — not just a row count — and it must survive a
    # restart (the uri persists in run_states, readable by any instance).
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("agg", "aggregate", {"groupBy": "event", "aggs": "count(*) AS n"}),
    ], "edges": [E("src", "agg")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "agg", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    uri = _output_field(st, "uri", outcome="committed")
    assert uri, "a non-write target's full result must be materialized to a durable artifact"
    # the artifact holds the EXACT grouped rows (the aggregate a sample can't preview), inspectable via
    # the normal sample-by-uri API — the same way the UI pages a Full result
    out = client.post("/api/data/sample", json={"uri": uri, "k": 100}).json()
    assert {c["name"] for c in out["columns"]} == {"event", "n"}
    assert out["rowCount"] == st["totalRows"] and out["rowCount"] > 0
    # durability: re-fetching the run status (served from run_states, i.e. after a restart / on another
    # instance) still carries the artifact uri, so the Full result is restorable
    assert _output_field(
        client.get(f"/api/run/{st['runId']}").json(), "uri", outcome="committed") == uri
    # but the ephemeral result artifact must NOT be re-cataloged into the Tables view on restart
    from hub.deps import get_deps
    tables = get_deps().catalog.list_page(CatalogQuery(limit=5000)).items
    assert not any("__result_" in t.uri for t in tables)
    assert not any(t.uri == uri for t in tables)


def test_transform_arrow_batches():
    code = "def fn(row):\n    row['area'] = row['width'] * row['height']\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
    ], "edges": [E("src", "xf")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "xf", "k": 20}).json()
    assert not r["notPreviewable"]
    assert all(row["area"] == row["width"] * row["height"] for row in r["rows"])


def test_join_and_sql():
    # A row-preserving SQL node is bounded-previewable on its own, but an upstream join requires a full
    # pass; the exact durable result remains inspectable.
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("events")}),
        N("j", "join", {"on": "user_id", "how": "inner"}),
        N("q", "sql", {"sql": "SELECT user_id, amount FROM input WHERE amount > 0"}),
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b"), E("j", "q")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 5}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")
    _, result = _full_result(g, "q", 5)
    assert result["rows"]


def test_union_stacks_inputs_row_wise():
    # union stacks its N inputs vertically. UNION ALL keeps every row (2 identical inputs → 2x); UNION
    # (distinct) dedups back to one copy. A lone input passes straight through.
    src = [N("a", "source", {"uri": _uri("events")}), N("b", "source", {"uri": _uri("events")})]
    edges = [E("a", "u"), E("b", "u")]
    n1 = _poll(client.post("/api/run", json={"graph": {"id": "c", "version": 1, "nodes": [src[0]], "edges": []},
                                             "targetNodeId": "a", "confirmed": True}).json()["runId"])["totalRows"]
    g_all = {"id": "c", "version": 1, "nodes": [*src, N("u", "union", {"mode": "all", "align": "name"})], "edges": edges}
    allrows = _poll(client.post("/api/run", json={"graph": g_all, "targetNodeId": "u", "confirmed": True}).json()["runId"])["totalRows"]
    assert allrows == 2 * n1
    g_dist = {"id": "c", "version": 1, "nodes": [*src, N("u", "union", {"mode": "distinct", "align": "name"})], "edges": edges}
    dist = _poll(client.post("/api/run", json={"graph": g_dist, "targetNodeId": "u", "confirmed": True}).json()["runId"])["totalRows"]
    assert dist == n1  # identical inputs dedup back to one copy
    g_one = {"id": "c", "version": 1, "nodes": [src[0], N("u", "union", {"mode": "all"})], "edges": [E("a", "u")]}
    one = _poll(client.post("/api/run", json={"graph": g_one, "targetNodeId": "u", "confirmed": True}).json()["runId"])["totalRows"]
    assert one == n1  # a single input just passes through


def test_union_by_name_aligns_differing_column_order():
    # BY NAME (the default) aligns columns by name across inputs, filling a missing one with NULL — so
    # a same-schema dataset in a different column order (or with an extra column) stacks correctly.
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("sa", "select", {"select": "user_id, event"}),      # (user_id, event)
        N("b", "source", {"uri": _uri("events")}),
        N("sb", "select", {"select": "event, user_id"}),      # same cols, reversed order
        N("u", "union", {"mode": "all", "align": "name"}),
    ], "edges": [E("a", "sa"), E("sa", "u"), E("b", "sb"), E("sb", "u")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "u", "k": 5}).json()
    assert not r["notPreviewable"]
    assert set(r["rows"][0].keys()) == {"user_id", "event"}  # aligned by name, not smashed by position


def test_union_is_relational_not_clean_ir():
    # union is a multi-input relational op → outside the map-style clean subset, so a distributed
    # map-engine (dp_ray) falls back to DuckDB for any graph containing it.
    from hub.ir import lower_to_ir
    from hub.models import Graph
    g = Graph(id="c", version=1, nodes=[
        N("a", "source", {"uri": _uri("events")}), N("b", "source", {"uri": _uri("events")}),
        N("u", "union", {"mode": "all"})], edges=[E("a", "u"), E("b", "u")])
    ir = lower_to_ir(g, "u")
    assert "union" in ir.unsupported() and not ir.is_clean()
    u = ir.by_id()["u"]
    assert u.op == "union" and u.config == {"mode": "all", "align": "name"} and len(u.inputs) == 2


def test_ir_shuffle_key_parser_and_distributable_gate():
    # a distributed backend shuffles on a KEY then lets DuckDB compute per partition, so the ONLY thing
    # parsed is the shuffle key (bare columns) — the aggregate expression itself is never parsed. A key
    # that isn't plain columns → None → the backend falls back to single-node DuckDB.
    from hub.ir import (DISTRIBUTABLE_RELATIONAL, lower_to_ir, parse_group_keys,
                        plan_is_clean, plan_is_distributable)
    from hub.compiler import compile_plan
    from hub.deps import get_deps
    from hub.models import Graph
    assert parse_group_keys("cat") == ["cat"] and parse_group_keys("a, b") == ["a", "b"]
    assert parse_group_keys("") == []                                          # global aggregate → no key
    assert parse_group_keys("lower(x)") is None and parse_group_keys("x*2") is None  # expression → DuckDB
    from hub.ir import parse_sort_keys
    assert parse_sort_keys("a, b DESC") == [("a", False), ("b", True)]        # bare cols + per-key direction
    assert parse_sort_keys("lower(a)") is None and parse_sort_keys("") is None  # expression / empty → DuckDB
    assert DISTRIBUTABLE_RELATIONAL == frozenset({"aggregate", "window", "dedup", "join", "sort"})

    d = get_deps()
    gg = Graph(**{"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("a", "aggregate", {"groupBy": "event", "aggs": "count(*) AS n"}),
        N("w", "write", {"name": "o"})], "edges": [E("s", "a"), E("a", "w")]})
    plan = compile_plan(gg, "w", d.registry, d.node_specs, d.node_ir)
    assert not plan_is_clean(plan)                                              # aggregate is not "clean"
    assert plan_is_distributable(plan, frozenset({"aggregate"}))               # but a shuffle backend claims it
    assert lower_to_ir(gg, "w", d.node_specs).is_distributable(frozenset({"aggregate"}))


def test_sql_groupby_preview_refuses_the_sample():
    # a sql GROUP BY / global aggregate over the 2000-row sample would present a PARTIAL aggregate as
    # complete (the honesty hole the acceptance found). It must refuse the sample like the aggregate node.
    from hub.executors.engine import sql_reduces_rows
    assert sql_reduces_rows("SELECT user_id, count(*) FROM input GROUP BY user_id")
    assert sql_reduces_rows("SELECT count(*) AS n FROM input")               # global aggregate
    assert sql_reduces_rows("SELECT DISTINCT user_id FROM input")
    assert sql_reduces_rows("SELECT any_value(event) FROM input")            # non-canonical reducing aggs
    assert sql_reduces_rows("SELECT max_by(event, amount) FROM input")
    # a windowed aggregate in a CTE/subquery must NOT cancel a genuine outer aggregate (per-aggregate OVER)
    assert sql_reduces_rows("WITH r AS (SELECT *, row_number() OVER (ORDER BY amount) rn FROM input) SELECT count(*) FROM r")
    assert not sql_reduces_rows("SELECT * FROM input WHERE amount > 0")      # row-preserving
    assert not sql_reduces_rows("SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY amount) r FROM input")  # pure window
    assert not sql_reduces_rows("SELECT amount AS max FROM input")           # 'max' as an alias, not agg()

    g = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("q", "sql", {"sql": "SELECT event, count(*) AS n FROM input GROUP BY event"}),
    ], "edges": [E("s", "q")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 50}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")   # honest, not a partial lie
    # the SAME query is correct on a full run (not-previewable is a preview stance, not a run block)
    done = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "q", "confirmed": True}).json()["runId"])
    assert done["status"] == "done"
    # a plain row-preserving sql still previews fine
    g2 = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("q", "sql", {"sql": "SELECT * FROM input WHERE event = 'purchase'"}),
    ], "edges": [E("s", "q")]}
    r2 = client.post("/api/run/preview", json={"graph": g2, "nodeId": "q", "k": 5}).json()
    assert not r2["notPreviewable"] and not r2.get("error")


def test_sql_join_and_window_require_a_full_pass(tmp_path):
    # JOIN / window / global ordering in SQL cannot be exact over bounded prefixes. Interactive preview
    # refuses them instead of silently rebuilding an unbounded relation; the durable run stays exact.
    import duckdb
    from hub.executors.engine import sql_needs_full_input
    assert sql_needs_full_input("SELECT * FROM input a JOIN input2 b USING(id)")
    assert sql_needs_full_input("SELECT *, row_number() OVER (ORDER BY x) FROM input")
    assert sql_needs_full_input("SELECT * FROM input QUALIFY row_number() OVER (ORDER BY x) = 1")
    assert not sql_needs_full_input("SELECT * FROM input WHERE x > 0")
    # cross-input shapes that read a SECOND input CTE also lie on truncated prefixes (acceptance #1/#2):
    assert sql_needs_full_input("SELECT * FROM input INTERSECT SELECT * FROM input2")   # set operator
    assert sql_needs_full_input("SELECT * FROM input EXCEPT SELECT * FROM input2")
    assert sql_needs_full_input("SELECT input.id FROM input, input2 WHERE input.id = input2.id")  # comma-join
    assert sql_needs_full_input("SELECT * FROM input WHERE id IN (SELECT id FROM input2)")  # subquery-join
    # the named-window form `OVER w … WINDOW w AS (…)` must be caught like the paren form (acceptance #3):
    assert sql_needs_full_input("SELECT id, rank() OVER w AS r FROM input WINDOW w AS (ORDER BY id DESC)")
    assert sql_needs_full_input("SELECT id, sum(x) OVER w FROM input WINDOW w AS (ORDER BY id)")
    assert not sql_needs_full_input("SELECT overflow FROM input WHERE overflow > 0")  # no false-positive on 'over…'
    # AST-parser wins the cases a regex misses (PR #35 review): a QUOTED window name, a self-join, a
    # self-subquery-join — all caught; a mere COLUMN named `input2` is NOT a table ref → not flagged.
    assert sql_needs_full_input('SELECT rank() OVER "w" AS r FROM input WINDOW "w" AS (ORDER BY id DESC)')
    assert sql_needs_full_input("SELECT * FROM input a, input b WHERE a.id = b.id + 1000")   # self comma-join
    assert sql_needs_full_input("SELECT * FROM input WHERE id IN (SELECT id - 1000 FROM input)")  # self subquery
    assert not sql_needs_full_input("SELECT input2 FROM input")          # input2 here is a COLUMN, not a table
    assert not sql_needs_full_input("WITH t AS (SELECT * FROM input) SELECT * FROM t")  # single-input CTE, faithful
    # a statement-level ORDER BY / top-N is sorted within the prefix → unfaithful on a sample (like the
    # sort node); a windowed / intra-aggregate ORDER BY is NOT a statement sort so it must NOT trip this:
    assert sql_needs_full_input("SELECT id FROM input ORDER BY score DESC LIMIT 3")   # top-N
    assert sql_needs_full_input("SELECT * FROM input ORDER BY x")                      # sorted preview
    assert not sql_needs_full_input("SELECT * FROM input LIMIT 10")                    # a bare prefix IS faithful
    assert not sql_needs_full_input("SELECT list(x ORDER BY x) FROM input GROUP BY k") # agg ORDER BY, not a sort
    left, right = str(tmp_path / "l.parquet"), str(tmp_path / "r.parquet")
    # matching keys live only past the 2000-row preview window of the left input
    duckdb.connect().execute(f"COPY (SELECT i AS id FROM range(0,3000) t(i)) TO '{left}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT i AS id FROM range(2500,5500) t(i)) TO '{right}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("r", "source", {"uri": right}),
        N("q", "sql", {"sql": "SELECT a.id FROM input a JOIN input2 b USING (id)"}),
    ], "edges": [E("l", "q"), E("r", "q")]}
    res = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 50}).json()
    assert res["notPreviewable"] and "full pass" in (res["reason"] or "")
    _, result = _full_result(g, "q", 50)
    assert result["rowCount"] > 0, "the durable SQL join must see matching rows beyond preview prefixes"


def test_sql_query_scope_cte_and_aggregate_message_reflects_groupby():
    # the SQL node exposes its source as the real query-scope CTE name `input`
    g = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("q", "sql", {"sql": "SELECT event, amount FROM input WHERE amount > 0"}),
    ], "edges": [E("s", "q")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 5}).json()
    assert not r["notPreviewable"] and not r.get("error"), r.get("reason")
    # the aggregate not-previewable reason is conditional on groupBy (was hardcoded 'global aggregate')
    gg = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("a", "aggregate", {"groupBy": "event", "aggs": "count(*) AS n"}),
    ], "edges": [E("s", "a")]}
    ra = client.post("/api/run/preview", json={"graph": gg, "nodeId": "a", "k": 5}).json()
    assert ra["notPreviewable"] and "grouped" in (ra["reason"] or "")
    g0 = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("a", "aggregate", {"aggs": "count(*) AS n"}),
    ], "edges": [E("s", "a")]}
    r0 = client.post("/api/run/preview", json={"graph": g0, "nodeId": "a", "k": 5}).json()
    assert r0["notPreviewable"] and "global" in (r0["reason"] or "")


def test_tight_memory_limit_caps_threads(monkeypatch, tmp_path):
    # at a tight memory_limit the default (all-core) thread count OOMs the order-preserving write even
    # though the query pipeline spills; _apply_session lowers threads to keep memory-per-thread sane and
    # never raises RAM above the operator's cap. (The 20M-row OOM itself is verified out-of-band.)
    import duckdb

    from hub import db
    from hub.db import _parse_bytes
    assert _parse_bytes("300MB") == 300_000_000 and _parse_bytes("2GiB") == 2 * 2 ** 30
    assert _parse_bytes("512") == 512 and _parse_bytes("nonsense") is None
    monkeypatch.setenv("DP_SPILL_DIR", str(tmp_path / "spill"))
    cores = int(duckdb.connect().execute("SELECT current_setting('threads')").fetchone()[0])

    monkeypatch.setenv("DP_MEMORY_LIMIT", "300MB")
    monkeypatch.delenv("DP_MIN_MEM_PER_THREAD_MB", raising=False)  # default 96MiB floor → ~2 threads
    ct = duckdb.connect(); db._apply_session(ct)
    t = int(ct.execute("SELECT current_setting('threads')").fetchone()[0])
    assert 1 <= t <= 2 and t <= cores                       # capped, never above the machine's cores

    monkeypatch.delenv("DP_MEMORY_LIMIT", raising=False)     # no limit → threads untouched
    cn = duckdb.connect(); db._apply_session(cn)
    assert int(cn.execute("SELECT current_setting('threads')").fetchone()[0]) == cores


def test_join_and_sort_preview_refuse_instead_of_scanning_full_inputs(tmp_path):
    # Truncated-prefix join/sort results lie, but constructing full inputs inside preview is unbounded.
    # Preview refuses both; durable runs still produce the exact matches and global order.
    import duckdb
    left, right = str(tmp_path / "left.parquet"), str(tmp_path / "right.parquet")
    # matching keys live only in rows past the 2000-row preview window of at least one side
    duckdb.connect().execute(f"COPY (SELECT i AS id, i*10 AS lval FROM range(0,3000) t(i)) TO '{left}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT i AS id FROM range(2500,5500) t(i)) TO '{right}' (FORMAT PARQUET)")
    gj = {"id": "c", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("r", "source", {"uri": right}),
        N("j", "join", {"on": "id", "how": "inner"}),
    ], "edges": [E("l", "j", None, "a"), E("r", "j", None, "b")]}
    rj = client.post("/api/run/preview", json={"graph": gj, "nodeId": "j", "k": 50}).json()
    assert rj["notPreviewable"] and "full pass" in (rj["reason"] or "")
    _, joined = _full_result(gj, "j", 50)
    assert joined["rowCount"] > 0
    gs = {"id": "c2", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("s", "sort", {"by": "lval DESC"}),
    ], "edges": [E("l", "s")]}
    rs = client.post("/api/run/preview", json={"graph": gs, "nodeId": "s", "k": 5}).json()
    assert rs["notPreviewable"] and "full pass" in (rs["reason"] or "")
    _, sorted_result = _full_result(gs, "s", 5)
    assert sorted_result["rows"][0]["lval"] == 29990


def test_join_on_expression_with_differing_keys(tmp_path):
    # join used to emit only USING(cols), requiring identical key names; an ON expression now supports
    # differently-named keys (a.id = b.uid) and renames right-side column clashes so downstream isn't
    # ambiguous.
    import duckdb
    left, right = str(tmp_path / "l.parquet"), str(tmp_path / "r.parquet")
    duckdb.connect().execute(f"COPY (SELECT i AS id, 'L'||i AS name FROM range(0,5) t(i)) TO '{left}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT i AS uid, 'R'||i AS name FROM range(2,7) t(i)) TO '{right}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("r", "source", {"uri": right}),
        N("j", "join", {"how": "inner", "condition": "a.id = b.uid"}),
    ], "edges": [E("l", "j", None, "a"), E("r", "j", None, "b")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 50}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")
    _, result = _full_result(g, "j", 50)
    cols = [c["name"] for c in result["columns"]]
    assert cols == ["id", "name", "uid", "name_2"]  # right-side 'name' renamed → no ambiguity
    assert result["rowCount"] == 3  # ids 2,3,4 overlap


def test_dedup_and_metric():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("sel", "select", {"select": "event"}),
        N("dd", "dedup", {}),
    ], "edges": [E("src", "sel"), E("sel", "dd")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "dd", "k": 50}).json()
    assert len(r["rows"]) == 4  # view/click/purchase/signup


def test_run_write_and_lineage():
    from hub import metadb

    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("wr", "write", {"name": "images_valid", "format": "parquet"}),
    ], "edges": [E("src", "flt"), E("flt", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    st = _poll(r["runId"])
    assert st["status"] == "done"
    assert _output_field(st, "table", outcome="committed") == "images_valid"
    assert st["totalRows"] and st["totalRows"] < 500  # filtered out invalids
    lin = client.get("/api/catalog/lineage", params={"uri": _uri("images")}).json()
    assert _output_field(st, "uri", outcome="committed") in {
        edge["child"] for edge in lin["edges"]}
    destination = _output_field(st, "uri", outcome="committed")
    with metadb.session() as session:
        facts = list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == destination,
            metadb.CatalogLineageFact.run_id == r["runId"],
        )))
    assert len(facts) == 1
    assert facts[0].source_uri == _uri("images")
    assert facts[0].attempt_id is None
    assert (facts[0].producer, facts[0].producer_version, facts[0].step_id) == ("c", 1, "wr")
    assert st["progress"] == 1.0  # a finished run reports full progress


def test_run_progress_and_stall_signal():
    # a finished run reports progress=1.0; the stall hint fires for a running run whose last step
    # completed longer ago than the (here, zero) threshold, and clears for a fresh one.
    from hub import metadb
    from hub.plugins.runner import _step_progress
    from hub.models import PerNodeStatus, RunStatus
    # _step_progress is a pure fraction of finished steps
    st = RunStatus(run_id="p", status="running", placement="local", per_node=[
        PerNodeStatus(node_id="a", status="done"), PerNodeStatus(node_id="b", status="running"),
        PerNodeStatus(node_id="c", status="queued")])
    assert _step_progress(st) == 1 / 3
    # end-to-end: a real run's status carries a 0..1 progress that reaches 1.0 at done
    g = {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": _uri("events")}),
         N("f", "filter", {"predicate": "amount > 0"})], "edges": [E("s", "f")]}
    done = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "f", "confirmed": True}).json()["runId"])
    assert done["status"] == "done" and done["progress"] == 1.0
    # the stall hint: a running run whose run_state hasn't updated within the threshold is flagged
    metadb.save_run_state("stall_run", RunStatus(run_id="stall_run", status="running", placement="local").model_dump())
    assert metadb.run_stalled("stall_run", 0.0) is True     # threshold 0 → any age counts as stalled
    assert metadb.run_stalled("stall_run", 10_000) is False  # generous threshold → not stalled
    assert metadb.run_stalled("no_such_run", 0.0) is False   # unknown run → never stalled
    # wedge watchdog probe: a healthy engine answers a trivial query fast; a HELD base lock (a wedge)
    # makes the probe hang past its budget → reported unresponsive, which the kernel uses to self-recycle.
    from hub import db
    import threading as _th
    assert db.responsive(5.0) is True                        # healthy engine → responsive
    got: list = []
    with db.lock():                                          # hold the base lock (simulates a wedge)
        w = _th.Thread(target=lambda: got.append(db.responsive(1.0)))
        w.start(); w.join(6)
    assert got == [False]                                    # couldn't complete while wedged → recycle signal
    # the terminal status carries a duration too (ms is set BEFORE the flip to 'done', not only in the
    # finally) so a poll that reads 'done' isn't left with ms=0.
    assert done["ms"] >= 0 and "ms" in done


def test_backend_job_liveness_uses_db_clock_and_flags_queued_runs(monkeypatch):
    from hub import metadb

    def utc(value):
        return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)

    with metadb.session() as session:
        database_now = metadb._db_now(session)
    monkeypatch.setattr(
        metadb, "_now", lambda: utc(database_now) - datetime.timedelta(days=365)
    )
    run_ids = ["db_clock_stall_queued", "db_clock_stall_running"]
    try:
        for run_id, status in zip(run_ids, ("queued", "running"), strict=True):
            ref = {
                "backend": "missing-liveness-test", "cluster_ref": "test-cluster",
                "attempt_id": f"attempt-{run_id}", "submission_id": f"submission-{run_id}",
                "job_uri": f"s3://test-control/{run_id}.dpjob",
                "result_uri": f"s3://test-control/{run_id}.dpresult",
            }
            metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
            metadb.bind_backend_job(run_id, ref, {
                "run_id": run_id, "status": status, "placement": "distributed", "per_node": [],
            })
            with metadb.session() as session:
                job = session.get(metadb.RunBackendJob, run_id)
                current_database_time = metadb._db_now(session)
                observed_age = (
                    utc(job.last_control_observed_at) - utc(current_database_time)
                ).total_seconds()
                assert abs(observed_age) < 2
                job.last_control_observed_at = current_database_time - datetime.timedelta(minutes=5)

            assert metadb.run_stalled(run_id, 60) is True
            response = client.get(f"/api/run/{run_id}")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == status
            assert response.json()["stalled"] is True

            assert metadb.note_backend_control_observed(run_id, ref["attempt_id"], 0) is True
            assert metadb.run_stalled(run_id, 60) is False
    finally:
        with metadb.session() as session:
            for run_id in run_ids:
                job = session.get(metadb.RunBackendJob, run_id)
                state = session.get(metadb.RunState, run_id)
                if job is not None:
                    session.delete(job)
                if state is not None:
                    session.delete(state)


def test_backend_job_reads_isolate_malformed_result_rows():
    from hub import metadb

    run_ids = [f"malformed_backend_result_{index}" for index in range(2)]
    try:
        for index, run_id in enumerate(run_ids):
            metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
            metadb.bind_backend_job(run_id, {
                "backend": "result-row-isolation-test", "cluster_ref": "test-cluster",
                "attempt_id": f"attempt-{run_id}", "submission_id": f"submission-{run_id}",
                "job_uri": f"s3://test-control/{run_id}.dpjob",
                "result_uri": f"s3://test-control/{run_id}.dpresult",
            }, {
                "run_id": run_id, "status": "running", "placement": "distributed", "per_node": [],
            })
            with metadb.session() as session:
                job = session.get(metadb.RunBackendJob, run_id)
                if index == 0:
                    job.result_doc = "{"

        malformed = metadb.backend_job(run_ids[0])
        assert malformed["result"] is None
        assert "result_doc" in malformed["_recovery_error"]

        active = {
            ref["run_id"]: ref
            for ref, _status in metadb.active_backend_jobs("result-row-isolation-test")
        }
        assert set(active) == set(run_ids)
        assert "result_doc" in active[run_ids[0]]["_recovery_error"]
        assert active[run_ids[1]]["result"] is None
        assert "_recovery_error" not in active[run_ids[1]]
    finally:
        with metadb.session() as session:
            for run_id in run_ids:
                job = session.get(metadb.RunBackendJob, run_id)
                state = session.get(metadb.RunState, run_id)
                if job is not None:
                    session.delete(job)
                if state is not None:
                    session.delete(state)


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_run_status_survives_detail_deletion(terminal):
    from hub import metadb

    run_id = f"terminal_fence_query_{terminal}"
    try:
        metadb.save_run_state(run_id, {
            "run_id": run_id, "status": terminal, "placement": "distributed", "per_node": [],
        })
        assert metadb.terminal_run_status(run_id) == terminal

        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            if state is not None:
                session.delete(state)

        assert metadb.get_run_state(run_id) is None
        assert metadb.terminal_run_status(run_id) == terminal
    finally:
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_sqlite_metadb_uses_wal():
    # the bundled default is SQLite under concurrent daemon-thread writes + polling; WAL + busy_timeout
    # keep those from raising SQLITE_BUSY. Assert the connect hook actually took (sqlite deployments only).
    from hub import metadb
    from sqlalchemy import text
    eng = metadb.engine()
    if not str(eng.url).startswith("sqlite"):
        pytest.skip("metadb is not SQLite in this deployment")
    with eng.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert int(c.execute(text("PRAGMA busy_timeout")).scalar()) >= 1000


def test_run_controller_evicts_terminal_runs_only():
    # RunController.self.runs grew unbounded (the in-process runners cap theirs); _evict now bounds it,
    # dropping oldest TERMINAL runs while never evicting an in-flight one.
    from hub.run_controller import RunController
    from hub.plugins.runner import _MAX_RUNS
    from hub.models import RunStatus
    rc = RunController(None, None, None)
    with rc._lock:
        rc.runs["live"] = RunStatus(
            run_id="live", status="running", placement="distributed")
        rc._published_statuses["live"] = rc.runs["live"].model_copy(deep=True)
        for i in range(_MAX_RUNS + 5):
            run_id = f"r{i}"
            rc.runs[run_id] = RunStatus(
                run_id=run_id, status="done", placement="distributed")
            rc._published_statuses[run_id] = rc.runs[run_id].model_copy(deep=True)
        rc._evict()
    assert len(rc.runs) <= _MAX_RUNS
    assert "live" in rc.runs  # the in-flight run is never dropped, even though it's the oldest key


def test_write_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("wr", "write", {"name": "x"}),
    ], "edges": [E("src", "wr")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "wr", "k": 10}).json()
    assert r["notPreviewable"] is True


def test_register_endpoint():
    uri = _uri("movies")
    t = client.post("/api/catalog/register", json={"uri": uri, "name": "movies_again"}).json()
    assert t["name"] == "movies_again" and t["rowCount"] == 200


def _upload(filename: str, body: bytes):
    # raw-body upload: the file bytes ARE the body; the name rides in the X-Upload-Filename header
    return client.post("/api/catalog/upload", content=body, headers={"X-Upload-Filename": filename})


def test_upload_registers_and_is_readable():
    r = _upload("cities.csv", b"id,city\n1,paris\n2,rome\n3,paris\n")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["name"] == "cities"
    assert {c["name"] for c in t["columns"]} == {"id", "city"} and t["rowCount"] == 3
    # visible in the (cross-instance) catalog and sampleable via its uri
    assert "cities" in {x["name"] for x in client.get(
        "/api/catalog/tables", params={"q": "cities"}
    ).json()["items"]}
    s = client.post("/api/data/sample", json={"uri": t["uri"], "k": 10}).json()
    assert len(s["rows"]) == 3


def test_upload_rejects_unsupported_type():
    assert _upload("notes.txt", b"hello").status_code == 400


def test_upload_rejects_oversized(monkeypatch):
    from hub.settings import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 8)  # tiny cap → the 20-byte body is aborted mid-stream
    assert _upload("big.csv", b"a\n" + b"1\n" * 9).status_code == 413


def test_request_body_and_graph_complexity_limits(monkeypatch):
    # SEC-10: every non-upload body is byte-capped; graphs are node/edge-capped; per-node code/SQL is
    # length-capped — all before the handler runs. Upload stays exempt (it streams + self-caps).
    from hub.models import MAX_CODE_LEN, MAX_GRAPH_NODES
    from hub.settings import settings
    # (1) too many nodes → 422 at validation
    over = {"id": "c", "version": 1,
            "nodes": [N(f"n{i}", "source", {}) for i in range(MAX_GRAPH_NODES + 1)], "edges": []}
    assert client.post("/api/graph/compile", json={"graph": over}).status_code == 422
    # (2) oversized code on a node → 422 (body itself well under the byte cap)
    big = {"id": "c", "version": 1, "nodes": [N("t", "transform", {"code": "x" * (MAX_CODE_LEN + 1)})], "edges": []}
    assert client.post("/api/graph/compile", json={"graph": big}).status_code == 422
    # (3) a body over the byte cap → 413 from the middleware
    monkeypatch.setattr(settings, "max_body_bytes", 200)
    payload = {"graph": {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": "x" * 500})], "edges": []}}
    assert client.post("/api/graph/compile", json=payload).status_code == 413
    # a small body still passes with the tiny cap in place (sanity: the cap isn't rejecting everything)
    monkeypatch.setattr(settings, "max_body_bytes", 64 * 1024**2)
    assert client.post("/api/graph/compile", json={"graph": {"id": "c", "version": 1, "nodes": [], "edges": []}}).status_code == 200


def test_graph_and_lineage_wire_identities_use_lossless_integer_bounds():
    from hub.models import (
        MAX_SAFE_INTEGER, Graph, GraphNode, LineageFact, LineagePublication,
    )

    Graph(
        id="g" * 512, version=MAX_SAFE_INTEGER,
        nodes=[GraphNode(id="n" * 256, type="source")],
    )
    for invalid in (True, "1", 1.0, -1, MAX_SAFE_INTEGER + 1):
        with pytest.raises(ValueError):
            Graph(id="graph", version=invalid)
    for invalid in ("", " graph", "graph ", "g" * 513):
        with pytest.raises(ValueError):
            Graph(id=invalid)
    for invalid in ("", " node", "node ", "n" * 257):
        with pytest.raises(ValueError):
            GraphNode(id=invalid, type="source")

    publication = {
        "idempotency_key": "publication", "run_id": "run", "producer": "canvas",
        "step_id": "write", "provenance": "run",
    }
    assert LineagePublication(
        **publication, producer_version=MAX_SAFE_INTEGER,
    ).producer_version == MAX_SAFE_INTEGER
    for invalid in (True, "1", 1.0, -1, MAX_SAFE_INTEGER + 1):
        with pytest.raises(ValueError):
            LineagePublication(**publication, producer_version=invalid)
    for field in ("idempotency_key", "run_id", "producer", "step_id"):
        with pytest.raises(ValueError):
            LineagePublication(**{**publication, field: f" {publication[field]}"})

    fact = {
        "id": "1", "fact_key": "fact", "publication_key": "publication",
        "source_key": "source", "source_uri": "/data/source.parquet",
        "destination_key": "destination", "destination_uri": "/data/output.parquet",
        "run_id": "run", "producer": "canvas", "step_id": "write",
        "provenance": "run",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    assert LineageFact(
        **fact, producer_version=MAX_SAFE_INTEGER,
    ).producer_version == MAX_SAFE_INTEGER
    for invalid in (True, "1", 1.0, -1, MAX_SAFE_INTEGER + 1):
        with pytest.raises(ValueError):
            LineageFact(**fact, producer_version=invalid)
    for field in ("fact_key", "source_key", "source_uri", "destination_uri", "run_id"):
        with pytest.raises(ValueError):
            LineageFact(**{**fact, field: f" {fact[field]}"})
    for field in ("run_id", "attempt_id", "producer", "step_id"):
        with pytest.raises(ValueError):
            LineageFact(**{**fact, field: ""})
    with pytest.raises(ValueError, match="include a timezone"):
        LineageFact(**{
            **fact, "created_at": datetime.datetime.now().replace(tzinfo=None),
        })
    with pytest.raises(ValueError, match="requires run, producer, and step"):
        LineageFact(**{**fact, "run_id": None})
    with pytest.raises(ValueError):
        LineageFact(**{
            **fact,
            "field_mappings": [
                {"source_field": f"source-{index}", "destination_field": "destination"}
                for index in range(257)
            ],
        })


def test_lineage_fact_page_cursor_and_size_contract_are_bounded():
    from hub.models import LineageFact, LineageFactsPage

    max_cursor = str(2**63 - 1)
    fact = LineageFact(
        id=max_cursor, fact_key="fact", publication_key="publication",
        source_key="source", source_uri="/data/source.parquet",
        destination_key="destination", destination_uri="/data/output.parquet",
        provenance="manual", created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    page = LineageFactsPage(items=[fact], next_after_id=max_cursor, has_more=True)
    assert page.next_after_id == max_cursor and page.has_more is True

    for invalid in ("0", "01", "-1", str(2**63), 1):
        with pytest.raises(ValueError):
            LineageFactsPage(next_after_id=invalid, has_more=True)
    with pytest.raises(ValueError, match="continuation state"):
        LineageFactsPage(has_more=True)
    with pytest.raises(ValueError, match="continuation state"):
        LineageFactsPage(next_after_id="1", has_more=False)
    assert len(LineageFactsPage(items=[fact] * 500).items) == 500
    with pytest.raises(ValueError):
        LineageFactsPage(items=[fact] * 501)


def test_run_output_catalog_version_is_exact_before_cache_admission():
    from hub.models import RunOutput, RunStatus
    from hub.run_outputs import outputs_cache_document

    fields = {
        "node_id": "write", "port_id": "out", "wire": "dataset",
        "publication_kind": "catalog", "outcome": "committed",
        "uri": "/data/output.parquet", "table": "output", "rows": 3,
    }
    exact = RunOutput(**fields, version="v-exact")
    status = RunStatus(
        run_id="run-exact", status="done", target_node_id="write", outputs=[exact],
    )
    assert outputs_cache_document(status) == {"outputs": [exact.model_dump()]}

    missing = RunOutput(**fields)
    with pytest.raises(RuntimeError, match="exact catalog versions"):
        outputs_cache_document(RunStatus(
            run_id="run-missing-version", status="done", target_node_id="write",
            outputs=[missing],
        ))
    for invalid in ("", " v1", "v1 ", "v" * 513):
        with pytest.raises(ValueError):
            RunOutput(**fields, version=invalid)
    with pytest.raises(ValueError, match="non-catalog"):
        RunOutput(**{
            **fields, "publication_kind": "result", "table": None, "version": "v1",
        })
    with pytest.raises(ValueError, match="non-committed"):
        RunOutput(**{
            **fields, "outcome": "pending", "uri": None, "table": None,
            "version": "v1", "rows": None,
        })


def test_upload_same_name_does_not_clobber():
    a = _upload("dup.csv", b"a\n1\n").json()
    b = _upload("dup.csv", b"a\n2\n").json()
    assert a["uri"] != b["uri"]  # a short suffix keeps the two stored files distinct


def test_upload_strips_control_chars_from_name():
    t = _upload("a\x01b\x7fc.csv", b"x\n1\n").json()  # control chars flow into the table id + UI
    assert t["name"] == "abc"


def test_map_column_type_is_distinct_from_struct():
    from hub.plugins.adapters import display_type
    assert display_type("MAP(VARCHAR, BIGINT)") == "map"   # was folded into 'struct' → UI showed [N]
    assert display_type("STRUCT(a INTEGER)") == "struct"


def test_sandbox_set_allowed_replaces_not_grows():
    from hub import sandbox
    sandbox.allow_modules({"pandas"})
    sandbox.set_allowed({"numpy"})       # replace, not grow
    assert "numpy" in sandbox._KERNEL_ALLOWED and "pandas" not in sandbox._KERNEL_ALLOWED
    sandbox.set_allowed(set())           # emptied requirements → allow nothing
    assert not sandbox._KERNEL_ALLOWED


def test_preview_k_defaults_to_setting_when_omitted(monkeypatch):
    # regression: PreviewRequest.k was typed int=50, so req.k was never None and the `else preview_k`
    # fallback (DP_PREVIEW_K) was dead. k is now optional → an omitted k uses settings.preview_k.
    from hub.settings import settings
    monkeypatch.setattr(settings, "preview_k", 3)
    g = {"id": "c", "version": 1, "nodes": [N("src", "source", {"uri": _uri("events")})], "edges": []}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "src"})  # no k → falls back to preview_k
    assert r.status_code == 200 and len(r.json()["rows"]) <= 3


# --------------------------------------------------------------------------- #
# Acceptance coverage backfill — deterministic paths the 3rd acceptance found untested
# --------------------------------------------------------------------------- #
def _age_kernel_heartbeat(canvas_id: str):
    import datetime
    from hub import metadb
    from hub.metadb import Kernel, session
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=metadb.KERNEL_STALE_S + 5)
    with session() as s:
        s.get(Kernel, canvas_id).heartbeat_at = old  # backdate past the stale threshold


def test_stale_kernel_is_reaped_and_its_run_failed():
    # the 'owning kernel truly dead' path (aged heartbeat): get_kernel reports stale, reap_kernels deletes
    # the lease (and returns the pair), reap_orphaned_runs then fails that lease's in-flight run.
    from hub import metadb
    metadb.claim_kernel("cv_stale", "k_stale", "tok"); metadb.mark_kernel_ready("cv_stale", "k_stale", "1.2.3.4:9")
    metadb.save_run_state("run_stale", {"run_id": "run_stale", "status": "running", "per_node": []},
                          canvas_id="cv_stale", kernel_id="k_stale")
    _age_kernel_heartbeat("cv_stale")
    assert metadb.get_kernel("cv_stale")["stale"] is True
    assert ("cv_stale", "k_stale") in metadb.reap_kernels()      # stale lease deleted
    assert metadb.get_kernel("cv_stale") is None
    metadb.reap_orphaned_runs()
    assert metadb.get_run_state("run_stale")["status"] == "failed"
    with metadb.session() as session:
        assert session.get(metadb.RunTerminalFence, "run_stale").status == "failed"


def test_claim_kernel_takes_over_a_stale_lease():
    # the takeover branch (won=True on a stale lease): a new claimer wins + fences the old kernel_id.
    from hub import metadb
    metadb.claim_kernel("cv_takeover", "k_old", "tokold")
    _age_kernel_heartbeat("cv_takeover")
    r = metadb.claim_kernel("cv_takeover", "k_new", "toknew")
    assert r["won"] is True and r["kernel_id"] == "k_new"
    assert metadb.heartbeat_kernel("cv_takeover", "k_old") is False  # the replaced id is fenced out
    assert metadb.heartbeat_kernel("cv_takeover", "k_new") is True
    metadb.drop_kernel("cv_takeover", "k_new")


def test_kernel_liveness_counts_in_process_preview_profile():
    # acceptance #5: the idle-TTL watchdog must treat the kernel as BUSY while an in-process preview or
    # profile runs — those don't appear in run_runner.runs (only offloaded /run does), so without the
    # in-flight term a full-dataset profile longer than idle-ttl would recycle its own warm kernel mid-run.
    from hub.kernel import _liveness_busy

    class _R:
        def __init__(self, s): self.status = s
    assert _liveness_busy(0, {}) is False                       # nothing in flight, no runs → idle
    assert _liveness_busy(1, {}) is True                        # an in-process preview/profile → BUSY
    assert _liveness_busy(3, {}) is True                        # several concurrent previews → BUSY
    assert _liveness_busy(0, {"r": _R("running")}) is True      # an offloaded run still counts
    assert _liveness_busy(0, {"r": _R("queued")}) is True
    assert _liveness_busy(0, {"a": _R("done"), "b": _R("failed")}) is False  # only terminal runs → idle
    assert _liveness_busy(1, {"a": _R("done")}) is True         # in-flight work wins over a finished run


def test_dp_execution_is_the_third_precedence_tier(tmp_path, monkeypatch):
    # precedence: per-user > workspace > DP_EXECUTION > kernel default. With no user/global setting,
    # DP_EXECUTION (settings.execution) is honored — the tier only incidentally covered before.
    from hub.deps import Deps
    from hub.settings import settings
    monkeypatch.setattr(settings, "execution", "local-out-of-core")
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    assert d.chosen_backend(uid=None) == "local-out-of-core"
    monkeypatch.setattr(settings, "execution", "")  # cleared → the kernel default
    assert d.chosen_backend(uid=None) == "kernel"


def test_relation_cache_drops_over_cap_and_never_retries():
    # the OOM guard: a relation over cap_rows is materialized with LIMIT cap+1, detected as too-big,
    # dropped (not cached), and remembered so it isn't re-materialized on the next put.
    from hub import db
    from hub.relation_cache import RelationCache
    c = RelationCache(cap_rows=3, max_entries=8)
    with db.lock():
        db.conn().execute("CREATE OR REPLACE VIEW _rc_big AS SELECT * FROM range(5) t(v)")  # 5 > cap 3
        assert c.put("big", "_rc_big") is None    # over cap → not cached
        assert c.get("big") is None               # a miss
        assert "big" in c._toobig                 # remembered → won't retry
        assert c.put("big", "_rc_big") is None


def test_kernel_deps_ensure_idempotent_and_records_failure(tmp_path, monkeypatch):
    # kernel_deps had ZERO direct coverage. Monkeypatch pip (no network): ensure skips a re-install of the
    # same set, re-installs a changed set, and on failure caches the attempt (no re-run) + logs to stderr.
    import subprocess
    import sys
    import hub.kernel_deps as kd
    calls = []

    def fake_ok(cmd, **kw):
        calls.append(cmd)
        ti = cmd.index("--target"); os.makedirs(os.path.join(cmd[ti + 1], "pandas"), exist_ok=True)
        return object()
    monkeypatch.setattr(kd.subprocess, "run", fake_ok)
    kd._installed.clear()
    tgt = str(tmp_path / "deps")
    assert "pandas" in kd.ensure(["pandas"], tgt) and len(calls) == 1
    kd.ensure(["pandas"], tgt); assert len(calls) == 1              # same set → skipped (idempotent)
    kd.ensure(["pandas", "numpy"], tgt); assert len(calls) == 2      # changed set → re-installed
    assert tgt == sys.path[0]                                        # deps dir inserted at front

    def fake_fail(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd, stderr=b"No matching distribution")
    monkeypatch.setattr(kd.subprocess, "run", fake_fail)
    tgt2 = str(tmp_path / "deps2")
    kd.ensure(["nope"], tgt2); n = len(calls)
    kd.ensure(["nope"], tgt2)                                        # failed set cached → no re-run
    assert len(calls) == n


def test_kernel_deps_helpers(tmp_path):
    import hub.kernel_deps as kd
    a = kd.deps_dir("/ws", "canvasA"); b = kd.deps_dir("/ws", "canvasB")
    assert a != b and a.startswith("/ws")                           # per-canvas isolation, stable hash
    assert kd.deps_dir("/ws", "canvasA") == a                       # deterministic
    from pathlib import Path
    t = Path(tmp_path)
    for n in ("pandas", "_internal", "junk.dist-info"):
        (t / n).mkdir()
    (t / "solo.py").touch(); (t / "notes.txt").touch()
    mods = kd._top_level_modules(t)
    assert mods == {"pandas", "solo"}                               # dirs + .py; skips _*, .dist-info, .txt


def test_upload_edge_cases():
    assert _upload("empty.csv", b"").status_code == 400             # empty upload
    assert _upload("bad.parquet", b"not a parquet at all").status_code == 400  # valid ext, corrupt bytes
    trav = _upload("../../../etc/passwd.csv", b"id\n1\n").json()    # path traversal in the filename
    assert trav["name"] == "passwd" and "/etc/" not in trav["uri"] and ".." not in trav["uri"]
    tsv = _upload("t.tsv", b"a\tb\n1\t2\n").json()                  # TSV auto-detect (no options passed)
    assert {c["name"] for c in tsv["columns"]} == {"a", "b"} and tsv["rowCount"] == 1
    nd = _upload("n.ndjson", b'{"a":1}\n{"a":2}\n').json()          # NDJSON local round-trip
    assert nd["rowCount"] == 2 and "a" in {c["name"] for c in nd["columns"]}


def test_profile_handles_nested_type_column():
    # profile.py's nested-type branch returns count-only for list/struct/map (no min/max/mean/distinct);
    # only scalar columns were exercised. A list column (images.embedding) must profile without crashing.
    from hub.deps import get_deps
    from hub.executors.profile import profile_node
    from hub.models import Graph
    d = get_deps()
    g = Graph(**{"id": "cP", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("sel", "select", {"select": "id, embedding"}),
    ], "edges": [E("src", "sel")]})
    r = profile_node(g, "sel", d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    assert not r.error
    assert "embedding" in [c.name for c in r.columns]  # nested column present, count-only, no crash


def test_full_profile_covers_the_whole_dataset_not_the_sample(tmp_path):
    # a FULL profile is a real full pass: exact count/nulls/min/max/mean over EVERY row and an HLL
    # distinct — where the sampled profile only sees the bounded preview prefix. Build a dataset whose
    # true min/max live PAST the 2000-row sample so the two profiles must disagree.
    import duckdb
    from hub.deps import get_deps
    from hub.executors.profile import profile_node
    from hub.models import Graph
    p = str(tmp_path / "big.parquet")
    # v = 0..9999 (true max 9999, all past the sample); id has 10000 distinct; 1000 nulls in w
    duckdb.connect().execute(
        f"COPY (SELECT i AS v, i AS id, CASE WHEN i < 1000 THEN NULL ELSE i END AS w "
        f"FROM range(0,10000) t(i)) TO '{p}' (FORMAT PARQUET)")
    d = get_deps()
    g = Graph(**{"id": "cF", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []})

    sm = profile_node(g, "s", d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    assert sm.sampled and sm.row_count <= 2000                 # sampled: bounded prefix
    assert sm.completeness == "sample"

    fl = profile_node(g, "s", d.resolve_adapter, d.registry, d.node_builders, d.node_specs, full=True)
    assert not fl.sampled and fl.row_count == 10000            # full: every row
    assert fl.completeness == "complete"
    byname = {c.name: c for c in fl.columns}
    assert byname["v"].min == "0" and byname["v"].max == "9999"        # true extents, past the sample
    assert abs((byname["v"].mean or 0) - 4999.5) < 1e-6               # exact mean over all rows
    assert byname["w"].nulls == 1000                                  # exact null count
    assert 7500 <= (byname["id"].distinct or 0) <= 12500             # HLL distinct ≈ 10000 (whole set, not ~2000)
    assert byname["id"].distinct_is_approximate is True


def test_code_cell_preview_profile_disabled_in_auth_mode(monkeypatch):
    # P0-EXEC-02: in multi-user (auth) mode, previewing/profiling a node whose cone runs an arbitrary
    # Python cell is refused — the thread-based timeout can't kill a runaway cell. Open mode is unchanged.
    from hub.deps import get_deps
    from hub.executors.preview import preview_node
    from hub.executors.profile import profile_node
    from hub.models import Graph
    d = get_deps()
    g = Graph(**{"id": "cx", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"mode": "map", "code": "def fn(row):\n    return row"}),
    ], "edges": [E("s", "xf")]})
    # open mode: a trivial cell previews fine (behavior unchanged)
    ok = preview_node(g, "xf", 5, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    assert not ok.not_previewable and not ok.error
    # auth mode: refused with an honest reason, for BOTH preview and profile
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)
    pv = preview_node(g, "xf", 5, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    assert pv.not_previewable and "multi-user" in (pv.reason or "")
    pf = profile_node(g, "xf", d.resolve_adapter, d.registry, d.node_builders, d.node_specs, full=True)
    assert pf.not_previewable and "multi-user" in (pf.reason or "")


def test_full_profile_has_a_deadline_and_does_not_pin_the_kernel(monkeypatch, tmp_path):
    # P0-EXEC-02: a full profile must be deadline-bounded + interruptible, so a huge pure-SQL aggregate
    # can't pin the warm kernel forever. A 62.5B-row hashed cross join over the wired input cannot finish in
    # 0.5s → it is interrupted, without weakening the user-SQL policy to allow range() table functions.
    import time as _t

    from hub.deps import get_deps
    from hub.executors import profile as profile_mod
    from hub.executors.profile import profile_node
    from hub.models import Graph
    monkeypatch.setattr(profile_mod, "PROFILE_FULL_BUDGET_S", 0.5)
    d = get_deps()
    source = str(tmp_path / "profile-deadline.parquet")
    import duckdb
    duckdb.connect().execute(
        f"COPY (SELECT i AS id FROM range(500) t(i)) TO '{source}' (FORMAT PARQUET)"
    )
    g = Graph(**{"id": "cD", "version": 1, "nodes": [
        N("s", "source", {"uri": source}),
        N("q", "sql", {"sql": (
            "SELECT hash(a.id, b.id, c.id, d.id) AS id "
            "FROM input a, input b, input c, input d"
        )}),
    ], "edges": [E("s", "q")]})
    t0 = _t.time()
    r = profile_node(g, "q", d.resolve_adapter, d.registry, d.node_builders, d.node_specs, full=True)
    assert _t.time() - t0 < 20  # interrupted, not pinned for the whole scan
    assert r.error and "budget" in (r.reason or "").lower()


# --------------------------------------------------------------------------- #
# Regression tests for adversarial-acceptance findings
# --------------------------------------------------------------------------- #
def test_aggregate_keeps_group_key():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("agg", "aggregate", {"groupBy": "event", "aggs": "count(*) AS n"}),
        N("wr", "write", {"name": "agg_out"}),
    ], "edges": [E("src", "agg"), E("agg", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_agg_out").uri, "k": 10}).json()
    assert "event" in [c["name"] for c in out["columns"]]  # group key retained
    assert "n" in [c["name"] for c in out["columns"]]


def test_metric_requires_a_full_run_for_its_true_value(tmp_path):
    import duckdb
    p = str(tmp_path / "big5k.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 AS v FROM range(0,5000)) TO '{p}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [E("src", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")
    _, result = _full_result(g, "m", 5)
    assert result["rows"][0]["value"] == 5000.0


def test_join_duplicate_columns_preserved():
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("images")}),
        N("j", "join", {"how": "inner"}),  # no key → cross join; both have 'id'
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 3}).json()
    assert r["notPreviewable"]
    _, result = _full_result(g, "j", 3)
    names = [c["name"] for c in result["columns"]]
    assert "id" in names and "id_2" in names           # both id columns kept, de-duped
    assert all(len(row) == len(names) for row in result["rows"])  # no column dropped


def test_join_using_dedups_nonkey_clashes_and_survives_full_run():
    # a USING join coalesces the KEY once, but two tables sharing a NON-key column (both events have
    # amount/event/id besides user_id) used to emit ambiguous duplicate names — fine in preview, then a
    # DuckDB ambiguity error on the full run. The projection now renames the right-side non-key clashes.
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("events")}),
        N("j", "join", {"on": "user_id", "how": "inner"}),
        N("s", "select", {"select": "user_id, amount, amount_2"}),  # downstream ref that would break on a dup
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b"), E("j", "s")]}
    preview = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 3}).json()
    assert preview["notPreviewable"]
    _, joined = _full_result(g, "j", 3)
    names = [c["name"] for c in joined["columns"]]
    assert "user_id" in names and names.count("user_id") == 1   # key coalesced once
    assert "amount" in names and "amount_2" in names            # non-key clash de-duped, not ambiguous
    # the FULL run (where the ambiguity used to surface) completes, and the downstream select resolves
    done = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "s", "confirmed": True}).json()["runId"])
    assert done["status"] == "done", done.get("error")


def test_full_using_join_coalesces_key_for_right_only_rows(tmp_path):
    # a USING join must COALESCE the key: for a RIGHT/FULL join a right-only row's key is the right value,
    # not NULL. The projection emits the key unqualified (not a.key) so right-only rows keep their key.
    import duckdb
    left, right = str(tmp_path / "l.parquet"), str(tmp_path / "r.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1,'a'),(2,'b')) t(id,lval)) TO '{left}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (2,'x'),(3,'y')) t(id,rval)) TO '{right}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("r", "source", {"uri": right}),
        N("j", "join", {"on": "id", "how": "full"}),
    ], "edges": [E("l", "j", None, "a"), E("r", "j", None, "b")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 50}).json()
    assert r["notPreviewable"]
    _, result = _full_result(g, "j", 50)
    ids = sorted(row["id"] for row in result["rows"] if row.get("id") is not None)
    assert ids == [1, 2, 3], f"right-only key (3) must be coalesced, not NULL — got {ids}"


def test_cancel_finished_run_stays_done():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("f", "filter", {"predicate": "amount > 1"}),
    ], "edges": [E("src", "f")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "f", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    after = client.post(f"/api/run/{r['runId']}/cancel").json()
    assert after["status"] == "done"  # a finished run is never relabeled cancelled


def test_typecheck_rejects_incompatible_connection():
    # metric outputs `metric`; aggregate's input accepts only `dataset` → must be rejected server-side
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("m", "metric", {"agg": "count"}),
        N("agg", "aggregate", {"aggs": "count(*) AS n"}),
    ], "edges": [E("src", "m"), {"id": "bad", "source": "m", "target": "agg", "data": {"wire": "metric"}}]}
    assert client.post("/api/run", json={"graph": g, "targetNodeId": "agg", "confirmed": True}).status_code == 400


def test_metric_edge_wire_does_not_422():
    # models.WireType must include 'metric'/'value' so an edge tagged with them parses (not a 422);
    # the tag is cosmetic (type-checking recomputes from specs), so source→metric stays valid.
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [{"id": "sm", "source": "src", "target": "m", "sourceHandle": None,
                  "targetHandle": None, "data": {"wire": "metric"}}]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5})
    assert r.status_code == 200  # not 422


def test_decimal_serialized_as_number():
    r = client.post("/api/data/sample", json={"uri": _uri("events"), "k": 3}).json()
    amounts = [row["amount"] for row in r["rows"]]
    assert all(isinstance(a, (int, float)) for a in amounts)  # small decimals stay numeric


def test_sample_node_preview_is_deterministic_and_provenance_bearing(tmp_path):
    # The explicit Sample node is the one preview that scans a complete local input. Its evidence must
    # distinguish a seeded reservoir sample from a bounded prefix, including a revision-bound identity.
    import duckdb
    p = str(tmp_path / "big.parquet")
    duckdb.connect().execute(f"COPY (SELECT i AS id FROM range(0,10000) t(i)) TO '{p}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": p}),
        N("sm", "sample", {"n": 200, "seed": 1}),
    ], "edges": [E("s", "sm")]}
    first = client.post("/api/run/preview", json={"graph": g, "nodeId": "sm", "k": 200}).json()
    again = client.post("/api/run/preview", json={"graph": g, "nodeId": "sm", "k": 200}).json()
    ids = [row["id"] for row in first["rows"]]
    assert max(ids) >= 2000, f"reservoir sample must reach past the first 2000 rows — got max {max(ids)}"
    assert first["rows"] == again["rows"]
    provenance = first["sampleProvenance"]
    assert provenance["strategy"] == "reservoir"
    assert provenance["seed"] == 1 and provenance["requestedRows"] == 200
    assert provenance["datasetIdentity"] == p and provenance["datasetRevision"]
    assert provenance["scannedRows"] == provenance["totalRows"] == 10_000
    assert first["rowLimit"] is None and "prefix" not in " ".join(provenance["limitations"]).lower()

    g["nodes"][1]["data"]["config"]["seed"] = 2
    changed_seed = client.post("/api/run/preview", json={"graph": g, "nodeId": "sm", "k": 200}).json()
    assert changed_seed["sampleProvenance"]["identity"] != provenance["identity"]

    import os
    os.remove(p)
    duckdb.connect().execute(f"COPY (SELECT i AS id FROM range(10000,20000) t(i)) TO '{p}' (FORMAT PARQUET)")
    changed_revision = client.post("/api/run/preview", json={"graph": g, "nodeId": "sm", "k": 200}).json()
    assert changed_revision["sampleProvenance"]["identity"] != changed_seed["sampleProvenance"]["identity"]

    filtered = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": p}),
        N("f", "filter", {"predicate": "id >= 15000"}),
        N("sm", "sample", {"n": 200, "seed": 2}),
    ], "edges": [E("s", "f"), E("f", "sm")]}
    first_filter = client.post(
        "/api/run/preview", json={"graph": filtered, "nodeId": "sm", "k": 200},
    ).json()
    filtered["nodes"][1]["data"]["config"]["predicate"] = "id >= 19000"
    changed_filter = client.post(
        "/api/run/preview", json={"graph": filtered, "nodeId": "sm", "k": 200},
    ).json()
    assert changed_filter["sampleProvenance"]["identity"] != first_filter["sampleProvenance"]["identity"]

    bypassed = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": p}),
        N("sm", "sample", {"n": 200, "seed": 2}),
    ], "edges": [E("s", "sm")]}
    bypassed["nodes"][1]["data"]["bypassed"] = True
    bypassed_result = client.post(
        "/api/run/preview", json={"graph": bypassed, "nodeId": "sm", "k": 200},
    ).json()
    assert bypassed_result["sampleProvenance"]["strategy"] == "prefix"
    assert bypassed_result["rowLimit"] == 2_000


def test_profiles_keep_sample_provenance_without_marking_full_sources_sampled(tmp_path):
    import duckdb
    from hub.deps import get_deps
    from hub.executors.profile import _reservoir_profile_allowed, profile_node
    from hub.models import Graph

    path = str(tmp_path / "profile-sample.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT i AS id FROM range(0,5000) t(i)) TO '{path}' (FORMAT PARQUET)")
    deps = get_deps()
    sampled_graph = Graph(**{"id": "profile-sample", "version": 1, "nodes": [
        N("source", "source", {"uri": path}),
        N("sample", "sample", {"n": 120, "seed": 9}),
        N("filter", "filter", {"predicate": "id >= 0"}),
    ], "edges": [E("source", "sample"), E("sample", "filter")]})

    sampled = profile_node(sampled_graph, "sample", deps.resolve_adapter, deps.registry,
                           deps.node_builders, deps.node_specs)
    assert sampled.sampled and sampled.sample_provenance is not None
    assert sampled.sample_provenance.strategy == "reservoir"
    assert sampled.sample_provenance.seed == 9
    assert sampled.sample_provenance.total_rows == 5000

    downstream = profile_node(sampled_graph, "filter", deps.resolve_adapter, deps.registry,
                              deps.node_builders, deps.node_specs)
    assert downstream.sampled and downstream.sample_provenance is not None
    assert downstream.sample_provenance.strategy == "reservoir"
    assert downstream.sample_provenance.seed == 9
    assert downstream.sample_provenance.total_rows == 5000

    side_branch = Graph(**{"id": "profile-sample-side-branch", "version": 1, "nodes": [
        *sampled_graph.nodes,
        N("side", "source", {"uri": path}),
        N("join", "join", {"on": "id", "how": "inner"}),
    ], "edges": [
        *sampled_graph.edges,
        E("filter", "join", None, "a"),
        E("side", "join", None, "b"),
    ]})
    assert not _reservoir_profile_allowed(side_branch, "join", deps.resolve_adapter)

    full_sampled = profile_node(sampled_graph, "sample", deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, full=True)
    assert not full_sampled.sampled and full_sampled.sample_provenance is not None
    assert full_sampled.sample_provenance.strategy == "reservoir"

    source_graph = Graph(**{"id": "profile-source", "version": 1, "nodes": [
        N("source", "source", {"uri": path}),
    ], "edges": []})
    full_source = profile_node(source_graph, "source", deps.resolve_adapter, deps.registry,
                               deps.node_builders, deps.node_specs, full=True)
    assert not full_source.sampled and full_source.sample_provenance is None


def test_sample_seed_and_size_survive_canvas_save_reload():
    canvas_id = "sample-provenance-save-reload"
    graph = {"id": canvas_id, "name": "sample provenance", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("sm", "sample", {"n": 125, "seed": 73}),
    ], "edges": [E("s", "sm")]}
    saved = client.put(f"/api/canvas/{canvas_id}", json=graph)
    assert saved.status_code == 200 and saved.json()["ok"]
    restored = client.get(f"/api/canvas/{canvas_id}")
    assert restored.status_code == 200
    assert next(node for node in restored.json()["nodes"] if node["id"] == "sm")["data"]["config"] == {
        "n": 125, "seed": 73,
    }


def test_ident_escapes_embedded_quotes_not_strips(tmp_path):
    # _ident used to STRIP embedded quotes, so a real column named e.g. `a"b` silently addressed a
    # different/nonexistent column; it must DOUBLE them (SQL escaping) so the right column resolves.
    from hub.executors.engine import _ident
    assert _ident('a"b') == 'a""b' and _ident("normal") == "normal"
    # functional: a metric over a column whose real name contains a quote resolves to THAT column (the
    # old strip would have addressed a different/nonexistent column or thrown a parser error).
    import duckdb
    p = str(tmp_path / "q.parquet")
    duckdb.connect().execute('''COPY (SELECT 5 AS "wei""rd") TO '%s' (FORMAT PARQUET)''' % p)
    g = {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": p}),
        N("m", "metric", {"agg": "max", "column": 'wei"rd'}),
    ], "edges": [E("s", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert r["notPreviewable"]
    _, result = _full_result(g, "m", 5)
    assert result["rows"][0]["value"] == 5


def test_high_precision_decimal_previews_exactly():
    # a DECIMAL whose value exceeds float64's ~15 exact digits must preview as the EXACT value the run
    # writes, not a rounded float — otherwise preview disagrees with the written parquet.
    import decimal

    import pyarrow as pa
    from hub.executors.engine import _table_to_rows
    tbl = pa.table({
        "big": pa.array([decimal.Decimal("12345678901234567.123456789")], type=pa.decimal128(38, 9)),
        "price": pa.array([decimal.Decimal("9.99")], type=pa.decimal128(6, 2)),
    })
    row = _table_to_rows(tbl)[0]
    assert row["big"] == "12345678901234567.123456789"        # exact string, not a rounded float
    assert isinstance(row["price"], float) and row["price"] == 9.99  # small decimal stays numeric


def test_plugin_run_applies_lowering(tmp_path):
    # the critical bug: plugin lowerings were dropped on a full run → untransformed writes
    from hub.sdk import NodeSpec, PortSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="const42", title="const42", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[])
    deps.node_specs[spec.kind] = spec
    deps.node_builders[spec.kind] = lambda engine, node, inputs: ctx.sql(inputs[0], "SELECT *, 42 AS c FROM input")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("p", "const42", {}),
        N("wr", "write", {"name": "plugin_out"}),
    ], "edges": [E("src", "p"), E("p", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_plugin_out").uri, "k": 3}).json()
    assert "c" in [c["name"] for c in out["columns"]]           # plugin build was applied
    assert all(row["c"] == 42 for row in out["rows"])           # transformed, not passthrough


def test_plugin_multi_input_descriptor_round_trips_and_executes_in_edge_order(tmp_path):
    """A plugin-declared multi port stays multi through API registration, persistence, and execution."""
    import duckdb
    from hub import db
    from hub.sdk import NodeSpec, PortSpec

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT 'first' AS source, 1 AS ordinal) TO '{first}' (FORMAT PARQUET)")
    duckdb.connect().execute(
        f"COPY (SELECT 'second' AS source, 2 AS ordinal) TO '{second}' (FORMAT PARQUET)")

    deps = get_deps()
    kind = "plugin_multi_input_contract"
    spec = NodeSpec(
        kind=kind, title="plugin multi input", category="compute",
        inputs=[PortSpec(id="items", label="Items", wire="dataset", multi=True)],
        outputs=[PortSpec(id="out", wire="dataset")], params=[],
    )

    def build(engine, _node, inputs):
        views = [engine._view(rel, f"plugin_multi_{index}") for index, rel in enumerate(inputs)]
        return db.conn().sql(" UNION ALL ".join(f"SELECT * FROM {view}" for view in views))

    prior_spec = deps.node_specs.get(kind)
    prior_builder = deps.node_builders.get(kind)
    deps.node_specs[kind] = spec
    deps.node_builders[kind] = build
    canvas_id = "plugin-multi-input-contract"
    graph = {"id": canvas_id, "name": "plugin multi input", "version": 1, "nodes": [
        N("first", "source", {"uri": str(first)}),
        N("second", "source", {"uri": str(second)}),
        N("combine", kind, {}),
    ], "edges": [E("first", "combine", th="items"), E("second", "combine", th="items")]}
    try:
        descriptor = next(item for item in client.get("/api/nodes").json() if item["kind"] == kind)
        assert descriptor["inputs"][0]["multi"] is True

        saved = client.put(f"/api/canvas/{canvas_id}", json=graph)
        assert saved.status_code == 200 and saved.json()["ok"]
        restored = client.get(f"/api/canvas/{canvas_id}")
        assert restored.status_code == 200
        assert restored.json()["edges"] == graph["edges"]

        result = client.post("/api/run/preview", json={"graph": restored.json(), "nodeId": "combine", "k": 10})
        assert result.status_code == 200, result.text
        assert result.json()["rows"] == [
            {"source": "first", "ordinal": 1},
            {"source": "second", "ordinal": 2},
        ]
    finally:
        client.delete(f"/api/canvas/{canvas_id}")
        if prior_spec is None:
            deps.node_specs.pop(kind, None)
        else:
            deps.node_specs[kind] = prior_spec
        if prior_builder is None:
            deps.node_builders.pop(kind, None)
        else:
            deps.node_builders[kind] = prior_builder


def test_plugin_column_parameter_preserves_ordered_values_end_to_end():
    """A typed plugin field list survives descriptor, canvas persistence, and lowering unchanged."""
    from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx, identifier, quote_identifier

    deps = get_deps()
    kind = "plugin_structured_columns_contract"
    spec = NodeSpec(
        kind=kind, title="plugin selected columns", category="compute",
        inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
        params=[ParamSpec(name="columns", type="columns", label="Selected columns")],
    )
    received: list[list[str]] = []

    def build(_engine, node, inputs):
        selected = node.data["config"]["columns"]
        received.append(list(selected))
        if not selected:
            return inputs[0]
        fields = ", ".join(quote_identifier(identifier(column, inputs[0].columns, label="plugin column"))
                           for column in selected)
        return ctx.sql(inputs[0], f"SELECT {fields} FROM input")

    prior_spec = deps.node_specs.get(kind)
    prior_builder = deps.node_builders.get(kind)
    deps.node_specs[kind] = spec
    deps.node_builders[kind] = build
    canvas_id = "plugin-structured-columns-contract"
    graph = {"id": canvas_id, "name": "plugin selected columns", "version": 1, "nodes": [
        N("source", "source", {"uri": _uri("events")} ),
        N("plugin", kind, {"columns": []}),
    ], "edges": [E("source", "plugin")]}
    try:
        descriptor = next(item for item in client.get("/api/nodes").json() if item["kind"] == kind)
        assert descriptor["params"] == [{"name": "columns", "type": "columns", "default": None,
                                         "options": None, "label": "Selected columns", "lang": None,
                                         "required": False, "showWhen": None}]

        for selected in ([], ["event"], ["amount", "event"]):
            graph["nodes"][1]["data"]["config"] = {"columns": selected}
            assert client.put(f"/api/canvas/{canvas_id}", json=graph).status_code == 200
            restored = client.get(f"/api/canvas/{canvas_id}")
            assert restored.status_code == 200
            assert restored.json()["nodes"][1]["data"]["config"]["columns"] == selected
            preview = client.post("/api/run/preview", json={"graph": restored.json(), "nodeId": "plugin", "k": 2})
            assert preview.status_code == 200, preview.text
            assert received[-1] == selected

        graph["nodes"][1]["data"]["config"] = {"columns": "amount,event"}
        rejected = client.post("/api/run/preview", json={"graph": graph, "nodeId": "plugin", "k": 2})
        assert rejected.status_code == 400
        assert "must be an ordered list of column names" in rejected.json()["detail"]
    finally:
        client.delete(f"/api/canvas/{canvas_id}")
        if prior_spec is None:
            deps.node_specs.pop(kind, None)
        else:
            deps.node_specs[kind] = prior_spec
        if prior_builder is None:
            deps.node_builders.pop(kind, None)
        else:
            deps.node_builders[kind] = prior_builder


def test_plugin_numeric_parameters_round_trip_and_reject_invalid_json_before_execution():
    """Typed plugin numbers survive persistence unchanged and invalid shapes never reach build()."""
    from hub.sdk import NodeSpec, ParamSpec, PortSpec

    deps = get_deps()
    kind = "plugin_numeric_contract"
    spec = NodeSpec(
        kind=kind, title="plugin numeric", category="compute",
        inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
        params=[ParamSpec(name="count", type="int", required=True),
                ParamSpec(name="ratio", type="float", default=0.5)],
    )
    received: list[tuple[int, float]] = []

    def build(_engine, node, inputs):
        config = node.data["config"]
        received.append((config["count"], config["ratio"]))
        return inputs[0]

    prior_spec = deps.node_specs.get(kind)
    prior_builder = deps.node_builders.get(kind)
    deps.node_specs[kind] = spec
    deps.node_builders[kind] = build
    canvas_id = "plugin-numeric-contract"
    graph = {"id": canvas_id, "name": "plugin numeric", "version": 1, "nodes": [
        N("source", "source", {"uri": _uri("events")}),
        N("plugin", kind, {"count": 0, "ratio": 0.0}),
    ], "edges": [E("source", "plugin")]}
    try:
        for count, ratio in ((0, 0.0), (-7, 1.25e2)):
            graph["nodes"][1]["data"]["config"] = {"count": count, "ratio": ratio}
            assert client.put(f"/api/canvas/{canvas_id}", json=graph).status_code == 200
            restored = client.get(f"/api/canvas/{canvas_id}")
            assert restored.status_code == 200
            config = restored.json()["nodes"][1]["data"]["config"]
            assert config == {"count": count, "ratio": ratio}
            preview = client.post(
                "/api/run/preview", json={"graph": restored.json(), "nodeId": "plugin", "k": 2})
            assert preview.status_code == 200, preview.text
            assert received[-1] == (count, ratio)

        executed = len(received)
        for invalid in (
            {"count": "12abc", "ratio": 0.5},
            {"count": 1, "ratio": "Infinity"},
            {"ratio": 0.5},
        ):
            graph["nodes"][1]["data"]["config"] = invalid
            rejected = client.post(
                "/api/run/preview", json={"graph": graph, "nodeId": "plugin", "k": 2})
            assert rejected.status_code == 400
        assert len(received) == executed
    finally:
        client.delete(f"/api/canvas/{canvas_id}")
        if prior_spec is None:
            deps.node_specs.pop(kind, None)
        else:
            deps.node_specs[kind] = prior_spec
        if prior_builder is None:
            deps.node_builders.pop(kind, None)
        else:
            deps.node_builders[kind] = prior_builder


def test_plugin_previewability_and_requirements_are_truthful_end_to_end():
    """A plugin descriptor must constrain preview and hard placement before a run is submitted."""
    from hub.models import ResourceSpec
    from hub.sdk import NodeSpec, PortSpec

    deps = get_deps()
    kind = "plugin_requires_preview_contract"
    spec = NodeSpec(
        kind=kind, title="plugin full pass", category="compute",
        inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
        params=[], previewable=False,
        requires=ResourceSpec(gpu=1, labels={"engine": "plugin-gpu"}),
    )
    prior_spec = deps.node_specs.get(kind)
    prior_builder = deps.node_builders.get(kind)
    deps.node_specs[kind] = spec
    deps.node_builders[kind] = lambda *_args: (_ for _ in ()).throw(AssertionError("must not preview"))
    canvas_id = "plugin-requires-preview-contract"
    graph = {"id": canvas_id, "name": "plugin full pass", "version": 1, "nodes": [
        N("source", "source", {"uri": _uri("events")}),
        N("plugin", kind, {}),
    ], "edges": [E("source", "plugin")]}
    try:
        descriptor = next(item for item in client.get("/api/nodes").json() if item["kind"] == kind)
        assert descriptor["previewable"] is False
        assert descriptor["requires"]["gpu"] == 1
        assert descriptor["requires"]["labels"] == {"engine": "plugin-gpu"}

        assert client.put(f"/api/canvas/{canvas_id}", json=graph).status_code == 200
        restored = client.get(f"/api/canvas/{canvas_id}")
        assert restored.status_code == 200 and restored.json()["edges"] == graph["edges"]

        preview = client.post("/api/run/preview", json={"graph": restored.json(), "nodeId": "plugin", "k": 10})
        assert preview.status_code == 200
        assert preview.json()["notPreviewable"] is True
        assert "not sample-previewable" in preview.json()["reason"]

        plan = client.post("/api/graph/plan", json={"graph": restored.json(), "targetNodeId": "plugin"})
        assert plan.status_code == 200
        region = plan.json()["regions"][-1]
        assert region["unsatisfied"] is True and "engine=plugin-gpu" in region["requires"]

        rejected = client.post("/api/run/estimate", json={"graph": restored.json(), "targetNodeId": "plugin"})
        assert rejected.status_code == 400
        assert "no registered backend can satisfy required resources" in rejected.json()["detail"]
    finally:
        client.delete(f"/api/canvas/{canvas_id}")
        if prior_spec is None:
            deps.node_specs.pop(kind, None)
        else:
            deps.node_specs[kind] = prior_spec
        if prior_builder is None:
            deps.node_builders.pop(kind, None)
        else:
            deps.node_builders[kind] = prior_builder


# --------------------------------------------------------------------------- #
# Regression tests for code-review findings (concurrency / correctness / security)
# --------------------------------------------------------------------------- #
def test_two_sql_nodes_no_view_collision():
    # two sql nodes in one graph both use `FROM input` — must not clobber each other (finding #1)
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("images")}),
        N("qa", "sql", {"sql": "SELECT id FROM input"}),
        N("b", "source", {"uri": _uri("events")}),
        N("qb", "sql", {"sql": "SELECT id FROM input"}),
    ], "edges": [E("a", "qa"), E("b", "qb")]}
    ra = client.post("/api/run/preview", json={"graph": g, "nodeId": "qa", "k": 5}).json()
    rb = client.post("/api/run/preview", json={"graph": g, "nodeId": "qb", "k": 5}).json()
    assert not ra["notPreviewable"] and not rb["notPreviewable"]
    assert [c["name"] for c in ra["columns"]] == ["id"]  # both resolve their OWN input


def test_concurrent_previews_do_not_corrupt():
    # the shared DuckDB connection + unique views must survive concurrent evaluation (findings #2/#3)
    import concurrent.futures as cf
    from hub.deps import get_deps
    from hub.executors.preview import preview_node
    deps = get_deps()

    def graph_for(uri, pred):
        return __import__("hub.models", fromlist=["Graph"]).Graph(**{
            "id": "c", "version": 1,
            "nodes": [N("src", "source", {"uri": uri}), N("f", "filter", {"predicate": pred}),
                      N("d", "dedup", {})],
            "edges": [E("src", "f"), E("f", "d")],
        })

    imgs, evs = _uri("images"), _uri("events")

    def run(i):
        if i % 2 == 0:
            r = preview_node(graph_for(imgs, "is_valid = true"), "d", 20,
                             deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs)
            return "images", all(row.get("is_valid") for row in r.rows), r.not_previewable
        r = preview_node(graph_for(evs, "amount > 1"), "d", 20,
                         deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs)
        return "events", all(row.get("amount", 0) > 1 for row in r.rows), r.not_previewable

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, range(24)))
    assert all(ok and not npv for _, ok, npv in results)  # no cross-contamination, no crash


def test_sandbox_blocks_format_string_escape():
    # the AST guard rejects `.__class__` attribute access; the format-field escape hides the dunder in
    # a STRING — "{0.__class__.__mro__[-1].__subclasses__}".format(()) — so string literals with '__'
    # (and getattr(x, "__class__")) are rejected too.
    code = "def fn(row):\n    row['x'] = '{0.__class__.__mro__[-1].__subclasses__}'.format(())\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
    ], "edges": [E("src", "xf")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "xf", "k": 5}).json()
    assert r["error"] or r["notPreviewable"]
    assert "__" in (r.get("reason") or "")


def test_credentials_store_secret_references_and_removed_settings_are_rejected():
    # Agent/object-store credentials are Creds, not generic settings. Cred APIs reject raw material and
    # echo safe references; removed setting keys stay unavailable even when given a valid reference.
    from hub import metadb

    for key, value in (("agentApiKey", "env:DP_FIXTURE_AGENT_KEY"), ("objectStore", {})):
        removed = client.put(
            "/api/settings", json={"scope": "global", "key": key, "value": value})
        assert removed.status_code == 400 and "/api/creds" in removed.json()["detail"]
    assert client.post("/api/creds", json={
        "name": "raw agent", "kind": "agent", "fields": {"apiKey": "sk-super-secret"},
    }).status_code == 400
    assert client.post("/api/creds", json={
        "name": "raw store", "kind": "object_store",
        "fields": {"accessKeyId": "AKIA", "secretAccessKey": "shh"},
    }).status_code == 400

    agent_id = store_id = None
    try:
        agent_id = client.post("/api/creds", json={
            "name": "agent", "kind": "agent",
            "fields": {"apiKey": "env:DP_FIXTURE_AGENT_KEY"},
        }).json()["id"]
        store_id = client.post("/api/creds", json={
            "name": "store", "kind": "object_store", "fields": {
            "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
            "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
            "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
            "region": "us-east-1",
        }}).json()["id"]
        metadb.set_setting("agentCredId", agent_id, "global")
        metadb.set_setting("defaultObjectStoreCredId", store_id, "global")

        global_settings = client.get("/api/settings").json()["global"]
        assert global_settings["agentCredId"] == agent_id
        assert global_settings["defaultObjectStoreCredId"] == store_id
        assert "agentApiKey" not in global_settings and "objectStore" not in global_settings
        creds = {cred["id"]: cred for cred in client.get("/api/creds").json()}
        assert creds[agent_id]["fields"]["apiKey"] == "env:DP_FIXTURE_AGENT_KEY"
        assert creds[store_id]["fields"] == {
            "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
            "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
            "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
            "region": "us-east-1",
        }
    finally:
        metadb.set_setting("agentCredId", "", "global")
        metadb.set_setting("defaultObjectStoreCredId", "", "global")
        for cred_id in (agent_id, store_id):
            if cred_id:
                metadb.cred_delete(cred_id)


def test_user_scoped_settings_are_isolated_per_user():
    # scope='user' settings persist and don't leak across users (backs the Settings UI's user tier).
    alice = client.post("/api/users", json={"name": "SettingsAlice"}).json()["id"]
    bob = client.post("/api/users", json={"name": "SettingsBob"}).json()["id"]
    client.put("/api/settings", json={"scope": "user", "key": "backend", "value": "local-subprocess"}, headers={"X-DP-User": alice})
    assert client.get("/api/settings", headers={"X-DP-User": alice}).json()["user"].get("backend") == "local-subprocess"
    assert client.get("/api/settings", headers={"X-DP-User": bob}).json()["user"].get("backend") is None  # not Alice's


def test_user_scoped_backend_preference_wins_over_global(tmp_path):
    # pick_runner resolves a per-user runner preference before the workspace default (empty = inherit).
    import types
    from hub.deps import Deps
    from hub import metadb
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    assert {"local-out-of-core", "local-subprocess"} <= {r.name for r in d.runners}
    plan = types.SimpleNamespace(acyclic=True)
    metadb.set_setting("backend", "local-out-of-core", scope="global")
    metadb.set_setting("backend", "local-subprocess", scope="user", scope_id="prefalice")
    try:
        assert d.pick_runner(plan).name == "local-out-of-core"            # no uid → workspace default
        assert d.pick_runner(plan, "prefalice").name == "local-subprocess"  # per-user override wins
        assert d.pick_runner(plan, "prefbob").name == "local-out-of-core"   # other user → workspace default
    finally:
        metadb.set_setting("backend", "", scope="global")  # don't leak a global runner choice to other tests


def test_default_execution_is_the_per_canvas_kernel(tmp_path, monkeypatch):
    # kernel-only: with no explicit backend choice, execution defaults to the per-canvas kernel
    # (process isolation + durability + warm reuse). An explicit Settings→Execution choice still wins.
    import types

    from hub import metadb
    from hub import settings as sm
    from hub.deps import Deps
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    plan = types.SimpleNamespace(acyclic=True)
    metadb.set_setting("backend", "", scope="global")
    monkeypatch.setattr(sm.settings, "execution", "")  # test the TRUE default (conftest sets DP_EXECUTION for suite speed)
    try:
        assert d.pick_runner(plan).name == "kernel"                  # default → the per-canvas kernel
        metadb.set_setting("backend", "local-out-of-core", scope="global")
        assert d.pick_runner(plan).name == "local-out-of-core"       # explicit choice wins over the default
    finally:
        metadb.set_setting("backend", "", scope="global")


def test_sandbox_blocks_dunder_escape():
    # the classic ().__class__.__mro__ escape must be rejected (finding #4)
    code = "def fn(row):\n    row['x'] = ().__class__.__mro__[-1].__subclasses__()\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
    ], "edges": [E("src", "xf")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "xf", "k": 5}).json()
    assert r["error"] or r["notPreviewable"]  # blocked, not executed
    assert "__" in (r.get("reason") or "") or "allowed" in (r.get("reason") or "")


def test_sandbox_pyarrow_root_cannot_do_file_io(tmp_path):
    # The injected pyarrow root is I/O-guarded: a cell reached pyarrow.OSFile / output_stream /
    # memory_map by ATTRIBUTE (no import, no dunder) → arbitrary local read/write. The proxy blocks the
    # file/stream builders while keeping the safe arrow surface (Table/array/compute/types).
    from hub import sandbox
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret")
    out = tmp_path / "escaped.bin"

    # 1) attribute-reach on the injected root is blocked — INCLUDING the pyarrow.lib.* re-export twins
    #    (pyarrow.lib.OSFile is pyarrow.OSFile), which the first attempt at this fix missed
    for expr in (f"pyarrow.OSFile('{secret}').read()",
                 f"pyarrow.memory_map('{secret}').read()",
                 f"pyarrow.output_stream('{out}').write(b'x')",
                 f"pyarrow.input_stream('{secret}').read()",
                 f"pyarrow.lib.OSFile('{out}', 'w').write(b'x')",
                 f"pyarrow.lib.memory_map('{secret}', 'r').read()"):
        fn = sandbox.compile_operator(f"def fn(t):\n    return {expr}", "map_batches")
        with pytest.raises(Exception):  # AttributeError('… is blocked …') surfaced through the op
            fn(None)
    assert not out.exists(), "a cell wrote a file via pyarrow.output_stream — escape not closed"

    # 2) a re-`import pyarrow` inside the cell also gets the guarded proxy, not the raw module
    fn = sandbox.compile_operator("def fn(t):\n    import pyarrow as pa\n    return pa.OSFile('/etc/hosts')", "map_batches")
    with pytest.raises(Exception):
        fn(None)

    # 3) the legitimate arrow surface still works (constructors, types, compute, decimal)
    import pyarrow as pa
    ok = sandbox.compile_operator(
        "def fn(t):\n    import pyarrow as pa\n    import pyarrow.compute as pc\n"
        "    return pa.table({'y': pc.multiply(t['x'], 2)})", "map_batches")
    res = ok(pa.table({"x": [1, 2, 3]}))
    assert res.column("y").to_pylist() == [2, 4, 6]
    dec = sandbox.compile_operator("def fn(t):\n    import pyarrow as pa\n    return pa.array([1,2], type=pa.decimal128(5,2))", "map_batches")
    assert len(dec(None)) == 2  # decimal array construction doesn't abort


def test_map_batches_skip_isolates_bad_rows_not_the_whole_batch():
    # on_error='skip' used to DROP the entire batch when a batch UDF threw — so how many rows survived
    # depended on the batch size (2048 in preview vs 8192 in the run), making preview disagree with the
    # run. Skip now re-runs the UDF row-by-row and keeps the successes, dropping only the rows that fail.
    import pyarrow as pa
    from hub.executors.engine import _apply_fn, _apply_batch, NotPreviewable, _XF_BATCH
    assert _XF_BATCH == _XF_BATCH  # single constant → preview and run read at the same batch size

    def rows_fn(rows):
        return [{"y": 100 // r["x"]} for r in rows]  # ZeroDivisionError on the x==0 row
    batch = pa.RecordBatch.from_pylist([{"x": 1}, {"x": 0}, {"x": 2}, {"x": 4}])
    out = _apply_fn(rows_fn, batch, "map_batches", "skip", None)
    assert [r["y"] for r in out] == [100, 50, 25]  # bad row dropped; the other 3 kept (NOT the batch)
    with pytest.raises(NotPreviewable):
        _apply_fn(rows_fn, batch, "map_batches", "raise", None)

    def arrow_fn(t):  # fails the whole batch when a zero is present; each good 1-row slice passes
        if 0 in t.column("x").to_pylist():
            raise ValueError("this batch has a zero")
        return t
    tbl = pa.table({"x": [1, 0, 2, 4]})
    res = _apply_batch(arrow_fn, tbl, "arrow", "skip", None)
    assert res.column("x").to_pylist() == [1, 2, 4]  # only the offending row dropped
    assert _apply_batch(arrow_fn, pa.table({"x": [0]}), "arrow", "skip", None) is None  # all-bad → nothing


def test_batch_schema_drift_widens_safely_and_fails_loudly_not_silently():
    # across batches a transform's output dtype can drift. The old code cast to the first batch with
    # safe=False, SILENTLY corrupting values (e.g. an out-of-range int64 wrapped into an int32). It now
    # casts safely: a widening passes; a lossy narrowing fails loudly instead of writing garbage.
    import pyarrow as pa
    from hub.executors.engine import _conform, NotPreviewable
    # safe WIDENING int32 -> int64 (first batch was int64): keep going
    assert _conform(pa.table({"x": pa.array([1, 2], pa.int32())}),
                    pa.schema([("x", pa.int64())]), None).column("x").to_pylist() == [1, 2]
    # a value that CANNOT fit the first batch's narrower type: loud, not a silent wrap
    with pytest.raises(NotPreviewable):
        _conform(pa.table({"x": pa.array([5_000_000_000], pa.int64())}),
                 pa.schema([("x", pa.int32())]), None)
    # a non-integral float can't be stored as the first batch's int without loss: loud
    with pytest.raises(NotPreviewable):
        _conform(pa.table({"x": pa.array([3.5])}), pa.schema([("x", pa.int64())]), None)
    # a structurally different batch (renamed column) is drift, not a cast: loud
    with pytest.raises(NotPreviewable):
        _conform(pa.table({"z": [1]}), pa.schema([("x", pa.int64())]), None)


def test_flat_map_streams_lazily_and_byte_estimate_tracks_payload():
    # flat_map must be STREAMED, not list()-materialized per row — else a large (or unbounded) per-row
    # fan-out balloons memory. Proof: an INFINITE generator per row; taking the first few must return
    # promptly. If the executor did list(fn(row)) it would hang forever here.
    import itertools
    import pyarrow as pa
    from hub.executors.engine import _iter_fn, _est_row_bytes

    def fan(r):
        i = 0
        while True:
            yield {"v": r["x"] * 1_000_000 + i}
            i += 1
    batch = pa.RecordBatch.from_pylist([{"x": 1}, {"x": 2}])
    first5 = list(itertools.islice(_iter_fn(fan, batch, "flat_map", "raise", None), 5))
    assert [r["v"] for r in first5] == [1_000_000, 1_000_001, 1_000_002, 1_000_003, 1_000_004]
    # the spill flush budget is bytes-first: a 10KB-blob row estimates far larger than a small int row
    assert _est_row_bytes({"b": b"x" * 10_000}) > 100 * _est_row_bytes({"n": 1})


def test_schema_contract_type_model_is_specificity_faithful():
    # a schema contract used to compare types through a COARSE bucket — so a decimal(38,9) contract
    # matched a plain double, a timestamp[ns] matched a us timestamp (false PASS on real drift), and a
    # list<int> contract FALSE-FAILED against an int list (VARCHAR[] vs BIGINT[]). The type model now
    # honors the contract's specificity: coarse stays lenient, precise is enforced exactly.
    from hub.executors.engine import canonical_type as C, type_satisfies as S, _duck_type

    # the COARSEST want — a typeless (name-only) contract column — asserts presence only, so it accepts
    # any actual (acceptance #4: it used to FALSE-FAIL every real type because ("other","") hit no branch).
    assert S(C(""), C("BIGINT")) and S(C(""), C("VARCHAR")) and S(C(""), C("STRUCT(a INTEGER)"))
    assert not S(C("geometry"), C("BIGINT"))          # a NAMED unknown type still compares exactly
    assert S(C("geometry"), C("geometry"))
    # only a genuinely-EMPTY type is the wildcard — an unrecognized-but-NON-empty type (numpy dtype
    # `<i8`, malformed `(int)`) must stay STRICT, else a precise contract silently widens (PR #35 review).
    assert not S(C("<i8"), C("BIGINT")) and not S(C("<f8"), C("DOUBLE")) and not S(C("(int)"), C("BIGINT"))

    # coarse / inferred (display-coarse) contracts stay lenient against the precise actual
    assert S(C("float"), C("DECIMAL(38,9)"))          # a float contract accepts a decimal actual
    assert S(C("list"), C("INTEGER[]"))               # bare list accepts a typed list (was a false FAIL)
    assert S(C("struct"), C("STRUCT(a INTEGER)"))
    assert S(C("map"), C("MAP(VARCHAR, BIGINT)"))
    assert S(C("timestamp"), C("TIMESTAMP_NS"))
    assert S(C("int"), C("BIGINT")) and S(C("string"), C("VARCHAR"))

    # a PRECISE contract is now enforced faithfully — real drift no longer passes
    assert S(C("decimal(38,9)"), C("DECIMAL(38,9)"))
    assert not S(C("decimal(38,9)"), C("DECIMAL(10,2)"))
    assert not S(C("decimal(38,9)"), C("DOUBLE"))
    assert S(C("list<int>"), C("INTEGER[]")) and not S(C("list<int>"), C("VARCHAR[]"))
    assert S(C("struct<a:int,b:string>"), C("STRUCT(a INTEGER, b VARCHAR)"))
    assert not S(C("struct<a:int>"), C("STRUCT(a VARCHAR)"))
    assert S(C("map<string,int>"), C("MAP(VARCHAR, BIGINT)"))
    assert not S(C("map<string,int>"), C("MAP(VARCHAR, VARCHAR)"))
    assert S(C("timestamp[ns]"), C("TIMESTAMP_NS")) and not S(C("timestamp[ns]"), C("TIMESTAMP"))
    assert S(C("timestamp[us]"), C("TIMESTAMP"))       # microsecond is the unmarked DuckDB default
    assert S(C("timestamp with time zone"), C("TIMESTAMP WITH TIME ZONE"))
    assert not S(C("timestamp with time zone"), C("TIMESTAMP"))

    # _duck_type now builds a PRECISE stand-in so a declared contract propagates faithfully downstream
    assert _duck_type("decimal(38,9)") == "DECIMAL(38,9)"
    assert _duck_type("list<int>") == "BIGINT[]"
    assert _duck_type("int") == "BIGINT" and _duck_type("string") == "VARCHAR"  # coarse still coarse


def test_write_mode_append_builds_a_readable_directory_dataset():
    # append is REAL: it writes a directory of part files that reads back as the accumulated rows
    def append_run():
        g = {"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("images")}),
            N("wr", "write", {"name": "append_ds", "writeMode": "append", "format": "parquet"}),
        ], "edges": [E("src", "wr")]}
        return _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])

    st1 = append_run()
    assert st1["status"] == "done", st1.get("error")
    n, out = st1["totalRows"], _output_field(st1, "uri", outcome="committed")
    assert n and out and not out.endswith(".parquet")  # a directory, not a single file
    append_run()  # a second part
    read_graph = {"id": "c", "version": 1,
                  "nodes": [N("s", "source", {"uri": out})], "edges": []}
    preview = client.post("/api/run/preview", json={
        "graph": read_graph, "nodeId": "s", "k": 100000,
    }).json()
    assert preview["notPreviewable"], "directory enumeration belongs in a cancellable durable run"
    readback = _poll(client.post("/api/run", json={
        "graph": read_graph, "targetNodeId": "s", "confirmed": True,
    }).json()["runId"])
    assert readback["status"] == "done", readback.get("error")
    assert readback["totalRows"] >= 2 * n and readback["totalRows"] % n == 0


def test_write_formats_round_trip(tmp_path):
    # every extension the write node accepts must read back — no silent corruption (review findings):
    # .json is written via DuckDB COPY (not parquet bytes in a .json file), and .pq / .tsv append parts
    # are discovered by the directory reader.
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    con = db.conn()
    with db.lock():
        rel = con.sql("SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2 AS a, 'y' AS b")
        a.write(str(tmp_path / "out.json"), rel, "overwrite")
        assert sorted(a.scan(str(tmp_path / "out.json")).fetchall()) == [(1, "x"), (2, "y")]
        for i, ext in enumerate((".pq", ".tsv", ".json")):  # distinct bases — one format per dataset
            res = a.write(str(tmp_path / f"app{i}{ext}"), con.sql("SELECT 3 AS a, 'z' AS b"), "append")
            assert a.scan(res["uri"]).fetchall() == [(3, "z")], ext  # part-*.<ext> read back from the dir


def test_append_is_transactional_and_lossless(tmp_path, monkeypatch):
    # ARC4 transactional append (blocks-production data-safety): (1) a crashed/failed part write leaves NO
    # readable partial part; (2) overwrite→append folds the prior single file in (no orphaned data);
    # (3) csv parts with a drifted column set reconcile by name; (4) mixing formats in one dataset is
    # rejected (would silently drop the non-winning parts on read).
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    con = db.conn()
    with db.lock():
        base = str(tmp_path / "ds.parquet")
        # (2) overwrite writes a single FILE; a later append must fold it in, not orphan it. The dataset uri
        # then becomes the part DIRECTORY (r2["uri"] = "…/ds"), which is what a consumer reads.
        a.write(base, con.sql("SELECT 1 AS id, 10 AS v"), "overwrite")
        assert os.path.isfile(base)
        r2 = a.write(base, con.sql("SELECT 2 AS id, 20 AS v"), "append")
        ds = r2["uri"]
        assert sorted(a.scan(ds).fetchall()) == [(1, 10), (2, 20)], "overwrite→append orphaned the prior file"
        assert not os.path.isfile(base), "the single file should have been migrated into the part dir"

        # (1) a failed append (partial part written, then a crash) must not corrupt the committed dataset
        def _boom(rel_, path_, ext_):
            with open(path_, "w") as f:
                f.write("PARTIAL-CORRUPT")   # a half-written part at the .tmp path
            raise RuntimeError("boom mid-write")
        monkeypatch.setattr(DuckDBAdapter, "_write_part", staticmethod(_boom))
        with pytest.raises(RuntimeError):
            a.write(base, con.sql("SELECT 3 AS id, 30 AS v"), "append")
        monkeypatch.undo()
        assert sorted(a.scan(ds).fetchall()) == [(1, 10), (2, 20)], "a failed append corrupted the dataset"

        # (3) csv appends with a DRIFTED column set reconcile by name (union_by_name), not misalign
        cbase = str(tmp_path / "cds.csv")
        a.write(cbase, con.sql("SELECT 1 AS id, 10 AS x"), "append")
        cr = a.write(cbase, con.sql("SELECT 2 AS id, 20 AS y"), "append")
        rel = a.scan(cr["uri"])   # the part directory
        assert set(rel.columns) == {"id", "x", "y"} and rel.aggregate("count(*)").fetchone()[0] == 2

        # (4) mixing extensions under one dataset base is rejected up-front — cross-format (.csv into a
        # .parquet dataset) AND same-format aliases (.pq into a .parquet dataset), since _read_dir globs
        # each concrete extension separately and would silently drop the non-winning parts.
        mbase = str(tmp_path / "mds.parquet")
        a.write(mbase, con.sql("SELECT 1 AS id"), "append")
        with pytest.raises(NotImplementedError, match="one file extension per output"):
            a.write(str(tmp_path / "mds.csv"), con.sql("SELECT 2 AS id"), "append")
        with pytest.raises(NotImplementedError, match="one file extension per output"):
            a.write(str(tmp_path / "mds.pq"), con.sql("SELECT 3 AS id"), "append")  # same format, still rejected


def test_concurrent_mixed_format_append_publishes_exactly_one_part(tmp_path, monkeypatch):
    # Both writers finish their lock-free staging while the dataset is still empty, then race to publish.
    # The format check must run under the SAME per-base lock as os.replace: one writer commits, while the
    # loser reports the stable mixed-format conflict and removes its unpublished staging file.
    import glob as _glob
    import threading as _th

    monkeypatch.setenv("DP_APPEND_COMPACT_PARTS", "0")
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter

    a = DuckDBAdapter()
    base = str(tmp_path / "mixed")
    staged = _th.Barrier(2)
    original_write_part = DuckDBAdapter._write_part

    def _stage_then_race(rel, path, ext):
        original_write_part(rel, path, ext)
        staged.wait(timeout=10)

    monkeypatch.setattr(DuckDBAdapter, "_write_part", staticmethod(_stage_then_race))
    successes: list[tuple[str, dict]] = []
    failures: list[tuple[str, BaseException]] = []

    def worker(ext: str, row_id: int) -> None:
        try:
            with db.run_scope() as sc:
                result = a.write(base + ext, sc.con.sql(f"SELECT {row_id} AS id"), "append")
            successes.append((ext, result))
        except BaseException as exc:  # capture the exact commit-time conflict from the worker thread
            failures.append((ext, exc))

    threads = [
        _th.Thread(target=worker, args=(".parquet", 1)),
        _th.Thread(target=worker, args=(".csv", 2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads), "mixed-format append race deadlocked"

    assert len(successes) == 1, successes
    assert len(failures) == 1, failures
    winner_ext, result = successes[0]
    loser_ext, error = failures[0]
    assert isinstance(error, NotImplementedError)
    assert "one file extension per output dataset" in str(error)
    assert result["uri"] == base and winner_ext != loser_ext

    committed = _glob.glob(os.path.join(base, "part-*"))
    assert len(committed) == 1 and committed[0].endswith(winner_ext), committed
    assert not any(path.endswith(loser_ext) for path in committed), "the conflicting writer published a part"
    assert not _glob.glob(base + ".parttmp-*"), "the conflicting writer leaked its staging file"
    with db.run_scope():
        assert a.scan(base).aggregate("count(*)").fetchone()[0] == 1


def test_append_object_store_overwrite_then_append_no_orphan(tmp_path, object_store_cred):
    # ARC4 transactional append on a REAL object store (moto): overwrite writes s3://…/out.parquet (a single
    # object); a subsequent append writes into the out/ prefix. The prior object must be MIGRATED into the
    # prefix (server-side move) so switching overwrite→append doesn't silently orphan it.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False
        a = DuckDBAdapter()
        uri = "s3://bkt/data/out.parquet"
        with db.lock():
            a.write(uri, db.conn().sql("SELECT 1 AS id, 10 AS v"), "overwrite")
            r = a.write(uri, db.conn().sql("SELECT 2 AS id, 20 AS v"), "append")
            rows = sorted(a.scan(r["uri"]).fetchall())
            # object-store csv/json append is REJECTED (the object reader reads a prefix as parquet → the
            # parts would be unreadable), rather than silently producing a write-only dataset.
            with pytest.raises(NotImplementedError, match="object-store append supports parquet only"):
                a.write("s3://bkt/data/out.csv", db.conn().sql("SELECT 1 AS id"), "append")
        assert rows == [(1, 10), (2, 20)], f"object overwrite→append orphaned the prior object: {rows}"
    finally:
        server.stop()
        object_store_cred(None)


def test_append_auto_compacts_at_threshold(tmp_path, monkeypatch):
    # ARC4 append-compaction: once a local append part dir exceeds DP_APPEND_COMPACT_PARTS committed parts,
    # it rewrites them into ONE part (bounding small-file growth), all rows preserved.
    import glob

    monkeypatch.setenv("DP_APPEND_COMPACT_PARTS", "3")
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    con = db.conn()
    base = str(tmp_path / "acc.parquet")
    with db.lock():
        ds = None
        for i in range(4):  # after the 4th append, 4 parts > threshold(3) → compact to 1
            ds = a.write(base, con.sql(f"SELECT {i} AS id, {i} * 10 AS v"), "append")["uri"]
        parts = glob.glob(os.path.join(ds, "**/*.parquet"), recursive=True)
        assert len(parts) == 1, f"expected 1 compacted part, got {len(parts)}: {parts}"
        rows = sorted(a.scan(ds).fetchall())
    assert rows == [(0, 0), (1, 10), (2, 20), (3, 30)], f"compaction lost/changed data: {rows}"


def test_append_concurrent_same_base_no_data_loss(tmp_path, monkeypatch):
    # ARC4 concurrency (regression for the per-base-lock fix): many appends to the SAME base, each in its
    # OWN run_scope (the runner's model — a per-thread cursor, NO outer db.lock()), running concurrently
    # while compaction fires must not lose a committed row or raise. Before the fix, an unlocked
    # rmtree(base)+swap in compaction raced other appends' publish → ~35% failures + lost rows. The part
    # is now staged as a sibling of base (compaction's rmtree can't destroy it) and makedirs+publish+
    # compaction run under a per-base lock.
    import threading as _th

    monkeypatch.setenv("DP_APPEND_COMPACT_PARTS", "3")  # compact aggressively → maximize the race window
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    base = str(tmp_path / "cc.parquet")
    errors: list[str] = []
    uris: list[str] = []
    n_threads, per_thread = 5, 20

    def worker(t: int):
        try:
            for i in range(per_thread):
                with db.run_scope() as sc:
                    res = a.write(base, sc.con.sql(f"SELECT {t * 1000 + i} AS id"), "append")
                    uris.append(res["uri"])
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [_th.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"concurrent append raised: {errors[:3]}"
    ds = uris[0]  # every append returns the same part-dir uri (name sans extension)
    with db.run_scope():
        got = a.scan(ds).aggregate("count(*)").fetchone()[0]
        distinct = a.scan(ds).aggregate("count(distinct id)").fetchone()[0]
    assert got == n_threads * per_thread, f"lost rows under concurrent append+compaction: {got} != {n_threads * per_thread}"
    assert distinct == n_threads * per_thread, f"duplicated/corrupted ids under concurrency: {distinct}"


def test_recover_orphans_restores_interrupted_compaction_and_cleans_staging(tmp_path):
    # ARC4 crash safety: startup recovery of temp siblings an interrupted append/compaction leaves behind.
    # (1) compaction cut between its two renames → base absent, data in `<base>.old-*` → restored;
    # (2) stale `.old-*` with base intact, an in-flight `.parttmp-*` part, a partial `.compact-*` → all
    # dropped; (3) a real output is untouched and staging junk never surfaces via list_outputs.
    import glob as _glob

    from hub.storage import LocalStorage
    root = str(tmp_path / "outputs")
    os.makedirs(root)

    # (1) interrupted compaction: originals parked in ds1.old-*, ds1 itself gone
    import duckdb
    old1 = os.path.join(root, "ds1.old-abc12345")
    os.makedirs(old1)
    duckdb.sql("SELECT 42 AS id").write_parquet(os.path.join(old1, "part-0.parquet"))
    # (2a) stale .old-* with the base dir present → superseded, should be dropped
    os.makedirs(os.path.join(root, "ds2"))
    duckdb.sql("SELECT 1 AS id").write_parquet(os.path.join(root, "ds2", "part-0.parquet"))
    os.makedirs(os.path.join(root, "ds2.old-def45678"))
    # (2b) in-flight append staging (a file, no data suffix) + (2c) partial compaction output (a dir)
    duckdb.sql("SELECT 9 AS id").write_parquet(os.path.join(root, "ds3.parttmp-0011223344"))
    os.makedirs(os.path.join(root, "ds4.compact-55667788"))
    # (3) a genuine published output must be left alone
    duckdb.sql("SELECT 7 AS id").write_parquet(os.path.join(root, "keep.parquet"))

    LocalStorage(root).recover_orphans()

    # ds1 restored from its .old-*, with the original data intact
    assert os.path.isdir(os.path.join(root, "ds1")), "interrupted compaction was not restored"
    parts = _glob.glob(os.path.join(root, "ds1", "*.parquet"))
    assert len(parts) == 1 and duckdb.read_parquet(parts[0]).fetchone()[0] == 42
    # every temp sibling is gone; the real output and restored base remain
    leftovers = [f for f in os.listdir(root) if any(s in f for s in (".old-", ".parttmp-", ".compact-"))]
    assert not leftovers, f"temp siblings not cleaned: {leftovers}"
    assert os.path.exists(os.path.join(root, "keep.parquet")) and os.path.isdir(os.path.join(root, "ds2"))
    names = {os.path.basename(u.rstrip("/")) for u in LocalStorage(root).list_outputs()}
    assert "keep.parquet" in names and not any(".parttmp-" in n or ".compact-" in n for n in names)


def test_recover_orphans_resolves_partition_overwrite_crash_points(tmp_path):
    """Before/during/after the two-rename commit, recovery exposes one complete version."""
    import duckdb

    from hub.storage import LocalStorage

    root = str(tmp_path / "outputs")
    os.makedirs(root)

    def write_version(path: str, value: int) -> None:
        part = os.path.join(path, f"cat={value}")
        os.makedirs(part)
        duckdb.sql(f"SELECT {value} AS id").write_parquet(os.path.join(part, "data.parquet"))

    def ids(path: str) -> list[int]:
        rows = duckdb.read_parquet(os.path.join(path, "**/*.parquet"), hive_partitioning=True).fetchall()
        return [row[0] for row in rows]

    # Crash before commit: the old base is still live and the unpublished staging dir is discarded.
    before = os.path.join(root, "before")
    write_version(before, 1)
    write_version(before + ".partition-new-11111111", 2)

    # Crash during commit: the old base has been parked but the staged version is not published yet.
    during = os.path.join(root, "during")
    write_version(during + ".partition-old-22222222", 3)
    write_version(during + ".partition-new-22222222", 4)

    # Crash after commit: the new base is live and only cleanup of the parked old version remains.
    after = os.path.join(root, "after")
    write_version(after, 6)
    write_version(after + ".partition-old-33333333", 5)

    LocalStorage(root).recover_orphans()

    assert ids(before) == [1]
    assert ids(during) == [3]
    assert ids(after) == [6]
    assert not [name for name in os.listdir(root) if ".partition-old-" in name or ".partition-new-" in name]


def test_partitioned_write_hive_layout_and_pruned_read(tmp_path):
    # ARC4 write-partitioned-merge: partitionBy → a Hive dir=val/ parquet directory, read back with the
    # partition column present + partition pruning; overwrite is clean (temp dir + swap, no stale
    # partitions); non-parquet / append / missing-column reject.
    import glob

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    con = db.conn()
    with db.lock():
        rel = con.sql("SELECT i AS id, mod(i, 3) AS cat, i * 2 AS v FROM range(0, 30) r(i)")
        out = str(tmp_path / "parted.parquet")
        ds = a.write(out, rel, "overwrite", partition_by="cat")["uri"]
        assert sorted(os.path.basename(p) for p in glob.glob(os.path.join(ds, "*"))) == ["cat=0", "cat=1", "cat=2"]
        r = a.scan(ds)
        assert set(r.columns) == {"id", "cat", "v"}, "partition column must be present on read"
        assert r.aggregate("count(*)").fetchone()[0] == 30
        assert a.scan(ds, predicate="cat = 1").aggregate("count(*)").fetchone()[0] == 10  # partition-pruned read
        # a second overwrite (fewer rows, fewer partitions) must REPLACE cleanly — no stale cat=2 left over
        rel2 = con.sql("SELECT i AS id, mod(i, 2) AS cat, i AS v FROM range(0, 4) r(i)")
        ds2 = a.write(out, rel2, "overwrite", partition_by="cat")["uri"]
        assert a.scan(ds2).aggregate("count(*)").fetchone()[0] == 4
        assert sorted(os.path.basename(p) for p in glob.glob(os.path.join(ds2, "*"))) == ["cat=0", "cat=1"]
        # rejects
        with pytest.raises(NotImplementedError):
            a.write(str(tmp_path / "x.csv"), rel, "overwrite", partition_by="cat")   # parquet-only
        with pytest.raises(NotImplementedError):
            a.write(out, rel, "append", partition_by="cat")                          # append unsupported
        with pytest.raises(ValueError):
            a.write(str(tmp_path / "y.parquet"), rel, "overwrite", partition_by="nope")  # column not in data
        # #1 regression guard: a FLAT append dir whose PATH contains a `key=val` segment must NOT get a
        # spurious partition column — hive parsing is scoped to real key=val partition SUBDIRS.
        eqbase = str(tmp_path / "run=abc" / "flat.parquet")
        os.makedirs(os.path.dirname(eqbase), exist_ok=True)
        fr = a.write(eqbase, con.sql("SELECT 1 AS id, 9 AS v"), "append")
        frel = a.scan(fr["uri"])
        assert "run" not in frel.columns and set(frel.columns) == {"id", "v"}, "flat dir got a spurious partition col"


def test_partitioned_local_overwrite_rolls_back_when_publish_fails(tmp_path, monkeypatch):
    from hub import db
    from hub.plugins import adapters as adapter_module
    from hub.plugins.adapters import DuckDBAdapter

    a = DuckDBAdapter()
    out = str(tmp_path / "parted.parquet")
    with db.lock():
        a.write(out, db.conn().sql("SELECT i AS id, mod(i, 3) AS cat FROM range(0, 30) r(i)"),
                "overwrite", partition_by="cat")

        base = os.path.splitext(out)[0]
        real_replace = os.replace
        failed = False

        def fail_publish_once(src: str, dst: str) -> None:
            nonlocal failed
            if not failed and src.startswith(base + ".partition-new-") and dst == base:
                failed = True
                raise OSError("injected publish failure")
            real_replace(src, dst)

        monkeypatch.setattr(adapter_module.os, "replace", fail_publish_once)
        with pytest.raises(OSError, match="injected publish failure"):
            a.write(out, db.conn().sql("SELECT i AS id, 0 AS cat FROM range(0, 4) r(i)"),
                    "overwrite", partition_by="cat")

        # The failed replacement synchronously restores the complete old version; no restart is needed.
        old = a.scan(base)
        assert old.aggregate("count(*)").fetchone()[0] == 30
        assert old.aggregate("count(DISTINCT cat)").fetchone()[0] == 3
        assert not [name for name in os.listdir(tmp_path)
                    if ".partition-old-" in name or ".partition-new-" in name]


def test_partitioned_object_overwrite_is_rejected_without_mutation(object_store_cred):
    # A multi-object Hive prefix cannot be atomically replaced by the file adapter. Reject before deleting
    # any existing object; ordinary single-object overwrite remains supported.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False
        cli = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                           region_name="us-east-1")
        a = DuckDBAdapter()
        uri = "s3://bkt/parted.parquet"
        cli.put_object(Bucket="bkt", Key="parted/cat=9/old.parquet", Body=b"complete-old-version")
        with db.lock():
            with pytest.raises(NotImplementedError, match="atomic table format or catalog commit"):
                a.write(uri, db.conn().sql("SELECT i AS id, mod(i, 3) AS cat FROM range(0, 30) r(i)"),
                        "overwrite", partition_by="cat")
            a.write("s3://bkt/plain.parquet", db.conn().sql("SELECT 7 AS id"), "overwrite")

        assert cli.get_object(Bucket="bkt", Key="parted/cat=9/old.parquet")["Body"].read() == b"complete-old-version"
        assert cli.head_object(Bucket="bkt", Key="plain.parquet")["ContentLength"] > 0
    finally:
        server.stop()
        object_store_cred(None)


def test_arrow_feather_is_streamed_out_of_core(tmp_path, monkeypatch):
    # ARC4 arrow-out-of-core: the feather/arrow WRITE streams RecordBatches via _stream_ipc (never
    # to_arrow_table's whole-table-in-RAM), and the local READ is a LAZY, re-scannable IPC dataset
    # (not feather.read_table). Prove the write goes through _stream_ipc + the read re-scans + round-trips.
    import hub.plugins.adapters as _ad
    from hub import db
    a = _ad.DuckDBAdapter()
    con = db.conn()
    calls = []
    orig = _ad._stream_ipc
    monkeypatch.setattr(_ad, "_stream_ipc", lambda rel, sink: (calls.append(1), orig(rel, sink))[1])
    with db.lock():
        rel = con.sql("SELECT i AS id, i * 2 AS v FROM range(0, 5000) t(i)")  # spans several 65536-row batches trivially
        out = str(tmp_path / "big.feather")
        a.write(out, rel, "overwrite")
        assert calls, "feather write must go through the streaming _stream_ipc path (not to_arrow_table)"
        r = a.scan(out)
        assert r.aggregate("count(*)").fetchone()[0] == 5000
        assert r.aggregate("count(*)").fetchone()[0] == 5000  # re-scannable (a one-shot reader would give 0)


def test_output_version_is_content_addressed_and_flags_schema_drift(tmp_path, caplog):
    # ARC4 output-versioning: an output's catalog version is a CONTENT hash of (schema + rows + fingerprint),
    # not a frozen 'v1' — the SAME data re-registers to the SAME version (a restart never spuriously bumps),
    # a changed schema/rows yields a NEW version, and an overwrite whose schema DRIFTED logs a warning.
    import logging

    import duckdb
    from hub.deps import Deps
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    cat = d.catalog
    p = str(tmp_path / "out.parquet")
    duckdb.connect().execute(f"COPY (SELECT 1 AS id, 10 AS v) TO '{p}' (FORMAT PARQUET)")
    t1 = cat.register_output(name="out", uri=p, parents=[])
    assert t1.version and t1.version != "v1" and t1.version.startswith("v")  # content-addressed, not 'v1'
    t2 = cat.register_output(name="out", uri=p, parents=[])                    # same file, unchanged
    assert t2.version == t1.version, "re-registering identical data must keep the version (no restart bump)"
    # overwrite with a DRIFTED schema → new version + a schema-change warning
    duckdb.connect().execute(f"COPY (SELECT 1 AS id, 'x' AS label, 2.0 AS score) TO '{p}' (FORMAT PARQUET)")
    with caplog.at_level(logging.WARNING, logger="hub"):
        t3 = cat.register_output(name="out", uri=p, parents=[])
    assert t3.version != t1.version, "a changed schema must produce a new version"
    assert any("schema changed" in r.getMessage() for r in caplog.records), "schema drift must be surfaced"


def test_catalog_output_receipt_attests_exact_unmanaged_version(tmp_path):
    from hub import metadb

    token = os.urandom(8).hex()
    uri = str(tmp_path / f"receipt-{token}.parquet")
    other_uri = str(tmp_path / f"receipt-other-{token}.parquet")
    event_key = f"receipt-{token}"
    wrong_version_key = f"receipt-wrong-version-{token}"
    metadb.catalog_upsert_entry(uri, "receipt", {
        "id": f"tbl_receipt_{token}", "name": "receipt", "uri": uri,
        "version": "v1", "columns": [], "tags": [],
    })
    metadb.catalog_upsert_entry(other_uri, "receipt-other", {
        "id": f"tbl_receipt_other_{token}", "name": "receipt-other", "uri": other_uri,
        "version": "v1", "columns": [], "tags": [],
    })
    try:
        metadb.catalog_record_output_publication(event_key, uri + "/", "v1")
        metadb.catalog_record_output_publication(event_key, uri, "v1")
        with metadb.session() as session:
            receipt = session.get(metadb.CatalogPublicationEvent, event_key)
            assert (receipt.effect_type, receipt.uri, receipt.version) == (
                "output", uri, "v1")

        with pytest.raises(RuntimeError, match="version does not match"):
            metadb.catalog_record_output_publication(wrong_version_key, uri, "v2")
        with metadb.session() as session:
            assert session.get(metadb.CatalogPublicationEvent, wrong_version_key) is None

        metadb.catalog_upsert_entry(uri, "receipt", {
            "id": f"tbl_receipt_{token}", "name": "receipt", "uri": uri,
            "version": "v2", "columns": [], "tags": [],
        })
        metadb.catalog_record_output_publication(event_key, uri, "v1")
        assert metadb.catalog_get(uri)["version"] == "v2"

        with pytest.raises(RuntimeError, match="publication key collision"):
            metadb.catalog_record_output_publication(event_key, other_uri, "v1")
    finally:
        metadb.catalog_delete_entry(uri)
        metadb.catalog_delete_entry(other_uri)


def test_unmanaged_output_replay_is_atomic_and_projection_independent(tmp_path, monkeypatch):
    import duckdb

    from hub import metadb
    from hub.deps import Deps
    from hub.models import LineagePublication

    token = os.urandom(8).hex()
    output = str(tmp_path / f"unmanaged-replay-{token}.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS id, 'value' AS label) TO '{output}' (FORMAT PARQUET)")
    catalog = Deps(str(tmp_path / "ws"), str(tmp_path / "data")).catalog
    embedded: list[str | None] = []
    monkeypatch.setattr(catalog, "_embed_one", lambda table: embedded.append(table.version))

    def publication(key: str) -> LineagePublication:
        return LineagePublication(
            idempotency_key=key,
            run_id=f"run-{key}",
            producer="unmanaged-replay-test",
            producer_version=1,
            step_id="write",
            provenance="run",
        )

    first_key = f"unmanaged-v1-{token}"
    first = publication(first_key)
    catalog.register_output_idempotent(
        first_key, name="unmanaged-replay", uri=output, version="v1",
        parents=[], pipeline="canvas", lineage=first)
    assert embedded == ["v1"]

    metadb.catalog_set_metadata(
        output, folder="curated/research", tags=["robotics"],
        owner="research", description="kept across output retries")
    with metadb.session() as session:
        curated_entry = session.get(metadb.CatalogEntry, output)
        assert curated_entry is not None
        curated_updated_at = curated_entry.updated_at
        curated_children = (
            list(session.scalars(select(metadb.CatalogTag.tag).where(
                metadb.CatalogTag.uri == output).order_by(metadb.CatalogTag.tag))),
            list(session.scalars(select(metadb.CatalogColumn.column).where(
                metadb.CatalogColumn.uri == output).order_by(metadb.CatalogColumn.column))),
        )

    # The URI is mutable. Advancing its physical bytes must not make an old exact retry probe the
    # replacement artifact or rewrite the projection frozen by the first publication.
    os.unlink(output)
    duckdb.connect().execute(
        f"COPY (SELECT 2 AS id, 'replacement' AS label, 3.5 AS score) "
        f"TO '{output}' (FORMAT PARQUET)")
    # The current governance projection is not part of the output effect identity. An exact retry
    # attests the old receipt before any entry/children mutation and does not re-embed.
    replay = catalog.register_output_idempotent(
        first_key, name="unmanaged-replay", uri=output, version="v1",
        parents=[], pipeline="canvas", lineage=first)
    assert (replay.uri, replay.version) == (output, "v1")
    current = metadb.catalog_get(output)
    assert (current["version"], current["folder"], current["tags"], current["owner"]) == (
        "v1", "curated/research", ["robotics"], "research")
    with metadb.session() as session:
        replayed_entry = session.get(metadb.CatalogEntry, output)
        assert replayed_entry is not None
        assert replayed_entry.updated_at == curated_updated_at
        assert (
            list(session.scalars(select(metadb.CatalogTag.tag).where(
                metadb.CatalogTag.uri == output).order_by(metadb.CatalogTag.tag))),
            list(session.scalars(select(metadb.CatalogColumn.column).where(
                metadb.CatalogColumn.uri == output).order_by(metadb.CatalogColumn.column))),
        ) == curated_children
    assert embedded == ["v1"]

    with pytest.raises(RuntimeError, match="publication key collision"):
        catalog.register_output_idempotent(
            first_key, name="unmanaged-replay", uri=output, version="changed-v1",
            parents=[], pipeline="canvas", lineage=first)
    with pytest.raises(RuntimeError, match="publication key collision"):
        catalog.register_output_idempotent(
            first_key, name="changed-name", uri=output, version="v1",
            parents=[], pipeline="canvas", lineage=first)
    after_collision = metadb.catalog_get(output)
    assert (after_collision["name"], after_collision["version"],
            after_collision["folder"], after_collision["tags"]) == (
        "unmanaged-replay", "v1", "curated/research", ["robotics"])
    with metadb.session() as session:
        unchanged_entry = session.get(metadb.CatalogEntry, output)
        assert unchanged_entry is not None
        assert unchanged_entry.updated_at == curated_updated_at
        assert (
            list(session.scalars(select(metadb.CatalogTag.tag).where(
                metadb.CatalogTag.uri == output).order_by(metadb.CatalogTag.tag))),
            list(session.scalars(select(metadb.CatalogColumn.column).where(
                metadb.CatalogColumn.uri == output).order_by(metadb.CatalogColumn.column))),
        ) == curated_children
    assert embedded == ["v1"]

    second_key = f"unmanaged-v2-{token}"
    catalog.register_output_idempotent(
        second_key, name="unmanaged-replay", uri=output, version="v2",
        parents=[], pipeline="canvas", lineage=publication(second_key))
    os.unlink(output)
    deleted_replay = catalog.register_output_idempotent(
        first_key, name="unmanaged-replay", uri=output, version="v1",
        parents=[], pipeline="canvas", lineage=first)
    assert (deleted_replay.uri, deleted_replay.version) == (output, "v1")
    assert metadb.catalog_get(output)["version"] == "v2"
    assert embedded == ["v1", "v2"]

    # A changed request using the old key still collides from caller-owned semantics before touching
    # the now-missing artifact.
    with pytest.raises(RuntimeError, match="publication key collision"):
        catalog.register_output_idempotent(
            first_key, name="unmanaged-replay", uri=output, version="changed-v1",
            parents=[], pipeline="canvas", lineage=first)

    metadb.catalog_delete_entry(output)
    tombstone_replay = catalog.register_output_idempotent(
        first_key, name="unmanaged-replay", uri=output, version="v1",
        parents=[], pipeline="canvas", lineage=first)
    assert (tombstone_replay.uri, tombstone_replay.version) == (output, "v1")
    assert metadb.catalog_get(output) is None
    assert embedded == ["v1", "v2"]


def test_concurrent_unmanaged_publishers_return_the_winning_observed_version(
        tmp_path, monkeypatch):
    import concurrent.futures
    import threading
    import uuid

    from sqlalchemy import delete

    from hub import metadb
    from hub.models import ColumnSchema, LineagePublication
    from hub.plugins.catalog import InMemoryCatalog

    token = uuid.uuid4().hex
    uri = f"mem://unmanaged-race/{token}"
    event_key = f"unmanaged-race-{token}"
    probes_ready = threading.Barrier(2)

    class Adapter:
        def __init__(self, marker: str):
            self.marker = marker

        def schema(self, _uri: str) -> list[ColumnSchema]:
            probes_ready.wait(timeout=5)
            return [ColumnSchema(name=self.marker, type="INTEGER")]

        @staticmethod
        def count(_uri: str) -> int:
            return 1

        def fingerprint(self, _uri: str) -> str:
            return self.marker

    adapters = [Adapter("winner-a"), Adapter("winner-b")]
    catalogs = [
        InMemoryCatalog(str(tmp_path), lambda _uri, adapter=adapter: adapter)
        for adapter in adapters
    ]
    embedded: list[str | None] = []
    for catalog in catalogs:
        monkeypatch.setattr(catalog, "_embed_one", lambda table: embedded.append(table.version))
    lineage = LineagePublication(
        idempotency_key=event_key,
        run_id=f"run-{token}",
        producer="unmanaged-race-test",
        producer_version=1,
        step_id="write",
        provenance="run",
    )

    def publish(catalog: InMemoryCatalog):
        return catalog.register_output_idempotent(
            event_key, name="unmanaged-race", uri=uri,
            parents=[], pipeline="canvas", lineage=lineage,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            receipts = [future.result(timeout=10) for future in (
                pool.submit(publish, catalogs[0]),
                pool.submit(publish, catalogs[1]),
            )]
        with metadb.session() as session:
            event = session.get(metadb.CatalogPublicationEvent, event_key)
            assert event is not None
            persisted_version = event.version
        assert persisted_version is not None
        assert {(receipt.uri, receipt.version) for receipt in receipts} == {
            (uri, persisted_version)}
        assert metadb.catalog_get(uri)["version"] == persisted_version
        assert embedded == [persisted_version]
    finally:
        metadb.catalog_delete_entry(uri)
        with metadb.session() as session:
            session.execute(delete(metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.event_key == event_key))
            session.execute(delete(metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.effect_type == "lineage",
                metadb.CatalogPublicationEvent.uri == uri))


def test_catalog_usage_counts_runs_and_deduplicates_one_publication_event(tmp_path, monkeypatch):
    import duckdb

    from hub import metadb
    from hub.backends import DurableCatalogPublisher
    from hub.deps import Deps
    from hub.models import LineagePublication

    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    assert isinstance(d.catalog, DurableCatalogPublisher)
    source = str(tmp_path / "source.parquet")
    output = str(tmp_path / "output.parquet")
    output_two = str(tmp_path / "output-two.parquet")
    event = lambda suffix: f"catalog-usage-{os.urandom(8).hex()}-{suffix}"  # noqa: E731
    missing_key = event("missing-lineage")
    attempt_a_one = event("attempt-a-write-one")
    attempt_a_two = event("attempt-a-write-two")
    attempt_a_usage = event("attempt-a-usage")
    attempt_b_write = event("attempt-b-write")
    attempt_b_usage = event("attempt-b-usage")
    attempt_c_usage = event("attempt-c-usage")
    duckdb.connect().execute(f"COPY (SELECT 1 AS id) TO '{source}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT 2 AS id) TO '{output}' (FORMAT PARQUET)")
    duckdb.connect().execute(f"COPY (SELECT 3 AS id) TO '{output_two}' (FORMAT PARQUET)")
    d.catalog.register_output(name="source", uri=source, parents=[])

    def publication(key: str, run_id: str, step_id: str) -> LineagePublication:
        return LineagePublication(
            idempotency_key=key, run_id=run_id, producer="durable-catalog-test",
            producer_version=1, step_id=step_id, provenance="run",
        )

    with pytest.raises(ValueError, match="requires lineage identity"):
        d.catalog.register_output_idempotent(
            missing_key, name="output", uri=output,
            parents=[source], pipeline="canvas",
        )
    assert metadb.catalog_get(output) is None

    receipt = d.catalog.register_output_idempotent(
        attempt_a_one, name="output", uri=output, parents=[source], pipeline="canvas",
        lineage=publication(attempt_a_one, "attempt-a", "write-one"),
    )
    assert receipt.idempotency_key == attempt_a_one and receipt.uri == output
    d.catalog.register_output_idempotent(
        attempt_a_two, name="output-two", uri=output_two,
        parents=[source], pipeline="canvas",
        lineage=publication(attempt_a_two, "attempt-a", "write-two"),
    )
    assert metadb.catalog_get(source)["usage"] == 0, "output count is not run popularity"
    d.catalog.record_usage_idempotent(attempt_a_usage, [source, source])
    d.catalog.record_usage_idempotent(attempt_a_usage, [source])
    assert metadb.catalog_get(source)["usage"] == 1, "one multi-sink run counts its parent once"

    d.catalog.register_output_idempotent(
        attempt_b_write, name="output", uri=output, parents=[source], pipeline="canvas",
        lineage=publication(attempt_b_write, "attempt-b", "write"),
    )
    d.catalog.record_usage_idempotent(attempt_b_usage, [source])
    assert metadb.catalog_get(source)["usage"] == 2, "two real runs sharing one lineage edge count twice"
    pairs = {(row["parent"], row["child"]): row["fact_count"]
             for row in metadb.catalog_lineage_pairs() if row["parent"] == source}
    assert pairs == {(source, output): 2, (source, output_two): 1}

    original = metadb.catalog_bump_usage_once
    crashed = False

    def _commit_then_crash(event_key, uris):
        nonlocal crashed
        applied = original(event_key, uris)
        if not crashed:
            crashed = True
            raise ConnectionError("publisher crashed after usage commit")
        return applied

    monkeypatch.setattr(metadb, "catalog_bump_usage_once", _commit_then_crash)
    with pytest.raises(ConnectionError, match="after usage commit"):
        d.catalog.record_usage_idempotent(attempt_c_usage, [source])
    d.catalog.record_usage_idempotent(attempt_c_usage, [source])
    assert metadb.catalog_get(source)["usage"] == 3

    d.catalog.register_output(name="output", uri=output, parents=[source], pipeline="canvas")
    d.catalog.register_output(name="output", uri=output, parents=[source], pipeline="canvas")
    assert metadb.catalog_get(source)["usage"] == 5, "legacy/local run calls retain per-run popularity"


def test_object_output_version_bumps_on_overwrite(tmp_path, object_store_cred):
    # ARC4 output-versioning on an object store (#41 review): the object fingerprint is uri-only, so two
    # writes of identical schema+row-count but different DATA would collide to the same version. _add now
    # folds the object's size+mtime (a cheap stat) into the version, so an overwrite bumps it.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.deps import Deps
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False
        d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
        uri = "s3://bkt/out.parquet"
        with db.lock():
            # v1 and v2 share schema (id:int, label:str) + row count (1), but differ in bytes
            d.resolve_adapter(uri).write(uri, db.conn().sql("SELECT 1 AS id, 'aaaa' AS label"), "overwrite")
            a = d.catalog.register_output(name="oout", uri=uri, parents=[])
            d.resolve_adapter(uri).write(uri, db.conn().sql("SELECT 1 AS id, 'zzzzzzzzzzzzzzzz' AS label"), "overwrite")
            b = d.catalog.register_output(name="oout", uri=uri, parents=[])
        assert a.version != b.version, "an object overwrite (same schema+rows, different data) must bump the version"
    finally:
        server.stop()
        object_store_cred(None)


def test_source_csv_parse_options(tmp_path):
    # the source node can override CSV auto-detection (delimiter + header) — needed for semicolon files,
    # headerless files, etc. Blank/'auto' keeps DuckDB's sniffer.
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    p = str(tmp_path / "s.csv")
    with open(p, "w") as f:
        f.write("a;b\n1;2\n3;4\n")
    a = DuckDBAdapter()
    with db.lock():
        auto = a.scan(p)  # auto: ';' sniffed, first row treated as header → 2 data rows
        assert auto.columns == ["a", "b"] and auto.aggregate("count(*)").fetchone()[0] == 2
        opt = a.scan(p, options={"delimiter": ";", "header": "no"})  # explicit: no header → 3 data rows
        assert opt.columns == ["column0", "column1"] and opt.aggregate("count(*)").fetchone()[0] == 3


def test_overwrite_is_atomic_and_preserves_old_data_on_failure(tmp_path):
    # a failed/cancelled overwrite must NOT truncate the existing dataset: we write to a temp sibling
    # and os.replace only on success (review finding — silent data loss on re-run failure).
    import glob as _glob
    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    a = DuckDBAdapter()
    con = db.conn()
    t = str(tmp_path / "out.parquet")
    with db.lock():
        a.write(t, con.sql("SELECT 42 AS a"), "overwrite")
        old = a.scan(t).fetchall()
        with pytest.raises(Exception):  # count(*) succeeds; the scan hits error() mid-write
            a.write(t, con.sql("SELECT CASE WHEN a >= 0 THEN error('boom') END AS a FROM range(3) t(a)"), "overwrite")
        assert a.scan(t).fetchall() == old  # original data intact
        assert not _glob.glob(str(tmp_path / "*.tmp-*"))  # no leftover temp file
        a.write(t, con.sql("SELECT 7 AS a"), "overwrite")  # a real overwrite still replaces cleanly
        assert a.scan(t).fetchall() == [(7,)]
        assert not _glob.glob(str(tmp_path / "*.tmp-*"))


def test_estimate_reports_real_rows_and_gates_only_large_runs(tmp_path):
    # the estimate is grounded: it reports the REAL source-row count (or None when unknown, not a
    # fabricated 1000), carries no invented ETA, and gates only a genuinely large countable pass.
    from hub import compiler
    from hub.deps import get_deps
    from hub.models import Graph
    deps = get_deps()
    p = _seq_parquet(tmp_path, n=10)
    g = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []})
    plan = compiler.compile_plan(g, "s", deps.registry, deps.node_specs)
    r = deps.runner
    assert r.estimate(plan, None).rows is None and r.estimate(plan, None).needs_confirm is False  # unknown → no fake, no gate
    assert r.estimate(plan, 10).needs_confirm is False and r.estimate(plan, 10).rows == 10          # small known
    assert r.estimate(plan, 6_000_000).needs_confirm is True                                        # big rows, no bytes → row gate
    # the cost model gates on EITHER signal: large bytes OR a large row count (neither subsumes the other) —
    assert r.estimate(plan, 200_000, 3 << 30).needs_confirm is True       # 200k WIDE rows = ~3GB → byte gate (row count wouldn't)
    assert r.estimate(plan, 6_000_000, 20 << 20).needs_confirm is True    # 6M rows (only ~20MB) → row floor still gates
    assert r.estimate(plan, 100_000, 50 << 20).needs_confirm is False     # few rows + small bytes → trivial, no gate
    assert r.estimate(plan, 200_000, 3 << 30).bytes == 3 << 30 and "GB" in r.estimate(plan, 200_000, 3 << 30).breakdown
    # end-to-end: the endpoint returns the real count for a small source, no gate, and no ETA field
    est = client.post("/api/run/estimate", json={"graph": g.model_dump(), "targetNodeId": "s"}).json()
    assert est["rows"] == 10 and est["needsConfirm"] is False and "seconds" not in est


def test_timeout_interrupts_stuck_query_and_frees_the_lock():
    # a runaway/long DuckDB query used to keep holding the process-global lock after its wall-clock
    # budget elapsed, wedging every later preview/run. run_with_timeout now interrupts it so the
    # worker unwinds and releases the lock.
    from hub import db
    from hub.sandbox import SandboxError, run_with_timeout
    con = db.conn()

    def stuck():
        with db.lock():  # ~10^10-row cross join — far exceeds the budget unless interrupted
            con.execute("SELECT count(*) FROM range(100000000) a(x), range(200) b(y)").fetchone()

    with pytest.raises(SandboxError):
        run_with_timeout(stuck, 0.5, on_timeout=db.interrupt)
    with db.lock():  # the lock is free and the connection is usable again — not wedged
        assert con.execute("SELECT 42").fetchone() == (42,)


def test_run_scope_does_not_hold_the_global_lock():
    # a run/preview now runs on its OWN cursor (db.run_scope), so it must NOT hold the process-global
    # lock — another thread can take db.lock() while a scope is mid-work. Before, a whole run held it.
    import threading
    from hub import db
    in_scope = threading.Event(); release = threading.Event()

    def worker():
        with db.run_scope() as scope:
            scope.con.execute("SELECT 1").fetchone()
            in_scope.set()
            release.wait(2.0)  # keep the scope open while the main thread tries the lock

    t = threading.Thread(target=worker, daemon=True); t.start()
    try:
        assert in_scope.wait(2.0)
        got = db.lock().acquire(timeout=1.0)  # must be immediately free — the scope doesn't hold it
        assert got, "a run_scope must not hold the process-global lock"
        db.lock().release()
    finally:
        release.set(); t.join(2.0)


def test_run_scope_failure_does_not_wedge_the_next_scope():
    # a failed statement aborts only THIS scope's cursor transaction; a fresh scope is unaffected
    # (the old shared connection would stay wedged with "current transaction is aborted").
    from hub import db
    with db.run_scope() as s1:
        with pytest.raises(Exception):
            s1.con.execute("SELECT * FROM does_not_exist_xyz").fetchone()
    with db.run_scope() as s2:
        assert s2.con.execute("SELECT 99").fetchone() == (99,)


def test_run_scope_tracks_views_on_the_scope_not_globally():
    # temp views minted inside a scope are tracked on the scope (dropped on its own cursor at exit),
    # never leaked to the global _created_views set — so one run's cleanup can't drop another's views.
    from hub import db
    before = set(db._created_views)
    with db.run_scope() as s:
        v = db.unique_view("t")
        assert v in s.views
    assert v not in db._created_views
    assert set(db._created_views) == before


def test_spill_names_are_unique_across_processes(tmp_path):
    # Independent kernels commonly share DP_SPILL_DIR. Both used to start at dp_sec_1.parquet, so
    # one run could overwrite or delete another kernel's spill file.
    import subprocess
    import sys

    spill_root = tmp_path / "shared-spill"
    env = {**os.environ, "DP_SPILL_DIR": str(spill_root)}
    code = (
        "import os\n"
        "from hub import db\n"
        "from hub.executors.engine import _spill_root\n"
        "print(os.path.join(_spill_root(), 'section', db.unique_view('sec') + '.parquet'))\n"
    )
    paths = [
        subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()
        for _ in range(2)
    ]

    assert len(set(paths)) == 2
    assert all(os.path.dirname(path) == str(spill_root / "section") for path in paths)
    assert all(os.path.basename(path).startswith("dp_sec_") for path in paths)


def test_metric_over_transform_upstream_not_previewable():
    # a metric whose upstream has a Python transform must refuse preview, not spill all rows (finding #6)
    code = "def fn(row):\n    row['w2'] = row['width'] * 2\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [E("src", "xf"), E("xf", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")


def test_agent_status_and_fallback_without_key(monkeypatch):
    # with no provider key (agent_model defaults to anthropic/*) the endpoint reports unavailable
    # and POST returns available:false (frontend then falls back to the offline planner) — never 500
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = client.get("/api/agent").json()
    assert st["available"] is False and st["reason"]
    act = client.post("/api/agent", json={"outcome": "sample images", "graph": {"nodes": [], "edges": []}}).json()
    assert act["available"] is False


def test_agent_builds_graph_via_tool_loop():
    # Drive run_agent with a Pydantic AI FunctionModel — no network, fully offline, and it exercises
    # the REAL add_node/connect dispatch + layout through pydantic-ai's actual tool loop. Ids are
    # deterministic (source_a1, filter_a2); the final text output becomes the summary (no finish tool).
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub.agent import run_agent

    steps = [
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": _uri("images")}}),
        ToolCallPart(tool_name="add_node", args={"kind": "filter", "config": {"predicate": "is_valid = true"}}),
        ToolCallPart(tool_name="connect", args={"source_id": "source_a1", "target_id": "filter_a2"}),
        TextPart("Built source -> filter."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("filter images where valid", {"nodes": [], "edges": []}, get_deps(),
                    model=FunctionModel(fn))
    kinds = [n["type"] for n in out["graph"]["nodes"]]
    assert kinds == ["source", "filter"]
    assert len(out["graph"]["edges"]) == 1
    assert out["summary"] == "Built source -> filter."
    xs = [n["position"]["x"] for n in out["graph"]["nodes"]]
    assert xs[0] != xs[1]  # layout gave distinct columns (source col 0, filter col 1)
    assert [t["tool"] for t in out["transcript"]] == ["add_node", "add_node", "connect"]


def test_agent_join_hints_and_validate_tools():
    # the LLM does declarative selection by calling OUR tools for ground truth: list_catalog (now with
    # primary-key candidates), join_hints (measured cardinality), validate (typed-wire + fan-out check).
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub.agent import run_agent
    ev, img = _uri("events"), _uri("images")
    steps = [
        ToolCallPart(tool_name="list_catalog", args={}),
        ToolCallPart(tool_name="join_hints", args={"left_uri": img, "right_uri": ev}),
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": img}}),
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": ev}}),
        ToolCallPart(tool_name="add_node", args={"kind": "join", "config": {"on": "id"}}),
        ToolCallPart(tool_name="connect", args={"source_id": "source_a1", "target_id": "join_a3", "target_handle": "a"}),
        ToolCallPart(tool_name="connect", args={"source_id": "source_a2", "target_id": "join_a3", "target_handle": "b"}),
        ToolCallPart(tool_name="validate", args={}),
        TextPart("Joined images and events on id."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("join images and events", {"nodes": [], "edges": []}, get_deps(), model=FunctionModel(fn))
    tr = {t["tool"]: t["result"] for t in out["transcript"]}
    # list_catalog now surfaces primary-key candidates the agent joins on
    assert any(row["keys"] for row in tr["list_catalog"]["tables"])
    # join_hints gives measured cardinality: id↔id is 1:1, id↔user_id is 1:N
    cards = {tuple(s["rightColumns"]): s["cardinality"] for s in tr["join_hints"]["suggestions"]}
    assert cards.get(("id",)) == "1:1" and cards.get(("user_id",)) == "1:N"
    # validate checks the built graph: no typed-wire errors, and the join node is analyzed
    assert tr["validate"]["type_errors"] == [] and "join_a3" in tr["validate"]["joins"]


def test_agent_can_answer_without_touching_the_canvas():
    # No plan/build mode: the model may just reply in text (no mutating tools). The graph comes back
    # unchanged with an empty transcript, so the frontend leaves the canvas alone.
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub.agent import run_agent

    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Sample, then filter on is_valid — want me to build it?")])

    g = {"nodes": [{"id": "s1", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {}}}], "edges": []}
    out = run_agent("how would I clean this?", g, get_deps(), model=FunctionModel(fn))
    assert out["transcript"] == []                             # no tools called at all
    assert [n["id"] for n in out["graph"]["nodes"]] == ["s1"]  # canvas untouched
    assert out["summary"].startswith("Sample")                # the text reply is the summary


def test_agent_status_honors_explicit_api_key(monkeypatch):
    # an explicit DP_AGENT_API_KEY override must make the agent available even with no env-var key
    # (regression: status previously only checked agent_base_url and mis-reported unavailable)
    from hub.agent import agent_status
    from hub.settings import settings
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "agent_api_key", "sk-explicit-override")
    assert agent_status()["available"] is True


def test_agent_recovers_from_tool_error_and_summarizes():
    # A tool that returns an {"error": ...} dict (here: connect before any node exists) must NOT crash
    # the run — the model sees the error, recovers, and finishes with a plain-text summary. Also proves
    # the failed connect left no dangling edge and was recorded in the transcript.
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub.agent import run_agent

    steps = [
        ToolCallPart(tool_name="connect", args={"source_id": "nope", "target_id": "nah"}),  # error, no crash
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": _uri("images")}}),
        TextPart("Recovered and added a source."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("build something", {"nodes": [], "edges": []}, get_deps(), model=FunctionModel(fn))
    assert [n["type"] for n in out["graph"]["nodes"]] == ["source"]
    assert out["graph"]["edges"] == []  # the failed connect added no dangling edge
    assert out["summary"] == "Recovered and added a source."
    assert any(t["tool"] == "connect" and "error" in t["result"] for t in out["transcript"])


def test_plugin_node_lowering():
    # simulate a plugin registering a typed node via the SDK contract
    from hub.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="upper_fmt", title="upper", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="format")])
    def build(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "format")
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM input')
    deps.node_specs[spec.kind] = spec
    deps.node_builders[spec.kind] = build

    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("up", "upper_fmt", {"column": "format"}),
    ], "edges": [E("src", "up")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "up", "k": 5}).json()
    assert not r["notPreviewable"]
    assert all(row["format"] in ("PNG", "JPG") for row in r["rows"])


# --------------------------------------------------------------------------- #
# Metadata DB: users, per-user canvases (multi-file), settings
# --------------------------------------------------------------------------- #
def test_me_defaults_to_local_user():
    me = client.get("/api/me").json()
    assert me["id"] == "local"
    # UX-01: /me carries capabilities so the UI can hide controls a user can't use. Open single-user
    # mode → the local user can manage global settings.
    assert "global_settings" in me.get("capabilities", [])


def test_users_create_and_list():
    before = {u["id"] for u in client.get("/api/users").json()}
    created = client.post("/api/users", json={"name": "Alice", "email": "a@x.io"}).json()
    assert created["name"] == "Alice"
    ids = {u["id"] for u in client.get("/api/users").json()}
    assert created["id"] in ids and "local" in ids and created["id"] not in before


def test_kernel_child_env_is_allowlisted_but_keeps_auth_mode(monkeypatch):
    # The long-lived kernel still owns DB-backed lease/run-state writes and data access, but it must not
    # inherit unrelated hub control/provider secrets. DP_AUTH_MODE preserves its FS/path confinement.
    from hub import auth, kernel_backend
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)
    monkeypatch.setenv("DP_AUTH_PASSWORD", "bootstrap-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("UNRELATED_DEPLOY_TOKEN", "control-secret")
    monkeypatch.setenv("DP_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t-key")
    env = kernel_backend._kernel_child_env()
    assert "DP_AUTH_SECRET" not in env          # the forgeable signing secret is stripped
    assert not {"DP_AUTH_PASSWORD", "OPENAI_API_KEY", "UNRELATED_DEPLOY_TOKEN"} & env.keys()
    assert env["DP_AUTH_MODE"] == "1"           # but the auth-mode signal survives
    assert env["DP_DATABASE_URL"] and env["AWS_SECRET_ACCESS_KEY"]  # explicit residual capabilities
    # The signal alone keeps workload confinement enabled, but is never usable signing material. The
    # kernel child does not import hub.main, so the hub-only startup guard does not block this process.
    monkeypatch.delenv("DP_AUTH_SECRET")
    monkeypatch.setenv("DP_AUTH_MODE", "1")
    assert auth.auth_enabled() is True and auth._secret() == ""
    assert auth.verify("local.0.9999999999.forged") is None
    with pytest.raises(RuntimeError, match="cannot sign a session"):
        auth.sign("local")


@pytest.mark.parametrize("raw_secret", [None, "", "   "])
def test_auth_mode_without_a_signing_secret_fails_closed(monkeypatch, raw_secret):
    """DP_AUTH_MODE is a workload marker, not an implicit public HMAC key."""
    import hashlib
    import hmac

    from hub import auth

    monkeypatch.setenv("DP_AUTH_MODE", "1")
    if raw_secret is None:
        monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    else:
        monkeypatch.setenv("DP_AUTH_SECRET", raw_secret)
    payload = "local.0.9999999999"
    forged = f"{payload}.{hmac.new((raw_secret or '').encode(), payload.encode(), hashlib.sha256).hexdigest()}"

    assert auth.auth_enabled() is True  # confinement remains enabled for a workload child
    assert auth.verify(forged) is None   # an empty/blank key can never authenticate a request
    with pytest.raises(RuntimeError, match="cannot sign a session"):
        auth.sign("local")
    with pytest.raises(RuntimeError, match="cannot authenticate hub sessions"):
        auth.reject_weak_secret()


@pytest.mark.parametrize("raw_secret", ["", "   "])
def test_blank_secret_without_auth_mode_remains_open_local_mode(monkeypatch, raw_secret):
    """An empty optional env value is still unconfigured; only DP_AUTH_MODE opts into auth."""
    from hub import auth

    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    monkeypatch.setenv("DP_AUTH_SECRET", raw_secret)

    assert auth.auth_enabled() is False
    auth.reject_weak_secret()
    assert auth.verify("local.0.9999999999.forged") is None
    with pytest.raises(RuntimeError, match="cannot sign a session"):
        auth.sign("local")


@pytest.mark.parametrize("raw_secret", [None, ""])
def test_hub_startup_rejects_auth_mode_without_a_signing_secret(tmp_path, raw_secret):
    """Importing the web app is hub startup; fail before opening its metadata database."""
    import subprocess
    import sys

    env = dict(os.environ)
    if raw_secret is None:
        env.pop("DP_AUTH_SECRET", None)
    else:
        env["DP_AUTH_SECRET"] = raw_secret
    env["DP_AUTH_MODE"] = "1"
    env["DP_WORKSPACE"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "import hub.main"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "cannot authenticate hub sessions without" in result.stderr
    assert "non-empty DP_AUTH_SECRET" in result.stderr


def test_cli_rejects_marker_only_auth_before_creating_metadata(tmp_path):
    """The normal `dataplay` path must fail before seed, DB migration, or dependency setup."""
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    env = dict(os.environ)
    env.pop("DP_AUTH_SECRET", None)
    env.pop("DP_DATABASE_URL", None)
    env["DP_AUTH_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hub.cli",
            "--workspace",
            str(workspace),
            "--no-open",
            "--no-seed",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "cannot authenticate hub sessions without" in result.stderr
    assert not (workspace / "dataplay.db").exists()


def test_authenticated_principal_deletion_never_falls_back_to_local_admin(monkeypatch):
    """Deleting a user between signature verification and principal resolution must return 401."""
    from fastapi import HTTPException

    from hub import auth, metadb
    from hub.security import current_identity

    monkeypatch.setenv("DP_AUTH_SECRET", "strict-principal-secret")
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    uid = "principal_deleted_during_auth"
    with metadb.session() as s:
        existing = s.get(metadb.User, uid)
        if existing is not None:
            s.delete(existing)
    with metadb.session() as s:
        s.add(metadb.User(id=uid, name="Deleted during auth"))
    token = auth.sign(uid)
    original_verify = auth.verify_claims

    def verify_then_delete(candidate):
        claims = original_verify(candidate)
        assert claims is not None
        with metadb.session() as s:
            user = s.get(metadb.User, claims.user_id)
            if user is not None:
                s.delete(user)
        return claims

    monkeypatch.setattr(auth, "verify_claims", verify_then_delete)
    with pytest.raises(HTTPException) as exc:
        current_identity(x_dp_user=None, dp_session=token)
    assert exc.value.status_code == 401
    assert metadb.user_token_epoch("local") is not None  # the fallback admin still exists but was not selected


def test_canvas_pip_deps_default_off_under_auth(monkeypatch):
    # P0-SEC-01 / SEC-04: per-canvas pip installs (arbitrary code + egress) default OFF in auth mode;
    # an explicit DP_CANVAS_PIP_DEPS always wins; open local tool defaults ON.
    from hub.settings import _canvas_pip_deps_default
    for k in ("DP_CANVAS_PIP_DEPS", "DP_AUTH_SECRET", "DP_AUTH_MODE"):
        monkeypatch.delenv(k, raising=False)
    assert _canvas_pip_deps_default() is True             # open tool → on
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)
    assert _canvas_pip_deps_default() is False            # auth/production → off by default
    monkeypatch.setenv("DP_CANVAS_PIP_DEPS", "1")
    assert _canvas_pip_deps_default() is True             # explicit opt-in wins


def test_per_user_password_is_not_a_skeleton_key(monkeypatch):
    # with auth on, a password authenticates ONLY its own user — no shared/skeleton password.
    from hub import auth
    from hub.metadb import User, session
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    bid = "bella_u"
    with session() as s:  # provision directly (create_user is now gated — see the auth-boundary test)
        if s.get(User, bid) is None:
            s.add(User(id=bid, name="Bella", password_hash=auth.hash_password("pwBella")))
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"userId": bid, "password": "pwBella"}).status_code == 200
    assert client.post("/api/auth/login", json={"userId": bid, "password": "otherpw"}).status_code == 401  # not a skeleton key
    assert client.post("/api/auth/login", json={"userId": bid, "password": "wrong"}).status_code == 401
    # self-service rotation: old must match; afterwards only the new password works
    client.cookies.clear()
    client.post("/api/auth/login", json={"userId": bid, "password": "pwBella"})
    assert client.post("/api/auth/password", json={"oldPassword": "x", "newPassword": "pwNew12"}).status_code == 403
    assert client.post("/api/auth/password", json={"oldPassword": "pwBella", "newPassword": "pwNew12"}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"userId": bid, "password": "pwBella"}).status_code == 401
    assert client.post("/api/auth/login", json={"userId": bid, "password": "pwNew12"}).status_code == 200
    client.cookies.clear()


def test_first_admin_bootstrap_from_env_password(monkeypatch):
    # P0-DEPLOY-01: a fresh auth-on deploy needs a first-admin credential before application replicas
    # start. The one-shot migration consumes DP_AUTH_PASSWORD and closes that bootstrap lockout.
    from hub import metadb
    client.cookies.clear()
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")     # auth on
    metadb.set_user_password("local", None)            # simulate the fresh/pre-bootstrap admin
    try:
        # no DP_AUTH_PASSWORD → the deadlock: the seeded admin can't authenticate at all
        assert client.post("/api/auth/login", json={"userId": "local", "password": "anything"}).status_code == 401
        # Run the migration-owned bootstrap again (idempotent at the current schema head).
        monkeypatch.setenv("DP_AUTH_PASSWORD", "bootstrap-pw-123")
        metadb.migrate_db()
        assert "DP_AUTH_PASSWORD" not in os.environ  # one-time input, not ambient runtime configuration
        assert metadb.is_admin("local")
        assert client.post("/api/auth/login", json={"userId": "local", "password": "bootstrap-pw-123"}).status_code == 200
        assert client.get("/api/canvas").status_code == 200  # a gated route is now reachable
    finally:
        client.cookies.clear()
        metadb.set_user_password("local", None)        # restore open-mode-friendly state for other tests


def test_api_routes_require_auth_when_enabled(monkeypatch):
    # SECURE DEFAULT: with auth enabled, the whole /api surface needs a session — the high-impact routes
    # (/run code-exec, /data file-read, POST /users self-registration) used to be wide open. Only the
    # login roster + auth status/login stay public.
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    client.cookies.clear()
    g = {"id": "x", "version": 1, "nodes": [N("s", "source", {"uri": "/etc/hosts"})], "edges": []}
    assert client.post("/api/run", json={"graph": g, "targetNodeId": "s"}).status_code == 401       # no unauth code-exec
    assert client.post("/api/data/sample", json={"uri": "/etc/passwd", "k": 5}).status_code == 401   # no unauth file-read
    assert client.post("/api/users", json={"name": "Mallory", "password": "x"}).status_code == 401    # no self-registration
    assert client.post("/api/catalog/register", json={"uri": "/etc/hosts"}).status_code == 401
    # a representative route from EACH router module must be gated (proves the include-time gate survived
    # the split into routers/{catalog,runs,workspace}): catalog (/data,/catalog), runs (/run), workspace
    # (/users,/canvas,/settings — /settings holds object-store secrets so it must never be reachable unauth)
    assert client.get("/api/canvas").status_code == 401
    assert client.get("/api/settings").status_code == 401
    # but the login screen's public surface still works pre-session
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/users").status_code == 200 and all("email" not in u for u in client.get("/api/users").json())
    client.cookies.clear()


def test_local_dataset_path_confined_in_auth_mode(monkeypatch):
    # in a multi-user (auth) deployment a source / register must not read an arbitrary local file —
    # local paths are confined to the workspace / data dir / DP_DATASET_ROOTS. Open mode = trusted, no confinement.
    import os as _os
    from hub import paths
    from hub.plugins.adapters import path_of
    from hub.settings import settings
    inside = _os.path.join(settings.data_dir, "some_dataset.parquet")
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    paths.ensure_local_uri_allowed("/etc/passwd")           # open mode → no confinement
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    paths.ensure_local_uri_allowed(inside)                  # inside a root → allowed
    paths.ensure_local_uri_allowed("s3://bucket/x.parquet")  # object-store → not a local path → allowed
    for escaped in ("/etc/passwd", "file:///etc/passwd", "FILE:///etc/passwd", "FiLe:///etc/passwd"):
        with pytest.raises(PermissionError):
            paths.ensure_local_uri_allowed(escaped)         # scheme case cannot bypass the same root check
    # urlsplit calls the drive letter a URI scheme; DuckDB calls it a local filename. The shared parser
    # must follow the executable adapter boundary so Windows cannot skip confinement.
    monkeypatch.setattr(paths, "allowed_roots", lambda: [_os.path.realpath("/definitely-allowed")])
    for drive_path in (r"C:\data\secret.csv", "C:/data/secret.csv"):
        expected_drive = drive_path.replace("/", "\\") if _os.name == "nt" else drive_path
        assert paths.local_path(drive_path) == expected_drive
        assert path_of(drive_path) == expected_drive
        with pytest.raises(PermissionError):
            paths.ensure_local_uri_allowed(drive_path)
    win_file_uri = "file:///C:/data/secret.csv"
    expected = r"C:\data\secret.csv" if _os.name == "nt" else "/C:/data/secret.csv"
    assert paths.local_path(win_file_uri) == expected


def test_canvas_crud_is_per_user():
    doc = {"id": "cv1", "name": "My Canvas", "version": 3, "nodes": [], "edges": []}
    r = client.put("/api/canvas/cv1", json=doc).json()
    assert r["ok"]
    listing = client.get("/api/canvas").json()
    assert any(c["id"] == "cv1" and c["name"] == "My Canvas" and c["version"] == 3 for c in listing)
    assert client.get("/api/canvas/cv1").json()["name"] == "My Canvas"
    # a different user cannot see it
    other = client.post("/api/users", json={"name": "Bob"}).json()["id"]
    assert client.get("/api/canvas", headers={"X-DP-User": other}).json() == []
    assert client.get("/api/canvas/cv1", headers={"X-DP-User": other}).status_code == 404
    # delete
    client.delete("/api/canvas/cv1")
    assert client.get("/api/canvas/cv1").status_code == 404


def test_canvas_create_reports_new_insert_without_claiming_an_existing_id():
    canvas_id = "cv-create-evidence"
    client.delete(f"/api/canvas/{canvas_id}")
    original = {"id": canvas_id, "name": "Original", "version": 1, "nodes": [], "edges": []}

    created = client.post("/api/canvas", json=original)
    assert created.status_code == 200
    assert created.json() == {"ok": True, "id": canvas_id, "created": True}

    # Retrying the same client-generated ID is not evidence of ownership and must not mutate the row.
    collision = client.post("/api/canvas", json={**original, "name": "Replacement"})
    assert collision.status_code == 200
    assert collision.json() == {"ok": True, "id": canvas_id, "created": False}
    assert client.get(f"/api/canvas/{canvas_id}").json()["name"] == "Original"

    # The same evidence contract holds across users: a collision neither transfers ownership nor grants access.
    other = client.post("/api/users", json={"name": "Canvas collision user"}).json()["id"]
    foreign_collision = client.post(
        "/api/canvas", json={**original, "name": "Foreign replacement"},
        headers={"X-DP-User": other},
    )
    assert foreign_collision.json() == {"ok": True, "id": canvas_id, "created": False}
    assert client.get(f"/api/canvas/{canvas_id}", headers={"X-DP-User": other}).status_code == 404
    assert client.get(f"/api/canvas/{canvas_id}").json()["name"] == "Original"
    client.delete(f"/api/canvas/{canvas_id}")


def test_canvas_version_history_and_restore():
    # every save keeps a (throttled) snapshot; a bad edit is recoverable by restoring an earlier one.
    a = {"id": "cvh", "name": "V", "version": 1, "nodes": [{"id": "n1", "type": "source",
         "position": {"x": 0, "y": 0}, "data": {"title": "n1", "config": {}}}], "edges": []}
    assert client.put("/api/canvas/cvh", json=a).json()["ok"]  # first save → snapshot A
    b = {**a, "version": 2, "nodes": []}                        # a "bad edit" that deletes the node
    assert client.put("/api/canvas/cvh", json=b).json()["ok"]  # doc now empty (auto-snapshot throttled)
    assert client.get("/api/canvas/cvh").json()["nodes"] == []  # confirm the bad state persisted
    versions = client.get("/api/canvas/cvh/versions").json()
    assert len(versions) >= 1  # snapshot A is there to restore
    restored = client.post("/api/canvas/cvh/restore", json={"version_id": versions[-1]["id"]}).json()
    assert [n["id"] for n in restored["doc"]["nodes"]] == ["n1"]  # node is back
    assert [n["id"] for n in client.get("/api/canvas/cvh").json()["nodes"]] == ["n1"]  # persisted
    # the restore itself snapshotted the pre-restore (empty) state, so it's undoable too
    assert any(v["label"] == "before restore" for v in client.get("/api/canvas/cvh/versions").json())
    client.delete("/api/canvas/cvh")


def test_settings_global_and_user_scope():
    client.put("/api/settings", json={"scope": "global", "key": "agentModel", "value": "openai/gpt-4o"})
    u = client.post("/api/users", json={"name": "Carol"}).json()["id"]
    client.put("/api/settings", json={"scope": "user", "key": "theme", "value": "dark"}, headers={"X-DP-User": u})
    g = client.get("/api/settings").json()
    assert g["global"]["agentModel"] == "openai/gpt-4o"
    # Carol sees her user setting; the default user does not
    assert client.get("/api/settings", headers={"X-DP-User": u}).json()["user"].get("theme") == "dark"
    assert client.get("/api/settings").json()["user"].get("theme") is None


def test_agent_activates_from_selected_cred(monkeypatch):
    # A selected Agent Cred makes the Agent available without a provider key in the process environment.
    from hub import metadb

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DP_FIXTURE_AGENT_FROM_UI", "sk-from-ui")
    assert client.get("/api/agent").json()["available"] is False
    cred_id = metadb.cred_upsert(
        None, "UI Agent", "agent", {"apiKey": "env:DP_FIXTURE_AGENT_FROM_UI"})["id"]
    metadb.set_setting("agentCredId", cred_id, "global")
    try:
        assert client.get("/api/agent").json()["available"] is True
    finally:
        metadb.set_setting("agentCredId", "", "global")
        metadb.cred_delete(cred_id)


# --------------------------------------------------------------------------- #
# Meta-programming: a `section` = a driver script over contained nodes (bounded control flow)
# --------------------------------------------------------------------------- #
def test_failed_run_does_not_wedge_later_previews(tmp_path):
    # a run against a missing file fails and aborts DuckDB's implicit transaction; if that's not
    # rolled back, EVERY later query on the shared connection errors ("transaction is aborted").
    bad = {"id": "c1", "version": 1, "nodes": [N("s", "source", {"uri": "does-not-exist.parquet"})], "edges": []}
    r = client.post("/api/run", json={"graph": bad, "targetNodeId": "s"}).json()
    assert _poll(r["runId"])["status"] == "failed"
    # a subsequent preview of a GOOD source must still work (the aborted transaction was cleared)
    p = _seq_parquet(tmp_path)
    good = {"id": "c1", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []}
    pv = client.post("/api/run/preview", json={"graph": good, "nodeId": "s", "k": 10}).json()
    assert not pv.get("error"), pv.get("reason")
    assert len(pv["rows"]) == 10


def test_preview_paginates_with_offset(tmp_path):
    p = _seq_parquet(tmp_path, n=500)  # column v = 0..499
    g = {"id": "c1", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []}
    pg0 = client.post("/api/run/preview", json={"graph": g, "nodeId": "s", "k": 10, "offset": 0}).json()
    pg1 = client.post("/api/run/preview", json={"graph": g, "nodeId": "s", "k": 10, "offset": 10}).json()
    assert [r["v"] for r in pg0["rows"]] == list(range(0, 10))
    assert [r["v"] for r in pg1["rows"]] == list(range(10, 20))  # offset advances the window
    assert pg0["rowCount"] is None and pg1["rowCount"] is None
    assert pg0["completeness"] == pg1["completeness"] == "sample"
    assert pg0["rowLimit"] == pg1["rowLimit"] == 2_000
    assert pg0["limitReason"] == pg1["limitReason"] == "preview-scan"


def test_materialized_artifact_sample_paginates_with_offset(tmp_path):
    p = _seq_parquet(tmp_path, n=105)  # column v = 0..104
    pg0 = client.post("/api/data/sample", json={"uri": p, "k": 50, "offset": 0}).json()
    pg1 = client.post("/api/data/sample", json={"uri": p, "k": 50, "offset": 50}).json()
    last = client.post("/api/data/sample", json={"uri": p, "k": 50, "offset": 100}).json()
    assert [r["v"] for r in pg0["rows"]] == list(range(50)) and pg0["hasMore"] is True
    assert [r["v"] for r in pg1["rows"]] == list(range(50, 100)) and pg1["hasMore"] is True
    assert [r["v"] for r in last["rows"]] == list(range(100, 105)) and last["hasMore"] is False
    assert pg0["rowCount"] == pg1["rowCount"] == last["rowCount"] == 105
    assert pg0["completeness"] == pg1["completeness"] == last["completeness"] == "page"
    assert client.post("/api/data/sample", json={"uri": p, "k": 50, "offset": -1}).status_code == 400
    os.remove(p)
    missing = client.post("/api/data/sample", json={"uri": p, "k": 50, "offset": 0})
    assert missing.status_code == 410 and "missing or expired" in missing.json()["detail"]


def test_preview_has_more_marks_the_last_page(tmp_path):
    p = _seq_parquet(tmp_path, n=100)  # exactly 2 pages of 50
    g = {"id": "c1", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []}
    a = client.post("/api/run/preview", json={"graph": g, "nodeId": "s", "k": 50, "offset": 0}).json()
    b = client.post("/api/run/preview", json={"graph": g, "nodeId": "s", "k": 50, "offset": 50}).json()
    assert len(a["rows"]) == 50 and a["hasMore"] is True     # page 0 → there IS a next page
    assert len(b["rows"]) == 50 and b["hasMore"] is False    # page 1 is the last — no phantom empty page


def test_subprocess_runner_executes_in_isolation(tmp_path):
    # the "local-subprocess" backend runs the job in a separate OS process (real isolation)
    from hub import metadb
    metadb.set_setting("backend", "local-subprocess", "global")
    try:
        p = _seq_lance(tmp_path, n=40)
        _ensure_run_canvas("c")
        g = {"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": p}),
            N("wr", "write", {"name": "subproc_out", "writeMode": "overwrite"}),
        ], "edges": [E("src", "wr")]}
        r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
        st = _poll(r["runId"], tries=400)
        assert st["status"] == "done", st.get("error")
        assert _output_field(st, "table", outcome="committed") == "subproc_out"
        assert (st["totalRows"] or st["rowsProcessed"]) == 40
        # the child wrote in its own (discarded) catalog — the parent must still register it live
        # The catalog endpoint is paginated; query the exact artifact so earlier suite outputs cannot
        # push this table off the first page and turn a registration assertion into an order-dependent
        # false failure.
        tables = client.get(
            "/api/catalog/tables",
            params={"uris": _output_field(st, "uri", outcome="committed")},
        ).json()["items"]
        assert any(t["name"] == "subproc_out" for t in tables)
        destination = _output_field(st, "uri", outcome="committed")
        with metadb.session() as session:
            facts = list(session.scalars(select(metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.destination_uri == destination,
                metadb.CatalogLineageFact.run_id == r["runId"],
            )))
        assert len(facts) == 1
        assert facts[0].source_uri == p and facts[0].attempt_id is None
        assert (facts[0].producer, facts[0].producer_version, facts[0].step_id) == ("c", 1, "wr")
    finally:
        metadb.set_setting("backend", "", "global")  # restore the default in-process runner


def test_subprocess_runner_enforces_named_schema_contract_without_hub_db_access(tmp_path):
    from hub import metadb

    metadb.save_schema_contract("subprocess_value_contract", [{"name": "v", "type": "int"}])
    metadb.set_setting("backend", "local-subprocess", "global")
    try:
        p = _seq_lance(tmp_path, n=10)
        _ensure_run_canvas("c")
        g = {"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": p}),
            N("xf", "transform", {
                "mode": "map",
                "code": "def fn(row): return row",
                "outputSchema": {"ref": "subprocess_value_contract"},
                "enforceSchema": True,
            }),
        ], "edges": [E("src", "xf")]}
        run = client.post(
            "/api/run", json={"graph": g, "targetNodeId": "xf", "confirmed": True}
        ).json()

        status = _poll(run["runId"], tries=400)

        assert status["status"] == "done", status.get("error")
        assert (status["totalRows"] or status["rowsProcessed"]) == 10
    finally:
        metadb.set_setting("backend", "", "global")


def test_subprocess_terminal_status_waits_for_parent_catalog_registration(tmp_path):
    import json
    import threading

    from hub.models import Graph, RunOutput, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    registration_started = threading.Event()
    allow_registration = threading.Event()

    class BlockingCatalog:
        entry = None

        def register_output(self, **_kwargs):
            registration_started.set()
            assert allow_registration.wait(timeout=5)
            self.entry = {
                "uri": _kwargs["uri"], "name": _kwargs["name"], "version": "v1",
            }
            return self.entry

        def get_table(self, uri):
            assert self.entry is not None and self.entry["uri"] == uri
            return self.entry

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    runner = SubprocessRunner(
        str(tmp_path / "workspace"), str(tmp_path), catalog=BlockingCatalog())
    run_id = "run_catalog_gate"
    graph = Graph.model_validate({
        "id": "c", "version": 1,
        "nodes": [N("write", "write", {"name": "result"})], "edges": [],
    })
    from hub.plugins.catalog import lineage_for_output
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", target_node_id="write", per_node=[],
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner._sink_contracts[run_id] = {"write": {
        "logical_uri": str(tmp_path / "result.parquet"),
        "published_uri": str(tmp_path / "result.parquet"),
        "name": "result", "parents": [],
        "lineage": lineage_for_output(graph, run_id, "write"),
    }}
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child",
        status="done",
        target_node_id="write",
        per_node=[],
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="committed",
            uri=str(tmp_path / "result.parquet"), table="result",
        )],
    ).model_dump()))
    runner._emit(graph, runner.runs[run_id])

    watcher = threading.Thread(target=runner._watch, args=(
        run_id, FinishedProcess(), str(status_file), str(tmp_path), graph, "write"
    ))
    watcher.start()
    assert registration_started.wait(timeout=5)

    assert runner.status(run_id).status == "running"

    allow_registration.set()
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert runner.status(run_id).status == "done"


def test_run_deadline_hard_kills_a_runaway_child(tmp_path):
    # cell-crash-isolation: a runaway cell (`while True`) in an isolated run must be HARD-KILLED at the
    # wall-clock deadline and resolve to 'failed' with a deadline message — never pin the worker forever.
    import time as _t

    from hub import compiler
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.settings import settings
    from hub.subprocess_runner import SubprocessRunner
    d = get_deps()
    p = _seq_parquet(tmp_path, n=10)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": "def fn(row):\n    while True:\n        pass"}),
        N("wr", "write", {"name": "deadline_out", "writeMode": "overwrite"}),
    ], "edges": [E("src", "xf"), E("xf", "wr")]})
    plan = compiler.compile_plan(g, "wr", d.registry, d.node_specs, d.node_ir)
    runner = SubprocessRunner(settings.workspace, settings.data_dir, catalog=d.catalog, deadline_s=2)
    st = runner.run(plan, g, "wr", "local")
    end = _t.time() + 30
    while _t.time() < end and runner.status(st.run_id).status not in ("done", "failed", "cancelled"):
        _t.sleep(0.2)
    final = runner.status(st.run_id)
    assert final.status == "failed", f"a runaway run should fail at the deadline, got {final.status}"
    assert "deadline" in (final.error or "").lower(), final.error
    assert st.run_id not in runner._procs or runner._procs[st.run_id].poll() is not None  # child reaped


def test_subprocess_region_cancel_stops_materializer_before_publish(tmp_path):
    # run_unit uses subrun's materializeUri path (the RunController handoff worker). Cancel after that path
    # reports running, then require terminal cancelled + a reaped child + no handoff artifact.
    import time as _t

    from hub.deps import get_deps
    from hub.models import Graph
    from hub.settings import settings
    from hub.subprocess_runner import SubprocessRunner

    source = _seq_parquet(tmp_path, n=5)
    graph = Graph(**{"id": "region_subprocess_cancel", "version": 1, "nodes": [
        N("src", "source", {"uri": source}),
        N("xf", "transform", {"source": "adhoc", "mode": "map",
                                  "code": "def fn(row):\n    while True:\n        pass"}),
    ], "edges": [E("src", "xf")]})
    output = str(tmp_path / "region_cancelled.parquet")
    runner = SubprocessRunner(settings.workspace, settings.data_dir,
                              catalog=get_deps().catalog, deadline_s=30)
    st = runner.run_unit(graph, "xf", output)
    deadline = _t.monotonic() + 10
    while runner.status(st.run_id).status == "queued" and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert runner.status(st.run_id).status == "running", "materializeUri child never entered its run path"
    runner.cancel(st.run_id)
    while runner.status(st.run_id).status not in ("done", "failed", "cancelled") and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert runner.status(st.run_id).status == "cancelled"
    while st.run_id in runner._procs and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert st.run_id not in runner._procs and not os.path.exists(output)


def test_subprocess_run_is_recorded_in_history(tmp_path):
    # run history must be captured for the isolated-process backend too. The PARENT records it (the
    # child disables its own on_complete to avoid a daemon-thread race that dropped records): a run
    # records exactly once — not zero (lost), not twice (double).
    import time as _t

    from hub import metadb
    from hub.metadb import Canvas, session
    cid = "cvs_subproc_hist"
    with session() as s:
        if s.get(Canvas, cid) is None:
            s.add(Canvas(id=cid, owner_id=metadb.DEFAULT_USER_ID, name="t", version=1, doc="{}", visibility="private"))
    before = len(metadb.list_runs(cid))
    metadb.set_setting("backend", "local-subprocess", "global")
    try:
        p = _seq_lance(tmp_path, n=30)
        g = {"id": cid, "version": 1, "nodes": [
            N("src", "source", {"uri": p}),
            N("wr", "write", {"filename": "hist_out.parquet", "writeMode": "overwrite"}),
        ], "edges": [E("src", "wr")]}
        r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
        assert _poll(r["runId"], tries=400)["status"] == "done"
        runs = metadb.list_runs(cid)
        for _ in range(50):  # let the parent watcher thread persist the terminal run
            runs = metadb.list_runs(cid)
            if len(runs) > before:
                break
            _t.sleep(0.1)
        assert len(runs) == before + 1, [r["status"] for r in runs]  # exactly one, no race-loss / double
        assert runs[0]["status"] == "done"
    finally:
        metadb.set_setting("backend", "", "global")


def test_lance_scan_streams_with_pushdown(tmp_path):
    # Lance scans stream into DuckDB via a scanner→RecordBatchReader (out-of-core) instead of loading
    # the whole dataset with ds.to_table(); column/limit/predicate pushdown still work.
    pytest.importorskip("lance")
    import lance
    import pyarrow as pa

    from hub import db
    from hub.plugins.adapters import LanceAdapter
    p = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": list(range(300)), "v": [i * 2 for i in range(300)]}), p)
    a = LanceAdapter()
    with db.lock():
        assert a.scan(p).aggregate("count(*)").fetchone()[0] == 300           # streamed full scan
        lim = a.scan(p, columns=["id"], limit=5)
        assert lim.columns == ["id"] and lim.fetchall() == [(0,), (1,), (2,), (3,), (4,)]  # pushdown
        assert a.scan(p, predicate="v >= 596").fetchall() == [(298, 596), (299, 598)]
        # ARC4 lance-mutable-pushdown: the adapter's OWN write must work (the old rel.record_batch was
        # broken on DuckDB 1.5.x → AttributeError on every Lance write), the predicate pushes into Lance
        # with correct filter-THEN-limit, and an unknown mode is rejected (not silently degraded to append).
        con = db.conn()
        wp = str(tmp_path / "w.lance")
        a.write(wp, con.sql("SELECT CAST(i AS BIGINT) AS id, CAST(mod(i, 3) AS BIGINT) AS cat FROM range(0, 30) r(i)"), "overwrite")
        assert a.count(wp) == 30 and a.scan(wp).aggregate("count(*)").fetchone()[0] == 30
        assert a.scan(wp, predicate="cat = 1").aggregate("count(*)").fetchone()[0] == 10
        assert a.scan(wp, predicate="cat = 1", limit=3).aggregate("count(*)").fetchone()[0] == 3  # filter, THEN limit
        a.write(wp, con.sql("SELECT CAST(99 AS BIGINT) AS id, CAST(0 AS BIGINT) AS cat"), "append")
        assert a.count(wp) == 31
        with pytest.raises(NotImplementedError, match="not supported"):
            a.write(wp, con.sql("SELECT CAST(1 AS BIGINT) AS id, CAST(0 AS BIGINT) AS cat"), "merge")
        assert a.count(wp) == 31  # the rejected write did NOT append
        # #44 review (HIGH): a DOUBLE-QUOTED identifier predicate must not be silently miscomputed by
        # Lance's datafusion dialect (which reads "col" as a string literal) — it routes to the DuckDB
        # fallback + returns correct rows. Use a reserved-word column that REQUIRES quoting.
        qp = str(tmp_path / "q.lance")
        a.write(qp, con.sql('SELECT CAST(i AS BIGINT) AS id, CAST(mod(i, 2) AS BIGINT) AS "select" FROM range(0, 10) r(i)'), "overwrite")
        assert a.scan(qp, predicate='"select" = 1').aggregate("count(*)").fetchone()[0] == 5
        proj = a.scan(qp, columns=["id"], predicate='"select" = 1')  # predicate on a PROJECTED-OUT column
        assert list(proj.columns) == ["id"] and proj.aggregate("count(*)").fetchone()[0] == 5


def test_vector_search_lance_ann_and_external_query(tmp_path):
    # vector-search uses Lance's native nearest (its index if present) on a durable full run and can query
    # by an arbitrary external vector. Preview refuses because Lance may flat-scan when no index exists.
    pytest.importorskip("lance")
    import lance
    import pyarrow as pa
    p = str(tmp_path / "vec.lance")
    vecs = [[1.0, 0, 0, 0], [0.9, 0.1, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    lance.write_dataset(pa.table({"id": list(range(5)), "embedding": pa.array(vecs, type=pa.list_(pa.float32(), 4))}), p)
    # query = row 0's vector [1,0,0,0] → itself is nearest, and a cosine _score column is exposed
    g = {"id": "cv", "version": 1, "nodes": [N("s", "source", {"uri": p}),
         N("vs", "vector-search", {"column": "embedding", "queryRow": 0, "k": 3})], "edges": [E("s", "vs")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "vs", "k": 10}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")
    _, result = _full_result(g, "vs", 10)
    assert "_score" in [c["name"] for c in result["columns"]] and result["rows"][0]["id"] == 0
    # an external query vector [0,1,0,0] → row 2 is nearest (no such row was the query)
    g2 = {"id": "cv2", "version": 1, "nodes": [N("s", "source", {"uri": p}),
          N("vs", "vector-search", {"column": "embedding", "queryVector": "[0,1,0,0]", "k": 2})], "edges": [E("s", "vs")]}
    _, result2 = _full_result(g2, "vs", 10)
    assert result2["rows"][0]["id"] == 2


def test_object_store_s3_roundtrip_and_browse(tmp_path, object_store_cred):
    # REAL object storage via DuckDB httpfs, proven end-to-end against an in-process S3 (moto server):
    # write a dataset to s3://, read it back, and browse the prefix.
    pytest.importorskip("moto")
    pytest.importorskip("flask")  # ThreadedMotoServer needs moto[server]
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db, destinations, metadb
    from hub.plugins.adapters import DuckDBAdapter

    try:  # httpfs is downloaded on first install — skip if this environment can't fetch it
        with db.lock():
            db.conn().execute("INSTALL httpfs")
            db.conn().execute("LOAD httpfs")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"httpfs unavailable: {e}")

    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False  # re-load httpfs + re-register the secret against the moto endpoint
        a = DuckDBAdapter()
        p = _seq_parquet(tmp_path, n=25)
        with db.lock():
            res = a.write("s3://bkt/data/out.parquet", db.conn().read_parquet(p), "overwrite")
            assert res["rows"] == 25
            n = a.scan("s3://bkt/data/out.parquet").aggregate("count(*) AS c").fetchone()[0]
        assert n == 25  # read back what we wrote, over the wire

        metadb.set_setting("destinations", [{"id": "b", "name": "bkt", "backend": "s3", "root": "s3://bkt"}], "global")
        br = destinations.browse(str(tmp_path), "b", "data")
        assert not br.get("error"), br.get("error")
        assert any(e["name"] == "out.parquet" for e in br["entries"])
    finally:
        server.stop()
        object_store_cred(None)  # restore the ambient credential chain for other tests


def test_object_store_concurrent_run_scopes_consume_base_published_credentials(object_store_cred):
    import threading

    from hub import db, metadb

    previous_id = metadb.get_setting("defaultObjectStoreCredId", "global", default="") or ""
    configured = {
        "endpoint": "http://example.invalid:9443", "region": "us-east-1",
        "accessKeyId": "scope-key", "secretAccessKey": "scope-secret",
    }
    try:
        # Publish an OLD committed secret first. A cursor keeps that version if replacement happens only
        # after BEGIN, so both run_scope entries must prime the NEW config before taking their snapshots.
        try:
            with db.lock():
                base = db._base_conn()
                base.execute("INSTALL httpfs")
                base.execute("LOAD httpfs")
                base.execute("DROP SECRET IF EXISTS dp_s3")
                base.execute("DROP SECRET IF EXISTS dp_gcs")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"httpfs unavailable: {exc}")
        object_store_cred({
            "endpoint": "https://old.invalid", "region": "us-west-2",
            "accessKeyId": "old-key", "secretAccessKey": "old-secret",
        })
        db._obj_store_loaded = False
        db._obj_store_secret_config = None
        db.ensure_object_store()
        object_store_cred(configured)

        scopes_ready = threading.Barrier(2)
        first_published = threading.Event()
        second_consumed = threading.Event()
        results = [None, None]
        errors = [None, None]

        def consume(index: int) -> None:
            try:
                with db.run_scope():
                    local_view = db.unique_view("object_store_scope")
                    db.conn().execute(f'CREATE TEMP VIEW "{local_view}" AS SELECT {index} AS value')
                    scopes_ready.wait(timeout=5)
                    if index == 0:
                        db.ensure_object_store()
                        first_published.set()
                        assert second_consumed.wait(timeout=5)
                    else:
                        assert first_published.wait(timeout=5)
                        db.ensure_object_store()
                        second_consumed.set()
                    resolved = db.conn().execute(
                        "SELECT name FROM which_secret('s3://bucket/key.parquet', 's3')"
                    ).fetchone()
                    detail = db.conn().execute(
                        "SELECT secret_string FROM duckdb_secrets() WHERE name = 'dp_s3'"
                    ).fetchone()
                    results[index] = (resolved, detail)
            except BaseException as exc:  # noqa: BLE001 — preserve thread assertion details
                errors[index] = exc
            finally:
                first_published.set()
                second_consumed.set()

        threads = [threading.Thread(target=consume, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == [None, None]
        for resolved, detail in results:
            assert resolved == ("dp_s3",)
            assert detail and "key_id=scope-key" in detail[0]
            assert "endpoint=example.invalid:9443" in detail[0]
    finally:
        metadb.set_setting("defaultObjectStoreCredId", previous_id, "global")
        db._obj_store_secret_config = None
        db.ensure_object_store()


def test_object_store_credential_fingerprint_tracks_static_aws_env(monkeypatch):
    from hub import db

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-one")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-one")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-one")
    monkeypatch.setenv("AWS_PROFILE", "profile-one")
    first = db._object_store_fingerprint({})

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-two")
    assert db._object_store_fingerprint({}) != first
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-one")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-two")
    assert db._object_store_fingerprint({}) != first
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-one")
    monkeypatch.setenv("AWS_PROFILE", "profile-two")
    assert db._object_store_fingerprint({}) != first


def test_object_store_destination_browse_binds_untrusted_prefix(monkeypatch):
    from contextlib import nullcontext

    from hub import db, destinations

    class Result:
        def fetchall(self):
            return [("s3://other-bucket/secret.parquet",)]

    class Connection:
        calls = []

        def execute(self, query, parameters=None):
            self.calls.append((query, parameters))
            return Result()

    con = Connection()
    monkeypatch.setattr(db, "ensure_object_store", lambda *a, **k: None)
    monkeypatch.setattr(db, "lock", nullcontext)
    monkeypatch.setattr(db, "conn", lambda: con)

    path = "data'); SELECT 'outside/secret.parquet' AS file; --"
    result = destinations.ObjectStoreBackend("s3").browse("s3://bkt/root", path)

    assert not result.get("error")
    assert result["entries"] == []
    assert con.calls == [
        ("SELECT file FROM glob(?)", [f"s3://bkt/root/{path}/*"]),
    ]


@pytest.mark.parametrize("path", [
    "../outside", "nested/../../outside", "%2e%2e/outside", "%252e%252e/outside",
    "%25252525252e%25252525252e/outside", "s3://other-bucket/data", "data/*",
    "nested\\..\\outside",
])
def test_object_store_destination_browse_rejects_paths_outside_root(monkeypatch, path):
    from hub import db, destinations

    called = False

    def ensure_object_store():
        nonlocal called
        called = True

    monkeypatch.setattr(db, "ensure_object_store", ensure_object_store)

    result = destinations.ObjectStoreBackend("s3").browse("s3://bkt/root", path)

    assert result["entries"] == []
    assert result.get("error")
    assert not called


def test_object_store_feather_roundtrip(tmp_path, object_store_cred):
    # Arrow/Feather (IPC) has no DuckDB file reader/writer, so it goes through pyarrow's own S3
    # filesystem. Previously a raw "s3://…" string was handed to pyarrow.feather → it wrote/read a
    # LOCAL file of that literal name (silent corruption). Prove a real round-trip over the wire.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter, object_fs

    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False
        a = DuckDBAdapter()
        p = _seq_parquet(tmp_path, n=17)
        uri = "s3://bkt/data/out.feather"
        with db.lock():
            res = a.write(uri, db.conn().read_parquet(p), "overwrite")
            assert res["rows"] == 17
            # the wrong old path would have created a local file literally named "s3://bkt/data/out.feather"
            assert not os.path.exists(uri), "feather must not have been written to the local FS"
            got = a.scan(uri).aggregate("count(*) AS c").fetchone()[0]
        assert got == 17  # read back the feather bytes over the wire
        fs, key = object_fs(uri)
        assert key == "bkt/data/out.feather"  # scheme stripped to bucket/key for the object filesystem

        # a failed overwrite must NOT destroy the prior good object (temp-key + move discipline) — the
        # streamed multipart upload would otherwise finalize a partial/empty object onto the destination.
        import hub.plugins.adapters as _ad
        orig = _ad._stream_ipc  # the arrow-IPC streamed writer used by the feather write path
        try:
            _ad._stream_ipc = lambda *a_, **k_: (_ for _ in ()).throw(RuntimeError("boom mid-write"))
            with db.lock():
                with pytest.raises(RuntimeError):
                    a.write(uri, db.conn().read_parquet(p), "overwrite")
                still = a.scan(uri).aggregate("count(*) AS c").fetchone()[0]
            assert still == 17  # the previous good object survived the failed overwrite
        finally:
            _ad._stream_ipc = orig
        # and no temp key was left behind
        left = [o["Key"] for o in boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k",
                aws_secret_access_key="s", region_name="us-east-1").list_objects_v2(Bucket="bkt").get("Contents", [])]
        assert not any(".tmp-" in k for k in left), left
    finally:
        server.stop()
        object_store_cred(None)


def test_object_fs_gcs_hmac_keys_fail_clearly(object_store_cred):
    # pyarrow's GCS filesystem has no HMAC-key parameter, so feather over gs:// can't reuse the DuckDB
    # HMAC creds — fail with a clear message instead of silently authenticating as a different identity.
    from hub.plugins.adapters import object_fs
    object_store_cred({"accessKeyId": "k", "secretAccessKey": "s"})
    try:
        with pytest.raises(NotImplementedError, match="ADC|Application Default|access token"):
            object_fs("gs://bucket/x.feather")
    finally:
        object_store_cred(None)


def test_upload_lands_bytes_in_object_store(tmp_path, object_store_cred):
    # Object-store deployments (multi-instance): uploaded bytes must round-trip through DuckDB httpfs to
    # s3:// so every web instance can read them. _land_upload re-encodes to the SAME format at the target
    # uri (csv stays csv); arrow/feather — which have no object-store reader — normalize to parquet.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.routers.catalog import _land_upload

    try:
        with db.lock():
            db.conn().execute("INSTALL httpfs"); db.conn().execute("LOAD httpfs")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"httpfs unavailable: {e}")

    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client("s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
                     region_name="us-east-1").create_bucket(Bucket="bkt")
        object_store_cred({"endpoint": endpoint, "region": "us-east-1",
                           "accessKeyId": "k", "secretAccessKey": "s"})
        db._obj_store_loaded = False
        deps = get_deps()

        csv = str(tmp_path / "u.csv")
        with open(csv, "w") as f:
            f.write("id,city\n1,paris\n2,rome\n")
        final = _land_upload(deps, csv, "s3://bkt/up/cities.csv")
        assert final == "s3://bkt/up/cities.csv"  # same format, kept in the object store
        with db.lock():
            assert deps.resolve_adapter(final).scan(final).aggregate("count(*) AS c").fetchone()[0] == 2

        import pyarrow as pa, pyarrow.feather as feather
        arrow = str(tmp_path / "u.arrow")
        feather.write_feather(pa.table({"v": [1, 2, 3]}), arrow)
        final2 = _land_upload(deps, arrow, "s3://bkt/up/x.arrow")
        assert final2 == "s3://bkt/up/x.parquet"  # arrow has no object-store reader → normalized to parquet
        with db.lock():
            assert deps.resolve_adapter(final2).scan(final2).aggregate("count(*) AS c").fetchone()[0] == 3
    finally:
        server.stop()
        object_store_cred(None)


def _section(nid, script, params=None, max_runs=200, outputs=None):
    config = {"script": script, "params": params or {}, "maxRuns": max_runs}
    if outputs:
        config["outputs"] = outputs
    return N(nid, "section", config)


def _section_child(nid, parent_id, alias, kind, config=None):
    return {"id": nid, "type": kind, "parentId": parent_id, "position": {"x": 0, "y": 0},
            "data": {"title": alias, "config": config or {}}}


def _seq_parquet(tmp_path, n=1000):
    import duckdb
    p = str(tmp_path / "seq.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT i AS v FROM range(0,{n}) t(i)) TO '{p}' (FORMAT PARQUET)")
    return p


def _seq_lance(tmp_path, n=1000):
    import lance
    import pyarrow as pa

    p = str(tmp_path / f"seq-{uuid.uuid4().hex}.lance")
    lance.write_dataset(pa.table({"v": list(range(n))}), p)
    get_deps().catalog._add(
        name=f"seq_{uuid.uuid4().hex}", uri=p, strict_probe=True)
    return p


def _ensure_run_canvas(canvas_id: str) -> None:
    from hub import metadb

    with metadb.session() as session:
        if session.get(metadb.Canvas, canvas_id) is None:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name=canvas_id))


def test_section_for_each_over_a_list(tmp_path):
    # for-each: run a filter per predicate in a list, concat the results (graph isn't fixed — a `for`)
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("parts = []\n"
              "for pred in params['preds']:\n"
              "    parts.append(run(f, data=inputs['in'], predicate=pred))\n"
              "emit(concat(parts))\n")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, {"preds": ["v >= 0 AND v < 100", "v >= 900"]}),
        _section_child("sec-filter", "sec", "f", "filter"),  # 100 + 100 rows, disjoint
        N("wr", "write", {"name": "sec_foreach"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    assert _output_field(st, "table", outcome="committed") == "sec_foreach"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_foreach").uri, "k": 5}).json()
    assert out["rowCount"] == 200  # concat of the two per-predicate runs


def test_section_iterate_until_condition(tmp_path):
    # iterate-until: shrink the dataset each pass; stop when a metric crosses a threshold (a while+if)
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("state = inputs['in']\n"
              "for i in range(params['max_iters']):\n"
              "    state = run(shrink, data=state, predicate='v >= %d' % (i * 200))\n"
              "    if value(run(cnt, data=state)) < params['target']:\n"
              "        break\n"
              "emit(state)\n")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, {"max_iters": 10, "target": 300}),
        _section_child("sec-shrink", "sec", "shrink", "filter"),
        _section_child("sec-count", "sec", "cnt", "metric", {"agg": "count"}),
        N("wr", "write", {"name": "sec_iter"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_iter").uri, "k": 5}).json()
    # v>=0(1000) v>=200(800) v>=400(600) v>=600(400) v>=800(200 < 300 → break) → 200 rows
    assert out["rowCount"] == 200


def test_section_multi_output_routes_by_port(tmp_path):
    # a section can emit several named output ports; each downstream node is wired to one port
    # (source_handle) and must receive exactly that port's rows — ComfyUI/Weave-style multi-output.
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("emit('low', run(f, data=inputs['in'], predicate='v < 100'))\n"    # 100 rows
              "emit('high', run(f, data=inputs['in'], predicate='v >= 900'))\n")  # 100 rows
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, outputs=["low", "high"]),
        _section_child("sec-filter", "sec", "f", "filter"),
        N("wl", "write", {"name": "sec_low"}),
        N("wh", "write", {"name": "sec_high"}),
    ], "edges": [E("src", "sec"), E("sec", "wl", sh="low"), E("sec", "wh", sh="high")]}
    direct = _poll(client.post("/api/run", json={
        "graph": g, "targetNodeId": "sec", "confirmed": True,
    }).json()["runId"])
    assert direct["status"] == "done" and direct["totalRows"] is None
    assert [output["portId"] for output in direct["outputs"]] == ["low", "high"]
    assert [output["rows"] for output in direct["outputs"]] == [100, 100]

    _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wl", "confirmed": True}).json()["runId"])
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wh", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    import duckdb
    def stats(name):
        uri = get_deps().catalog.get_table(f"tbl_{name}").uri
        return duckdb.connect(":memory:").execute(f"SELECT count(*), min(v), max(v) FROM read_parquet('{uri}')").fetchone()
    # assert CONTENT, not just row count — both ports are 100 rows, so a handle-routing bug that
    # writes the wrong port's data would pass a count-only check but fail here (min/max differ).
    assert stats("sec_low") == (100, 0, 99)      # the "low" port: v < 100
    assert stats("sec_high") == (100, 900, 999)  # the "high" port: v >= 900
    assert duckdb.connect(":memory:").execute(
        f"SELECT count(*), min(v), max(v) FROM read_parquet('{direct['outputs'][0]['uri']}')"
    ).fetchone() == (100, 0, 99)
    assert duckdb.connect(":memory:").execute(
        f"SELECT count(*), min(v), max(v) FROM read_parquet('{direct['outputs'][1]['uri']}')"
    ).fetchone() == (100, 900, 999)


def test_run_history_persisted_with_canvas(tmp_path):
    # a finished run is recorded under its canvas (survives restart) + exposed at /canvas/{id}/runs
    import uuid

    from hub import metadb
    p = _seq_parquet(tmp_path)
    output_name = f"hist_out_{uuid.uuid4().hex}"
    client.put("/api/canvas/hist_canvas", json={"id": "hist_canvas", "name": "h", "version": 1, "nodes": [], "edges": []})  # persist the canvas
    g = {"id": "hist_canvas", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), N("wr", "write", {"name": output_name}),
    ], "edges": [E("src", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    runs = []
    for _ in range(40):  # on_complete persists in the run's finally, a beat after status flips to done
        runs = metadb.list_runs("hist_canvas")
        if runs:
            break
        time.sleep(0.05)
    assert runs and runs[0]["status"] == "done"
    assert _output_field(runs[0], "table", outcome="committed") == output_name
    history_uri = _output_field(runs[0], "uri", outcome="committed")
    status_uri = _output_field(st, "uri", outcome="committed")
    assert runs[0]["runId"] == st["runId"] and history_uri == status_uri
    # The history row, not the runner's in-memory map, is sufficient to reopen the committed artifact.
    # This is the restart/reopen path used by Run history after a fresh browser/server process.
    reopened = client.post(
        "/api/data/sample", json={"uri": history_uri, "k": 5, "offset": 0}).json()
    assert len(reopened["rows"]) == 5 and reopened["rowCount"] == st["totalRows"]
    api_runs = client.get("/api/canvas/hist_canvas/runs").json()
    api_record = next(r for r in api_runs if r["runId"] == st["runId"])
    assert api_record["status"] == "done"
    assert _output_field(api_record, "uri", outcome="committed") == status_uri
    # durable per-node breakdown (telemetry) travels with the run record, not just the transient RunState
    pn = runs[0]["perNode"]
    assert pn and all("node_id" in p and "status" in p for p in pn)
    assert any(p["node_id"] == "wr" and p["status"] == "done" for p in pn)


def test_non_write_full_result_reopens_from_durable_history(tmp_path):
    """The ephemeral Full Result link, not only a write-node output, survives in run history."""
    from hub import metadb

    canvas_id = "nonwrite_history_canvas"
    client.put(f"/api/canvas/{canvas_id}", json={
        "id": canvas_id, "name": "non-write history", "version": 1, "nodes": [], "edges": [],
    })
    source = _seq_parquet(tmp_path, n=105)
    graph = {"id": canvas_id, "version": 1, "nodes": [
        N("src", "source", {"uri": source}),
        N("agg", "aggregate", {"groupBy": "v", "aggs": "count(*) AS n"}),
    ], "edges": [E("src", "agg")]}
    status = _poll(client.post("/api/run", json={
        "graph": graph, "targetNodeId": "agg", "confirmed": True,
    }).json()["runId"])
    assert status["status"] == "done"
    status_uri = _output_field(status, "uri", outcome="committed")
    assert status_uri and _output_field(status, "table") is None

    records = []
    for _ in range(40):
        records = metadb.list_runs(canvas_id)
        if any(r["runId"] == status["runId"] for r in records):
            break
        time.sleep(0.05)
    record = next(r for r in records if r["runId"] == status["runId"])
    assert _output_field(record, "uri", outcome="committed") == status_uri
    assert _output_field(record, "table") is None

    # Reopen and page using only the durable history link, as the UI does after a restart.
    second = client.post("/api/data/sample", json={
        "uri": _output_field(record, "uri", outcome="committed"), "k": 50, "offset": 50,
    }).json()
    assert len(second["rows"]) == 50 and second["rowCount"] == 105 and second["hasMore"] is True


def test_full_result_commit_failure_fails_the_run(tmp_path):
    """A local relation is not a successful full run until its reopenable artifact commits."""
    from hub.compiler import compile_plan
    from hub.models import Graph
    from hub.plugins.runner import LocalRunner

    class FailingStorage:
        def output_uri(self, name, ext):
            return f"fail://results/{name}{ext}"

    class FailingAdapter:
        def write(self, uri, rel, mode="overwrite"):
            raise OSError("artifact commit failed")

    d = get_deps()
    source = _seq_parquet(tmp_path, n=10)
    graph = Graph(**{"id": "commit_failure", "version": 1,
                     "nodes": [N("s", "source", {"uri": source})], "edges": []})

    def resolve(uri):
        return FailingAdapter() if uri.startswith("fail://") else d.resolve_adapter(uri)

    runner = LocalRunner(resolve, d.registry, d.catalog, d.workspace,
                         node_builders=d.node_builders, node_specs=d.node_specs,
                         storage=FailingStorage())
    status = runner.run(compile_plan(graph, "s", d.registry, d.node_specs), graph, "s", "local")
    for _ in range(200):
        status = runner.status(status.run_id)
        if status.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.01)
    assert status.status == "failed"
    assert "artifact commit failed" in (status.error or "")
    assert _output_field(status, "uri", outcome="failed") is None


def test_headless_run_executes_a_saved_canvas(tmp_path, capsys):
    # ARC7 headless-run: `dataplay run <canvas>` runs a SAVED canvas to completion in-process (cron/CI),
    # exits 0 on done, materializes the write, and reuses the exact start_run path the UI/MCP use.
    from hub.cli import _headless_run
    from hub.deps import get_deps
    p = _seq_parquet(tmp_path)
    doc = {"id": "hr_canvas", "name": "hr", "version": 1,
           "nodes": [N("src", "source", {"uri": p}), N("wr", "write", {"name": "hr_out"})],
           "edges": [E("src", "wr")]}
    client.put("/api/canvas/hr_canvas", json=doc)
    code = _headless_run(get_deps(), "hr_canvas", None, 30.0, as_json=False)
    out = capsys.readouterr().out
    assert code == 0, f"headless run exited {code}; stdout:\n{out}"
    assert "DONE" in out and "hr_out" in out
    # the write actually materialized a queryable output table
    assert any(t["name"] == "hr_out" for t in client.get(
        "/api/catalog/tables", params={"q": "hr_out"}
    ).json()["items"])


def test_headless_run_resolves_by_name_and_reports_failure(tmp_path):
    # name lookup (not just id) works; an unknown canvas errors out (SystemExit), not a silent 0.
    import pytest

    from hub.cli import _headless_run, _load_canvas_graph
    from hub.deps import get_deps
    p = _seq_parquet(tmp_path)
    doc = {"id": "hr_named_id", "name": "my-nightly-job", "version": 1,
           "nodes": [N("src", "source", {"uri": p}), N("wr", "write", {"name": "hr_named_out"})],
           "edges": [E("src", "wr")]}
    client.put("/api/canvas/hr_named_id", json=doc)
    graph, cid = _load_canvas_graph("my-nightly-job")  # resolves a unique NAME → its id
    assert cid == "hr_named_id" and len(graph.nodes) == 2
    with pytest.raises(SystemExit):  # unknown ref must fail loudly
        _headless_run(get_deps(), "no_such_canvas_xyz", None, 5.0, as_json=False)


def test_headless_run_kernel_failure_is_a_clean_exit(tmp_path, monkeypatch):
    # review MAJOR: the DEFAULT backend is the kernel, whose run() raises RuntimeError/OSError when a
    # kernel won't start / is unreachable — headless must convert that to a clean SystemExit (exit code),
    # NOT let a traceback escape. (The suite's in-process backend never hits that path, so force it.)
    import pytest

    import hub.routers.runs as runs_mod
    from hub import cli
    from hub.deps import get_deps
    p = _seq_parquet(tmp_path)
    doc = {"id": "kf_canvas", "name": "kf", "version": 1,
           "nodes": [N("src", "source", {"uri": p}), N("wr", "write", {"name": "kf_out"})],
           "edges": [E("src", "wr")]}
    client.put("/api/canvas/kf_canvas", json=doc)

    def _boom(*a, **k):
        raise RuntimeError("kernel for canvas 'kf_canvas' did not become ready in 1.0s")

    monkeypatch.setattr(runs_mod, "start_run", _boom)  # _headless_run imports start_run at call time
    with pytest.raises(SystemExit) as ei:
        cli._headless_run(get_deps(), "kf_canvas", None, 5.0, as_json=False)
    assert "cannot run canvas 'kf_canvas'" in str(ei.value)


def test_headless_timeout_cancels_waits_and_never_publishes(tmp_path, monkeypatch, capsys):
    # P0-EXEC-03: a timeout is a stop request, not merely a client-side return. Hold the built-in local
    # adapter immediately before its write, let the CLI deadline fire, and prove _headless_run waits for
    # the worker's terminal acknowledgement before returning 124. The fenced write must never appear later.
    import re
    import threading
    import uuid

    from hub import metadb
    from hub.cli import _headless_run
    from hub.deps import get_deps

    d = get_deps()
    src = _seq_parquet(tmp_path, n=20)
    suffix = uuid.uuid4().hex
    output_name = f"cli_timeout_{suffix}.parquet"
    table_name = output_name.removesuffix(".parquet")
    output_uri = d.storage.output_uri(table_name, ".parquet")
    cid = f"cli_timeout_{suffix}"
    client.put(f"/api/canvas/{cid}", json={
        "id": cid, "name": cid, "version": 1,
        "nodes": [N("src", "source", {"uri": src}),
                  N("wr", "write", {"filename": output_name, "writeMode": "overwrite"})],
        "edges": [E("src", "wr")],
    })

    resolve = d.runner.resolve_adapter
    real = resolve(output_uri)
    entered = threading.Event()
    finished = threading.Event()

    class _SlowCommitAdapter:
        def __getattr__(self, name):
            return getattr(real, name)

        def write(self, uri, rel, mode="overwrite", partition_by=None, cancelled=None):
            if uri != output_uri:
                return real.write(uri, rel, mode, partition_by=partition_by, cancelled=cancelled)
            entered.set()
            try:
                assert cancelled is not None, "the runner must pass its cancellation fence to the adapter"
                deadline = time.monotonic() + 5
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return real.write(uri, rel, mode, partition_by=partition_by, cancelled=cancelled)
            finally:
                finished.set()

    slow = _SlowCommitAdapter()
    monkeypatch.setattr(d.runner, "resolve_adapter", lambda uri: slow if uri == output_uri else resolve(uri))
    previous_backend = metadb.get_setting("backend", "global", default="") or ""
    metadb.set_setting("backend", "local-out-of-core", "global")
    try:
        code = _headless_run(d, cid, "wr", 0.01, as_json=False)
    finally:
        metadb.set_setting("backend", previous_backend, "global")

    captured = capsys.readouterr()
    match = re.search(r"run (run_[0-9a-f]+)", captured.err)
    assert code == 124 and match, captured.err
    run_id = match.group(1)
    assert entered.is_set() and finished.is_set(), "CLI returned before the local worker acknowledged stop"
    assert d.runner.status(run_id).status == "cancelled"
    assert metadb.get_run_state(run_id)["status"] == "cancelled"
    assert not os.path.exists(output_uri)
    time.sleep(0.2)
    assert not os.path.exists(output_uri), "a cancelled attempt published after the CLI returned"
    assert not any(t.name == table_name
                   for t in d.catalog.list_page(CatalogQuery(limit=5000)).items)


def test_headless_timeout_stops_isolated_child_without_late_publish(tmp_path, capsys):
    # The production-local path is the isolated subprocess runner (also used inside the default kernel).
    # A runaway Python cell ignores DuckDB interrupt, so this exercises cooperative cancel followed by the
    # bounded hard-kill fallback. Terminal cancelled is emitted only after the child is reaped.
    import re
    import uuid

    from hub import metadb
    from hub.cli import _headless_run
    from hub.deps import get_deps

    d = get_deps()
    src = _seq_lance(tmp_path, n=5)
    suffix = uuid.uuid4().hex
    output_name = f"cli_subprocess_timeout_{suffix}.parquet"
    table_name = output_name.removesuffix(".parquet")
    output_uri = d.storage.output_uri(table_name, ".parquet")
    cid = f"cli_subprocess_timeout_{suffix}"
    client.put(f"/api/canvas/{cid}", json={
        "id": cid, "name": cid, "version": 1,
        "nodes": [
            N("src", "source", {"uri": src}),
            N("xf", "transform", {"source": "adhoc", "mode": "map",
                                      "code": "def fn(row):\n    while True:\n        pass"}),
            N("wr", "write", {"filename": output_name, "writeMode": "overwrite"}),
        ],
        "edges": [E("src", "xf"), E("xf", "wr")],
    })

    previous_backend = metadb.get_setting("backend", "global", default="") or ""
    metadb.set_setting("backend", "local-subprocess", "global")
    try:
        code = _headless_run(d, cid, None, 0.05, as_json=False)
    finally:
        metadb.set_setting("backend", previous_backend, "global")

    captured = capsys.readouterr()
    match = re.search(r"run (run_[0-9a-f]+)", captured.err)
    assert code == 124 and match, captured.err
    run_id = match.group(1)
    owner = d.run_index[run_id]
    assert owner.name == "local-subprocess" and owner.status(run_id).status == "cancelled"
    assert run_id not in owner._procs, "terminal cancellation was published before the child was reaped"
    assert metadb.get_run_state(run_id)["status"] == "cancelled"
    assert not os.path.exists(output_uri)
    time.sleep(0.2)
    assert not os.path.exists(output_uri), "the reaped child published after the CLI returned"
    assert not any(t.name == table_name
                   for t in d.catalog.list_page(CatalogQuery(limit=5000)).items)


def test_headless_timeout_cancels_multi_region_handoff(tmp_path, monkeypatch, capsys):
    # A checkpoint makes the RunController publish an intermediate region parquet before the final write.
    # Hold that handoff, time out the CLI, and prove both the handoff and user output stay unpublished.
    import glob
    import re
    import uuid

    from hub import metadb
    from hub.cli import _headless_run
    from hub.deps import get_deps

    d = get_deps()
    source = _seq_parquet(tmp_path, n=20)
    suffix = uuid.uuid4().hex
    output_name = f"cli_region_timeout_{suffix}.parquet"
    table_name = output_name.removesuffix(".parquet")
    output_uri = d.storage.output_uri(table_name, ".parquet")
    cid = f"cli_region_timeout_{suffix}"
    client.put(f"/api/canvas/{cid}", json={
        "id": cid, "name": cid, "version": 1,
        "nodes": [
            N("src", "source", {"uri": source}),
            {"id": "checkpoint", "type": "filter", "position": {"x": 0, "y": 0},
             "data": {"config": {"predicate": "v >= 0", "checkpoint": True}}},
            N("wr", "write", {"filename": output_name, "writeMode": "overwrite"}),
        ],
        "edges": [E("src", "checkpoint"), E("checkpoint", "wr")],
    })

    region_root = os.path.join(d.workspace, "regions")
    before = set(glob.glob(os.path.join(region_root, "*")))
    resolve = d.resolve_adapter
    real = resolve(os.path.join(region_root, "probe.parquet"))
    entered = False

    class _SlowRegionAdapter:
        def __getattr__(self, name):
            return getattr(real, name)

        def write(self, uri, rel, mode="overwrite", partition_by=None, cancelled=None):
            nonlocal entered
            entered = True
            assert cancelled is not None
            deadline = time.monotonic() + 5
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            return real.write(uri, rel, mode, partition_by=partition_by, cancelled=cancelled)

    slow = _SlowRegionAdapter()
    monkeypatch.setattr(d, "resolve_adapter",
                        lambda uri: slow if os.path.dirname(str(uri)) == region_root else resolve(uri))
    previous_backend = metadb.get_setting("backend", "global", default="") or ""
    metadb.set_setting("backend", "local-out-of-core", "global")
    try:
        code = _headless_run(d, cid, "wr", 0.01, as_json=False)
    finally:
        metadb.set_setting("backend", previous_backend, "global")

    captured = capsys.readouterr()
    match = re.search(r"run (run_[0-9a-f]+)", captured.err)
    assert code == 124 and match and entered, captured.err
    run_id = match.group(1)
    assert d.run_index[run_id] is d.controller and d.controller.status(run_id).status == "cancelled"
    assert metadb.get_run_state(run_id)["status"] == "cancelled"
    assert not os.path.exists(output_uri)
    assert set(glob.glob(os.path.join(region_root, "*"))) == before, "cancelled handoff published late"
    assert not any(t.name == table_name
                   for t in d.catalog.list_page(CatalogQuery(limit=5000)).items)


def test_local_overwrite_cancel_fence_preserves_previous_output(tmp_path):
    # The local adapter writes a temp sibling first. A cancellation that arrives after staging but before
    # os.replace must discard that temp and leave the previously published dataset byte-for-byte readable.
    import glob

    import duckdb

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter
    from hub.plugins.runner import _CancelToken

    output = str(tmp_path / "fenced.parquet")
    cancel_file = tmp_path / "cancel.requested"
    duckdb.connect().execute(f"COPY (SELECT 1 AS value) TO '{output}' (FORMAT PARQUET)")
    checks = 0
    token = _CancelToken(cancel_file.exists)

    def cancelled():
        nonlocal checks
        checks += 1
        if checks == 3:  # after row count + pre-write check: staging is complete, publish has not happened
            assert glob.glob(output + ".tmp-*"), "test did not reach the staging-to-publish boundary"
            cancel_file.touch()  # the same external request file the isolated child observes
        return token.is_set()

    with db.run_scope():
        replacement = db.conn().sql("SELECT 2 AS value")
        with pytest.raises(RuntimeError, match="cancelled before output commit"):
            DuckDBAdapter().write(output, replacement, cancelled=cancelled)
    assert duckdb.connect().read_parquet(output).fetchall() == [(1,)]
    assert not glob.glob(output + ".tmp-*"), "cancelled staging file leaked"


def test_headless_sigint_cancels_and_returns_run_identity(monkeypatch, capsys):
    # KeyboardInterrupt is the in-process form of SIGINT. It follows the same cancel/ack path, returns the
    # conventional shell code 130, and emits a machine-readable run identity under --json.
    import json

    import hub.routers.runs as runs_mod
    from hub.cli import _headless_run
    from hub.deps import get_deps
    from hub.models import RunStatus

    cid = "cli_sigint_canvas"
    client.put(f"/api/canvas/{cid}", json={
        "id": cid, "name": cid, "version": 1, "nodes": [], "edges": [],
    })
    state = RunStatus(run_id="run_sigint_test", status="running", placement="local", per_node=[])

    class _InterruptOwner:
        cancelled = False
        cancel_acknowledges_stop = True

        def status(self, run_id):
            assert run_id == state.run_id
            if not self.cancelled:
                raise KeyboardInterrupt
            return state

        def cancel(self, run_id):
            assert run_id == state.run_id
            self.cancelled = True
            state.status = "cancelled"
            return state

    owner = _InterruptOwner()
    monkeypatch.setattr(runs_mod, "start_run", lambda *args, **kwargs: (state, owner))
    code = _headless_run(get_deps(), cid, None, 30.0, as_json=True)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 130 and owner.cancelled
    assert payload["run_id"] == state.run_id and payload["status"] == "cancelled"
    assert payload["exit_reason"] == "interrupted by SIGINT" and payload["cancel_acknowledged"] is True
    assert state.run_id in captured.err and "SIGINT" in captured.err


def test_cli_does_not_trust_an_eager_plugin_cancel_status():
    # Legacy/plugin backends may relabel a run cancelled while their driver still owns live work. Without
    # the explicit acknowledgement seam, CLI cancellation must time out as unacknowledged, not claim safety.
    from hub.cli import _cancel_and_wait
    from hub.models import RunStatus

    state = RunStatus(run_id="run_eager_plugin", status="running", placement="distributed", per_node=[])

    class _EagerPlugin:
        def cancel(self, _run_id):
            state.status = "cancelled"
            return state

        def status(self, _run_id):
            return state

    class _NoDurableState:
        @staticmethod
        def get_run_state(_run_id):
            return None

    final, acknowledged, error = _cancel_and_wait(_EagerPlugin(), state.run_id, state,
                                                   _NoDurableState(), timeout_s=0)
    assert final.status == "cancelled" and acknowledged is False and error is None


def test_headless_run_canvas_params(tmp_path):
    # ARC7 canvas parameters: ${NAME} tokens in a canvas's configs are bound per run via --param (cron/CI);
    # an UNBOUND token must fail loudly, not run against the literal "${src}" path.
    import pytest

    from hub.cli import _apply_params, _headless_run, _load_canvas_graph
    from hub.deps import get_deps
    p = _seq_parquet(tmp_path)
    doc = {"id": "param_canvas", "name": "pc", "version": 1,
           "nodes": [N("src", "source", {"uri": "${src}"}), N("wr", "write", {"name": "param_out"})],
           "edges": [E("src", "wr")]}
    client.put("/api/canvas/param_canvas", json=doc)

    # bound: ${src} → the real parquet path → runs to done + materializes the output
    code = _headless_run(get_deps(), "param_canvas", None, 30.0, as_json=False, params={"src": p})
    assert code == 0
    assert any(t["name"] == "param_out" for t in client.get(
        "/api/catalog/tables", params={"q": "param_out"}
    ).json()["items"])

    # unbound: no --param → loud failure BEFORE the run (not a run against a literal "${src}" path)
    with pytest.raises(SystemExit) as ei:
        _headless_run(get_deps(), "param_canvas", None, 30.0, as_json=False, params={})
    assert "unbound canvas parameter" in str(ei.value) and "src" in str(ei.value)

    # _apply_params substitutes within config; a bound token is replaced verbatim, title left untouched
    g, _ = _load_canvas_graph("param_canvas")
    _apply_params(g, {"src": "/data/x.parquet"})
    assert g.nodes[0].data["config"]["uri"] == "/data/x.parquet"

    # M1: a token whose name has a '-'/'.' still binds (regex is [^}]+), and is NEVER left as a silent
    # literal — an unbound one of these errors just like a plain name (the review's silent-hole fix)
    doc2 = {"id": "pc2", "name": "pc2", "version": 1,
            "nodes": [N("src", "source", {"uri": "d/${my-date}/x"})], "edges": []}
    client.put("/api/canvas/pc2", json=doc2)
    g2, _ = _load_canvas_graph("pc2")
    _apply_params(g2, {"my-date": "2026-07-12"})
    assert g2.nodes[0].data["config"]["uri"] == "d/2026-07-12/x"
    with pytest.raises(SystemExit) as ei2:
        _apply_params(_load_canvas_graph("pc2")[0], {})  # unbound hyphen token must fail loudly
    assert "my-date" in str(ei2.value)

    # m2: targeting one node must NOT require params from an UNRELATED branch. Branch A (src_a→wr_a) needs
    # no param; branch B has an unbound ${dev}. Running --node wr_a substitutes/checks only A's cone.
    doc3 = {"id": "pc3", "name": "pc3", "version": 1,
            "nodes": [N("src_a", "source", {"uri": p}), N("wr_a", "write", {"name": "pc3_a"}),
                      N("src_b", "source", {"uri": "${dev}"}), N("wr_b", "write", {"name": "pc3_b"})],
            "edges": [E("src_a", "wr_a"), E("src_b", "wr_b")]}
    client.put("/api/canvas/pc3", json=doc3)
    ga, _ = _load_canvas_graph("pc3")
    _apply_params(ga, {}, node="wr_a")  # only wr_a's cone (src_a, wr_a) — the unbound ${dev} in B is not in scope
    # (no SystemExit) — and the whole-canvas view WOULD flag it:
    with pytest.raises(SystemExit):
        _apply_params(_load_canvas_graph("pc3")[0], {})


def test_coordination_tables_pruned_to_cap(monkeypatch):
    # ARC5 coordination-table-prune: run_records (per-canvas history) and run_states (one full-JSON row
    # per run) must NOT grow without bound in the local DB. record_run caps per-canvas history; a run
    # reaching a terminal status prunes finished run_states to the newest N — while NEVER pruning a live
    # (running/queued) row, so the reaper + in-flight status lookups are untouched.
    from sqlalchemy import func, select as _select

    from hub import metadb
    from hub.models import RunOutput
    monkeypatch.setattr(metadb, "_RUN_HISTORY_MAX", 3)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 3)

    # run_records: per-canvas cap (isolated by a unique canvas id)
    cid = "prune_canvas_x"
    with metadb.session() as s:
        s.add(metadb.Canvas(id=cid, owner_id=metadb.DEFAULT_USER_ID, name="t", version=1, doc="{}", visibility="private"))
    for i in range(7):
        output = RunOutput(
            node_id="n", port_id="out", wire="dataset", publication_kind="result",
            outcome="committed", uri=f"/tmp/{cid}-{i}.parquet", rows=i,
        )
        assert metadb.record_run(
            cid, "n", "run", "done", rows=i, outputs=[output.model_dump()]
        ) is True
    with metadb.session() as s:
        surviving = sorted(r.rows for r in s.scalars(_select(metadb.RunRecord).where(metadb.RunRecord.canvas_id == cid)))
    # exactly the cap, and the NEWEST 3 (rows 4,5,6) survive — pins keep-newest, not merely the count
    # (a reversed keep-oldest impl would pass a count-only assertion). Each record_run is its own DB
    # transaction (ms apart) so created_at is strictly increasing → deterministic, no timestamp ties.
    assert surviving == [4, 5, 6], f"run_records prune must keep the newest {3}: {surviving}"

    # run_states: global terminal cap, live rows never pruned. Clean slate first (other tests leave rows).
    with metadb.session() as s:
        for rs in s.scalars(_select(metadb.RunState)).all():
            s.delete(rs)
    metadb.save_run_state("live_run_a", {"run_id": "live_run_a", "status": "running"})
    metadb.save_run_state("live_run_b", {"run_id": "live_run_b", "status": "queued"})
    for i in range(6):
        metadb.save_run_state(f"done_run_{i}", {"run_id": f"done_run_{i}", "status": "done"})
    with metadb.session() as s:
        term_ids = {r.run_id for r in s.scalars(_select(metadb.RunState).where(metadb.RunState.status.in_(metadb._TERMINAL_RUN)))}
        n_live = s.scalar(_select(func.count()).select_from(metadb.RunState).where(~metadb.RunState.status.in_(metadb._TERMINAL_RUN)))
    # terminal capped to the newest 3 (done_run_3/4/5); live rows (running/queued) never pruned
    assert term_ids == {"done_run_3", "done_run_4", "done_run_5"}, f"terminal prune must keep newest: {term_ids}"
    assert n_live == 2, f"live run_states must never be pruned: {n_live}"
    assert metadb.get_run_state("live_run_a") and metadb.get_run_state("live_run_b")


def test_telemetry_sink_seam_fires_once_per_finished_run(tmp_path):
    # reg.add_telemetry_sink registers a consumer that gets a normalized record on each finished run; a
    # sink that raises is swallowed (never fails the run) and the other sinks still fire.
    import hub.deps as dm
    from hub.deps import Deps, Registry
    from hub.models import Graph, PerNodeStatus, RunOutput, RunStatus

    ws = tmp_path / "ws"; ws.mkdir()
    d = Deps(str(ws), str(tmp_path / "data"))
    got: list[dict] = []
    Registry(d).add_telemetry_sink(lambda rec: (_ for _ in ()).throw(RuntimeError("boom")))  # bad sink first
    Registry(d).add_telemetry_sink(got.append)  # good sink still fires
    assert len(d.telemetry_sinks) == 2

    g = Graph(**{"id": "no_such_canvas", "version": 1, "nodes": [], "edges": []})
    st = RunStatus(
        run_id="r1", status="done", target_node_id="n", total_rows=5, ms=12,
        placement="local",
        per_node=[PerNodeStatus(node_id="n", status="done", rows=5, ms=12)],
        outputs=[RunOutput(
            node_id="n", port_id="out", wire="dataset", publication_kind="result",
            outcome="committed", uri="/tmp/r1.parquet", rows=5,
        )],
    )
    dm._persist_run(d, g, "n", st)  # no canvas → record_run no-ops, but the sink seam still fans out
    from hub.observability import drain_sinks
    assert drain_sinks()

    assert len(got) == 1
    rec = got[0]
    assert rec["run_id"] == "r1" and rec["status"] == "done" and rec["rows"] == 5 and rec["ms"] == 12
    assert rec["per_node"] and rec["per_node"][0]["node_id"] == "n"

    # an internal region sub-run (RunController._subgraph uses the sentinel id '_region') must NOT leak a
    # phantom telemetry record to sinks — the controller fires the real completion once for the logical run
    region = Graph(**{"id": "_region", "version": 1, "nodes": [], "edges": []})
    dm._persist_run(d, region, "n", RunStatus(run_id="r_region", status="done", placement="local"))
    assert len(got) == 1 and got[0]["run_id"] == "r1"


def test_run_log_reference_plugin_appends_finished_runs(tmp_path, monkeypatch):
    # dp_run_log registers a telemetry sink via reg.add_telemetry_sink that appends one JSON line per
    # finished run — the reference for an OTel/warehouse exporter (offline-first: core ships none).
    import json
    import shutil
    from pathlib import Path

    import hub.deps as dm
    from hub.deps import Deps
    from hub.models import Graph, PerNodeStatus, RunOutput, RunStatus

    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("DP_RUN_LOG", str(log))  # the plugin reads path via reg.config (env fallback)
    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_run_log",
                    ws / "plugins" / "dp_run_log")
    d = Deps(str(ws), str(tmp_path / "data"))
    assert len(d.telemetry_sinks) == 1  # the plugin registered its sink at load

    g = Graph(**{"id": "no_such_canvas", "version": 1, "nodes": [], "edges": []})
    for rid, status in [("r1", "done"), ("r2", "failed")]:
        output = RunOutput(
            node_id="n", port_id="out", wire="dataset", publication_kind="result",
            outcome="committed" if status == "done" else "failed",
            uri=f"/tmp/{rid}.parquet" if status == "done" else None,
            rows=3 if status == "done" else None,
            error="failed" if status == "failed" else None,
        )
        dm._persist_run(d, g, "n", RunStatus(
            run_id=rid, status=status, target_node_id="n",
            total_rows=3 if status == "done" else None, ms=7, placement="local",
            error="failed" if status == "failed" else None,
            per_node=[PerNodeStatus(node_id="n", status=status, rows=3, ms=7)],
            outputs=[output],
        ))

    from hub.observability import drain_sinks
    assert drain_sinks()
    lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    assert [x["run_id"] for x in lines] == ["r1", "r2"]  # one JSON line per finished run, in order
    assert lines[0]["status"] == "done" and lines[1]["status"] == "failed"


def test_collab_relay_broadcasts_and_leave(live_collab_url):
    # the collab room relays a peer's message to others and tells them when a peer leaves
    import asyncio
    import websockets

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/room1"
        async with websockets.connect(url, proxy=None) as b:
            await _collab_seed(b)
            async with websockets.connect(url, proxy=None) as a:
                assert (await _collab_recv(a))["mode"] == "sync"
                await _collab_send(a, {"clientId": "A", "type": "presence", "name": "Ann"})
                got = await _collab_recv(b)
                assert got["clientId"] == "A" and got["type"] == "presence"
            assert await _collab_recv(b) == {"type": "server", "event": "leave", "clientId": "A"}

    asyncio.run(scenario())


def test_collab_room_state_elects_one_seed_and_re_elects_after_disconnect(live_collab_url):
    # Connection ordering, not client timing, elects one seed; another unsynced writer must wait.
    import asyncio
    import websockets

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/room-state"
        async with websockets.connect(url, proxy=None) as first:
            first_plan = await _collab_recv(first)
            assert first_plan["type"] == "server" and first_plan["mode"] == "seed"
            async with websockets.connect(url, proxy=None) as second:
                assert await _collab_recv(second) == {
                    "type": "server", "event": "room-state", "mode": "wait",
                }
        async with websockets.connect(url, proxy=None) as fresh:
            fresh_plan = await _collab_recv(fresh)
            assert fresh_plan["type"] == "server" and fresh_plan["mode"] == "seed"
            assert fresh_plan["requestId"] != first_plan["requestId"]

    asyncio.run(scenario())


def test_collab_seed_lease_rotates_after_partial_handshake(monkeypatch, live_collab_url):
    # A seed that sends its snapshot but never acknowledges readiness still blocks the room. Lease the
    # complete handshake, rotate in connection order, and make the timed-out peer sync from the winner.
    import asyncio
    import websockets
    from hub import main as hub_main

    monkeypatch.setattr(hub_main, "_COLLAB_SEED_READY_TIMEOUT_SECONDS", 0.08)

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/partial-seed-timeout"
        async with websockets.connect(url, proxy=None) as first:
            first_plan = await _collab_recv(first)
            assert first_plan["mode"] == "seed"
            await _collab_send(first, {
                "type": "yjs", "seed": True, "requestId": first_plan["requestId"],
                "update": "partial-state",
            })

            async with websockets.connect(url, proxy=None) as replacement:
                assert (await _collab_recv(replacement))["mode"] == "wait"
                assert await _collab_recv(first) == {
                    "type": "server", "event": "room-state", "mode": "wait",
                }
                replacement_plan = await _collab_recv(replacement)
                assert replacement_plan["mode"] == "seed"
                assert replacement_plan["requestId"] != first_plan["requestId"]

                await _collab_send(replacement, {
                    "type": "yjs", "seed": True, "requestId": replacement_plan["requestId"],
                    "update": "authoritative-state",
                })
                await _collab_send(replacement, {
                    "type": "sync-ready", "requestId": replacement_plan["requestId"],
                })
                assert (await _collab_recv(replacement))["mode"] == "ready"

                sync_plan = await _collab_recv(first)
                assert sync_plan["mode"] == "sync"
                await _collab_send(first, {
                    "type": "ysync", "requestId": sync_plan["requestId"], "sv": "partial-vector",
                })
                assert (await _collab_recv(replacement))["requestId"] == sync_plan["requestId"]
                await _collab_send(replacement, {
                    "type": "yjs", "sync": True, "replyTo": sync_plan["requestId"],
                    "update": "authoritative-state",
                })
                assert (await _collab_recv(first))["replyTo"] == sync_plan["requestId"]
                await _collab_send(first, {
                    "type": "sync-ready", "requestId": sync_plan["requestId"],
                })
                assert (await _collab_recv(first))["mode"] == "ready"

    asyncio.run(scenario())


def test_collab_all_silent_seed_candidates_become_unavailable_then_retry(monkeypatch, live_collab_url):
    # Every connected writer gets one bounded seed lease. Exhaustion is explicit unavailable (never
    # ready), followed by a fresh bounded election pass so open silent sockets cannot cause a permanent wait.
    import asyncio
    import websockets
    from hub import main as hub_main

    monkeypatch.setattr(hub_main, "_COLLAB_SEED_READY_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(hub_main, "_COLLAB_UNAVAILABLE_RETRY_SECONDS", 0.06)

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/all-silent-seeds"
        async with websockets.connect(url, proxy=None) as first:
            first_plan = await _collab_recv(first)
            assert first_plan["mode"] == "seed"
            async with websockets.connect(url, proxy=None) as second:
                assert (await _collab_recv(second))["mode"] == "wait"

                assert (await _collab_recv(first))["mode"] == "wait"
                second_plan = await _collab_recv(second)
                assert second_plan["mode"] == "seed"
                assert second_plan["requestId"] != first_plan["requestId"]

                assert await _collab_recv(first) == {
                    "type": "server", "event": "room-state", "mode": "unavailable",
                }
                assert await _collab_recv(second) == {
                    "type": "server", "event": "room-state", "mode": "unavailable",
                }

                retry_plan = await _collab_recv(first)
                assert retry_plan["mode"] == "seed"
                assert retry_plan["requestId"] not in {
                    first_plan["requestId"], second_plan["requestId"],
                }
                assert (await _collab_recv(second))["mode"] == "wait"

                await _collab_send(first, {
                    "type": "yjs", "seed": True, "requestId": retry_plan["requestId"],
                    "update": "recovered-state",
                })
                await _collab_send(first, {
                    "type": "sync-ready", "requestId": retry_plan["requestId"],
                })
                assert (await _collab_recv(first))["mode"] == "ready"
                await _collab_sync(first, second, update="recovered-state")

    asyncio.run(scenario())


def test_collab_plans_are_isolated_between_rooms(live_collab_url):
    # Replanning one canvas must not invalidate an in-flight seed request in another canvas.
    import asyncio
    import websockets

    async def scenario() -> None:
        base = live_collab_url.replace("http://", "ws://") + "/ws/collab/"
        async with websockets.connect(base + "room-a", proxy=None) as room_a:
            assert (await _collab_recv(room_a))["mode"] == "seed"
            async with websockets.connect(base + "room-b", proxy=None) as room_b:
                plan_b = await _collab_recv(room_b)
                assert plan_b["mode"] == "seed"
                async with websockets.connect(base + "room-a", proxy=None) as room_a_waiter:
                    assert (await _collab_recv(room_a_waiter))["mode"] == "wait"
                    await _collab_send(room_b, {
                        "type": "yjs", "seed": True, "requestId": plan_b["requestId"], "update": "B",
                    })
                    await _collab_send(room_b, {"type": "sync-ready", "requestId": plan_b["requestId"]})
                    assert await _collab_recv(room_b) == {
                        "type": "server", "event": "room-state", "mode": "ready",
                    }

    asyncio.run(scenario())


def test_collab_room_lock_survives_last_leave_after_a_joiner_captures_it():
    # Deterministic model of the race: the old last socket releases after a new socket has retained
    # (but not yet acquired) the lock. A third join must still receive that exact same lock.
    from hub import main as hub_main

    canvas_id = "lock-last-leave-concurrent-join"
    old_socket_lock = hub_main._retain_collab_room_lock(canvas_id)
    waiting_join_lock = hub_main._retain_collab_room_lock(canvas_id)
    assert waiting_join_lock is old_socket_lock
    hub_main._release_collab_room_lock(canvas_id, old_socket_lock)
    assert hub_main._collab_room_locks[canvas_id] is waiting_join_lock

    third_join_lock = hub_main._retain_collab_room_lock(canvas_id)
    assert third_join_lock is waiting_join_lock
    hub_main._release_collab_room_lock(canvas_id, waiting_join_lock)
    hub_main._release_collab_room_lock(canvas_id, third_join_lock)
    assert canvas_id not in hub_main._collab_room_locks
    assert canvas_id not in hub_main._collab_room_lock_refs


def test_collab_deadline_progress_isolated_from_slow_role_and_socket_io(monkeypatch):
    import asyncio
    from typing import cast

    from fastapi import WebSocket
    from hub import main as hub_main

    class FakeSocket:
        def __init__(self, send_gate: asyncio.Event | None = None):
            self.send_gate = send_gate
            self.sent: list[dict[str, object]] = []
            self.closed: list[int] = []

        async def send_json(self, payload: dict[str, object]) -> None:
            if self.send_gate is not None:
                await self.send_gate.wait()
            self.sent.append(payload)

        async def close(self, code: int) -> None:
            self.closed.append(code)

    monkeypatch.setattr(hub_main, "_COLLAB_SEED_READY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(hub_main, "_COLLAB_SEND_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(hub_main, "_collab_now", lambda: 100.0)

    async def scenario() -> None:
        canvas_id = "deadline-io-isolation"
        slow_send_gate, slow_role_gate = asyncio.Event(), asyncio.Event()
        slow_obj, fast_obj = FakeSocket(slow_send_gate), FakeSocket()
        slow, fast = cast(WebSocket, slow_obj), cast(WebSocket, fast_obj)
        lock = hub_main._retain_collab_room_lock(canvas_id)
        sender_tasks: list[asyncio.Task[None]] = []
        fanout_task: asyncio.Task[None] | None = None
        try:
            async with lock:
                room = hub_main._collab_rooms.setdefault(canvas_id, set())
                for order, peer in enumerate((slow, fast), start=1):
                    outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=8)
                    hub_main._collab_sessions[peer] = hub_main._CollabSession(
                        f"user-{order}", f"token-{order}", "editor", 0.0,
                    )
                    hub_main._collab_canvas[peer] = canvas_id
                    hub_main._collab_roles[peer] = "editor"
                    hub_main._collab_order[peer] = order
                    hub_main._collab_outboxes[peer] = outbox
                    room.add(peer)
                    task = asyncio.create_task(hub_main._run_collab_sender(peer, canvas_id, outbox))
                    hub_main._collab_sender_tasks[peer] = task
                    sender_tasks.append(task)
                hub_main._replan_collab_room_locked(room, canvas_id)

            async def slow_role_lookup(peer: WebSocket, _canvas_id: str) -> str:
                if peer is slow:
                    await slow_role_gate.wait()
                return "editor"

            monkeypatch.setattr(hub_main, "_live_collab_role", slow_role_lookup)
            fanout_task = asyncio.create_task(hub_main._fanout_collab(
                canvas_id, lock, None, {"type": "presence", "clientId": "probe"},
            ))

            started = asyncio.get_running_loop().time()
            while not any(frame.get("mode") == "seed" for frame in fast_obj.sent):
                if asyncio.get_running_loop().time() - started > 0.25:
                    raise AssertionError("seed deadline was blocked by unrelated role/socket I/O")
                await asyncio.sleep(0.005)
            assert not fanout_task.done(), "the role lookup should still be blocked"
            assert slow_obj.sent == [], "the slow websocket send should still be blocked"
            assert any(frame.get("clientId") == "probe" for frame in fast_obj.sent), (
                "the healthy peer should not wait for another peer's role lookup"
            )

            slow_role_gate.set()
            slow_send_gate.set()
            await asyncio.wait_for(fanout_task, timeout=0.2)
        finally:
            slow_role_gate.set()
            slow_send_gate.set()
            if fanout_task is not None:
                await asyncio.gather(fanout_task, return_exceptions=True)
            async with lock:
                room = hub_main._collab_rooms.get(canvas_id, set())
                for peer in (slow, fast):
                    if peer in room:
                        hub_main._detach_collab_peer_locked(peer, canvas_id, room, announce=False)
                hub_main._replan_collab_room_locked(room, canvas_id)
            await asyncio.gather(*sender_tasks, return_exceptions=True)
            hub_main._release_collab_room_lock(canvas_id, lock)
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_collab_authorization_query_characterization(monkeypatch):
    import asyncio
    from collections import Counter
    from typing import cast

    from fastapi import WebSocket
    from hub import main as hub_main

    class FakeSocket:
        pass

    now = [100.0]
    auth_checks: Counter[str] = Counter()
    role_queries: Counter[str] = Counter()
    users_by_token = {f"token-{index}": f"user-{index}" for index in range(1, 4)}

    def counted_verify(token: str) -> str | None:
        auth_checks[token] += 1
        return users_by_token.get(token)

    def counted_role(_canvas_id: str, user_id: str) -> str:
        role_queries[user_id] += 1
        return "editor"

    monkeypatch.setattr(hub_main, "_collab_now", lambda: now[0])
    monkeypatch.setattr(hub_main.auth, "verify", counted_verify)
    monkeypatch.setattr(hub_main.metadb, "canvas_role", counted_role)

    async def scenario() -> None:
        canvas_id = "collab-auth-query-characterization"
        peers = [cast(WebSocket, FakeSocket()) for _ in range(3)]
        sender = peers[0]
        lock = hub_main._retain_collab_room_lock(canvas_id)
        try:
            async with lock:
                room = hub_main._collab_rooms.setdefault(canvas_id, set())
                for order, peer in enumerate(peers, start=1):
                    hub_main._collab_sessions[peer] = hub_main._CollabSession(
                        f"user-{order}", f"token-{order}", "editor", now[0],
                    )
                    hub_main._collab_canvas[peer] = canvas_id
                    hub_main._collab_roles[peer] = "editor"
                    hub_main._collab_order[peer] = order
                    hub_main._collab_outboxes[peer] = asyncio.Queue(maxsize=128)
                    room.add(peer)

            async def emit_frame(frame: int) -> None:
                assert await hub_main._refresh_collab_sender_role(sender, canvas_id, lock) == "editor"
                await hub_main._fanout_collab(
                    canvas_id, lock, sender,
                    {"type": "presence", "clientId": f"probe-{frame}"},
                )

            # Admission authorization stays fresh across frame-rate traffic. Before the cache this
            # exact burst performed 12 session checks + 12 role queries for every peer (36 of each).
            for frame in range(12):
                await emit_frame(frame)
            assert auth_checks == Counter()
            assert role_queries == Counter()

            # At the exact interval boundary, concurrent frames share one revalidation per connection.
            interval = hub_main._COLLAB_ROLE_REVALIDATION_INTERVAL_SECONDS
            now[0] = 100.0 + interval
            await asyncio.gather(*(emit_frame(frame) for frame in range(12, 24)))
            assert auth_checks == Counter({token: 1 for token in users_by_token})
            assert role_queries == Counter({user_id: 1 for user_id in users_by_token.values()})

            # Frame count and recipient fanout cannot add queries while that authorization is fresh.
            now[0] = 100.0 + (2 * interval) - 0.001
            for frame in range(24, 36):
                await emit_frame(frame)
            assert auth_checks == Counter({token: 1 for token in users_by_token})
            assert role_queries == Counter({user_id: 1 for user_id in users_by_token.values()})

            # The next fixed boundary adds exactly one session + role query per active connection.
            now[0] = 100.0 + (2 * interval)
            await asyncio.gather(*(emit_frame(frame) for frame in range(36, 48)))
            assert auth_checks == Counter({token: 2 for token in users_by_token})
            assert role_queries == Counter({user_id: 2 for user_id in users_by_token.values()})
        finally:
            async with lock:
                room = hub_main._collab_rooms.get(canvas_id, set())
                for peer in peers:
                    if peer in room:
                        hub_main._detach_collab_peer_locked(
                            peer, canvas_id, room, announce=False,
                        )
            hub_main._release_collab_room_lock(canvas_id, lock)

    asyncio.run(scenario())


def test_collab_role_store_failure_fails_closed_at_revalidation(monkeypatch):
    import asyncio
    from typing import cast

    from fastapi import WebSocket
    from hub import main as hub_main

    class FakeSocket:
        def __init__(self) -> None:
            self.closed: list[int] = []

        async def close(self, code: int) -> None:
            self.closed.append(code)

    clock = _ManualCollabClock()
    monkeypatch.setattr(hub_main, "_collab_now", clock)
    monkeypatch.setattr(hub_main.auth, "verify", lambda _token: "role-store-user")
    role_queries = 0

    def broken_role_store(_canvas_id: str, _user_id: str) -> str:
        nonlocal role_queries
        role_queries += 1
        raise RuntimeError("role store unavailable")

    monkeypatch.setattr(hub_main.metadb, "canvas_role", broken_role_store)

    async def scenario() -> None:
        canvas_id = "collab-role-store-failure"
        socket_obj = FakeSocket()
        peer = cast(WebSocket, socket_obj)
        lock = hub_main._retain_collab_room_lock(canvas_id)
        try:
            async with lock:
                room = hub_main._collab_rooms.setdefault(canvas_id, set())
                hub_main._collab_sessions[peer] = hub_main._CollabSession(
                    "role-store-user", "token", "editor", clock(),
                )
                hub_main._collab_canvas[peer] = canvas_id
                hub_main._collab_roles[peer] = "editor"
                hub_main._collab_order[peer] = 1
                hub_main._collab_outboxes[peer] = asyncio.Queue(maxsize=8)
                room.add(peer)

            # A transient store problem is not consulted on every healthy frame while admission is fresh.
            assert await hub_main._refresh_collab_sender_role(peer, canvas_id, lock) == "editor"
            assert role_queries == 0

            # At the fixed boundary, the failed lookup removes and closes only the affected socket.
            clock.advance(hub_main._COLLAB_ROLE_REVALIDATION_INTERVAL_SECONDS)
            assert await hub_main._refresh_collab_sender_role(peer, canvas_id, lock) is None
            assert role_queries == 1
            assert peer not in hub_main._collab_rooms.get(canvas_id, set())
            assert socket_obj.closed == [1008]
        finally:
            async with lock:
                room = hub_main._collab_rooms.get(canvas_id, set())
                if peer in room:
                    hub_main._detach_collab_peer_locked(peer, canvas_id, room, announce=False)
            hub_main._release_collab_room_lock(canvas_id, lock)

    asyncio.run(scenario())


def test_collab_cancelled_initial_join_releases_all_lifecycle_state(monkeypatch):
    import asyncio
    from typing import cast

    from fastapi import WebSocket
    from hub import main as hub_main

    class WaitingSocket:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

        def __init__(self):
            self.accepted = asyncio.Event()

        async def accept(self) -> None:
            self.accepted.set()

        async def receive_json(self) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self, code: int) -> None:
            del code

    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)

    async def scenario() -> None:
        canvas_id = "cancelled-initial-collab-join"
        socket_obj = WaitingSocket()
        socket = cast(WebSocket, socket_obj)
        held_lock = hub_main._retain_collab_room_lock(canvas_id)
        await held_lock.acquire()
        task = asyncio.create_task(hub_main.ws_collab(socket, canvas_id))
        await asyncio.wait_for(socket_obj.accepted.wait(), timeout=0.2)
        while hub_main._collab_room_lock_refs.get(canvas_id, 0) < 2:
            await asyncio.sleep(0)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # cleanup remains shielded even if the ASGI server repeats cancellation
        held_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        hub_main._release_collab_room_lock(canvas_id, held_lock)

        assert canvas_id not in hub_main._collab_rooms
        assert socket not in hub_main._collab_sessions
        assert socket not in hub_main._collab_canvas
        assert socket not in hub_main._collab_order
        assert canvas_id not in hub_main._collab_room_locks
        assert canvas_id not in hub_main._collab_room_lock_refs

    asyncio.run(scenario())


def test_collab_handshake_relays_sync_through_reconnect(live_collab_url):
    # This is intentionally frame-level: the relay remains agnostic to opaque Yjs payloads while a
    # second client deterministically requests A's state on initial join and reconnect, with no sleeps.
    import asyncio
    import websockets

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/sync-reconnect"
        async with websockets.connect(url, proxy=None) as a:
            await _collab_seed(a, update="A-state")
            async with websockets.connect(url, proxy=None) as b:
                first_request = await _collab_sync(a, b, update="A-state", state_vector="b-state")
            async with websockets.connect(url, proxy=None) as b_reconnected:
                second_request = await _collab_sync(a, b_reconnected, update="A-state", state_vector="b2-state")
                assert second_request != first_request

    asyncio.run(scenario())


def test_collab_two_simultaneous_joiners_can_only_sync_from_the_ready_authority(live_collab_url):
    # Hold both authoritative replies until both joiners have requested state. The two empty joiners
    # cannot answer one another, and independent server request ids keep reversed replies correlated.
    import asyncio
    import websockets

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/slow-authority"
        async with websockets.connect(url, proxy=None) as authority:
            await _collab_seed(authority, update="newer-unpersisted-state")
            async with websockets.connect(url, proxy=None) as joiner_a:
                plan_a = await _collab_recv(joiner_a)
                async with websockets.connect(url, proxy=None) as joiner_b:
                    plan_b = await _collab_recv(joiner_b)
                    assert plan_a["mode"] == plan_b["mode"] == "sync"
                    assert plan_a["requestId"] != plan_b["requestId"]

                    await _collab_send(joiner_a, {
                        "type": "ysync", "requestId": plan_a["requestId"], "sv": "a-vector",
                    })
                    await _collab_send(joiner_b, {
                        "type": "ysync", "requestId": plan_b["requestId"], "sv": "b-vector",
                    })
                    requests = [await _collab_recv(authority), await _collab_recv(authority)]
                    assert {(msg["requestId"], msg["sv"]) for msg in requests} == {
                        (plan_a["requestId"], "a-vector"), (plan_b["requestId"], "b-vector"),
                    }

                    # An empty joiner tries to volunteer a reply for its peer. It is not synchronized,
                    # so the relay drops it and keeps both plans unchanged.
                    await _collab_send(joiner_a, {
                        "type": "yjs", "sync": True, "replyTo": plan_b["requestId"], "update": "EMPTY",
                    })
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(joiner_b.recv(), timeout=0.05)

                    for joiner, plan in ((joiner_b, plan_b), (joiner_a, plan_a)):
                        await _collab_send(authority, {
                            "type": "yjs", "sync": True, "replyTo": plan["requestId"],
                            "update": "newer-unpersisted-state",
                        })
                        assert await _collab_recv(joiner) == {
                            "type": "yjs", "sync": True, "replyTo": plan["requestId"],
                            "update": "newer-unpersisted-state",
                        }
                        await _collab_send(joiner, {"type": "sync-ready", "requestId": plan["requestId"]})
                        assert await _collab_recv(joiner) == {
                            "type": "server", "event": "room-state", "mode": "ready",
                        }

    asyncio.run(scenario())


def test_collab_sync_request_and_ready_ack_have_bounded_deadlines(monkeypatch, live_collab_url):
    # Bound the whole directed-sync handshake, not just the authority's response: a joiner that never
    # requests or never acknowledges a forwarded reply enters unavailable/backoff and can safely retry.
    import asyncio
    import websockets
    from hub import main as hub_main

    monkeypatch.setattr(hub_main, "_COLLAB_SYNC_REQUEST_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(hub_main, "_COLLAB_SYNC_READY_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(hub_main, "_COLLAB_UNAVAILABLE_RETRY_SECONDS", 0.05)

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/sync-phase-deadlines"
        async with websockets.connect(url, proxy=None) as authority:
            await _collab_seed(authority, update="authoritative-state")
            async with websockets.connect(url, proxy=None) as joiner:
                silent_request = await _collab_recv(joiner)
                assert silent_request["mode"] == "sync"
                assert (await _collab_recv(joiner))["mode"] == "unavailable"

                silent_ready = await _collab_recv(joiner)
                assert silent_ready["mode"] == "sync"
                assert silent_ready["requestId"] != silent_request["requestId"]
                await _collab_send(joiner, {
                    "type": "ysync", "requestId": silent_ready["requestId"], "sv": "joiner-vector",
                })
                assert (await _collab_recv(authority))["requestId"] == silent_ready["requestId"]
                await _collab_send(authority, {
                    "type": "yjs", "sync": True, "replyTo": silent_ready["requestId"],
                    "update": "authoritative-state",
                })
                assert (await _collab_recv(joiner))["replyTo"] == silent_ready["requestId"]
                assert (await _collab_recv(joiner))["mode"] == "unavailable"

                recovered = await _collab_recv(joiner)
                assert recovered["mode"] == "sync"
                assert recovered["requestId"] != silent_ready["requestId"]
                await _collab_send(joiner, {
                    "type": "ysync", "requestId": recovered["requestId"], "sv": "joiner-vector",
                })
                assert (await _collab_recv(authority))["requestId"] == recovered["requestId"]
                await _collab_send(authority, {
                    "type": "yjs", "sync": True, "replyTo": recovered["requestId"],
                    "update": "authoritative-state",
                })
                assert (await _collab_recv(joiner))["replyTo"] == recovered["requestId"]
                await _collab_send(joiner, {
                    "type": "sync-ready", "requestId": recovered["requestId"],
                })
                assert (await _collab_recv(joiner))["mode"] == "ready"

    asyncio.run(scenario())


def test_collab_silent_responder_rotates_to_another_ready_writer(monkeypatch, live_collab_url):
    # A live TCP connection is not proof that the browser can answer. Bound the first attempt, then
    # rotate to the next ready writer with a fresh request id; never let the joiner seed itself.
    import asyncio
    import websockets
    from hub import main as hub_main

    monkeypatch.setattr(hub_main, "_COLLAB_SYNC_RESPONSE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/silent-first-responder"
        async with websockets.connect(url, proxy=None) as first:
            await _collab_seed(first, update="newer-state")
            async with websockets.connect(url, proxy=None) as healthy:
                await _collab_sync(first, healthy, update="newer-state")
                async with websockets.connect(url, proxy=None) as joiner:
                    first_plan = await _collab_recv(joiner)
                    assert first_plan["mode"] == "sync"
                    await _collab_send(joiner, {
                        "type": "ysync", "requestId": first_plan["requestId"], "sv": "joiner-vector",
                    })
                    assert (await _collab_recv(first))["requestId"] == first_plan["requestId"]

                    replacement = await _collab_recv(joiner)
                    assert replacement["mode"] == "sync"
                    assert replacement["requestId"] != first_plan["requestId"]
                    await _collab_send(joiner, {
                        "type": "ysync", "requestId": replacement["requestId"], "sv": "joiner-vector",
                    })
                    assert await _collab_recv(healthy) == {
                        "type": "ysync", "requestId": replacement["requestId"], "sv": "joiner-vector",
                    }
                    await _collab_send(healthy, {
                        "type": "yjs", "sync": True, "replyTo": replacement["requestId"],
                        "update": "newer-state",
                    })
                    assert (await _collab_recv(joiner))["replyTo"] == replacement["requestId"]
                    await _collab_send(joiner, {
                        "type": "sync-ready", "requestId": replacement["requestId"],
                    })
                    assert (await _collab_recv(joiner))["mode"] == "ready"

    asyncio.run(scenario())


def test_collab_all_silent_authorities_become_unavailable_not_seed(monkeypatch, live_collab_url):
    # Exhausting ready responders is an availability failure, never evidence that the room is empty.
    import asyncio
    import websockets
    from hub import main as hub_main

    monkeypatch.setattr(hub_main, "_COLLAB_SYNC_RESPONSE_TIMEOUT_SECONDS", 0.04)

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/all-silent-responders"
        async with websockets.connect(url, proxy=None) as first:
            await _collab_seed(first, update="newer-state")
            async with websockets.connect(url, proxy=None) as second:
                await _collab_sync(first, second, update="newer-state")
                async with websockets.connect(url, proxy=None) as joiner:
                    for authority in (first, second):
                        plan = await _collab_recv(joiner)
                        assert plan["mode"] == "sync"
                        await _collab_send(joiner, {
                            "type": "ysync", "requestId": plan["requestId"], "sv": "joiner-vector",
                        })
                        assert (await _collab_recv(authority))["requestId"] == plan["requestId"]
                    assert await _collab_recv(joiner) == {
                        "type": "server", "event": "room-state", "mode": "unavailable",
                    }

    asyncio.run(scenario())


def test_collab_re_elects_waiting_joiner_when_unannounced_seed_vanishes(live_collab_url):
    # A seed can vanish before presence or sync-ready; the oldest waiting writer is elected immediately.
    import asyncio
    import websockets

    async def scenario() -> None:
        url = live_collab_url.replace("http://", "ws://") + "/ws/collab/vanished-peer"
        async with websockets.connect(url, proxy=None) as peer:
            assert (await _collab_recv(peer))["mode"] == "seed"
            async with websockets.connect(url, proxy=None) as joiner:
                assert (await _collab_recv(joiner))["mode"] == "wait"
                await peer.close()
                await _collab_seed(joiner, update="J-edit")
                async with websockets.connect(url, proxy=None) as later:
                    await _collab_sync(joiner, later, update="J-edit", state_vector="later-state")

    asyncio.run(scenario())


def test_collab_ws_requires_auth_when_enabled(monkeypatch):
    # with auth enabled, the collab channel is gated like the HTTP routes — no session → rejected
    import pytest
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    client.cookies.clear()
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/collab/some_canvas"):
                pass
    finally:
        client.cookies.clear()


def test_run_ws_requires_auth_when_enabled(monkeypatch):
    # the run-status stream carries per-node status, error text (may embed paths) + output names — gate
    # it like GET /run/{id} and the collab ws, instead of streaming to any unauthenticated socket.
    import pytest
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    client.cookies.clear()
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/run/run_whatever"):
                pass
    finally:
        client.cookies.clear()


def test_evict_never_drops_a_running_run():
    # a long run submitted early must not be evicted by 100 later submissions while still executing —
    # _evict skips non-terminal runs (else its status poll would 404 and strand the node).
    from hub.deps import get_deps
    from hub.models import RunStatus
    from hub.plugins.runner import _MAX_RUNS
    r = get_deps().runner
    with r._lock:
        saved = dict(r.runs)
        saved_published = dict(r._published_statuses)
        try:
            r.runs.clear()
            r._published_statuses.clear()
            r.runs["run_live"] = RunStatus(run_id="run_live", status="running")  # oldest + still running
            r._published_statuses["run_live"] = r.runs["run_live"].model_copy(deep=True)
            for i in range(_MAX_RUNS + 5):
                run_id = f"run_done_{i}"
                r.runs[run_id] = RunStatus(run_id=run_id, status="done")
                r._published_statuses[run_id] = r.runs[run_id].model_copy(deep=True)
            r._evict()
            assert "run_live" in r.runs          # the in-flight run survived
            assert len(r.runs) == _MAX_RUNS      # only terminal runs were dropped, down to the cap
        finally:
            r.runs.clear()
            r.runs.update(saved)
            r._published_statuses.clear()
            r._published_statuses.update(saved_published)


def test_run_state_persists_and_survives_loss_of_memory():
    # a run's status is mirrored to the shared DB (run_states), so GET /run/{id} still answers after the
    # owning runner forgets it in memory — the enabler for stateless web instances + restart survival.
    from hub import metadb
    from hub.deps import get_deps
    g = {"id": "cv_runstate", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("f", "filter", {"predicate": "amount > 0"}),
    ], "edges": [E("src", "f")]}
    rid = client.post("/api/run", json={"graph": g, "targetNodeId": "f", "confirmed": True}).json()["runId"]
    assert _poll(rid)["status"] == "done"
    assert metadb.get_run_state(rid)["status"] == "done"   # mirrored to the DB on the terminal transition
    # drop it from every in-memory runner + the index (simulate another instance / a kernel restart)
    deps = get_deps()
    for runner in deps.runners:
        runner.runs.pop(rid, None)
    deps.run_index.pop(rid, None)
    got = client.get(f"/api/run/{rid}").json()
    assert got["status"] == "done"                          # resolved from the DB, not the synthetic terminal
    assert "not found" not in (got.get("error") or "")


def test_reaper_fails_kernelless_orphan_but_spares_live_kernel_run():
    # a kernel-less (in-process) run left 'running' when the hub stopped must be reaped to terminal on
    # boot, else a client polls it forever. But a run owned by a STILL-LIVE kernel must survive so the
    # client can reattach — the exact distinction the old blanket reconcile got wrong.
    from hub import metadb
    metadb.save_run_state("run_orphan_x", {"run_id": "run_orphan_x", "status": "running", "per_node": []})
    metadb.claim_kernel("cv_live", "k_live_1", "tok")                 # a live kernel (fresh heartbeat)
    metadb.save_run_state("run_live_y", {"run_id": "run_live_y", "status": "running", "per_node": []},
                          canvas_id="cv_live", kernel_id="k_live_1")
    assert metadb.reap_orphaned_runs() >= 1
    assert metadb.get_run_state("run_orphan_x")["status"] == "failed"  # no kernel → reaped
    assert metadb.get_run_state("run_live_y")["status"] == "running"   # live kernel → spared (reattach)
    metadb.drop_kernel("cv_live", "k_live_1")


def test_periodic_reaper_spares_kernelless_run_but_fails_dead_kernel_run():
    # the PERIODIC reaper (only_kernel_runs=True) runs WHILE the hub lives — so unlike boot it must NOT
    # touch a kernel-less in-process run (that belongs to this live hub), yet it must still fail a run
    # whose owning kernel is gone (kernel crashed / restarted mid-run) — else that run spins 'running'
    # forever and the client reattaches to a ghost. This is the fix for the boot-only-reaper bug.
    from hub import metadb
    metadb.save_run_state("run_local_live", {"run_id": "run_local_live", "status": "running", "per_node": []})
    metadb.save_run_state("run_dead_kernel", {"run_id": "run_dead_kernel", "status": "running", "per_node": []},
                          canvas_id="cv_dead", kernel_id="k_gone")  # no lease for k_gone → its kernel is gone
    metadb.reap_orphaned_runs(only_kernel_runs=True)
    assert metadb.get_run_state("run_local_live")["status"] == "running"   # spared: a live hub owns it
    assert metadb.get_run_state("run_dead_kernel")["status"] == "failed"   # dead kernel → reaped


def test_cancel_falls_back_to_db_status_when_not_owned_here():
    # a run this process doesn't own (hub restarted, or another stateless instance accepted it): cancel
    # must resolve via the DB-backed kernel backend and return the last-known status, never 404.
    from hub import metadb
    metadb.save_run_state("run_cancel_db", {"run_id": "run_cancel_db", "status": "running", "per_node": []},
                          canvas_id="cv_cancel_db", kernel_id="k_none")  # no live kernel owns cv_cancel_db
    get_deps().run_index.pop("run_cancel_db", None)  # ensure this process doesn't own it
    r = client.post("/api/run/run_cancel_db/cancel")
    assert r.status_code == 200, r.text                       # not a 404
    assert r.json()["status"] in ("running", "cancelled", "failed")


def test_cancel_returns_full_terminal_detail_not_the_pruned_fence():
    # a finished run whose full RunState detail still exists (and thus also has a terminal fence) must
    # return that detail on cancel, never the compact fence projection that drops outputs / fabricates
    # a 'terminal_details_pruned' error.
    from hub import metadb
    metadb.save_run_state(
        "run_cancel_done",
        {"run_id": "run_cancel_done", "status": "done", "per_node": [],
         "target_node_id": "out", "rows_processed": 42, "total_rows": 42,
         "outputs": [{
             "node_id": "out", "port_id": "out", "wire": "dataset",
             "publication_kind": "catalog", "outcome": "committed",
             "uri": "s3://cancel-detail/out.parquet", "table": "out", "rows": 42,
         }],
         "progress": 1.0},
        canvas_id="cv_cancel_done", kernel_id="k_none")  # no live kernel owns it
    get_deps().run_index.pop("run_cancel_done", None)     # this process doesn't own it
    assert metadb.terminal_run_status("run_cancel_done") == "done"  # a fence row exists alongside detail
    body = client.post("/api/run/run_cancel_done/cancel").json()
    assert body["status"] == "done"
    assert _output_field(body, "uri", outcome="committed") == "s3://cancel-detail/out.parquet"
    assert _output_field(body, "table") == "out"
    assert body.get("error") is None


def test_kernel_for_run_none_when_owning_kernel_fenced_out():
    # after a takeover (k_old replaced by k_new on one canvas), cancel must NOT be routed to k_new — it
    # never ran the run. kernel_for_run returns None so the backend falls back to the persisted status.
    from hub import metadb
    metadb.claim_kernel("cv_kfr", "k_new", "tok"); metadb.mark_kernel_ready("cv_kfr", "k_new", "1.2.3.4:9")
    metadb.save_run_state("run_kfr", {"run_id": "run_kfr", "status": "running"},
                          canvas_id="cv_kfr", kernel_id="k_old")  # owned by a since-replaced kernel
    assert metadb.kernel_for_run("run_kfr") is None
    metadb.drop_kernel("cv_kfr", "k_new")


def test_restart_clears_lease_even_if_kernel_unreachable():
    # restart must be authoritative: it clears the lease itself, so a dead/unreachable kernel that can't
    # drop its own lease doesn't leave the canvas bound to a dead endpoint until the reaper fires.
    from hub import metadb
    from hub.metadb import Canvas, session
    cid = "cv_restart_auth"
    with session() as s:
        if s.get(Canvas, cid) is None:
            s.add(Canvas(id=cid, owner_id=metadb.DEFAULT_USER_ID, name="t", version=1, doc="{}", visibility="private"))
    metadb.claim_kernel(cid, "k_restart", "tok")
    metadb.mark_kernel_ready(cid, "k_restart", "127.0.0.1:1")  # unreachable endpoint (shutdown POST fails)
    r = client.post(f"/api/canvas/{cid}/kernel/restart")
    assert r.status_code == 200 and r.json()["restarted"] is True
    assert metadb.get_kernel(cid) is None  # lease cleared despite the kernel being unreachable


def test_kernel_lease_is_single_spawner_and_fenced():
    # the split-brain invariant: for one canvas, exactly one claimer wins; a fenced-out (replaced)
    # kernel can neither heartbeat nor drop the new owner's lease.
    from hub import metadb
    a = metadb.claim_kernel("cv_fence", "k_A", "tokA")
    b = metadb.claim_kernel("cv_fence", "k_B", "tokB")               # a fresh lease exists → B loses
    assert a["won"] is True and b["won"] is False
    assert metadb.heartbeat_kernel("cv_fence", "k_A") is True        # owner heartbeats
    assert metadb.heartbeat_kernel("cv_fence", "k_ghost") is False   # a stranger id is fenced out
    metadb.mark_kernel_ready("cv_fence", "k_A", "127.0.0.1:9999")
    assert metadb.get_kernel("cv_fence")["endpoint"] == "127.0.0.1:9999"
    metadb.drop_kernel("cv_fence", "k_ghost")                        # a zombie can't delete the owner
    assert metadb.get_kernel("cv_fence") is not None
    metadb.drop_kernel("cv_fence", "k_A")                            # the real owner releases
    assert metadb.get_kernel("cv_fence") is None


def test_active_runs_survive_a_simulated_hub_restart():
    # the reattach invariant: a run whose kernel is alive is SPARED by the boot reconcile (exactly what
    # a hub restart runs) and still surfaces via /canvas/{id}/active-runs with its target node, so a
    # reopened tab re-subscribes. (Actually SIGKILLing the hub process is a manual/e2e step.)
    from hub import metadb
    from hub.metadb import Canvas, session
    cid = "cv_reattach"
    with session() as s:
        if s.get(Canvas, cid) is None:
            s.add(Canvas(id=cid, owner_id=metadb.DEFAULT_USER_ID, name="t", version=1, doc="{}", visibility="private"))
    metadb.claim_kernel(cid, "k_reattach", "tok")     # a live kernel (fresh heartbeat)
    metadb.save_run_state("run_reattach", {"run_id": "run_reattach", "status": "running",
                                           "target_node_id": "sink", "per_node": []},
                          canvas_id=cid, kernel_id="k_reattach")
    metadb.reap_kernels(); metadb.reap_orphaned_runs()   # <- exactly what init_db runs on a hub restart
    assert metadb.get_run_state("run_reattach")["status"] == "running"   # spared: its kernel is alive
    active = client.get(f"/api/canvas/{cid}/active-runs").json()
    assert [r["runId"] for r in active] == ["run_reattach"]
    assert active[0]["targetNodeId"] == "sink"           # camelCase wire shape + the sink to re-bind to
    metadb.drop_kernel(cid, "k_reattach")


def test_canvas_kernel_state_and_restart():
    # the Jupyter-style kernel controls: state is visible per canvas, and restart is a safe no-op when
    # nothing's live (the next run spawns fresh). Token/endpoint are never exposed.
    from hub import metadb
    from hub.metadb import Canvas, session
    cid = "cv_krestart"
    with session() as s:
        if s.get(Canvas, cid) is None:
            s.add(Canvas(id=cid, owner_id=metadb.DEFAULT_USER_ID, name="t", version=1, doc="{}", visibility="private"))
    assert client.get(f"/api/canvas/{cid}/kernel").json() == {"exists": False}
    metadb.claim_kernel(cid, "k1", "tok")                      # a lease, not yet ready (no endpoint)
    st = client.get(f"/api/canvas/{cid}/kernel").json()
    assert st == {"exists": True, "state": "starting", "stale": False} and "token" not in st
    r = client.post(f"/api/canvas/{cid}/kernel/restart").json()
    assert r == {"ok": True, "restarted": False}               # not ready → nothing to shut down
    metadb.drop_kernel(cid, "k1")


def test_canvas_declared_deps_gate_the_sandbox_import_allowlist():
    # ① per-canvas deps: a module outside the default cell allowlist is blocked, UNTIL the canvas
    # declares it (the kernel installs it → sandbox.allow_modules), then a transform can import it.
    from hub import sandbox
    src = "import base64\ndef fn(row): return row"
    try:
        sandbox.compile_operator(src, "map")
        assert False, "base64 should be blocked by default"
    except sandbox.SandboxError:
        pass
    try:
        sandbox.allow_modules({"base64"})          # what the kernel does after installing declared deps
        assert callable(sandbox.compile_operator(src, "map"))
    finally:
        sandbox._KERNEL_ALLOWED.discard("base64")  # don't leak the allowance to other tests


def test_pod_spawner_builds_manifests_and_conforms_to_the_spi():
    # Phase 3: the PodSpawner is the cross-host substrate behind the SAME KernelSpawner protocol. Test
    # the manifest it builds with a FAKE k8s client (no cluster needed) — a pod running `hub.kernel`
    # bound to 0.0.0.0 advertising its Service DNS, and a Service that selects it.
    from hub.backends import KernelSpawner
    from hub.kernel_backend import LocalProcessSpawner
    from hub.pod_spawner import PodSpawner
    assert isinstance(LocalProcessSpawner("/ws", "/ws/data"), KernelSpawner)  # both substrates conform

    class FakeApi:
        def __init__(self): self.calls = []
        def create_namespaced_service(self, ns, body): self.calls.append(("svc", ns, body))
        def create_namespaced_pod(self, ns, body): self.calls.append(("pod", ns, body))
        def delete_namespaced_pod(self, name, ns, **kw): self.calls.append(("del-pod", name, ns))
        def delete_namespaced_service(self, name, ns, **kw): self.calls.append(("del-svc", name, ns))

    api = FakeApi()
    sp = PodSpawner("/ws", "/ws/data", client=api)
    assert isinstance(sp, KernelSpawner)
    sp.spawn("cv-abc", "k1", "tok123")
    assert [c[0] for c in api.calls] == ["svc", "pod"]      # Service created before the Pod
    svc, pod = api.calls[0][2], api.calls[1][2]
    name = svc["metadata"]["name"]
    assert name.startswith("dp-kernel-")
    cmd = pod["spec"]["containers"][0]["command"]
    assert cmd[:3] == ["python", "-m", "hub.kernel"]
    assert {"cv-abc", "k1", "tok123", "0.0.0.0"} <= set(cmd)  # our canvas/kernel/token + bind-all
    assert f"{name}.default.svc.cluster.local" in cmd        # advertise-host = the Service DNS
    assert svc["metadata"]["labels"] == {"app": "dp-kernel", "dp-canvas": name}
    assert svc["spec"]["selector"]["dp-canvas"] == pod["metadata"]["labels"]["dp-canvas"]  # Service → Pod
    sp.kill("cv-abc", "k1")
    assert ("del-pod", name, "default") in api.calls and ("del-svc", name, "default") in api.calls


def test_pod_spawner_names_per_kernel_and_is_fenced_and_idempotent():
    # regressions: (a) the pod/service name must be unique per (canvas, kernel_id) so a new kernel never
    # collides with a still-terminating old one; (b) kill is thereby FENCED — killing k1 can't delete
    # k2's objects; (c) a 409 AlreadyExists on create (a retry of the same kernel_id) is tolerated.
    from hub.pod_spawner import PodSpawner

    class Fake:
        def __init__(self): self.created, self.deleted, self.pod_409 = [], [], False
        def create_namespaced_service(self, ns, body): self.created.append(body["metadata"]["name"])
        def create_namespaced_pod(self, ns, body):
            if self.pod_409:
                e = Exception("conflict"); e.status = 409; raise e
            self.created.append(body["metadata"]["name"])
        def delete_namespaced_pod(self, name, ns, **kw): self.deleted.append(name)
        def delete_namespaced_service(self, name, ns, **kw): self.deleted.append(name)

    fake = Fake()
    sp = PodSpawner("/ws", "/ws/data", client=fake)
    n1, n2 = sp._name("cv", "k1"), sp._name("cv", "k2")
    assert n1 != n2                                   # same canvas, different kernel → distinct names
    sp.spawn("cv", "k1", "t"); sp.spawn("cv", "k2", "t")
    sp.kill("cv", "k1")                               # fenced: only k1's objects, never k2's (the new owner)
    assert set(fake.deleted) == {n1}
    fake.pod_409 = True
    sp.spawn("cv", "k3", "t")                         # a 409 on pod create must NOT raise (idempotent)


def test_adapter_and_catalog_conform_to_formal_protocols():
    # the two seams that were duck-typed are now runtime_checkable Protocols, and the BUILT-INS conform —
    # i.e. the built-in adapters + catalog are the first implementations through the seam, not a privileged
    # core path. A plugin has a typed target instead of reverse-engineering call sites.
    from hub.backends import CatalogProvider, DatasetAdapter, DatasetPreviewAdapter
    from hub.plugins.adapters import DuckDBAdapter, LanceAdapter

    class FullRunOnlyAdapter:
        """A valid third-party adapter must not be forced to claim bounded preview support."""

        name = "full-run-only"

        def matches(self, _uri): return True
        def scan(self, _uri, columns=None, predicate=None, limit=None, options=None): return None
        def schema(self, _uri): return []
        def count(self, _uri): return None
        def fingerprint(self, _uri): return "full-run-only"
        def write(self, uri, _rel, mode="overwrite"): return {"uri": uri, "rows": 0}

    assert isinstance(DuckDBAdapter(), DatasetAdapter)
    assert isinstance(LanceAdapter(), DatasetAdapter)
    assert isinstance(DuckDBAdapter(), DatasetPreviewAdapter)
    assert isinstance(LanceAdapter(), DatasetPreviewAdapter)
    assert isinstance(FullRunOnlyAdapter(), DatasetAdapter)
    assert not isinstance(FullRunOnlyAdapter(), DatasetPreviewAdapter)
    assert isinstance(get_deps().catalog, CatalogProvider)  # the built-in InMemoryCatalog IS the reference


def test_plugin_capability_detector_tags_columns():
    # add_capability is a real seam now: a capability with a detect(col)->bool tags matching columns via
    # tag_columns, no core edit. (Built-in media/vector still run from the hardcoded heuristics.)
    from hub.deps import Registry, get_deps
    from hub.models import ColumnSchema
    from hub.plugins import capabilities as caps

    class GeoCap:
        id = "geo"
        label = "Geo"
        def detect(self, col):
            return col.name in ("lat", "lon")

    Registry(get_deps()).add_capability(GeoCap())
    cols = caps.tag_columns([ColumnSchema(name="lat", type="float"), ColumnSchema(name="city", type="string")])
    assert "geo" in cols[0].capabilities and "geo" not in cols[1].capabilities


def test_sql_catalog_reference_plugin(tmp_path):
    # the shipped examples/plugins/dp_sql_catalog SqlCatalog surfaces datasets from a SQL (name, uri)
    # table — the read-external catalog pattern: overrides bounded reads, inherits resolve_ref +
    # the KeyError-on-miss contract. Proves the CatalogProvider seam end-to-end (SQLite; no extra dep).
    import importlib.util
    from pathlib import Path
    import duckdb
    import pytest as _pt
    import sqlalchemy as sa
    from hub.deps import get_deps

    p = str(tmp_path / "sales.parquet")
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT i AS id, i * 2 AS amt FROM range(0, 5) t(i)) TO '{p}' (FORMAT PARQUET)")
    dburl = "sqlite:///" + str(tmp_path / "meta.db")
    eng = sa.create_engine(dburl)
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE datasets (name TEXT, uri TEXT)"))
        c.execute(sa.text("INSERT INTO datasets VALUES ('sales', :u)"), {"u": p})

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_sql_catalog" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_sql_catalog_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    cat = mod.SqlCatalog(str(tmp_path / "empty"), get_deps().resolve_adapter, dburl, "datasets")
    assert isinstance(cat, __import__("hub.backends", fromlist=["CatalogProvider"]).CatalogProvider)
    assert "sales" in {t.name for t in cat.list_page(CatalogQuery(limit=5000)).items}
    t = cat.get_table("sales")
    assert t.uri == p and {c.name for c in t.columns} == {"id", "amt"} and t.row_count == 5
    assert cat.resolve_ref("sales") == p          # inherited: bare name → uri
    with _pt.raises(KeyError):                     # inherited contract: miss raises KeyError
        cat.get_table("nonexistent")


def test_plugin_secret_not_leaked_via_settings():
    # A plugin's secret [[config]] field stores a reference, never the material token. GET echoes the
    # reference; PUT rejects a raw secret. Non-secret plugin config remains a normal string setting.
    import json as _json

    from hub import metadb
    from hub.deps import get_deps

    deps = get_deps()
    fake = {"name": "dp_secretpk", "source": "drop-in",
            "config": [{"key": "token", "type": "password", "secret": True},
                       {"key": "host", "type": "string"}]}
    deps.plugins.append(fake)
    try:
        bad = client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretpk.token", "value": "sk-LEAK-xyz"})
        assert bad.status_code == 400 and "secret reference" in bad.json()["detail"]
        client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretpk.token", "value": "env:DP_SECRET_PK_TOKEN"})
        client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretpk.host", "value": "db.internal"})
        r = client.get("/api/settings").json()
        assert r["global"]["plugin.dp_secretpk.token"] == "env:DP_SECRET_PK_TOKEN"
        assert "sk-LEAK-xyz" not in _json.dumps(r)
        assert r["global"]["plugin.dp_secretpk.host"] == "db.internal"
        assert metadb.get_setting("plugin.dp_secretpk.token", "global") == "env:DP_SECRET_PK_TOKEN"
    finally:
        deps.plugins.remove(fake)
        metadb.set_setting("plugin.dp_secretpk.token", "", "global")
        metadb.set_setting("plugin.dp_secretpk.host", "", "global")


def test_json_view_capability_reference_plugin(tmp_path):
    # the dp_json_view reference plugin adds a VIEWER TAB with no frontend code: its detector tags
    # JSON-doc columns, and its declarative viewer is surfaced in KernelInfo.capability_views for the
    # SPA to render generically. Proves the capability seam (detector + viewer) end-to-end.
    import shutil
    from pathlib import Path

    from hub.deps import Deps
    from hub.models import ColumnSchema
    from hub.plugins.capabilities import tag_columns

    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_json_view"
    shutil.copytree(src, ws / "plugins" / "dp_json_view")
    deps = Deps(str(ws), str(tmp_path / "data"))

    views = {v.id: v for v in deps.info().capability_views}
    assert "json-doc" in views                                      # surfaced for the SPA
    assert views["json-doc"].label == "JSON" and views["json-doc"].viewer == {"kind": "json"}

    cols = tag_columns([ColumnSchema(name="payload", type="string"), ColumnSchema(name="qty", type="int")])
    tagged = {c.name: c.capabilities for c in cols}
    assert "json-doc" in tagged["payload"] and "json-doc" not in tagged["qty"]  # detector tags the right column


def test_plugin_config_resolution(tmp_path, monkeypatch):
    # reg.config precedence for a pack's dataplay.toml [[config]] field:
    #   UI setting (plugin.<pack>.<key>) > declared env var > declared default > the arg default.
    from hub import metadb
    from hub.deps import Deps, Registry

    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    deps._manifests["dp_cfgx"] = {"config": [
        {"key": "url", "type": "string", "env": "DP_CFGX_URL"},
        {"key": "table", "type": "string", "default": "datasets"},
    ]}
    reg = Registry(deps); reg._pack = "dp_cfgx"
    assert reg.config("table") == "datasets"          # declared default
    assert reg.config("url") is None                  # nothing set anywhere
    assert reg.config("url", "arg") == "arg"           # falls to the arg default
    monkeypatch.setenv("DP_CFGX_URL", "sqlite:///env.db")
    assert reg.config("url") == "sqlite:///env.db"     # declared env var
    metadb.set_setting("plugin.dp_cfgx.url", "sqlite:///ui.db", "global")
    assert reg.config("url") == "sqlite:///ui.db"      # UI setting WINS over env
    assert Registry(deps).config("url", "d") == "d"    # no current pack → only the arg default


def test_plugin_config_schema_surfaces_via_manifest(tmp_path, monkeypatch):
    # dropping dp_sql_catalog in: its dataplay.toml [[config]] lands on the /api/plugins entry, and
    # register() activates the plugin via reg.config's env fallback (DP_SQL_CATALOG_URL) — proving the
    # declarative-schema → configurable-plugin path end-to-end.
    import shutil
    from pathlib import Path

    import sqlalchemy as sa

    from hub.deps import Deps

    dburl = "sqlite:///" + str(tmp_path / "cat.db")
    with sa.create_engine(dburl).begin() as c:
        c.execute(sa.text("CREATE TABLE datasets (name TEXT, uri TEXT)"))
    monkeypatch.setenv("DP_SQL_CATALOG_URL", dburl)

    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_sql_catalog"
    shutil.copytree(src, ws / "plugins" / "dp_sql_catalog")
    deps = Deps(str(ws), str(tmp_path / "data"))

    assert deps.catalog.name == "sql-catalog"          # reg.config → env fallback activated it
    entry = next(p for p in deps.plugins if p["name"] == "dp_sql_catalog")
    fields = {f["key"]: f for f in entry["config"]}    # [[config]] parsed + attached to the plugin entry
    assert set(fields) == {"url", "table"}
    assert fields["url"]["env"] == "DP_SQL_CATALOG_URL" and fields["table"]["default"] == "datasets"


def test_hf_datasets_adapter_reference_plugin(monkeypatch):
    # the shipped examples/plugins/dp_hf_datasets adapter reads hf:// datasets. Proven WITHOUT network: a
    # real in-memory HF Dataset is returned by a patched load_dataset, so the real arrow→DuckDB path runs.
    ds_mod = __import__("pytest").importorskip("datasets")
    import importlib.util
    from pathlib import Path
    from hub import db
    from hub.backends import DatasetAdapter, DatasetPreviewAdapter

    fake = ds_mod.Dataset.from_dict({"id": [1, 2, 3], "txt": ["a", "b", "c"]})
    loads: list[tuple[str, str | None, str | None]] = []

    def load_dataset(name, config=None, split=None):
        loads.append((name, config, split))
        return fake

    monkeypatch.setattr(ds_mod, "load_dataset", load_dataset)
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_hf_datasets" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_hf_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    a = mod.HfDatasetsAdapter()
    assert isinstance(a, DatasetAdapter)
    assert not isinstance(a, DatasetPreviewAdapter)
    assert a.matches("hf://x:train") and not a.matches("/tmp/x.parquet")
    assert mod._parse("hf://glue@mrpc:validation") == ("glue", "mrpc", "validation")
    assert mod._parse("hf://stanfordnlp/imdb") == ("stanfordnlp/imdb", None, "train")  # id keeps its '/'
    assert a.fingerprint("hf://x") == a.fingerprint("hf://x")
    assert loads == [], "the URI-only fingerprint must not load the remote dataset during preflight"
    assert {c.name for c in a.schema("hf://x")} == {"id", "txt"}   # schema/count manage their own base_guard
    assert a.count("hf://x") == 3
    with db.base_guard():
        assert len(a.scan("hf://x", limit=2).fetchall()) == 2


def test_iceberg_adapter_reference_plugin(monkeypatch):
    # the shipped examples/plugins/dp_iceberg adapter reads iceberg:// tables. Exercised against a stand-in
    # catalog (no live warehouse): a fake load_catalog → table.scan().to_arrow() returns a pyarrow table,
    # so the adapter's uri-parse + arrow→DuckDB + column/limit path runs. importorskip → CI skips without it.
    import pytest as _pt
    _pt.importorskip("pyiceberg")
    import importlib.util
    from pathlib import Path
    import pyarrow as pa
    import pyiceberg.catalog as pc
    from hub import db
    from hub.backends import DatasetAdapter, DatasetPreviewAdapter

    tbl = pa.table({"id": [1, 2], "v": ["a", "b"]})
    scan = type("S", (), {"to_arrow": lambda self: tbl})()
    table = type("T", (), {"scan": lambda self, **kw: scan})()
    loads: list[str | None] = []

    def load_catalog(name=None, **_kwargs):
        loads.append(name)
        return type("C", (), {"load_table": lambda self, i: table})()

    monkeypatch.setattr(pc, "load_catalog", load_catalog)

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_iceberg" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_iceberg_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    a = mod.IcebergAdapter()
    assert isinstance(a, DatasetAdapter)
    assert not isinstance(a, DatasetPreviewAdapter)
    assert a.matches("iceberg://prod/sales.orders") and not a.matches("/tmp/x.parquet")
    assert mod._parse("iceberg://prod/sales.orders") == ("prod", "sales.orders")  # first '/' splits catalog
    assert a.fingerprint("iceberg://prod/db.t") == a.fingerprint("iceberg://prod/db.t")
    assert loads == [], "the URI-only fingerprint must not load the table during preflight"
    assert {c.name for c in a.schema("iceberg://prod/db.t")} == {"id", "v"}
    assert a.count("iceberg://prod/db.t") == 2
    with db.base_guard():
        assert len(a.scan("iceberg://prod/db.t", columns=["id"], limit=1).fetchall()) == 1


def test_placeable_backend_protocol():
    # the optional distributed-placement contract exists as a typed Protocol; a full impl conforms, a
    # partial one does not (the core feature-detects per method, so a non-distributed backend omits them).
    from hub.backends import PlaceableBackend

    class FullPlaceable:
        def workers(self): return []
        def place(self, requires): return None
        def run_unit(self, graph, output_node, output_uri, requires=None): return None

    class Partial:
        def workers(self): return []

    assert isinstance(FullPlaceable(), PlaceableBackend)
    assert not isinstance(Partial(), PlaceableBackend)


def test_kernel_spawner_selectable_via_dotted_path(monkeypatch):
    # a 3rd substrate is a config value, not a core patch: DP_KERNEL_SPAWNER=pkg.mod:Cls loads the plugin.
    from hub import deps as depsmod
    from hub.settings import settings
    monkeypatch.setattr(settings, "kernel_spawner", "hub.tests.test_kernel:_FakeSpawner")
    sp = depsmod._make_spawner("/ws", "/ws/data")
    assert type(sp).__name__ == "_FakeSpawner" and sp.args == ("/ws", "/ws/data")


def test_storage_selectable_via_dotted_path(monkeypatch):
    from hub import storage
    monkeypatch.setenv("DP_STORAGE", "hub.tests.test_kernel:_FakeStorage")
    s = storage.make_storage("/ws")
    assert type(s).__name__ == "_FakeStorage" and s.ws == "/ws"


def test_kernel_spawner_is_selectable_pod(tmp_path, monkeypatch):
    # DP_KERNEL_SPAWNER=pod swaps the substrate under KernelBackend without touching anything else
    from hub import settings as sm
    from hub.deps import Deps
    from hub.pod_spawner import PodSpawner
    monkeypatch.setattr(sm.settings, "kernel_spawner", "pod")
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))  # constructing PodSpawner is lazy (no k8s client yet)
    assert isinstance(d.kernel_backend().spawner, PodSpawner)


def test_kernel_backend_runs_a_canvas_end_to_end():
    # DP_EXECUTION=kernel routes a run to a real, DETACHED per-canvas kernel PROCESS: it runs the job on
    # its own warm engine and writes run_states, which the hub reads back → done. Exercises the whole
    # path (atomic lease → spawn → token-authed loopback command channel → run → status via the DB).
    from hub import deps as dm, kernel_backend, metadb
    from hub import settings as sm
    canvas_id = f"cv_kernel_e2e_{uuid.uuid4().hex}"
    os.environ["DP_KERNEL_IDLE_TTL"] = "6"          # backstop: the detached kernel self-exits soon if idle
    old = sm.settings.execution
    sm.settings.execution = "kernel"
    dm.set_workspace(sm.settings.workspace, sm.settings.data_dir)   # rebuild deps → registers KernelBackend
    try:
        import lance
        import pyarrow as pa

        deps = dm.get_deps()
        assert any(getattr(r, "name", "") == "kernel" for r in deps.runners)
        source_uri = os.path.join(sm.settings.data_dir, f"kernel-e2e-{uuid.uuid4().hex}.lance")
        lance.write_dataset(pa.table({"amount": [1, 2, 3]}), source_uri)
        deps.catalog._add(
            name=f"kernel_e2e_{uuid.uuid4().hex}", uri=source_uri, strict_probe=True)
        with metadb.session() as session:
            session.add(metadb.Canvas(id=canvas_id, owner_id="local", name="kernel e2e"))
        g = {"id": canvas_id, "version": 1, "nodes": [
            N("src", "source", {"uri": source_uri}),
            N("flt", "filter", {"predicate": "amount > 1"}),
        ], "edges": [E("src", "flt")]}
        r = client.post("/api/run", json={
            "graph": g, "targetNodeId": "flt", "confirmed": True,
            "submissionId": str(uuid.uuid4()),
        }).json()
        st = _poll(r["runId"], tries=400)
        assert st["status"] == "done", st.get("error")
        assert metadb.get_kernel(canvas_id) is not None   # a live kernel owned the run
        # Phase 2 step 1: preview + profile also route to the (same, warm) kernel and return correct data
        pv = client.post("/api/run/preview", json={"graph": g, "nodeId": "flt", "k": 5}).json()
        assert not pv["notPreviewable"] and not pv.get("error"), pv
        assert all(row["amount"] > 1 for row in pv["rows"])          # filter really ran on the kernel
        pf = client.post("/api/run/profile", json={"graph": g, "nodeId": "flt"}).json()
        assert pf["sampled"] is True and any(c["name"] == "amount" for c in pf["columns"])
    finally:
        k = metadb.get_kernel(canvas_id)
        if k and k.get("endpoint"):
            try:
                kernel_backend._post(k["endpoint"], "/shutdown", k["token"], {}, timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
        os.environ.pop("DP_KERNEL_IDLE_TTL", None)
        sm.settings.execution = old
        dm.set_workspace(sm.settings.workspace, sm.settings.data_dir)  # restore clean runners (pinned contract)


def test_catalog_entries_are_shared_across_instances(tmp_path):
    # a dataset/output registered on one instance's catalog is visible to ANOTHER instance (and after a
    # restart) via the shared DB — the catalog half of making the web tier stateless.
    from hub.deps import get_deps
    from hub.plugins.catalog import InMemoryCatalog
    deps = get_deps()
    uri = str(tmp_path / "shared_out.parquet")
    deps.catalog.register_output(name="shared_out_x", uri=uri, version="v1", parents=[], pipeline="canvas")
    # a FRESH catalog with an empty data_dir (a different web instance) seeds nothing locally, but loads
    # the entry from the shared DB on read
    other = InMemoryCatalog(str(tmp_path / "empty_dir"), deps.resolve_adapter)
    assert "shared_out_x" in [
        t.name for t in other.list_page(CatalogQuery(limit=5000)).items]
    assert other.get_table("shared_out_x").uri == uri


def test_catalog_reflects_updates_and_deletions_across_instances(tmp_path):
    # P0-CAT-01: a peer's cache must converge on UPDATES and DELETES another instance made — not just
    # additions. The shared DB is authoritative; each read reconciles against it.
    from hub.deps import get_deps
    from hub.models import CatalogTable
    from hub.plugins.catalog import InMemoryCatalog
    deps = get_deps()
    uri = str(tmp_path / "conv.parquet")
    deps.catalog.register_output(name="conv_ds", uri=uri, version="v1", parents=[], pipeline="canvas")
    tid = deps.catalog.get_table("conv_ds").id
    peer = InMemoryCatalog(str(tmp_path / "peer_dir"), deps.resolve_adapter)
    assert peer.get_table("conv_ds").version == "v1"  # peer now has it cached
    # UPDATE on the first instance → peer converges (previously it kept the stale v1 forever)
    deps.catalog.register(CatalogTable(id=tid, name="conv_ds", uri=uri, version="v2", row_count=99))
    assert peer.get_table("conv_ds").version == "v2"
    assert peer.get_table("conv_ds").row_count == 99
    # DELETE on the first instance → peer drops it (previously it kept serving the removed dataset)
    assert deps.catalog.unregister("conv_ds") is True
    with pytest.raises(KeyError):
        peer.get_table("conv_ds")
    assert "conv_ds" not in [
        t.name for t in peer.list_page(CatalogQuery(limit=5000)).items]


def test_pipelines_import_reports_not_configured():
    # with no importer plugin, the endpoint must HONESTLY report 501 not-configured — it used to 400
    # with an AttributeError because Deps had no .importer attr (dead scaffolding). Now deps.importer
    # defaults to NullImporter → ImporterNotConfigured → 501.
    r = client.post("/api/pipelines/import", json={"config": "x", "params": {}})
    assert r.status_code == 501
    assert "importer" in r.text.lower()


@pytest.mark.parametrize("source_handle", [None, "missing"])
def test_pipeline_import_rejects_invalid_multi_output_source_handles(source_handle):
    from hub.models import Graph, PipelineImport

    graph = Graph.model_validate({
        "id": "imported-invalid", "version": 1,
        "nodes": [
            N("section", "section", {"outputs": ["left", "right"]}),
            N("filter", "filter", {"predicate": "value > 0"}),
        ],
        "edges": [{
            "id": "section-filter", "source": "section", "target": "filter",
            "sourceHandle": source_handle, "data": {"wire": "dataset"},
        }],
    })
    deps = get_deps()
    previous = deps.importer
    deps.importer = SimpleNamespace(import_pipeline=lambda config, _params: PipelineImport(
        config=config, graph=graph))
    try:
        response = client.post("/api/pipelines/import", json={"config": "foreign"})
    finally:
        deps.importer = previous

    assert response.status_code == 400
    assert "source handle" in response.text.lower()


def test_pipeline_import_accepts_an_exact_multi_output_source_handle():
    from hub.models import Graph, PipelineImport

    graph = Graph.model_validate({
        "id": "imported-valid", "version": 1,
        "nodes": [
            N("section", "section", {"outputs": ["left", "right"]}),
            N("filter", "filter", {"predicate": "value > 0"}),
        ],
        "edges": [{
            "id": "section-filter", "source": "section", "target": "filter",
            "sourceHandle": "right", "data": {"wire": "dataset"},
        }],
    })
    deps = get_deps()
    previous = deps.importer
    deps.importer = SimpleNamespace(import_pipeline=lambda config, _params: PipelineImport(
        config=config, graph=graph))
    try:
        response = client.post("/api/pipelines/import", json={"config": "foreign"})
    finally:
        deps.importer = previous

    assert response.status_code == 200
    assert response.json()["graph"]["edges"][0]["sourceHandle"] == "right"


def test_datasets_place_destination_reference_plugin(tmp_path):
    # the dp_datasets_place reference destination goes through reg.add_destination (the DestinationBackend
    # seam) and browses only dataset files, hiding clutter; path traversal is fenced to the root.
    import importlib.util
    from pathlib import Path

    from hub import destinations
    from hub.destinations import DestinationBackend

    root = tmp_path / "place"
    (root / "sub").mkdir(parents=True)
    for fn in ("a.parquet", "b.csv", "notes.txt", ".hidden.parquet"):
        (root / fn).write_text("x")

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_datasets_place" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_datasets_place_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    b = mod.DatasetsPlace()
    assert isinstance(b, DestinationBackend)                       # conforms to the formal Protocol
    entries = b.browse(str(root), "")["entries"]
    names = {e["name"]: e["kind"] for e in entries}
    assert names == {"a.parquet": "file", "b.csv": "file", "sub": "dir"}  # .txt + dotfile hidden
    assert b.target_uri(str(root), "sub", "../../etc/x.parquet").endswith("x.parquet")  # basename only
    assert os.path.realpath(b.target_uri(str(root), "../..", "o.parquet")).startswith(os.path.realpath(str(root)))

    # reg.add_destination registers the kind through the real Registry (not a module-level call)
    (tmp_path / "ws").mkdir()
    from hub.deps import Deps, Registry
    reg = Registry(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    reg.add_destination(mod.DatasetsPlace())
    assert "datasets" in destinations.backend_kinds()


def test_layout_handles_large_reverse_ordered_graph():
    # graph.layout must be iterative — a long chain listed sink-first (reverse topological order) used to
    # overflow the recursion limit and 500 the import route. Now it lays out left-to-right by depth.
    from hub import graph as gmod
    from hub.models import Graph

    n = 700
    nodes = [{"id": f"x{i}", "type": "select", "position": {"x": 0, "y": 0}, "data": {"config": {}}} for i in range(n)]
    edges = [{"id": f"e{i}", "source": f"x{i}", "target": f"x{i+1}", "data": {"wire": "dataset"}} for i in range(n - 1)]
    gr = Graph(**{"id": "c", "version": 1, "nodes": list(reversed(nodes)), "edges": edges})  # sink-first
    gmod.layout(gr)  # must not RecursionError
    pos = {node.id: node.position for node in gr.nodes}
    assert pos["x0"].x < pos["x350"].x < pos["x699"].x  # root → … → sink, laid out by increasing depth


def test_json_pipeline_importer_round_trips_to_a_run():
    # The dp_json_pipeline reference importer parses a JSON pipeline into a runnable canvas graph; the
    # /pipelines/import route lays it out; the returned graph runs end-to-end — proving import→canvas→run
    # plugin-only (readiness item 8). Loaded via importlib and swapped onto deps.importer for the test.
    import importlib.util
    import json
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_json_pipeline" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_json_pipeline_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    from hub.plugins.importer import Importer
    imp = mod.JsonPipelineImporter()
    assert isinstance(imp, Importer)  # conforms to the formal SPI Protocol

    deps = get_deps()
    prev = deps.importer
    deps.importer = imp
    try:
        cfg = json.dumps({"source": _uri("events"),
                          "steps": [{"filter": "amount > 0"}, {"select": "amount"}],
                          "write": {"name": "imported_out"}})
        g = client.post("/api/pipelines/import", json={"config": cfg}).json()["graph"]
        assert [n["type"] for n in g["nodes"]] == ["source", "filter", "select", "write"]
        assert [(e["source"], e["target"]) for e in g["edges"]] == [("src", "step1"), ("step1", "step2"), ("step2", "sink")]
        assert not all(n["position"]["x"] == 0 and n["position"]["y"] == 0 for n in g["nodes"])  # route laid it out
        st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "sink", "confirmed": True}).json()["runId"])
        assert st["status"] == "done"  # the imported graph is directly runnable
    finally:
        deps.importer = prev


def _provision_private_collab_canvas(cid: str, owner: str, *users: str) -> None:
    from hub.metadb import Canvas, User, session
    with session() as s:
        canvas = s.get(Canvas, cid)
        if canvas is None:
            s.add(Canvas(id=cid, owner_id=owner, name="t", version=1, doc="{}", visibility="private"))
        else:
            canvas.owner_id, canvas.visibility = owner, "private"
        for uid in (owner, *users):
            if s.get(User, uid) is None:
                s.add(User(id=uid, name=uid))


class _ManualCollabClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _manual_collab_clock(monkeypatch) -> _ManualCollabClock:  # noqa: ANN001 — pytest fixture
    from hub import main as hub_main

    clock = _ManualCollabClock()
    monkeypatch.setattr(hub_main, "_collab_now", clock)
    return clock


def _expire_collab_authorization(clock: _ManualCollabClock) -> None:
    from hub import main as hub_main

    clock.advance(hub_main._COLLAB_ROLE_REVALIDATION_INTERVAL_SECONDS)


@pytest.fixture(scope="module")
def live_collab_url():
    """Real single-loop ASGI server for cross-socket tests.

    Starlette's nested TestClient websocket sessions each own a different portal/loop; yielding to a
    DB worker before sending through another session is therefore not a faithful relay test. Uvicorn
    exercises the production topology: every socket on this worker shares one event loop.
    """
    import socket
    import threading
    import uvicorn

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off", ws="websockets-sansio"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("test collaboration server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)


async def _collab_send(ws, msg: dict) -> None:  # noqa: ANN001 — websockets client protocol varies by version
    import json
    await ws.send(json.dumps(msg))


async def _collab_recv(ws) -> dict:  # noqa: ANN001 — websockets client protocol varies by version
    import asyncio
    import json
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=3))


async def _collab_seed(ws, *, update: str = "seed-state") -> str:  # noqa: ANN001
    """Complete the relay-elected seed handshake and return its server-generated request id."""
    plan = await _collab_recv(ws)
    assert plan["type"] == "server" and plan["event"] == "room-state" and plan["mode"] == "seed"
    request_id = plan["requestId"]
    await _collab_send(ws, {
        "type": "yjs", "seed": True, "requestId": request_id, "update": update,
    })
    await _collab_send(ws, {"type": "sync-ready", "requestId": request_id})
    assert await _collab_recv(ws) == {"type": "server", "event": "room-state", "mode": "ready"}
    return request_id


async def _collab_sync(
    authority, joiner, *, update: str = "authority-state", state_vector: str = "joiner-vector",
) -> str:  # noqa: ANN001
    """Complete one server-directed sync without relying on timing or client-selected authority."""
    plan = await _collab_recv(joiner)
    assert plan["type"] == "server" and plan["event"] == "room-state" and plan["mode"] == "sync"
    request_id = plan["requestId"]
    await _collab_send(joiner, {"type": "ysync", "requestId": request_id, "sv": state_vector})
    assert await _collab_recv(authority) == {
        "type": "ysync", "requestId": request_id, "sv": state_vector,
    }
    await _collab_send(authority, {
        "type": "yjs", "sync": True, "replyTo": request_id, "update": update,
    })
    assert await _collab_recv(joiner) == {
        "type": "yjs", "sync": True, "replyTo": request_id, "update": update,
    }
    await _collab_send(joiner, {"type": "sync-ready", "requestId": request_id})
    assert await _collab_recv(joiner) == {"type": "server", "event": "room-state", "mode": "ready"}
    return request_id


async def _expect_ws_policy_close(ws) -> None:  # noqa: ANN001 — protocol varies by websockets version
    import asyncio
    from websockets.exceptions import ConnectionClosed
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), timeout=3)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 1008


def test_collab_relay_gates_viewer_doc_updates(monkeypatch, live_collab_url):
    # A viewer may watch (presence + peers' edits) but its OWN doc updates must not be relayed.
    import asyncio
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    cid = "cvs_viewer_gate"
    _provision_private_collab_canvas(cid, "owner_u", "editor_u", "viewer_u")
    metadb.share_canvas(cid, "editor_u", "editor")
    metadb.share_canvas(cid, "viewer_u", "viewer")

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(ws_url, additional_headers={"Cookie": f"dp_session={auth.sign('editor_u')}"}, proxy=None) as ed:
            await _collab_seed(ed)
            async with websockets.connect(ws_url, additional_headers={"Cookie": f"dp_session={auth.sign('viewer_u')}"}, proxy=None) as vw:
                await _collab_sync(ed, vw)
                await _collab_send(vw, {"clientId": "V", "type": "yjs", "update": "AAAA"})
                await _collab_send(vw, {"clientId": "V", "type": "presence", "name": "Val"})
                got = await _collab_recv(ed)
                assert got["type"] == "presence" and got["clientId"] == "V"
                await _collab_send(ed, {"clientId": "E", "type": "yjs", "update": "BBBB"})
                got2 = await _collab_recv(vw)
                assert got2 == {"type": "yjs", "update": "BBBB"}

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_rejects_server_frame_forgery_without_reaching_a_waiting_editor(monkeypatch, live_collab_url):
    # A viewer cannot forge the old peerCount=0 hydration signal (or any other relay-only frame).
    # Each violation is visible only as a room-data-free error on the offender; the editor's original
    # directed request remains valid and is answered solely by the synchronized authority.
    import asyncio
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    cid = "cvs_server_frame_forgery"
    authority_uid, editor_uid, viewer_uid = "forge_owner", "forge_editor", "forge_viewer"
    _provision_private_collab_canvas(cid, authority_uid, editor_uid, viewer_uid)
    metadb.share_canvas(cid, editor_uid, "editor")
    metadb.share_canvas(cid, viewer_uid, "viewer")

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        authority_headers = {"Cookie": f"dp_session={auth.sign(authority_uid)}"}
        editor_headers = {"Cookie": f"dp_session={auth.sign(editor_uid)}"}
        viewer_headers = {"Cookie": f"dp_session={auth.sign(viewer_uid)}"}
        async with websockets.connect(ws_url, additional_headers=authority_headers, proxy=None) as authority:
            await _collab_seed(authority, update="newer-unpersisted-state")
            async with websockets.connect(ws_url, additional_headers=editor_headers, proxy=None) as editor:
                editor_plan = await _collab_recv(editor)
                assert editor_plan["mode"] == "sync"
                for forged in (
                    {"type": "room-state", "peerCount": 0},
                    {"type": "server", "event": "room-state", "mode": "seed"},
                    {"type": "leave", "clientId": "victim"},
                    {"type": "external-edit", "canvasId": cid},
                    {"type": "ownership", "owner": "attacker"},
                ):
                    async with websockets.connect(
                        ws_url, additional_headers=viewer_headers, proxy=None,
                    ) as attacker:
                        assert (await _collab_recv(attacker))["mode"] == "sync"
                        await _collab_send(attacker, forged)
                        assert await _collab_recv(attacker) == {
                            "type": "server", "event": "protocol-error", "code": "server-frame-forgery",
                        }
                        await _expect_ws_policy_close(attacker)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(editor.recv(), timeout=0.05)

                request_id = editor_plan["requestId"]
                await _collab_send(editor, {"type": "ysync", "requestId": request_id, "sv": "editor-vector"})
                assert await _collab_recv(authority) == {
                    "type": "ysync", "requestId": request_id, "sv": "editor-vector",
                }

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_viewer_only_room_waits_for_a_writer_seed(monkeypatch, live_collab_url):
    # A viewer cannot seed or answer. The first writer is elected even when the viewer arrived first,
    # and only after that writer is ready does the viewer receive a directed read sync.
    import asyncio
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    cid = "cvs_viewer_first_sync"
    _provision_private_collab_canvas(cid, "owner_u", "editor_u", "viewer_u")
    metadb.share_canvas(cid, "editor_u", "editor")
    metadb.share_canvas(cid, "viewer_u", "viewer")
    viewer_cookie = f"dp_session={auth.sign('viewer_u')}"
    editor_cookie = f"dp_session={auth.sign('editor_u')}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": viewer_cookie}, proxy=None,
        ) as viewer:
            assert await _collab_recv(viewer) == {
                "type": "server", "event": "room-state", "mode": "wait",
            }
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": editor_cookie}, proxy=None,
            ) as editor:
                await _collab_seed(editor, update="writer-state")
                await _collab_sync(editor, viewer, update="writer-state", state_vector="viewer-state")

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_revoked_viewer_cannot_inject_presence(monkeypatch, live_collab_url):
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, viewer_uid = "cvs_presence_revoke", "presence_owner", "presence_viewer"
    _provision_private_collab_canvas(cid, owner_uid, viewer_uid)
    metadb.share_canvas(cid, viewer_uid, "viewer")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    viewer_cookie = f"dp_session={auth.sign(viewer_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None,
        ) as owner:
            await _collab_seed(owner)
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": viewer_cookie}, proxy=None,
            ) as viewer:
                assert (await _collab_recv(viewer))["mode"] == "sync"
                async with httpx.AsyncClient(
                    base_url=live_collab_url, headers={"Cookie": owner_cookie},
                ) as http:
                    response = await http.delete(f"/api/canvas/{cid}/share/{viewer_uid}")
                assert response.status_code == 200
                _expire_collab_authorization(clock)

                await _collab_send(viewer, {
                    "type": "presence", "clientId": "revoked", "name": "must-not-relay",
                })
                await _expect_ws_policy_close(viewer)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(owner.recv(), timeout=0.05)

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_revoked_joiner_cannot_receive_sync_baseline(monkeypatch, live_collab_url):
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, joiner_uid = "cvs_sync_target_revoke", "target_owner", "target_joiner"
    _provision_private_collab_canvas(cid, owner_uid, joiner_uid)
    metadb.share_canvas(cid, joiner_uid, "editor")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    joiner_cookie = f"dp_session={auth.sign(joiner_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None,
        ) as owner:
            await _collab_seed(owner)
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": joiner_cookie}, proxy=None,
            ) as joiner:
                plan = await _collab_recv(joiner)
                await _collab_send(joiner, {
                    "type": "ysync", "requestId": plan["requestId"], "sv": "joiner-vector",
                })
                assert (await _collab_recv(owner))["requestId"] == plan["requestId"]

                async with httpx.AsyncClient(
                    base_url=live_collab_url, headers={"Cookie": owner_cookie},
                ) as http:
                    response = await http.delete(f"/api/canvas/{cid}/share/{joiner_uid}")
                assert response.status_code == 200
                _expire_collab_authorization(clock)
                await _collab_send(owner, {
                    "type": "yjs", "sync": True,
                    "replyTo": plan["requestId"], "update": "must-not-leak",
                })
                await _expect_ws_policy_close(joiner)

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_live_viewer_promotion_replans_seed_on_presence(monkeypatch, live_collab_url):
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, viewer_uid = "cvs_live_promote", "promote_owner", "promote_viewer"
    _provision_private_collab_canvas(cid, owner_uid, viewer_uid)
    metadb.share_canvas(cid, viewer_uid, "viewer")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    viewer_cookie = f"dp_session={auth.sign(viewer_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": viewer_cookie}, proxy=None,
        ) as viewer:
            assert await _collab_recv(viewer) == {
                "type": "server", "event": "room-state", "mode": "wait",
            }
            async with httpx.AsyncClient(
                base_url=live_collab_url, headers={"Cookie": owner_cookie},
            ) as http:
                response = await http.post(
                    f"/api/canvas/{cid}/share", json={"userId": viewer_uid, "role": "editor"},
                )
            assert response.status_code == 200
            _expire_collab_authorization(clock)

            await _collab_send(viewer, {
                "type": "presence", "clientId": "promoted", "name": "now-editor",
            })
            seed = await _collab_recv(viewer)
            assert seed["type"] == "server" and seed["mode"] == "seed"
            await _collab_send(viewer, {
                "type": "yjs", "seed": True,
                "requestId": seed["requestId"], "update": "promoted-state",
            })
            await _collab_send(viewer, {
                "type": "sync-ready", "requestId": seed["requestId"],
            })
            assert await _collab_recv(viewer) == {
                "type": "server", "event": "room-state", "mode": "ready",
            }

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_baseline_queue_fences_partial_state_during_authority_loss(live_collab_url):
    import asyncio
    import websockets

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + "/ws/collab/baseline-authority-loss"
        async with websockets.connect(ws_url, proxy=None) as authority:
            await _collab_seed(authority, update="persisted-base")
            async with websockets.connect(ws_url, proxy=None) as joiner:
                join_plan = await _collab_recv(joiner)
                assert join_plan["mode"] == "sync"
                async with websockets.connect(ws_url, proxy=None) as third:
                    assert (await _collab_recv(third))["mode"] == "sync"

                    # Before a baseline is queued, an ordinary authority delta is unsafe for both
                    # empty replicas and must be excluded from fan-out.
                    await _collab_send(authority, {
                        "type": "yjs", "update": "unsafe-pre-baseline-delta",
                    })
                    for peer in (joiner, third):
                        with pytest.raises(asyncio.TimeoutError):
                            await asyncio.wait_for(peer.recv(), timeout=0.05)

                    request_id = join_plan["requestId"]
                    await _collab_send(joiner, {
                        "type": "ysync", "requestId": request_id, "sv": "joiner-vector",
                    })
                    assert await _collab_recv(authority) == {
                        "type": "ysync", "requestId": request_id, "sv": "joiner-vector",
                    }
                    await _collab_send(authority, {
                        "type": "yjs", "sync": True,
                        "replyTo": request_id, "update": "persisted-base",
                    })
                    await _collab_send(authority, {
                        "type": "yjs", "update": "ordered-post-baseline-delta",
                    })

                    # The per-peer FIFO makes the baseline visible before the concurrent delta.
                    assert await _collab_recv(joiner) == {
                        "type": "yjs", "sync": True,
                        "replyTo": request_id, "update": "persisted-base",
                    }
                    assert await _collab_recv(joiner) == {
                        "type": "yjs", "update": "ordered-post-baseline-delta",
                    }
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(third.recv(), timeout=0.05)

                    await authority.close()
                    replacement = await _collab_recv(joiner)
                    assert replacement["type"] == "server" and replacement["mode"] == "seed"

    asyncio.run(scenario())


def test_collab_room_state_rechecks_downgraded_sync_responder(monkeypatch, live_collab_url):
    # A joiner must not remain blocked on a peer that was an editor when counted but became a viewer
    # before answering. At the bounded revalidation boundary, the rejected reply refreshes room-state
    # without relaying viewer document data.
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb

    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid = "cvs_sync_responder_downgrade"
    owner_uid, joiner_uid, responder_uid = "owner_sd", "joiner_sd", "responder_sd"
    _provision_private_collab_canvas(cid, owner_uid, joiner_uid, responder_uid)
    metadb.share_canvas(cid, joiner_uid, "editor")
    metadb.share_canvas(cid, responder_uid, "editor")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    joiner_cookie = f"dp_session={auth.sign(joiner_uid)}"
    responder_cookie = f"dp_session={auth.sign(responder_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(
            ws_url, additional_headers={"Cookie": responder_cookie}, proxy=None,
        ) as responder:
            await _collab_seed(responder, update="newer-state")
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": joiner_cookie}, proxy=None,
            ) as joiner:
                plan = await _collab_recv(joiner)
                assert plan["mode"] == "sync"
                await _collab_send(joiner, {"type": "ysync", "requestId": plan["requestId"], "sv": "state"})
                assert await _collab_recv(responder) == {
                    "type": "ysync", "requestId": plan["requestId"], "sv": "state",
                }

                async with httpx.AsyncClient(
                    base_url=live_collab_url, headers={"Cookie": owner_cookie},
                ) as http:
                    response = await http.post(
                        f"/api/canvas/{cid}/share", json={"userId": responder_uid, "role": "viewer"},
                    )
                assert response.status_code == 200
                _expire_collab_authorization(clock)

                await _collab_send(responder, {
                    "type": "yjs", "update": "BLOCKED", "sync": True,
                    "replyTo": plan["requestId"],
                })
                await _collab_send(responder, {"clientId": "R", "type": "presence", "name": "viewer"})
                replacement = await _collab_recv(joiner)
                assert replacement["type"] == "server" and replacement["mode"] == "seed"
                assert replacement["requestId"] != plan["requestId"]
                assert await _collab_recv(joiner) == {
                    "clientId": "R", "type": "presence", "name": "viewer",
                }

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_rechecks_editor_downgrade_on_the_same_socket(monkeypatch, live_collab_url):
    # After the fixed authorization interval, a connected editor's downgrade takes effect without
    # reconnecting: its next Yjs update is dropped while viewer-safe traffic keeps working.
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, editor_uid = "cvs_live_downgrade", "live_owner_d", "live_editor_d"
    _provision_private_collab_canvas(cid, owner_uid, editor_uid)
    metadb.share_canvas(cid, editor_uid, "editor")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    editor_cookie = f"dp_session={auth.sign(editor_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None) as owner:
            await _collab_seed(owner)
            async with websockets.connect(ws_url, additional_headers={"Cookie": editor_cookie}, proxy=None) as editor:
                await _collab_sync(owner, editor)
                async with httpx.AsyncClient(base_url=live_collab_url, headers={"Cookie": owner_cookie}) as http:
                    response = await http.post(f"/api/canvas/{cid}/share", json={"userId": editor_uid, "role": "viewer"})
                assert response.status_code == 200
                _expire_collab_authorization(clock)

                await _collab_send(editor, {"clientId": "E", "type": "yjs", "update": "BLOCKED_AFTER_DOWNGRADE"})
                await _collab_send(editor, {"clientId": "E", "type": "presence", "name": "still watching"})
                got = await _collab_recv(owner)  # same-socket ordering proves the preceding Yjs was dropped
                assert got["type"] == "presence" and got["name"] == "still watching"

                await _collab_send(owner, {"clientId": "O", "type": "yjs", "update": "READABLE_AS_VIEWER"})
                got2 = await _collab_recv(editor)
                assert got2["type"] == "yjs" and got2["update"] == "READABLE_AS_VIEWER"

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_rechecks_removed_sender_and_recipient_sockets(monkeypatch, live_collab_url):
    # Both sockets were admitted while the user was an editor. At the next bounded revalidation after
    # unshare, one cannot relay a Yjs write and the other closes before receiving a document update.
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, editor_uid = "cvs_live_remove", "live_owner_r", "live_editor_r"
    _provision_private_collab_canvas(cid, owner_uid, editor_uid)
    metadb.share_canvas(cid, editor_uid, "editor")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    editor_cookie = f"dp_session={auth.sign(editor_uid)}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None) as owner_a:
            await _collab_seed(owner_a)
            async with websockets.connect(ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None) as owner_b:
                await _collab_sync(owner_a, owner_b)
                async with websockets.connect(ws_url, additional_headers={"Cookie": editor_cookie}, proxy=None) as removed_writer:
                    await _collab_sync(owner_a, removed_writer)
                    async with websockets.connect(ws_url, additional_headers={"Cookie": editor_cookie}, proxy=None) as removed_reader:
                        await _collab_sync(owner_a, removed_reader)
                        async with httpx.AsyncClient(base_url=live_collab_url, headers={"Cookie": owner_cookie}) as http:
                            response = await http.delete(f"/api/canvas/{cid}/share/{editor_uid}")
                        assert response.status_code == 200
                        _expire_collab_authorization(clock)

                        await _collab_send(removed_writer, {"clientId": "RW", "type": "yjs", "update": "MUST_NOT_RELAY"})
                        await _expect_ws_policy_close(removed_writer)

                        await _collab_send(owner_b, {"clientId": "OB", "type": "yjs", "update": "AFTER_REMOVAL"})
                        assert await _collab_recv(owner_a) == {"type": "yjs", "update": "AFTER_REMOVAL"}
                        await _expect_ws_policy_close(removed_reader)

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


def test_collab_logout_revokes_sender_and_recipient(monkeypatch, live_collab_url):
    # Real logout bumps the session epoch. At the next bounded revalidation, every already-open socket
    # is fenced before either active document direction proceeds; all of the user's sessions are revoked.
    import asyncio
    import httpx
    import websockets
    from hub import auth, metadb
    monkeypatch.setenv("DP_AUTH_SECRET", "ws-revocation-test-secret-not-weak-0123456789")
    clock = _manual_collab_clock(monkeypatch)
    cid, owner_uid, revoked_uid = "cvs_token_revoke", "live_owner_t", "live_editor_t"
    _provision_private_collab_canvas(cid, owner_uid, revoked_uid)
    metadb.share_canvas(cid, revoked_uid, "editor")
    owner_cookie = f"dp_session={auth.sign(owner_uid)}"
    revoked_token = auth.sign(revoked_uid)
    revoked_cookie = f"dp_session={revoked_token}"

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/collab/{cid}"
        async with websockets.connect(ws_url, additional_headers={"Cookie": owner_cookie}, proxy=None) as owner:
            await _collab_seed(owner)
            async with websockets.connect(ws_url, additional_headers={"Cookie": revoked_cookie}, proxy=None) as writer:
                await _collab_sync(owner, writer)
                async with websockets.connect(ws_url, additional_headers={"Cookie": revoked_cookie}, proxy=None) as reader:
                    await _collab_sync(owner, reader)
                    async with httpx.AsyncClient(
                        base_url=live_collab_url, headers={"Cookie": revoked_cookie},
                    ) as http:
                        response = await http.post("/api/auth/logout")
                    assert response.status_code == 200
                    assert auth.verify(revoked_token) is None
                    _expire_collab_authorization(clock)

                    await _collab_send(writer, {
                        "clientId": "revoked-writer", "type": "yjs", "update": "MUST_NOT_RELAY",
                    })
                    await _expect_ws_policy_close(writer)

                    await _collab_send(owner, {
                        "clientId": "owner", "type": "yjs", "update": "MUST_NOT_RECEIVE",
                    })
                    await _expect_ws_policy_close(reader)

    try:
        asyncio.run(scenario())
    finally:
        metadb.delete_canvas_cascade(cid)


@pytest.mark.parametrize("revocation", ["token", "share"])
def test_run_ws_rechecks_session_and_read_access(monkeypatch, live_collab_url, revocation):
    # A first status frame is the synchronization barrier: after revocation commits, the next stream
    # boundary must be a 1008 close, never another row-count/error/output-bearing status payload.
    import asyncio
    import websockets
    from hub import auth, metadb
    from hub.models import RunStatus
    monkeypatch.setenv("DP_AUTH_SECRET", "ws-revocation-test-secret-not-weak-0123456789")
    run_id, cid = f"run_ws_revoke_{revocation}", f"cvs_run_ws_revoke_{revocation}"
    owner_uid, reader_uid = f"run_owner_{revocation}", f"run_reader_{revocation}"
    _provision_private_collab_canvas(cid, owner_uid, reader_uid)
    metadb.share_canvas(cid, reader_uid, "viewer")
    metadb.save_run_state(
        run_id, RunStatus(run_id=run_id, status="running", rows_processed=7).model_dump(),
        canvas_id=cid,
    )
    metadb.bind_run_owner(run_id, owner_uid, cid)
    get_deps().run_index.pop(run_id, None)
    get_deps().run_owner.pop(run_id, None)
    reader_token = auth.sign(reader_uid)

    async def scenario() -> None:
        ws_url = live_collab_url.replace("http://", "ws://") + f"/ws/run/{run_id}"
        async with websockets.connect(
                ws_url, additional_headers={"Cookie": f"dp_session={reader_token}"}, proxy=None) as ws:
            first = await _collab_recv(ws)
            assert first["runId"] == run_id and first["status"] == "running"
            if revocation == "token":
                await asyncio.to_thread(metadb.bump_token_epoch, reader_uid)
                assert auth.verify(reader_token) is None
            else:
                await asyncio.to_thread(metadb.unshare_canvas, cid, reader_uid)
                assert metadb.canvas_role(cid, reader_uid) is None
            await _expect_ws_policy_close(ws)

    try:
        asyncio.run(scenario())
    finally:
        get_deps().run_index.pop(run_id, None)
        get_deps().run_owner.pop(run_id, None)
        metadb.delete_canvas_cascade(cid)


def test_execution_backend_plugin_contract(tmp_path):
    # a plugin can register an alternate execution backend (pod/Ray/queue/…); the kernel routes runs
    # to the first backend whose can_run(plan) is true. This proves the ExecutionBackend extension point.
    from hub.backends import ExecutionBackend
    from hub.deps import Deps

    class FakeBackend:
        name = "fake-pod"
        def can_run(self, plan): return True
        def estimate(self, plan, rows): return None
        def run(self, plan, graph, target_node_id, placement): return None
        def status(self, run_id): return None
        def cancel(self, run_id): return None

    from hub import metadb
    fake = FakeBackend()
    assert isinstance(fake, ExecutionBackend)  # structural conformance to the contract
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    d.runners.insert(0, fake)
    metadb.set_setting("backend", "fake-pod", scope="global")  # select the plugin backend by name
    try:
        assert d.pick_runner(object()) is fake  # the chosen backend is routed to (the extension point)
    finally:
        metadb.set_setting("backend", "", scope="global")


def test_spi_contracts_are_the_real_ones():
    # the old plugins/base.py "contract" was dead code with wrong signatures; it's deleted. The live
    # contracts must match the real code: adapters expose the methods the engine actually calls, and
    # the runners structurally satisfy ExecutionBackend.
    import importlib
    from hub.backends import ExecutionBackend
    from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
    from hub.plugins.runner import LocalRunner
    from hub.subprocess_runner import SubprocessRunner
    assert importlib.util.find_spec("hub.plugins.base") is None  # dead SPI file is gone
    for adapter in (DuckDBAdapter(), LanceAdapter()):
        for m in ("matches", "scan", "schema", "count", "fingerprint", "write"):
            assert callable(getattr(adapter, m, None)), f"{type(adapter).__name__} missing {m}"
    deps = get_deps()
    assert isinstance(deps.runner, ExecutionBackend)
    assert isinstance([r for r in deps.runners if isinstance(r, SubprocessRunner)][0], ExecutionBackend)
    assert isinstance(deps.runner, LocalRunner)


def test_plugin_version_negotiation(tmp_path):
    # a drop-in pack declaring a newer core than we provide is SKIPPED with a clear error (not
    # registered then crashed); one declaring our version (or none) loads normally.
    from hub.deps import Deps, CORE_API_VERSION

    def make_pack(name, min_core):
        d = tmp_path / "ws" / "plugins" / name
        d.mkdir(parents=True)
        (d / "dataplay.toml").write_text(
            f'name = "{name}"\nversion = "0.1.0"\n' + (f"min_core_api = {min_core}\n" if min_core is not None else ""))
        (d / "__init__.py").write_text(
            "from hub.sdk import NodeSpec, PortSpec\n"
            "def register(reg):\n"
            f"    reg.add_node(NodeSpec(kind='{name}_node', title='{name}', category='compute',\n"
            "        inputs=[PortSpec(id='in', wire='dataset')], outputs=[PortSpec(id='out', wire='dataset')], params=[]))\n")

    make_pack("goodpack", CORE_API_VERSION)
    make_pack("toonew", CORE_API_VERSION + 1)
    make_pack("unversioned", None)
    make_pack("stringver", '"1.0"')  # the DOCUMENTED form — F11: int("1.0") used to crash the parse
    make_pack("garbage", '"abc"')    # a non-version value → clear error, not a raw traceback
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    assert "goodpack_node" in d.node_specs and "unversioned_node" in d.node_specs  # compatible / no manifest → load
    assert "stringver_node" in d.node_specs                                        # "1.0" parses to major 1 → loads
    assert "toonew_node" not in d.node_specs                                       # incompatible → skipped
    err = [p for p in d.plugins if p.get("name") == "toonew" and p.get("error")]
    assert err and "core API" in err[0]["error"]
    gerr = [p for p in d.plugins if p.get("name") == "garbage" and p.get("error")]
    assert gerr and "version number" in gerr[0]["error"]


def test_catalog_plugin_is_finalized_before_catalog_services(tmp_path, monkeypatch):
    """A catalog selected by a drop-in plugin is the one every later service observes."""
    import importlib
    import sys
    import uuid

    import duckdb

    from hub import graph as graph_mod
    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.models import Graph
    from hub.plugins import catalog as catalog_plugin
    from hub.routers.runs import _profile_plan_digest
    from hub.subprocess_runner import SubprocessRunner

    plugin_name = "catalog_replacement_for_composition_test"
    plugin = tmp_path / "ws_one" / "plugins" / plugin_name
    plugins_dir = str(plugin.parent)
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "from hub.plugins.catalog import InMemoryCatalog\n"
        "instances = []\n"
        "class ReplacementCatalog(InMemoryCatalog):\n"
        "    def __init__(self, *args):\n"
        "        super().__init__(*args)\n"
        "def register(reg):\n"
        "    catalog = ReplacementCatalog(reg.deps.data_dir, reg.deps.resolve_adapter)\n"
        "    instances.append(catalog)\n"
        "    reg.set_catalog(catalog)\n"
    )
    source = tmp_path / "source.parquet"
    source_name = f"catalog_composition_source_{uuid.uuid4().hex}"
    output_name = f"catalog_composition_output_{uuid.uuid4().hex}"
    duckdb.connect().execute(f"COPY (SELECT 1 AS value) TO '{source}' (FORMAT PARQUET)")

    try:
        first = Deps(str(tmp_path / "ws_one"), str(tmp_path / "data_one"))
        module = importlib.import_module(plugin_name)
        selected = module.instances[-1]
        assert first.catalog is selected
        assert first.runner.catalog is selected
        assert next(r for r in first.runners if isinstance(r, SubprocessRunner)).catalog is selected
        assert first.controller.deps.catalog is selected

        # Read and profile preflight resolve an alias through the selected provider.
        selected.register_output(name=source_name, uri=str(source), parents=[])
        graph = Graph(**{"id": "catalog-composition", "version": 1, "nodes": [
            N("source", "source", {"uri": source_name}),
            N("write", "write", {"name": output_name}),
        ], "edges": [E("source", "write")]})
        graph_mod.resolve_source_refs(graph, first.catalog.resolve_ref)
        assert graph.nodes[0].data["config"]["uri"] == str(source)
        assert _profile_plan_digest(graph, "source", "out", first)

        # A real write reaches the same object through the runner's publication path.
        seen_publication_catalogs = []
        real_unmanaged_publication_supported = catalog_plugin.unmanaged_publication_supported
        monkeypatch.setattr(
            catalog_plugin, "unmanaged_publication_supported",
            lambda catalog: (
                seen_publication_catalogs.append(catalog),
                real_unmanaged_publication_supported(catalog),
            )[1],
        )
        started = first.runner.run(
            compile_plan(graph, "write", first.registry, first.node_specs), graph, "write", "local")
        status = started
        for _ in range(100):
            status = first.runner.status(started.run_id)
            if status.status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert status.status == "done", status.error
        assert selected in seen_publication_catalogs

        # A second composition root invokes the plugin again and does not share its catalog instance.
        second_workspace = tmp_path / "ws_two"
        second_plugin = second_workspace / "plugins" / plugin_name
        second_plugin.parent.mkdir(parents=True)
        second_plugin.symlink_to(plugin, target_is_directory=True)
        second = Deps(str(second_workspace), str(tmp_path / "data_two"))
        assert second.catalog is module.instances[-1]
        assert second.catalog is not selected
        assert second.runner.catalog is second.catalog
    finally:
        sys.modules.pop(plugin_name, None)
        if plugins_dir in sys.path:
            sys.path.remove(plugins_dir)


def test_required_default_catalog_failure_aborts_composition(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.plugins import default_catalog

    monkeypatch.setattr(default_catalog, "register", lambda _reg: (_ for _ in ()).throw(RuntimeError("catalog boom")))
    with pytest.raises(RuntimeError, match="catalog boom"):
        Deps(str(tmp_path / "ws"), str(tmp_path / "data"))


def test_runner_plugin_constructs_after_catalog_and_local_runner(tmp_path):
    """The bundled dp_ray plugin can bind both the selected catalog and its local delegate."""
    import sys
    from pathlib import Path

    from hub.deps import Deps

    plugin_name = "dp_ray"
    plugin = tmp_path / "ws" / "plugins" / plugin_name
    plugins_dir = str(plugin.parent)
    plugin.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[3] / "examples" / "plugins" / plugin_name
    plugin.symlink_to(source, target_is_directory=True)

    try:
        deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"), maintain_storage=False)
        runner = next(r for r in deps.runners if getattr(r, "name", None) == "ray-data")
        assert runner.base is deps.runner
        assert runner.catalog is deps.catalog
        assert not next(p for p in deps.plugins if p["name"] == plugin_name).get("error")
    finally:
        sys.modules.pop(plugin_name, None)
        if plugins_dir in sys.path:
            sys.path.remove(plugins_dir)


def test_core_api_range_check(monkeypatch):
    # OSS-01: the plugin-API check is a semantic RANGE (min ≤ need ≤ core), not just a floor — a plugin
    # built for a now-dropped OLDER major is rejected up front, not registered then crashed. Also proves
    # the shared helper handles the too-new and non-version cases (used by all three load paths).
    from hub import deps as deps_mod
    assert deps_mod._core_api_error(None) is None                       # undeclared → loads (unchanged)
    assert deps_mod._core_api_error(deps_mod.CORE_API_VERSION) is None  # exactly this core → ok
    assert "requires core API" in deps_mod._core_api_error(deps_mod.CORE_API_VERSION + 1)  # too new
    assert "version number" in deps_mod._core_api_error("abc")          # non-version → clear error
    # simulate a future core that dropped major 1: a plugin targeting the now-unsupported major is rejected
    monkeypatch.setattr(deps_mod, "MIN_SUPPORTED_API", 2)
    monkeypatch.setattr(deps_mod, "CORE_API_VERSION", 3)
    assert "breaking SPI change" in deps_mod._core_api_error(1)         # below the supported floor
    assert deps_mod._core_api_error(2) is None and deps_mod._core_api_error(3) is None  # in range


def test_nodespec_frontend_backend_parity():
    # backend nodespecs (/api/nodes) and the frontend hand-built cards (web/src/nodes/kinds/*.tsx)
    # define every built-in kind twice; this guards against the two silently drifting on ports/accepts
    # (they had, on `sql`). Parse each frontend register({...}) literal and compare to BUILTIN_NODE_SPECS.
    import re
    from pathlib import Path
    from hub.nodespecs import BUILTIN_NODE_SPECS
    kinds_dir = Path(__file__).resolve().parents[3] / "web" / "src" / "nodes" / "kinds"
    assert kinds_dir.is_dir(), kinds_dir

    def balanced(s: str, start: int, o: str, c: str):
        # content between the first `o` at/after start and its matching `c`, plus the closing index
        i = s.find(o, start)
        if i < 0:
            return None, -1
        depth = 0
        for k in range(i, len(s)):
            if s[k] == o:
                depth += 1
            elif s[k] == c:
                depth -= 1
                if depth == 0:
                    return s[i + 1:k], k
        return None, -1

    def array_ports(body: str, key: str) -> dict[str, tuple[str, tuple]]:
        # parse `key: [ {port}, {port} ]` → {port_id: (wire, sorted(accepts))}, balancing brackets/braces
        m = re.search(rf"\b{key}:\s*\[", body)
        if not m:
            return {}
        arr, _ = balanced(body, m.start(), "[", "]")
        if arr is None:
            return {}
        out, pos = {}, 0
        while True:
            obj, end = balanced(arr, pos, "{", "}")
            if obj is None:
                break
            pos = end + 1
            pid = re.search(r"\bid:\s*'([^']+)'", obj)
            wire = re.search(r"\bwire:\s*'([^']+)'", obj)
            if not (pid and wire):
                continue
            acc = re.search(r"\baccepts:\s*\[([^\]]*)\]", obj)  # accepts has no nested [] → plain regex ok
            accepts = tuple(sorted(re.findall(r"'([^']+)'", acc.group(1)))) if acc else ()
            out[pid.group(1)] = (wire.group(1), accepts)
        return out

    # index frontend files by the kind their register({...}) literal declares
    fe: dict[str, dict] = {}
    for f in kinds_dir.glob("*.tsx"):
        src = f.read_text()
        ri = src.find("register(")
        if ri < 0:
            continue
        body, _ = balanced(src, ri, "{", "}")  # the first arg object literal
        if body is None:
            continue
        kind = re.search(r"\bkind:\s*'([^']+)'", body)
        if not kind:
            continue
        fe[kind.group(1)] = {"file": f.name,
                             "inputs": array_ports(body, "inputs"),
                             "outputs": array_ports(body, "outputs")}

    mismatches = []
    checked = 0
    for spec in BUILTIN_NODE_SPECS:
        card = fe.get(spec.kind)
        if card is None:  # some backend kinds render via the generic card, not a hand-built one — fine
            continue
        checked += 1
        be_in = {p.id: (p.wire, tuple(sorted(p.accepts or []))) for p in spec.inputs}
        be_out = {p.id: (p.wire, tuple(sorted(p.accepts or []))) for p in spec.outputs}
        if be_in != card["inputs"]:
            mismatches.append(f"{spec.kind} ({card['file']}) inputs: backend {be_in} != frontend {card['inputs']}")
        if be_out != card["outputs"]:
            mismatches.append(f"{spec.kind} ({card['file']}) outputs: backend {be_out} != frontend {card['outputs']}")
    assert checked >= 8, f"parser matched too few kinds ({checked}) — frontend format may have changed"
    assert not mismatches, "backend/frontend node-spec drift:\n" + "\n".join(mismatches)


def test_password_change_revokes_outstanding_sessions(monkeypatch):
    # a signed token embeds the user's session epoch; a password change bumps the epoch, so every token
    # issued before it stops verifying immediately (not after the 7-day TTL). A deleted/unknown user's
    # token never verifies either.
    from hub import auth, metadb
    from hub.metadb import User, session
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    uid = "revoke_u"
    with session() as s:
        if s.get(User, uid) is None:
            s.add(User(id=uid, name="Rev", password_hash=auth.hash_password("pw1")))
    tok = auth.sign(uid)
    assert auth.verify(tok) == uid                       # valid now
    assert metadb.set_user_password(uid, auth.hash_password("pw2"))  # rotate → epoch bumps
    assert auth.verify(tok) is None                      # the old token is revoked
    assert auth.verify(auth.sign(uid)) == uid            # a freshly-signed token works
    assert auth.verify("ghost_u.0.9999999999.deadbeef") is None      # unknown user → revoked
    assert auth.verify(f"{uid}.0.9999999999.deadbeef") is None       # forged mac → rejected


def test_change_password_keeps_the_acting_session(monkeypatch):
    # bumping the epoch revokes OTHER sessions, but the caller changing their OWN password must not be
    # logged out — /auth/password re-issues the acting cookie at the new epoch.
    from hub import auth
    from hub.metadb import User, session
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    client.cookies.clear()
    uid = "chpw_u"
    with session() as s:
        if s.get(User, uid) is None:
            s.add(User(id=uid, name="Chpw", password_hash=auth.hash_password("oldpw1")))
    try:
        assert client.post("/api/auth/login", json={"userId": uid, "password": "oldpw1"}).status_code == 200
        assert client.get("/api/canvas").status_code == 200                       # logged in
        r = client.post("/api/auth/password", json={"oldPassword": "oldpw1", "newPassword": "newpw2"})
        assert r.status_code == 200
        assert client.get("/api/canvas").status_code == 200                       # NOT logged out (cookie re-issued)
    finally:
        client.cookies.clear()


def test_password_compare_and_set_handles_unset_and_stale_credentials():
    from hub import auth, metadb
    from hub.metadb import User, session

    uid = "password_cas_unset"
    with session() as s:
        s.add(User(id=uid, name="Unset CAS", password_hash=None, token_epoch=7))

    first_hash = auth.hash_password("first-password")
    assert metadb.compare_and_set_user_password(uid, None, 7, first_hash) == 8
    assert metadb.compare_and_set_user_password(uid, None, 8, auth.hash_password("stale-password")) is None
    with session() as s:
        user = s.get(User, uid)
        assert user.password_hash == first_hash
        assert user.token_epoch == 8


def test_verify_password_rejects_noncanonical_or_wrong_length_encodings():
    import base64

    from hub import auth

    password = "strict-password"
    valid = auth.hash_password(password)
    prefix, salt_b64, hash_b64 = valid.split("$")
    salt = base64.b64decode(salt_b64)
    derived = base64.b64decode(hash_b64)
    malformed = [
        f"{prefix}${salt_b64}!${hash_b64}",
        f"{prefix}${salt_b64}${hash_b64}!",
        f"{prefix}${salt_b64}=${hash_b64}",
        f"{prefix}${base64.b64encode(salt[:-1]).decode()}${hash_b64}",
        f"{prefix}${base64.b64encode(salt + b'x').decode()}${hash_b64}",
        f"{prefix}${salt_b64}${base64.b64encode(derived[:-1]).decode()}",
        f"{prefix}${salt_b64}${base64.b64encode(derived + b'x').decode()}",
        f"{valid}$trailing-field",
    ]
    assert auth.verify_password(password, valid)
    assert all(not auth.verify_password(password, candidate) for candidate in malformed)


def test_concurrent_session_epoch_bumps_are_not_lost():
    import concurrent.futures
    import threading

    from hub import metadb
    from hub.metadb import User, session

    uid = "password_epoch_bump_race"
    with session() as s:
        s.add(User(id=uid, name="Epoch Bump", token_epoch=20))
    start = threading.Barrier(2)

    def bump(_index):
        start.wait(timeout=10)
        metadb.bump_token_epoch(uid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(bump, range(2)))
    assert metadb.user_token_epoch(uid) == 22


def test_concurrent_password_changes_exactly_one_wins(monkeypatch):
    import concurrent.futures
    import threading

    from hub import auth
    from hub.metadb import User, session

    monkeypatch.setenv("DP_AUTH_SECRET", "password-cas-secret")
    uid = "password_cas_race"
    old_hash = auth.hash_password("old-password")
    with session() as s:
        s.add(User(id=uid, name="Password CAS", password_hash=old_hash, token_epoch=11))
    session_token = auth.sign(uid)

    # Both requests must finish verifying the exact same old hash before either reaches the CAS.
    # The database conditional update, not request timing or a Python lock, decides the winner.
    verified = threading.Barrier(2)
    real_verify_password = auth.verify_password

    def verify_together(password, stored):
        valid = real_verify_password(password, stored)
        if stored == old_hash:
            verified.wait(timeout=10)
        return valid

    monkeypatch.setattr(auth, "verify_password", verify_together)
    passwords = ("winner-candidate-a", "winner-candidate-b")
    clients = (TestClient(app), TestClient(app))

    def rotate(index):
        response = clients[index].post(
            "/api/auth/password",
            json={"oldPassword": "old-password", "newPassword": passwords[index]},
            headers={"Cookie": f"dp_session={session_token}"},
        )
        return passwords[index], response

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(rotate, range(2)))
    finally:
        for request_client in clients:
            request_client.close()

    winners = [(password, response) for password, response in results.items() if response.status_code == 200]
    losers = [(password, response) for password, response in results.items() if response.status_code == 409]
    assert len(winners) == len(losers) == 1
    winner_password, winner_response = winners[0]
    loser_password, loser_response = losers[0]
    assert loser_response.json()["detail"] == "password was changed concurrently; sign in again"
    assert "set-cookie" not in loser_response.headers
    assert winner_response.cookies.get("dp_session")

    with session() as s:
        user = s.get(User, uid)
        assert user.token_epoch == 12
        assert auth.verify_password(winner_password, user.password_hash)
        assert not auth.verify_password(loser_password, user.password_hash)
    assert auth.verify(winner_response.cookies.get("dp_session")) == uid


def test_revoked_inflight_password_change_cannot_resurrect_session(monkeypatch):
    import concurrent.futures
    import threading

    from fastapi import Depends

    from hub import auth, metadb
    from hub.metadb import User, session
    from hub.security import RequestIdentity, current_identity, current_user

    monkeypatch.setenv("DP_AUTH_SECRET", "password-admission-secret")
    uid = "password_admission_epoch"
    old_hash = auth.hash_password("old-password")
    with session() as s:
        s.add(User(id=uid, name="Admission Epoch", password_hash=old_hash, token_epoch=30))
    session_token = auth.sign(uid)
    claims = auth.verify_claims(session_token)
    assert claims is not None and claims.user_id == uid and claims.epoch == 30

    request_admitted = threading.Event()
    resume_request = threading.Event()

    def pause_after_admission(identity: RequestIdentity = Depends(current_identity)):
        request_admitted.set()
        assert resume_request.wait(timeout=10)
        return identity.user_id

    # Override the global gate itself. Its current_identity subdependency has already verified and
    # cached the signed epoch when this pauses; the password route must consume that exact identity.
    app.dependency_overrides[current_user] = pause_after_admission
    request_client = TestClient(app)

    def rotate():
        return request_client.post(
            "/api/auth/password",
            json={"oldPassword": "old-password", "newPassword": "resurrected-password"},
            headers={"Cookie": f"dp_session={session_token}"},
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(rotate)
            try:
                assert request_admitted.wait(timeout=10)
                metadb.bump_token_epoch(uid)
            finally:
                resume_request.set()
            response = pending.result(timeout=10)
    finally:
        app.dependency_overrides.pop(current_user, None)
        request_client.close()

    assert response.status_code == 409
    assert "set-cookie" not in response.headers
    with session() as s:
        user = s.get(User, uid)
        assert user.token_epoch == 31
        assert user.password_hash == old_hash


def test_old_password_login_fails_if_credential_rotates_after_verification(monkeypatch):
    import concurrent.futures
    import threading

    from hub import auth, metadb
    from hub.metadb import User, session

    monkeypatch.setenv("DP_AUTH_SECRET", "login-snapshot-secret")
    uid = "login_snapshot_race"
    old_hash = auth.hash_password("old-password")
    new_hash = auth.hash_password("new-password")
    with session() as s:
        s.add(User(id=uid, name="Login Snapshot", password_hash=old_hash, token_epoch=40))

    password_verified = threading.Event()
    resume_login = threading.Event()
    real_verify_password = auth.verify_password

    def pause_after_verification(password, stored):
        valid = real_verify_password(password, stored)
        if password == "old-password" and stored == old_hash:
            password_verified.set()
            assert resume_login.wait(timeout=10)
        return valid

    monkeypatch.setattr(auth, "verify_password", pause_after_verification)
    request_client = TestClient(app)

    def login():
        return request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "old-password"},
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(login)
            try:
                assert password_verified.wait(timeout=10)
                assert metadb.compare_and_set_user_password(uid, old_hash, 40, new_hash) == 41
            finally:
                resume_login.set()
            response = pending.result(timeout=10)
    finally:
        request_client.close()

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid user or password"
    assert "set-cookie" not in response.headers
    with session() as s:
        user = s.get(User, uid)
        assert user.token_epoch == 41
        assert user.password_hash == new_hash


def test_login_cookie_cannot_catch_up_to_rotation_after_snapshot_confirmation(monkeypatch):
    from hub import auth, metadb
    from hub.metadb import User, session

    monkeypatch.setenv("DP_AUTH_SECRET", "login-signing-epoch-secret")
    uid = "login_signing_epoch"
    old_hash = auth.hash_password("old-password")
    new_hash = auth.hash_password("new-password")
    with session() as s:
        s.add(User(id=uid, name="Login Signing Epoch", password_hash=old_hash, token_epoch=45))

    signed_epochs = []
    real_sign_at_epoch = auth.sign_at_epoch

    def rotate_before_sign(user_id, epoch, now=None):
        signed_epochs.append(epoch)
        assert metadb.compare_and_set_user_password(user_id, old_hash, 45, new_hash) == 46
        return real_sign_at_epoch(user_id, epoch, now)

    monkeypatch.setattr(auth, "sign_at_epoch", rotate_before_sign)
    request_client = TestClient(app)
    try:
        response = request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "old-password"},
        )
    finally:
        request_client.close()

    issued = response.cookies.get("dp_session")
    assert response.status_code == 200
    assert signed_epochs == [45]
    assert issued and issued.split(".")[1] == "45"
    assert metadb.user_token_epoch(uid) == 46
    assert auth.verify(issued) is None


def test_password_change_cookie_cannot_catch_up_to_a_later_revocation(monkeypatch):
    from hub import auth, metadb
    from hub.metadb import User, session

    monkeypatch.setenv("DP_AUTH_SECRET", "password-epoch-secret")
    uid = "password_cas_epoch"
    with session() as s:
        s.add(User(id=uid, name="Password Epoch", password_hash=auth.hash_password("old-password"),
                   token_epoch=4))
    session_token = auth.sign(uid)

    signed_epochs = []
    real_sign_at_epoch = auth.sign_at_epoch

    def revoke_before_sign(user_id, epoch, now=None):
        signed_epochs.append(epoch)
        metadb.bump_token_epoch(user_id)
        return real_sign_at_epoch(user_id, epoch, now)

    monkeypatch.setattr(auth, "sign_at_epoch", revoke_before_sign)
    request_client = TestClient(app)
    try:
        response = request_client.post(
            "/api/auth/password",
            json={"oldPassword": "old-password", "newPassword": "new-password"},
            headers={"Cookie": f"dp_session={session_token}"},
        )
    finally:
        request_client.close()

    assert response.status_code == 200
    issued = response.cookies.get("dp_session")
    assert signed_epochs == [5]
    assert issued and issued.split(".")[1] == "5"
    assert metadb.user_token_epoch(uid) == 6
    assert auth.verify(issued) is None


def test_signed_session_auth(monkeypatch):
    # with auth enabled, identity must come from a valid signed session cookie — a raw header is not
    # trusted, protected endpoints 401 without a session, and login requires the user's own password.
    from hub import auth
    from hub.metadb import User, session
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    client.cookies.clear()
    uid = "sess_u"
    with session() as s:  # provision directly (create_user is gated when auth is on)
        if s.get(User, uid) is None:
            s.add(User(id=uid, name="Sess", password_hash=auth.hash_password("sesspw1")))
    try:
        assert client.get("/api/auth/status").json() == {"authEnabled": True, "userId": None}
        assert client.get("/api/canvas").status_code == 401                                 # no session
        assert client.get("/api/canvas", headers={"X-DP-User": uid}).status_code == 401     # header not trusted
        assert client.post("/api/auth/login", json={"userId": uid, "password": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"userId": uid, "password": "sesspw1"}).status_code == 200
        assert client.get("/api/canvas").status_code == 200                                 # signed cookie carried
        assert client.get("/api/auth/status").json()["userId"] == uid
    finally:
        client.cookies.clear()


def test_canvas_sharing_access_and_authz():
    # sharing: a canvas is private to its owner until explicitly shared; shared editors can read/write
    # but not delete (owner-only); workspace visibility opens it to everyone.
    bob = client.post("/api/users", json={"name": "Bob"}).json()["id"]
    cid = "share_cv"
    client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "shared", "version": 1, "nodes": [], "edges": []})
    hb = {"X-DP-User": bob}
    assert client.get(f"/api/canvas/{cid}", headers=hb).status_code == 404  # not shared yet
    client.post(f"/api/canvas/{cid}/share", json={"userId": bob, "role": "editor"})  # owner shares
    assert client.get(f"/api/canvas/{cid}", headers=hb).status_code == 200
    assert client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "x", "version": 2, "nodes": [], "edges": []}, headers=hb).status_code == 200
    client.delete(f"/api/canvas/{cid}", headers=hb)  # editor delete is a no-op (owner-only)
    assert client.get(f"/api/canvas/{cid}").status_code == 200  # still there
    assert any(f["id"] == cid and f["shared"] for f in client.get("/api/canvas", headers=hb).json())
    assert any(sh["userId"] == bob for sh in client.get(f"/api/canvas/{cid}/shares").json()["shares"])
    # Bob (editor) cannot share it further (owner-only)
    assert client.post(f"/api/canvas/{cid}/share", json={"userId": "local", "role": "editor"}, headers=hb).status_code == 403


def test_share_cannot_grant_owner_role():
    # P0-AUTH-01: a share may only confer 'editor'|'viewer'. Granting the literal 'owner' role used to
    # be accepted and canvas_role() then treated the recipient as owner (delete/reshare). It must 422,
    # and even a pre-existing bad row must not escalate (canvas_role clamps it to viewer).
    from hub import metadb
    mallory = client.post("/api/users", json={"name": "Mallory"}).json()["id"]
    cid = "escalate_cv"
    client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "c", "version": 1, "nodes": [], "edges": []})
    # the API rejects an 'owner' (or junk) role outright
    assert client.post(f"/api/canvas/{cid}/share", json={"userId": mallory, "role": "owner"}).status_code == 422
    assert client.post(f"/api/canvas/{cid}/share", json={"userId": mallory, "role": "admin"}).status_code == 422
    # the metadb write boundary rejects it too
    import pytest
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(ValueError):
        metadb.share_canvas(cid, mallory, "owner")
    # and the DB CHECK constraint is the last line of defense — an 'owner' row can't even be inserted
    with pytest.raises(IntegrityError):
        with metadb.session() as s:
            s.add(metadb.CanvasShare(canvas_id=cid, user_id=mallory, role="owner"))
    # a legitimately shared viewer cannot delete (ownership stays with owner_id)
    client.post(f"/api/canvas/{cid}/share", json={"userId": mallory, "role": "viewer"})
    hm = {"X-DP-User": mallory}
    client.delete(f"/api/canvas/{cid}", headers=hm)
    assert client.get(f"/api/canvas/{cid}").status_code == 200  # still there


def test_kernel_restart_requires_edit_access():
    # P0-AUTH-01: restarting a canvas's kernel is a disruptive action; a read-only viewer must not be
    # able to kill a shared canvas's kernel.
    viewer = client.post("/api/users", json={"name": "RestartViewer"}).json()["id"]
    cid = "restart_authz_cv"
    client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "c", "version": 1, "nodes": [], "edges": []})
    client.post(f"/api/canvas/{cid}/share", json={"visibility": "workspace_view"})  # everyone read-only
    hv = {"X-DP-User": viewer}
    assert client.get(f"/api/canvas/{cid}", headers=hv).status_code == 200        # viewer can read
    assert client.post(f"/api/canvas/{cid}/kernel/restart", headers=hv).status_code == 403  # but not restart
    assert client.post(f"/api/canvas/{cid}/kernel/restart").status_code == 200    # owner can


def test_workspace_view_visibility_is_read_only():
    # 'workspace_view' opens a canvas to everyone but read-only (viewer); 'workspace' opens it editable.
    carol = client.post("/api/users", json={"name": "Carol"}).json()["id"]
    cid = "wsview_cv"
    client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "ro", "version": 1, "nodes": [], "edges": []})
    hc = {"X-DP-User": carol}
    assert client.get(f"/api/canvas/{cid}", headers=hc).status_code == 404  # private until shared
    # owner opens it view-only to the workspace
    client.post(f"/api/canvas/{cid}/share", json={"visibility": "workspace_view"})
    assert client.get(f"/api/canvas/{cid}", headers=hc).status_code == 200            # everyone can read
    assert client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "x", "version": 2, "nodes": [], "edges": []}, headers=hc).status_code == 403  # but not write
    assert any(f["id"] == cid and f["role"] == "viewer" for f in client.get("/api/canvas", headers=hc).json())
    # flip to editable workspace visibility → the same user can now write
    client.post(f"/api/canvas/{cid}/share", json={"visibility": "workspace"})
    assert client.put(f"/api/canvas/{cid}", json={"id": cid, "name": "y", "version": 3, "nodes": [], "edges": []}, headers=hc).status_code == 200
    # an unknown visibility value is rejected
    assert client.post(f"/api/canvas/{cid}/share", json={"visibility": "public"}).status_code == 400


def test_written_outputs_reregister_on_restart(tmp_path):
    # durability: an output written to storage must reappear in the catalog after a kernel restart
    # (a fresh Deps), not vanish because the seeded data_dir doesn't include the outputs location.
    import duckdb
    from hub.deps import Deps
    ws, data = str(tmp_path / "ws"), str(tmp_path / "data")
    d1 = Deps(ws, data)
    uri = d1.storage.output_uri("myout", ".parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 AS x) TO '{uri}' (FORMAT PARQUET)")
    d2 = Deps(ws, data)  # simulate restart
    assert "myout" in [
        t.name for t in d2.catalog.list_page(CatalogQuery(limit=5000)).items]


def test_section_runs_its_parentid_children(tmp_path):
    # visual containment: a canvas node whose parentId is the section is a callable child — its
    # alias is its title, so the driver calls run("keep", …).
    p = _seq_parquet(tmp_path)  # v = 0..999
    child = {"id": "child1", "type": "filter", "parentId": "sec", "position": {"x": 0, "y": 0},
             "data": {"title": "keep", "config": {"predicate": "v < 300"}}}
    sec = {"id": "sec", "type": "section", "position": {"x": 0, "y": 0},
           "data": {"title": "sec", "config": {"script": "emit(run('keep', data=inputs['in']))\n",
                                               "params": {}, "maxRuns": 50}}}
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), sec, child, N("wr", "write", {"name": "sec_parent"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_parent").uri, "k": 5}).json()
    assert out["rowCount"] == 300  # the contained 'keep' filter kept v < 300


def test_section_nests_multiple_levels_by_parentid(tmp_path):
    # visual containment nests: a section contained in another section (parentId) runs, and its own
    # contained node resolves — outer.run('inner') carries inner's subtree so inner.run('keep') works.
    p = _seq_parquet(tmp_path)  # v = 0..999
    keep = {"id": "k1", "type": "filter", "parentId": "inner", "position": {"x": 0, "y": 0},
            "data": {"title": "keep", "config": {"predicate": "v < 300"}}}
    inner = {"id": "inner", "type": "section", "parentId": "outer", "position": {"x": 0, "y": 0},
             "data": {"title": "inner", "config": {"script": "emit(run('keep', data=inputs['in']))\n",
                                                   "params": {}, "maxRuns": 50}}}
    outer = {"id": "outer", "type": "section", "position": {"x": 0, "y": 0},
             "data": {"title": "outer", "config": {"script": "emit(run('inner', data=inputs['in']))\n",
                                                   "params": {}, "maxRuns": 50}}}
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), outer, inner, keep, N("wr", "write", {"name": "sec_nested"}),
    ], "edges": [E("src", "outer"), E("outer", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_nested").uri, "k": 5}).json()
    assert out["rowCount"] == 300  # outer → inner → keep(v<300), nested two levels deep


def test_section_child_multi_output_requires_and_honors_output_port(tmp_path):
    p = _seq_parquet(tmp_path)
    child = {
        "id": "inner-filter", "type": "filter", "parentId": "inner",
        "position": {"x": 0, "y": 0},
        "data": {"title": "f", "config": {}},
    }
    inner = {
        "id": "inner", "type": "section", "parentId": "outer",
        "position": {"x": 0, "y": 0},
        "data": {"title": "inner", "config": {
            "outputs": ["low", "high"],
            "script": (
                "emit('low', run(f, data=inputs['in'], predicate='v < 10'))\n"
                "emit('high', run(f, data=inputs['in'], predicate='v >= 990'))\n"
            ),
        }},
    }

    def graph(outer_script: str, output_name: str):
        outer = {
            "id": "outer", "type": "section", "position": {"x": 0, "y": 0},
            "data": {"config": {"script": outer_script}},
        }
        return {"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": p}), outer, inner, child,
            N("wr", "write", {"name": output_name}),
        ], "edges": [E("src", "outer"), E("outer", "wr")]}

    missing = _poll(client.post("/api/run", json={
        "graph": graph("emit(run(inner, data=inputs['in']))\n", "section_missing_port"),
        "targetNodeId": "wr", "confirmed": True,
    }).json()["runId"])
    assert missing["status"] == "failed"
    assert "select an output port" in (missing.get("error") or "")

    selected = _poll(client.post("/api/run", json={
        "graph": graph(
            "emit(run(inner, data=inputs['in'], output_port='high'))\n",
            "section_selected_port",
        ),
        "targetNodeId": "wr", "confirmed": True,
    }).json()["runId"])
    assert selected["status"] == "done"
    import duckdb
    uri = get_deps().catalog.get_table("tbl_section_selected_port").uri
    assert duckdb.connect(":memory:").execute(
        f"SELECT count(*), min(v), max(v) FROM read_parquet('{uri}')").fetchone() == (
            10, 990, 999)


def test_plan_hash_includes_section_children():
    # regression: a node CONTAINED in a section (parent_id) carries the real behavior (its predicate /
    # transform code), but it isn't in the upstream chain — a naive chain hash collided (proven). The
    # content key must fold section descendants, else editing a contained node reuses a stale result.
    from hub.models import Graph
    r = get_deps().runner

    def graph_with(pred):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            {"id": "sec", "type": "section", "position": {"x": 0, "y": 0},
             "data": {"config": {"script": "emit(run('keep', data=inputs['in']))\n", "maxRuns": 10}}},
            {"id": "k", "type": "filter", "parentId": "sec", "position": {"x": 0, "y": 0},
             "data": {"title": "keep", "config": {"predicate": pred}}},
            N("wr", "write", {"name": "o"}),
        ], "edges": [E("src", "sec"), E("sec", "wr")]})

    assert r._plan_hash(graph_with("amount > 0"), "wr") != r._plan_hash(graph_with("amount > 999"), "wr")
    assert r._plan_hash(graph_with("amount > 0"), "wr") == r._plan_hash(graph_with("amount > 0"), "wr")


def test_plan_hash_includes_bypass_and_disable_flags():
    # regression: bypassed/disabled live on data (siblings of config) and the engine lowers the relation
    # from them, but the plan hash folded only data.config — so toggling bypass served a STALE cached
    # preview/result. The hash must change when either flag flips.
    from hub.models import Graph
    r = get_deps().runner

    def graph_with(flags):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            {"id": "f", "type": "filter", "position": {"x": 0, "y": 0},
             "data": {"title": "f", "config": {"predicate": "amount > 0"}, **flags}},
            N("wr", "write", {"name": "o"}),
        ], "edges": [E("src", "f"), E("f", "wr")]})

    base = r._plan_hash(graph_with({}), "wr")
    assert base != r._plan_hash(graph_with({"bypassed": True}), "wr")   # bypass changes the lowered plan
    assert base != r._plan_hash(graph_with({"disabled": True}), "wr")   # disable too
    assert base != r._plan_hash(graph_with({"title": "renamed"}), "wr") # title too (a metric emits it)
    assert base == r._plan_hash(graph_with({"bypassed": False}), "wr")  # explicit False == absent


def test_plan_hash_includes_requirements():
    # P0-CACHE-01: a transform can import a canvas requirement, so a package-version edit must change
    # the plan hash — else the durable/warm cache serves a result computed against the old version.
    from hub.models import Graph
    r = get_deps().runner

    def graph_with(reqs):
        return Graph(**{"id": "c", "version": 1, "requirements": reqs, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            N("f", "filter", {"predicate": "amount > 0"}),
            N("wr", "write", {"name": "o"}),
        ], "edges": [E("src", "f"), E("f", "wr")]})

    assert r._plan_hash(graph_with(["pkg==1"]), "wr") != r._plan_hash(graph_with(["pkg==2"]), "wr")
    assert r._plan_hash(graph_with(["a", "b"]), "wr") == r._plan_hash(graph_with(["b", "a"]), "wr")  # order-free


def test_completed_run_result_is_db_cached(tmp_path):
    # A2: a finished run persists its result pointer to the shared DB (result_cache), so it's reused
    # across a kernel restart / another stateless instance — not just the accepting process's dict.
    from hub import metadb
    from hub.models import Graph
    from hub.run_outputs import sole_committed_document_output
    p = _seq_parquet(tmp_path)
    gd = {"id": "c", "version": 1, "nodes": [N("src", "source", {"uri": p}), N("wr", "write", {"name": "a2cache"})],
          "edges": [E("src", "wr")]}
    first = _poll(client.post(
        "/api/run", json={"graph": gd, "targetNodeId": "wr", "confirmed": True}
    ).json()["runId"])
    assert first["status"] == "done"
    first_output = _sole_output(first, outcome="committed")
    assert first_output["version"]
    phash = get_deps().runner._plan_hash(Graph(**gd), "wr")
    c = metadb.get_result(phash)
    cached_output = sole_committed_document_output(c)
    assert cached_output and cached_output.uri and cached_output.table  # persisted to the shared DB
    assert cached_output.version == first_output["version"]
    fresh_output = sole_committed_document_output(get_deps().runner._cache_get(phash))
    assert fresh_output and fresh_output.table == cached_output.table  # a fresh instance reads the same pointer

    second = _poll(client.post(
        "/api/run", json={"graph": gd, "targetNodeId": "wr", "confirmed": True}
    ).json()["runId"])
    assert second["status"] == "done"
    second_output = _sole_output(second, outcome="committed")
    assert (second_output["uri"], second_output["version"]) == (
        first_output["uri"], first_output["version"])

    with metadb.session() as session:
        facts = list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == first_output["uri"],
            metadb.CatalogLineageFact.run_id.in_([first["runId"], second["runId"]]),
        ).order_by(metadb.CatalogLineageFact.id)))
    assert [fact.run_id for fact in facts] == [first["runId"], second["runId"]]
    assert all(fact.destination_version == first_output["version"] for fact in facts)


def test_plan_cacheable_opt_out():
    # A2: a node with config.cacheable=False makes the whole plan non-cacheable (non-deterministic op),
    # so its result is neither stored nor reused.
    from hub.models import Graph
    r = get_deps().runner
    base = lambda extra: Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "xf", "type": "transform", "position": {"x": 0, "y": 0},
         "data": {"config": {"source": "adhoc", "mode": "map", "code": "def fn(row):\n    return row", **extra}}},
    ], "edges": [E("src", "xf")]})
    assert r._plan_cacheable(base({}), "xf") is True
    assert r._plan_cacheable(base({"cacheable": False}), "xf") is False


def test_kernel_info_reports_backends_with_capacity():
    # Phase B: KernelInfo gains a real backends[] topology (additive — the runners contract is kept).
    info = client.get("/api/kernel").json()
    assert info["runners"] == ["local-out-of-core", "local-subprocess", "kernel"]  # kernel is now a registered, selectable backend
    names = {b["name"] for b in info["backends"]}
    assert {"local-out-of-core", "local-subprocess"} <= names
    w = info["backends"][0]["workers"][0]
    assert w["capacity"]["cpu"] >= 1  # a real local worker advertising the host's capacity


def test_node_spec_exposes_requires():
    # Phase B: NodeSpec carries an optional `requires` (plugin-declared compute need); built-ins none.
    specs = client.get("/api/nodes").json()
    src = next(s for s in specs if s["kind"] == "source")
    assert "requires" in src and src["requires"] is None


def test_placement_satisfies_and_graph_requires():
    # C1: the capability-match rule + whole-graph aggregate requirement.
    from hub import placement
    from hub.models import Graph, ResourceSpec
    gpu = ResourceSpec(cpu=16, gpu=2, gpu_type="a100", mem="64GB")
    cpu = ResourceSpec(cpu=8)
    assert placement.satisfies(gpu, ResourceSpec(gpu=2, gpu_type="a100"))
    assert placement.satisfies(gpu, None)                             # no requirement → any worker
    assert not placement.satisfies(cpu, ResourceSpec(gpu=1))          # cpu worker can't host a gpu step
    assert not placement.satisfies(gpu, ResourceSpec(gpu=4))          # not enough gpus
    assert not placement.satisfies(gpu, ResourceSpec(gpu_type="h100"))  # wrong gpu type
    assert not placement.satisfies(cpu, ResourceSpec(mem="32GB"))     # not enough mem
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "cap", "type": "transform", "position": {"x": 0, "y": 0},
         "data": {"config": {"code": "def fn(row):\n    return row", "requires": {"gpu": 8, "gpuType": "a100"}}}},
    ], "edges": [E("src", "cap")]})
    req = placement.graph_requires(g, get_deps().node_specs)
    assert req.gpu == 8 and req.gpu_type == "a100"                    # max over the graph's nodes


def test_pool_backend_workers_and_placement(tmp_path, monkeypatch):
    # C1: DP_POOL_WORKERS registers a reference pool backend that advertises workers with capacities
    # and places by capability — the whole path is real (only the GPU is simulated), no cluster needed.
    import json

    from hub.deps import Deps
    from hub.models import ResourceSpec
    monkeypatch.setenv("DP_POOL_WORKERS", json.dumps([{"name": "cpu", "cpu": 8},
                                                      {"name": "gpu", "cpu": 16, "gpu": 2, "gpu_type": "a100"}]))
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    pool = next(r for r in d.runners if r.name == "local-pool")
    assert {w.id for w in pool.workers()} == {"cpu", "gpu"}
    assert pool.place(ResourceSpec(gpu=2, gpu_type="a100")) == "gpu"  # only the gpu worker satisfies it
    assert pool.place(ResourceSpec(cpu=4)) in ("cpu", "gpu")          # both satisfy; idle-first
    assert pool.place(ResourceSpec(gpu=4)) is None                    # nothing in the pool satisfies it
    # and it surfaces in KernelInfo.backends (→ the Compute view) with its capacities
    pool_b = next(b for b in d.info().backends if b.name == "local-pool")
    assert any(w.capacity.gpu == 2 and w.capacity.gpu_type == "a100" for w in pool_b.workers)


def test_run_routes_to_a_capability_matching_backend(tmp_path, monkeypatch):
    # C1.5: a graph declaring a GPU requirement auto-routes to a backend that can place it (the pool),
    # even when the default backend can't. A hint, not a gate: no requirement → the choice is untouched.
    import json

    from hub.deps import Deps
    from hub.models import Graph
    from hub.routers.runs import _route_by_capability
    monkeypatch.setenv("DP_POOL_WORKERS", json.dumps([{"name": "gpu", "cpu": 16, "gpu": 2, "gpu_type": "a100"}]))
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    gpu_graph = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "cap", "type": "transform", "position": {"x": 0, "y": 0},
         "data": {"config": {"code": "def fn(row):\n    return row", "requires": {"gpu": 2, "gpuType": "a100"}}}},
    ], "edges": [E("src", "cap")]})
    plain = Graph(**{"id": "c", "version": 1, "nodes": [N("src", "source", {"uri": _uri("events")})], "edges": []})
    assert _route_by_capability(d, d.runner, gpu_graph).name == "local-pool"  # in-process can't place gpu → pool
    assert _route_by_capability(d, d.runner, plain) is d.runner               # no requirement → unchanged


def test_planner_partitions_by_placement():
    # C2: split a run's graph into regions — plain graph = one region; a GPU transform in the middle =
    # three regions (cpu → gpu → cpu) with materialized handoffs; a section is one opaque unit.
    from hub import planner
    from hub.models import Graph
    specs = get_deps().node_specs

    def place_fn(req):  # fake GPU pool
        return ("pool", "gpu") if req.gpu else None

    lin = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}), N("f", "filter", {"predicate": "amount > 0"}), N("wr", "write", {"name": "o"}),
    ], "edges": [E("src", "f"), E("f", "wr")]})
    r = planner.plan_regions(lin, "wr", specs, place_fn)
    assert len(r) == 1 and r[0].node_ids == {"src", "f", "wr"} and r[0].backend == "default"

    gpu = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}), N("f", "filter", {"predicate": "amount > 0"}),
        {"id": "cap", "type": "transform", "position": {"x": 0, "y": 0},
         "data": {"config": {"code": "def fn(r):\n    return r", "requires": {"gpu": 2}}}},
        N("wr", "write", {"name": "o"}),
    ], "edges": [E("src", "f"), E("f", "cap"), E("cap", "wr")]})
    rs = planner.plan_regions(gpu, "wr", specs, place_fn)
    by_out = {x.output_node: x for x in rs}
    assert set(by_out) == {"f", "cap", "wr"}                                   # f/cap/wr are boundaries
    assert by_out["cap"].backend == "pool" and by_out["cap"].worker == "gpu"   # placed on the GPU worker
    assert by_out["f"].node_ids == {"src", "f"}                                # region absorbs its upstream
    order = [x.output_node for x in rs]
    assert order.index("f") < order.index("cap") < order.index("wr")           # topo order
    assert any(ci[0] == "f" and ci[2] == "cap" for ci in by_out["cap"].cut_inputs)   # cap reads f's ref
    assert any(ci[0] == "cap" and ci[2] == "wr" for ci in by_out["wr"].cut_inputs)   # wr reads cap's ref

    sec = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "sec", "type": "section", "position": {"x": 0, "y": 0}, "data": {"config": {"script": "emit(inputs['in'])\n"}}},
        {"id": "gk", "type": "transform", "parentId": "sec", "position": {"x": 0, "y": 0},
         "data": {"config": {"code": "def fn(r):\n    return r", "requires": {"gpu": 4}}}},
        N("wr", "write", {"name": "o"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]})
    rs2 = planner.plan_regions(sec, "wr", specs, place_fn)
    all_nodes = {n for x in rs2 for n in x.node_ids}
    assert "gk" not in all_nodes                                               # section child isn't a top-level region node
    secr = next(x for x in rs2 if "sec" in x.node_ids)
    assert secr.backend == "pool" and secr.worker == "gpu"                     # requires escalated from the contained node


def test_run_controller_executes_checkpointed_regions(tmp_path):
    # C2: a `checkpoint` splits the run into two regions; the controller materializes the upstream
    # region, then runs the final region reading its ref — the result matches an unsplit run.
    import uuid

    p = _seq_parquet(tmp_path)  # v = 0..999
    output_name = f"ckpt_out_{uuid.uuid4().hex}"
    gd = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        {"id": "f1", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "v < 500", "checkpoint": True}}},
        N("f2", "filter", {"predicate": "v >= 100"}),
        N("wr", "write", {"name": output_name}),
    ], "edges": [E("src", "f1"), E("f1", "f2"), E("f2", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": gd, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done", st
    table = get_deps().catalog.get_table(f"tbl_{output_name}")
    out = client.post("/api/data/sample", json={"uri": table.uri, "k": 5}).json()
    assert out["rowCount"] == 400  # v in [100, 500) → 400 rows, split across two regions
    edges = get_deps().catalog.lineage(table.uri).edges
    assert any(edge.parent == p and edge.child == table.uri for edge in edges)
    assert not any(edge.child == table.uri and "/regions/" in edge.parent for edge in edges)
    from hub import metadb
    with metadb.session() as session:
        facts = list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == table.uri,
            metadb.CatalogLineageFact.run_id == st["runId"],
        )))
    assert len(facts) == 1
    assert facts[0].source_uri == p
    assert facts[0].attempt_id and facts[0].attempt_id != st["runId"]
    assert (facts[0].producer, facts[0].producer_version, facts[0].step_id) == ("c", 1, "wr")


def test_default_region_runs_isolated_when_kernel_is_selected():
    # P0-EXEC-01: a multi-region run's DEFAULT (unplaced) region must NOT execute in the hub PID when the
    # per-canvas kernel is the selected backend — it routes to the isolated, deadline-bounded, sandboxed
    # child (hub.subprocess_runner). Only an explicit in-process (local-out-of-core) selection keeps the base.
    from hub import metadb
    from hub.models import ResourceSpec
    from hub.planner import Region
    d = get_deps()
    region = Region(id="r", node_ids={"a"}, output_node="a", backend="default", worker=None,
                    requires=ResourceSpec(), cut_inputs=[])
    metadb.set_setting("backend", "kernel", scope="global")
    try:
        assert d.controller._backend_runner(region).name == "local-subprocess"  # isolated, not the hub PID
        metadb.set_setting("backend", "local-out-of-core", scope="global")
        assert d.controller._backend_runner(region) is d.runner                  # explicit in-process wins
    finally:
        metadb.set_setting("backend", "", scope="global")  # restore the suite default


def test_run_controller_places_a_region_on_a_pool_worker(tmp_path, monkeypatch):
    # C3: a GPU-requiring transform in the middle physically runs in the pool WORKER's process
    # (subprocess run_unit), the rest on the default backend; the joined result is correct.
    import json

    from hub.deps import Deps
    from hub.models import Graph
    monkeypatch.setenv("DP_POOL_WORKERS", json.dumps([{"name": "gpu", "cpu": 8, "gpu": 2, "gpu_type": "a100"}]))
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    p = _seq_parquet(tmp_path)  # v = 0..999
    gd = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        {"id": "cap", "type": "transform", "position": {"x": 0, "y": 0},
         "data": {"config": {"source": "adhoc", "mode": "map",
                             "code": "def fn(row):\n    row['v2'] = row['v'] * 2\n    return row",
                             "requires": {"gpu": 2, "gpuType": "a100"}}}},
        N("wr", "write", {"name": "c3out"}),
    ], "edges": [E("src", "cap"), E("cap", "wr")]})
    st = d.controller.run(gd, "wr")
    assert st is not None  # a placed region → multi-region (not the single-region base path)
    for _ in range(300):
        if d.controller.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    final = d.controller.status(st.run_id)
    assert final.status == "done", final.error
    assert final.rows_processed == 1000                              # all rows joined back through the ref handoffs
    tbl = d.catalog.get_table("tbl_c3out")
    assert "v2" in [c.name for c in tbl.columns]                     # the GPU-placed transform ran (added v2)


def test_plan_not_cacheable_for_stale_prone_plans():
    # adversarial-review fix: the DURABLE cache must NOT reuse plans whose identity isn't fully in the
    # key — object-store/mem sources (URI-only fingerprint), append (not idempotent), library/plugin
    # ops (code not hashed). A miss just recomputes; a stale durable hit is fleet-wide wrong data.
    from hub.models import Graph
    r = get_deps().runner
    obj = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": "s3://b/x.parquet"})], "edges": []})
    assert r._plan_cacheable(obj, "s") is False
    ap = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": _uri("events")}),
                                                     N("w", "write", {"name": "o", "writeMode": "append"})], "edges": [E("s", "w")]})
    assert r._plan_cacheable(ap, "w") is False
    lib = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": _uri("events")}),
                   {"id": "xf", "type": "transform", "position": {"x": 0, "y": 0}, "data": {"config": {"source": "library", "processor": "p1", "version": "v1"}}}], "edges": [E("s", "xf")]})
    assert r._plan_cacheable(lib, "xf") is False
    ok = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": _uri("events")}), N("f", "filter", {"predicate": "amount > 0"})], "edges": [E("s", "f")]})
    assert r._plan_cacheable(ok, "f") is True  # plain local overwrite plan is still reusable


def test_subgraph_preserves_join_operand_order():
    # adversarial-review fix (#5): the region's reduced graph must keep a multi-input node's operands
    # in ORIGINAL order — the engine feeds join positionally, so a swapped ref/intra edge silently
    # joins the wrong sides. Here operand 'a' is a cut (ref), 'b' is intra; order must stay [a, b].
    from hub import graph as gg
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region
    ctrl = get_deps().controller
    graph = Graph(**{"id": "c", "version": 1, "nodes": [
        N("upA", "source", {"uri": _uri("events")}), N("inB", "source", {"uri": _uri("events")}),
        {"id": "j", "type": "join", "position": {"x": 0, "y": 0}, "data": {"config": {}}},
    ], "edges": [
        {"id": "ea", "source": "upA", "target": "j", "targetHandle": "a", "data": {"wire": "dataset"}},
        {"id": "eb", "source": "inB", "target": "j", "targetHandle": "b", "data": {"wire": "dataset"}},
    ]})
    region = Region(id="r", node_ids={"inB", "j"}, output_node="j", backend="default", worker=None,
                    requires=ResourceSpec(), cut_inputs=[("upA", None, "j", "a")])
    sub = ctrl._subgraph(graph, region, {"upA": "/tmp/ref.parquet"})
    assert [e.target_handle for e in gg.incoming(sub, "j")] == ["a", "b"]  # ref 'a' first, intra 'b' second


def test_region_ref_ids_are_deterministic_collision_safe_and_reused():
    from hub import graph as gg
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region
    from hub.run_controller import _region_internal_id_base

    real_source = "s3://raw/real"
    real_ref = "s3://managed/regions/real"
    ref_base = _region_internal_id_base("ref", "x")
    first_edge_base = _region_internal_id_base(
        "edge", "cut-right", "x", None, "z-right", None)
    graph = Graph(**{"id": "lineage", "version": 1, "nodes": [
        N("raw", "source", {"uri": real_source}),
        N("x", "filter", {"predicate": "id > 0"}),
        # Client-controlled nodes occupy the old predictable ID, the new SHA-derived ref base, and
        # the first cut-edge base. None may be mistaken for a controller-owned source or edge.
        N("__ref_x", "source", {"uri": "s3://attacker/old-id"}),
        N(ref_base, "source", {"uri": "s3://attacker/ref-base"}),
        N(first_edge_base, "source", {"uri": "s3://attacker/edge-base"}),
        # Deliberately non-lexical region-node order: reconstruction must follow graph.nodes, not its set.
        N("z-right", "filter", {"predicate": "id > 0"}),
        N("a-left", "filter", {"predicate": "id > 0"}),
        N("join", "join", {}), N("write", "write", {"name": "out"}),
    ], "edges": [
        {"id": "raw-x", "source": "raw", "target": "x"},
        {"id": "cut-right", "source": "x", "target": "z-right"},
        {"id": "cut-left", "source": "x", "target": "a-left"},
        # Incoming join order is intentionally different from region-node order and must be preserved.
        {"id": "left-join", "source": "a-left", "target": "join", "targetHandle": "a"},
        {"id": "right-join", "source": "z-right", "target": "join", "targetHandle": "b"},
        {"id": "join-write", "source": "join", "target": "write"},
    ]})
    region = Region(
        id="final", node_ids={"a-left", "z-right", "join", "write"}, output_node="write",
        backend="default", worker=None, requires=ResourceSpec(),
        cut_inputs=[("x", None, "z-right", None), ("x", None, "a-left", None)],
    )
    sub = get_deps().controller._subgraph(graph, region, {"x": real_ref})
    repeated = get_deps().controller._subgraph(graph, region, {"x": real_ref})

    assert sub.model_dump(by_alias=True) == repeated.model_dump(by_alias=True)
    assert [node.id for node in sub.nodes[:4]] == ["z-right", "a-left", "join", "write"]
    generated_source = next(
        node for node in sub.nodes
        if (node.data.get("config") or {}).get("uri") == real_ref)
    assert generated_source.id.startswith(ref_base + "_")
    assert generated_source.id not in {node.id for node in graph.nodes}
    cut_edges = [edge for edge in sub.edges if edge.target in {"z-right", "a-left"}]
    assert len(cut_edges) == 2
    assert {edge.source for edge in cut_edges} == {generated_source.id}
    assert next(edge for edge in cut_edges if edge.target == "z-right").id.startswith(
        first_edge_base + "_")
    assert [edge.target_handle for edge in gg.incoming(sub, "join")] == ["a", "b"]
    assert gg.execution_source_uris(sub, "write") == [real_ref]
    assert gg.all_upstream_publication_uris(sub, "write") == [real_source]

    node_ids = [node.id for node in sub.nodes]
    edge_ids = [edge.id for edge in sub.edges]
    assert len(node_ids) == len(set(node_ids))
    assert len(edge_ids) == len(set(edge_ids))
    assert len(node_ids + edge_ids) == len(set(node_ids + edge_ids))
    dumped = sub.model_dump(by_alias=True)
    assert "publicationSourceUris" not in dumped and "_publication_source_uris" not in dumped

    # Neither a top-level private-looking key nor user-controlled node data can forge provenance.
    forged = Graph.model_validate({
        "id": "forged", "version": 1,
        "_publication_source_uris": {"source": ["s3://forged/top-level"]},
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"publicationSourceUris": ["s3://forged/node"],
                            "config": {"uri": "s3://real/source"}}}],
        "edges": [],
    })
    assert gg.all_upstream_publication_uris(forged, "source") == ["s3://real/source"]


def test_region_publication_lineage_survives_multiple_nested_cuts():
    from hub import graph as gg
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region

    source = "s3://raw/original"
    graph = Graph(**{"id": "nested-lineage", "version": 1, "nodes": [
        N("source", "source", {"uri": source}),
        N("stage-one", "filter", {"predicate": "id > 0"}),
        N("stage-two", "filter", {"predicate": "id > 1"}),
        N("write", "write", {"name": "out"}),
    ], "edges": [
        E("source", "stage-one"), E("stage-one", "stage-two"), E("stage-two", "write"),
    ]})
    middle_region = Region(
        id="middle", node_ids={"stage-two", "write"}, output_node="write",
        backend="default", worker=None, requires=ResourceSpec(),
        cut_inputs=[("stage-one", None, "stage-two", None)],
    )
    middle_ref = "s3://managed/regions/middle"
    middle = get_deps().controller._subgraph(
        graph, middle_region, {"stage-one": middle_ref})
    assert gg.execution_source_uris(middle, "write") == [middle_ref]
    assert gg.all_upstream_publication_uris(middle, "write") == [source]

    final_region = Region(
        id="final", node_ids={"write"}, output_node="write",
        backend="default", worker=None, requires=ResourceSpec(),
        cut_inputs=[("stage-two", None, "write", None)],
    )
    final_ref = "s3://managed/regions/final"
    final = get_deps().controller._subgraph(
        middle, final_region, {"stage-two": final_ref})
    assert gg.execution_source_uris(final, "write") == [final_ref]
    assert gg.all_upstream_publication_uris(final, "write") == [source]


def test_controller_refuses_unsafe_splits():
    # adversarial-review fix (#6): a checkpoint would split this, but an INTERMEDIATE write must commit
    # (materializing it would drop the commit) — so the controller refuses to split and runs it whole.
    from hub.models import Graph
    ctrl = get_deps().controller
    g_mid_write = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "f1", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "amount > 0", "checkpoint": True}}},
        N("w1", "write", {"name": "mid"}),  # intermediate write (not the target)
        N("wr", "write", {"name": "fin"}),
    ], "edges": [E("src", "f1"), E("f1", "w1"), E("w1", "wr")]})
    assert ctrl.run(g_mid_write, "wr") is None  # refuses the split → base runner (whole graph, commits both writes)


def test_base_guard_recovers_an_aborted_base_connection():
    # adversarial-review fix (#10): a failed base-conn statement leaves DuckDB's implicit transaction
    # ABORTED, and one shared connection then rejects EVERY later op with "transaction is aborted" —
    # wedging the whole engine until restart. base_guard must roll it back so ops self-heal. Reproduce
    # the aborted state deterministically, then prove a follow-up op recovers (would wedge forever
    # without the rollback). SELECT 1 executes even when aborted, so probe a real table read.
    from hub import db
    path = _uri("events")
    q = f"SELECT count(*) AS n FROM read_parquet('{path.replace(chr(39), chr(39) * 2)}')"
    assert db.query(q)[0]["n"] > 0  # baseline
    with db.lock():  # wedge the base connection: a failing statement inside an explicit transaction
        db._base_conn().execute("BEGIN TRANSACTION")
        try:
            db._base_conn().execute("SELECT CAST('abc' AS INTEGER)").fetchall()  # leaves txn aborted
        except Exception:  # noqa: BLE001
            pass
    try:
        db.query(q)  # inherits the aborted txn, fails — but base_guard ROLLS BACK on the way out
    except Exception:  # noqa: BLE001
        pass
    assert db.query(q)[0]["n"] > 0  # healed — stays wedged forever without the rollback


def test_base_connection_concurrent_access_stays_clean():
    # adversarial-review fix (#10): catalog register (count/schema on runner / subprocess-watch DAEMON
    # threads), run_scope cursor creation, and request threads ALL touch the base DuckDB connection —
    # which is not safe for concurrent use. base_guard (count/schema/query) + serialized cursor
    # creation must keep it clean under contention (before the fix this raced: count()->None, an
    # aborted-transaction cascade, or a hard crash). Hammer all three paths concurrently.
    import concurrent.futures

    from hub import db
    uri = _uri("events")
    adapter = get_deps().resolve_adapter(uri)
    errors: list[Exception] = []

    def metadata(_):
        try:
            assert adapter.count(uri) and adapter.count(uri) > 0  # never a concurrency-induced None
            adapter.schema(uri)
            db.query("SELECT count(*) FROM range(10)")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def scoped(_):
        try:
            with db.run_scope():  # exercises the (now serialized) base-connection cursor creation
                db.conn().execute("SELECT count(*) FROM range(5000)").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(metadata, i) for i in range(120)] + [ex.submit(scoped, i) for i in range(120)]
        concurrent.futures.wait(futs)
    assert not errors, errors[:3]


def test_key_detection_tags_id_columns():
    # catalog-driven join hints start from key detection: id-like columns get a "key" capability;
    # media / vector / value columns never do (an image_url is not a join key).
    from hub.models import ColumnSchema
    from hub.plugins.capabilities import tag_columns
    tagged = {c.name: c.capabilities for c in tag_columns([
        ColumnSchema(name="id", type="int"), ColumnSchema(name="user_id", type="int"),
        ColumnSchema(name="order_uuid", type="string"), ColumnSchema(name="amount", type="float"),
        ColumnSchema(name="image_url", type="string"), ColumnSchema(name="grid", type="int"),
    ])}
    assert "key" in tagged["id"] and "key" in tagged["user_id"] and "key" in tagged["order_uuid"]
    assert "key" not in tagged["amount"]      # a measure, not a key
    assert "key" not in tagged["image_url"]   # media beats the (absent) key match
    assert "key" not in tagged["grid"]        # ends in 'id' but isn't an id column


def test_catalog_infers_key_candidates():
    # every seeded dataset exposes inferred primary-key candidates (composite-aware model, single here)
    d = get_deps()
    evs = d.catalog.get_table("tbl_events")
    keycols = {tuple(k.columns) for k in evs.keys}
    assert ("id",) in keycols and ("user_id",) in keycols
    assert all(k.confidence == "inferred" for k in evs.keys)  # name-based, not yet measured


def test_join_suggestions_measure_cardinality():
    # THE catalog-driven join hint: two datasets → ranked keys with MEASURED cardinality. events.id
    # and images.id are both unique (1:1); images.id ↔ events.user_id is 1:N (user_id repeats).
    d = get_deps()
    body = {"leftUri": d.catalog.get_table("tbl_images").uri, "rightUri": d.catalog.get_table("tbl_events").uri}
    sugg = client.post("/api/catalog/join-suggestions", json=body).json()
    assert sugg, "expected at least one join suggestion"
    top = sugg[0]
    assert top["leftColumns"] == ["id"] and top["rightColumns"] == ["id"] and top["cardinality"] == "1:1"
    assert top["confidence"] == "verified"  # cardinality came from the data, not a guess
    one_to_many = next(s for s in sugg if s["rightColumns"] == ["user_id"])
    assert one_to_many["cardinality"] == "1:N"  # one image id → many events


def test_grain_propagates_through_relational_ops():
    # the core insight: a filtered/sampled dataset keeps its key (still joinable); a group-by re-grains
    # to its group key. grain_of computes this structurally (no scan).
    from hub import grain
    from hub.models import Graph
    d = get_deps()
    g_ = Graph(**{"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "f", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "amount > 0"}}},
        {"id": "agg", "type": "aggregate", "position": {"x": 0, "y": 0}, "data": {"config": {"groupBy": "user_id", "aggs": "count(*) AS n"}}},
    ], "edges": [E("src", "f"), E("f", "agg")]})
    assert grain.grain_of(g_, "f", d.catalog).columns == ["id"]        # filter preserves the key
    ag = grain.grain_of(g_, "agg", d.catalog)
    assert ag.columns == ["user_id"] and ag.verified                    # re-grained to the group key, unique


def test_join_analysis_warns_on_fanout():
    # P2: when the best join isn't 1:1, warn that rows fan out. events-aggregated-by-user_id (unique
    # user_id) joined to raw events (user_id repeats) is 1:N.
    ev = _uri("events")
    graph = {"id": "c", "version": 1, "nodes": [
        N("l0", "source", {"uri": ev}),
        {"id": "l", "type": "aggregate", "position": {"x": 0, "y": 0}, "data": {"config": {"groupBy": "user_id", "aggs": "count(*) AS n"}}},
        N("r", "source", {"uri": ev}),
        N("j", "join", {}),
    ], "edges": [E("l0", "l"), E("l", "j", th="a"), E("r", "j", th="b")]}
    ja = client.post("/api/graph/join-analysis", json={"graph": graph, "targetNodeId": "j"}).json()
    assert ja["suggestions"], "expected a user_id join suggestion"
    assert ja["suggestions"][0]["cardinality"] == "1:N"
    assert ja["warning"] and "fans out" in ja["warning"]


def test_measure_unique_handles_one_shot_reader_relation():
    # adversarial-review #1: an adapter (Lance) whose scan returns a ONE-SHOT Arrow reader relation
    # must be measured in a SINGLE pass — a two-pass count-then-distinct drains the reader and reports
    # every key non-unique. A unique key must read unique; a repeating one, non-unique.
    import pyarrow as pa

    from hub import db, relationships as rel
    tbl = pa.table({"id": list(range(100)), "grp": [i % 10 for i in range(100)]})

    class OneShot:
        def scan(self, uri, columns=None, **k):
            sel = tbl.select(columns) if columns else tbl
            return db.conn().from_arrow(pa.RecordBatchReader.from_batches(sel.schema, sel.to_batches()))
    resolve = lambda uri: OneShot()  # noqa: E731
    assert rel.measure_unique("x", ["id"], resolve)[0] is True     # not drained to distinct=0
    assert rel.measure_unique("x", ["grp"], resolve)[0] is False   # 10 distinct / 100 rows


def test_cardinality_unknown_when_key_unmeasurable():
    # adversarial-review #4/#8: an unreadable column (or empty data) → uniqueness is None ('unknown'),
    # never a false 'not unique' that would fabricate an N:M cardinality stamped 'verified'.
    from hub import relationships as rel
    d = get_deps()
    assert rel.measure_unique(_uri("events"), ["nope_missing_col"], d.resolve_adapter)[0] is None
    assert rel.cardinality(None, True) == "unknown"


def test_grain_lost_when_select_renames_the_key():
    # adversarial-review #2: a select that renames/derives the key must NOT keep reporting the old key
    # as grain (a downstream measure would then hit the wrong physical column). A bare passthrough keeps it.
    from hub import grain
    from hub.models import Graph
    d = get_deps()

    def sel(expr):
        g_ = Graph(**{"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            {"id": "s", "type": "select", "position": {"x": 0, "y": 0}, "data": {"config": {"select": expr}}},
        ], "edges": [E("src", "s")]})
        return grain.grain_of(g_, "s", d.catalog)
    assert sel("id, amount").columns == ["id"]        # bare passthrough → key survives
    assert sel("id AS event_id, amount").known is False  # renamed away → grain not claimed
    assert sel("md5(id) AS h").known is False            # derived → grain not claimed


def test_declared_key_overrides_inference_and_grain():
    # declared keys are the escape hatch (opaque transforms / missed heuristics): a declared PK leads
    # the table's keys and WINS in grain over inferred/measured. Cleans up so it can't leak.
    from hub import grain
    from hub.models import Graph
    d = get_deps()
    ev = d.catalog.get_table("tbl_events")
    try:
        r = client.put(f"/api/catalog/tables/{ev.id}/key", json={"columns": ["user_id"]})
        assert r.status_code == 200
        keys = r.json()["keys"]
        assert keys[0] == {"columns": ["user_id"], "confidence": "declared", "unique": None}
        assert not any(k["columns"] == ["user_id"] and k["confidence"] == "inferred" for k in keys)  # dedup
        g_ = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev.uri})], "edges": []})
        gi = grain.grain_of(g_, "s", d.catalog)
        assert gi.columns == ["user_id"] and gi.verified  # declared key wins over the inferred `id`
        assert client.put(f"/api/catalog/tables/{ev.id}/key", json={"columns": ["nope"]}).status_code == 400
    finally:
        client.put(f"/api/catalog/tables/{ev.id}/key", json={"columns": []})  # clear (don't leak)
    # clearing restores ALL inferred keys (a declared key must not eat the name-heuristic fallback)
    assert {tuple(k.columns) for k in d.catalog.get_table(ev.id).keys} == {("id",), ("user_id",)}


def test_atomic_catalog_edit_commits_metadata_and_key_with_cas(monkeypatch):
    """The drawer's one Save is all-or-nothing, and an older drawer cannot overwrite it."""
    from hub import metadb

    table_id = "tbl_events"
    original = client.get(f"/api/catalog/tables/{table_id}").json()
    original_key = next((key["columns"] for key in original["keys"]
                         if key["confidence"] == "declared"), [])
    changed = {
        "expectedRevision": original["metadataRevision"],
        "name": "events atomic", "folder": "curated/atomic", "tags": ["atomic", "gold"],
        "owner": "catalog-team", "description": "one commit", "declaredKey": ["user_id"],
    }
    try:
        saved = client.put(f"/api/catalog/tables/{table_id}/edit", json=changed)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["name"] == "events atomic"
        assert body["folder"] == "curated/atomic"
        assert body["tags"] == ["atomic", "gold"]
        assert body["keys"][0]["columns"] == ["user_id"]
        assert body["metadataRevision"] != original["metadataRevision"]

        stale = client.put(f"/api/catalog/tables/{table_id}/edit", json=changed)
        assert stale.status_code == 409
        assert client.get(f"/api/catalog/tables/{table_id}").json()["name"] == "events atomic"

        before_failure = client.get(f"/api/catalog/tables/{table_id}").json()
        failing = {**changed, "expectedRevision": before_failure["metadataRevision"],
                   "name": "must not persist", "declaredKey": []}
        monkeypatch.setattr(metadb, "_sync_children", lambda *_args: (_ for _ in ()).throw(RuntimeError("injected failure")))
        failing_client = TestClient(app, raise_server_exceptions=False)
        failed = failing_client.put(f"/api/catalog/tables/{table_id}/edit", json=failing)
        assert failed.status_code == 500
        after_failure = client.get(f"/api/catalog/tables/{table_id}").json()
        assert after_failure["name"] == before_failure["name"]
        assert after_failure["folder"] == before_failure["folder"]
        assert after_failure["keys"] == before_failure["keys"]
    finally:
        monkeypatch.undo()
        client.put(f"/api/catalog/tables/{table_id}/metadata", json={
            "name": original["name"], "folder": original["folder"], "tags": original["tags"],
            "owner": original["owner"], "description": original["description"],
        })
        client.put(f"/api/catalog/tables/{table_id}/key", json={"columns": original_key})


def test_atomic_catalog_edit_rejects_external_catalog_subclasses(monkeypatch, tmp_path):
    """Built-in storage transactions must never be reported as writes to an external provider."""
    from hub.deps import get_deps
    from hub.plugins.catalog import InMemoryCatalog

    class ReadOnlyExternal(InMemoryCatalog):
        folders_mutable = False

    monkeypatch.setattr(
        get_deps(), "catalog", ReadOnlyExternal(str(tmp_path), lambda _uri: object()))
    response = client.put("/api/catalog/tables/anything/edit", json={
        "expectedRevision": "m1_stale",
        "folder": "", "tags": [], "owner": None, "description": None, "declaredKey": [],
    })
    assert response.status_code == 501


def test_relationship_crud_and_leads_join_analysis():
    # declared relationships persist (Settings, cross-instance) and TRUMP measurement in join analysis.
    ev, img = _uri("events"), _uri("images")
    rel = {"leftUri": img, "leftColumns": ["id"], "rightUri": ev, "rightColumns": ["user_id"], "cardinality": "1:N"}
    try:
        assert client.post("/api/catalog/relationships", json=rel).status_code == 200
        listed = client.get(f"/api/catalog/relationships?uri={ev}").json()
        assert len(listed) == 1 and listed[0]["cardinality"] == "1:N" and listed[0]["confidence"] == "declared"
        graph = {"id": "c", "version": 1, "nodes": [
            N("l", "source", {"uri": img}), N("r", "source", {"uri": ev}), N("j", "join", {}),
        ], "edges": [E("l", "j", th="a"), E("r", "j", th="b")]}
        ja = client.post("/api/graph/join-analysis", json={"graph": graph, "targetNodeId": "j"}).json()
        top = ja["suggestions"][0]
        assert top["confidence"] == "declared" and top["leftColumns"] == ["id"] and top["rightColumns"] == ["user_id"]
    finally:
        client.post("/api/catalog/relationships/delete", json=rel)
    assert client.get("/api/catalog/relationships").json() == []


def test_measure_unique_composite_null_is_not_unique():
    # adversarial-review #8: a composite key with a NULL field must read NON-unique (a NULL key can't
    # join) — count(DISTINCT (a,b)) alone counts null-bearing tuples as distinct, so a FILTER excludes
    # them, matching the single-column NULL semantics.
    import pyarrow as pa

    from hub import db, relationships as rel
    # rows: (1,None),(1,None),(2,'y') — distinct non-null tuples = 1 (only (2,'y')), count(*) = 3 → not unique
    tbl = pa.table({"a": [1, 1, 2], "b": [None, None, "y"]})

    class OneShot:
        def scan(self, uri, columns=None, **k):
            sel = tbl.select(columns) if columns else tbl
            return db.conn().from_arrow(pa.RecordBatchReader.from_batches(sel.schema, sel.to_batches()))
    assert rel.measure_unique("x", ["a", "b"], lambda u: OneShot())[0] is False


def test_relationships_survives_a_malformed_stored_row():
    # adversarial-review #3: a bad row in the relationships Setting (manual edit / version skew) must
    # be skipped, not 500 the whole feature (incl. the delete path needed to remove it).
    from hub import metadb
    from hub.deps import get_deps
    cat = get_deps().catalog
    good = {"leftUri": _uri("images"), "leftColumns": ["id"], "rightUri": _uri("events"),
            "rightColumns": ["user_id"], "cardinality": "1:N", "confidence": "declared"}
    try:
        metadb.catalog_upsert_relationship("__bad__", {"garbage": True})       # a malformed row
        metadb.catalog_upsert_relationship("__good__", good)                   # a valid row
        rels = cat.relationships()  # must not raise
        assert len(rels) == 1 and rels[0].cardinality == "1:N"
        assert client.get("/api/catalog/relationships").status_code == 200
    finally:
        metadb.catalog_delete_relationship("__bad__")
        metadb.catalog_delete_relationship("__good__")


def test_join_analysis_reflects_the_configured_key():
    # adversarial-review: analyze_join (→ agent `validate`) must report the cardinality of the key the
    # join is ACTUALLY configured with, not the top-ranked candidate — else the 'no fan-out' all-clear
    # is wrong. images.id↔events.id is 1:1 (ranks top), but a join CONFIGURED on id=user_id is 1:N.
    from hub import relationships as rel
    from hub.executors.schema import schema_for_graph
    from hub.models import Graph
    d = get_deps()
    graph = Graph(**{"id": "c", "version": 1, "nodes": [
        N("l", "source", {"uri": _uri("images")}), N("r", "source", {"uri": _uri("events")}),
        {"id": "j", "type": "join", "position": {"x": 0, "y": 0}, "data": {"config": {"condition": "a.id = b.user_id"}}},
    ], "edges": [E("l", "j", th="a"), E("r", "j", th="b")]})
    cols = schema_for_graph(graph, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    ja = rel.analyze_join(graph, "j", cols, d.catalog, d.resolve_adapter)
    top = ja.suggestions[0]
    assert top.left_columns == ["id"] and top.right_columns == ["user_id"]  # the CONFIGURED key leads
    assert top.cardinality == "1:N" and ja.warning and "fans out" in ja.warning


def test_transform_schema_contract_types_the_port_and_propagates_downstream():
    # A transform is untyped by default (its output columns need running Python). A user-declared
    # contract (config.outputSchema) must: (a) type the transform's OWN port verbatim, and (b) type
    # a DOWNSTREAM relational node too — via a schema-only typed stand-in relation, no code run.
    from hub.executors.schema import schema_for_graph
    from hub.models import Graph
    d = get_deps()

    def cols(nodes, edges):
        gph = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges})
        return schema_for_graph(gph, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)

    src = N("s", "source", {"uri": _uri("events")})
    edges = [E("s", "x"), E("x", "f")]

    # undeclared → transform + its downstream filter are untyped (null)
    plain = cols([src, N("x", "transform", {"source": "adhoc", "code": "def fn(r): return r"}),
                  N("f", "filter", {"predicate": "score > 0"})], edges)
    assert plain["x"] is None and plain["f"] is None

    # declared → transform types verbatim, and the downstream filter is typed FROM the contract
    contract = [{"name": "user_id", "type": "int", "capabilities": []},
                {"name": "score", "type": "float", "capabilities": []}]
    typed = cols([src, N("x", "transform", {"source": "adhoc", "code": "def fn(r): return r",
                                             "outputSchema": contract}),
                  N("f", "filter", {"predicate": "score > 0"})], edges)
    assert typed["x"] is not None and [c["name"] for c in typed["x"]] == ["user_id", "score"]
    assert typed["x"][0]["type"] == "int"  # the port shows the user's EXACT declared type
    assert typed["f"] is not None and [c["name"] for c in typed["f"]] == ["user_id", "score"]  # propagated


def test_transform_schema_contract_ignores_bypass_disabled_and_odd_names():
    # Adversarial-review fixes: (1) a BYPASSED declared code op passes its INPUT through — its declaration
    # must NOT apply (own port + downstream reflect the real input, not the contract). (2) a DISABLED
    # declared code op emits nothing → untyped (like a disabled relational node), not the declared cols.
    # (3) a declared column NAME with an embedded double-quote must not break the stand-in SQL.
    from hub.executors.schema import schema_for_graph
    from hub.models import Graph
    d = get_deps()

    def cols(x_data):
        nodes = [N("s", "source", {"uri": _uri("events")}),
                 {"id": "x", "type": "transform", "position": {"x": 0, "y": 0},
                  "data": {"title": "x", "config": {"source": "adhoc", "code": "def fn(r): return r",
                                                    "outputSchema": [{"name": "score", "type": "float", "capabilities": []}]},
                           **x_data}},
                 N("f", "filter", {"predicate": "id > 0"})]
        gph = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "x"), E("x", "f")]})
        return schema_for_graph(gph, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)

    ev_cols = ["id", "user_id", "event", "amount"]  # the real events schema
    # bypassed → declaration ignored; transform + downstream reflect the passthrough (input) columns
    byp = cols({"bypassed": True})
    assert [c["name"] for c in byp["x"]] == ev_cols and "score" not in [c["name"] for c in byp["x"]]
    assert [c["name"] for c in byp["f"]] == ev_cols  # downstream typed from the real passthrough
    # disabled → emits nothing → untyped (null) on its own port too, like any disabled node
    dis = cols({"disabled": True})
    assert dis["x"] is None and dis["f"] is None
    # a declared name with a literal double-quote must not raise — the stand-in escapes it, downstream types
    weird = Graph(**{"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("x", "transform", {"source": "adhoc", "code": "def fn(r): return r",
                             "outputSchema": [{"name": 'a"b', "type": "int", "capabilities": []}]}),
        N("f", "filter", {"predicate": "1=1"})], "edges": [E("s", "x"), E("x", "f")]})
    wc = schema_for_graph(weird, d.resolve_adapter, d.registry, d.node_builders, d.node_specs)
    assert [c["name"] for c in wc["x"]] == ['a"b']  # own port verbatim
    assert wc["f"] is not None and [c["name"] for c in wc["f"]] == ['a"b']  # propagated (stand-in didn't crash)


def test_transform_batch_format_pandas_and_arrow():
    # #1: map_batches can hand the whole batch to the cell as a pandas DataFrame or a pyarrow Table
    # (type-preserving), not just row-dicts — and the choice flows through resolve_config to the engine.
    from hub import db
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")  # has an int column 'amount'

    def total(fmt, code):
        gr = Graph(**{"id": "c", "version": 1, "nodes": [
            N("s", "source", {"uri": ev}),
            N("t", "transform", {"mode": "map_batches", "batchFormat": fmt, "code": code}),
        ], "edges": [E("s", "t")]})
        with db.run_scope():
            eng = BuildEngine(gr, d.resolve_adapter, d.registry, full=True,
                              node_specs=d.node_specs, node_builders=d.node_builders)
            return eng.relation("t").aggregate("count(*) AS n, sum(doubled) AS s").fetchone()

    # arrow needs no pandas → always exercised
    n_ar, s_ar = total("arrow", "def fn(t):\n    import pyarrow.compute as pc\n    return t.append_column('doubled', pc.multiply(t['id'], 2))")
    assert n_ar > 0
    # pandas is an OPTIONAL, user-declared runtime dep (not a core/dev dep) — skip if it isn't importable
    # OR is only partially installed (pyarrow's to_pandas reads pandas.__version__; a partial install with
    # no __version__ would fail there, which is an env problem, not a defect in this feature).
    pd = pytest.importorskip("pandas")
    if not getattr(pd, "__version__", None):
        pytest.skip("pandas is importable but not functional in this environment (no __version__)")
    n_pd, s_pd = total("pandas", "def fn(df):\n    df['doubled'] = df['id'] * 2\n    return df")
    assert n_pd > 0 and (n_pd, s_pd) == (n_ar, s_ar)  # both formats produce the same doubled column


def test_apply_batch_skip_drops_the_batch_not_the_schema():
    # fix: on_error='skip' must DROP a failed batch (return None → caller drops), NOT emit a wrong-schema
    # empty table — otherwise a good batch (which adds/renames a column) can't concat with it and the run aborts.
    import pyarrow as pa
    from hub.executors.engine import _apply_batch
    t = pa.table({"x": [1, 2]})
    assert _apply_batch(lambda tb: 1 / 0, t, "arrow", "skip", None) is None          # skip → dropped
    with pytest.raises(Exception):
        _apply_batch(lambda tb: 1 / 0, t, "arrow", "raise", None)                    # raise → error
    out = _apply_batch(lambda tb: tb.append_column("y", pa.array([9, 9])), t, "arrow", "raise", None)
    assert out.column_names == ["x", "y"]                                            # success → the output table


def test_sandbox_allows_pyarrow_compute_but_denies_file_io():
    # fix: the arrow batch format needs pyarrow core + compute, but the soft baseline stays I/O-free —
    # pyarrow's file-I/O submodules (fs/csv/parquet/dataset) must NOT be importable from an ad-hoc cell.
    from hub import sandbox
    assert sandbox._guarded_import("pyarrow") is not None
    assert sandbox._guarded_import("pyarrow.compute") is not None
    for m in ("pyarrow.fs", "pyarrow.csv", "pyarrow.parquet", "pyarrow.dataset", "pyarrow.feather"):
        with pytest.raises(ImportError):
            sandbox._guarded_import(m)


def test_ray_mapper_honors_batch_format():
    # dp_ray must run the SAME arrow-native path for a pandas/arrow map_batches, so Ray == local.
    import pyarrow as pa
    op = _load_dp_ray()._make_mapper({"mode": "map_batches", "batchFormat": "arrow",
                                      "code": "def fn(t):\n    import pyarrow.compute as pc\n    return t.append_column('y', pc.multiply(t['x'], 2))"})
    out = op(pa.table({"x": [1, 2, 3]}))
    assert out.column_names == ["x", "y"] and out["y"].to_pylist() == [2, 4, 6]


def test_assert_node_surfaces_violations_and_gates_the_run():
    # the data-quality gate: its relation IS the violating rows (predicate not TRUE → view data shows what
    # failed), and severity=error fails the run while warn records the count and continues.
    import time

    from hub import db
    from hub.compiler import compile_plan
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")  # 'id' is a non-negative int in the seed

    def graph(pred, sev="warn"):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            N("s", "source", {"uri": ev}),
            N("a", "assert", {"predicate": pred, "severity": sev}),
            N("f", "filter", {"predicate": "TRUE"}),
        ], "edges": [E("s", "a"), E("a", "f", "pass")]})

    # engine: assert relation = the VIOLATING rows (IS NOT TRUE catches false + null)
    with db.run_scope():
        eng = BuildEngine(graph("id >= 0"), d.resolve_adapter, d.registry, full=True,
                          node_specs=d.node_specs, node_builders=d.node_builders)
        total = int(eng.relation("s").aggregate("count(*) AS n").fetchone()[0])
        assert int(eng.relation("a", "out").aggregate("count(*) AS n").fetchone()[0]) == 0  # all satisfy → 0
        eng2 = BuildEngine(graph("id < 0"), d.resolve_adapter, d.registry, full=True,
                           node_specs=d.node_specs, node_builders=d.node_builders)
        assert int(eng2.relation("a", "out").aggregate("count(*) AS n").fetchone()[0]) == total  # none satisfy → all

    def run(pred, sev, target="f"):
        g = graph(pred, sev)
        st = d.runner.run(
            compile_plan(g, target, d.registry, d.node_specs), g, target, "local")
        for _ in range(200):
            s = d.runner.status(st.run_id)
            if s.status in ("done", "failed", "cancelled"):
                return s
            time.sleep(0.05)
        return s

    err = run("id < 0", "error")                    # every row violates + severity=error → run FAILS
    assert err.status == "failed" and "assert" in (err.error or "").lower()
    assert run("id < 0", "warn").status == "done"   # same violations but warn → run succeeds
    assert run("id >= 0", "error").status == "done"  # no violations → error severity passes

    # no predicate = ZERO violations (NOT "every row violates"): the relation is empty, not the passthrough
    with db.run_scope():
        eng3 = BuildEngine(graph(""), d.resolve_adapter, d.registry, full=True,
                           node_specs=d.node_specs, node_builders=d.node_builders)
        assert int(eng3.relation("a", "out").aggregate("count(*) AS n").fetchone()[0]) == 0
    assert run("", "error").status == "done"        # empty predicate + severity=error → must NOT fail the run
    # a predicate that can't evaluate (missing column): warn is non-blocking (run succeeds); error fails clean
    assert run("no_such_col > 0", "warn").status == "done"
    assert run("no_such_col > 0", "error").status == "failed"

    # A direct warn-severity Assert publishes both declared ports in NodeSpec order. Their independent
    # row counts and contents remain on RunOutput; the ambiguous scalar totalRows stays null.
    direct = run("id >= 5", "warn", "a")
    assert direct.status == "done" and direct.total_rows is None
    assert [output.port_id for output in direct.outputs] == ["pass", "out"]
    assert [output.rows for output in direct.outputs] == [total, 5]
    import duckdb
    pass_uri, violations_uri = [output.uri for output in direct.outputs]
    assert duckdb.connect(":memory:").execute(
        f"SELECT min(id) FROM read_parquet('{pass_uri}')").fetchone()[0] == 0
    assert duckdb.connect(":memory:").execute(
        f"SELECT max(id) FROM read_parquet('{violations_uri}')").fetchone()[0] == 4


def test_assert_named_ports_are_independently_previewable_and_profiled():
    graph = {"id": "assert-inspection-ports", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("a", "assert", {"predicate": "id >= 5", "severity": "warn"}),
    ], "edges": [E("s", "a")]}

    violations = client.post(
        "/api/run/preview",
        json={"graph": graph, "nodeId": "a", "portId": "out", "k": 10},
    )
    passed = client.post(
        "/api/run/preview",
        json={"graph": graph, "nodeId": "a", "portId": "pass", "k": 10},
    )
    assert violations.status_code == passed.status_code == 200
    assert [row["id"] for row in violations.json()["rows"]] == [0, 1, 2, 3, 4]
    assert len(passed.json()["rows"]) == 10

    violation_profile = client.post(
        "/api/run/profile", json={"graph": graph, "nodeId": "a", "portId": "out"})
    pass_profile = client.post(
        "/api/run/profile", json={"graph": graph, "nodeId": "a", "portId": "pass"})
    assert violation_profile.status_code == pass_profile.status_code == 200
    assert violation_profile.json()["rowCount"] == 5
    assert pass_profile.json()["rowCount"] > violation_profile.json()["rowCount"]

    schema = client.post("/api/graph/schema", json={"graph": graph})
    assert schema.status_code == 200, schema.text
    assert set(schema.json()["a"]) == {"out", "pass"}
    assert [column["name"] for column in schema.json()["a"]["out"]] == ["id", "user_id", "event", "amount"]
    assert schema.json()["a"]["pass"] == schema.json()["a"]["out"]


def test_assert_node_is_a_transparent_gate_before_a_write(tmp_path):
    # P0-DATA-01: assert must be usable INLINE as a real gate — its 'pass' output forwards EVERY row (so a
    # downstream write gets the data, not the violations), while severity=error still blocks the write.
    import time

    from hub import db
    from hub.compiler import compile_plan
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")  # 2000 rows, 'id' = 0..1999

    def graph(pred, sev):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            N("s", "source", {"uri": ev}),
            N("a", "assert", {"predicate": pred, "severity": sev}),
            N("wr", "write", {"filename": str(tmp_path / "asserted.parquet")}),
        ], "edges": [E("s", "a"), E("a", "wr", "pass")]})  # write reads the PASSTHROUGH handle

    # the two ports are distinct: 'out' (default) = violations, 'pass' = every input row
    with db.run_scope():
        eng = BuildEngine(graph("id >= 5", "warn"), d.resolve_adapter, d.registry, full=True,
                          node_specs=d.node_specs, node_builders=d.node_builders)
        total = int(eng.relation("s").aggregate("count(*) AS n").fetchone()[0])
        viol = int(eng.relation("a", "out").aggregate("count(*) AS n").fetchone()[0])  # violations
        passed = int(eng.relation("a", "pass").aggregate("count(*) AS n").fetchone()[0])  # passthrough
        assert 0 < viol < total and passed == total  # 'pass' is EVERY row, not the 5 violations

    def run(pred, sev):
        g = graph(pred, sev)
        st = d.runner.run(compile_plan(g, "wr", d.registry, d.node_specs), g, "wr", "local")
        for _ in range(200):
            s = d.runner.status(st.run_id)
            if s.status in ("done", "failed", "cancelled"):
                return s
            time.sleep(0.05)
        return s

    warned = run("id >= 5", "warn")   # warn → run succeeds and writes ALL rows (via 'pass'), not violations
    from hub.run_outputs import sole_output
    assert warned.status == "done" and warned.total_rows == total
    assert sole_output(warned, committed=True) is not None
    errored = run("id >= 5", "error")  # error → run fails at the assert, BEFORE the write commits
    assert errored.status == "failed" and "assert" in (errored.error or "").lower()
    assert all(output.uri is None for output in errored.outputs)  # no sink side effect


def test_window_fill_unnest_nodes(tmp_path):
    # the data-cleaning built-ins: window (add a partitioned analytic column), fill (impute nulls),
    # unnest (explode a list column → one row per element).
    import duckdb

    from hub import db
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    d = get_deps()

    def eng(graph):
        return BuildEngine(graph, d.resolve_adapter, d.registry, full=True,
                           node_specs=d.node_specs, node_builders=d.node_builders)

    # window: row_number() per user_id ordered by amount
    pw = str(tmp_path / "w.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1,10.0),(1,5.0),(2,7.0)) t(user_id,amount)) TO '{pw}' (FORMAT PARQUET)")
    gw = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pw}),
        N("w", "window", {"expr": "row_number()", "partitionBy": "user_id", "orderBy": "amount", "as": "rn"})],
        "edges": [E("s", "w")]})
    with db.run_scope():
        t = eng(gw).relation("w").order("user_id, amount").to_arrow_table()
        assert "rn" in t.column_names
        assert list(zip(t.column("user_id").to_pylist(), t.column("rn").to_pylist())) == [(1, 1), (1, 2), (2, 1)]

    # fill: zero-fill + mean-fill the nulls in x
    pf = str(tmp_path / "f.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1,10),(2,NULL),(3,NULL)) t(id,x)) TO '{pf}' (FORMAT PARQUET)")
    with db.run_scope():
        gz = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pf}),
            N("f", "fill", {"columns": "x", "method": "zero"})], "edges": [E("s", "f")]})
        assert eng(gz).relation("f").order("id").to_arrow_table().column("x").to_pylist() == [10, 0, 0]
        gm = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pf}),
            N("f", "fill", {"columns": "x", "method": "mean"})], "edges": [E("s", "f")]})
        assert eng(gm).relation("f").order("id").to_arrow_table().column("x").to_pylist() == [10, 10, 10]

    # unnest: explode the list column → 3 + 1 = 4 rows, id repeated per element
    pu = str(tmp_path / "u.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1,[10,20,30]),(2,[40])) t(id,tags)) TO '{pu}' (FORMAT PARQUET)")
    gu = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pu}),
        N("u", "unnest", {"column": "tags"})], "edges": [E("s", "u")]})
    with db.run_scope():
        t = eng(gu).relation("u").order("id, tags").to_arrow_table()
        assert t.num_rows == 4 and sorted(t.column("tags").to_pylist()) == [10, 20, 30, 40]
        assert t.column("id").to_pylist().count(1) == 3  # id 1 repeated once per list element

    # window is a blocking op (sorts/partitions the full input) → placement counts its working set
    from hub import estimate as _est
    assert _est.is_blocking("window") and not _est.is_blocking("fill") and not _est.is_blocking("unnest")

    # a whitespace-only `as` must NOT produce an empty identifier (AS "") → crash; it defaults to "window"
    pblank = str(tmp_path / "wb.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1,2.0)) t(user_id,amount)) TO '{pblank}' (FORMAT PARQUET)")
    gb = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pblank}),
        N("w", "window", {"expr": "row_number()", "as": "   "})], "edges": [E("s", "w")]})
    with db.run_scope():
        assert "window" in eng(gb).relation("w").to_arrow_table().column_names

    # A window aggregate cannot be exact on a bounded prefix, and preview must not rebuild an unbounded
    # input. The interactive engine refuses it; full execution above remains exact.
    pbig = str(tmp_path / "big.parquet")
    duckdb.connect().execute(f"COPY (SELECT i AS x FROM range(1,101) t(i)) TO '{pbig}' (FORMAT PARQUET)")
    gwin = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pbig}),
        N("w", "window", {"expr": "sum(x)", "as": "total"})], "edges": [E("s", "w")]})
    with db.run_scope():
        prev = BuildEngine(gwin, d.resolve_adapter, d.registry, sample_k=10, full=False,
                           node_specs=d.node_specs, node_builders=d.node_builders)
        from hub.executors.engine import NotPreviewable
        with pytest.raises(NotPreviewable, match="full pass"):
            prev.relation("w")


def test_pivot_unpivot_nodes(tmp_path):
    # ARC10 reshape built-ins: pivot (long → wide, out-of-core aggregate) + unpivot (wide → long),
    # lowering to DuckDB PIVOT / UNPIVOT.
    import duckdb
    import pytest

    from hub import db
    from hub.executors.engine import BuildEngine, NotPreviewable
    from hub.models import Graph
    d = get_deps()

    def eng(graph, **kw):
        return BuildEngine(graph, d.resolve_adapter, d.registry, node_specs=d.node_specs,
                           node_builders=d.node_builders, **kw)

    # pivot: long (uid, cat, amt) → wide (uid, a, b) with sum(amt); u2 has no 'b' → NULL cell
    pl = str(tmp_path / "long.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES ('u1','a',10),('u1','b',20),('u2','a',5)) t(uid,cat,amt)) TO '{pl}' (FORMAT PARQUET)")
    gp = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pl}),
        N("p", "pivot", {"pivotOn": "cat", "using": "sum(amt)", "groupBy": "uid"})], "edges": [E("s", "p")]})
    with db.run_scope():
        t = eng(gp, full=True).relation("p").order("uid").to_arrow_table()
        assert set(t.column_names) == {"uid", "a", "b"}
        assert {r["uid"]: (r["a"], r["b"]) for r in t.to_pylist()} == {"u1": (10, 20), "u2": (5, None)}

    # pivot's output columns are the DISTINCT values of `cat` → a sample would produce wrong columns; refuse
    with db.run_scope(), pytest.raises(NotPreviewable):
        eng(gp, full=False, sample_k=1).relation("p")

    # unpivot: wide (uid, a, b) → long (uid, name, value); previewable (row-wise, no aggregate). Data has a
    # NULL cell in EACH row → default (includeNulls) must keep every (row × column) cell — DuckDB's bare
    # UNPIVOT would silently drop the NULL cells (and a fully-NULL row would vanish entirely).
    pw = str(tmp_path / "wide.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES ('u1',10,NULL),('u2',NULL,5)) t(uid,a,b)) TO '{pw}' (FORMAT PARQUET)")
    gu = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pw}),
        N("u", "unpivot", {"columns": "a, b", "nameColumn": "name", "valueColumn": "value"})], "edges": [E("s", "u")]})
    with db.run_scope():
        t2 = eng(gu, full=True).relation("u").order("uid, name").to_arrow_table()
        assert set(t2.column_names) == {"uid", "name", "value"}
        assert sorted((r["uid"], r["name"], r["value"]) for r in t2.to_pylist()) == \
            [("u1", "a", 10), ("u1", "b", None), ("u2", "a", None), ("u2", "b", 5)]  # NULLs kept, no row loss

    # includeNulls=false → drop the NULL cells (the compact form)
    gd = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": pw}),
        N("u", "unpivot", {"columns": "a, b", "includeNulls": False})], "edges": [E("s", "u")]})
    with db.run_scope():
        t3 = eng(gd, full=True).relation("u").to_arrow_table()
        assert sorted((r["uid"], r["name"], r["value"]) for r in t3.to_pylist()) == [("u1", "a", 10), ("u2", "b", 5)]

    # pivot's columns are data-dependent → its schema_only port is UNTYPED (not a misleading [uid] subset)
    from hub.executors.schema import schema_for_graph
    ports = schema_for_graph(gp, d.resolve_adapter, d.registry, node_builders=d.node_builders, node_specs=d.node_specs)
    assert ports.get("p") is None, f"pivot port should be untyped (data-dependent columns), got {ports.get('p')}"

    # pivot aggregates the full input → blocking (drives region placement); unpivot is row-wise → not
    from hub import estimate as _est
    assert _est.is_blocking("pivot") and not _est.is_blocking("unpivot")


def test_failed_run_attributes_error_to_a_node_with_a_hint():
    # a failed run names WHERE it broke (per-node error) + WHY (a fix hint for common error classes),
    # not just a global banner. A bad column reference → the target node carries the error + hint.
    import time

    from hub.compiler import compile_plan
    from hub.models import Graph
    from hub.plugins.runner import _diagnose
    d = get_deps()
    ev = _uri("events")
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": ev}), N("sel", "select", {"select": "no_such_col"})], "edges": [E("s", "sel")]})
    st = d.runner.run(compile_plan(g, "sel", d.registry, d.node_specs), g, "sel", "local")
    for _ in range(200):
        s = d.runner.status(st.run_id)
        if s.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert s.status == "failed"
    assert (s.error or "").startswith("at '")  # the banner names WHERE it broke, not just a bare error
    assert "Hint:" in (s.error or "")          # ...and WHY / how to fix (the diagnostic hint)
    # the error is attributed to a specific node (which one may shift under source pushdown fusing the
    # projection into the scan — the message + the amber column warnings still point at the bad reference)
    failed = next((p for p in s.per_node if p.status == "failed" and p.error), None)
    assert failed is not None and "no_such_col" in failed.error and "Hint:" in failed.error
    # the diagnostic maps recognized error classes to a hint, and stays silent (None) on unknown ones
    assert "column references" in (_diagnose("Binder Error: Referenced column x not found") or "")
    assert _diagnose("Conversion Error: Could not convert") is not None
    assert _diagnose("some unrecognized failure") is None
    # a binder error that is NOT an unknown-column (function/arg resolution) gets the general hint, NOT the
    # "check column references" one that would send the user down the wrong path
    fn_hint = _diagnose("Binder Error: No function matches the given name and argument types")
    assert fn_hint is not None and "column references" not in fn_hint


def test_estimate_sizes_is_conservative_and_honest():
    # the per-node size estimate: conservative (never under-estimate), honest (unknown → None, not a
    # fabricated number), and measured-actuals override. Feeds placement + the confirm-gate + a UI hint.
    from hub.estimate import estimate_sizes
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")  # a real seeded source with a known row count

    def est(nodes, edges, **kw):
        return estimate_sizes(Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges}), d.resolve_adapter, **kw)

    nodes = [N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "amount > 0"}),
             N("p", "sample", {"n": 100}), N("a", "aggregate", {"groupBy": "user_id", "aggs": "count(*) AS n"})]
    edges = [E("s", "f"), E("f", "p"), E("p", "a")]
    e = est(nodes, edges)
    assert e["s"].rows is not None and e["s"].rows > 0 and e["s"].confidence == "exact"
    assert e["f"].rows == e["s"].rows and e["f"].confidence == "bounded"   # filter = input (all-qualify upper bound)
    assert e["p"].rows == min(100, e["s"].rows) and e["p"].confidence == "bounded"  # sample ≤ n
    assert e["a"].rows is None and e["a"].confidence == "unknown" and e["a"].blocking  # aggregate collapse: unknown
    assert e["s"].bytes and e["s"].bytes > 0  # bytes scale with rows

    # a measured actual overrides the estimate and propagates downstream
    e2 = est(nodes, edges, actuals={"f": 42})
    assert e2["f"].rows == 42 and e2["f"].confidence == "exact"
    assert e2["p"].rows == 42  # min(100, 42)

    # a code op is honestly unknown (not a fabricated passthrough); a bypassed node passes input through
    e3 = est([N("s", "source", {"uri": ev}), N("t", "transform", {"code": "def fn(r): return r"})], [E("s", "t")])
    assert e3["t"].rows is None and e3["t"].confidence == "unknown"
    byp = {"id": "b", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"title": "b", "config": {"predicate": "x"}, "bypassed": True}}
    e4 = est([N("s", "source", {"uri": ev}), byp], [E("s", "b")])
    assert e4["b"].rows == e4["s"].rows  # bypassed → passthrough


def test_estimate_propagates_measured_vector_width_downstream():
    # estimator-schema-derived: a column's MEASURED width (a real float[512] embedding, from the source
    # schema) must survive downstream through row-preserving ops — not collapse to the coarse display-type
    # width ("list" ≈ 128B) at the filter, which would mis-size a downstream region ~30x small and misplace
    # it local. The estimator now propagates the input's width (conservative max) for pass-through ops.
    from hub.estimate import estimate_sizes
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")  # a real source (for the row count); the widths come from the provided schema
    schemas = {"s": [{"name": "id", "type": "int"}, {"name": "emb", "type": "float[512]"}],
               "f": [{"name": "id", "type": "int"}, {"name": "emb", "type": "list"}]}  # coarse display at f
    nodes = [N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "id >= 0"})]
    e = estimate_sizes(Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "f")]}),
                       d.resolve_adapter, schemas=schemas)
    sw = e["s"].bytes / e["s"].rows
    assert abs(sw - (8 + 512 * 8)) < 2, f"source width should MEASURE the float[512] vector, got {sw}"
    fw = e["f"].bytes / e["f"].rows
    assert abs(fw - sw) < 2, f"filter must propagate the measured width {sw}, not the coarse display width {fw}"
    assert fw > 1000  # sanity: not collapsed to the ~128B coarse 'list' width


def test_estimate_actual_path_keeps_measured_width():
    # A node with a measured `actual` row count must STILL carry the sharpened (probed/propagated) per-row
    # width — the earlier fast-path used the coarse display width, so `exact_rows x coarse_width` was a hard
    # UNDER-estimate, and a source-with-an-actual skipped vector-width probing entirely (defeating the whole
    # mechanism on the 2nd, ground-truth run). Now the width is measured once, before the actual short-circuit.
    from hub.estimate import estimate_sizes
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")
    schemas = {"s": [{"name": "id", "type": "int"}, {"name": "emb", "type": "float[512]"}],
               "f": [{"name": "id", "type": "int"}, {"name": "emb", "type": "list"}]}
    nodes = [N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "id >= 0"})]
    g = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "f")]})
    # source has a ground-truth actual → it must NOT skip vector probing.
    e = estimate_sizes(g, d.resolve_adapter, schemas=schemas, actuals={"s": 1000})
    sw = e["s"].bytes / e["s"].rows
    assert e["s"].rows == 1000                                # actual wins the row count
    assert abs(sw - (8 + 512 * 8)) < 2, f"actual source must still MEASURE the vector width, got {sw}"
    fw = e["f"].bytes / e["f"].rows
    assert abs(fw - sw) < 2, f"measured width must survive downstream of an already-run node, got {fw}"


def test_row_width_accounts_for_vector_and_list_columns():
    # a fixed-size embedding (float[1024]) must not be scored as its scalar base (float=8B) — that's a
    # ~1000x undercount that mis-sizes a vector working set as tiny and mis-places it local.
    from hub.estimate import _col_width, _row_width
    assert _col_width("float") == 8
    assert _col_width("float[1024]") == 1024 * 8   # the whole vector, not one element
    assert _col_width("int[3]") == 3 * 8
    assert _col_width("varchar[]") > 24            # variable-length list of strings > a single string
    assert _col_width("struct") >= 64              # nested value, coarse
    wide = _row_width([{"name": "id", "type": "int"}, {"name": "emb", "type": "float[1024]"}])
    assert wide >= 1024 * 8 and wide > _row_width([{"name": "id", "type": "int"}]) * 100


def test_source_metadata_count_is_memoized_by_fingerprint():
    # An adapter's explicit metadata-only row count is memoized by fingerprint. A changed fingerprint
    # recounts; a transient metadata failure is NOT cached. Ordinary count() is never used here because
    # it may full-scan a source during interactive preflight.
    from hub import estimate
    estimate._COUNT_CACHE.clear()
    calls = {"n": 0}
    fp = {"v": "fp1"}
    class Stub:
        def fingerprint(self, uri): return fp["v"]
        def metadata_count(self, uri): calls["n"] += 1; return 123
    resolve = lambda uri: Stub()  # noqa: E731
    assert estimate._counted(resolve, "s3://x/a.csv") == 123
    assert estimate._counted(resolve, "s3://x/a.csv") == 123
    assert calls["n"] == 1          # 2nd call served from cache — no re-scan
    fp["v"] = "fp2"                 # the file changed → new fingerprint → recount
    assert estimate._counted(resolve, "s3://x/a.csv") == 123
    assert calls["n"] == 2
    estimate._COUNT_CACHE.clear()
    fail = {"boom": True}
    class Flaky:
        def fingerprint(self, uri): return "fpf"
        def metadata_count(self, uri):
            if fail["boom"]: raise RuntimeError("io")
            return 7
    assert estimate._counted(lambda uri: Flaky(), "x.csv") is None
    fail["boom"] = False
    assert estimate._counted(lambda uri: Flaky(), "x.csv") == 7  # retried, not stuck on a cached None


def test_source_width_probes_wide_list_columns(tmp_path):
    # Parquet stores a fixed-size embedding as a VARIABLE list, so DuckDB reads it as bare 'float[]'
    # (dimension lost on disk). The flat _col_width assumes 16 elems → a 512-wide embedding is undercounted
    # ~128x and the byte confirm-gate misses the multi-GB table it targets. _source_width PROBES the real
    # avg element count so the per-row bytes (and thus the gate's max-over-cone) are right.
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.estimate import _col_width, _source_width, estimate_sizes
    from hub.models import Graph
    d = get_deps()
    p = str(tmp_path / "emb.parquet")
    pq.write_table(pa.table({"id": list(range(64)),
                             "emb": pa.array([[1.0] * 512] * 64, type=pa.list_(pa.float32(), 512))}), p)
    cols = [{"name": "id", "type": "int"}, {"name": "emb", "type": "float[]"}]
    assert _col_width("float[]") == 128                              # the flat (undercounting) default
    w = _source_width(d.resolve_adapter, p, cols)
    assert w >= 512 * 4, f"probe should score the ~512-wide embedding, got {w}"   # ~2056, not 8+128
    # end-to-end: the source's byte estimate reflects the probed width (feeds the byte confirm-gate)
    g = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": p})], "edges": []})
    est = estimate_sizes(g, d.resolve_adapter, schemas={"s": cols})
    assert est["s"].bytes and est["s"].bytes >= 64 * 512 * 4          # 64 rows × ~2KB, not 64 × 136


def test_latest_actuals_feeds_estimator_only_for_latest_nodes():
    # the last successful run's per-node rows sharpen the estimate for a not-yet-run downstream node,
    # but only while the producing node is still 'latest' (an edited/'stale' node's old count would lie).
    from hub import metadb
    from hub.routers.runs import _actuals_for
    from hub.models import Graph, RunOutput
    cid = "cv_actuals"
    with metadb.session() as s:
        if s.get(metadb.Canvas, cid) is None:
            s.add(metadb.Canvas(id=cid, owner_id="local", name="a", doc="{}"))
    # realistic: the per_node breakdown leaves a lazy relation's own rows null, so the target count comes
    # from RunRecord.rows (=total_rows) — latest_actuals must read that, not only per_node.
    output = RunOutput(
        node_id="j", port_id="out", wire="dataset", publication_kind="result",
        outcome="committed", uri="/tmp/cv-actuals-j.parquet", rows=4321,
    )
    metadb.record_run(
        cid, "j", "run", "done", rows=4321, outputs=[output.model_dump()],
        per_node=[{"node_id": "j", "rows": None, "status": "done"}],
    )
    assert metadb.latest_actuals(cid) == {"j": 4321}
    g_latest = Graph(id=cid, version=1, nodes=[N("j", "join", {})], edges=[])
    g_latest.nodes[0].data["status"] = "latest"
    assert _actuals_for(g_latest, get_deps()) == {"j": 4321}
    g_stale = Graph(id=cid, version=1, nodes=[N("j", "join", {})], edges=[])
    g_stale.nodes[0].data["status"] = "stale"
    assert _actuals_for(g_stale, get_deps()) == {}  # edited node → don't trust its old count


def test_cost_based_placement_routes_a_heavy_region_and_is_a_noop_without_a_backend():
    # Phase B: a blocking region whose estimated working set exceeds the local budget (here: unknown,
    # because its input is an opaque transform) 'wants' a bigger backend. With none registered it stays
    # on the default (no behavior change); with one advertising the memory, the heavy region routes to it.
    from hub.models import Graph, ResourceSpec
    from hub.placement import satisfies
    d = get_deps()
    ev = _uri("events")
    nodes = [N("s", "source", {"uri": ev}), N("t", "transform", {"code": "def fn(r): return r"}),
             N("a", "aggregate", {"groupBy": "user_id", "aggs": "count(*) AS n"})]
    graph = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "t"), E("t", "a")]})

    # (1) no cluster backend → everything on the default → single fused region → base runner (unchanged)
    assert all(r.backend == "default" for r in d.controller.plan(graph, "a"))

    # (2) a backend advertising big memory → the heavy aggregate region routes to it
    class _Big:
        name = "big"

        def place(self, requires):
            return "w1" if satisfies(ResourceSpec(mem="1000GB"), requires) else None

    d.runners.insert(0, _Big())
    try:
        regions = d.controller.plan(graph, "a")
        assert next(r for r in regions if r.output_node == "a").backend == "big"
        assert next(r for r in regions if r.output_node == "t").backend == "default"  # light region stays local
    finally:
        d.runners[:] = [r for r in d.runners if getattr(r, "name", "") != "big"]


def test_cost_placement_respects_a_manual_mem_pin():
    # decision: a manual config.requires.mem is AUTHORITATIVE — the cost estimator must not raise it.
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")
    agg = {"id": "a", "type": "aggregate", "position": {"x": 0, "y": 0},
           "data": {"title": "a", "config": {"groupBy": "user_id", "aggs": "count(*) AS n", "requires": {"mem": "2GB"}}}}
    nodes = [N("s", "source", {"uri": ev}), N("t", "transform", {"code": "def fn(r): return r"}), agg]
    graph = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "t"), E("t", "a")]})
    assert "a" not in d.controller._cost_requires(graph, "a")  # pinned mem → estimator adds nothing
    agg["data"]["config"].pop("requires")                       # without the pin it WOULD add a mem floor
    graph2 = Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": [E("s", "t"), E("t", "a")]})
    assert "a" in d.controller._cost_requires(graph2, "a")


def test_graph_plan_endpoint():
    # #4: the run-plan preview — a plain graph is one 'default' region; a checkpoint splits it into two,
    # and the upstream boundary materializes to the local tier. Makes placement + tiering visible.
    ev = _uri("events")
    plain = {"graph": {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "amount > 0"})],
        "edges": [E("s", "f")]}, "targetNodeId": "f"}
    r1 = client.post("/api/graph/plan", json=plain).json()
    assert len(r1["regions"]) == 1 and r1["regions"][0]["backend"] == "default"

    ckpt = {"graph": {"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": ev}),
        {"id": "f", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"title": "f", "config": {"predicate": "amount > 0", "checkpoint": True}}},
        N("g", "filter", {"predicate": "amount > 1"})],
        "edges": [E("s", "f"), E("f", "g")]}, "targetNodeId": "g"}
    r2 = client.post("/api/graph/plan", json=ckpt).json()
    assert len(r2["regions"]) == 2
    assert next(x for x in r2["regions"] if x["outputNode"] == "f")["tier"] == "local"  # boundary → local


def test_run_plan_flags_unsatisfied_resource_requirement():
    # C: a node pins a resource (GPU) no registered backend provides → the run-plan flags it "unsatisfied"
    # (pre-flight: it will run local, which may lack it). A plain graph has nothing unsatisfied.
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")
    gpu = {"id": "a", "type": "aggregate", "position": {"x": 0, "y": 0},
           "data": {"title": "a", "config": {"groupBy": "user_id", "aggs": "count(*) AS n", "requires": {"gpu": 4, "gpuType": "a100"}}}}
    g = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}), gpu], "edges": [E("s", "a")]})
    plan = d.controller.plan_summary(g, "a")
    assert any(r.get("unsatisfied") and "a100" in (r.get("requires") or "") for r in plan)
    # the pre-flight tells you WHAT is available, not just "no backend provides it"
    assert all(r.get("available") for r in plan if r.get("unsatisfied"))

    # _available_summary reflects whatever placement backends advertise via workers()
    from hub.models import ResourceSpec, WorkerInfo
    class _FakeGPUBackend:
        def workers(self):
            return [WorkerInfo(id="g", capacity=ResourceSpec(gpu=8, gpu_type="a100", labels={"engine": "ray"}), state="idle")]
    d.runners.append(_FakeGPUBackend())
    try:
        summ = d.controller._available_summary()
        assert "8×a100" in summ and "engine=ray" in summ
    finally:
        d.runners.pop()

    g2 = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "id > 0"})], "edges": [E("s", "f")]})
    assert all(not r.get("unsatisfied") for r in d.controller.plan_summary(g2, "f"))

    # a GPU node on a DISCONNECTED pipeline must NOT flag the CPU-only target's run — the pre-flight is
    # scoped to the target's upstream cone, not the whole canvas.
    g3 = Graph(**{"id": "c", "version": 1, "nodes": [
        N("s", "source", {"uri": ev}), N("f", "filter", {"predicate": "id > 0"}),
        N("s2", "source", {"uri": ev}), gpu], "edges": [E("s", "f"), E("s2", "a")]})
    assert all(not r.get("unsatisfied") for r in d.controller.plan_summary(g3, "f"))

    # a mem-only requirement is SOFT (the local out-of-core engine spills) → never "no backend provides it"
    mem = {"id": "m", "type": "aggregate", "position": {"x": 0, "y": 0},
           "data": {"title": "m", "config": {"groupBy": "user_id", "aggs": "count(*) AS n", "requires": {"mem": "999GB"}}}}
    g4 = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}), mem], "edges": [E("s", "m")]})
    assert all(not r.get("unsatisfied") for r in d.controller.plan_summary(g4, "m"))


def test_graph_estimate_endpoint():
    # the size-hint endpoint: an exact count for a real source, honest confidence label
    ev = _uri("events")
    body = {"graph": {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev})], "edges": []}}
    r = client.post("/api/graph/estimate", json=body).json()
    assert r["s"]["rows"] is not None and r["s"]["rows"] > 0 and r["s"]["confidence"] == "exact"


def test_move_tier_copies_a_region_parquet():
    # C3 auto data-movement: copy a materialized region parquet from one location to another (used to
    # reuse a prior run's result on a different tier instead of recomputing). Content must be preserved.
    import os as _os
    import tempfile
    import threading
    from hub import db
    from hub.tiers import Tier
    d = get_deps()
    with tempfile.TemporaryDirectory() as tmp:
        src = _os.path.join(tmp, "src.parquet")
        dst = _os.path.join(tmp, "sub", "dst.parquet")
        with db.run_scope():
            db.conn().sql("SELECT * FROM (VALUES (1,'a'),(2,'b')) t(id,name)").write_parquet(src)
        d.controller._move_tier(src, dst, Tier("local", _os.path.join(tmp, "sub"), 0), threading.Event())
        assert _os.path.exists(dst)
        with db.run_scope():
            assert db.conn().read_parquet(dst).aggregate("count(*) AS n").fetchone()[0] == 2


def test_controller_region_cancel_fences_staged_handoff(tmp_path, monkeypatch):
    # Region handoffs used to call relation.write_parquet(out_uri) directly. Land cancellation exactly
    # after the replacement parquet is staged and prove the controller never publishes the handoff.
    import glob
    import threading

    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region

    workspace = tmp_path / "region-ws"
    data_dir = tmp_path / "region-data"
    workspace.mkdir()
    data_dir.mkdir()
    d = Deps(str(workspace), str(data_dir))
    source = _seq_parquet(tmp_path, n=10)
    graph = Graph(**{"id": "region_cancel", "version": 1,
                     "nodes": [N("s", "source", {"uri": source})], "edges": []})
    region = Region(id="r", node_ids={"s"}, output_node="s", backend="default", worker=None,
                    requires=ResourceSpec(), cut_inputs=[])
    subgraph = d.controller._subgraph(graph, region, {})
    key = d.runner._plan_hash(subgraph, "s")
    output = str(workspace / "regions" / f"r_{key}.parquet")
    cancel = threading.Event()
    run_id = "run_region_cancel_fence"
    d.controller._cancel[run_id] = cancel
    resolve = d.resolve_adapter
    real = resolve(output)
    checks = 0

    class _FenceAdapter:
        def __getattr__(self, name):
            return getattr(real, name)

        def write(self, uri, rel, mode="overwrite", partition_by=None, cancelled=None):
            nonlocal checks

            def fence():
                nonlocal checks
                checks += 1
                if checks == 3:
                    assert glob.glob(output + ".tmp-*"), "region handoff did not reach its publish fence"
                    cancel.set()
                return bool(cancelled and cancelled())

            return real.write(uri, rel, mode, partition_by=partition_by, cancelled=fence)

    monkeypatch.setattr(d, "resolve_adapter", lambda uri: _FenceAdapter() if uri == output else resolve(uri))
    monkeypatch.setattr(d.controller, "_backend_runner", lambda *args, **kwargs: d.runner)
    try:
        with pytest.raises(RuntimeError, match="cancelled before output commit"):
            d.controller._materialize(run_id, graph, region, {}, [region])
    finally:
        d.controller._cancel.pop(run_id, None)
    assert cancel.is_set() and not os.path.exists(output)
    assert not glob.glob(output + ".tmp-*"), "cancelled region staging file leaked"


def test_controller_does_not_evict_unowned_region_artifacts(tmp_path):
    # Region files can remain referenced by the result cache, catalog, another hub instance, or a running
    # reader. A previous newest-500 cleanup guessed ownership from mtime and deleted valid data. Until an
    # ownership/reference ledger exists, retaining an artifact is safer than corrupting a published result.
    import os as _os

    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region

    workspace = tmp_path / "region-retention-ws"
    data_dir = tmp_path / "region-retention-data"
    workspace.mkdir()
    data_dir.mkdir()
    regions = workspace / "regions"
    regions.mkdir()
    artifacts = []
    for i in range(501):
        artifact = regions / f"existing_{i:03d}.parquet"
        artifact.write_bytes(b"published")
        artifacts.append(artifact)
    _os.utime(artifacts[0], (1, 1))  # the former mtime-based pruning always selected this valid artifact

    d = Deps(str(workspace), str(data_dir))
    source = _seq_parquet(tmp_path, n=10)
    graph = Graph(**{"id": "region_retention", "version": 1,
                     "nodes": [N("s", "source", {"uri": source})], "edges": []})
    region = Region(id="r", node_ids={"s"}, output_node="s", backend="default", worker=None,
                    requires=ResourceSpec(), cut_inputs=[])

    output = d.controller._materialize("run_region_retention", graph, region, {}, [region])

    assert all(artifact.exists() for artifact in artifacts)
    assert _os.path.exists(output)


def test_row_estimate_uses_the_shared_estimator():
    # the confirm-gate now shares hub.estimate: the "volume" of a source→sample(100) run is the source
    # count (max across the cone) + estimated bytes, and an all-unknown run is (None, None) (→ err toward
    # confirm), not fabricated.
    from hub.routers.runs import _cone_size
    from hub.models import Graph
    d = get_deps()
    ev = _uri("events")
    g1 = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}), N("p", "sample", {"n": 100})], "edges": [E("s", "p")]})
    rows, byts, sizes = _cone_size(g1, "p", d)
    assert rows == d.resolve_adapter(ev).count(ev)  # max over cone = source count
    assert byts is not None and byts > 0            # bytes estimate available whenever rows is
    assert sizes and "s" in sizes                   # full per-node estimate returned for placement reuse
    g2 = Graph(**{"id": "c", "version": 1, "nodes": [N("t", "transform", {"code": "def fn(r): return r"})], "edges": []})
    r2, b2, _ = _cone_size(g2, "t", d)
    assert r2 is None and b2 is None  # all unknown → no fabricated rows/bytes number


def test_storage_tier_selection():
    # Phase C: pick the cheapest tier reachable by BOTH producer and consumer. local→local = local;
    # a remote party forces the shared object store; no object store + a remote party = no common tier.
    from hub.tiers import Tier, pick_tier, backend_reach, LOCAL_REACH, REMOTE_REACH
    tm = {"local": Tier("local", "/x", 0), "object": Tier("object", "s3://b/r", 10)}
    assert pick_tier(tm, [LOCAL_REACH, LOCAL_REACH]).name == "local"        # local handoff → local
    assert pick_tier(tm, [LOCAL_REACH, REMOTE_REACH]).name == "object"      # remote consumer → object
    assert pick_tier({"local": tm["local"]}, [LOCAL_REACH, REMOTE_REACH]) is None  # no shared tier

    class _B:
        name = "b"
    assert backend_reach(_B(), True) == LOCAL_REACH                        # default → local + object
    assert backend_reach(_B(), False) == REMOTE_REACH                      # assumed-remote → object only

    class _C:
        name = "c"

        def reachable_tiers(self):
            return ("local",)
    assert backend_reach(_C(), False) == ("local",)                        # a backend can override its reach


def test_boundary_tier_is_local_for_default_handoff_object_for_remote():
    from hub.planner import Region
    from hub.models import ResourceSpec
    d = get_deps()
    prod = Region(id="r_x", node_ids={"x"}, output_node="x", backend="default", worker=None, requires=ResourceSpec(), cut_inputs=[])
    cons_local = Region(id="r_y", node_ids={"y"}, output_node="y", backend="default", worker=None, requires=ResourceSpec(), cut_inputs=[("x", None, "y", None)])
    assert d.controller._boundary_tier(prod, [prod, cons_local]).name == "local"  # default→default stays local

    cons_remote = Region(id="r_z", node_ids={"z"}, output_node="z", backend="big", worker="w1", requires=ResourceSpec(), cut_inputs=[("x", None, "z", None)])
    old = os.environ.get("DP_STORAGE_URL")
    os.environ["DP_STORAGE_URL"] = "s3://bucket/out"
    try:
        assert d.controller._boundary_tier(prod, [prod, cons_remote]).name == "object"  # remote → object store
    finally:
        os.environ.pop("DP_STORAGE_URL", None) if old is None else os.environ.__setitem__("DP_STORAGE_URL", old)


def test_materialize_refuses_a_handoff_with_no_shared_tier(monkeypatch):
    # dist-refuse-split-no-tier: when a region hands off to a backend with NO shared reachable tier,
    # materializing to local would silently route data to a dead end (a remote backend can't read it).
    # The controller now FAILS FAST with an actionable message instead of the old warn-and-use-local.
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region
    d = get_deps()
    # (a) FAITHFUL: the real backend_reach→pick_tier chain actually returns None for a remote (object-only)
    # consumer with NO object store configured — this is the condition the fail-fast guards.
    old = os.environ.pop("DP_STORAGE_URL", None)
    try:
        prod = Region(id="rp", node_ids={"s"}, output_node="s", backend="default", worker=None,
                      requires=ResourceSpec(), cut_inputs=[])
        cons = Region(id="rc", node_ids={"z"}, output_node="z", backend="big", worker="w1",
                      requires=ResourceSpec(), cut_inputs=[("s", None, "z", None)])
        assert d.controller._boundary_tier(prod, [prod, cons]) is None  # object-only consumer, no store
    finally:
        if old is not None:
            os.environ["DP_STORAGE_URL"] = old
    # (b) and _materialize fails fast on that None (rather than the old silent local fallback)
    g = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": _uri("events")})], "edges": []})
    region = Region(id="r", node_ids={"s"}, output_node="s", backend="default", worker=None,
                    requires=ResourceSpec(), cut_inputs=[])
    monkeypatch.setattr(d.controller, "_boundary_tier", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="no storage tier reachable"):
        d.controller._materialize("run_x", g, region, {}, [region])


def test_parallel_regions_run_independent_regions_concurrently(monkeypatch):
    # dist-parallel-regions: the RunController materialized regions in a strict sequential loop; it now
    # runs INDEPENDENT regions concurrently (a DAG wave scheduler) while respecting dependencies. Two
    # independent intermediate regions feeding a final must overlap in flight; results stay correct.
    import threading as _th
    import time as _t

    from hub.models import Graph, PerNodeStatus, ResourceSpec, RunOutput, RunStatus
    from hub.planner import Region
    ctrl = get_deps().controller
    regs = [
        Region(id="ra", node_ids={"a"}, output_node="a", backend="default", worker=None,
               requires=ResourceSpec(), cut_inputs=[]),
        Region(id="rb", node_ids={"b"}, output_node="b", backend="default", worker=None,
               requires=ResourceSpec(), cut_inputs=[]),
        Region(id="rf", node_ids={"f"}, output_node="f", backend="default", worker=None,
               requires=ResourceSpec(), cut_inputs=[("a", None, "f", None), ("b", None, "f", None)]),
    ]
    rid = "run_parallel_regions"
    expected_output = RunOutput(
        node_id="f", port_id="out", wire="dataset",
        publication_kind="result", outcome="pending",
    )
    ctrl.runs[rid] = RunStatus(
        run_id=rid, status="queued", placement="distributed", target_node_id="f",
        outputs=[expected_output],
        per_node=[PerNodeStatus(node_id=n, status="queued", label=n)
                  for n in ("a", "b", "f")],
    )
    ctrl._cancel[rid] = _th.Event()
    inflight, peak, lock = [0], [0], _th.Lock()

    def fake_mat(run_id, graph, region, ref_uri, regions=None, uid=None):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        _t.sleep(0.2)
        with lock:
            inflight[0] -= 1
        return f"/tmp/{region.output_node}.parquet"

    monkeypatch.setattr(ctrl, "_materialize", fake_mat)
    monkeypatch.setattr(ctrl, "_run_final", lambda *a, **k: RunStatus(
        run_id="x", status="done", placement="distributed", target_node_id="f",
        per_node=[], rows_processed=1, total_rows=1,
        outputs=[RunOutput(
            node_id="f", port_id="out", wire="dataset",
            publication_kind="result", outcome="committed", uri="/tmp/f", rows=1,
        )],
    ))
    monkeypatch.setattr(ctrl, "on_status", None)
    monkeypatch.setattr(ctrl, "on_complete", None)
    monkeypatch.setenv("DP_REGION_CONCURRENCY", "4")
    try:
        ctrl._orchestrate(rid, Graph(id="c", version=1, nodes=[], edges=[]), "f", regs)
        assert ctrl.runs[rid].status == "done", ctrl.runs[rid].error
        assert peak[0] >= 2, f"independent regions did not run concurrently (peak in-flight = {peak[0]})"
    finally:
        ctrl.runs.pop(rid, None)  # don't leak this synthetic run onto the shared controller


def test_dist_cancel_cancels_every_concurrent_subrun_and_await_survives_dropped_event():
    # acceptance #9: with the parallel wave scheduler, several regions' sub-runs are in flight at once.
    # cancel() must cancel EVERY one (the old single _sub slot cancelled only the last-registered → the
    # siblings leaked), and _await must not KeyError when a sibling failure pops self._cancel mid-poll.
    import threading as _th

    from hub.models import RunStatus
    ctrl = get_deps().controller

    class _Backend:
        def __init__(self): self.cancelled = []
        def cancel(self, sub_id): self.cancelled.append(sub_id)

    rid = "run_cancel_all"
    be_a, be_b = _Backend(), _Backend()
    ctrl.runs[rid] = RunStatus(run_id=rid, status="running", placement="distributed", target_node_id="f", per_node=[])
    ctrl._cancel[rid] = _th.Event()
    try:
        ctrl._track_sub(rid, be_a, "sub_a")     # two regions in flight concurrently on different backends
        ctrl._track_sub(rid, be_b, "sub_b")
        ctrl.cancel(rid)
        assert ctrl._cancel[rid].is_set()
        assert be_a.cancelled == ["sub_a"] and be_b.cancelled == ["sub_b"], "cancel must reach BOTH sub-runs"

        # _await captures the Event once, so it does not KeyError if self._cancel[cancel_run] is gone (a
        # sibling failure pops it while this orphaned poll is still running). cancel_run points at a key
        # that isn't in self._cancel → the old `self._cancel[cancel_run]` would raise on the first poll.
        polls = ["running", "running", "done"]

        class _Poller:
            def status(self, _sid):
                return RunStatus(run_id=_sid, status=polls.pop(0), placement="distributed", per_node=[])
            def cancel(self, _sid): pass
        s = ctrl._await(_Poller(), "sub_x", cancel_run="run_no_such_key")
        assert s.status == "done"  # polled to completion without KeyError

        # A Ray-like legacy backend may report cancelled before its driver stops. Without the explicit
        # acknowledgement seam, the controller must keep polling rather than finalize the logical run.
        eager_polls = ["cancelled", "cancelled", "done"]

        class _EagerBackend:
            calls = 0

            def status(self, sub_id):
                self.calls += 1
                return RunStatus(run_id=sub_id, status=eager_polls.pop(0), placement="distributed", per_node=[])

            def cancel(self, _sub_id):
                pass

        eager = _EagerBackend()
        assert ctrl._await(eager, "sub_eager").status == "done" and eager.calls == 3
    finally:
        ctrl.runs.pop(rid, None)
        ctrl._cancel.pop(rid, None)
        ctrl._sub.pop(rid, None)


def test_declared_keys_and_relationships_are_independent_rows():
    # #9 fix: each declared key / relationship is its OWN DB row (not one shared JSON blob), so setting
    # one never rewrites/clobbers another — the mechanism that stops cross-instance lost updates.
    from hub import metadb
    from hub.models import Relationship
    d = get_deps()
    ev, img, mov = _uri("events"), _uri("images"), _uri("movies")
    r1 = Relationship(left_uri=img, left_columns=["id"], right_uri=ev, right_columns=["user_id"], cardinality="1:N")
    r2 = Relationship(left_uri=mov, left_columns=["id"], right_uri=ev, right_columns=["user_id"], cardinality="1:N")
    try:
        d.catalog.set_declared_key(ev, ["user_id"])
        d.catalog.set_declared_key(img, ["id"])                 # must NOT drop events' key
        km = metadb.catalog_declared_keys()
        assert km.get(ev) == ["user_id"] and km.get(img) == ["id"]
        d.catalog.add_relationship(r1)
        d.catalog.add_relationship(r2)                          # a different pair → its own row
        assert len(d.catalog.relationships()) == 2
        d.catalog.remove_relationship(r1)                       # removing one leaves the other intact
        rest = d.catalog.relationships()
        assert len(rest) == 1 and rest[0].left_uri == mov
    finally:
        d.catalog.set_declared_key(ev, [])
        d.catalog.set_declared_key(img, [])
        d.catalog.remove_relationship(r1)
        d.catalog.remove_relationship(r2)


def test_admin_gate_on_global_settings_and_users(monkeypatch):
    # F10: instance-wide config (global settings, user creation) is admin-only in multi-user mode; the
    # seeded/bootstrap user is admin, a new user is not. Open single-user mode has no gate.
    from fastapi import HTTPException

    from hub import metadb
    from hub.routers.workspace import _require_admin
    assert metadb.is_admin("local") is True                 # the bootstrap/default user is admin
    with metadb.session() as s:
        u = metadb.User(name="bob")
        s.add(u)
        s.flush()
        bob = u.id
    assert metadb.is_admin(bob) is False                    # a freshly-created user is NOT admin
    _require_admin(bob)                                      # open mode (no auth) → no privilege boundary
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)          # auth on
    with pytest.raises(HTTPException):
        _require_admin(bob)                                 # non-admin → 403
    _require_admin("local")                                 # admin → allowed


def test_sql_fs_sandbox_confines_reads_in_auth_mode(monkeypatch, tmp_path):
    # F4: in multi-user (auth) mode with no object store, DuckDB's filesystem is confined to the
    # allowed roots, so a `sql` node's read_csv/COPY can't reach arbitrary local files. Test the
    # mechanism on an isolated connection (doesn't touch the shared base conn).
    import os
    import tempfile

    import duckdb

    from hub import db, metadb
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)          # auth on
    monkeypatch.setenv("DP_DATASET_ROOTS", str(tmp_path))   # allowed root
    monkeypatch.delenv("DP_STORAGE_URL", raising=False)     # no object store → the FS sandbox must apply
    # "no object store" means ALL sources empty: env (above), default cred, and any s3/gs destination
    metadb.set_setting("destinations", [], scope="global", scope_id="")
    metadb.set_setting("defaultObjectStoreCredId", "", scope="global", scope_id="")
    inside = tmp_path / "ok.csv"
    inside.write_text("a\n1\n")
    outside_dir = tempfile.mkdtemp()                        # a dir NOT under any allowed root
    outside = os.path.join(outside_dir, "secret.csv")
    with open(outside, "w") as f:
        f.write("s\n9\n")
    c = duckdb.connect(":memory:")
    db._maybe_sandbox_fs(c)  # apply the same sandbox the base conn gets in auth + no-object-store mode
    assert c.execute(f"SELECT count(*) FROM read_csv('{inside}')").fetchone()[0] == 1  # inside a root: OK
    with pytest.raises(Exception):
        c.execute(f"SELECT count(*) FROM read_csv('{outside}')").fetchall()            # outside: blocked


def test_object_store_via_env_keeps_external_access_enabled(monkeypatch):
    # P0-STOR-01: auth ON + DP_STORAGE_URL=s3:// (object store via env, creds from the AWS chain, no DB
    # object-store Cred) must NOT disable external access — else ensure_object_store()'s httpfs load +
    # s3 read/write fail closed. The object store wins over the FS sandbox (external access stays on).
    import duckdb
    from hub import db
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)            # auth on
    monkeypatch.setenv("DP_STORAGE_URL", "s3://bucket/out")   # object store via env, no DB setting
    c = duckdb.connect(":memory:")
    db._maybe_sandbox_fs(c)
    assert c.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is True


def test_catalog_missing_flag_and_unregister(tmp_path):
    # F32: a registered dataset whose local file is later deleted is flagged `missing` (UI can grey it
    # out); DELETE /catalog/tables/{id} prunes the dead entry instead of surfacing a raw IOException.
    import os

    import duckdb
    p = tmp_path / "tmp_ds.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1 AS a) TO '{p}' (FORMAT PARQUET)")
    reg = client.post("/api/catalog/register", json={"uri": str(p), "name": "tmp_ds"}).json()
    tid = reg["id"]
    assert client.get(f"/api/catalog/tables/{tid}").json()["missing"] is False   # file present
    os.remove(p)
    assert client.get(f"/api/catalog/tables/{tid}").json()["missing"] is True    # file gone → flagged
    assert client.delete(f"/api/catalog/tables/{tid}").status_code == 200
    assert client.get(f"/api/catalog/tables/{tid}").status_code == 404           # pruned, not resurrected


def test_chart_node_produces_series():
    # F37 (charting): the chart node builds an (x, y) series — grouped agg(y) by x (bar/line), or
    # raw x,y points (scatter). Grouped charts require a durable full run; raw points stay bounded-previewable.
    ev = _uri("events")

    def chart_graph(cfg):
        return {"id": "c", "version": 1, "nodes": [
            N("s", "source", {"uri": ev}),
            {"id": "ch", "type": "chart", "position": {"x": 0, "y": 0}, "data": {"config": cfg}}],
            "edges": [E("s", "ch")]}

    def chart(cfg):
        g = chart_graph(cfg)
        return client.post("/api/run/preview", json={"graph": g, "nodeId": "ch", "k": 50}).json()
    bar = chart({"chartType": "bar", "x": "event", "agg": "count"})
    assert bar.get("notPreviewable") and "full pass" in (bar.get("reason") or "")
    _, grouped = _full_result(chart_graph({"chartType": "bar", "x": "event", "agg": "count"}), "ch", 50)
    assert {c["name"] for c in grouped["columns"]} == {"x", "y"}
    assert {r["x"] for r in grouped["rows"]} == {"view", "click", "purchase", "signup"}
    scatter = chart({"chartType": "scatter", "x": "user_id", "y": "amount", "agg": "none"})
    assert {c["name"] for c in scatter["columns"]} == {"x", "y"} and scatter["rows"]
    assert chart({"chartType": "bar", "agg": "count"}).get("notPreviewable")       # no X → honest refusal
    assert chart({"chartType": "bar", "x": "event", "agg": "sum"}).get("notPreviewable")  # sum needs a Y (not silent count)
    minmax = chart({"chartType": "bar", "x": "event", "y": "event", "agg": "max"})  # max of a TEXT column
    assert minmax.get("notPreviewable")
    _, minmax_result = _full_result(
        chart_graph({"chartType": "bar", "x": "event", "y": "event", "agg": "max"}), "ch", 50,
    )
    assert not minmax_result.get("error")  # TRY_CAST → NULL y, not a raw ConversionException


def test_grouped_chart_keeps_all_groups_while_interactive_view_is_capped(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source = tmp_path / "many-groups.parquet"
    groups = 2_001
    pq.write_table(pa.table({
        "group_name": [f"group-{index:04d}" for index in range(groups)],
        "value": list(range(groups)),
    }), source)
    graph = {"id": "chart-many-groups", "version": 1, "nodes": [
        N("source", "source", {"uri": str(source)}),
        N("chart", "chart", {"chartType": "bar", "x": "group_name", "agg": "count"}),
    ], "edges": [E("source", "chart")]}

    status, interactive = _full_result(graph, "chart", 2_000)
    output = _sole_output(status, outcome="committed")
    assert output["rows"] == groups
    assert len(interactive["rows"]) == 2_000
    assert interactive["rowCount"] == groups
    assert interactive["hasMore"] is False
    assert interactive["completeness"] == "capped"
    assert interactive["rowLimit"] == 2_000
    assert interactive["limitReason"] == "interactive-row-budget"
    assert interactive["limitScope"] == "result-window"

    from hub import metadb

    storage = get_deps().storage
    original_acquire = storage.acquire_result_read
    export_guards = []

    def capture_export_guard(uri, owner):
        guard = original_acquire(uri, owner)
        if owner.startswith(f"export:{status['runId']}:"):
            export_guards.append(guard)
        return guard

    monkeypatch.setattr(storage, "acquire_result_read", capture_export_guard)
    export_params = {"nodeId": "chart", "portId": "out", "filename": "grouped chart"}
    preflight = client.head(
        f"/api/run/{status['runId']}/export", params=export_params,
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.content == b""
    assert int(preflight.headers["content-length"]) > 0
    assert preflight.headers["x-data-scope"] == "full-result"
    exported = client.get(
        f"/api/run/{status['runId']}/export",
        params=export_params,
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["x-data-scope"] == "full-result"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["content-disposition"] == (
        'attachment; filename="grouped_chart-full-result.parquet"'
    )
    table = pq.read_table(pa.BufferReader(exported.content))
    assert table.num_rows == groups
    assert table.column("x").to_pylist()[-1] == "group-2000"
    assert export_guards
    assert all(not metadb.local_result_read_active(
        guard.uri, storage.namespace_id, guard.reader_id,
    ) for guard in export_guards), "response completion must release the exact artifact read fence"

    # Exercise the response itself with a real ASGI http.disconnect message. Starlette cancels the body
    # iterator after its first chunk; the response-level outer finally must still release the exact read
    # fence (iterator/background cleanup alone is not a sufficient disconnect contract).
    import asyncio
    from hub.routers import runs as runs_router

    monkeypatch.setattr(runs_router, "_EXPORT_CHUNK_BYTES", 1)
    disconnected = runs_router.export_run_result(
        status["runId"], node_id="chart", port_id="out", filename="disconnect",
        user_id=None, uid="local",
    )
    sent = []

    async def disconnect_after_one_chunk():
        first_receive = True
        body_sent = asyncio.Event()

        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await body_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                body_sent.set()

        await disconnected({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": f"/api/run/{status['runId']}/export", "raw_path": b"/export",
            "query_string": b"", "root_path": "", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("testserver", 80),
        }, receive, send)

    asyncio.run(disconnect_after_one_chunk())
    assert any(message["type"] == "http.response.body" and message.get("body")
               for message in sent)
    assert all(not metadb.local_result_read_active(
        guard.uri, storage.namespace_id, guard.reader_id,
    ) for guard in export_guards), "ASGI disconnect must release the result read fence"

    # ASGI 2.4 no longer runs a disconnect-listener task: a socket loss is an OSError from body send,
    # translated by Starlette to ClientDisconnect before background work. The response outer finally is
    # therefore the only reliable owner and must finish closing before the exception reaches the server.
    from starlette.requests import ClientDisconnect

    send_failed = runs_router.export_run_result(
        status["runId"], node_id="chart", port_id="out", filename="send-failure",
        user_id=None, uid="local",
    )
    failed_messages = []

    async def fail_during_body_send():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            failed_messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("client socket closed")

        with pytest.raises(ClientDisconnect):
            await send_failed({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": f"/api/run/{status['runId']}/export", "raw_path": b"/export",
                "query_string": b"", "root_path": "", "headers": [],
                "client": ("127.0.0.1", 1), "server": ("testserver", 80),
            }, receive, send)

    asyncio.run(fail_during_body_send())
    assert any(message["type"] == "http.response.body" and message.get("body")
               for message in failed_messages)
    assert all(not metadb.local_result_read_active(
        guard.uri, storage.namespace_id, guard.reader_id,
    ) for guard in export_guards), "send failure must synchronously release the result read fence"
    assert client.head(
        f"/api/run/{status['runId']}/export",
        params={"nodeId": "n" * 257, "portId": "out"},
    ).status_code == 422
    assert client.head(
        f"/api/run/{status['runId']}/export",
        params={"nodeId": "n" * 256, "portId": "out"},
    ).status_code == 404


def test_export_resources_close_is_reentrant_safe():
    import contextlib

    from hub.routers.runs import _ExportResources

    stack = contextlib.ExitStack()
    resources = _ExportResources(stack)
    callbacks = []
    stack.callback(lambda: callbacks.append("closed"))
    stack.callback(resources.close)

    resources.close()

    assert callbacks == ["closed"]
    resources.close()


def test_durable_output_falls_back_on_status_outage_but_not_programming_errors(monkeypatch):
    from fastapi import HTTPException

    from hub import metadb
    from hub.backends import BackendStatusUnavailable
    from hub.models import RunOutput, RunStatus
    from hub.routers import runs as runs_router

    output = RunOutput(
        node_id="node", port_id="out", wire="dataset", publication_kind="result",
        outcome="committed", uri="/tmp/retained.parquet", rows=3,
    )
    retained = RunStatus(
        run_id="retained-run", status="done", placement="local", target_node_id="node",
        outputs=[output], per_node=[],
    ).model_dump()

    class UnavailableRunner:
        @staticmethod
        def status(_run_id):
            raise BackendStatusUnavailable("backend unavailable")

    monkeypatch.setattr(runs_router, "_runner_for", lambda _run_id: UnavailableRunner())
    monkeypatch.setattr(metadb, "get_run_state", lambda _run_id: retained)
    assert runs_router._durable_run_output("retained-run", "node", "out") == output

    class BrokenRunner:
        @staticmethod
        def status(_run_id):
            raise RuntimeError("invariant broken")

    monkeypatch.setattr(runs_router, "_runner_for", lambda _run_id: BrokenRunner())
    with pytest.raises(RuntimeError, match="invariant broken"):
        runs_router._durable_run_output("retained-run", "node", "out")

    monkeypatch.setattr(runs_router, "_runner_for", lambda _run_id: UnavailableRunner())
    monkeypatch.setattr(metadb, "get_run_state", lambda _run_id: None)
    monkeypatch.setattr(metadb, "get_run_record_outputs", lambda _run_id: None)
    with pytest.raises(HTTPException) as unavailable:
        runs_router._durable_run_output("missing-run", "node", "out")
    assert unavailable.value.status_code == 503


def test_run_output_routes_fall_back_to_logical_run_history_and_keep_catalog_totals(tmp_path):
    import uuid
    import pyarrow as pa
    import pyarrow.parquet as pq
    from hub import metadb

    canvas_id = f"history-output-{uuid.uuid4().hex}"
    result_run_id = f"logical-result-{uuid.uuid4().hex}"
    catalog_run_id = f"logical-catalog-{uuid.uuid4().hex}"
    result_path = tmp_path / "history-result.parquet"
    catalog_path = tmp_path / "history-catalog.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), result_path)
    pq.write_table(pa.table({"value": [10, 20, 30]}), catalog_path)
    with metadb.session() as session:
        session.add(metadb.Canvas(id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="history"))
    try:
        assert metadb.record_run(
            canvas_id, "result-node", "run", "done", rows=3, run_id=result_run_id,
            outputs=[{
                "node_id": "result-node", "port_id": "out", "port_label": "Output",
                "wire": "dataset", "publication_kind": "result", "outcome": "committed",
                "uri": str(result_path), "table": None, "rows": 3, "error": None,
            }],
        )
        assert metadb.record_run(
            canvas_id, "write-node", "run", "done", rows=1, run_id=catalog_run_id,
            outputs=[{
                "node_id": "write-node", "port_id": "out", "port_label": "Output",
                "wire": "dataset", "publication_kind": "catalog", "outcome": "committed",
                "uri": str(catalog_path), "table": "catalog_table", "rows": 1, "error": None,
            }],
        )
        assert metadb.get_run_state(result_run_id) is None
        result_page = client.post(
            f"/api/run/{result_run_id}/sample",
            json={"nodeId": "result-node", "portId": "out", "k": 2, "offset": 0},
        )
        assert result_page.status_code == 200, result_page.text
        assert result_page.json()["rowCount"] == 3
        exported = client.get(
            f"/api/run/{result_run_id}/export",
            params={"nodeId": "result-node", "portId": "out"},
        )
        assert exported.status_code == 200
        assert pq.read_table(pa.BufferReader(exported.content)).num_rows == 3

        catalog_page = client.post(
            f"/api/run/{catalog_run_id}/sample",
            json={"nodeId": "write-node", "portId": "out", "k": 2, "offset": 0},
        )
        assert catalog_page.status_code == 200, catalog_page.text
        assert catalog_page.json()["rowCount"] == 3  # artifact total, not append mutation rows=1
        assert client.get(
            f"/api/run/{catalog_run_id}/export",
            params={"nodeId": "write-node", "portId": "out"},
        ).status_code == 409

        history_rows = {row["runId"]: row for row in metadb.list_runs(canvas_id)}
        history_id = history_rows[result_run_id]["id"]
        assert metadb.get_run_record_output(history_id, "result-node", "out") is None
        assert client.get(
            f"/api/run/{history_id}/export",
            params={"nodeId": "result-node", "portId": "out"},
        ).status_code == 404

        from hub.mcp import Playground

        mcp_sample = Playground(
            get_deps(), metadb.DEFAULT_USER_ID, "http://127.0.0.1:8471",
        ).sample_result({"runId": result_run_id, "limit": 2, "columns": ["value"]})
        assert mcp_sample["rows"] == [{"value": 1}, {"value": 2}]
        assert mcp_sample["nodeId"] == "result-node" and mcp_sample["portId"] == "out"
        assert mcp_sample["rowCount"] == 3 and mcp_sample["completeness"] == "page"
    finally:
        metadb.delete_canvas_cascade(canvas_id)


def test_object_attempt_single_shard_is_sampleable_and_streamable_but_multi_shard_is_explicit(
        tmp_path, monkeypatch):
    import contextlib
    from urllib.parse import urlsplit

    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    from hub import db
    from hub.handoff import MANIFEST_NAME, write_manifest
    from hub.models import RunOutput
    from hub.plugins import adapters
    from hub.routers import runs as runs_router
    import hub.handoff as handoff

    object_root = tmp_path / "object-store"

    def fake_object_fs(uri):
        parsed = urlsplit(uri)
        return pafs.LocalFileSystem(), str(
            object_root / parsed.netloc / parsed.path.lstrip("/"))

    monkeypatch.setattr(adapters, "object_fs", fake_object_fs)
    monkeypatch.setattr(handoff, "object_fs", fake_object_fs)

    @contextlib.contextmanager
    def fixed_attempt_read_scope(_storage, _uris, *, owner):
        assert owner
        yield []

    monkeypatch.setattr(runs_router, "source_read_scope", fixed_attempt_read_scope)

    class LocalObjectAdapter:
        name = "local-object-test"

        def preview_scan(self, uri, columns=None, limit=2_000):
            _fs, local = fake_object_fs(uri)
            relation = db.conn().read_parquet(local).limit(limit)
            if columns:
                relation = relation.project(", ".join(columns))
            return relation

        def metadata_count(self, uri):
            _fs, local = fake_object_fs(uri)
            return int(pq.ParquetFile(local).metadata.num_rows)

    monkeypatch.setattr(get_deps(), "resolve_adapter", lambda _uri: LocalObjectAdapter())

    def build_attempt(name: str, parts: list[pa.Table]) -> str:
        uri = f"s3://bucket/results/{name}.parquet.attempt-ns-g1-{name}"
        _fs, prefix = fake_object_fs(uri)
        os.makedirs(prefix, exist_ok=True)
        for index, table in enumerate(parts):
            pq.write_table(table, os.path.join(prefix, f"part-{index:05d}.parquet"))
        commit_dir = os.path.join(
            os.path.dirname(prefix), "_dp_commits", os.path.basename(prefix))
        os.makedirs(commit_dir, exist_ok=True)
        write_manifest(
            uri, run_id=f"run-{name}", rows=sum(table.num_rows for table in parts),
            schema=parts[0].schema,
        )
        assert os.path.exists(os.path.join(commit_dir, MANIFEST_NAME))
        return uri

    one = build_attempt("one", [pa.table({"value": [1, 2, 3]})])
    many = build_attempt(
        "many", [pa.table({"value": [1]}), pa.table({"value": [2]})])

    def output(uri: str, rows: int) -> RunOutput:
        return RunOutput(
            node_id="node", port_id="out", wire="dataset", publication_kind="result",
            outcome="committed", uri=uri, rows=rows,
        )

    selected = output(one, 3)
    monkeypatch.setattr(runs_router, "_require_run_read_access", lambda _run, _uid: None)
    monkeypatch.setattr(runs_router, "_durable_run_output", lambda *_args: selected)

    sample = client.post(
        "/api/run/object-run/sample",
        json={"nodeId": "node", "portId": "out", "k": 2, "offset": 0},
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["rows"] == [{"value": 1}, {"value": 2}]
    assert sample.json()["rowCount"] == 3
    exported = client.get(
        "/api/run/object-run/export", params={"nodeId": "node", "portId": "out"},
    )
    assert exported.status_code == 200, exported.text
    assert pq.read_table(pa.BufferReader(exported.content)).to_pylist() == [
        {"value": 1}, {"value": 2}, {"value": 3},
    ]

    selected = output(many, 2)
    sample = client.post(
        "/api/run/object-run/sample",
        json={"nodeId": "node", "portId": "out", "k": 2, "offset": 0},
    )
    assert sample.status_code == 200
    assert sample.json()["notPreviewable"] is True
    assert "multiple storage shards" in sample.json()["reason"]
    exported = client.get(
        "/api/run/object-run/export", params={"nodeId": "node", "portId": "out"},
    )
    assert exported.status_code == 406

    selected = output(one, 999)
    assert client.post(
        "/api/run/object-run/sample",
        json={"nodeId": "node", "portId": "out", "k": 2, "offset": 0},
    ).status_code == 409
    assert client.head(
        "/api/run/object-run/export", params={"nodeId": "node", "portId": "out"},
    ).status_code == 409


def test_source_node_accepts_catalog_name():
    # F50: a source node can name a catalog table (by name OR id) instead of the full path/uri.
    for ref in ("events", "tbl_events"):
        g = {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ref})], "edges": []}
        r = client.post("/api/run/preview", json={"graph": g, "nodeId": "s", "k": 3}).json()
        assert not r.get("error") and not r.get("notPreviewable") and r["rows"], (ref, r)


def test_library_transform_falls_back_to_kept_code():
    # F9: a promoted library node whose processor is gone (in-memory promote lost on restart) still
    # runs — the node keeps its original code and the engine falls back to it, so the user's code is
    # never destroyed (was: NotPreviewable "processor not registered", code already nulled = data loss).
    graph = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        {"id": "t", "type": "transform", "position": {"x": 0, "y": 0}, "data": {"config": {
            "source": "library", "processor": "gone_xyz", "version": "v1", "mode": "map",
            "code": "def fn(row):\n    row['flagged'] = True\n    return row"}}},
    ], "edges": [E("src", "t")]}
    r = client.post("/api/run/preview", json={"graph": graph, "nodeId": "t", "k": 5}).json()
    assert not r.get("notPreviewable"), r
    assert r["rows"] and all(row.get("flagged") is True for row in r["rows"])


def test_example_plugin_loads_and_runs(tmp_path):
    # the shipped examples/plugins/dp_example package loads via drop-in discovery and its `redact`
    # node runs end-to-end — proof the plugin SPI works for a real third-party package (README claim).
    import shutil
    from pathlib import Path

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_example"
    assert src.exists(), src
    ws = tmp_path / "ws"
    (ws / "plugins").mkdir(parents=True)
    shutil.copytree(src, ws / "plugins" / "dp_example")

    d = Deps(str(ws), str(tmp_path / "data"))
    assert "redact" in d.node_specs  # discovered + registered, no core edit

    p = str(tmp_path / "people.parquet")
    duckdb.connect().execute(f"COPY (SELECT 'alice' AS name UNION ALL SELECT 'bob') TO '{p}' (FORMAT PARQUET)")
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}},
        {"id": "r", "type": "redact", "position": {"x": 0, "y": 0}, "data": {"config": {"column": "name", "keep": 1}}},
    ], "edges": [{"id": "e", "source": "src", "target": "r", "data": {"wire": "dataset"}}]})
    with db.run_scope():
        eng = BuildEngine(g, d.resolve_adapter, d.registry, full=True,
                             node_builders=d.node_builders, node_specs=d.node_specs)
        rows = sorted(eng.relation("r").fetchall())
    assert rows == [("a****",), ("b**",)]  # 'alice'→'a'+4× *, 'bob'→'b'+2× *


def test_source_pushdown_into_scan(tmp_path):
    # A single-consumer source→filter / source→select chain hands the predicate / projection to
    # adapter.scan() on a full run, so an adapter that can prune at the source does — while the
    # filter/select node STILL applies its op, so results are byte-identical. A spy adapter (delegating
    # to the real DuckDB one) records the scan() kwargs; the guards (target-is-the-source, ≥2 consumers,
    # non-plain projection) are exercised too.
    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    from hub.plugins.adapters import DuckDBAdapter

    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,'c')) t(x,y)) TO '{p}' (FORMAT PARQUET)")

    calls: list[dict] = []
    real = DuckDBAdapter()

    class Spy:
        name = "spy"
        def matches(self, uri): return True
        def scan(self, uri, columns=None, predicate=None, limit=None, options=None):
            calls.append({"columns": columns, "predicate": predicate})
            return real.scan(uri, columns=columns, predicate=predicate, limit=limit, options=options)
        def schema(self, uri): return real.schema(uri)
        def count(self, uri): return real.count(uri)
        def fingerprint(self, uri): return real.fingerprint(uri)
        def write(self, uri, rel, mode="overwrite"): return real.write(uri, rel, mode)

    (tmp_path / "ws").mkdir()
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    src = {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}}

    def run(nodes, edges, target, output_node):
        calls.clear()
        with db.run_scope():
            eng = BuildEngine(Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges}),
                                 lambda uri: Spy(), d.registry, full=True, node_builders=d.node_builders,
                                 node_specs=d.node_specs, pushdown=True, output_node=output_node)
            return sorted(eng.relation(target).fetchall())

    def edge(s, t): return {"id": f"{s}{t}", "source": s, "target": t, "data": {"wire": "dataset"}}

    flt = {"id": "f", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "x > 1"}}}
    rows = run([src, flt], [edge("src", "f")], "f", "f")
    assert calls[0]["predicate"] == "x > 1"           # predicate handed to the source
    assert rows == [(2, "b"), (3, "c")]               # …and still correct (filter re-applies)

    sel = {"id": "s", "type": "select", "position": {"x": 0, "y": 0}, "data": {"config": {"select": "x, y"}}}
    run([src, sel], [edge("src", "s")], "s", "s")
    assert calls[0]["columns"] == ["x", "y"]          # a plain column list is pushed as a projection

    sel2 = {"id": "s", "type": "select", "position": {"x": 0, "y": 0}, "data": {"config": {"select": "x AS z"}}}
    run([src, sel2], [edge("src", "s")], "s", "s")
    assert calls[0]["columns"] is None                # `x AS z` isn't a provable column subset → not pushed

    rows = run([src, flt], [edge("src", "f")], "src", "src")
    assert calls[0]["predicate"] is None and rows == [(1, "a"), (2, "b"), (3, "c")]  # never prune the target itself

    flt2 = {"id": "f2", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "x < 3"}}}
    run([src, flt, flt2], [edge("src", "f"), edge("src", "f2")], "f", "f")
    assert calls[0]["predicate"] is None              # ≥2 consumers → can't prove safety → no push-down


def test_plugin_node_ir_hook_runs_on_duckdb_and_ray(tmp_path):
    # dp_upper registers a node with an engine-neutral emit hook (reg.add_node(..., ir=…)): it lowers to
    # a CLEAN `map` op (not opaque), so a distributed backend runs it, and its DuckDB build() + its ir op
    # share the operator → identical results. The Ray operator identity is checked without a live cluster.
    import importlib.util
    import shutil
    from pathlib import Path

    import duckdb
    import pyarrow as pa

    from hub import db, ir as irmod
    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph

    p = str(tmp_path / "t.parquet")
    duckdb.connect().execute(f"COPY (SELECT 'ab' AS name UNION ALL SELECT 'cd') TO '{p}' (FORMAT PARQUET)")
    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_upper", ws / "plugins" / "dp_upper")
    d = Deps(str(ws), str(tmp_path / "data"))
    assert "upper" in d.node_specs and "upper" in d.node_ir  # node + its ir hook both registered

    G = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}},
        {"id": "u", "type": "upper", "position": {"x": 0, "y": 0}, "data": {"config": {"column": "name"}}},
        {"id": "w", "type": "write", "position": {"x": 0, "y": 0}, "data": {"config": {"name": "o"}}},
    ], "edges": [{"id": "e1", "source": "src", "target": "u", "data": {"wire": "dataset"}},
                 {"id": "e2", "source": "u", "target": "w", "data": {"wire": "dataset"}}]})

    step = irmod.lower_to_ir(G, "w", d.node_specs, d.node_ir).by_id()["u"]
    assert step.op == "map" and "code" in step.config           # emit hook → a clean op (not opaque:upper)
    assert irmod.lower_to_ir(G, "w", d.node_specs, d.node_ir).is_clean()
    assert irmod.plan_is_clean(compile_plan(G, "w", d.registry, d.node_specs, d.node_ir))  # can_run agrees
    assert not irmod.lower_to_ir(G, "w", d.node_specs).is_clean()  # WITHOUT the hook → opaque → falls back

    with db.run_scope():                                        # DuckDB build() uppercases
        eng = BuildEngine(G, d.resolve_adapter, d.registry, full=True, node_builders=d.node_builders, node_specs=d.node_specs)
        assert sorted(r[0] for r in eng.relation("u").fetchall()) == ["AB", "CD"]

    ray_src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_ray_ref2", ray_src)
    ray_mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(ray_mod)
    out = ray_mod._make_mapper(step.config)(pa.table({"name": ["ab", "cd"]}))  # the SAME op dp_ray would run
    assert sorted(out.column("name").to_pylist()) == ["AB", "CD"]  # Ray operator ≡ DuckDB build, by construction


def test_similarity_dedup_plugin_clusters_and_marks_representatives(tmp_path):
    # dp_similarity_dedup registers a `similarity-dedup` node: it clusters rows by embedding cosine distance
    # and adds dup_group + is_representative. Exact-duplicate embeddings must land in one cluster each, with
    # exactly one representative per cluster — regardless of threshold.
    import shutil
    from pathlib import Path

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph

    p = str(tmp_path / "emb.parquet")
    # rows 0,1 ≡ [1,0,0]; rows 2,3 ≡ [0,1,0]; row 4 = [0,0,1] → 3 clusters, 3 representatives
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES "
        f"(0,[1.0,0.0,0.0]),(1,[1.0,0.0,0.0]),(2,[0.0,1.0,0.0]),(3,[0.0,1.0,0.0]),(4,[0.0,0.0,1.0])"
        f") t(rid, embedding)) TO '{p}' (FORMAT PARQUET)")
    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_similarity_dedup",
                    ws / "plugins" / "dp_similarity_dedup")
    d = Deps(str(ws), str(tmp_path / "data"))
    assert "similarity-dedup" in d.node_specs

    G = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}},
        {"id": "dd", "type": "similarity-dedup", "position": {"x": 0, "y": 0},
         "data": {"config": {"column": "embedding", "threshold": 0.05}}},
    ], "edges": [{"id": "e1", "source": "src", "target": "dd", "data": {"wire": "dataset"}}]})

    with db.run_scope():
        eng = BuildEngine(G, d.resolve_adapter, d.registry, full=True,
                          node_builders=d.node_builders, node_specs=d.node_specs)
        tbl = eng.relation("dd").order("rid").to_arrow_table()
        assert "dup_group" in tbl.column_names and "is_representative" in tbl.column_names
        groups = tbl.column("dup_group").to_pylist()
        reps = tbl.column("is_representative").to_pylist()
        # 0,1 share a cluster; 2,3 share a cluster; 4 alone → 3 distinct groups, one representative each
        assert groups[0] == groups[1] and groups[2] == groups[3]
        assert len(set(groups)) == 3  # three distinct clusters
        assert sum(bool(x) for x in reps) == 3
        # a representative leads exactly the rows that carry its own group id
        assert reps[0] != reps[1]  # one of {0,1} leads, the other doesn't

    # edge cases on _dedup directly: empty input still emits the columns (so downstream filter() works);
    # a ragged/variable-length list column passes through without crashing (not a fixed-width vector).
    import importlib.util
    import polars as pl
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_similarity_dedup" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_simdedup_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    base = pl.DataFrame({"embedding": [[1.0, 0.0, 0.0]]})
    empty_out = mod._dedup(base.head(0), "embedding", 0.05)
    assert empty_out.height == 0 and "dup_group" in empty_out.columns and "is_representative" in empty_out.columns
    ragged_out = mod._dedup(pl.DataFrame({"embedding": [[1.0, 0.0], [1.0]]}), "embedding", 0.05)
    assert ragged_out.height == 2 and "dup_group" not in ragged_out.columns  # passthrough, no crash


def test_source_preflight_fragments_cold_and_run_plan(tmp_path, monkeypatch, object_store_cred):
    # cluster 14: a cheap pre-run probe flags a huge fragment count / cold-tier source in the run-plan,
    # so a full run fails fast (or warns) instead of hanging or OOMing.
    import duckdb

    from hub import preflight
    from hub.models import Graph

    d = tmp_path / "frags"; d.mkdir()
    for i in range(6):
        duckdb.connect().execute(f"COPY (SELECT {i} AS x) TO '{d}/part{i}.parquet' (FORMAT PARQUET)")
    pf = preflight.source_preflight(str(d), fragment_warn=5)
    assert pf["fragments"] == 6 and any("fragments" in w for w in pf["warnings"])   # 6 files > 5 → warn
    single = str(d / "part0.parquet")
    assert preflight.source_preflight(single, fragment_warn=5)["fragments"] == 1     # one file → no warn
    assert not preflight.source_preflight(single, fragment_warn=5)["warnings"]

    # cold-tier: an object in GLACIER is flagged (best-effort via boto3 — moto stands in for S3)
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    # Object storage must be configured for the probe to run. A non-empty Cred satisfies the guard;
    # mock_aws intercepts boto3.
    object_store_cred({"region": "us-east-1"})
    try:
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="bkt")
            s3.put_object(Bucket="bkt", Key="d/a.parquet", Body=b"x", StorageClass="GLACIER")
            s3.put_object(Bucket="bkt", Key="d/b.parquet", Body=b"y")  # STANDARD
            assert preflight._cold_objects("s3://bkt/d", 1000) == 1
    finally:
        object_store_cred(None)  # restore for other tests

    # the run-plan surfaces the source pre-flight (low threshold → the 6-file dir trips it)
    monkeypatch.setattr(preflight, "_FRAGMENT_WARN", 5)
    dd = get_deps()
    g = Graph(**{"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": str(d)}), N("f", "filter", {"predicate": "x >= 0"})], "edges": [E("s", "f")]})
    plan = dd.controller.plan_summary(g, "f")
    assert any(w for r in plan for w in (r.get("preflight") or []))


def test_named_schema_contracts_reference_enforce_and_diff():
    # cluster 4: schema contracts become NAMED + VERSIONED workspace artifacts multiple pipelines reference,
    # with enforced drift + a structural diff — not just per-node non-enforcing warnings.
    from hub import metadb
    from hub.executors.engine import declared_schema
    from hub.models import GraphNode

    v1_columns = [
        {"fieldId": "caption.id", "name": "id", "type": "int", "nullable": False, "hasDefault": False, "provenance": "declared"},
        {"fieldId": "caption.text", "name": "text", "type": "string", "nullable": True, "provenance": "declared"},
    ]
    v2_columns = v1_columns + [{"fieldId": "caption.score", "name": "score", "type": "double", "nullable": True, "provenance": "declared"}]
    v1 = metadb.save_schema_contract("caption_v", v1_columns)
    v2 = metadb.save_schema_contract("caption_v", v2_columns)
    assert (v1, v2) == (1, 2)
    assert metadb.get_schema_contract("caption_v")["version"] == 2                     # latest by default
    assert len(metadb.get_schema_contract("caption_v", 1)["columns"]) == 2             # a specific version
    d = metadb.diff_columns(metadb.get_schema_contract("caption_v", 1)["columns"],
                            metadb.get_schema_contract("caption_v", 2)["columns"])
    assert d.status == "compatible" and d.fields[-1].kind == "added"

    # endpoints: save (new version), list, get-with-versions, diff
    assert client.post("/api/schemas", json={"name": "ep", "columns": [{"name": "a", "type": "int", "nullable": True, "capabilities": []}]}).json()["version"] == 1
    assert any(s["name"] == "ep" for s in client.get("/api/schemas").json())
    assert client.get("/api/schemas/ep").json()["versions"] == [1]
    assert client.get("/api/schemas/diff", params={"name": "caption_v", "a": 1, "b": 2}).json()["status"] == "compatible"

    # a node REFERENCES a named contract → declared_schema resolves it to the (latest) columns
    node = GraphNode(id="t", type="transform", position={"x": 0, "y": 0},
                     data={"config": {"outputSchema": {"ref": "caption_v"}}})
    assert [c["name"] for c in declared_schema(node)] == ["id", "text", "score"]

    # ENFORCE at run: a contract that doesn't match the actual output FAILS the run; a matching one passes
    ev = _uri("events")  # id BIGINT, user_id BIGINT, event VARCHAR, amount DECIMAL→DOUBLE

    def run_enforced(schema):
        g = {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}),
             N("t", "transform", {"mode": "map", "code": "def fn(r): return r", "outputSchema": schema, "enforceSchema": True})],
             "edges": [E("s", "t")]}
        return _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "t", "confirmed": True}).json()["runId"])

    bad = run_enforced([{"name": "totally_wrong", "type": "int", "capabilities": []}])
    assert bad["status"] == "failed" and "schema contract" in (bad.get("error") or "")
    good = run_enforced([{"name": "id", "type": "int", "capabilities": []}, {"name": "user_id", "type": "int", "capabilities": []},
                         {"name": "event", "type": "string", "capabilities": []}, {"name": "amount", "type": "double", "capabilities": []}])
    assert good["status"] == "done"  # names + normalized types all match → no drift

    # enforce must NOT silently no-op when the referenced contract can't resolve (deleted/typo'd ref) —
    # a safety gate that quietly turns itself off is worse than none: the run fails with a clear reason.
    g = {"id": "c", "version": 1, "nodes": [N("s", "source", {"uri": ev}),
         N("t", "transform", {"mode": "map", "code": "def fn(r): return r", "outputSchema": {"ref": "no_such_contract"}, "enforceSchema": True})],
         "edges": [E("s", "t")]}
    miss = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "t", "confirmed": True}).json()["runId"])
    assert miss["status"] == "failed" and "can't be enforced" in (miss.get("error") or "")


def test_warm_resource_reused_across_batches_and_runs(tmp_path):
    # dp_warm_resource's `warm-map` node builds an expensive handle ONCE via ctx.resource and reuses the
    # SAME instance across batches and across separate runs on the (warm) kernel — the pain being pipelines
    # that reload a model per batch. We prove it by watching one instance accumulate work across two runs.
    import shutil
    from pathlib import Path

    import duckdb

    from hub import db, sdk
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph

    p = str(tmp_path / "t.parquet")
    duckdb.connect().execute(f"COPY (SELECT 'Ab' AS name FROM range(1, 3001) t(i)) TO '{p}' (FORMAT PARQUET)")  # 3000 rows → multiple arrow batches
    ws = tmp_path / "ws"; (ws / "plugins").mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_warm_resource",
                    ws / "plugins" / "dp_warm_resource")
    d = Deps(str(ws), str(tmp_path / "data"))
    assert "warm-map" in d.node_specs

    G = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "s", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}},
        {"id": "wm", "type": "warm-map", "position": {"x": 0, "y": 0}, "data": {"config": {"column": "name"}}},
    ], "edges": [{"id": "e", "source": "s", "target": "wm", "data": {"wire": "dataset"}}]})

    sdk._RESOURCES.pop("dp_warm_resource:model", None)  # start from a cold cache for a deterministic count

    def run_once():
        with db.run_scope():
            eng = BuildEngine(G, d.resolve_adapter, d.registry, full=True,
                              node_specs=d.node_specs, node_builders=d.node_builders)
            tbl = eng.relation("wm").to_arrow_table()
        assert tbl.column("name").to_pylist()[0] == "ab"  # normalized (strip+lower)
        return tbl.num_rows

    n1 = run_once()
    model = sdk._RESOURCES["dp_warm_resource:model"]  # the warm handle, cached process-globally
    assert n1 == 3000 and model.calls == 3000            # one instance saw all rows of run 1
    run_once()
    assert sdk._RESOURCES["dp_warm_resource:model"] is model  # SAME instance (not rebuilt)
    assert model.calls == 6000                                # it accumulated run 2 too → warm across runs

    # close_resources releases handles that expose close()/__exit__ and clears the cache
    class _H:
        closed = False
        def close(self): type(self).closed = True
    sdk._RESOURCES["k"] = _H()
    sdk.close_resources()
    assert _H.closed is True and sdk._RESOURCES == {}

    # a factory that itself calls ctx.resource() for ANOTHER key must NOT deadlock (reentrant lock)
    from hub.sdk import ctx as _ctx
    for k in ("nest:inner", "nest:outer"):
        sdk._RESOURCES.pop(k, None)
    def _outer():
        inner = _ctx.resource("nest:inner", lambda: 41)  # nested resource() from inside a factory
        return inner + 1
    assert _ctx.resource("nest:outer", _outer) == 42  # returns (no hang) → reentrancy holds

    # a factory returning None is CACHED (constructed at most once), not rebuilt every call
    calls = []
    def _none_factory():
        calls.append(1)
        return None
    sdk._RESOURCES.pop("nest:none", None)
    assert _ctx.resource("nest:none", _none_factory) is None
    assert _ctx.resource("nest:none", _none_factory) is None
    assert len(calls) == 1  # built once, then the cached None is returned


def test_ir_unify_regressions(tmp_path):
    # three regressions the IR-unify adversarial pass caught, now fixed + locked:
    import duckdb
    import pytest as _pt

    from hub import db, ir as irmod
    from hub.deps import get_deps
    from hub.executors.engine import BuildEngine, NotPreviewable
    from hub.models import Graph, GraphNode

    p = str(tmp_path / "n.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM range(1,6) t(x)) TO '{p}' (FORMAT PARQUET)")
    d = get_deps()
    src = {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": p}}}

    def G(nodes, edges):
        return Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges})

    def eng(graph):
        return BuildEngine(graph, d.resolve_adapter, d.registry, full=True,
                           node_builders=d.node_builders, node_specs=d.node_specs)

    # (1) a sample node configured n=0 means ZERO rows — not the sample_k fallback (the `or 0` falsy bug)
    g1 = G([src, {"id": "s", "type": "sample", "position": {"x": 0, "y": 0}, "data": {"config": {"n": 0}}}],
           [{"id": "e", "source": "src", "target": "s", "data": {"wire": "dataset"}}])
    with db.run_scope():
        assert eng(g1).relation("s").fetchall() == []

    # (2) a library transform with no processor + no code raises honestly (not a silent passthrough)
    g2 = G([src, {"id": "t", "type": "transform", "position": {"x": 0, "y": 0}, "data": {"config": {"source": "library"}}}],
           [{"id": "e", "source": "src", "target": "t", "data": {"wire": "dataset"}}])
    with db.run_scope(), _pt.raises(NotPreviewable):
        eng(g2).relation("t")

    # (3) a plugin ir hook that RAISES degrades to an IR opaque:<plugin-kind> step — never bricks
    # compile/estimate/run. The plugin's config remains an opaque payload; literal graph kind `opaque`
    # is unrelated and has no special execution semantics.
    def boom(node):
        raise ValueError("bad plugin hook")
    node_ir = {"myplugin": boom}
    plugin_config = {"providerMetadata": {"revision": 7}, "customFlag": True}
    op, config = irmod._op_and_config(
        GraphNode(id="m", type="myplugin", data={"config": plugin_config}), node_ir)
    assert op == "opaque:myplugin" and config == plugin_config
    g3 = G([src, {"id": "m", "type": "myplugin", "position": {"x": 0, "y": 0}, "data": {"config": {}}}],
           [{"id": "e", "source": "src", "target": "m", "data": {"wire": "dataset"}}])
    assert not irmod.lower_to_ir(g3, "m", d.node_specs, node_ir).is_clean()  # opaque → not clean, no raise


def test_unknown_node_kind_fails_closed():
    # P0-DATA-02: a missing plugin / misspelled kind must NOT compile+run as a silent passthrough
    # (which reports success while omitting the intended work). It fails closed everywhere, naming the
    # offending node id + kind.
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("x", "totally_not_a_real_kind", {}),
    ], "edges": [E("a", "x")]}
    # compile surfaces the error instead of a happy `opaque:` plan
    plan = client.post("/api/graph/compile", json={"graph": g, "targetNodeId": "x"}).json()
    assert plan.get("error") and "totally_not_a_real_kind" in plan["error"]
    # preview / run / profile fail closed with a 400 naming the node id + kind
    pv = client.post("/api/run/preview", json={"graph": g, "nodeId": "x", "k": 5})
    assert pv.status_code == 400 and "totally_not_a_real_kind" in pv.text and "'x'" in pv.text
    rn = client.post("/api/run", json={"graph": g, "targetNodeId": "x", "confirmed": True})
    assert rn.status_code == 400 and "totally_not_a_real_kind" in rn.text
    pf = client.post("/api/run/profile", json={"graph": g, "nodeId": "x"})
    assert pf.status_code == 400
    # and the engine itself refuses rather than passing through, even if the API gate is bypassed
    from hub import db
    from hub.executors.engine import BuildEngine, NotPreviewable
    from hub.models import Graph
    d = get_deps()
    be = BuildEngine(Graph(**g), d.resolve_adapter, d.registry, full=True,
                     node_builders=d.node_builders, node_specs=d.node_specs)
    with db.run_scope(), pytest.raises(NotPreviewable):
        be.relation("x")


def test_obsolete_source_table_alias_does_not_execute():
    graph = {"id": "old-source", "version": 1, "nodes": [
        N("source", "source", {"table": "events"}),
    ], "edges": []}
    preview = client.post("/api/run/preview", json={"graph": graph, "nodeId": "source", "k": 5})
    assert preview.status_code == 200
    assert preview.json()["notPreviewable"] is True
    assert preview.json()["reason"] == "no dataset selected"


def test_resolve_config_is_the_shared_builtin_resolver():
    # hub.ir.resolve_config is the SINGLE resolver both the IR and the DuckDB engine (executors/engine.py
    # _lower) read built-in config through — canonicalizing keys so they can't diverge. Lock the contract.
    from hub.ir import resolve_config
    from hub.models import GraphNode

    def N(t, cfg, **data):
        return GraphNode(id="n", type=t, data={"config": cfg, **data})

    assert resolve_config(N("select", {"select": "a, b"})) == {"expr": "a, b"}            # select|expr → expr
    assert resolve_config(N("aggregate", {"group": "k", "aggs": "sum(x)"})) == {"groupBy": "k", "aggs": "sum(x)"}
    assert resolve_config(N("source", {"uri": "t", "tableId": "display-t", "delimiter": ";", "header": "No"})) == \
        {"uri": "t", "options": {"delimiter": ";", "header": "no"}}                        # tableId is display identity, not an executable ref
    assert resolve_config(N("source", {"table": "old-only"})) == {"uri": None}                # obsolete table alias is not executed
    assert resolve_config(N("source", {"uri": "/p.parquet"})) == {"uri": "/p.parquet"}      # no options key when unset
    assert resolve_config(N("sample", {})) == {"n": None, "seed": 42}                       # n unset → engine supplies sample_k
    assert resolve_config(N("write", {"filename": "x.csv", "format": "csv", "writeMode": "append",
                                      "destId": "archive", "destPath": "daily/2026", "partitionBy": ""})) == \
        {"name": None, "filename": "x.csv", "title": None, "format": "csv", "writeMode": "append",
         "destId": "archive", "destPath": "daily/2026", "partitionBy": ""}
    assert resolve_config(N("metric", {"agg": "mean", "column": "x"})) == {"agg": "mean", "column": "x"}  # verbatim


def test_lower_to_ir_and_clean_classification():
    # The engine-neutral IR normalizes each node to (op, resolved config, input wiring); is_clean()
    # marks a run a map-style engine can execute, and plan_is_clean() answers the same from a
    # CompilePlan (what can_run gets) — they must agree. Underpins the dp_ray reference backend.
    from hub import ir
    from hub.compiler import compile_plan
    from hub.models import Graph

    def G(nodes, edges):
        return Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges})

    src = N("src", "source", {"uri": _uri("events")})
    wr = N("w", "write", {"name": "o"})

    clean = G([src, N("m", "transform", {"mode": "map", "code": "def fn(r): return r"}), wr],
              [E("src", "m"), E("m", "w")])
    cir = ir.lower_to_ir(clean, "w")
    assert [s.op for s in cir.steps] == ["read", "map", "write"]
    assert cir.is_clean() and not cir.unsupported()
    assert cir.by_id()["m"].inputs == [("src", None)]     # input wiring captured
    assert ir.plan_is_clean(compile_plan(clean, "w"))      # can_run-side agrees

    # a transform with NO `mode` key defaults to "map" on BOTH sides (compiler + IR) → they agree
    ml = G([src, N("m2", "transform", {"code": "def fn(r): return r"}), wr], [E("src", "m2"), E("m2", "w")])
    assert ir.lower_to_ir(ml, "w").is_clean() and ir.plan_is_clean(compile_plan(ml, "w"))

    dirty = G([src, N("j", "sql", {"sql": "SELECT * FROM input"}), wr], [E("src", "j"), E("j", "w")])
    di = ir.lower_to_ir(dirty, "w")
    assert di.unsupported() == ["sql"] and not di.is_clean()
    assert not ir.plan_is_clean(compile_plan(dirty, "w"))  # …and falls back

    byp = N("b", "filter", {"predicate": "x>0"}); byp["data"]["bypassed"] = True
    dis = N("d", "filter", {"predicate": "x>0"}); dis["data"]["disabled"] = True
    ops = {s.id: s.op for s in ir.lower_to_ir(G([src, byp, dis], [E("src", "b"), E("b", "d")])).steps}
    assert ops["b"] == "passthrough" and ops["d"] == "disabled"

    # a filter NODE is a SQL predicate (not clean); a transform in filter MODE is a Python op (clean)
    g3 = G([src, N("fn", "filter", {"predicate": "x>0"}),
            N("tf", "transform", {"mode": "filter", "code": "def fn(r): return True"})],
           [E("src", "fn"), E("fn", "tf")])
    o3 = {s.id: s.op for s in ir.lower_to_ir(g3, "tf").steps}
    assert o3["fn"] == "filter_sql" and o3["tf"] == "filter"


def _load_dp_ray():
    import importlib.util
    from pathlib import Path
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_ray_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)  # loads WITHOUT ray (lazy import)
    return mod


def _ray_node(nid, t, cfg):
    return {"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"config": cfg}}


def _ray_edge(s, t):
    return {"id": f"{s}{t}", "source": s, "target": t, "data": {"wire": "dataset"}}


def test_ray_backend_operator_gating_and_fallback(tmp_path):
    # The dp_ray reference backend runs the clean IR subset on Ray Data. Ray's streaming executor must
    # spawn worker processes, which many sandboxes/CI can't — so this test covers everything that does
    # NOT need a live cluster (the live differential run is test_ray_backend_live_differential, opt-in):
    #   (1) the map/filter operator the backend runs ON Ray IS the DuckDB engine's sandbox operator, so
    #       results are identical BY CONSTRUCTION — verified by applying _make_mapper to a real Arrow batch;
    #   (2) can_run gates the clean subset from the CompilePlan;
    #   (3) a non-clean (relational) graph runs but falls back to the DuckDB LocalRunner.
    # The dp_ray module imports ray LAZILY (inside run/execute), so this loads and runs without ray.
    import time

    import duckdb
    import pyarrow as pa

    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    mod = _load_dp_ray()
    rr = mod.RayRunner(deps)

    def G(nodes, edges):
        return Graph(**{"id": "c", "version": 1, "nodes": nodes, "edges": edges})

    # (1) the exact operator the Ray map_batches UDF applies, run on a real Arrow batch — no Ray needed
    tbl = pa.table({"x": list(range(1, 11))})
    mapped = mod._make_mapper({"mode": "map", "code": "def fn(r):\n    r['x'] = r['x']*2\n    return r", "onError": "raise"})(tbl)
    assert mapped.column("x").to_pylist() == [2 * i for i in range(1, 11)]
    filtered = mod._make_mapper({"mode": "filter", "code": "def fn(r):\n    return r['x'] > 8", "onError": "raise"})(tbl)
    assert filtered.column("x").to_pylist() == [9, 10]

    # (2) can_run: a source→map→write graph is clean; a relational graph is not
    clean = G([_ray_node("src", "source", {"uri": "x.parquet"}),
               _ray_node("m", "transform", {"mode": "map", "code": "def fn(r): return r"}),
               _ray_node("w", "write", {"name": "o"})], [_ray_edge("src", "m"), _ray_edge("m", "w")])
    assert rr.can_run(compile_plan(clean, "w", deps.registry, deps.node_specs)) is True

    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM range(1,6) t(x)) TO '{p}' (FORMAT PARQUET)")
    # (3a) a GROUPED aggregate is NOW Ray-runnable — Ray hash-shuffles on the group key, DuckDB computes
    # per partition (any aggregate expr, byte-identical). can_run AND _ray_runnable both agree.
    agg = G([_ray_node("src", "source", {"uri": p}),
             _ray_node("a", "aggregate", {"groupBy": "x", "aggs": "sum(x) AS s, stddev(x) AS sd"}),
             _ray_node("w", "write", {"name": "agg_out"})], [_ray_edge("src", "a"), _ray_edge("a", "w")])
    assert rr.can_run(compile_plan(agg, "w", deps.registry, deps.node_specs)) is True
    assert rr._ray_runnable(lower_to_ir(agg, "w", deps.node_specs)) is True   # sum/stddev fine — DuckDB runs it
    # (3b) a GLOBAL aggregate (no group key = no shuffle key) → _ray_runnable False → DuckDB single-node
    summ = G([_ray_node("src", "source", {"uri": p}),
              _ray_node("a", "aggregate", {"aggs": "sum(x) AS s"}),
              _ray_node("w", "write", {"name": "sum_out"})], [_ray_edge("src", "a"), _ray_edge("a", "w")])
    assert rr._ray_runnable(lower_to_ir(summ, "w", deps.node_specs)) is False

    # (3b-i) an ORDER-SENSITIVE aggregate (list/first/…) WITHOUT an intra-aggregate ORDER BY is NOT
    # byte-identical distributed (hash-shuffle reorders intra-group rows) → _ray_runnable False; WITH an
    # ORDER BY inside the call it IS deterministic → True (acceptance #6).
    def _agg(aggs):
        gg = G([_ray_node("src", "source", {"uri": p}),
                _ray_node("a", "aggregate", {"groupBy": "x", "aggs": aggs}),
                _ray_node("w", "write", {"name": "o"})], [_ray_edge("src", "a"), _ray_edge("a", "w")])
        return rr._ray_runnable(lower_to_ir(gg, "w", deps.node_specs))
    assert _agg("list(x) AS xs") is False                       # unordered list → reorders → fall back
    assert _agg("string_agg(CAST(x AS VARCHAR), ',') AS s") is False
    assert _agg("count(*) AS n, first(x) AS f") is False        # a 2nd agg being fine doesn't save `first`
    assert _agg("list(x ORDER BY x) AS xs") is False            # conservatively rejected by name (AST can't
                                                                # see the ORDER BY — DuckDB rewrites it out)
    assert _agg("sum(x) AS s, count(*) AS n") is True           # plain reducing aggs are order-free
    # #39 review: every alias the parser can emit + mode (arbitrary among ties) are caught; value-keyed
    # aggregates (median/quantile/histogram) are order-free and still distribute.
    assert _agg("argmax(x, y) AS a") is False and _agg("argmin(x, y) AS a") is False   # no-underscore alias
    assert _agg("mode(x) AS m") is False
    assert _agg("max_by(x, y) AS a") is False
    assert _agg("median(x) AS md, quantile(x, 0.5) AS q") is True

    # (3b-ii) a RANKING/OFFSET window needs a non-empty ORDER BY to distribute faithfully; a pure-AGGREGATE
    # window (sum/count OVER (PARTITION BY k)) is whole-partition → byte-identical even with no ORDER BY,
    # so it must NOT be over-rejected (acceptance #7 + #39 review).
    def _win(cfg):
        gg = G([_ray_node("src", "source", {"uri": p}),
                _ray_node("wn", "window", cfg),
                _ray_node("w", "write", {"name": "o"})], [_ray_edge("src", "wn"), _ray_edge("wn", "w")])
        return rr._ray_runnable(lower_to_ir(gg, "w", deps.node_specs))
    assert _win({"partitionBy": "x", "expr": "row_number()", "as": "rn"}) is False          # ranking, no ORDER BY
    assert _win({"partitionBy": "x", "orderBy": "x", "expr": "row_number()", "as": "rn"}) is True
    assert _win({"partitionBy": "x", "expr": "first_value(x)", "as": "f"}) is False         # offset, no ORDER BY
    assert _win({"partitionBy": "x", "expr": "sum(x)", "as": "s"}) is True                  # aggregate window → OK
    assert _win({"partitionBy": "x", "expr": "count(*)", "as": "c"}) is True                # no ORDER BY needed
    # an ORDER-SENSITIVE aggregate used AS a window (list/string_agg OVER (PARTITION BY k)) parses to a
    # WINDOW_AGGREGATE type but its result still depends on intra-partition row order → must fall back even
    # WITH no orderBy (the shuffle scrambles the partition). Regression from the #7 over-reject fix.
    assert _win({"partitionBy": "x", "expr": "list(x)", "as": "l"}) is False
    assert _win({"partitionBy": "x", "expr": "string_agg(CAST(x AS VARCHAR), ',')", "as": "s"}) is False
    assert _win({"partitionBy": "x", "orderBy": "x", "expr": "list(x)", "as": "l"}) is False  # even with orderBy

    # (3b-iii) full-row dedup over a FLOAT column falls back — the shuffle's raw-byte equality splits
    # -0.0/0.0 (and NaN payloads) that DuckDB DISTINCT coalesces (acceptance #12). Uses RAW DuckDB types,
    # so NESTED floats (struct/list of double) also fall back, while DECIMAL (exact) + int still distribute.
    def _dedup(select_sql):
        pth = str(tmp_path / f"d_{abs(hash(select_sql))}.parquet")
        duckdb.connect().execute(f"COPY (SELECT {select_sql} FROM range(1,6) t(x)) TO '{pth}' (FORMAT PARQUET)")
        gg = G([_ray_node("src", "source", {"uri": pth}), _ray_node("d", "dedup", {}),
                _ray_node("w", "write", {"name": "o"})], [_ray_edge("src", "d"), _ray_edge("d", "w")])
        ir = lower_to_ir(gg, "w", deps.node_specs)
        assert rr._ray_runnable(ir) is True                     # config-only gate: full-row dedup is fine
        return rr._dedup_needs_single_node(gg, ir)
    assert _dedup("x::DOUBLE AS d, x AS i") is True             # scalar double → local
    assert _dedup("{'a': x::DOUBLE} AS s, x AS i") is True      # NESTED double in a struct → local (raw types)
    assert _dedup("[x::DOUBLE] AS lst, x AS i") is True         # list of double → local
    assert _dedup("x AS i, (x*10) AS j") is False              # int-only → distributes
    assert _dedup("x::DECIMAL(18,2) AS dec, x AS i") is False   # DECIMAL is exact (no -0.0/NaN) → distributes

    # (3c) a genuinely non-distributable op (a raw sql node) → can_run False → run() delegates to the
    # DuckDB base runner (never touches Ray) → it completes.
    dirty = G([_ray_node("src", "source", {"uri": p}),
               _ray_node("s", "sql", {"sql": "SELECT * FROM input WHERE x > 2"}),
               _ray_node("w", "write", {"name": "sql_out"})], [_ray_edge("src", "s"), _ray_edge("s", "w")])
    dplan = compile_plan(dirty, "w", deps.registry, deps.node_specs)
    assert rr.can_run(dplan) is False

    st = rr.run(dplan, dirty, "w", "local")
    for _ in range(300):
        if deps.runner.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert deps.runner.status(st.run_id).status == "done"

    # a library transform with no inlined code isn't Ray-runnable (a worker can't reach the driver registry)
    libg = G([_ray_node("src", "source", {"uri": "x.parquet"}),
              _ray_node("m", "transform", {"mode": "map", "source": "library", "processor": "p1"}),
              _ray_node("w", "write", {"name": "o"})], [_ray_edge("src", "m"), _ray_edge("m", "w")])
    assert rr._ray_runnable(lower_to_ir(libg, "w", deps.node_specs)) is False


def test_ray_sink_contract_matches_local_without_a_live_cluster(tmp_path, monkeypatch):
    """Ray's driver-side commit must use the local sink contract even when no Ray cluster is available."""
    import json
    import re

    import duckdb
    import pyarrow.parquet as pq

    from hub import destinations
    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    workspace = tmp_path / "ws"
    workspace.mkdir()
    local_root, ray_root = tmp_path / "local-dest", tmp_path / "ray-dest"
    (local_root / "daily/2026").mkdir(parents=True)
    (ray_root / "daily/2026").mkdir(parents=True)
    roots = {"local-test": local_root, "ray-test": ray_root}

    def target_uri(_workspace, destination_id, path, filename):
        return str(roots[destination_id] / path / os.path.basename(filename))

    monkeypatch.setattr(destinations, "target_uri", target_uri)
    deps = Deps(str(workspace), str(tmp_path / "data"))
    mod = _load_dp_ray()
    ray_runner = mod.RayRunner(deps)
    source_uri = str(tmp_path / "input.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES ('a', 1), ('b', 2), ('a', 3)) t(cat, value)) "
        f"TO '{source_uri}' (FORMAT PARQUET)"
    )
    source_table = pq.read_table(source_uri)

    class Dataset:
        def __init__(self, table, *, no_blocks=False):
            self.table = table
            self.no_blocks = no_blocks
            self.iterated = False
            self.writes = []

        def materialize(self):
            return self

        def size_bytes(self):
            return self.table.nbytes

        def count(self):
            return self.table.num_rows

        def iter_batches(self, batch_format):
            assert batch_format == "pyarrow"
            self.iterated = True
            return iter(()) if self.no_blocks else iter((self.table,))

        def schema(self):
            return self.table.schema

        def write_parquet(self, path, **kwargs):
            os.makedirs(path, exist_ok=True)
            pq.write_table(self.table, os.path.join(path, "part-000000.parquet"))
            self.writes.append({"path": path, **kwargs})

    def graph(destination_id, filename, *, mode="overwrite", partition_by="", empty=False,
              output_format="parquet"):
        nodes = [_ray_node("src", "source", {"uri": source_uri})]
        edges = []
        parent = "src"
        if empty:
            nodes.append(_ray_node("f", "transform", {
                "mode": "filter", "code": "def fn(row):\n    return False",
            }))
            edges.append(_ray_edge("src", "f"))
            parent = "f"
        nodes.append(_ray_node("w", "write", {
            "filename": filename, "format": output_format, "writeMode": mode,
            "destId": destination_id, "destPath": "daily/2026", "partitionBy": partition_by,
        }))
        edges.append(_ray_edge(parent, "w"))
        return Graph(**{"id": f"{destination_id}-{filename}", "version": 1,
                        "nodes": nodes, "edges": edges})

    def wait_local(gr):
        status = deps.runner.run(
            compile_plan(gr, "w", deps.registry, deps.node_specs), gr, "w", "local"
        )
        for _ in range(300):
            status = deps.runner.status(status.run_id)
            if status.status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.02)
        assert status.status == "done", status.error
        return status

    def ray_commit(gr, table, *, no_blocks=False):
        ir = lower_to_ir(gr, "w", deps.node_specs)
        write = ir.by_id()["w"]
        parent = write.inputs[0][0]
        target = ray_runner._resolve_sink_targets(ir)["w"]
        committed = ray_runner._commit(write, {parent: Dataset(table, no_blocks=no_blocks)}, target)
        ray_runner._register_outputs(gr, {"outputs": [
            {"step_id": write.id, "name": committed[2], "uri": committed[1]},
        ]})
        return committed

    # Destination, nested path, format, partitionBy, and overwrite are identical to the local contract.
    local_partitioned = graph("local-test", "local_partitioned.parquet", partition_by="cat")
    local_status = wait_local(local_partitioned)
    local_partitioned_uri = _output_field(local_status, "uri", outcome="committed")
    ray_partitioned = graph("ray-test", "ray_partitioned.parquet", partition_by="cat")
    ray_rows, ray_uri, _ = ray_commit(ray_partitioned, source_table)
    assert ray_rows == 3
    assert local_partitioned_uri == str(local_root / "daily/2026/local_partitioned")
    assert ray_uri == str(ray_root / "daily/2026/ray_partitioned")
    scan = lambda uri: sorted(deps.resolve_adapter(uri).scan(uri).fetchall())  # noqa: E731
    assert scan(local_partitioned_uri) == scan(ray_uri)

    # With no filename extension, format chooses the same physical extension on both backends.
    local_csv = wait_local(graph("local-test", "local_csv", output_format="csv"))
    local_csv_uri = _output_field(local_csv, "uri", outcome="committed")
    _, ray_csv_uri, _ = ray_commit(graph("ray-test", "ray_csv", output_format="csv"), source_table)
    assert local_csv_uri.endswith("/local_csv.csv") and ray_csv_uri.endswith("/ray_csv.csv")
    assert scan(local_csv_uri) == scan(ray_csv_uri)

    # Append remains append (not an implicit overwrite) and returns the adapter's parts-directory URI.
    local_append = graph("local-test", "local_append.parquet", mode="append")
    local_append_uri = _output_field(wait_local(local_append), "uri", outcome="committed")
    wait_local(local_append)
    ray_append = graph("ray-test", "ray_append.parquet", mode="append")
    _, ray_append_uri, _ = ray_commit(ray_append, source_table)
    _, ray_append_uri, _ = ray_commit(ray_append, source_table)
    assert len(scan(local_append_uri)) == len(scan(ray_append_uri)) == 6
    assert scan(local_append_uri) == scan(ray_append_uri)

    # An empty Ray Dataset can expose zero blocks; commit reconstructs a zero-row table from its schema.
    local_empty = graph("local-test", "local_empty.parquet", empty=True)
    local_empty_uri = _output_field(wait_local(local_empty), "uri", outcome="committed")
    empty_table = mod._make_mapper({
        "mode": "filter", "code": "def fn(row):\n    return False", "onError": "raise",
    })(source_table)
    assert empty_table.num_rows == 0 and empty_table.schema == source_table.schema
    ray_empty = graph("ray-test", "ray_empty.parquet", empty=True)
    empty_rows, ray_empty_uri, ray_empty_name = ray_commit(ray_empty, empty_table, no_blocks=True)
    assert empty_rows == 0
    con = duckdb.connect()
    local_schema = [(r[0], str(r[1])) for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{local_empty_uri}')"
    ).fetchall()]
    ray_schema = [(r[0], str(r[1])) for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{ray_empty_uri}/**/*.parquet')"
    ).fetchall()]
    assert ray_schema == local_schema == [("cat", "VARCHAR"), ("value", "INTEGER")]
    version = deps.catalog.get_table(ray_empty_name).version
    assert version != "v1" and re.fullmatch(r"v[0-9a-f]{10}", version)

    # The hub resolves destId exactly once. An isolated driver with no control-plane settings can commit
    # from the physical URI in its job without calling destinations.target_uri again.
    driver_graph = graph("ray-test", "driver_resolved.parquet")
    driver_ir = lower_to_ir(driver_graph, "w", deps.node_specs)
    resolved = ray_runner._resolve_sink_targets(driver_ir)

    def no_destination_settings(*_args, **_kwargs):
        raise AssertionError("driver read destination settings")

    monkeypatch.setattr(destinations, "target_uri", no_destination_settings)
    direct_dataset = Dataset(source_table)
    monkeypatch.setattr(ray_runner, "_build", lambda *_args, **_kwargs: direct_dataset)
    result = ray_runner._run_ir_sync(
        driver_ir, driver_graph, "w", sink_targets=resolved, attempt_id="whole-test"
    )
    assert result["status"] == "done", result.get("error")
    ray_runner._register_outputs(driver_graph, result)
    assert result["rows"] == 3
    assert result["output_uri"] == mod._attempt_handoff_uri(
        resolved["w"], "whole-test", scope="w"
    )
    assert result["outputs"][0]["logical_uri"] == resolved["w"]
    assert direct_dataset.iterated is False
    assert direct_dataset.writes[0]["path"] == result["output_uri"]
    with open(os.path.join(result["output_uri"], "_DP_SUCCESS.json")) as manifest_file:
        assert json.load(manifest_file)["runId"] == "whole-test"
    assert deps.catalog.get_table(result["output_table"]).uri == result["output_uri"]


def test_ray_worker_direct_attempt_is_create_only_and_empty_retry_cannot_publish_stale_shards(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.handoff import read_manifest, validate_shards, write_manifest

    mod = _load_dp_ray()

    class Dataset:
        def __init__(self, table):
            self.table = table
            self.materialized = False

        def materialize(self):
            self.materialized = True
            return self

        def count(self):
            return self.table.num_rows

        def schema(self):
            return self.table.schema

        def write_parquet(self, path, **_kwargs):
            os.makedirs(path, exist_ok=True)
            pq.write_table(self.table, os.path.join(path, "part-000000.parquet"))

    schema = pa.schema([("x", pa.int64())])
    committed_uri = str(tmp_path / "whole.attempt-same")
    first = Dataset(pa.table({"x": pa.array([1, 2], type=pa.int64())}))
    assert mod._write_worker_direct_parquet(first, committed_uri, attempt_id="same") == (2, committed_uri)
    manifest = read_manifest(committed_uri)
    assert manifest is not None and validate_shards(committed_uri, manifest)
    original = pq.read_table(os.path.join(committed_uri, "part-000000.parquet")).column("x").to_pylist()

    empty_retry = Dataset(pa.Table.from_batches([], schema=schema))
    assert mod._write_worker_direct_parquet(
        empty_retry, committed_uri, attempt_id="same"
    ) == (2, committed_uri)
    assert empty_retry.materialized is False, "a committed retry must reattach without executing/writing"
    assert pq.read_table(os.path.join(committed_uri, "part-000000.parquet")).column("x").to_pylist() == original

    wrong_uri = str(tmp_path / "whole.attempt-wrong-run")
    os.makedirs(wrong_uri)
    pq.write_table(pa.table({"x": [7]}), os.path.join(wrong_uri, "part.parquet"))
    write_manifest(wrong_uri, run_id="different-run", rows=1, schema=schema)
    wrong_retry = Dataset(pa.Table.from_batches([], schema=schema))
    with pytest.raises(RuntimeError, match="already exists without an exact committed inventory"):
        mod._write_worker_direct_parquet(wrong_retry, wrong_uri, attempt_id="expected-run")
    assert wrong_retry.materialized is False

    partial_uri = str(tmp_path / "whole.attempt-partial")
    os.makedirs(partial_uri)
    pq.write_table(pa.table({"x": [99]}), os.path.join(partial_uri, "stale.parquet"))
    partial_retry = Dataset(pa.Table.from_batches([], schema=schema))
    with pytest.raises(RuntimeError, match="already exists without an exact committed inventory"):
        mod._write_worker_direct_parquet(partial_retry, partial_uri, attempt_id="partial")
    assert partial_retry.materialized is False
    assert pq.read_table(os.path.join(partial_uri, "stale.parquet")).column("x").to_pylist() == [99]

    unknown_uri = str(tmp_path / "whole.attempt-unknown")
    os.makedirs(unknown_uri)
    with open(os.path.join(unknown_uri, "writer.tmp"), "wb") as marker:
        marker.write(b"partial")
    with pytest.raises(RuntimeError, match="already exists without an exact committed inventory"):
        mod._write_worker_direct_parquet(
            Dataset(pa.Table.from_batches([], schema=schema)), unknown_uri, attempt_id="unknown"
        )

    class Ray56EmptyDataset(Dataset):
        def schema(self, fetch_if_missing=True):
            return None if self.materialized else self.table.schema

    empty_uri = str(tmp_path / "whole.attempt-empty")
    fresh_empty = Ray56EmptyDataset(pa.Table.from_batches([], schema=schema))
    assert mod._write_worker_direct_parquet(fresh_empty, empty_uri, attempt_id="empty") == (0, empty_uri)
    empty_manifest = read_manifest(empty_uri)
    assert empty_manifest is not None and empty_manifest["rows"] == 0
    assert validate_shards(empty_uri, empty_manifest)
    assert pq.read_table(os.path.join(empty_uri, "part-000000.parquet")).schema == schema


def test_ray_typed_empty_collect_and_broadcast_join_keep_declared_schema(monkeypatch):
    import sys
    from types import SimpleNamespace

    import pyarrow as pa

    mod = _load_dp_ray()

    class ZeroBlockDataset:
        def __init__(self, schema):
            self.declared = schema
            self.materialized = False

        def schema(self, fetch_if_missing=True):
            return None if self.materialized else self.declared

        def materialize(self):
            self.materialized = True
            return self

        def size_bytes(self):
            return 0

        def iter_batches(self, **_kwargs):
            return iter(())

        def to_arrow_refs(self):
            return []

    collected_schema = pa.schema([("k", pa.int64()), ("value", pa.string())])
    collected = mod._collect_arrow(ZeroBlockDataset(collected_schema), purpose="typed-empty test")
    assert collected.num_rows == 0 and collected.schema == collected_schema

    class ResultDataset:
        def __init__(self, table):
            self.table = table

        def schema(self, fetch_if_missing=True):
            return self.table.schema

        def to_pylist(self):
            return self.table.to_pylist()

    class LeftDataset:
        def columns(self):
            return ["k", "left_value", "extra"]

        def schema(self, fetch_if_missing=True):
            return pa.schema([
                ("k", pa.int64()), ("left_value", pa.string()), ("extra", pa.int64()),
            ])

        def map_batches(self, fn, **_kwargs):
            return ResultDataset(fn(pa.table({
                "k": [1], "left_value": ["left"], "extra": [11],
            })))

    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(get=lambda _refs: []))
    right_schema = pa.schema([("k", pa.int64()), ("right_value", pa.string())])
    step = SimpleNamespace(
        inputs=[("left", None), ("right", None)],
        config={"on": "k", "how": "left"},
    )
    left = mod._remember_ray_schema(LeftDataset(), pa.schema([("k", pa.int64())]))
    joined = object.__new__(mod.RayRunner)._build_join(
        step, {"left": left, "right": ZeroBlockDataset(right_schema)}
    )
    assert joined.schema() == pa.schema([
        ("k", pa.int64()), ("left_value", pa.string()), ("extra", pa.int64()),
        ("right_value", pa.string()),
    ])
    assert joined.to_pylist() == [{
        "k": 1, "left_value": "left", "extra": 11, "right_value": None,
    }]


def test_ray_empty_schema_lineage_covers_supported_ops_and_declared_udfs(monkeypatch):
    import sys
    from types import SimpleNamespace

    import pyarrow as pa

    mod = _load_dp_ray()
    runner = object.__new__(mod.RayRunner)
    monkeypatch.setenv("DP_RAY_SHUFFLE_PARTITIONS", "2")
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(get=lambda refs: refs))

    class SchemaDroppingDataset:
        def __init__(self, refs=None):
            self.refs = list(refs or [])

        def schema(self, fetch_if_missing=True):
            return None

        def materialize(self):
            return self

        def count(self):
            return 0

        def size_bytes(self):
            return 0

        def iter_batches(self, **_kwargs):
            return iter(())

        def to_arrow_refs(self):
            return self.refs

        def map_batches(self, _fn, **_kwargs):
            return SchemaDroppingDataset()

        def repartition(self, *_args, **_kwargs):
            return SchemaDroppingDataset()

        def sort(self, *_args, **_kwargs):
            return SchemaDroppingDataset(refs=[pa.table({})])

    source_schema = pa.schema([("k", pa.int64()), ("v", pa.int64())])

    def source():
        return mod._remember_ray_schema(SchemaDroppingDataset(), source_schema)

    filter_step = SimpleNamespace(
        op="filter", inputs=[("src", None)],
        config={"mode": "filter", "code": "def fn(row): return False", "onError": "raise"},
    )

    def empty_parent():
        return runner._build(filter_step, {"src": source()})

    assert mod._collect_arrow(empty_parent(), purpose="empty filter").schema == source_schema
    relational = [
        (SimpleNamespace(op="aggregate", inputs=[("src", None)],
                         config={"groupBy": "k", "aggs": "count(*) AS n"}), ["k", "n"]),
        (SimpleNamespace(op="window", inputs=[("src", None)],
                         config={"partitionBy": "k", "orderBy": "v",
                                 "expr": "row_number()", "as": "rn"}), ["k", "v", "rn"]),
        (SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}), ["k", "v"]),
        (SimpleNamespace(op="sort", inputs=[("src", None)], config={"by": "k"}), ["k", "v"]),
    ]
    for step, names in relational:
        table = mod._collect_arrow(runner._build(step, {"src": empty_parent()}), purpose=step.op)
        assert table.num_rows == 0 and table.schema.names == names

    right_schema = pa.schema([("k", pa.int64()), ("right_value", pa.string())])
    right = mod._remember_ray_schema(SchemaDroppingDataset(refs=[pa.table({})]), right_schema)
    join_step = SimpleNamespace(
        inputs=[("left", None), ("right", None)], config={"on": "k", "how": "left"},
    )
    joined = runner._build_join(join_step, {"left": empty_parent(), "right": right})
    assert mod._collect_arrow(joined, purpose="empty join").schema == pa.schema([
        ("k", pa.int64()), ("v", pa.int64()), ("right_value", pa.string()),
    ])

    declared = SimpleNamespace(
        op="flat_map", inputs=[("src", None)],
        config={
            "mode": "flat_map", "code": "def fn(row): return []", "onError": "raise",
            "outputSchema": [{"name": "renamed", "type": "int"}],
        },
    )
    declared_table = mod._collect_arrow(
        runner._build(declared, {"src": source()}), purpose="declared empty map"
    )
    assert declared_table.schema == pa.schema([("renamed", pa.int64())])

    undeclared = SimpleNamespace(
        op="flat_map", inputs=[("src", None)],
        config={"mode": "flat_map", "code": "def fn(row): return []", "onError": "raise"},
    )
    undeclared_dataset = runner._build(undeclared, {"src": source()})
    with pytest.raises(RuntimeError, match="did not expose an Arrow schema"):
        mod._collect_arrow(undeclared_dataset, purpose="unknown empty map")
    with pytest.raises(RuntimeError, match="empty Ray dedup input did not expose"):
        runner._build(
            SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}),
            {"src": undeclared_dataset},
        )


def test_ray_ir_carries_transform_output_schema_for_empty_results():
    from hub.ir import lower_to_ir
    from hub.models import Graph

    deps = get_deps()
    contract = [{"name": "renamed", "type": "int", "capabilities": []}]
    graph = Graph(**{"id": "ray-schema-contract", "version": 1, "nodes": [
        N("s", "source", {"uri": _uri("events")}),
        N("t", "transform", {
            "mode": "flat_map", "code": "def fn(row): return []", "outputSchema": contract,
            "enforceSchema": True,
        }),
    ], "edges": [E("s", "t")]})
    step = lower_to_ir(graph, "t", deps.node_specs).by_id()["t"]
    assert step.config["outputSchema"] == contract
    assert step.config["enforceSchema"] is True
    assert "distributed schema enforcement is not implemented" in (
        object.__new__(_load_dp_ray().RayRunner)._ray_unsupported_reason(
            lower_to_ir(graph, "t", deps.node_specs)
        ) or ""
    )


@pytest.fixture
def ray_catalog_object_store(object_store_cred):
    """A real versioned S3-compatible provider for Ray catalog publication tests."""
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1",
        )
        client.create_bucket(Bucket="ray-catalog-lifecycle")
        client.put_bucket_versioning(
            Bucket="ray-catalog-lifecycle", VersioningConfiguration={"Status": "Enabled"})
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
        yield client
    finally:
        object_store_cred(None)
        server.stop()


def test_ray_sink_attempt_retention_runs_only_after_catalog_publication(
        tmp_path, monkeypatch, ray_catalog_object_store):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import metadb
    from hub.deps import Deps
    from hub import handoff
    from hub.models import Graph
    from hub.plugins.adapters import object_fs

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    class ParquetProbe:
        def schema(self, _uri):
            from hub.models import ColumnSchema
            return [ColumnSchema(name="x", type="VARCHAR")]

        def count(self, _uri):
            return 1

        def fingerprint(self, _uri):
            return "ray-publication-test"

    monkeypatch.setattr(runner.catalog, "resolve", lambda _uri: ParquetProbe())
    graph = Graph(**{"id": "sink-gc-order", "version": 1, "nodes": [
        _ray_node("w", "write", {"filename": "out.parquet", "writeMode": "overwrite"}),
    ], "edges": []})
    logical = "s3://ray-catalog-lifecycle/outputs/out.parquet"
    first = "s3://ray-catalog-lifecycle/outputs/out.attempt-catalog-first"
    second = "s3://ray-catalog-lifecycle/outputs/out.attempt-catalog-second"
    failed = "s3://ray-catalog-lifecycle/outputs/out.attempt-catalog-failed"
    for uri, run_id in ((first, "catalog-first"), (second, "catalog-second"),
                        (failed, "catalog-failed")):
        metadb.allocate_object_attempt(
            logical_uri=logical, kind="sink", run_id=run_id,
            allocation_key=f"ray-catalog-{run_id}", catalog_key_base="tbl_out",
            uri_factory=lambda _namespace, _generation, _attempt_id, uri=uri: uri)
        fs, path = object_fs(uri)
        table = pa.table({"x": [run_id]})
        with fs.open_output_stream(path + "/part-00000.parquet") as stream:
            pq.write_table(table, stream)
        handoff.write_manifest(uri, run_id=run_id, rows=1, schema=table.schema)

    runner._register_outputs(graph, {"outputs": [
        {"step_id": "w", "name": "out", "uri": first, "logical_uri": logical},
    ]})
    stable_id = metadb.catalog_get(first)["id"]
    runner._register_outputs(graph, {"outputs": [
        {"step_id": "w", "name": "out", "uri": second, "logical_uri": logical},
    ]})
    assert metadb.catalog_get(first)["uri"] == second
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, first) is None
    assert metadb.catalog_get(second)["id"] == stable_id
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, first).state == "superseded"
        assert session.get(metadb.ObjectAttempt, second).state == "published"

    # The writer has finished and the controller has captured exact inventory; only publication fails.
    handoff.prepare_attempt_commit(failed)
    monkeypatch.setattr(
        metadb, "catalog_upsert_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )
    with pytest.raises(RuntimeError, match="atomically publish"):
        runner._register_outputs(graph, {"outputs": [
            {"step_id": "w", "name": "out", "uri": failed, "logical_uri": logical},
        ]})
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, failed).state == "abandoned"
        assert session.get(metadb.ObjectAttempt, second).state == "published"

    metadb.catalog_delete_entry(second)
    for uri in (first, second, failed):
        metadb.quarantine_object_attempt(uri, "test cleanup")


def test_ray_sink_swallowed_catalog_persist_failure_does_not_publish(
        tmp_path, monkeypatch, ray_catalog_object_store):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import metadb
    from hub.deps import Deps
    from hub import handoff
    from hub.models import Graph
    from hub.plugins.adapters import object_fs

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    class ParquetProbe:
        def schema(self, _uri):
            from hub.models import ColumnSchema
            return [ColumnSchema(name="x", type="VARCHAR")]

        def count(self, _uri):
            return 1

        def fingerprint(self, _uri):
            return "ray-publication-test"

    monkeypatch.setattr(runner.catalog, "resolve", lambda _uri: ParquetProbe())
    graph = Graph(**{"id": "sink-persist-failure", "version": 1, "nodes": [
        _ray_node("w", "write", {"filename": "out.parquet", "writeMode": "overwrite"}),
    ], "edges": []})
    attempt = "s3://ray-catalog-lifecycle/outputs/out.attempt-persist-failure"
    metadb.catalog_delete_entry(attempt)
    metadb.allocate_object_attempt(
        logical_uri="s3://ray-catalog-lifecycle/outputs/out.parquet", kind="sink",
        run_id="persist-failure", allocation_key="ray-catalog-persist-failure",
        catalog_key_base="tbl_out",
        uri_factory=lambda _namespace, _generation, _attempt_id: attempt)
    fs, path = object_fs(attempt)
    table = pa.table({"x": ["persist-failure"]})
    with fs.open_output_stream(path + "/part-00000.parquet") as stream:
        pq.write_table(table, stream)
    handoff.write_manifest(
        attempt, run_id="persist-failure", rows=1, schema=table.schema)

    monkeypatch.setattr(
        metadb, "catalog_upsert_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database unavailable SECRET_SENTINEL")),
    )
    with pytest.raises(RuntimeError, match="atomically publish") as raised:
        runner._register_outputs(graph, {"outputs": [{
            "step_id": "w", "name": "out", "uri": attempt,
            "logical_uri": "s3://ray-catalog-lifecycle/outputs/out.parquet",
        }]})
    assert "SECRET_SENTINEL" not in str(raised.value)
    assert metadb.catalog_get(attempt) is None
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, attempt).state == "abandoned"
    metadb.quarantine_object_attempt(attempt, "test cleanup")


def test_core_managed_publisher_fails_closed_on_unreadable_output(
        tmp_path, monkeypatch, ray_catalog_object_store):
    from hub import handoff, metadb
    from hub.deps import Deps
    from hub.plugins.adapters import object_fs

    logical = "s3://ray-catalog-lifecycle/outputs/unreadable.parquet"
    attempt = "s3://ray-catalog-lifecycle/outputs/unreadable.attempt-probe"
    metadb.allocate_object_attempt(
        logical_uri=logical, kind="sink", run_id="unreadable-probe",
        allocation_key="unreadable-probe", catalog_key_base="tbl_unreadable",
        uri_factory=lambda _namespace, _generation, _attempt_id: attempt)
    fs, path = object_fs(attempt)
    with fs.open_output_stream(path + "/part-00000.parquet") as stream:
        stream.write(b"not-parquet-but-terminal")
    handoff.write_manifest(attempt, run_id="unreadable-probe", rows=1, schema="x: string")

    catalog = Deps(str(tmp_path / "ws"), str(tmp_path / "data")).catalog

    class BrokenProbe:
        def schema(self, _uri):
            raise RuntimeError("schema unavailable")

        def count(self, _uri):
            raise RuntimeError("count unavailable")

    monkeypatch.setattr(catalog, "resolve", lambda _uri: BrokenProbe())
    with pytest.raises(RuntimeError, match="schema/count probe failed"):
        catalog.publish_managed_output("unreadable", attempt)
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, attempt).state == "abandoned"
    metadb.quarantine_object_attempt(attempt, "test cleanup")


@pytest.mark.parametrize(("suffix", "marker_key", "marker_body", "should_commit"), [
    ("empty", "/", b"", True),
    ("nonempty", "/", b"not-a-directory-marker", False),
    ("nested", "/nested/", b"", False),
])
def test_managed_attempt_inventory_allows_only_empty_exact_root_marker(
        tmp_path, ray_catalog_object_store, suffix, marker_key, marker_body, should_commit):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import handoff, metadb
    from hub.plugins.adapters import object_fs

    logical = f"s3://ray-catalog-lifecycle/outputs/root-marker-{suffix}.parquet"
    attempt = f"s3://ray-catalog-lifecycle/outputs/root-marker-{suffix}.attempt-test"
    metadb.allocate_object_attempt(
        logical_uri=logical, kind="sink", run_id=f"root-marker-{suffix}",
        allocation_key=f"root-marker-{suffix}", catalog_key_base=f"tbl_root_marker_{suffix}",
        uri_factory=lambda _namespace, _generation, _attempt_id: attempt)
    fs, path = object_fs(attempt)
    bucket, key = path.split("/", 1)
    ray_catalog_object_store.put_object(
        Bucket=bucket, Key=key.rstrip("/") + marker_key, Body=marker_body)
    table = pa.table({"x": [1]})
    with fs.open_output_stream(path.rstrip("/") + "/part-00000.parquet") as stream:
        pq.write_table(table, stream)
    handoff.write_manifest(
        attempt, run_id=f"root-marker-{suffix}", rows=1, schema=table.schema)

    if should_commit:
        handoff.prepare_attempt_commit(attempt)
        inventory = metadb.object_attempt_inventory(attempt)
        assert any(item["key"] == path.rstrip("/") + "/" and item["size"] == 0
                   for item in inventory)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, attempt).state == "committed"
    else:
        with pytest.raises(RuntimeError, match="inventory could not be proven"):
            handoff.prepare_attempt_commit(attempt)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, attempt).state == "quarantined"
    metadb.quarantine_object_attempt(attempt, "test cleanup")


def test_ray_managed_publisher_preflight_rejects_lookalike_custom_catalog(tmp_path, monkeypatch):
    import inspect

    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))

    class LookalikeCatalog:
        def publish_managed_output(self, **_kwargs):
            raise AssertionError("custom publisher must not receive lifecycle authority")

    runner.catalog = LookalikeCatalog()
    graph = Graph(**{"id": "external-managed-preflight", "version": 1, "nodes": [
        _ray_node("w", "write", {"filename": "out.parquet", "writeMode": "overwrite"}),
    ], "edges": []})
    ir = lower_to_ir(graph, "w", runner.node_specs, runner.deps.node_ir)
    allocated = []
    monkeypatch.setattr(mod, "allocate_attempt", lambda **kwargs: allocated.append(kwargs))
    with pytest.raises(RuntimeError, match="core transactional catalog publisher"):
        runner._claim_sink_attempts(
            ir, {"w": "s3://external-managed/outputs/out.parquet"}, "external-preflight")
    assert allocated == []
    assert "metadb" not in inspect.getsource(runner._register_outputs)


def test_ray_rejects_any_multiple_write_sinks_before_allocation(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    graph = Graph(**{"id": "ray-multi-write", "version": 1, "nodes": [
        _ray_node("first", "write", {"filename": "first.csv"}),
        _ray_node("second", "write", {"filename": "second.csv"}),
    ], "edges": []})
    ir = lower_to_ir(graph, "second", runner.node_specs, runner.deps.node_ir)
    allocations = []
    monkeypatch.setattr(mod, "allocate_attempt", lambda **kwargs: allocations.append(kwargs))
    with pytest.raises(RuntimeError, match="multiple Ray write sinks"):
        runner._claim_sink_attempts(
            ir, {"first": "/tmp/first.csv", "second": "/tmp/second.csv"}, "run")
    assert allocations == []


def test_ray_unmanaged_publication_attests_external_catalog_and_all_join_lineage_across_region_cut(
        tmp_path):
    from hub import graph as graph_mod, metadb
    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec
    from hub.planner import Region

    runner = _load_dp_ray().RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    source_a = str(tmp_path / "a.parquet")
    source_b = str(tmp_path / "b.parquet")
    output = str(tmp_path / "joined.csv")
    registered = {}
    readbacks = []

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            registered.update(kwargs)
            return {"uri": kwargs["uri"], "name": kwargs["name"], "version": "v1"}

        @staticmethod
        def get_table(uri):
            readbacks.append(uri)
            return {"uri": uri, "name": registered["name"], "version": "v1"}

    runner.catalog = Catalog()
    graph = Graph(**{"id": "ray-lineage", "version": 1, "nodes": [
        _ray_node("a", "source", {"uri": source_a}),
        _ray_node("b", "source", {"uri": source_b}),
        _ray_node("join", "join", {}),
        _ray_node("transform", "transform", {}),
        _ray_node("write", "write", {"filename": "joined.csv"}),
    ], "edges": [
        {"id": "a-join", "source": "a", "target": "join",
         "targetHandle": "a", "data": {"wire": "dataset"}},
        {"id": "b-join", "source": "b", "target": "join",
         "targetHandle": "b", "data": {"wire": "dataset"}},
        _ray_edge("join", "transform"),
        _ray_edge("transform", "write"),
    ]})
    expected = graph_mod.all_upstream_source_uris(graph, "write")
    region = Region(
        id="final", node_ids={"write"}, output_node="write", backend="default",
        worker=None, requires=ResourceSpec(),
        cut_inputs=[("transform", None, "write", None)],
    )
    graph = runner.deps.controller._subgraph(
        graph, region, {"transform": str(tmp_path / "region-ref.parquet")})
    graph._publication_run_id = "outer-run"
    runner._register_outputs(graph, {"outputs": [{
        "step_id": "write", "name": "joined", "uri": output,
        "logical_uri": output,
    }]}, expected_targets={"write": output}, expected_attempts={},
        backend_run_id="ray-subrun")

    assert set(expected) == {source_a, source_b}
    assert graph_mod.execution_source_uris(graph, "write") == [str(tmp_path / "region-ref.parquet")]
    assert registered["parents"] == metadb.catalog_lineage_parent_tokens(expected)
    assert registered["lineage"].run_id == "outer-run"
    assert registered["lineage"].attempt_id == "ray-subrun"
    assert registered["lineage"].producer == "ray-lineage"
    assert readbacks == [output]


def test_ray_settlement_logs_provider_detail_but_returns_generic_error(
        tmp_path, monkeypatch, caplog):
    from hub.deps import Deps
    from hub.models import Graph, RunStatus

    runner = _load_dp_ray().RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    monkeypatch.setattr(
        runner, "_register_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SECRET_RAY_CATALOG_DETAIL")))
    caplog.set_level("ERROR")
    status = RunStatus(run_id="run", status="running", per_node=[])
    runner._settle_popen_result(
        Graph(id="ray-generic", version=1, nodes=[], edges=[]), status,
        {"status": "done", "rows": 0, "outputs": [{"step_id": "write"}]}, 0)
    assert status.status == "failed"
    assert status.error == (
        "Catalog registration failed "
        "(code=catalog_registration_failed,type=RuntimeError)"
    )
    assert "SECRET_RAY_CATALOG_DETAIL" not in status.error
    assert "SECRET_RAY_CATALOG_DETAIL" in caplog.text


def test_ray_control_plane_setup_and_preflight_errors_are_generic(
        tmp_path, monkeypatch, caplog):
    from hub.deps import Deps
    from hub.models import CompilePlan, Graph, PlanStep

    runner = _load_dp_ray().RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    graph = Graph(**{"id": "ray-control-error", "version": 1, "nodes": [
        _ray_node("write", "write", {"filename": "out.csv"}),
    ], "edges": []})
    plan = CompilePlan(target_node_id="write", steps=[
        PlanStep(node_id="write", kind="write", label="write"),
    ])
    monkeypatch.setattr(runner, "_ray_unsupported_reason", lambda _ir: None)
    monkeypatch.setattr(runner, "_dedup_unsupported_reason", lambda _graph, _ir: None)
    monkeypatch.setattr(runner, "_resource_unsupported_reason", lambda _requires, _ir: None)
    monkeypatch.setattr(
        runner, "_source_unsupported_reason", lambda _graph, _target, _ir: None)
    monkeypatch.setattr(runner, "_resolve_sink_targets", lambda _ir: {"write": "/tmp/out.csv"})
    monkeypatch.setattr(
        runner, "_claim_sink_attempts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SECRET_RAY_ALLOCATION_TOKEN")))
    caplog.set_level("ERROR")
    failed = runner.run(plan, graph, "write", "distributed")
    assert failed.status == "failed"
    assert failed.error == (
        "Object sink attempt allocation failed "
        "(code=sink_attempt_allocation_failed,type=RuntimeError)"
    )
    assert "SECRET_RAY_ALLOCATION_TOKEN" not in failed.error
    assert "SECRET_RAY_ALLOCATION_TOKEN" in caplog.text

    monkeypatch.setattr(
        runner, "_resolve_sink_targets",
        lambda _ir: (_ for _ in ()).throw(
            RuntimeError("s3://user:SECRET_URI_PASSWORD@example/out")))
    monkeypatch.setattr(runner, "_requires_ray", lambda *_args, **_kwargs: True)
    unsupported = runner.run(
        plan, graph, "write", "distributed", run_id="ray-preflight-secret")
    assert unsupported.status == "failed"
    assert "sink preflight failed" in (unsupported.error or "")
    assert "SECRET_URI_PASSWORD" not in (unsupported.error or "")
    assert "SECRET_URI_PASSWORD" in caplog.text


def test_ray_whole_run_scopes_each_sink_attempt_by_step_and_unstripped_target(tmp_path):
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.deps import Deps
    from hub.handoff import read_manifest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))

    class Dataset:
        def __init__(self, value):
            self.table = pa.table({"x": [value]})
            self.written = False

        def materialize(self):
            return self

        def count(self):
            return 1

        def schema(self):
            return self.table.schema

        def write_parquet(self, path, **_kwargs):
            self.written = True
            os.makedirs(path, exist_ok=True)
            pq.write_table(self.table, os.path.join(path, "part.parquet"))

    target_parquet = str(tmp_path / "out.parquet")
    target_pq = str(tmp_path / "out.pq")
    first_ds, second_ds = Dataset(1), Dataset(2)
    first_step = SimpleNamespace(
        id="write-a", op="write", inputs=[("src", None)],
        config={"filename": "out.parquet", "writeMode": "overwrite"},
    )
    second_step = SimpleNamespace(
        id="write-b", op="write", inputs=[("src", None)],
        config={"filename": "out.pq", "writeMode": "overwrite"},
    )
    first = runner._commit(
        first_step, {"src": first_ds}, target_parquet, attempt_id="whole-shared"
    )
    second = runner._commit(
        second_step, {"src": second_ds}, target_pq, attempt_id="whole-shared"
    )

    assert first_ds.written and second_ds.written
    assert first[1] == mod._attempt_handoff_uri(target_parquet, "whole-shared", scope="write-a")
    assert second[1] == mod._attempt_handoff_uri(target_pq, "whole-shared", scope="write-b")
    assert first[1] != second[1], "same-base sink attempts must never reattach each other"
    assert pq.read_table(os.path.join(first[1], "part.parquet")).column("x").to_pylist() == [1]
    assert pq.read_table(os.path.join(second[1], "part.parquet")).column("x").to_pylist() == [2]
    assert read_manifest(first[1])["runId"] == read_manifest(second[1])["runId"] == "whole-shared"

    # Even if a caller reuses a step ID, the complete pre-strip logical URI remains in the digest input.
    assert mod._attempt_handoff_uri(target_parquet, "whole-shared", scope="same") != \
        mod._attempt_handoff_uri(target_pq, "whole-shared", scope="same")


def test_ray_native_object_parquet_read_never_scans_through_the_driver(tmp_path, monkeypatch):
    """Only the exact built-in adapter may use the proved, credentials-aware native path."""
    import sys
    from types import ModuleType, SimpleNamespace

    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    from hub.deps import Deps
    from hub.plugins import adapters

    workspace = tmp_path / "ws"
    workspace.mkdir()
    runner = _load_dp_ray().RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    filesystem = pafs.LocalFileSystem()
    read_calls = []
    source_file = tmp_path / "source.parquet"
    prefix = tmp_path / "tenant=must-not-surface" / "upstream.attempt-abc"
    prefix.mkdir(parents=True)
    hive = tmp_path / "true-hive"
    (hive / "cat=alpha").mkdir(parents=True)
    pq.write_table(pa.table({"x": pa.array([1], type=pa.int64())}), source_file)
    pq.write_table(pa.table({"x": pa.array([2], type=pa.int32())}), prefix / "part-000000.parquet")
    pq.write_table(pa.table({"x": pa.array([3], type=pa.int64())}), prefix / "part-000001.parquet")
    pq.write_table(pa.table({"x": pa.array([4], type=pa.int64())}), hive / "cat=alpha" / "part.parquet")

    def object_fs(uri):
        if uri.endswith(".parquet"):
            return filesystem, str(source_file)
        if "true-hive" in uri:
            return filesystem, str(hive)
        return filesystem, str(prefix)

    monkeypatch.setattr(adapters, "object_fs", object_fs)
    ray = ModuleType("ray")
    ray_data = ModuleType("ray.data")
    datasource = ModuleType("ray.data.datasource")

    class Partitioning:
        def __init__(self, style, **kwargs):
            self.style, self.kwargs = style, kwargs

    class DatasetPaths(list):
        pass

    def read_parquet(paths, **kwargs):
        read_calls.append((paths, kwargs))
        return DatasetPaths(paths)

    datasource.Partitioning = Partitioning
    ray_data.datasource = datasource
    ray_data.read_parquet = read_parquet
    ray.data = ray_data
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setitem(sys.modules, "ray.data", ray_data)
    monkeypatch.setitem(sys.modules, "ray.data.datasource", datasource)

    adapter = runner.resolve_adapter("s3://bucket/source.parquet")
    metadata_scans = []

    class MetadataRelation:
        def __init__(self, schema):
            self.schema = schema

        def to_arrow_table(self):
            return pa.Table.from_batches([], schema=self.schema)

    def metadata_scan(uri, *, limit=None, **_kwargs):
        assert limit == 0, "native proof may query adapter metadata but must never scan data rows"
        metadata_scans.append(uri)
        if "true-hive" in uri:
            return MetadataRelation(pa.schema([("x", pa.int64()), ("cat", pa.string())]))
        return MetadataRelation(pa.schema([("x", pa.int64())]))

    monkeypatch.setattr(adapter, "scan", metadata_scan)

    file_result = runner._build(SimpleNamespace(
        op="read", config={"uri": "s3://bucket/source.parquet"},
    ), {})
    prefix_result = runner._build(SimpleNamespace(
        op="read", config={"uri": "s3://bucket/upstream.attempt-abc"},
    ), {})
    hive_result = runner._build(SimpleNamespace(
        op="read", config={"uri": "s3://bucket/true-hive"},
    ), {})
    assert file_result == [str(source_file)]
    assert prefix_result == [str(prefix / "part-000000.parquet"), str(prefix / "part-000001.parquet")]
    assert hive_result == [str(hive / "cat=alpha" / "part.parquet")]
    assert [call[0] for call in read_calls] == [file_result, prefix_result, hive_result]
    assert all(call[1]["filesystem"] is filesystem for call in read_calls)
    assert all(call[1]["partitioning"].style == "hive" for call in read_calls)
    assert all(call[1]["columns"] == call[1]["schema"].names for call in read_calls)
    assert read_calls[0][1]["partitioning"].kwargs["base_dir"] == str(tmp_path)
    assert read_calls[1][1]["partitioning"].kwargs["base_dir"] == str(prefix)
    assert read_calls[1][1]["schema"] == pa.schema([("x", pa.int64())])
    assert read_calls[2][1]["partitioning"].kwargs["base_dir"] == str(hive)
    assert read_calls[2][1]["partitioning"].kwargs["base_dir"] != str(tmp_path)
    assert read_calls[2][1]["schema"] == pa.schema([("x", pa.int64()), ("cat", pa.string())])
    assert read_calls[2][1]["partitioning"].kwargs["field_types"] == {"cat": str}
    assert metadata_scans == [
        "s3://bucket/source.parquet",
        "s3://bucket/upstream.attempt-abc",
        "s3://bucket/true-hive",
    ]


@pytest.mark.parametrize("already_scoped", [False, True])
def test_ray_native_metadata_oracle_does_not_block_unrelated_run_scopes(
        tmp_path, monkeypatch, already_scoped):
    import threading

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter

    mod = _load_dp_ray()
    source = tmp_path / "metadata-oracle.parquet"
    pq.write_table(pa.table({"value": [1]}), source)
    real_scan = DuckDBAdapter.scan
    oracle_started = threading.Event()
    release_oracle = threading.Event()
    errors = []

    def slow_scan(adapter, *args, **kwargs):
        relation = real_scan(adapter, *args, **kwargs)

        class SlowRelation:
            def to_arrow_table(self):
                oracle_started.set()
                if not release_oracle.wait(5):
                    raise TimeoutError("test did not release the metadata oracle")
                return relation.to_arrow_table()

        return SlowRelation()

    monkeypatch.setattr(DuckDBAdapter, "scan", slow_scan)

    def prove_native():
        try:
            if already_scoped:
                with db.run_scope():
                    mod._native_parquet_plan(str(source), DuckDBAdapter())
            else:
                mod._native_parquet_plan(str(source), DuckDBAdapter())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    proof = threading.Thread(target=prove_native)
    proof.start()
    assert oracle_started.wait(5)
    unrelated_entered = threading.Event()

    def unrelated_run():
        with db.run_scope():
            unrelated_entered.set()

    unrelated = threading.Thread(target=unrelated_run)
    unrelated.start()
    entered_before_release = unrelated_entered.wait(2)
    release_oracle.set()
    proof.join(5)
    unrelated.join(5)

    assert entered_before_release, "slow metadata I/O held the global DuckDB base lock"
    assert not proof.is_alive() and not unrelated.is_alive()
    assert errors == []


def test_ray_native_parquet_fragment_selection_matches_adapter_rows(tmp_path):
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    from hub.plugins.adapters import DuckDBAdapter

    mod = _load_dp_ray()
    adapter = DuckDBAdapter()

    names = tmp_path / "fragment-names"
    names.mkdir()
    for relative, value in (
        ("part.parquet", 1),
        ("_late.parquet", 2),
        (".hidden.parquet", 3),
        ("late.PARQUET", 99),
    ):
        pq.write_table(pa.table({"x": [value]}), names / relative)
    names_plan = mod._native_parquet_plan(str(names), adapter)
    adapter_rows = sorted(row[0] for row in adapter.scan(str(names)).project("x").fetchall())
    native_rows = sorted(
        value
        for path in names_plan["paths"]
        for value in pq.read_table(path).column("x").to_pylist()
    )
    assert adapter_rows == native_rows == [1, 2, 3]

    nested = tmp_path / "hidden-nested"
    (nested / ".hidden-dir").mkdir(parents=True)
    pq.write_table(pa.table({"x": [1]}), nested / "part.parquet")
    pq.write_table(pa.table({"x": [2]}), nested / ".hidden-dir" / "part.parquet")
    _fs, root, info, files = mod._file_infos(str(nested))
    assert sorted(mod._native_parquet_fragments(str(nested), root, info, files)) == sorted(
        [str(nested / "part.parquet"), str(nested / ".hidden-dir" / "part.parquet")]
    )
    with pytest.raises(RuntimeError, match="mixes root files and partition directories"):
        mod._native_parquet_plan(str(nested), adapter)

    aliases = tmp_path / "mixed-parquet-aliases"
    aliases.mkdir()
    pq.write_table(pa.table({"x": [10]}), aliases / "part.parquet")
    pq.write_table(pa.table({"x": [20]}), aliases / "part.pq")
    alias_plan = mod._native_parquet_plan(str(aliases), adapter)
    adapter_rows = sorted(row[0] for row in adapter.scan(str(aliases)).project("x").fetchall())
    native_rows = sorted(
        value
        for path in alias_plan["paths"]
        for value in pq.read_table(path).column("x").to_pylist()
    )
    assert adapter_rows == native_rows == [10]

    exact = tmp_path / "exact.PARQUET"
    pq.write_table(pa.table({"x": [30]}), exact)
    exact_plan = mod._native_parquet_plan(str(exact), adapter)
    assert exact_plan["paths"] == [str(exact)]
    assert adapter.scan(str(exact)).project("x").fetchall() == [(30,)]

    # Object prefixes have a different, explicit contract: lowercase .parquet only. An explicit object
    # is still exact regardless of extension because the adapter addresses that one key directly.
    directory = SimpleNamespace(type=pafs.FileType.Directory)
    object_files = [SimpleNamespace(path=path) for path in (
        "bucket/prefix/part.parquet",
        "bucket/prefix/_late.parquet",
        "bucket/prefix/.hidden.parquet",
        "bucket/prefix/part.pq",
        "bucket/prefix/late.PARQUET",
    )]
    assert mod._native_parquet_fragments(
        "s3://bucket/prefix", "bucket/prefix", directory, object_files
    ) == [
        "bucket/prefix/.hidden.parquet",
        "bucket/prefix/_late.parquet",
        "bucket/prefix/part.parquet",
    ]
    assert mod._native_parquet_fragments(
        "s3://bucket/exact.PQ", "bucket/exact.PQ",
        SimpleNamespace(type=pafs.FileType.File), [SimpleNamespace(path="bucket/exact.PQ")],
    ) == ["bucket/exact.PQ"]
    assert mod._native_parquet_fragments(
        "s3://bucket/directory.parquet", "bucket/directory.parquet", directory,
        [SimpleNamespace(path="bucket/directory.parquet/part.parquet")],
    ) == []


@pytest.mark.parametrize(
    ("outcome", "expected_status", "cleanup_fails"),
    [
        ("success", "done", False),
        ("failure", "failed", False),
        ("cancel", "cancelled", False),
        ("cleanup-failure", "failed", True),
        ("settle-failure", "failed", False),
    ],
)
def test_ray_popen_supervisor_erases_sensitive_workdir_and_closes_log(
        tmp_path, monkeypatch, outcome, expected_status, cleanup_fails):
    import json
    import shutil
    import subprocess
    import tempfile
    import threading
    from pathlib import Path

    from hub.deps import Deps
    from hub.models import Graph, PerNodeStatus, RunStatus

    mod = _load_dp_ray()
    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(data)))
    runner.on_status = None
    workdirs: list[Path] = []
    log_handles = []
    catalog_calls = []
    completion_observations = []
    discarded_attempts = []
    real_mkdtemp = tempfile.mkdtemp
    real_rmtree = shutil.rmtree

    def _mkdtemp(*, prefix):
        path = Path(real_mkdtemp(prefix=prefix, dir=tmp_path))
        workdirs.append(path)
        return str(path)

    class FakeProcess:
        def __init__(self, _args, *, stdout, **_kwargs):
            log_handles.append(stdout)
            with open(_args[-1]) as stream:
                job = json.load(stream)
            if outcome in ("success", "cleanup-failure", "settle-failure"):
                Path(job["status_file"]).write_text(json.dumps({
                    "status": "done", "rows": 1,
                    "outputs": [{"step_id": "write", "name": "out", "uri": "/tmp/out"}],
                }))
                self.returncode = 0
            elif outcome == "failure":
                Path(job["status_file"]).write_text(json.dumps({
                    "status": "failed", "error": "driver failed", "rows": 0, "outputs": [],
                }))
                self.returncode = 1
            else:
                self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    if cleanup_fails:
        monkeypatch.setattr(
            shutil, "rmtree",
            lambda _path: (_ for _ in ()).throw(OSError("forced cleanup denial")),
        )
    if outcome == "settle-failure":
        monkeypatch.setattr(
            runner, "_settle_popen_result",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("forced settlement failure")),
        )
        monkeypatch.setattr(mod, "discard_attempt", discarded_attempts.append)
    run_id = f"popen-cleanup-{outcome}"
    graph = Graph(id=f"canvas-{outcome}", version=1, nodes=[], edges=[])
    status = RunStatus(
        run_id=run_id, status="queued", placement="distributed",
        per_node=[PerNodeStatus(node_id="target", status="queued")],
    )
    runner.runs[run_id] = status
    runner._register_outputs = lambda _graph, result, **_kwargs: catalog_calls.append(
        (result, any(path.exists() for path in workdirs))
    )
    runner._cancel[run_id] = threading.Event()
    if outcome == "cancel":
        runner._cancel[run_id].set()
    runner.on_complete = lambda _graph, _target, final: completion_observations.append(
        (final.status, any(path.exists() for path in workdirs))
    )

    materialize_uri = "/tmp/unpublished-attempt" if outcome == "settle-failure" else None
    runner._supervise(run_id, graph, None, status, materialize_uri=materialize_uri)

    assert status.status == expected_status
    assert log_handles and all(handle.closed for handle in log_handles)
    if cleanup_fails:
        assert completion_observations == [("failed", True)]
        assert "workdir cleanup failed" in (status.error or "")
        assert catalog_calls == []
        assert workdirs and all(path.exists() for path in workdirs)
        for path in workdirs:
            real_rmtree(path)
    else:
        assert completion_observations == [(expected_status, False)]
        if outcome == "success":
            assert len(catalog_calls) == 1 and catalog_calls[0][1] is False
        else:
            assert catalog_calls == []
        assert workdirs and all(not path.exists() for path in workdirs)
        assert list(tmp_path.glob("dp_ray_*")) == []
    if outcome == "settle-failure":
        assert "result settlement failed" in (status.error or "")
        assert discarded_attempts == [materialize_uri]
        assert run_id not in runner._cancel


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group containment")
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("cancel", "cancelled"), ("deadline", "failed")],
)
def test_ray_local_driver_fences_descendant_before_terminal(
        tmp_path, monkeypatch, mode, expected_status):
    import subprocess
    import sys
    import threading

    from hub.deps import Deps
    from hub.models import Graph, RunStatus

    mod = _load_dp_ray()
    marker = tmp_path / f"ray-{mode}-descendant"
    grandchild = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.01)
"""
    child = f"""
import json, os, subprocess, sys, time
job = json.load(open(sys.argv[1]))
subprocess.Popen([sys.executable, "-c", {grandchild!r}])
while not os.path.exists({str(marker)!r}): time.sleep(0.01)
payload = {{"status": "running", "rows": 0, "outputs": []}}
tmp = job["status_file"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["status_file"])
while True: time.sleep(1)
"""
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        assert kwargs.get("start_new_session") is True
        return real_popen([sys.executable, "-c", child, command[-1]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("DP_RUN_DEADLINE_S", "0.25" if mode == "deadline" else "10")
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(data)))
    run_id = f"ray-{mode}-descendant"
    graph = Graph(id=run_id, version=1, nodes=[], edges=[])
    status = RunStatus(run_id=run_id, status="queued", placement="distributed", per_node=[])
    runner.runs[run_id] = status
    runner._cancel[run_id] = threading.Event()
    terminal_emissions = []
    completions = []
    runner.on_status = lambda _graph, observed: terminal_emissions.append(observed.status)
    runner.on_complete = lambda *_args: completions.append(status.status)
    worker = threading.Thread(
        target=runner._supervise, args=(run_id, graph, None, status), daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (marker.exists() and marker.stat().st_size):
        time.sleep(0.01)
    assert marker.exists() and marker.stat().st_size
    if mode == "cancel":
        runner._cancel[run_id].set()
    worker.join(timeout=8)
    assert not worker.is_alive()
    size_at_terminal = marker.stat().st_size
    time.sleep(0.2)

    assert status.status == expected_status
    if mode == "deadline":
        assert "deadline" in (status.error or "")
    assert marker.stat().st_size == size_at_terminal
    assert terminal_emissions.count(expected_status) == 1
    assert completions == [expected_status]
    assert run_id not in runner._driver_scopes


def test_ray_unreaped_driver_stays_nonterminal_and_retains_every_fence(
        tmp_path, monkeypatch):
    import shutil
    import subprocess
    import tempfile
    import threading

    from hub import handoff
    from hub.deps import Deps
    from hub.models import Graph, RunStatus

    mod = _load_dp_ray()
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(data)))
    work = tmp_path / "retained-ray-driver"
    work.mkdir()
    emitted, completed, started, lease_events = [], [], [], []

    class Lease:
        def __enter__(self):
            lease_events.append("opened")
            return self

        def check(self):
            return None

        def __exit__(self, *_args):
            lease_events.append("closed")

    processes = []

    class UnreapableProcess:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.reapable = False
            processes.append(self)

        def poll(self):
            return self.returncode if self.reapable else None

        def terminate(self):
            if not self.reapable:
                raise OSError("terminate unavailable")

        def kill(self):
            if not self.reapable:
                raise OSError("kill unavailable")

        def wait(self, timeout=None):
            if not self.reapable:
                raise OSError("wait unavailable")
            return self.returncode

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            started.append(self.target)
            raise RuntimeError("thread start unavailable")

    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(work))
    monkeypatch.setattr(subprocess, "Popen", UnreapableProcess)
    monkeypatch.setattr(threading, "Thread", DeferredThread)
    monkeypatch.setattr(handoff, "managed_read_lease", lambda *_args, **_kwargs: Lease())
    real_rmtree = shutil.rmtree
    removed = []
    monkeypatch.setattr(shutil, "rmtree", lambda path: removed.append(path))

    run_id = "ray-unreaped"
    graph = Graph(**{
        "id": "ray-unreaped", "version": 1,
        "nodes": [_ray_node("source", "source", {"uri": "s3://bucket/source.attempt-x"})],
        "edges": [],
    })
    status = RunStatus(run_id=run_id, status="queued", placement="distributed", per_node=[])
    runner.runs[run_id] = status
    runner._cancel[run_id] = threading.Event()
    runner._cancel[run_id].set()  # drive the supervisor into its terminate/reap path
    runner.on_status = lambda _graph, observed: emitted.append(observed.status)
    runner.on_complete = lambda *_args: completed.append(True)

    runner._supervise(run_id, graph, None, status)

    assert status.status == "running" and status.stalled is True
    assert status.error == "Ray driver termination is still being reconciled"
    assert emitted and emitted[-1] == "running"
    assert completed == [] and removed == []
    assert work.is_dir() and run_id in runner._cancel
    assert run_id in runner._unreaped_drivers and started
    assert lease_events == ["opened"]
    assert not runner.cancel_acknowledged(run_id)

    assert processes
    processes[0].reapable = True
    processes[0].returncode = 0
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    runner._terminate_all()  # the atexit fallback owns reconciliation if Thread.start failed

    assert status.status == "cancelled" and runner.cancel_acknowledged(run_id)
    assert completed == [True] and not work.exists()
    assert run_id not in runner._unreaped_drivers
    assert run_id not in runner._driver_scopes and run_id not in runner._driver_workdirs
    assert lease_events == ["opened", "closed"]


def test_ray_clean_done_receipt_wins_late_cancel_and_nonzero_done_stays_private(
        tmp_path, monkeypatch):
    import json
    import subprocess
    import tempfile
    import threading
    from pathlib import Path

    from hub.deps import Deps
    from hub.models import Graph, RunOutput, RunStatus
    from hub.run_outputs import sole_output

    mod = _load_dp_ray()
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(data)))
    registered = []
    driver_work = tmp_path / "late-cancel-driver"
    driver_work.mkdir()

    class DoneProcess:
        returncode = 0

        def __init__(self, args, **_kwargs):
            with open(args[-1]) as stream:
                job = json.load(stream)
            Path(job["status_file"]).write_text(json.dumps({
                "status": "done", "rows": 1,
                "outputs": [{
                    "step_id": "write", "name": "out", "uri": "/tmp/out.csv",
                    "logical_uri": "/tmp/out.csv",
                }],
                "output_uri": "/tmp/out.csv", "output_table": "out",
            }))

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        tempfile, "mkdtemp",
        lambda **_kwargs: str(driver_work),
    )
    monkeypatch.setattr(subprocess, "Popen", DoneProcess)
    monkeypatch.setattr(
        runner, "_register_outputs",
        lambda _graph, result, **_kwargs: registered.append(result),
    )
    run_id = "ray-late-cancel"
    graph = Graph(id="ray-late-cancel", version=1, nodes=[], edges=[])
    status = RunStatus(
        run_id=run_id, status="queued", placement="distributed",
        target_node_id="write", per_node=[],
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner.runs[run_id] = status
    runner._cancel[run_id] = threading.Event()
    runner._cancel[run_id].set()

    runner._supervise(run_id, graph, None, status)

    committed = sole_output(status, committed=True)
    assert status.status == "done" and committed is not None
    assert committed.uri == "/tmp/out.csv" and committed.table == "out"
    assert len(registered) == 1
    assert not runner.cancel_acknowledged(run_id)

    failed = RunStatus(
        run_id="ray-done-nonzero", status="running", target_node_id="write",
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner._settle_popen_result(
        graph, failed, {
            "status": "done", "rows": 1,
            "outputs": [{"step_id": "write"}],
        }, -9,
    )
    assert failed.status == "failed"
    assert failed.error == "Ray driver exited unsuccessfully after writing a terminal receipt"
    failed_output = sole_output(failed)
    assert failed_output is not None and failed_output.outcome == "failed"
    assert failed_output.uri is None and failed_output.table is None
    assert len(registered) == 1

    # Local Popen driver errors surface verbatim (same host/trust boundary), unlike remote Jobs runs
    # whose text is reduced to a stable code by _apply_job_result.
    local_failure = RunStatus(run_id="ray-local-failure", status="running")
    runner._settle_popen_result(
        graph, local_failure,
        {"status": "failed", "error": "Binder Error: column X not found"}, 0,
    )
    assert local_failure.status == "failed"
    assert local_failure.error == "Binder Error: column X not found"


def test_ray_reconcile_and_shutdown_have_one_terminal_finalizer(tmp_path, monkeypatch):
    import threading

    from hub.deps import Deps

    runner = _load_dp_ray().RayRunner(
        Deps(str(tmp_path / "workspace"), str(tmp_path / "data")))
    barrier = threading.Barrier(2)
    finalized = []

    class ReapedProcess:
        returncode = 0

    proc = ReapedProcess()
    run_id = "ray-one-finalizer"
    from hub.process_scope import OwnedProcessScope

    scope = OwnedProcessScope(proc, owns_process_group=False)
    state = {"run_id": run_id, "scope": scope, "work": str(tmp_path / "work")}
    runner._unreaped_drivers[run_id] = state
    runner._driver_scopes[run_id] = scope
    runner._driver_workdirs[run_id] = state["work"]
    runner._cancel[run_id] = threading.Event()
    monkeypatch.setattr(
        runner, "_try_reap_driver",
        lambda _proc: (barrier.wait(timeout=5), True)[1],
    )
    monkeypatch.setattr(
        runner, "_finish_supervision",
        lambda claimed: finalized.append(claimed),
    )

    workers = [
        threading.Thread(target=runner._reconcile_unreaped_driver, args=(run_id,)),
        threading.Thread(target=runner._terminate_all),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert finalized == [state]
    assert run_id not in runner._unreaped_drivers
    runner._finalizing_drivers.discard(run_id)
    runner._driver_scopes.pop(run_id, None)
    runner._driver_workdirs.pop(run_id, None)
    runner._cancel.pop(run_id, None)


def test_ray_popen_multi_sink_failure_keeps_partial_output_private_and_untouched(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph, RunOutput, RunStatus
    from hub.run_outputs import sole_output

    mod = _load_dp_ray()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    graph = Graph(**{
        "id": "multi-sink-failure", "version": 1,
        "nodes": [
            _ray_node("src", "source", {"uri": "/tmp/input.parquet"}),
            _ray_node("first", "write", {"name": "first"}),
            _ray_node("second", "write", {"name": "second"}),
        ],
        "edges": [_ray_edge("src", "first"), _ray_edge("src", "second")],
    })
    ir = lower_to_ir(graph, None, runner.node_specs)
    # URI naming is not ownership: a custom/stable file may legitimately contain `.attempt-`.
    first_output = tmp_path / "custom.attempt-output.csv"
    first_output.write_text("x\n1\n")
    sink_targets = {"first": str(first_output), "second": "/tmp/second"}
    commits = []

    monkeypatch.setattr(runner, "_build", lambda *_args, **_kwargs: object())

    def commit(step, *_args, **_kwargs):
        commits.append(step.id)
        if step.id == "second":
            raise RuntimeError("second sink failed")
        return 3, str(first_output), "first"

    monkeypatch.setattr(runner, "_commit", commit)
    result = runner._run_ir_sync(
        ir, graph, None,
        sink_targets=sink_targets,
        attempt_id="run",
    )
    assert result["status"] == "failed"
    assert commits == ["first", "second"]
    partial_outputs = [{
        "step_id": "first", "name": "first", "uri": str(first_output),
        "logical_uri": str(first_output),
    }]
    assert result["outputs"] == partial_outputs

    catalog_calls = []
    monkeypatch.setattr(runner, "_register_outputs", lambda *_args: catalog_calls.append(True))
    monkeypatch.setattr(
        mod, "discard_attempt",
        lambda _uri: pytest.fail("terminal settlement guessed ownership from a returned URI"),
    )
    status = RunStatus(
        run_id="run", status="running", target_node_id="second",
        outputs=[RunOutput(
            node_id="second", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner._settle_popen_result(
        graph, status, result, 1,
        expected_targets=sink_targets, expected_attempts={},
    )

    assert status.status == "failed"
    failed_output = sole_output(status)
    assert failed_output is not None and failed_output.outcome == "failed"
    assert failed_output.uri is None and failed_output.table is None
    assert catalog_calls == []
    assert first_output.read_text() == "x\n1\n"
    assert result["outputs"] == partial_outputs

    cancelled = dict(
        result, status="cancelled", error=None,
        output_uri=str(first_output), output_table="first",
    )
    cancelled_status = RunStatus(
        run_id="cancelled-run", status="running", target_node_id="second",
        outputs=[RunOutput(
            node_id="second", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner._settle_popen_result(
        graph, cancelled_status, cancelled, -15,
        expected_targets=sink_targets, expected_attempts={},
    )
    assert cancelled_status.status == "cancelled"
    cancelled_output = sole_output(cancelled_status)
    assert cancelled_output is not None and cancelled_output.outcome == "cancelled"
    assert cancelled_output.uri is None and cancelled_output.table is None
    assert catalog_calls == []
    assert first_output.read_text() == "x\n1\n"
    assert cancelled["outputs"] == result["outputs"]


def test_ray_popen_done_result_keeps_expected_output_binding_and_registration(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.models import Graph, RunOutput, RunStatus
    from hub.run_outputs import sole_output

    mod = _load_dp_ray()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    graph = Graph(id="multi-sink-success", version=1, nodes=[], edges=[])
    expected_targets = {
        "first": "/tmp/first.parquet", "second": "/tmp/second.parquet",
    }
    expected_attempts = {
        "first": "/tmp/first.attempt-run", "second": "/tmp/second.attempt-run",
    }
    monkeypatch.setattr(
        runner.catalog, "register_output",
        lambda **_kwargs: pytest.fail("an incomplete expected sink set reached the catalog"),
    )
    missing_status = RunStatus(
        run_id="missing", status="running", target_node_id="second",
        outputs=[RunOutput(
            node_id="second", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )
    runner._settle_popen_result(
        graph, missing_status,
        {"status": "done", "rows": 0, "outputs": []}, 0,
        expected_targets=expected_targets, expected_attempts=expected_attempts,
    )
    assert missing_status.status == "failed"
    assert missing_status.error == (
        "Catalog registration failed "
        "(code=catalog_registration_failed,type=RuntimeError)"
    )
    missing_output = sole_output(missing_status)
    assert missing_output is not None and missing_output.outcome == "failed"
    assert missing_output.uri is None and missing_output.table is None

    result = {
        "status": "done", "rows": 5,
        "output_uri": "/tmp/second.attempt-run", "output_table": "second",
        "outputs": [
            {"step_id": "first", "name": "first", "uri": "/tmp/first.attempt-run",
             "logical_uri": "/tmp/first.parquet"},
            {"step_id": "second", "name": "second", "uri": "/tmp/second.attempt-run",
             "logical_uri": "/tmp/second.parquet"},
        ],
    }
    registered = []

    def register_outputs(got_graph, got_result, **kwargs):
        registered.append((got_graph, got_result, kwargs))
        return {"first": "v-first", "second": "v-second"}

    monkeypatch.setattr(runner, "_register_outputs", register_outputs)
    status = RunStatus(
        run_id="run", status="running", target_node_id="second",
        outputs=[RunOutput(
            node_id="second", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="pending",
        )],
    )

    runner._settle_popen_result(
        graph, status, result, 0,
        expected_targets=expected_targets, expected_attempts=expected_attempts,
    )

    assert registered == [(graph, result, {
        "expected_targets": expected_targets, "expected_attempts": expected_attempts,
        "backend_run_id": "run",
    })]
    assert status.status == "done" and status.progress == 1.0
    output = sole_output(status, committed=True)
    assert output is not None
    assert output.uri == result["output_uri"] and output.table == "second"
    assert output.version == "v-second"
    assert status.rows_processed == status.total_rows == 5


def test_ray_popen_managed_publication_reads_version_from_nested_table(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hub.deps import Deps
    from hub.models import Graph
    from hub.plugins import catalog as catalog_plugin

    runner = _load_dp_ray().RayRunner(
        Deps(str(tmp_path / "workspace"), str(tmp_path / "data")))
    graph = Graph(**{
        "id": "ray-managed-version", "version": 1,
        "nodes": [_ray_node("write", "write", {"filename": "out.parquet"})],
        "edges": [],
    })
    physical_uri = "s3://shared/out.attempt-run"

    def publish(**kwargs):
        return {
            "uri": kwargs["uri"], "generation": 1,
            "table": SimpleNamespace(
                uri=kwargs["uri"], name=kwargs["name"], version="v-managed-exact"),
        }

    monkeypatch.setattr(catalog_plugin, "core_managed_publisher", lambda _catalog: publish)
    versions = runner._register_outputs(graph, {"outputs": [{
        "step_id": "write", "name": "out", "uri": physical_uri,
        "logical_uri": "s3://shared/out.parquet",
    }]})

    assert versions == {"write": "v-managed-exact"}


def test_ray_hive_native_plan_matches_adapter_schema_and_fails_closed_on_unproved_layouts(
        tmp_path, monkeypatch):
    import datetime
    import sys
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.deps import Deps
    from hub.plugins.adapters import DuckDBAdapter

    mod = _load_dp_ray()
    adapter = DuckDBAdapter()

    native = tmp_path / "native-hive"
    for path, value in (("n=1/tier=alpha", 10), ("n=2/tier=beta", 20)):
        directory = native / path
        directory.mkdir(parents=True)
        pq.write_table(pa.table({"v": [value]}), directory / "part.parquet")
    plan = mod._native_parquet_plan(str(native), adapter)
    assert plan["schema"] == pa.schema([
        ("v", pa.int64()), ("n", pa.int64()), ("tier", pa.string()),
    ])
    assert plan["partition_types"] == {"n": int, "tier": str}
    assert plan["schema"].names == ["v", "n", "tier"]

    duplicate = tmp_path / "duplicate-hive" / "x=1" / "x=2"
    duplicate.mkdir(parents=True)
    pq.write_table(pa.table({"v": [1]}), duplicate / "part.parquet")
    with pytest.raises(RuntimeError, match="repeats a Hive partition key"):
        mod._native_parquet_plan(str(tmp_path / "duplicate-hive"), adapter)

    encoded_sentinel = tmp_path / "encoded-sentinel" / "p=%5F%5FHIVE%5FDEFAULT%5FPARTITION%5F%5F"
    encoded_sentinel.mkdir(parents=True)
    pq.write_table(pa.table({"v": [1]}), encoded_sentinel / "part.parquet")
    with pytest.raises(RuntimeError, match="default-partition sentinel"):
        mod._native_parquet_plan(str(tmp_path / "encoded-sentinel"), adapter)

    sentinel = tmp_path / "sentinel"
    for partition, value in (("p=__HIVE_DEFAULT_PARTITION__", 1), ("p=present", 2)):
        directory = sentinel / partition
        directory.mkdir(parents=True)
        pq.write_table(pa.table({"v": [value]}), directory / "part.parquet")
    with pytest.raises(RuntimeError, match="default-partition sentinel"):
        mod._native_parquet_plan(str(sentinel), adapter)

    dated = tmp_path / "date-hive"
    for partition in ("d=2026-07-12", "d=2026-07-13"):
        directory = dated / partition
        directory.mkdir(parents=True)
        pq.write_table(pa.table({"v": [1]}), directory / "part.parquet")
    with pytest.raises(RuntimeError, match="unsupported Ray 2.56 partition type date32"):
        mod._native_parquet_plan(str(dated), adapter)

    reversed_keys = tmp_path / "reversed-hive" / "z=1" / "a=alpha"
    reversed_keys.mkdir(parents=True)
    pq.write_table(pa.table({"v": [7]}), reversed_keys / "part.parquet")
    reversed_root = tmp_path / "reversed-hive"
    with pytest.raises(RuntimeError, match="Hive column order differs"):
        mod._native_parquet_plan(str(reversed_root), adapter)

    ancestor_root = tmp_path / "tenant=acme" / "genuine-hive"
    ancestor_partition = ancestor_root / "n=1"
    ancestor_partition.mkdir(parents=True)
    pq.write_table(pa.table({"v": [1]}), ancestor_partition / "part.parquet")
    trapped = DuckDBAdapter()
    monkeypatch.setattr(
        trapped, "scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ancestor-Hive rejection must happen before the adapter metadata probe")
        ),
    )
    with pytest.raises(RuntimeError, match="below a Hive-looking root/ancestor"):
        mod._native_parquet_plan(str(ancestor_root), trapped)

    # A flat dataset below a Hive-looking ancestor is safe: adapter Hive parsing is disabled and Ray is
    # rooted at the flat dataset, so the ancestor never becomes a logical column.
    flat = tmp_path / "tenant=acme" / "flat"
    flat.mkdir(parents=True)
    pq.write_table(pa.table({"v": [3]}), flat / "part.parquet")
    flat_plan = mod._native_parquet_plan(str(flat), adapter)
    assert flat_plan["schema"] == pa.schema([("v", pa.int64())])
    assert flat_plan["partition_types"] == {}

    # Sentinel NULL and DATE still preserve exact DuckDB semantics on the bounded compatibility path.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))

    class DatasetTable:
        def __init__(self, table):
            self._table = table

        def __getattr__(self, name):
            return getattr(self._table, name)

    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(
        put=lambda value: value,
        data=SimpleNamespace(from_arrow_refs=lambda refs: DatasetTable(pa.concat_tables(refs))),
    ))
    sentinel_table = runner._build(SimpleNamespace(
        op="read", config={"uri": str(sentinel)},
    ), {})
    assert sorted(sentinel_table.to_pylist(), key=lambda row: row["v"]) == [
        {"v": 1, "p": None}, {"v": 2, "p": "present"},
    ]
    date_table = runner._build(SimpleNamespace(
        op="read", config={"uri": str(dated)},
    ), {})
    assert date_table.schema.field("d").type == pa.date32()
    assert sorted(row["d"] for row in date_table.to_pylist()) == [
        datetime.date(2026, 7, 12), datetime.date(2026, 7, 13),
    ]
    reversed_table = runner._build(SimpleNamespace(
        op="read", config={"uri": str(reversed_root)},
    ), {})
    assert reversed_table.schema == pa.schema([
        ("v", pa.int64()), ("a", pa.string()), ("z", pa.int64()),
    ])
    assert reversed_table.to_pylist() == [{"v": 7, "a": "alpha", "z": 1}]


@pytest.mark.skipif(not os.environ.get("DP_TEST_RAY_LIVE"), reason="real Ray 2.56 Hive schema/rows regression")
def test_ray_native_hive_live_exposes_logical_schema_columns_and_typed_rows(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.plugins.adapters import DuckDBAdapter

    ray = pytest.importorskip("ray")
    mod = _load_dp_ray()
    root = tmp_path / "live-hive"
    for path, value in (("n=1/tier=alpha", 10), ("n=2/tier=beta", 20)):
        directory = root / path
        directory.mkdir(parents=True)
        pq.write_table(pa.table({"v": [value]}), directory / "part.parquet")
    plan = mod._native_parquet_plan(str(root), DuckDBAdapter())
    monkeypatch.setenv("RAY_DATA_DISABLE_PROGRESS_BARS", "1")
    ray.init(num_cpus=2, include_dashboard=False, log_to_driver=False)
    try:
        dataset = mod._read_native_parquet(ray, plan).materialize()
        schema = getattr(dataset.schema(), "base_schema", dataset.schema())
        assert schema == pa.schema([
            ("v", pa.int64()), ("n", pa.int64()), ("tier", pa.string()),
        ])
        assert dataset.columns() == ["v", "n", "tier"]
        assert sorted(dataset.take_all(), key=lambda row: row["n"]) == [
            {"v": 10, "n": 1, "tier": "alpha"},
            {"v": 20, "n": 2, "tier": "beta"},
        ]
    finally:
        ray.shutdown()


@pytest.mark.skipif(not os.environ.get("DP_TEST_RAY_LIVE"), reason="real Ray 2.56 typed-empty regression")
def test_ray_typed_empty_paths_live_keep_pre_materialization_schema(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.handoff import read_manifest, validate_shards
    from hub.plugins.adapters import DuckDBAdapter

    ray = pytest.importorskip("ray")
    mod = _load_dp_ray()
    runner = object.__new__(mod.RayRunner)
    empty_hive = tmp_path / "empty-hive" / "n=1"
    empty_hive.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_batches([], schema=pa.schema([("v", pa.int64())])),
        empty_hive / "part.parquet",
    )
    hive_plan = mod._native_parquet_plan(str(tmp_path / "empty-hive"), DuckDBAdapter())
    expected_hive_schema = pa.schema([("v", pa.int64()), ("n", pa.int64())])
    assert hive_plan["schema"] == expected_hive_schema

    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"k": [1, 2], "v": [10, 20]}), source)
    source_plan = mod._native_parquet_plan(str(source), DuckDBAdapter())
    source_schema = pa.schema([("k", pa.int64()), ("v", pa.int64())])

    right_source = tmp_path / "right.parquet"
    right_schema = pa.schema([("k", pa.int64()), ("right_value", pa.string())])
    pq.write_table(pa.table({"k": [1], "right_value": ["right"]}, schema=right_schema), right_source)
    right_plan = mod._native_parquet_plan(str(right_source), DuckDBAdapter())

    filter_step = SimpleNamespace(
        op="filter", inputs=[("src", None)],
        config={"mode": "filter", "code": "def fn(row):\n    return False", "onError": "raise"},
    )

    def filtered(plan):
        return runner._build(filter_step, {"src": mod._read_native_parquet(ray, plan)})

    monkeypatch.setenv("RAY_DATA_DISABLE_PROGRESS_BARS", "1")
    monkeypatch.setenv("DP_RAY_SHUFFLE_PARTITIONS", "2")
    from ray._private import ray_constants
    from hub.ray_compat import patch_hash_shuffle
    patch_hash_shuffle()
    monkeypatch.setenv(
        ray_constants.WORKER_PROCESS_SETUP_HOOK_ENV_VAR, "hub.ray_compat.patch_hash_shuffle"
    )
    ray.init(num_cpus=2, include_dashboard=False, log_to_driver=False)
    try:
        # A source that is already empty keeps its native logical schema.
        direct_empty = mod._collect_arrow(
            mod._read_native_parquet(ray, hive_plan), purpose="live native empty source"
        )
        assert direct_empty.schema == expected_hive_schema

        # A supported transform can erase every Ray block and both of Ray's schema() surfaces. Driver-side
        # lineage must still type worker-direct publication and bounded collection without re-running it.
        output = str(tmp_path / "typed-empty.attempt-live")
        assert mod._write_worker_direct_parquet(
            filtered(source_plan), output, attempt_id="live-empty"
        ) == (0, output)
        manifest = read_manifest(output)
        assert manifest is not None and validate_shards(output, manifest)
        assert pq.read_table(os.path.join(output, "part-000000.parquet")).schema == source_schema

        collected = mod._collect_arrow(
            filtered(source_plan), purpose="live typed-empty collect"
        )
        assert collected.num_rows == 0 and collected.schema == source_schema

        left = ray.data.from_items([{"k": 1, "left_value": "left"}], override_num_blocks=1)
        right_empty_join = SimpleNamespace(
            inputs=[("left", None), ("right", None)],
            config={"on": "k", "how": "left"},
        )
        joined = runner._build_join(
            right_empty_join, {"left": left, "right": filtered(right_plan)}
        ).materialize()
        joined_schema = getattr(joined.schema(), "base_schema", joined.schema())
        assert joined_schema == pa.schema([
            ("k", pa.int64()), ("left_value", pa.string()), ("right_value", pa.string()),
        ])
        assert joined.take_all() == [{"k": 1, "left_value": "left", "right_value": None}]

        # A non-empty zero-column ref (Ray's empty sort representation) must use its lineage hint instead
        # of entering the `if refs` branch as an untyped table.
        sorted_empty = runner._build(SimpleNamespace(
            op="sort", inputs=[("src", None)], config={"by": "k"},
        ), {"src": filtered(right_plan)})
        sort_joined = runner._build_join(
            right_empty_join, {"left": left, "right": sorted_empty}
        ).materialize()
        assert sort_joined.take_all() == [{"k": 1, "left_value": "left", "right_value": None}]

        # Every claimed relational operator has an explicit empty-schema rule; left-empty join derives
        # its own projection instead of inheriting either side.
        relational = [
            (SimpleNamespace(op="aggregate", inputs=[("src", None)],
                             config={"groupBy": "k", "aggs": "count(*) AS n"}), ["k", "n"]),
            (SimpleNamespace(op="window", inputs=[("src", None)],
                             config={"partitionBy": "k", "orderBy": "v",
                                     "expr": "row_number()", "as": "rn"}), ["k", "v", "rn"]),
            (SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}), ["k", "v"]),
            (SimpleNamespace(op="sort", inputs=[("src", None)], config={"by": "k"}), ["k", "v"]),
        ]
        for step, expected_names in relational:
            table = mod._collect_arrow(
                runner._build(step, {"src": filtered(source_plan)}),
                purpose=f"live empty {step.op}",
            )
            assert table.num_rows == 0 and table.schema.names == expected_names

        sorted_then_deduped = runner._build(
            SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}),
            {"src": runner._build(
                SimpleNamespace(op="sort", inputs=[("src", None)], config={"by": "k"}),
                {"src": filtered(source_plan)},
            )},
        )
        assert mod._collect_arrow(
            sorted_then_deduped, purpose="live empty sort then dedup"
        ).schema == source_schema

        left_empty = filtered(source_plan)
        left_empty_join = SimpleNamespace(
            inputs=[("left", None), ("right", None)], config={"on": "k", "how": "left"},
        )
        joined_empty = mod._collect_arrow(
            runner._build_join(
                left_empty_join,
                {"left": left_empty, "right": mod._read_native_parquet(ray, right_plan)},
            ),
            purpose="live left-empty join",
        )
        assert joined_empty.num_rows == 0
        assert joined_empty.schema.names == ["k", "v", "right_value"]

        # outputSchema is empty-result lineage, not a projection contract for non-empty data. A narrow
        # declaration must not make Ray silently drop columns that the UDF actually returned.
        narrow_map = SimpleNamespace(
            op="map", inputs=[("src", None)],
            config={
                "mode": "map",
                "code": "def fn(row):\n    return {'k': row['k'], 'extra': row['v'] + 1}",
                "onError": "raise", "outputSchema": [{"name": "k", "type": "int"}],
            },
        )
        mapped = runner._build(narrow_map, {"src": mod._read_native_parquet(ray, source_plan)})
        joined_nonempty = mod._collect_arrow(
            runner._build_join(
                left_empty_join,
                {"left": mapped, "right": mod._read_native_parquet(ray, right_plan)},
            ),
            purpose="live non-empty join with narrow schema lineage",
        )
        assert joined_nonempty.schema.names == ["k", "extra", "right_value"]
        assert sorted(joined_nonempty.to_pylist(), key=lambda row: row["k"]) == [
            {"k": 1, "extra": 11, "right_value": "right"},
            {"k": 2, "extra": 21, "right_value": None},
        ]

        drop_all = SimpleNamespace(
            op="filter", inputs=[("src", None)],
            config={"mode": "filter", "code": "def fn(row):\n    return False", "onError": "raise"},
        )
        filtered_actual = mod._collect_arrow(
            runner._build(drop_all, {"src": mapped}),
            purpose="live runtime-schema filter-to-empty",
        )
        assert filtered_actual.num_rows == 0
        assert filtered_actual.schema.names == ["k", "extra"]

        runtime_relational = [
            (SimpleNamespace(
                op="aggregate", inputs=[("src", None)],
                config={"groupBy": "extra", "aggs": "count(*) AS n"},
            ), ["extra", "n"]),
            (SimpleNamespace(
                op="window", inputs=[("src", None)],
                config={"partitionBy": "extra", "orderBy": "k",
                        "expr": "row_number()", "as": "rn"},
            ), ["k", "extra", "rn"]),
        ]
        for step, names in runtime_relational:
            actual = mod._collect_arrow(
                runner._build(step, {"src": mapped}), purpose=f"live runtime-schema {step.op}"
            )
            assert actual.schema.names == names
            assert actual.num_rows == 2
        runtime_dedup = mod._collect_arrow(
            runner._build(
                SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}),
                {"src": mapped},
            ),
            purpose="live runtime-schema dedup",
        )
        assert runtime_dedup.schema.names == ["k", "extra"]
        assert runtime_dedup.num_rows == 2

        declared_flat_map = SimpleNamespace(
            op="flat_map", inputs=[("src", None)],
            config={
                "mode": "flat_map", "code": "def fn(row):\n    return []", "onError": "raise",
                "outputSchema": [{"name": "renamed", "type": "int"}],
            },
        )
        declared_dataset = runner._build(
            declared_flat_map, {"src": mod._read_native_parquet(ray, source_plan)}
        )
        declared_empty = mod._collect_arrow(declared_dataset, purpose="live declared empty map")
        assert declared_empty.schema == pa.schema([("renamed", pa.int64())])
        declared_dedup = runner._build(
            SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}),
            {"src": declared_dataset},
        )
        assert mod._collect_arrow(
            declared_dedup, purpose="live declared empty map then dedup"
        ).schema == pa.schema([("renamed", pa.int64())])

        undeclared_flat_map = SimpleNamespace(
            op="flat_map", inputs=[("src", None)],
            config={"mode": "flat_map", "code": "def fn(row):\n    return []", "onError": "raise"},
        )
        undeclared_dataset = runner._build(
            undeclared_flat_map, {"src": mod._read_native_parquet(ray, source_plan)}
        )
        with pytest.raises(RuntimeError, match="did not expose an Arrow schema"):
            mod._collect_arrow(undeclared_dataset, purpose="live undeclared empty map")
        with pytest.raises(RuntimeError, match="empty Ray dedup input did not expose"):
            runner._build(
                SimpleNamespace(op="dedup", inputs=[("src", None)], config={"on": ""}),
                {"src": undeclared_dataset},
            )
    finally:
        ray.shutdown()


def test_ray_custom_parquet_adapters_never_take_the_native_filesystem_path(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from hub.deps import Deps
    from hub.plugins.adapters import DuckDBAdapter
    from hub.sinks import SinkSpec

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))

    class CustomParquetAdapter(DuckDBAdapter):
        def scan(self, _uri, **_kwargs):
            raise AssertionError("custom adapter scan must not run after Ray dispatch")

    custom = CustomParquetAdapter()
    monkeypatch.setattr(runner, "resolve_adapter", lambda _uri: custom)
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(data=SimpleNamespace()))
    for uri in (str(tmp_path / "custom.parquet"), "s3://bucket/custom.parquet"):
        with pytest.raises(RuntimeError, match="CustomParquetAdapter.*explicit bounded/distributed"):
            runner._build(SimpleNamespace(op="read", config={"uri": uri}), {})
        assert mod._worker_direct_parquet_sink(
            SinkSpec.from_config({"filename": "custom.parquet"}), uri, custom
        ) is False


def test_ray_compatibility_sources_are_size_bounded_before_adapter_scan(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pyarrow as pa

    from hub.deps import Deps

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mod = _load_dp_ray()
    runner = mod.RayRunner(Deps(str(workspace), str(tmp_path / "data")))
    sentinel = SimpleNamespace()
    scans = []
    source = tmp_path / "small.csv"
    source.write_text("x\n1\n2\n")
    adapter = runner.resolve_adapter(str(source))
    original_scan = adapter.scan

    def tracked_scan(uri):
        scans.append(uri)
        return original_scan(uri)

    monkeypatch.setattr(adapter, "scan", tracked_scan)
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(
        put=lambda value: value,
        data=SimpleNamespace(from_arrow_refs=lambda refs: sentinel),
    ))
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "1024")
    assert runner._build(SimpleNamespace(op="read", config={"uri": str(source)}), {}) is sentinel
    assert scans == [str(source)]

    too_large = tmp_path / "large.csv"
    too_large.write_bytes(b"x" * 17)
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "16")
    with pytest.raises(RuntimeError, match="above the 16-byte limit"):
        runner._build(SimpleNamespace(op="read", config={"uri": str(too_large)}), {})
    with pytest.raises(RuntimeError, match="byte size is unknown"):
        runner._build(SimpleNamespace(op="read", config={"uri": str(tmp_path / "missing.csv")}), {})
    assert scans == [str(source)], "over-limit and unknown sources must fail before adapter.scan"

    decoded = pa.table({"x": [1, 2, 3]})  # 24 Arrow bytes from a 6-byte CSV source

    class DecodedRelation:
        def to_arrow_reader(self, _batch_rows):
            return pa.RecordBatchReader.from_batches(decoded.schema, decoded.to_batches())

    def decoded_scan(uri):
        scans.append(uri)
        return DecodedRelation()

    monkeypatch.setattr(adapter, "scan", decoded_scan)
    with pytest.raises(RuntimeError, match="24-byte driver compatibility collect, above the 16-byte limit"):
        runner._build(SimpleNamespace(op="read", config={"uri": str(source)}), {})


def test_ray_driver_collect_rejects_unknown_or_over_limit_before_iteration(monkeypatch):
    import pyarrow as pa

    mod = _load_dp_ray()
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "16")

    class Dataset:
        def __init__(self, size):
            self.size = size
            self.iterated = False

        def materialize(self):
            return self

        def size_bytes(self):
            if self.size is None:
                raise RuntimeError("unknown")
            return self.size

        def iter_batches(self, *, batch_format):
            assert batch_format == "pyarrow"
            self.iterated = True
            return iter((pa.table({"x": [1]}),))

        def schema(self):
            return pa.schema([("x", pa.int64())])

    small = Dataset(8)
    assert mod._collect_arrow(small, purpose="CSV sink").column("x").to_pylist() == [1]
    assert small.iterated
    for size, message in ((17, "above the 16-byte limit"), (None, "byte size is unknown")):
        rejected = Dataset(size)
        with pytest.raises(RuntimeError, match=message):
            mod._collect_arrow(rejected, purpose="CSV sink")
        assert rejected.iterated is False


def test_ray_rejects_or_falls_back_before_dispatch_for_unsupported_sinks(tmp_path, monkeypatch):
    import duckdb

    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    workspace = tmp_path / "ws"
    workspace.mkdir()
    deps = Deps(str(workspace), str(tmp_path / "data"))
    mod = _load_dp_ray()
    ray_runner = mod.RayRunner(deps)

    source_uri = str(tmp_path / "unused.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS x) TO '{source_uri}' (FORMAT PARQUET)"
    )

    def graph(config):
        return Graph(**{"id": "sink-gate", "version": 1, "nodes": [
            _ray_node("src", "source", {"uri": source_uri}),
            _ray_node("w", "write", config),
        ], "edges": [_ray_edge("src", "w")]})

    # Whole-graph dispatch carries the hub-resolved map into the supervisor; region materialization does
    # not, because it stops before a write sink. Defer the thread so this stays cluster-free.
    dispatched = []

    class DeferredThread:
        def __init__(self, *args, **kwargs):
            dispatched.append({"args": args, "kwargs": kwargs})

        def start(self):
            return None

    monkeypatch.setattr(mod.threading, "Thread", DeferredThread)
    unit = Graph(**{"id": "unit", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": source_uri}),
    ], "edges": []})
    ray_runner.run_unit(unit, "src", str(tmp_path / "unit.parquet"))
    assert "sink_targets" not in dispatched[-1]["kwargs"].get("kwargs", {})

    valid = graph({"filename": "valid.parquet"})
    ray_runner.run(compile_plan(valid, "w", deps.registry, deps.node_specs), valid, "w", "local")
    sink_targets = dispatched[-1]["kwargs"]["kwargs"]["sink_targets"]
    assert sink_targets == {"w": str(workspace / "outputs/valid.parquet")}
    sink_attempts = dispatched[-1]["kwargs"]["kwargs"]["sink_attempts"]
    assert sink_attempts["w"].startswith(str(workspace / "outputs/valid.attempt-"))

    monkeypatch.setenv("DP_RAY_REMOTE", "1")
    assert ray_runner._sink_targets_runnable(lower_to_ir(valid, "w", deps.node_specs)) is False
    monkeypatch.delenv("DP_RAY_REMOTE")

    # This combination is outside the product contract and must never be dispatched with partitionBy
    # silently discarded. The Ray gate rejects it; run() delegates before starting a Ray supervisor.
    invalid = graph({"filename": "x.parquet", "writeMode": "append", "partitionBy": "cat"})
    invalid_ir = lower_to_ir(invalid, "w", deps.node_specs)
    assert ray_runner._ray_runnable(invalid_ir) is False
    sentinel = object()
    monkeypatch.setattr(ray_runner.base, "run", lambda *args, **kwargs: sentinel)
    assert ray_runner.run(
        compile_plan(invalid, "w", deps.registry, deps.node_specs), invalid, "w", "local"
    ) is sentinel

    # A plugin adapter without a partition_by keyword is also detected by the metadata-only preflight.
    class NoPartitionAdapter:
        def write(self, uri, relation, mode="overwrite"):
            raise AssertionError("must not be invoked")

    partitioned = graph({"filename": "x.parquet", "writeMode": "overwrite", "partitionBy": "cat"})
    partitioned_ir = lower_to_ir(partitioned, "w", deps.node_specs)
    assert ray_runner._ray_runnable(partitioned_ir) is True
    monkeypatch.setattr(ray_runner, "resolve_adapter", lambda _uri: NoPartitionAdapter())
    assert ray_runner._sink_targets_runnable(partitioned_ir) is False
    assert ray_runner.run(
        compile_plan(partitioned, "w", deps.registry, deps.node_specs), partitioned, "w", "local"
    ) is sentinel


def test_remote_ray_bounds_local_sources_and_never_dispatches_a_local_region_handoff(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import duckdb

    from hub.deps import Deps
    from hub.ir import lower_to_ir
    from hub.models import Graph

    workspace = tmp_path / "ws"
    workspace.mkdir()
    deps = Deps(str(workspace), str(tmp_path / "data"))
    mod = _load_dp_ray()
    runner = mod.RayRunner(deps)
    source = str(tmp_path / "small.parquet")
    duckdb.connect().execute(f"COPY (SELECT 1 AS x) TO '{source}' (FORMAT PARQUET)")
    graph = Graph(**{"id": "remote-local", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": source}),
    ], "edges": []})

    monkeypatch.setenv("DP_RAY_REMOTE", "1")
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "1048576")
    sentinel = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(
        put=lambda value: value,
        data=SimpleNamespace(
            from_arrow_refs=lambda _refs: sentinel,
            read_parquet=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("remote workers must not read a hub-local Parquet path")
            ),
        ),
    ))
    assert runner._build(SimpleNamespace(op="read", config={"uri": source}), {}) is sentinel
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "1")
    assert "above the 1-byte limit" in (
        runner._source_unsupported_reason(
            graph, "src", lower_to_ir(graph, "src", runner.node_specs)) or ""
    )
    monkeypatch.setenv("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", "1048576")

    dispatched = []

    class TrappedThread:
        def __init__(self, *args, **kwargs):
            dispatched.append((args, kwargs))

        def start(self):
            raise AssertionError("a local remote-region handoff must be rejected/fallback before dispatch")

    monkeypatch.setattr(mod.threading, "Thread", TrappedThread)
    local = runner.run_unit(
        graph, "src", str(tmp_path / "local-region.parquet"), run_id="remote_local_fallback"
    )
    assert local.status == "done" and local.placement == "local"
    pinned = runner.run_unit(
        graph, "src", str(tmp_path / "pinned-region.parquet"),
        requires={"labels": {"engine": "ray"}}, run_id="remote_local_pinned",
    )
    assert pinned.status == "failed"
    assert "remote Ray cluster cannot materialize" in (pinned.error or "")
    assert dispatched == []


@pytest.mark.skipif(not os.environ.get("DP_TEST_RAY_LIVE"), reason="live Ray run — opt-in because it needs the [ray] extra + a real Ray executor (slow). Verified passing on macOS AND Linux via the dp_ray subprocess driver (which disables Ray's uv-run worker hook). Enable: DP_TEST_RAY_LIVE=1.")
def test_ray_backend_live_differential(tmp_path):
    # End-to-end: run source→map→filter→write on Ray Data and assert the output equals the DuckDB
    # LocalRunner's, byte-for-byte. Opt-in (Ray's streaming executor needs to spawn workers, which a
    # sandbox/CI often can't); run on a real machine: DP_TEST_RAY_LIVE=1 uv run --no-sync pytest -k live_differential
    import time

    import duckdb

    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.models import Graph

    pytest.importorskip("ray")
    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM range(1,11) t(x)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)

    # source → map(x*2) → filter(x>8) → write: the dp_ray subprocess driver runs the whole graph on Ray
    # and its parquet output must equal the DuckDB LocalRunner's, byte-for-byte.
    def mk(wname):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            _ray_node("src", "source", {"uri": p}),
            _ray_node("m", "transform", {"mode": "map", "code": "def fn(row):\n    row['x'] = row['x'] * 2\n    return row"}),
            _ray_node("f", "transform", {"mode": "filter", "code": "def fn(row):\n    return row['x'] > 8"}),
            _ray_node("w", "write", {"name": wname}),
        ], "edges": [_ray_edge("src", "m"), _ray_edge("m", "f"), _ray_edge("f", "w")]})

    def poll(runner, run_id):
        for _ in range(900):
            st = runner.status(run_id)
            if st.status in ("done", "failed", "cancelled"):
                return st
            time.sleep(0.1)
        return st

    gr = mk("ray_out")
    st_ray = poll(rr, rr.run(compile_plan(gr, "w", deps.registry, deps.node_specs), gr, "w", "local").run_id)
    assert st_ray.status == "done" and st_ray.placement == "distributed", st_ray.error
    gl = mk("local_out")
    st_loc = poll(deps.runner, deps.runner.run(compile_plan(gl, "w", deps.registry, deps.node_specs), gl, "w", "local").run_id)
    assert st_loc.status == "done", st_loc.error

    def rows(uri):
        return sorted(r[0] for r in deps.resolve_adapter(uri).scan(uri).project("x").fetchall())
    assert rows(_output_field(st_ray, "uri", outcome="committed")) == rows(
        _output_field(st_loc, "uri", outcome="committed")) == [10, 12, 14, 16, 18, 20]


def test_ray_opts_maps_region_requires_to_ray_task_placement():
    # the planner's region `requires` → per-Ray-task placement options: gpu → num_gpus (each map task
    # needs a GPU); a non-`engine` label value → a custom resource (declare it via `ray start --resources`);
    # cpu/mem omitted (per-region aggregates, not the per-task cost Ray schedules on). No Ray needed.
    ropts = _load_dp_ray()._ray_opts
    assert ropts(None) == {} and ropts({}) == {}
    assert ropts({"gpu": 2}) == {"num_gpus": 2.0}
    assert ropts({"labels": {"engine": "ray"}}) == {}  # the claim label is not a placement resource
    assert ropts({"labels": {"engine": "ray", "pool": "a100"}}) == {"resources": {"a100": 0.001}}
    assert ropts({"gpu_type": "a100"}) == {"num_gpus": 1.0, "accelerator_type": "A100"}
    assert ropts({"cpu": 8, "mem": "64GB"}) == {}  # aggregates — not mapped to per-task options
    both = ropts({"gpu": 1, "labels": {"pool": "gpu1"}})
    assert both == {"num_gpus": 1.0, "resources": {"gpu1": 0.001}}


def test_ray_gpu_batch_rows_are_validated_capped_and_frozen(monkeypatch):
    mod = _load_dp_ray()
    monkeypatch.delenv("DP_RAY_GPU_BATCH_ROWS", raising=False)
    assert mod._gpu_batch_rows() == mod._GPU_BATCH_ROWS_DEFAULT
    monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", "8192")
    assert mod._gpu_batch_rows() == 8192
    monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", str(mod._GPU_BATCH_ROWS_MAX * 10))
    assert mod._gpu_batch_rows() == mod._GPU_BATCH_ROWS_MAX
    for invalid in ("0", "-1", "many"):
        monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", invalid)
        with pytest.raises(ValueError, match="positive integer"):
            mod._gpu_batch_rows()


def test_ray_custom_labels_are_advertised_to_the_pre_dispatch_gate(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.models import ResourceSpec

    monkeypatch.setenv("DP_RAY_LABELS", "pool=a100, zone=use1, malformed, engine=other")
    (tmp_path / "ws").mkdir()
    rr = _load_dp_ray().RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))

    labels = rr.workers()[0].capacity.labels
    assert labels == {"engine": "ray", "pool": "a100", "zone": "use1"}
    assert rr._resource_unsupported_reason(
        ResourceSpec(labels={"engine": "ray", "pool": "a100"})) is None
    assert "exceed advertised" in rr._resource_unsupported_reason(
        ResourceSpec(labels={"engine": "ray", "pool": "h100"}))


def test_ray_whole_run_enforces_and_forwards_final_region_resources(tmp_path, monkeypatch):
    import duckdb

    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.models import Graph

    (tmp_path / "ws").mkdir()
    mod = _load_dp_ray()
    rr = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    source_uri = str(tmp_path / "input.parquet")
    duckdb.connect().execute(f"COPY (SELECT 1 AS x) TO '{source_uri}' (FORMAT PARQUET)")

    def graph(pool, *, with_write=True):
        nodes = [
            _ray_node("src", "source", {"uri": source_uri, "requires": {
                "labels": {"engine": "ray", "pool": pool}}}),
        ]
        edges = []
        if with_write:
            nodes.append(_ray_node(
                "write", "write", {"filename": str(tmp_path / "ray-final.parquet")}
            ))
            edges.append(_ray_edge("src", "write"))
        return Graph(**{
            "id": "ray-final-placement", "version": 1,
            "nodes": nodes, "edges": edges,
        })

    rejected = graph("h100")
    failed = rr.run(compile_plan(rejected, "write", rr.deps.registry, rr.node_specs),
                    rejected, "write", "local")
    assert failed.status == "failed" and "exceed advertised" in (failed.error or "")
    failed_whole = rr.run(compile_plan(rejected, None, rr.deps.registry, rr.node_specs),
                          rejected, None, "local")
    assert failed_whole.status == "failed" and "exceed advertised" in (failed_whole.error or "")

    monkeypatch.setenv("DP_RAY_LABELS", "pool=a100")
    accepted = graph("a100")
    dispatched = {}

    class _Thread:
        def __init__(self, *, target, args, kwargs, daemon):
            dispatched.update(target=target, args=args, kwargs=kwargs, daemon=daemon)

        def start(self):
            pass

    monkeypatch.setattr(mod.threading, "Thread", _Thread)
    queued = rr.run(compile_plan(accepted, None, rr.deps.registry, rr.node_specs),
                    accepted, None, "local")
    assert queued.status == "queued"
    assert dispatched["kwargs"]["requires"]["labels"] == {"engine": "ray", "pool": "a100"}

    # A whole graph with no selected target and no write has no public output identity. Even with a
    # valid Ray pin it must fail before allocating an attempt or dispatching a driver thread.
    dispatched.clear()
    source_only = graph("a100", with_write=False)
    no_output = rr.run(
        compile_plan(source_only, None, rr.deps.registry, rr.node_specs),
        source_only, None, "local",
    )
    assert no_output.status == "failed"
    assert "no publishable output target" in (no_output.error or "")
    assert dispatched == {}

    # A plugin NodeSpec default is a real placement pin even when node config has no requires override.
    from hub.models import ResourceSpec
    rr.node_specs["sql"] = rr.node_specs["sql"].model_copy(
        update={"requires": ResourceSpec(labels={"engine": "ray"})})
    spec_pinned = Graph(**{"id": "ray-spec-pin", "version": 1, "nodes": [
        _ray_node("src2", "source", {"uri": "unused.parquet"}),
        _ray_node("sql", "sql", {"sql": "SELECT * FROM input"}),
    ], "edges": [_ray_edge("src2", "sql")]})
    pinned = rr.run(compile_plan(spec_pinned, "sql", rr.deps.registry, rr.node_specs),
                    spec_pinned, "sql", "local")
    assert pinned.status == "failed" and "explicitly required" in (pinned.error or "")


def test_ray_shuffle_without_partition_override_uses_materialized_block_count(monkeypatch):
    import pyarrow as pa

    mod = _load_dp_ray()
    rr = object.__new__(mod.RayRunner)
    calls = []
    monkeypatch.delenv("DP_RAY_SHUFFLE_PARTITIONS", raising=False)

    class _Data:
        def materialize(self):
            calls.append("materialize")
            return self

        def num_blocks(self):
            return 3

        def schema(self, fetch_if_missing=True):
            return pa.schema([("k", pa.int64())])

        def repartition(self, *args, **kwargs):
            calls.append((args, kwargs))
            return self

        def map_batches(self, _fn, **_kwargs):
            return self

    data = _Data()
    assert rr._shuffle_duckdb(data, ["k"], "SELECT DISTINCT * FROM _blk") is data
    assert calls == ["materialize", ((3,), {"keys": ["k"]})]


def test_ray_relational_compute_forwards_task_resources_and_rejects_pinned_sort(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pyarrow as pa

    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec

    monkeypatch.setenv("DP_RAY_LABELS", "pool=a100")
    (tmp_path / "ws").mkdir()
    mod = _load_dp_ray()
    rr = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    monkeypatch.setattr(rr, "_source_unsupported_reason", lambda *_args: None)
    gpu_opts = {"num_gpus": 1.0, "accelerator_type": "A100", "resources": {"a100": 0.001}}
    calls = []
    monkeypatch.setenv("DP_RAY_SHUFFLE_PARTITIONS", "4")

    class _Data:
        def __init__(self, refs=None):
            self.refs = refs or []

        def columns(self):
            return ["k"]

        def schema(self, fetch_if_missing=True):
            return pa.schema([("k", pa.int64())])

        def materialize(self):
            return self

        def size_bytes(self):
            return 8

        def count(self):
            return 1

        def repartition(self, *args, **kwargs):
            return self

        def map_batches(self, fn, **kwargs):
            calls.append(kwargs)
            return self

        def to_arrow_refs(self):
            return self.refs

    with pytest.raises(RuntimeError, match="whole hash partition"):
        rr._shuffle_duckdb(_Data(), ["k"], "SELECT DISTINCT * FROM _blk", gpu_opts)
    rr._shuffle_duckdb(_Data(), ["k"], "SELECT DISTINCT * FROM _blk", {"resources": {"a100": 0.001}})
    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(get=lambda refs: [pa.table({"k": [1]})]))
    step = SimpleNamespace(inputs=[("left", None), ("right", None)],
                           config={"how": "inner", "on": "k"})
    rr._build_join(step, {"left": _Data(), "right": _Data(["ref"])}, gpu_opts)
    assert len(calls) == 2
    assert calls[0]["batch_size"] is None and calls[0]["resources"] == {"a100": 0.001}
    assert calls[1]["batch_size"] == mod._GPU_BATCH_ROWS_DEFAULT
    assert calls[1]["num_gpus"] == 1.0 and calls[1]["accelerator_type"] == "A100"
    transform = SimpleNamespace(
        op="map", inputs=[("parent", None)],
        config={"mode": "map", "code": "def fn(row): return row"},
    )
    rr._build(transform, {"parent": _Data()}, gpu_opts)
    assert calls[2]["batch_size"] == mod._GPU_BATCH_ROWS_DEFAULT
    assert calls[2]["accelerator_type"] == "A100"

    monkeypatch.setenv("DP_RAY_GPUS", "2")
    monkeypatch.setenv("DP_RAY_GPU_TYPE", "a100")
    gpu_req = ResourceSpec(gpu_type="a100", labels={"engine": "ray"})
    for op in ("aggregate", "window", "dedup"):
        reason = rr._resource_unsupported_reason(
            gpu_req, SimpleNamespace(steps=[SimpleNamespace(op=op)])
        )
        assert "whole-partition" in reason and op in reason
    monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", "0")
    reason = rr._resource_unsupported_reason(
        gpu_req, SimpleNamespace(steps=[SimpleNamespace(op="map")])
    )
    assert "positive integer" in reason
    monkeypatch.delenv("DP_RAY_GPU_BATCH_ROWS")

    graph = Graph(**{"id": "ray-pinned-sort", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": "unused.parquet"}),
        _ray_node("sort", "sort", {"by": "k"}),
    ], "edges": [_ray_edge("src", "sort")]})
    status = rr.run_unit(
        graph, "sort", str(tmp_path / "sort.parquet"),
        requires=ResourceSpec(labels={"engine": "ray", "pool": "a100"}))
    assert status.status == "failed" and "sort cannot honor" in (status.error or "")


def test_ray_explicit_placement_fails_unsupported_shape_while_unpinned_falls_back(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec

    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    monkeypatch.setattr(rr, "_source_unsupported_reason", lambda *_args: None)
    graph = Graph(**{"id": "ray-placement", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": "unused.parquet"}),
        _ray_node("sql", "sql", {"sql": "SELECT * FROM input"}),
    ], "edges": [_ray_edge("src", "sql")]})
    sentinel = object()
    monkeypatch.setattr(rr, "_materialize_local", lambda *_args, **_kwargs: sentinel)

    assert rr.run_unit(graph, "sql", str(tmp_path / "fallback.parquet"), requires=None) is sentinel

    explicit = rr.run_unit(
        graph,
        "sql",
        str(tmp_path / "must-ray.parquet"),
        requires=ResourceSpec(labels={"engine": "ray"}),
    )
    assert explicit.status == "failed"
    assert explicit.placement == "distributed"
    assert "explicitly required" in (explicit.error or "")
    assert "sql" in (explicit.error or "")
    import uuid
    from sqlalchemy import func, select
    object_run = f"unsupported-object-{uuid.uuid4().hex}"
    object_explicit = rr.run_unit(
        graph, "sql", "s3://no-writer-dispatched/out.parquet",
        requires=ResourceSpec(labels={"engine": "ray"}), run_id=object_run)
    assert object_explicit.status == "failed"
    from hub import metadb
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == object_run)) == 0

    # A GPU claim is itself a hard Ray placement pin. Omitting labels.engine must never turn it
    # into a local DuckDB execution that silently ignores the requested accelerator.
    gpu_explicit = rr.run_unit(
        graph,
        "src",
        str(tmp_path / "must-gpu.parquet"),
        requires=ResourceSpec(gpu=1),
    )
    assert gpu_explicit.status == "failed"
    assert gpu_explicit.placement == "distributed"
    assert "requested resources" in (gpu_explicit.error or "")


def test_ray_explicit_placement_fails_unadvertised_resources_before_dispatch(tmp_path, monkeypatch):
    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec

    monkeypatch.setenv("DP_RAY_GPUS", "1")
    monkeypatch.setenv("DP_RAY_GPU_TYPE", "a100")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    monkeypatch.setattr(rr, "_source_unsupported_reason", lambda *_args: None)
    graph = Graph(**{"id": "ray-resource-placement", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": "unused.parquet"}),
    ], "edges": []})

    status = rr.run_unit(
        graph,
        "src",
        str(tmp_path / "must-ray.parquet"),
        requires=ResourceSpec(gpu=2, gpu_type="a100", labels={"engine": "ray"}),
    )

    assert status.status == "failed"
    assert "requested resources" in (status.error or "")
    assert "advertised Ray capacity" in (status.error or "")


def test_ray_region_handoff_uses_an_immutable_attempt_prefix(monkeypatch):
    import hashlib
    import json

    from hub import metadb

    owner = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(metadb, "object_attempt_owner_id", lambda: owner)
    attempt_uri = _load_dp_ray()._attempt_handoff_uri

    def digest_hex(uri, run_id, scope=None):
        doc = {"runId": run_id, "scope": scope, "uri": uri}
        if uri.startswith(("s3://", "gs://", "gcs://", "r2://")):
            doc["owner"] = owner
        identity = json.dumps(doc,
                              ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    first = attempt_uri("s3://bucket/regions/node_hash.parquet", "unit_first")
    second = attempt_uri("s3://bucket/regions/node_hash.parquet", "unit_second")

    assert first == (
        "s3://bucket/regions/node_hash.attempt-unit_first-"
        + digest_hex("s3://bucket/regions/node_hash.parquet", "unit_first")
    )
    assert second == (
        "s3://bucket/regions/node_hash.attempt-unit_second-"
        + digest_hex("s3://bucket/regions/node_hash.parquet", "unit_second")
    )
    assert first != second
    unsafe = attempt_uri("/tmp/out.parquet", "../../unsafe run")
    collision = attempt_uri("/tmp/out.parquet", "unsafe run")
    assert unsafe.startswith("/tmp/out.attempt-unsafe_run-") and unsafe != collision
    assert attempt_uri("/tmp/out.parquet", "same", scope="w") != \
        attempt_uri("/tmp/out.pq", "same", scope="w")
    assert attempt_uri("/tmp/out.parquet", "same", scope="w1") != \
        attempt_uri("/tmp/out.parquet", "same", scope="w2")
    long_uri = attempt_uri("/tmp/out.parquet", "x" * 10_000)
    readable, suffix = long_uri.split(".attempt-", 1)[1].rsplit("-", 1)
    assert len(readable) == 64 and len(suffix) == 32
    owner_a_uri = attempt_uri("s3://bucket/out.parquet", "same", scope="w")
    monkeypatch.setattr(metadb, "object_attempt_owner_id", lambda: "f" * 32)
    assert owner_a_uri != attempt_uri("s3://bucket/out.parquet", "same", scope="w")


def test_ray_attempt_uri_fits_local_components_and_object_child_paths(tmp_path):
    from urllib.parse import urlsplit

    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.handoff import _object_manifest_path, read_manifest

    mod = _load_dp_ray()
    schema = pa.schema([("value", pa.int64())])

    class EmptyDataset:
        def materialize(self):
            return self

        def count(self):
            return 0

        def schema(self, fetch_if_missing=True):
            return schema

    logical_names = ["x" * 180 + ".parquet", "数据" * 40 + ".parquet"]
    for index, logical_name in enumerate(logical_names):
        logical_uri = str(tmp_path / logical_name)
        run_id = f"local-{index}-" + "r" * 10_000
        attempt = mod._attempt_handoff_uri(logical_uri, run_id)
        assert len(attempt.rsplit("/", 1)[-1].encode("utf-8")) <= 240
        assert mod._write_worker_direct_parquet(
            EmptyDataset(), attempt, attempt_id=run_id
        ) == (0, attempt)
        assert read_manifest(attempt)["runId"] == run_id
        assert pq.read_table(os.path.join(attempt, "part-000000.parquet")).schema == schema

    # Object attempts leave 128 bytes below the provider's 1024-byte key ceiling for Ray's shard name
    # and the sibling commit record. The digest still distinguishes long IDs after readable truncation.
    parent = "p" * 830
    logical = f"s3://bucket/{parent}/out.parquet"
    first = mod._attempt_handoff_uri(logical, "a" * 10_000)
    second = mod._attempt_handoff_uri(logical, "b" * 10_000)
    assert first != second
    for attempt in (first, second):
        parsed = urlsplit(attempt)
        key = parsed.path.lstrip("/")
        assert len(key.encode("utf-8")) <= 896
        assert len(key.encode("utf-8")) + 128 <= 1024
        assert len((key + "/part-000000.parquet").encode("utf-8")) <= 1024
        manifest_path = _object_manifest_path(f"{parsed.netloc}/{key}")
        manifest_key = manifest_path.split("/", 1)[1]
        assert len(manifest_key.encode("utf-8")) <= 1024

    too_deep = f"s3://bucket/{'p' * 860}/out.parquet"
    with pytest.raises(RuntimeError, match="shorten its parent path"):
        mod._attempt_handoff_uri(too_deep, "run")


def test_ray_region_reattaches_committed_attempt_and_refuses_partial_retry(tmp_path):
    import duckdb

    from hub.deps import Deps
    from hub.handoff import write_manifest
    from hub.models import Graph

    (tmp_path / "ws").mkdir()
    mod = _load_dp_ray()
    rr = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    graph = Graph(**{"id": "reattach", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": "unused.parquet"}),
    ], "edges": []})

    suggested = str(tmp_path / "region.parquet")
    run_id = "unit_committed"
    attempt = mod._attempt_handoff_uri(suggested, run_id)
    os.makedirs(attempt)
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS x) TO '{attempt}/part-0.parquet' (FORMAT PARQUET)")
    write_manifest(attempt, run_id=run_id, rows=1, schema="x: int64")
    rr._acknowledge_cancel(run_id)  # a prior owner reused this explicit ID; registration must clear it
    done = rr.run_unit(graph, "src", suggested, run_id=run_id)
    assert done.status == "done" and done.rows_processed == 1
    assert _output_field(done, "uri", outcome="committed") == attempt
    assert not rr.cancel_acknowledged(run_id)
    restarted = mod.RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    reattached = restarted.run_unit(graph, "src", suggested, run_id=run_id)
    assert reattached.status == "done"
    assert _output_field(reattached, "uri", outcome="committed") == attempt

    partial_id = "unit_partial"
    partial = mod._attempt_handoff_uri(suggested, partial_id)
    os.makedirs(partial)
    duckdb.connect().execute(
        f"COPY (SELECT 2 AS x) TO '{partial}/part-0.parquet' (FORMAT PARQUET)")
    failed = rr.run_unit(graph, "src", suggested, run_id=partial_id)
    assert failed.status == "failed" and "refusing to overwrite" in (failed.error or "")

    wrong_id = "unit_wrong_manifest"
    wrong = mod._attempt_handoff_uri(suggested, wrong_id)
    os.makedirs(wrong)
    duckdb.connect().execute(
        f"COPY (SELECT 3 AS x) TO '{wrong}/part-0.parquet' (FORMAT PARQUET)")
    write_manifest(wrong, run_id="different-owner", rows=1, schema="x: int64")
    wrong_status = mod.RayRunner(
        Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    ).run_unit(graph, "src", suggested, run_id=wrong_id)
    assert wrong_status.status == "failed" and "refusing to overwrite" in (wrong_status.error or "")


def test_region_attempt_requires_a_valid_commit_manifest(tmp_path):
    # Shards alone are not a published handoff. The controller must reject a partial/corrupt attempt and
    # accept it only after the writer's last operation installs a valid success manifest.
    import duckdb

    from hub.handoff import MANIFEST_NAME, write_manifest

    attempt = tmp_path / "region.attempt-unit_1"
    attempt.mkdir()
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS x) TO '{attempt / 'part-0.parquet'}' (FORMAT PARQUET)")
    ctrl = get_deps().controller
    assert ctrl._region_output_exists(str(attempt)) is False
    (attempt / MANIFEST_NAME).write_text("{broken")
    assert ctrl._region_output_exists(str(attempt)) is False
    write_manifest(str(attempt), run_id="unit_1", rows=1, schema="x: int64")
    assert ctrl._region_output_exists(str(attempt)) is True
    duckdb.connect().execute(
        f"COPY (SELECT 2 AS x) TO '{attempt / 'unexpected.parquet'}' (FORMAT PARQUET)")
    assert ctrl._region_output_exists(str(attempt)) is False  # extra shard is also a partial/mixed attempt
    os.remove(attempt / "unexpected.parquet")
    assert ctrl._region_output_exists(str(attempt)) is True
    os.remove(attempt / "part-0.parquet")
    assert ctrl._region_output_exists(str(attempt)) is False  # lifecycle/manual shard loss => recompute


def test_region_attempt_cleanup_is_scoped_to_an_attempt(tmp_path):
    from hub import handoff

    assert handoff._object_manifest_path("bucket/base/regions/out.attempt-a") == \
        "bucket/base/regions/_dp_commits/out.attempt-a/_DP_SUCCESS.json"

    stable = tmp_path / "stable"
    failed = tmp_path / "region.attempt-failed"
    stable.mkdir()
    failed.mkdir()
    (stable / "keep").write_text("stable")
    (failed / "part.parquet").write_text("partial")
    handoff.discard_attempt(str(stable))
    handoff.discard_attempt(str(failed))
    assert stable.exists() and not failed.exists()  # the cleanup API can never delete a stable prefix


def test_object_attempt_registry_serializes_publish_and_discard():
    import threading

    from hub import handoff, metadb

    logical = "s3://registry-lock-test/out.parquet"
    uris = [f"s3://registry-lock-test/out.attempt-{suffix}" for suffix in ("a", "b")]
    for index, uri in enumerate(uris):
        metadb.allocate_object_attempt(
            logical_uri=logical, kind="sink", run_id=f"registry-lock-{index}",
            allocation_key=f"registry-lock-{index}", catalog_key_base="tbl_out",
            uri_factory=lambda _namespace, _generation, _attempt_id, uri=uri: uri)
        key = uri.removeprefix("s3://") + "/part.parquet"
        metadb.record_object_attempt_commit(uri, [{
            "member_id": handoff._member_id("unversioned_object", key, "null"),
            "key": key, "member_type": "unversioned_object", "size": 1,
            "etag": None, "version_id": None, "upload_id": None,
            "is_latest": True, "is_commit": False,
        }])

    barrier = threading.Barrier(3)
    errors = []

    def publish(uri):
        try:
            barrier.wait()
            metadb.catalog_upsert_entry(
                uri, "out", {"id": "tbl_out", "name": "out", "uri": uri})
        except Exception as exc:  # noqa: BLE001 — asserted below after both threads join
            errors.append(exc)
            metadb.abandon_committed_object_attempt(uri)

    threads = [threading.Thread(target=publish, args=(uri,)) for uri in uris]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(errors) <= 1
    with metadb.session() as session:
        states = {uri: session.get(metadb.ObjectAttempt, uri).state for uri in uris}
        published = [uri for uri, state in states.items() if state == "published"]
        assert len(published) == 1
        assert metadb.catalog_get(uris[0])["uri"] == published[0]
        assert states[next(uri for uri in uris if uri != published[0])] in (
            "superseded", "abandoned")

    fenced = "s3://registry-lock-test/out.attempt-fenced"
    metadb.allocate_object_attempt(
        logical_uri=logical, kind="sink", run_id="registry-lock-fenced",
        allocation_key="registry-lock-fenced", catalog_key_base="tbl_out",
        uri_factory=lambda _namespace, _generation, _attempt_id: fenced)
    assert metadb.mark_object_attempt_terminal(fenced) is True
    with pytest.raises(RuntimeError, match="terminal proof"):
        metadb.catalog_upsert_entry(
            fenced, "out", {"id": "tbl_out", "name": "out", "uri": fenced})
    # Unregister the durable catalog pointer before quarantining its physical attempt. Cleanup must
    # exercise the same ownership boundary as production rather than bypassing a live reference.
    metadb.catalog_delete_entry(published[0])
    for uri in uris:
        metadb.quarantine_object_attempt(uri, "test cleanup")
    metadb.quarantine_object_attempt(fenced, "test cleanup")


def test_object_attempt_registry_reaps_superseded_and_fenced_failures(
        monkeypatch, object_store_cred):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    import pyarrow as pa
    import pyarrow.fs as pafs
    from moto.server import ThreadedMotoServer

    from hub import handoff, metadb
    from hub.models import RunOutput
    from hub.plugins.adapters import object_fs

    server = ThreadedMotoServer(port=0)
    server.start()
    cleanup = []
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1",
        )
        client.create_bucket(Bucket="bkt")
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
        monkeypatch.setenv("DP_ATTEMPT_INVENTORY_QUIET_SECONDS", "0")
        def land(uri, run_id):
            fs, path = object_fs(uri)
            with fs.open_output_stream(path + "/part-000000.parquet") as stream:
                stream.write(run_id.encode())
            handoff.write_manifest(uri, run_id=run_id, rows=1, schema="x: int64")

        def reserve(uri, logical_uri, kind, run_id):
            return metadb.allocate_object_attempt(
                logical_uri=logical_uri, kind=kind, run_id=run_id,
                allocation_key=f"legacy-registry-test:{run_id}",
                catalog_key_base=(f"tbl_{run_id}" if kind == "sink" else None),
                uri_factory=lambda _namespace, _generation, _attempt_id: uri)

        def reap_until_deleted(uri):
            import datetime

            for _ in range(5):
                with metadb.session() as session:
                    row = session.get(metadb.ObjectAttempt, uri)
                    if row is None or row.state == "deleted":
                        return
                    now = metadb._db_now(session)
                    if row.quiet_until is not None:
                        row.quiet_until = now - datetime.timedelta(seconds=1)
                    if row.next_delete_at is not None:
                        row.next_delete_at = now - datetime.timedelta(seconds=1)
                    if row.delete_empty_observed_at is not None:
                        row.delete_empty_observed_at = now - datetime.timedelta(seconds=1)
                result = handoff.reap_attempts(
                    retention_seconds=0, delete_grace_seconds=0)
                if uri in result["deleted"]:
                    return
            raise AssertionError(f"attempt did not pass the final-empty barrier: {uri}")

        sink_logical = "s3://bkt/outputs/out.parquet"
        sink_old = "s3://bkt/outputs/out.attempt-old"
        sink_new = "s3://bkt/outputs/out.attempt-new"
        for uri, run_id in ((sink_old, "old"), (sink_new, "new")):
            reserve(uri, sink_logical, "sink", run_id)
            land(uri, run_id)
            handoff.prepare_attempt_commit(uri)
            metadb.catalog_upsert_entry(uri, "out", {"id": f"tbl_{run_id}", "name": "out", "uri": uri})
        cleanup.append(sink_new)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, sink_old).state == "superseded"
            assert session.get(metadb.ObjectAttempt, sink_new).state == "published"

        # A generic failure cleanup call cannot delete an already published winner.
        handoff.discard_attempt(sink_new)
        new_fs, new_path = object_fs(sink_new)
        assert handoff.read_manifest(sink_new) is not None
        assert new_fs.get_file_info(new_path + "/part-000000.parquet").size > 0
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, sink_new).state == "published"

        first = handoff.reap_attempts(retention_seconds=3600, delete_grace_seconds=3600)
        assert first["deleted"] == []
        assert metadb.catalog_get(sink_old)["uri"] == sink_new
        assert handoff.read_manifest(sink_old) is not None  # retirement never mutates objects before GC
        fs, old_path = object_fs(sink_old)
        assert fs.get_file_info(old_path + "/part-000000.parquet").size > 0  # active readers get grace
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, sink_old).state == "superseded"

        reap_until_deleted(sink_old)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, sink_old).state == "deleted"

        # Region retirement follows the exact result-cache pointer swap, not an object-prefix listing or
        # wall-clock guess. An active cache URI is retained; the replaced URI enters the same two phases.
        region_logical = "s3://bkt/regions/r_src_hash.parquet"
        region_old = "s3://bkt/regions/r_src_hash.attempt-old"
        region_new = "s3://bkt/regions/r_src_hash.attempt-new"
        for uri, run_id in ((region_old, "region-old"), (region_new, "region-new")):
            reserve(uri, region_logical, "region", run_id)
            land(uri, run_id)
            handoff.prepare_attempt_commit(uri)
        def result_doc(uri: str) -> dict:
            return {"outputs": [RunOutput(
                node_id="src", port_id="out", wire="dataset",
                publication_kind="result", outcome="committed", uri=uri, rows=1,
            ).model_dump()]}

        assert metadb.put_result("object-gc-region-key", result_doc(region_old)) == []
        assert metadb.put_result(
            "object-gc-region-key", result_doc(region_new)) == [region_old]
        cleanup.append(region_new)
        region_phase1 = handoff.reap_attempts(retention_seconds=3600, delete_grace_seconds=0)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, region_old).state == "superseded"
        assert region_phase1["deleted"] == []
        reap_until_deleted(region_old)

        # RunState and age are not writer-exit proof. Only the supervisor that observed child exit may
        # fence and discard its exact unpublished attempt.
        partial = "s3://bkt/outputs/live.attempt-partial"
        reserve(partial, "s3://bkt/outputs/live.parquet", "sink", "live-object-gc")
        partial_fs, partial_path = object_fs(partial)
        with partial_fs.open_output_stream(partial_path + "/part-000000.parquet") as stream:
            stream.write(b"partial")
        metadb.save_run_state("live-object-gc", {"run_id": "live-object-gc", "status": "running"})
        assert handoff.reap_attempts(
            retention_seconds=3600, delete_grace_seconds=0)["deleted"] == []
        assert partial_fs.get_file_info(partial_path + "/part-000000.parquet").size > 0
        metadb.save_run_state("live-object-gc", {"run_id": "live-object-gc", "status": "failed"})
        assert handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"] == []
        assert partial_fs.get_file_info(partial_path + "/part-000000.parquet").size > 0
        handoff.discard_attempt(partial)
        assert partial_fs.get_file_info(partial_path + "/part-000000.parquet").size > 0
        reap_until_deleted(partial)
        assert partial_fs.get_file_info(partial_path + "/part-000000.parquet").type == \
            pafs.FileType.NotFound

        orphan = "s3://bkt/outputs/orphan.attempt-crashed-hub"
        reserve(orphan, "s3://bkt/outputs/orphan.parquet", "sink", "missing-run-owner")
        orphan_fs, orphan_path = object_fs(orphan)
        with orphan_fs.open_output_stream(orphan_path + "/part-000000.parquet") as stream:
            stream.write(b"partial")
        assert handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"] == []
        assert orphan_fs.get_file_info(orphan_path + "/part-000000.parquet").size > 0
        handoff.discard_attempt(orphan)  # explicit terminal-backend acknowledgement in this test
        reap_until_deleted(orphan)

        # A failed driver cannot delete object data merely because its own process is unwinding: remote
        # Ray workers may still be writing. The durable backend reconciler/provider lifecycle owns it.
        worker_failed = "s3://bkt/outputs/worker.attempt-driver-failed"
        reserve(worker_failed, "s3://bkt/outputs/worker.parquet", "sink", "driver-failed")

        class FailedRemoteWrite:
            def materialize(self):
                return self

            def count(self):
                return 1

            def schema(self):
                return pa.schema([("x", pa.int64())])

            @staticmethod
            def write_parquet(path, *, filesystem, **_kwargs):
                with filesystem.open_output_stream(path.rstrip("/") + "/late.parquet") as stream:
                    stream.write(b"still-writing")
                raise RuntimeError("driver lost a remote task")

        with pytest.raises(RuntimeError, match="driver lost"):
            _load_dp_ray()._write_worker_direct_parquet(
                FailedRemoteWrite(), worker_failed, attempt_id="driver-failed"
            )
        failed_fs, failed_path = object_fs(worker_failed)
        assert failed_fs.get_file_info(failed_path + "/late.parquet").size > 0
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttempt, worker_failed).state == "writing"
        handoff.discard_attempt(worker_failed)  # simulated durable terminal acknowledgement
        reap_until_deleted(worker_failed)

        # Missing rows have no tombstone/CAS fence, so generic cleanup must leave legacy data alone.
        legacy = "s3://bkt/outputs/legacy.attempt-unregistered"
        legacy_fs, legacy_path = object_fs(legacy)
        with legacy_fs.open_output_stream(legacy_path + "/part.parquet") as stream:
            stream.write(b"legacy")
        handoff.discard_attempt(legacy)
        assert legacy_fs.get_file_info(legacy_path + "/part.parquet").size > 0
        legacy_fs.delete_dir(legacy_path)

        keys = [item["Key"] for item in client.list_objects_v2(Bucket="bkt").get("Contents", [])]
        assert not any("attempt-old" in key or "attempt-partial" in key or "crashed-hub" in key
                       for key in keys), keys
        assert any("attempt-new" in key for key in keys)
    finally:
        try:
            metadb.catalog_delete_entry("s3://bkt/outputs/out.attempt-new")
            # Result-cache documents cannot use a tombstone shape. Remove the durable cache row and its
            # exact object-attempt reference in one test cleanup transaction, as retention would.
            with metadb.session() as session:
                cache = session.get(
                    metadb.ResultCache, "object-gc-region-key", with_for_update=True)
                if cache is not None:
                    metadb._replace_attempt_ref(
                        session, "result_cache", "object-gc-region-key", None)
                    session.delete(cache)
            reap_until_deleted("s3://bkt/outputs/out.attempt-new")
        except Exception:
            pass
        server.stop()
        object_store_cred(None)


def test_ray_region_worker_direct_write_and_progress(tmp_path):
    # opt-in live Ray: run_unit (region mode) writes WORKER-DIRECT — the output is a DIRECTORY of parquet
    # shards (each written in parallel by a Ray task, no driver collect/OOM), readable + correct; and the
    # placed sub-run's progress reaches 1.0 (the seam that surfaces a placed region's progress).
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray region test")
    import glob as _glob
    import time

    import duckdb

    from hub.deps import Deps
    from hub.models import Graph
    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM range(1,21) t(x)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    # a small per-row busy loop (sandbox-safe — no import) widens the compute window so the interim-
    # progress poll below can catch a mid-run value (the seam surfaces a placed region's progress, not
    # just its terminal state).
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        _ray_node("m", "transform", {"mode": "map", "code": "def fn(row):\n    t = 0\n    for _i in range(400000):\n        t += _i\n    row['x'] = row['x'] * 2\n    return row"}),
    ], "edges": [_ray_edge("src", "m")]})
    suggested = str(tmp_path / "region_out.parquet")  # a single-file uri — worker-direct makes it a DIR
    st = rr.run_unit(g, "m", suggested)
    saw_interim = False
    for _ in range(900):
        s = rr.status(st.run_id)
        if s.status == "running" and s.progress is not None and 0.0 < s.progress < 1.0:
            saw_interim = True  # a placed region's progress advanced mid-run, before completion
        if s.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert s.status == "done", s.error
    assert s.progress == 1.0                                   # the placed sub-run reported terminal progress
    assert saw_interim, "the placed sub-run's progress never surfaced an interim value to the parent"
    d = _output_field(s, "uri", outcome="committed")
    assert os.path.isdir(d), f"worker-direct write must produce a DIRECTORY of shards, got {d}"
    assert d != suggested and ".attempt-" in d
    manifest_path = os.path.join(d, "_DP_SUCCESS.json")
    assert os.path.isfile(manifest_path), "completed handoff has no commit manifest"
    import json as _json
    with open(manifest_path) as manifest_file:
        manifest = _json.load(manifest_file)
    assert manifest["format"] == "data-playground-ray-handoff-v2"
    assert manifest["runId"] == st.run_id and manifest["rows"] == 20
    assert manifest["shards"] and all(x["path"].endswith(".parquet") for x in manifest["shards"])
    assert _glob.glob(os.path.join(d, "**", "*.parquet"), recursive=True), "no parquet shards written"
    got = sorted(r[0] for r in duckdb.connect().execute(f"SELECT x FROM read_parquet('{d}/**/*.parquet')").fetchall())
    assert got == [2 * i for i in range(1, 21)]

    # Re-materializing the same content-addressed suggestion publishes a NEW immutable physical prefix.
    # A concurrent/failed attempt can therefore never mix its shards into the committed result.
    st2 = rr.run_unit(g, "m", suggested)
    for _ in range(900):
        s2 = rr.status(st2.run_id)
        if s2.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert s2.status == "done", s2.error
    retry_uri = _output_field(s2, "uri", outcome="committed")
    assert retry_uri != d
    assert os.path.isfile(os.path.join(retry_uri, "_DP_SUCCESS.json"))
    again = sorted(r[0] for r in duckdb.connect().execute(
        f"SELECT x FROM read_parquet('{retry_uri}/**/*.parquet')").fetchall())
    assert again == [2 * i for i in range(1, 21)]
    original = sorted(r[0] for r in duckdb.connect().execute(f"SELECT x FROM read_parquet('{d}/**/*.parquet')").fetchall())
    assert original == again, "publishing a retry mutated the previously committed attempt"


def test_ray_aggregate_live_differential(tmp_path):
    # opt-in live Ray: a distributed GROUP BY (count/min/max) via Ray Data's native HASH shuffle must be
    # byte-identical to DuckDB's single-node aggregate. Canonical comparison: same schema + same rows as a
    # SORTED multiset (both engines emit aggregate rows in arbitrary order), NULLs included. Forces a real
    # multi-partition shuffle (DP_RAY_SHUFFLE_PARTITIONS=4) so the exchange actually crosses partitions.
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray aggregate differential")
    import time

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    os.environ["DP_RAY_SHUFFLE_PARTITIONS"] = "4"
    p = str(tmp_path / "events.parquet")
    # cat has ~5 groups incl. some NULLs (exercises count(*) vs count(col) null semantics); v is int.
    duckdb.connect().execute(
        f"COPY (SELECT (CASE WHEN i % 37 = 0 THEN NULL ELSE i % 5 END) AS cat, i AS v "
        f"FROM range(0, 4000) t(i)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        # DuckDB runs the WHOLE aggregate list per partition — incl. stddev, which the old Ray-native-op
        # approach couldn't express at all — proving the shuffle+DuckDB design handles any DuckDB aggregate.
        _ray_node("a", "aggregate", {"groupBy": "cat",
                                     "aggs": "count(*) AS n, count(v) AS nv, min(v) AS lo, max(v) AS hi, "
                                             "sum(v) AS sm, avg(v) AS av, stddev(v) AS sd"}),
    ], "edges": [_ray_edge("src", "a")]})
    st = rr.run_unit(g, "a", str(tmp_path / "agg_out.parquet"))
    for _ in range(900):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    st = rr.status(st.run_id)
    assert st.status == "done", st.error
    # the DuckDB oracle: same graph on the single-node engine
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="a").relation("a").to_arrow_table()
    con = duckdb.connect()
    con.register("oracle", oracle)
    ray_uri = _output_field(st, "uri", outcome="committed")
    ray_src = f"read_parquet('{ray_uri}/**/*.parquet', union_by_name=true)"  # reconcile per-shard drift
    q = "SELECT cat, n, nv, lo, hi, sm, av, sd FROM {src}"
    rmap = {(-1 if r[0] is None else r[0]): r for r in con.execute(q.format(src=ray_src)).fetchall()}
    dmap = {(-1 if r[0] is None else r[0]): r for r in con.execute(q.format(src="oracle")).fetchall()}
    assert set(rmap) == set(dmap) and len(dmap) >= 5  # same groups (incl. the NULL cat)
    for k, dr in dmap.items():
        rr_ = rmap[k]
        # DuckDB computes each COMPLETE group per partition → count/min/max/sum(int) and avg(int)=
        # exact-sum/exact-count are all bit-identical to the single-node oracle (cols cat..av).
        assert rr_[:7] == dr[:7], f"exact cols differ for cat={k}: ray={rr_} duck={dr}"
        # stddev accumulates squared deviations in float; within-group row order can differ across the
        # shuffle, so it matches within float tolerance (not bit-for-bit) — still DuckDB's own function.
        assert abs((rr_[7] or 0.0) - (dr[7] or 0.0)) <= 1e-9 * max(1.0, abs(dr[7] or 0.0)), f"stddev tol cat={k}"


def test_ray_aggregate_float_nan_and_allnull_parity(tmp_path):
    # P0-DIST-01: the audit found Ray's NATIVE Min/Max aggregators diverged from DuckDB on FLOAT columns
    # (NaN treated as largest by DuckDB but skipped by Ray; signed-zero not preserved) and lost the type
    # on an all-null column. The backend now hash-shuffles by the group key and runs DuckDB GROUP BY per
    # COMPLETE partition, so min/max are computed by DuckDB — byte-identical by construction. Prove it on
    # the exact divergent fixtures, comparing VALUES (NaN-safe via a string cast) AND schema.
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray float/null aggregate parity differential")
    import time

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    os.environ["DP_RAY_SHUFFLE_PARTITIONS"] = "4"
    p = str(tmp_path / "floats.parquet")
    # group g; f is DOUBLE with NaN, -0.0/+0.0, and normal values; an all-null DOUBLE column `an`
    duckdb.connect().execute(
        "COPY (SELECT * FROM (VALUES "
        "(0, 'nan'::DOUBLE, NULL::DOUBLE), (0, 1.0, NULL), (0, 2.0, NULL), "
        "(1, '-0.0'::DOUBLE, NULL), (1, '0.0'::DOUBLE, NULL), "
        "(2, -5.0, NULL), (2, 'nan'::DOUBLE, NULL)) t(g, f, an)) "
        f"TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        _ray_node("a", "aggregate", {"groupBy": "g", "aggs": "min(f) AS lo, max(f) AS hi, "
                                     "min(an) AS anlo, max(an) AS anhi"}),
    ], "edges": [_ray_edge("src", "a")]})
    st = rr.run_unit(g, "a", str(tmp_path / "f_out.parquet"))
    for _ in range(900):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    st = rr.status(st.run_id)
    assert st.status == "done", st.error
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="a").relation("a").to_arrow_table()
    con = duckdb.connect()
    con.register("oracle", oracle)
    ray_uri = _output_field(st, "uri", outcome="committed")
    ray_src = f"read_parquet('{ray_uri}/**/*.parquet', union_by_name=true)"
    # cast the floats to VARCHAR so NaN ('nan') and signed zero ('-0.0'/'0.0') compare faithfully
    q = ("SELECT g, CAST(lo AS VARCHAR), CAST(hi AS VARCHAR), "
         "CAST(anlo AS VARCHAR), CAST(anhi AS VARCHAR) FROM {src}")
    rmap = {r[0]: r for r in con.execute(q.format(src=ray_src)).fetchall()}
    dmap = {r[0]: r for r in con.execute(q.format(src="oracle")).fetchall()}
    assert set(rmap) == set(dmap) == {0, 1, 2}
    for k in dmap:
        assert rmap[k] == dmap[k], f"float min/max diverged for g={k}: ray={rmap[k]} duck={dmap[k]}"
    # schema parity: the all-null column keeps DuckDB's DOUBLE type on both sides (not an untyped null)
    rsch = con.execute(f"SELECT anlo, anhi FROM {ray_src} LIMIT 0").description
    osch = con.execute("SELECT anlo, anhi FROM oracle LIMIT 0").description
    assert [c[1] for c in rsch] == [c[1] for c in osch]  # same declared column types


def test_ray_window_live_differential(tmp_path):
    # opt-in live Ray: a distributed WINDOW (row_number PARTITION BY cat ORDER BY v) via the SAME
    # shuffle+DuckDB mechanism — hash-shuffle by the partition key so each window-partition is complete on
    # one node, DuckDB runs the window per partition — must be byte-identical to single-node DuckDB. v is
    # globally unique, so the row_number within each cat is deterministic (no ties).
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray window differential")
    import time

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    os.environ["DP_RAY_SHUFFLE_PARTITIONS"] = "4"
    p = str(tmp_path / "events.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT (i % 5) AS cat, i AS v FROM range(0, 4000) t(i)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        _ray_node("w", "window", {"expr": "row_number()", "partitionBy": "cat", "orderBy": "v", "as": "rn"}),
    ], "edges": [_ray_edge("src", "w")]})
    st = rr.run_unit(g, "w", str(tmp_path / "win_out.parquet"))
    for _ in range(900):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    st = rr.status(st.run_id)
    assert st.status == "done", st.error
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="w").relation("w").to_arrow_table()
    con = duckdb.connect()
    con.register("oracle", oracle)
    ray_uri = _output_field(st, "uri", outcome="committed")
    ray_src = f"read_parquet('{ray_uri}/**/*.parquet', union_by_name=true)"
    q = "SELECT v, cat, rn FROM {src} ORDER BY v"        # v is unique → deterministic total order
    ray_rows = con.execute(q.format(src=ray_src)).fetchall()
    duck_rows = con.execute(q.format(src="oracle")).fetchall()
    assert ray_rows == duck_rows, f"Ray window != DuckDB\nray[:5]={ray_rows[:5]}\nduck[:5]={duck_rows[:5]}"
    assert len(ray_rows) == 4000 and max(r[2] for r in ray_rows) == 800  # 5 cats × 800 rows each


def test_ray_dedup_live_differential(tmp_path):
    # opt-in live Ray: full-row DISTINCT via shuffle-by-all-columns → DuckDB DISTINCT per partition must
    # equal single-node DuckDB. Every surviving row is identical to the dups it replaces, so it's a clean
    # multiset comparison (order-independent, deterministic).
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray dedup differential")
    import time

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    os.environ["DP_RAY_SHUFFLE_PARTITIONS"] = "4"
    p = str(tmp_path / "dups.parquet")
    # 4000 rows over exactly 200 distinct (cat, v) pairs (cat 0..19 × v 0..9, independent) → 20 dups each,
    # spread across partitions so dedup must colocate identical rows via the all-column shuffle
    duckdb.connect().execute(
        f"COPY (SELECT (i % 20) AS cat, ((i // 20) % 10) AS v FROM range(0, 4000) t(i)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        _ray_node("d", "dedup", {}),                        # no `on` → full-row DISTINCT
    ], "edges": [_ray_edge("src", "d")]})
    st = rr.run_unit(g, "d", str(tmp_path / "dedup_out.parquet"))
    for _ in range(900):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    st = rr.status(st.run_id)
    assert st.status == "done", st.error
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="d").relation("d").to_arrow_table()
    con = duckdb.connect()
    con.register("oracle", oracle)
    ray_uri = _output_field(st, "uri", outcome="committed")
    ray_src = f"read_parquet('{ray_uri}/**/*.parquet', union_by_name=true)"
    q = "SELECT cat, v FROM {src} ORDER BY cat, v"
    ray_rows = con.execute(q.format(src=ray_src)).fetchall()
    duck_rows = con.execute(q.format(src="oracle")).fetchall()
    assert ray_rows == duck_rows and len(ray_rows) == 200, f"dedup differs: {len(ray_rows)} vs {len(duck_rows)}"


def test_ray_join_live_differential(tmp_path):
    # opt-in live Ray: a BROADCAST join (big LEFT fact ⋈ small RIGHT dim on user_id) must equal single-node
    # DuckDB — same join_sql, so the coalesced USING key + _2-suffix naming match exactly. Join output
    # order is arbitrary, so compare as a sorted multiset.
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray join differential")
    import time

    import duckdb

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    left_p, right_p = str(tmp_path / "fact.parquet"), str(tmp_path / "dim.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT (i % 100) AS user_id, i AS amount FROM range(0, 4000) t(i)) TO '{left_p}' (FORMAT PARQUET)")
    duckdb.connect().execute(  # small dim (broadcast side); user_id 99 missing → exercises LEFT; a clashing
        # `amount` column forces the _2-suffix naming so the differential also proves that on the Ray path
        f"COPY (SELECT i AS user_id, ('u' || i) AS name, (i * 10) AS amount FROM range(0, 99) t(i)) TO '{right_p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)

    def mk(how):
        return Graph(**{"id": "c", "version": 1, "nodes": [
            _ray_node("l", "source", {"uri": left_p}), _ray_node("r", "source", {"uri": right_p}),
            _ray_node("j", "join", {"on": "user_id", "how": how}),
        ], "edges": [_ray_edge("l", "j"), _ray_edge("r", "j")]})

    con = duckdb.connect()
    for how, expect in (("inner", 3960), ("left", 4000)):        # 40 rows have user_id 99 (no dim match)
        g = mk(how)
        st = rr.run_unit(g, "j", str(tmp_path / f"join_{how}.parquet"))
        for _ in range(900):
            if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.1)
        st = rr.status(st.run_id)
        assert st.status == "done", f"{how}: {st.error}"
        with db.run_scope():
            oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                                 node_specs=deps.node_specs, output_node="j").relation("j").to_arrow_table()
        con.register("oracle", oracle)
        ray_uri = _output_field(st, "uri", outcome="committed")
        ray_src = f"read_parquet('{ray_uri}/**/*.parquet', union_by_name=true)"
        # (a) SCHEMA byte-identity: the output columns (incl. the coalesced key + the `_2`-suffixed right
        # `amount`) must match the single-node engine exactly, in the same order.
        ray_cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {ray_src}").fetchall()]
        duck_cols = [c[0] for c in con.execute("DESCRIBE SELECT * FROM oracle").fetchall()]
        assert ray_cols == duck_cols == ["user_id", "amount", "name", "amount_2"], f"{how} schema: {ray_cols}"
        # (b) VALUES: full-row multiset over the (deterministic) unique left amount
        q = "SELECT user_id, amount, name, amount_2 FROM {src} ORDER BY amount, user_id"
        ray_rows = con.execute(q.format(src=ray_src)).fetchall()
        duck_rows = con.execute(q.format(src="oracle")).fetchall()
        con.unregister("oracle")
        assert ray_rows == duck_rows, f"{how} join Ray != DuckDB (n={len(ray_rows)} vs {len(duck_rows)})"
        assert len(ray_rows) == expect, f"{how}: expected {expect} rows, got {len(ray_rows)}"


def test_ray_sort_live_differential(tmp_path):
    # opt-in live Ray: a distributed SORT (Ray range-shuffle → repartition(1) → one ordered file) must,
    # when READ IN FILE ORDER (pyarrow, which preserves it), match single-node DuckDB's ordered sequence
    # exactly — including NULL placement. Key = (k, v): k has ties + NULLs, v is unique → a total order.
    if not os.environ.get("DP_TEST_RAY_LIVE"):
        pytest.skip("set DP_TEST_RAY_LIVE=1 to run the live-Ray sort differential")
    import glob as _glob
    import time

    import duckdb
    import pyarrow.parquet as _pq

    from hub import db
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    p = str(tmp_path / "s.parquet")
    duckdb.connect().execute(
        f"COPY (SELECT (CASE WHEN i % 50 = 0 THEN NULL ELSE i % 100 END) AS k, i AS v "
        f"FROM range(0, 4000) t(i)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)

    def run_sort(by, out):
        g = Graph(**{"id": "c", "version": 1, "nodes": [
            _ray_node("src", "source", {"uri": p}), _ray_node("s", "sort", {"by": by}),
        ], "edges": [_ray_edge("src", "s")]})
        st = rr.run_unit(g, "s", out)
        for _ in range(900):
            if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.1)
        st = rr.status(st.run_id)
        assert st.status == "done", f"{by}: {st.error}"
        ray_uri = _output_field(st, "uri", outcome="committed")
        files = sorted(_glob.glob(os.path.join(ray_uri, "**", "*.parquet"), recursive=True))
        ray_rows = _pq.read_table(files).to_pylist()      # physical file order (pyarrow preserves it)
        with db.run_scope():
            oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                                 node_specs=deps.node_specs, output_node="s").relation("s").to_arrow_table()
        return ray_rows, oracle.to_pylist()

    # (1) TOTAL key, ascending: (k, v) with v unique → exact sequence == DuckDB, incl. NULLS LAST placement
    ray_rows, duck_rows = run_sort("k, v", str(tmp_path / "asc.parquet"))
    assert [(r["k"], r["v"]) for r in ray_rows] == [(r["k"], r["v"]) for r in duck_rows] and len(ray_rows) == 4000

    # (2) TOTAL key, DESCENDING (v unique): exact reversed sequence — confirms DESC + NULLS placement match
    ray_rows, duck_rows = run_sort("v DESC", str(tmp_path / "desc.parquet"))
    assert [r["v"] for r in ray_rows] == [r["v"] for r in duck_rows] == list(range(3999, -1, -1))

    # (3) NON-total key (k alone, ties + NULLs): the honest contract — CORRECTLY sorted (k monotonic
    # non-decreasing, NULLS last) + the SAME multiset as DuckDB, but tie-order is NOT asserted equal
    # (unstable in both engines — the "byte-identical" promise holds only for a total key).
    ray_rows, duck_rows = run_sort("k", str(tmp_path / "ties.parquet"))
    ks = [r["k"] for r in ray_rows]
    non_null = [x for x in ks if x is not None]
    assert non_null == sorted(non_null), "non-NULL keys not ascending in file order"
    assert ks[len(non_null):] == [None] * (len(ks) - len(non_null)), "NULLs not placed LAST"
    assert sorted((r["k"] is None, r["k"] or 0, r["v"]) for r in ray_rows) == \
           sorted((r["k"] is None, r["k"] or 0, r["v"]) for r in duck_rows)  # same multiset of rows


def test_ray_region_requires_fail_before_dispatch(tmp_path, monkeypatch):
    # An explicit Ray region whose declared resources are not advertised fails before driver dispatch;
    # it never becomes a permanently pending Ray task that looks like a hung run.
    from hub.deps import Deps
    from hub.models import Graph, ResourceSpec
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    monkeypatch.setattr(rr, "_source_unsupported_reason", lambda *_args: None)
    g = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": "unused.parquet"}),
        _ray_node("m", "transform", {"mode": "map", "code": "def fn(row):\n    row['x'] = row['x'] + 1\n    return row"}),
    ], "edges": [_ray_edge("src", "m")]})
    # A resource the configured Ray capacity does not advertise is a terminal placement error.
    req = ResourceSpec(labels={"engine": "ray", "need": "gpu_pool_that_does_not_exist"})
    st = rr.run_unit(g, "m", str(tmp_path / "b.parquet"), requires=req)
    assert st.status == "failed"
    assert "advertised Ray capacity" in (st.error or "")


def test_ray_backend_placement_and_tiers(tmp_path):
    # D: dp_ray is a PlaceableBackend (region dispatch). place() claims a region ONLY when it explicitly
    # asks for engine=ray — the cost-based mem policy must NOT silently route here. No live Ray needed.
    from hub.deps import Deps
    from hub.models import ResourceSpec
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    assert rr.reachable_tiers() == ("local", "object")
    assert [w.id for w in rr.workers()] == ["ray"]
    assert rr.place(ResourceSpec(labels={"engine": "ray"})) == "ray"   # explicit opt-in → claimed
    assert rr.place(ResourceSpec(mem="1000GB")) is None                # a cost-based mem need → NOT claimed
    assert rr.place(None) is None
    assert hasattr(rr, "run_unit")                                     # PlaceableBackend region entry present


def test_ray_cancel_is_acknowledged_only_after_driver_reap(tmp_path):
    import threading as _threading

    from hub.backends import stop_acknowledged
    from hub.deps import Deps
    from hub.models import RunStatus

    (tmp_path / "ws").mkdir()
    rr = _load_dp_ray().RayRunner(Deps(str(tmp_path / "ws"), str(tmp_path / "data")))
    run_id = "unit_cancel_ack"
    rr.runs[run_id] = RunStatus(run_id=run_id, status="cancelled", placement="distributed", per_node=[])
    rr._published_statuses[run_id] = rr.runs[run_id].model_copy(deep=True)
    assert not rr.cancel_acknowledged(run_id)
    assert not stop_acknowledged(rr, rr.status(run_id))

    timer = _threading.Timer(0.02, rr._acknowledge_cancel, args=(run_id,))
    timer.start()
    try:
        settled = get_deps().controller._await(rr, run_id)
    finally:
        timer.cancel()
    assert settled.status == "cancelled" and rr.cancel_acknowledged(run_id)


def test_ray_backend_run_unit_falls_back_locally_for_a_nonclean_region(tmp_path):
    # D fix: run_unit on a region the backend can't distribute must NOT call the missing
    # LocalRunner.run_unit — it materializes with the local DuckDB engine (correct, non-distributed). No
    # Ray needed. Also guards _materialize_local creating a missing parent dir for the output uri. Uses an
    # EXPRESSION group key (x*2): the aggregate op is claimed but the key isn't plain columns, so there's
    # no shuffle key → _ray_runnable False → local fallback. (A bare-column group key WOULD distribute.)
    import duckdb

    from hub.deps import Deps
    from hub.models import Graph
    from hub.run_outputs import sole_output
    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES (1),(1),(2)) t(x)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    gr = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("s", "source", {"uri": p}),
        _ray_node("a", "aggregate", {"groupBy": "x*2", "aggs": "count(*) AS n"}),
    ], "edges": [_ray_edge("s", "a")]})
    out = str(tmp_path / "sub" / "unit.parquet")
    st = rr.run_unit(gr, "a", out, run_id="unit_local_fallback")
    assert st.status == "done", st.error
    assert st.placement == "local"  # a non-clean region fell back to the local engine, not Ray
    output = sole_output(st, committed=True)
    assert output is not None
    assert output.uri == _load_dp_ray()._attempt_handoff_uri(out, "unit_local_fallback")
    assert not os.path.exists(out), "the fallback wrote the stable controller suggestion"
    assert os.path.isfile(os.path.join(output.uri, "_DP_SUCCESS.json"))
    assert duckdb.connect().execute(
        f"SELECT count(*) FROM read_parquet('{output.uri}/**/*.parquet')").fetchone()[0] == 2

    # Reattaching the same explicit attempt is read-only and returns the committed physical prefix.
    manifest_mtime = os.path.getmtime(os.path.join(output.uri, "_DP_SUCCESS.json"))
    again = rr.run_unit(gr, "a", out, run_id="unit_local_fallback")
    again_output = sole_output(again, committed=True)
    assert again.status == "done" and again_output is not None
    assert again_output.uri == output.uri
    assert os.path.getmtime(os.path.join(output.uri, "_DP_SUCCESS.json")) == manifest_mtime


@pytest.mark.skipif(not os.environ.get("DP_TEST_RAY_LIVE"), reason="live Ray run — opt-in (needs [ray] + a real executor). Enable: DP_TEST_RAY_LIVE=1.")
def test_ray_backend_run_unit_live(tmp_path):
    # D end-to-end: run_unit executes a region on Ray (reads Parquet via ray.data.read_parquet) and
    # materializes output_node to an immutable worker-direct parts prefix.
    import time

    import duckdb

    from hub.deps import Deps
    from hub.models import Graph
    from hub.run_outputs import sole_output

    pytest.importorskip("ray")
    p = str(tmp_path / "nums.parquet")
    duckdb.connect().execute(f"COPY (SELECT * FROM range(1,11) t(x)) TO '{p}' (FORMAT PARQUET)")
    (tmp_path / "ws").mkdir()
    deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    rr = _load_dp_ray().RayRunner(deps)
    # a clean region: source → map(x*2); materialize the map node's output to a uri (no write node)
    gr = Graph(**{"id": "c", "version": 1, "nodes": [
        _ray_node("src", "source", {"uri": p}),
        _ray_node("m", "transform", {"mode": "map", "code": "def fn(row):\n    row['x'] = row['x'] * 2\n    return row"}),
    ], "edges": [_ray_edge("src", "m")]})
    out = str(tmp_path / "unit_out.parquet")
    st = rr.run_unit(gr, "m", out)
    for _ in range(900):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    st = rr.status(st.run_id)
    assert st.status == "done", st.error
    output = sole_output(st, committed=True)
    assert output is not None and output.uri != out and ".attempt-" in output.uri
    assert os.path.isfile(os.path.join(output.uri, "_DP_SUCCESS.json"))
    got = sorted(r[0] for r in duckdb.connect().execute(
        f"SELECT x FROM read_parquet('{output.uri}/**/*.parquet')").fetchall())
    assert got == [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]


def test_section_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", "emit(inputs['in'])"),
    ], "edges": [E("src", "sec")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "sec", "k": 5}).json()
    assert r["notPreviewable"] is True


def test_section_maxruns_is_bounded():
    # an unbounded-looking loop must fail closed at maxRuns, not run away
    script = "while True:\n    run(f, data=inputs['in'], predicate='amount > 0')\n"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", script, max_runs=3),
        _section_child("sec-filter", "sec", "f", "filter"),
        N("wr", "write", {"name": "sec_runaway"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "failed" and "maxRuns" in (st.get("error") or "")


def test_inline_section_subnodes_are_not_executed():
    # An old inline body remains an ordinary unknown config field; it must not be silently revived as
    # executable nodes. Current section ownership is exclusively the canvas's parentId containment.
    graph = {"id": "old-inline-section", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("sec", "section", {
            "script": "emit(run('old-filter', data=inputs['in']))",
            "subnodes": [{"alias": "old-filter", "type": "filter", "config": {}}],
        }),
        N("wr", "write", {"name": "must_not_publish"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    status = _poll(client.post("/api/run", json={
        "graph": graph, "targetNodeId": "wr", "confirmed": True,
    }).json()["runId"])
    assert status["status"] == "failed"
    assert "section calls unknown node 'old-filter'" in (status.get("error") or "")
