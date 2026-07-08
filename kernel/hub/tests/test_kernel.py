"""End-to-end kernel tests — the real out-of-core build engine on real files."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app

client = TestClient(app)


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
    assert specs["filter"]["params"][0]["name"] == "predicate"


def test_catalog_and_capabilities():
    tabs = {t["name"]: t for t in client.get("/api/catalog/tables").json()}
    assert {"images", "movies", "events"} <= set(tabs)
    caps = {c["name"]: c["capabilities"] for c in tabs["images"]["columns"]}
    assert "media" in caps["image_url"] and "vector" in caps["embedding"]
    assert tabs["images"]["rowCount"] == 500


def test_sample():
    r = client.post("/api/data/sample", json={"uri": _uri("images"), "k": 5}).json()
    assert len(r["rows"]) == 5 and r["rowCount"] == 500 and r["truncated"] is True


def test_preview_pipeline_derives_and_sorts():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
        N("srt", "sort", {"by": "area DESC"}),
    ], "edges": [E("src", "flt"), E("flt", "sel"), E("sel", "srt")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "srt", "k": 10}).json()
    assert not r["notPreviewable"]
    assert "area" in [c["name"] for c in r["columns"]]
    areas = [row["area"] for row in r["rows"]]
    assert areas == sorted(areas, reverse=True)


def test_profile_returns_column_stats():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
    ], "edges": [E("src", "sel")]}
    r = client.post("/api/run/profile", json={"graph": g, "nodeId": "sel"}).json()
    assert not r["error"] and not r["notPreviewable"]
    assert r["sampled"] is True and r["rowCount"] > 0
    cols = {c["name"]: c for c in r["columns"]}
    assert "area" in cols
    area = cols["area"]
    assert area["nonNull"] + area["nulls"] == r["rowCount"]  # every row is null or not
    assert area["distinct"] is not None
    assert area["mean"] is not None                          # numeric → has a mean
    assert area["min"] is not None and area["max"] is not None


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
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("events")}),
        N("j", "join", {"on": "user_id", "how": "inner"}),
        N("q", "sql", {"sql": "SELECT count(*) AS n FROM input"}),
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b"), E("j", "q")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 5}).json()
    assert not r["notPreviewable"] and r["rows"][0]["n"] > 0


def test_preview_is_faithful_for_join_and_sort(tmp_path):
    # preview used to truncate each source to its first 2000 rows and THEN join/sort — so a join of
    # two non-overlapping prefixes showed 0 matches, and a sort showed the top of an arbitrary prefix.
    # It now runs these over the full inputs (bounded by the preview limit), so the sample is faithful.
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
    assert not rj["notPreviewable"] and rj["rowCount"] > 0  # real matches (2500..2999), not the old 0
    gs = {"id": "c2", "version": 1, "nodes": [
        N("l", "source", {"uri": left}), N("s", "sort", {"by": "lval DESC"}),
    ], "edges": [E("l", "s")]}
    rs = client.post("/api/run/preview", json={"graph": gs, "nodeId": "s", "k": 5}).json()
    assert rs["rows"][0]["lval"] == 29990  # the TRUE global max (id=2999), not a 2000-row-prefix max


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
    assert not r["notPreviewable"], r.get("reason")
    cols = [c["name"] for c in r["columns"]]
    assert cols == ["id", "name", "uid", "name_2"]  # right-side 'name' renamed → no ambiguity
    assert r["rowCount"] == 3  # ids 2,3,4 overlap


def test_dedup_and_metric():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("sel", "select", {"select": "event"}),
        N("dd", "dedup", {}),
    ], "edges": [E("src", "sel"), E("sel", "dd")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "dd", "k": 50}).json()
    assert len(r["rows"]) == 4  # view/click/purchase/signup


def test_run_write_and_lineage():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("wr", "write", {"name": "images_valid", "format": "parquet"}),
    ], "edges": [E("src", "flt"), E("flt", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    st = _poll(r["runId"])
    assert st["status"] == "done" and st["outputTable"] == "images_valid"
    assert st["totalRows"] and st["totalRows"] < 500  # filtered out invalids
    lin = client.get("/api/catalog/lineage", params={"uri": _uri("images")}).json()
    assert any(e["child"].endswith("images_valid.parquet") for e in lin["edges"])


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
    assert "cities" in {x["name"] for x in client.get("/api/catalog/tables").json()}
    s = client.post("/api/data/sample", json={"uri": t["uri"], "k": 10}).json()
    assert len(s["rows"]) == 3


def test_upload_rejects_unsupported_type():
    assert _upload("notes.txt", b"hello").status_code == 400


def test_upload_rejects_oversized(monkeypatch):
    from hub.settings import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 8)  # tiny cap → the 20-byte body is aborted mid-stream
    assert _upload("big.csv", b"a\n" + b"1\n" * 9).status_code == 413


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


def test_metric_preview_is_true_value(tmp_path):
    import duckdb
    p = str(tmp_path / "big5k.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 AS v FROM range(0,5000)) TO '{p}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [E("src", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert not r["notPreviewable"] and not r.get("error")
    assert r["rows"][0]["value"] == 5000.0  # TRUE count over 5000 rows, not the 2000 preview sample


def test_join_duplicate_columns_preserved():
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("images")}),
        N("j", "join", {"how": "inner"}),  # no key → cross join; both have 'id'
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 3}).json()
    names = [c["name"] for c in r["columns"]]
    assert "id" in names and "id_2" in names           # both id columns kept, de-duped
    assert all(len(row) == len(names) for row in r["rows"])  # no column dropped


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
    assert all(isinstance(a, (int, float)) for a in amounts)  # not strings


def test_plugin_run_applies_lowering(tmp_path):
    # the critical bug: plugin lowerings were dropped on a full run → untransformed writes
    from hub.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="const42", title="const42", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[])
    deps.node_specs[spec.kind] = spec
    deps.node_builders[spec.kind] = lambda engine, node, inputs: ctx.sql(inputs[0], "SELECT *, 42 AS c FROM {input}")
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


def test_settings_redacts_secrets():
    # GET /settings must not disclose the LLM key or object-store secrets in plaintext; a PUT that
    # echoes the redaction sentinel preserves the stored secret (doesn't overwrite it with dots).
    client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": "sk-super-secret"})
    client.put("/api/settings", json={"scope": "global", "key": "objectStore",
                                      "value": {"accessKeyId": "AKIA", "secretAccessKey": "shh", "region": "us-east-1"}})
    g = client.get("/api/settings").json()["global"]
    assert g["agentApiKey"] == "__redacted__"                    # key not disclosed
    assert g["objectStore"]["secretAccessKey"] == "__redacted__" and g["objectStore"]["accessKeyId"] == "__redacted__"
    assert g["objectStore"]["region"] == "us-east-1"             # non-secret still visible
    # saving back the redacted view keeps the real secrets (a no-op edit doesn't wipe them)
    client.put("/api/settings", json={"scope": "global", "key": "objectStore",
                                      "value": {"accessKeyId": "__redacted__", "secretAccessKey": "__redacted__", "region": "eu-west-1"}})
    from hub import metadb
    stored = metadb.get_setting("objectStore", "global")
    assert stored["secretAccessKey"] == "shh" and stored["accessKeyId"] == "AKIA" and stored["region"] == "eu-west-1"
    client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": ""})  # restore


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
    n, out = st1["totalRows"], st1["outputUri"]
    assert n and out and not out.endswith(".parquet")  # a directory, not a single file
    append_run()  # a second part
    pv = client.post("/api/run/preview", json={"graph": {"id": "c", "version": 1,
        "nodes": [N("s", "source", {"uri": out})], "edges": []}, "nodeId": "s", "k": 100000}).json()
    assert pv["rowCount"] >= 2 * n and pv["rowCount"] % n == 0  # each append added a full part, read back together


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
        for ext in (".pq", ".tsv", ".json"):
            res = a.write(str(tmp_path / f"app{ext}"), con.sql("SELECT 3 AS a, 'z' AS b"), "append")
            assert a.scan(res["uri"]).fetchall() == [(3, "z")], ext  # part-*.<ext> read back from the dir


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
    assert r.estimate(plan, 6_000_000).needs_confirm is True                                        # big known → gate
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
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM _')
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


def test_users_create_and_list():
    before = {u["id"] for u in client.get("/api/users").json()}
    created = client.post("/api/users", json={"name": "Alice", "email": "a@x.io"}).json()
    assert created["name"] == "Alice"
    ids = {u["id"] for u in client.get("/api/users").json()}
    assert created["id"] in ids and "local" in ids and created["id"] not in before


def test_per_user_password_is_not_a_skeleton_key(monkeypatch):
    # with auth on, a password authenticates ONLY its own user — no shared/skeleton password.
    from hub import auth, metadb
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


def test_api_routes_require_auth_when_enabled(monkeypatch):
    # SECURE DEFAULT: with auth enabled, the whole /api surface needs a session — the high-impact routes
    # (/run code-exec, /data file-read, POST /users self-registration) used to be wide open. Only the
    # login roster + auth status/login stay public.
    from hub import auth, metadb
    from hub.metadb import User, session
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
    from hub.settings import settings
    inside = _os.path.join(settings.data_dir, "some_dataset.parquet")
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    paths.ensure_local_uri_allowed("/etc/passwd")           # open mode → no confinement
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    paths.ensure_local_uri_allowed(inside)                  # inside a root → allowed
    paths.ensure_local_uri_allowed("s3://bucket/x.parquet")  # object-store → not a local path → allowed
    with pytest.raises(PermissionError):
        paths.ensure_local_uri_allowed("/etc/passwd")       # outside every root → rejected


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


def test_agent_activates_from_settings_key(monkeypatch):
    # setting a provider key via /settings (the UI path) must make the agent available — no env var
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/agent").json()["available"] is False
    client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": "sk-from-ui"})
    try:
        assert client.get("/api/agent").json()["available"] is True
    finally:
        client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": ""})


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
        p = _seq_parquet(tmp_path, n=40)
        g = {"id": "c", "version": 1, "nodes": [
            N("src", "source", {"uri": p}),
            N("wr", "write", {"name": "subproc_out", "writeMode": "overwrite"}),
        ], "edges": [E("src", "wr")]}
        r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
        st = _poll(r["runId"], tries=400)
        assert st["status"] == "done", st.get("error")
        assert st["outputTable"] == "subproc_out"
        assert (st["totalRows"] or st["rowsProcessed"]) == 40
        # the child wrote in its own (discarded) catalog — the parent must still register it live
        tables = client.get("/api/catalog/tables").json()
        assert any(t["name"] == "subproc_out" for t in tables)
    finally:
        metadb.set_setting("backend", "", "global")  # restore the default in-process runner


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
        p = _seq_parquet(tmp_path, n=30)
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


def test_vector_search_lance_ann_and_external_query(tmp_path):
    # vector-search uses Lance's native nearest (its index if present) when the input is a bare Lance
    # source, and can query by an arbitrary external vector — not only an existing row.
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
    assert not r["notPreviewable"], r.get("reason")
    assert "_score" in [c["name"] for c in r["columns"]] and r["rows"][0]["id"] == 0
    # an external query vector [0,1,0,0] → row 2 is nearest (no such row was the query)
    g2 = {"id": "cv2", "version": 1, "nodes": [N("s", "source", {"uri": p}),
          N("vs", "vector-search", {"column": "embedding", "queryVector": "[0,1,0,0]", "k": 2})], "edges": [E("s", "vs")]}
    r2 = client.post("/api/run/preview", json={"graph": g2, "nodeId": "vs", "k": 10}).json()
    assert r2["rows"][0]["id"] == 2


def test_object_store_s3_roundtrip_and_browse(tmp_path):
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
        metadb.set_setting("objectStore", {"endpoint": endpoint, "region": "us-east-1",
                                           "accessKeyId": "k", "secretAccessKey": "s", "useSsl": False}, "global")
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
        metadb.set_setting("objectStore", {}, "global")  # restore the default credential chain for other tests


def test_upload_lands_bytes_in_object_store(tmp_path):
    # Object-store deployments (multi-instance): uploaded bytes must round-trip through DuckDB httpfs to
    # s3:// so every web instance can read them. _land_upload re-encodes to the SAME format at the target
    # uri (csv stays csv); arrow/feather — which have no object-store reader — normalize to parquet.
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db, metadb
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
        metadb.set_setting("objectStore", {"endpoint": endpoint, "region": "us-east-1",
                                           "accessKeyId": "k", "secretAccessKey": "s", "useSsl": False}, "global")
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
        metadb.set_setting("objectStore", {}, "global")


def _section(nid, script, subnodes, params=None, max_runs=200):
    return N(nid, "section", {"script": script, "subnodes": subnodes,
                              "params": params or {}, "maxRuns": max_runs})


def _seq_parquet(tmp_path, n=1000):
    import duckdb
    p = str(tmp_path / "seq.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT i AS v FROM range(0,{n}) t(i)) TO '{p}' (FORMAT PARQUET)")
    return p


def test_section_for_each_over_a_list(tmp_path):
    # for-each: run a filter per predicate in a list, concat the results (graph isn't fixed — a `for`)
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("parts = []\n"
              "for pred in params['preds']:\n"
              "    parts.append(run(f, data=inputs['in'], predicate=pred))\n"
              "emit(concat(parts))\n")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}],
                 {"preds": ["v >= 0 AND v < 100", "v >= 900"]}),  # 100 + 100 rows, disjoint
        N("wr", "write", {"name": "sec_foreach"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done" and st["outputTable"] == "sec_foreach"
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
        _section("sec", script, [
            {"alias": "shrink", "type": "filter", "config": {}},
            {"alias": "cnt", "type": "metric", "config": {"agg": "count"}},
        ], {"max_iters": 10, "target": 300}),
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
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}]),
        N("wl", "write", {"name": "sec_low"}),
        N("wh", "write", {"name": "sec_high"}),
    ], "edges": [E("src", "sec"), E("sec", "wl", sh="low"), E("sec", "wh", sh="high")]}
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


def test_run_history_persisted_with_canvas(tmp_path):
    # a finished run is recorded under its canvas (survives restart) + exposed at /canvas/{id}/runs
    from hub import metadb
    p = _seq_parquet(tmp_path)
    client.put("/api/canvas/hist_canvas", json={"id": "hist_canvas", "name": "h", "version": 1, "nodes": [], "edges": []})  # persist the canvas
    g = {"id": "hist_canvas", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), N("wr", "write", {"name": "hist_out"}),
    ], "edges": [E("src", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    runs = []
    for _ in range(40):  # on_complete persists in the run's finally, a beat after status flips to done
        runs = metadb.list_runs("hist_canvas")
        if runs:
            break
        time.sleep(0.05)
    assert runs and runs[0]["status"] == "done" and runs[0]["outputTable"] == "hist_out"
    assert any(r["status"] == "done" for r in client.get("/api/canvas/hist_canvas/runs").json())


def test_collab_relay_broadcasts_and_leave():
    # the collab room relays a peer's message to others and tells them when a peer leaves
    with client.websocket_connect("/ws/collab/room1") as b:
        with client.websocket_connect("/ws/collab/room1") as a:
            a.send_json({"clientId": "A", "type": "presence", "name": "Ann"})
            got = b.receive_json()
            assert got["clientId"] == "A" and got["type"] == "presence"
        leave = b.receive_json()  # a disconnected → b is told to drop A
        assert leave == {"type": "leave", "clientId": "A"}


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
        try:
            r.runs.clear()
            r.runs["run_live"] = RunStatus(run_id="run_live", status="running")  # oldest + still running
            for i in range(_MAX_RUNS + 5):
                r.runs[f"run_done_{i}"] = RunStatus(run_id=f"run_done_{i}", status="done")
            r._evict()
            assert "run_live" in r.runs          # the in-flight run survived
            assert len(r.runs) == _MAX_RUNS      # only terminal runs were dropped, down to the cap
        finally:
            r.runs.clear()
            r.runs.update(saved)


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
    from hub.backends import CatalogProvider, DatasetAdapter
    from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
    assert isinstance(DuckDBAdapter(), DatasetAdapter)
    assert isinstance(LanceAdapter(), DatasetAdapter)
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
    # table — the read-external catalog pattern: overrides list_tables/get_table, inherits resolve_ref +
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
    assert "sales" in {t.name for t in cat.list_tables(None)}
    t = cat.get_table("sales")
    assert t.uri == p and {c.name for c in t.columns} == {"id", "amt"} and t.row_count == 5
    assert cat.resolve_ref("sales") == p          # inherited: bare name → uri
    with _pt.raises(KeyError):                     # inherited contract: miss raises KeyError
        cat.get_table("nonexistent")


def test_plugin_secret_not_leaked_via_settings():
    # a plugin's secret [[config]] field must NOT be readable in plaintext via GET /api/settings (only
    # admins can PUT global settings, but any authenticated user can GET them) — the same "secrets never
    # echo" contract /api/plugins upholds. And PUT with the redaction sentinel must keep the stored secret.
    import json as _json

    from hub import metadb
    from hub.deps import get_deps

    deps = get_deps()
    fake = {"name": "dp_secretpk", "source": "drop-in",
            "config": [{"key": "token", "type": "password", "secret": True},
                       {"key": "host", "type": "string"}]}
    deps.plugins.append(fake)
    try:
        client.put("/api/settings", json={"scope": "global", "key": "plugin.dp_secretpk.token", "value": "sk-LEAK-xyz"})
        client.put("/api/settings", json={"scope": "global", "key": "plugin.dp_secretpk.host", "value": "db.internal"})
        r = client.get("/api/settings").json()
        assert r["global"]["plugin.dp_secretpk.token"] == "__redacted__"   # secret redacted
        assert "sk-LEAK-xyz" not in _json.dumps(r)                          # value never in the payload
        assert r["global"]["plugin.dp_secretpk.host"] == "db.internal"      # non-secret still visible
        # PUT of the redaction sentinel keeps the stored secret (doesn't overwrite with dots)
        client.put("/api/settings", json={"scope": "global", "key": "plugin.dp_secretpk.token", "value": "__redacted__"})
        assert metadb.get_setting("plugin.dp_secretpk.token", "global") == "sk-LEAK-xyz"
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
    from hub.backends import DatasetAdapter

    fake = ds_mod.Dataset.from_dict({"id": [1, 2, 3], "txt": ["a", "b", "c"]})
    monkeypatch.setattr(ds_mod, "load_dataset", lambda name, config=None, split=None: fake)
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_hf_datasets" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_hf_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    a = mod.HfDatasetsAdapter()
    assert isinstance(a, DatasetAdapter)
    assert a.matches("hf://x:train") and not a.matches("/tmp/x.parquet")
    assert mod._parse("hf://glue@mrpc:validation") == ("glue", "mrpc", "validation")
    assert mod._parse("hf://stanfordnlp/imdb") == ("stanfordnlp/imdb", None, "train")  # id keeps its '/'
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
    from hub.backends import DatasetAdapter

    tbl = pa.table({"id": [1, 2], "v": ["a", "b"]})
    scan = type("S", (), {"to_arrow": lambda self: tbl})()
    table = type("T", (), {"scan": lambda self, **kw: scan})()
    monkeypatch.setattr(pc, "load_catalog", lambda name=None, **kw: type("C", (), {"load_table": lambda self, i: table})())

    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_iceberg" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_iceberg_ref", src)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    a = mod.IcebergAdapter()
    assert isinstance(a, DatasetAdapter)
    assert a.matches("iceberg://prod/sales.orders") and not a.matches("/tmp/x.parquet")
    assert mod._parse("iceberg://prod/sales.orders") == ("prod", "sales.orders")  # first '/' splits catalog
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
        def run_unit(self, graph, output_node, output_uri): return None

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
    canvas_id = "cv_kernel_e2e"
    os.environ["DP_KERNEL_IDLE_TTL"] = "6"          # backstop: the detached kernel self-exits soon if idle
    old = sm.settings.execution
    sm.settings.execution = "kernel"
    dm.set_workspace(sm.settings.workspace, sm.settings.data_dir)   # rebuild deps → registers KernelBackend
    try:
        assert any(getattr(r, "name", "") == "kernel" for r in dm.get_deps().runners)
        g = {"id": canvas_id, "version": 1, "nodes": [
            N("src", "source", {"uri": _uri("events")}),
            N("flt", "filter", {"predicate": "amount > 1"}),
        ], "edges": [E("src", "flt")]}
        r = client.post("/api/run", json={"graph": g, "targetNodeId": "flt", "confirmed": True}).json()
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
    assert "shared_out_x" in [t.name for t in other.list_tables(None)]
    assert other.get_table("shared_out_x").uri == uri


def test_pipelines_import_reports_not_configured():
    # with no importer plugin, the endpoint must HONESTLY report 501 not-configured — it used to 400
    # with an AttributeError because Deps had no .importer attr (dead scaffolding). Now deps.importer
    # defaults to NullImporter → ImporterNotConfigured → 501.
    r = client.post("/api/pipelines/import", json={"config": "x", "params": {}})
    assert r.status_code == 501
    assert "importer" in r.text.lower()


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


def test_collab_relay_gates_viewer_doc_updates(monkeypatch):
    # a viewer may watch (presence + peers' edits) but its OWN doc updates ('yjs' carries CRDT state)
    # must NOT be relayed — else an editor peer would merge + autosave them, laundering a change past
    # the read-only boundary that put_canvas enforces.
    from hub import auth, metadb
    from hub.metadb import Canvas, session
    monkeypatch.setenv("DP_AUTH_SECRET", "s3cr3t")
    cid = "cvs_viewer_gate"
    with session() as s:
        if s.get(Canvas, cid) is None:
            s.add(Canvas(id=cid, owner_id="owner_u", name="t", version=1, doc="{}", visibility="private"))
    metadb.share_canvas(cid, "editor_u", "editor")
    metadb.share_canvas(cid, "viewer_u", "viewer")
    ed_cookie = {"cookie": f"dp_session={auth.sign('editor_u')}"}
    vw_cookie = {"cookie": f"dp_session={auth.sign('viewer_u')}"}
    client.cookies.clear()
    try:
        with client.websocket_connect(f"/ws/collab/{cid}", headers=ed_cookie) as ed:
            with client.websocket_connect(f"/ws/collab/{cid}", headers=vw_cookie) as vw:
                # the viewer sends a doc update then a presence; the editor must receive ONLY the presence
                # (if the yjs had been relayed it would arrive first)
                vw.send_json({"clientId": "V", "type": "yjs", "update": "AAAA"})
                vw.send_json({"clientId": "V", "type": "presence", "name": "Val"})
                got = ed.receive_json()
                assert got["type"] == "presence" and got["clientId"] == "V"  # yjs dropped, presence relayed
                # an editor's doc update DOES reach the viewer (a writer's edits flow to watchers)
                ed.send_json({"clientId": "E", "type": "yjs", "update": "BBBB"})
                got2 = vw.receive_json()
                assert got2["type"] == "yjs" and got2["clientId"] == "E"
    finally:
        client.cookies.clear()


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
    assert "myout" in [t.name for t in d2.catalog.list_tables(None)]


def test_section_runs_its_parentid_children(tmp_path):
    # visual containment: a canvas node whose parentId is the section is a callable child — its
    # alias is its title, so the driver calls run("keep", …). No form-declared subnodes needed.
    p = _seq_parquet(tmp_path)  # v = 0..999
    child = {"id": "child1", "type": "filter", "parentId": "sec", "position": {"x": 0, "y": 0},
             "data": {"title": "keep", "config": {"predicate": "v < 300"}}}
    sec = {"id": "sec", "type": "section", "position": {"x": 0, "y": 0},
           "data": {"title": "sec", "config": {"script": "emit(run('keep', data=inputs['in']))\n",
                                               "subnodes": [], "params": {}, "maxRuns": 50}}}
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
                                                   "subnodes": [], "params": {}, "maxRuns": 50}}}
    outer = {"id": "outer", "type": "section", "position": {"x": 0, "y": 0},
             "data": {"title": "outer", "config": {"script": "emit(run('inner', data=inputs['in']))\n",
                                                   "subnodes": [], "params": {}, "maxRuns": 50}}}
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), outer, inner, keep, N("wr", "write", {"name": "sec_nested"}),
    ], "edges": [E("src", "outer"), E("outer", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_nested").uri, "k": 5}).json()
    assert out["rowCount"] == 300  # outer → inner → keep(v<300), nested two levels deep


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
             "data": {"config": {"script": "emit(run('keep', data=inputs['in']))\n", "subnodes": [], "maxRuns": 10}}},
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


def test_completed_run_result_is_db_cached(tmp_path):
    # A2: a finished run persists its result pointer to the shared DB (result_cache), so it's reused
    # across a kernel restart / another stateless instance — not just the accepting process's dict.
    from hub import metadb
    from hub.models import Graph
    p = _seq_parquet(tmp_path)
    gd = {"id": "c", "version": 1, "nodes": [N("src", "source", {"uri": p}), N("wr", "write", {"name": "a2cache"})],
          "edges": [E("src", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": gd, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    phash = get_deps().runner._plan_hash(Graph(**gd), "wr")
    c = metadb.get_result(phash)
    assert c and c.get("uri") and c.get("table")                 # persisted to the shared DB
    assert get_deps().runner._cache_get(phash)["table"] == c["table"]  # a fresh instance reads the same pointer


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
    p = _seq_parquet(tmp_path)  # v = 0..999
    gd = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        {"id": "f1", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "v < 500", "checkpoint": True}}},
        N("f2", "filter", {"predicate": "v >= 100"}),
        N("wr", "write", {"name": "ckpt_out"}),
    ], "edges": [E("src", "f1"), E("f1", "f2"), E("f2", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": gd, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done", st
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_ckpt_out").uri, "k": 5}).json()
    assert out["rowCount"] == 400  # v in [100, 500) → 400 rows, split across two regions


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
    d = get_deps()
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


def test_relationship_crud_and_leads_join_analysis():
    # declared relationships persist (Settings, cross-instance) and TRUMP measurement in join analysis.
    d = get_deps()
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

    from hub import db
    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)          # auth on
    monkeypatch.setenv("DP_DATASET_ROOTS", str(tmp_path))   # allowed root
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
    # raw x,y points (scatter). Runs out-of-core server-side; the panel renders the SVG.
    ev = _uri("events")

    def chart(cfg):
        g = {"id": "c", "version": 1, "nodes": [
            N("s", "source", {"uri": ev}),
            {"id": "ch", "type": "chart", "position": {"x": 0, "y": 0}, "data": {"config": cfg}}],
            "edges": [E("s", "ch")]}
        return client.post("/api/run/preview", json={"graph": g, "nodeId": "ch", "k": 50}).json()
    bar = chart({"chartType": "bar", "x": "event", "agg": "count"})
    assert not bar.get("notPreviewable") and {c["name"] for c in bar["columns"]} == {"x", "y"}
    assert {r["x"] for r in bar["rows"]} == {"view", "click", "purchase", "signup"}  # one point per distinct event
    scatter = chart({"chartType": "scatter", "x": "user_id", "y": "amount", "agg": "none"})
    assert {c["name"] for c in scatter["columns"]} == {"x", "y"} and scatter["rows"]
    assert chart({"chartType": "bar", "agg": "count"}).get("notPreviewable")       # no X → honest refusal
    assert chart({"chartType": "bar", "x": "event", "agg": "sum"}).get("notPreviewable")  # sum needs a Y (not silent count)
    minmax = chart({"chartType": "bar", "x": "event", "y": "event", "agg": "max"})  # max of a TEXT column
    assert not minmax.get("error"), minmax  # TRY_CAST → NULL y (dropped), not a raw ConversionException


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

    # (3) a plugin ir hook that RAISES degrades to opaque — never bricks compile/estimate/run
    def boom(node):
        raise ValueError("bad plugin hook")
    node_ir = {"myplugin": boom}
    assert irmod._op_and_config(GraphNode(id="m", type="myplugin", data={"config": {}}), node_ir)[0] == "opaque:myplugin"
    g3 = G([src, {"id": "m", "type": "myplugin", "position": {"x": 0, "y": 0}, "data": {"config": {}}}],
           [{"id": "e", "source": "src", "target": "m", "data": {"wire": "dataset"}}])
    assert not irmod.lower_to_ir(g3, "m", d.node_specs, node_ir).is_clean()  # opaque → not clean, no raise


def test_resolve_config_is_the_shared_builtin_resolver():
    # hub.ir.resolve_config is the SINGLE resolver both the IR and the DuckDB engine (executors/engine.py
    # _lower) read built-in config through — canonicalizing keys so they can't diverge. Lock the contract.
    from hub.ir import resolve_config
    from hub.models import GraphNode

    def N(t, cfg, **data):
        return GraphNode(id="n", type=t, data={"config": cfg, **data})

    assert resolve_config(N("select", {"select": "a, b"})) == {"expr": "a, b"}            # select|expr → expr
    assert resolve_config(N("aggregate", {"group": "k", "aggs": "sum(x)"})) == {"groupBy": "k", "aggs": "sum(x)"}
    assert resolve_config(N("source", {"table": "t", "delimiter": ";", "header": "No"})) == \
        {"uri": "t", "options": {"delimiter": ";", "header": "no"}}                        # uri|table → uri; opts nested + normalized
    assert resolve_config(N("source", {"uri": "/p.parquet"})) == {"uri": "/p.parquet"}     # no options key when unset
    assert resolve_config(N("sample", {})) == {"n": None, "seed": 42}                       # n unset → engine supplies sample_k
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
    dirty = G([_ray_node("src", "source", {"uri": p}),
               _ray_node("a", "aggregate", {"aggs": "count(*) AS n"}),
               _ray_node("w", "write", {"name": "agg_out"})], [_ray_edge("src", "a"), _ray_edge("a", "w")])
    dplan = compile_plan(dirty, "w", deps.registry, deps.node_specs)
    assert rr.can_run(dplan) is False  # (3) relational → not clean

    # …and run() delegates a non-clean graph to the DuckDB base runner (never touches Ray) → it completes
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
        return sorted(r[0] for r in duckdb.connect().execute(f"SELECT x FROM read_parquet('{uri}')").fetchall())
    assert rows(st_ray.output_uri) == rows(st_loc.output_uri) == [10, 12, 14, 16, 18, 20]


def test_section_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", "emit(inputs['in'])", []),
    ], "edges": [E("src", "sec")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "sec", "k": 5}).json()
    assert r["notPreviewable"] is True


def test_section_maxruns_is_bounded():
    # an unbounded-looking loop must fail closed at maxRuns, not run away
    script = "while True:\n    run(f, data=inputs['in'], predicate='amount > 0')\n"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}], max_runs=3),
        N("wr", "write", {"name": "sec_runaway"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "failed" and "maxRuns" in (st.get("error") or "")
