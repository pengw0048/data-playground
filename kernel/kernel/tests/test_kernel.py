"""End-to-end kernel tests — the real out-of-core lowering engine on real files."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from kernel.deps import get_deps
from kernel.main import app

client = TestClient(app)


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def N(nid, t, cfg):
    return {"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"title": nid, "config": cfg}}


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
    assert info["runners"] == ["local-out-of-core", "local-subprocess"]
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
    from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="const42", title="const42", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[])
    deps.node_specs[spec.kind] = spec
    deps.node_lowerings[spec.kind] = lambda engine, node, inputs: ctx.sql(inputs[0], "SELECT *, 42 AS c FROM {input}")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("p", "const42", {}),
        N("wr", "write", {"name": "plugin_out"}),
    ], "edges": [E("src", "p"), E("p", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_plugin_out").uri, "k": 3}).json()
    assert "c" in [c["name"] for c in out["columns"]]           # plugin lowering was applied
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
    from kernel.deps import get_deps
    from kernel.executors.preview import preview_node
    deps = get_deps()

    def graph_for(uri, pred):
        return __import__("kernel.models", fromlist=["Graph"]).Graph(**{
            "id": "c", "version": 1,
            "nodes": [N("src", "source", {"uri": uri}), N("f", "filter", {"predicate": pred}),
                      N("d", "dedup", {})],
            "edges": [E("src", "f"), E("f", "d")],
        })

    imgs, evs = _uri("images"), _uri("events")

    def run(i):
        if i % 2 == 0:
            r = preview_node(graph_for(imgs, "is_valid = true"), "d", 20,
                             deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)
            return "images", all(row.get("is_valid") for row in r.rows), r.not_previewable
        r = preview_node(graph_for(evs, "amount > 1"), "d", 20,
                         deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)
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
    from kernel import metadb
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
    from kernel.deps import Deps
    from kernel import metadb
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
    from kernel import db
    from kernel.plugins.adapters import DuckDBAdapter
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
    from kernel import db
    from kernel.plugins.adapters import DuckDBAdapter
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
    from kernel import db
    from kernel.plugins.adapters import DuckDBAdapter
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
    from kernel import compiler
    from kernel.deps import get_deps
    from kernel.models import Graph
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
    from kernel import db
    from kernel.sandbox import SandboxError, run_with_timeout
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
    from kernel import db
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
    from kernel import db
    with db.run_scope() as s1:
        with pytest.raises(Exception):
            s1.con.execute("SELECT * FROM does_not_exist_xyz").fetchone()
    with db.run_scope() as s2:
        assert s2.con.execute("SELECT 99").fetchone() == (99,)


def test_run_scope_tracks_views_on_the_scope_not_globally():
    # temp views minted inside a scope are tracked on the scope (dropped on its own cursor at exit),
    # never leaked to the global _created_views set — so one run's cleanup can't drop another's views.
    from kernel import db
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

    from kernel.agent import run_agent

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


def test_agent_status_honors_explicit_api_key(monkeypatch):
    # an explicit DP_AGENT_API_KEY override must make the agent available even with no env-var key
    # (regression: status previously only checked agent_base_url and mis-reported unavailable)
    from kernel.agent import agent_status
    from kernel.settings import settings
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "agent_api_key", "sk-explicit-override")
    assert agent_status()["available"] is True


def test_agent_recovers_from_tool_error_and_summarizes():
    # A tool that returns an {"error": ...} dict (here: connect before any node exists) must NOT crash
    # the run — the model sees the error, recovers, and finishes with a plain-text summary. Also proves
    # the failed connect left no dangling edge and was recorded in the transcript.
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kernel.agent import run_agent

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
    from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="upper_fmt", title="upper", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="format")])
    def lower(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "format")
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM _')
    deps.node_specs[spec.kind] = spec
    deps.node_lowerings[spec.kind] = lower

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
    from kernel import auth, metadb
    from kernel.metadb import User, session
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
    from kernel import auth, metadb
    from kernel.metadb import User, session
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
    from kernel import paths
    from kernel.settings import settings
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
    from kernel import metadb
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

    from kernel import metadb
    from kernel.metadb import Canvas, session
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

    from kernel import db
    from kernel.plugins.adapters import LanceAdapter
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

    from kernel import db, destinations, metadb
    from kernel.plugins.adapters import DuckDBAdapter

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
    from kernel import metadb
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
    from kernel.deps import get_deps
    from kernel.models import RunStatus
    from kernel.plugins.runner import _MAX_RUNS
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
    from kernel import metadb
    from kernel.deps import get_deps
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


def test_reconcile_marks_orphaned_runs_interrupted():
    # a run left 'running' when the kernel stopped must be reconciled to terminal on startup, else a
    # client would poll it forever (the persisted status would say 'running' with no executor behind it).
    from kernel import metadb
    metadb.save_run_state("run_orphan_x", {"run_id": "run_orphan_x", "status": "running", "per_node": []})
    assert metadb.get_run_state("run_orphan_x")["status"] == "running"
    assert metadb.reconcile_orphaned_runs() >= 1
    d = metadb.get_run_state("run_orphan_x")
    assert d["status"] == "failed" and "restart" in (d.get("error") or "")


def test_catalog_entries_are_shared_across_instances(tmp_path):
    # a dataset/output registered on one instance's catalog is visible to ANOTHER instance (and after a
    # restart) via the shared DB — the catalog half of making the web tier stateless.
    from kernel.deps import get_deps
    from kernel.plugins.catalog import InMemoryCatalog
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


def test_collab_relay_gates_viewer_doc_updates(monkeypatch):
    # a viewer may watch (presence + peers' edits) but its OWN doc updates ('yjs' carries CRDT state)
    # must NOT be relayed — else an editor peer would merge + autosave them, laundering a change past
    # the read-only boundary that put_canvas enforces.
    from kernel import auth, metadb
    from kernel.metadb import Canvas, session
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
    from kernel.backends import ExecutionBackend
    from kernel.deps import Deps

    class FakeBackend:
        name = "fake-pod"
        def can_run(self, plan): return True
        def estimate(self, plan, rows): return None
        def run(self, plan, graph, target_node_id, placement): return None
        def status(self, run_id): return None
        def cancel(self, run_id): return None

    fake = FakeBackend()
    assert isinstance(fake, ExecutionBackend)  # structural conformance to the contract
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    d.runners.insert(0, fake)
    assert d.pick_runner(object()) is fake  # selected over the local runner because can_run is true


def test_spi_contracts_are_the_real_ones():
    # the old plugins/base.py "contract" was dead code with wrong signatures; it's deleted. The live
    # contracts must match the real code: adapters expose the methods the engine actually calls, and
    # the runners structurally satisfy ExecutionBackend.
    import importlib
    from kernel.backends import ExecutionBackend
    from kernel.plugins.adapters import DuckDBAdapter, LanceAdapter
    from kernel.plugins.runner import LocalRunner
    from kernel.subprocess_runner import SubprocessRunner
    assert importlib.util.find_spec("kernel.plugins.base") is None  # dead SPI file is gone
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
    from kernel.deps import Deps, CORE_API_VERSION

    def make_pack(name, min_core):
        d = tmp_path / "ws" / "plugins" / name
        d.mkdir(parents=True)
        (d / "dataplay.toml").write_text(
            f'name = "{name}"\nversion = "0.1.0"\n' + (f"min_core_api = {min_core}\n" if min_core is not None else ""))
        (d / "__init__.py").write_text(
            "from kernel.sdk import NodeSpec, PortSpec\n"
            "def register(reg):\n"
            f"    reg.add_node(NodeSpec(kind='{name}_node', title='{name}', category='compute',\n"
            "        inputs=[PortSpec(id='in', wire='dataset')], outputs=[PortSpec(id='out', wire='dataset')], params=[]))\n")

    make_pack("goodpack", CORE_API_VERSION)
    make_pack("toonew", CORE_API_VERSION + 1)
    make_pack("unversioned", None)
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
    assert "goodpack_node" in d.node_specs and "unversioned_node" in d.node_specs  # compatible / no manifest → load
    assert "toonew_node" not in d.node_specs                                       # incompatible → skipped
    err = [p for p in d.plugins if p.get("name") == "toonew" and p.get("error")]
    assert err and "core API" in err[0]["error"]


def test_nodespec_frontend_backend_parity():
    # backend nodespecs (/api/nodes) and the frontend hand-built cards (web/src/nodes/kinds/*.tsx)
    # define every built-in kind twice; this guards against the two silently drifting on ports/accepts
    # (they had, on `sql`). Parse each frontend register({...}) literal and compare to BUILTIN_NODE_SPECS.
    import re
    from pathlib import Path
    from kernel.nodespecs import BUILTIN_NODE_SPECS
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
    from kernel import auth
    from kernel.metadb import User, session
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
    from kernel.deps import Deps
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
