"""End-to-end kernel tests — the real out-of-core lowering engine on real files."""

from __future__ import annotations

import time

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
    assert info["runners"] == ["local-out-of-core"]
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
    # sql outputs sql-view; aggregate input accepts only dataset → must be rejected server-side
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("q", "sql", {"sql": "SELECT * FROM input"}),
        N("agg", "aggregate", {"aggs": "count(*) AS n"}),
    ], "edges": [E("src", "q"), {"id": "bad", "source": "q", "target": "agg", "data": {"wire": "sql-view"}}]}
    assert client.post("/api/run", json={"graph": g, "targetNodeId": "agg", "confirmed": True}).status_code == 400


def test_metric_edge_wire_does_not_422():
    # an edge carrying wire='metric' (metric→variable is a valid connection) must not 422 the graph
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("m", "metric", {"agg": "count"}),
        N("v", "variable", {}),
    ], "edges": [E("src", "m"), {"id": "mv", "source": "m", "target": "v", "data": {"wire": "metric"}}]}
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
