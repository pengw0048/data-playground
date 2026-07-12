"""Multi-node byte-identical validation for the dp_ray distributed backend (ARC3).

Runs INSIDE the `driver` container of docker-compose.ray.yml, against a REAL Ray cluster (a head + 2
worker containers, each its own filesystem) plus MinIO as the shared object store. Proves, precisely:

  1. the CLUSTER genuinely spreads a HASH-SHUFFLE EXCHANGE (the aggregate's exact mechanism) across
     >=2 distinct Ray node ids — the node probe repartitions-by-key then reports per-partition node ids;
  2. native object-store Parquet reads feed a distributed GROUP BY and broadcast join, each confirmed
     to have run on the Ray path
     (`status.placement == "distributed"` — NOT dp_ray's silent single-node fallback), written
     WORKER-DIRECT to object storage, are byte-identical to single-node DuckDB (schema + rows as a sorted
     multiset, NULLs included);
  3. physical-footer schema drift is unified, a flat-root Hive-looking ancestor stays out of the schema,
     and typed numeric/string Hive columns survive a real distributed aggregate + broadcast join;
  4. a whole-graph overwrite sink writes Ray blocks directly to an immutable object prefix, publishes a
     success manifest, registers that actual URI, and never substitutes the stable logical filename.

Note the honest scope: N nodes is credited to the cluster/shuffle (the probe), not to the specific tested
query — a query's own node spread isn't observable from this process (the dp_ray run executes in its own
subprocess driver); `placement == "distributed"` is what proves the tested query ran distributed.

The three oracles (schema parity, aggregate rows, join rows) each have a fault-injection control so none
can be silently inert — see DP_MULTINODE_FAULT below.

Exit 0 = PASS. This is the "no distributed op is trusted until byte-identical on a real cluster" gate.
`ray.init` must never share a process with DuckDB (the dp_ray deadlock), so the node probe runs in its
own subprocess and the aggregate/join run via the dp_ray subprocess driver — this process only does DuckDB.

DP_MULTINODE_FAULT (a self-test of the harness itself): "schema" | "rows" | "join" each perturbs exactly
ONE oracle's input so that oracle MUST report a mismatch → the run MUST exit nonzero. Run the harness once
per value (each must fail) plus once clean (must pass) to prove every oracle actually compares. "1" == "rows".
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def _log(m: str) -> None:
    print(f"[multinode] {m}", flush=True)


def _wait_tcp(host: str, port: int, timeout: float = 120) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"{host}:{port} not reachable in {timeout}s")


def _wait_http(url: str, timeout: float = 120) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url + "/minio/health/live", timeout=3)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise TimeoutError(f"{url} not healthy in {timeout}s")


# a self-contained node-diversity probe (own process — ray.init must not coexist with DuckDB). It runs
# the SAME hash-shuffle the distributed aggregate uses (repartition by key → map over each partition), so
# it proves the cluster spreads a SHUFFLE EXCHANGE across >=2 nodes — the exact mechanism under test — not
# just that independent read tasks scatter. (Node diversity of the specific tested query can't be observed
# from this process — the dp_ray run executes in its own subprocess driver — so the PASS log is careful to
# credit N nodes to the cluster/shuffle, and proves the tested query ran DISTRIBUTED via st.placement.)
_NODE_PROBE = """
import os, ray
os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
ray.init(address=os.environ["RAY_ADDRESS"], configure_logging=False, log_to_driver=False)
import ray.data
ds = ray.data.range(8000, override_num_blocks=16).map(lambda r: {"k": r["id"] % 20, "id": r["id"]})
ds = ds.repartition(16, keys=["k"])          # the SAME hash-shuffle the distributed GROUP BY runs on
def _nid(b):
    import ray as _r
    nid = _r.get_runtime_context().get_node_id()
    return {"node": [nid] * len(b["k"])}
nodes = {r["node"] for r in ds.map_batches(_nid, batch_format="numpy").take_all()}
print("NODES", len(nodes))
"""


def _schema_diff(a, b) -> str | None:
    """None if two Arrow tables have the same column set with the same types (order-independent — both
    engines emit aggregate rows/columns in arbitrary order), else a human diff string."""
    sa = {f.name: str(f.type) for f in a.schema}
    sb = {f.name: str(f.type) for f in b.schema}
    if sa == sb:
        return None
    return f"ray={sa} duck={sb}"


def _load_dp_ray():
    path = os.environ.get("DP_RAY_MODULE", "/app/examples/plugins/dp_ray/__init__.py")
    spec = importlib.util.spec_from_file_location("dp_ray_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    endpoint = os.environ["DP_S3_ENDPOINT"]               # http://minio:9000
    key, secret = os.environ["DP_S3_KEY"], os.environ["DP_S3_SECRET"]
    bucket = os.environ.get("DP_S3_BUCKET", "dpray")
    # the container already joined the cluster (`ray start --address=…` in the entrypoint) → RAY_ADDRESS is
    # "auto" (connect to the local raylet). Only the object store still needs a readiness wait.
    _wait_http(endpoint)
    _log(f"joined cluster; object store {endpoint} reachable")

    # (1) multi-node proof — a separate process (keeps ray.init out of this DuckDB process)
    probe = subprocess.run([sys.executable, "-c", _NODE_PROBE], capture_output=True, text=True, timeout=240)
    n_nodes = next((int(l.split()[1]) for l in probe.stdout.splitlines() if l.startswith("NODES")), 0)
    if n_nodes < 2:
        _log(f"FAIL: cluster is not multi-node — only {n_nodes} node id(s) ran work\n{probe.stdout}\n{probe.stderr[-800:]}")
        return 1
    _log(f"multi-node OK: a hash-shuffle exchange spanned {n_nodes} distinct Ray node ids")
    # fault modes (a deliberate FAILING control): each value perturbs ONE oracle's input so a green run
    # can only mean that oracle actually compares. Run the harness once per value (schema/rows/join) — each
    # MUST exit nonzero — plus once clean (no fault) which MUST pass. "1" == "rows" (back-compat).
    fault = os.environ.get("DP_MULTINODE_FAULT", "").strip().lower()
    if fault == "1":
        fault = "rows"

    # (2) byte-identical differential — DuckDB oracle vs the dp_ray cluster run (no ray.init here)
    from hub import db, metadb
    from hub.compiler import compile_plan
    from hub.deps import Deps
    from hub.executors.engine import BuildEngine
    from hub.models import Graph
    ws, data = tempfile.mkdtemp(prefix="mn_ws_"), tempfile.mkdtemp(prefix="mn_data_")
    deps = Deps(ws, data)
    metadb.init_db()  # create the metadata schema in this fresh workspace DB (settings/catalog/run_states)
    metadb.set_setting("objectStore", {"accessKeyId": key, "secretAccessKey": secret,
                                       "endpoint": endpoint, "region": "us-east-1", "useSsl": False}, "global")
    src, out_uri = f"s3://{bucket}/src.parquet", f"s3://{bucket}/agg_out.parquet"

    db.ensure_object_store()
    # the `createbucket` compose service made the bucket; write the source dataset into the object store
    db.conn().execute(
        f"COPY (SELECT (CASE WHEN i % 37 = 0 THEN NULL ELSE i % 5 END) AS cat, i AS v "
        f"FROM range(0, 8000) t(i)) TO '{src}' (FORMAT PARQUET)")
    _log("source written to the object store")

    g = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": src}}},
        {"id": "a", "type": "aggregate", "position": {"x": 0, "y": 0},
         "data": {"config": {"groupBy": "cat",
                             "aggs": "count(*) AS n, count(v) AS nv, min(v) AS lo, max(v) AS hi, "
                                     "sum(v) AS sm, avg(v) AS av"}}},
    ], "edges": [{"id": "e", "source": "src", "target": "a", "data": {"wire": "dataset"}}]})

    rr = _load_dp_ray().RayRunner(deps)
    st = rr.run_unit(g, "a", out_uri)                     # → dp_ray driver subprocess attaches to RAY_ADDRESS
    for _ in range(1800):
        if rr.status(st.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    st = rr.status(st.run_id)
    # placement MUST be "distributed" — dp_ray silently falls back to the single-node DuckDB engine for a
    # shape it can't distribute (_materialize_local → placement="local"), and THAT would be byte-identical
    # to the DuckDB oracle trivially. Without this check the whole gate could pass on a local fallback,
    # proving nothing about distributed execution (acceptance #10).
    if st.status != "done" or st.placement != "distributed":
        _log(f"FAIL: cluster GROUP BY not distributed — status={st.status} placement={st.placement} — {st.error}")
        return 1
    _log(f"cluster GROUP BY done (placement={st.placement}) → {st.output_uri}")

    # DuckDB oracle: the same aggregate on the single-node engine, reading the same object-store source
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="a").relation("a").to_arrow_table()
    ray_tbl = deps.resolve_adapter(st.output_uri).scan(st.output_uri).to_arrow_table()  # union_by_name in the adapter
    if fault == "schema":  # perturb the SCHEMA oracle's input so it MUST catch a mismatch
        ray_tbl = ray_tbl.rename_columns([c + "_x" for c in ray_tbl.column_names])
    # SCHEMA parity (not just row values, which DuckDB would coerce): the worker-direct Parquet must
    # carry the same column names AND Arrow types as the single-node result (OPS-12).
    serr = _schema_diff(ray_tbl, oracle)
    if serr:
        _log(f"FAIL: cluster aggregate SCHEMA != DuckDB — {serr}")
        return 1
    import duckdb
    con = duckdb.connect()
    con.register("oracle", oracle)
    con.register("rayout", ray_tbl)
    q = "SELECT cat, n, nv, lo, hi, sm, av FROM {t} ORDER BY cat NULLS FIRST, n, nv, lo, hi"
    ray_rows = con.execute(q.format(t="rayout")).fetchall()
    duck_rows = con.execute(q.format(t="oracle")).fetchall()
    if fault == "rows" and duck_rows:  # perturb the ROW oracle's input so it MUST catch a mismatch
        duck_rows = duck_rows[1:]
    if ray_rows != duck_rows:
        _log(f"FAIL: cluster aggregate != DuckDB\nray ={ray_rows}\nduck={duck_rows}")
        return 1
    # NB: {n_nodes} credits the CLUSTER's shuffle (the probe), NOT this specific query — the query's own
    # node spread isn't observable from here; placement=distributed (above) is what proves it ran on Ray.
    _log(f"PASS: distributed GROUP BY (placement=distributed) byte-identical to DuckDB "
         f"({len(ray_rows)} groups incl. NULL); worker-direct object-store output; cluster shuffle spans {n_nodes} nodes")

    # Native Parquet proof on the real Ray/MinIO path. Ray 2.56 otherwise infers one footer and applies
    # Hive parsing above the requested dataset root. Exercise both failure modes plus a genuine immediate
    # Hive partition before this backend can claim worker-direct object reads.
    if not fault:
        ancestor_src = f"s3://{bucket}/tenant=must-not-surface/schema_drift"
        db.conn().execute(
            f"COPY (SELECT CAST(1 AS INTEGER) AS x) TO '{ancestor_src}/part-32.parquet' (FORMAT PARQUET)"
        )
        db.conn().execute(
            f"COPY (SELECT CAST(2 AS BIGINT) AS x) TO '{ancestor_src}/part-64.parquet' (FORMAT PARQUET)"
        )
        ancestor_graph = Graph(**{"id": "native-schema", "version": 1, "nodes": [
            {"id": "src", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": ancestor_src}}},
        ], "edges": []})
        ancestor_status = rr.run_unit(
            ancestor_graph, "src", f"s3://{bucket}/native_schema_out.parquet"
        )
        for _ in range(1800):
            if rr.status(ancestor_status.run_id).status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        ancestor_status = rr.status(ancestor_status.run_id)
        if ancestor_status.status != "done" or ancestor_status.placement != "distributed":
            _log(f"FAIL: native schema/ancestor-Hive run did not distribute: {ancestor_status.error}")
            return 1
        ancestor_table = deps.resolve_adapter(ancestor_status.output_uri).scan(
            ancestor_status.output_uri
        ).to_arrow_table()
        if ancestor_table.column_names != ["x"] or str(ancestor_table.schema.field("x").type) != "int64" \
                or sorted(ancestor_table.column("x").to_pylist()) != [1, 2]:
            _log(f"FAIL: native schema union/ancestor-Hive boundary drifted: {ancestor_table}")
            return 1

        hive_src = f"s3://{bucket}/true_hive"
        db.conn().execute(
            f"COPY (SELECT CAST(10 AS BIGINT) AS v) TO '{hive_src}/cat=1/tier=alpha/part-a.parquet' "
            "(FORMAT PARQUET)"
        )
        db.conn().execute(
            f"COPY (SELECT CAST(20 AS BIGINT) AS v) TO '{hive_src}/cat=2/tier=beta/part-b.parquet' "
            "(FORMAT PARQUET)"
        )
        hive_dim = f"s3://{bucket}/true_hive_dim.parquet"
        db.conn().execute(
            f"COPY (SELECT * FROM (VALUES (1::BIGINT, 'one'), (2::BIGINT, 'two')) t(cat, name)) "
            f"TO '{hive_dim}' (FORMAT PARQUET)"
        )
        hive_graph = Graph(**{"id": "native-hive", "version": 1, "nodes": [
            {"id": "src", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": hive_src}}},
            {"id": "agg", "type": "aggregate", "position": {"x": 0, "y": 0},
             "data": {"config": {"groupBy": "cat, tier", "aggs": "sum(v) AS total"}}},
            {"id": "dim", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": hive_dim}}},
            {"id": "join", "type": "join", "position": {"x": 0, "y": 0},
             "data": {"config": {"on": "cat", "how": "inner"}}},
        ], "edges": [
            {"id": "h1", "source": "src", "target": "agg", "data": {"wire": "dataset"}},
            {"id": "h2", "source": "agg", "target": "join", "data": {"wire": "dataset"}},
            {"id": "h3", "source": "dim", "target": "join", "data": {"wire": "dataset"}},
        ]})
        hive_status = rr.run_unit(hive_graph, "join", f"s3://{bucket}/native_hive_out.parquet")
        for _ in range(1800):
            if rr.status(hive_status.run_id).status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        hive_status = rr.status(hive_status.run_id)
        if hive_status.status != "done" or hive_status.placement != "distributed":
            _log(f"FAIL: true-Hive native read did not distribute: {hive_status.error}")
            return 1
        hive_table = deps.resolve_adapter(hive_status.output_uri).scan(hive_status.output_uri).to_arrow_table()
        with db.run_scope():
            hive_oracle = BuildEngine(
                hive_graph, deps.resolve_adapter, deps.registry, full=True,
                node_specs=deps.node_specs, output_node="join",
            ).relation("join").to_arrow_table()
        hive_schema_error = _schema_diff(hive_table, hive_oracle)
        con.register("hive_ray", hive_table)
        con.register("hive_oracle", hive_oracle)
        hive_query = "SELECT cat, tier, total, name FROM {table} ORDER BY cat, tier"
        hive_rows = con.execute(hive_query.format(table="hive_ray")).fetchall()
        hive_oracle_rows = con.execute(hive_query.format(table="hive_oracle")).fetchall()
        if (hive_schema_error or str(hive_table.schema.field("cat").type) != "int64"
                or str(hive_table.schema.field("tier").type) != "string"
                or hive_rows != hive_oracle_rows
                or [(row[0], row[1], int(row[2]), row[3]) for row in hive_rows]
                != [(1, "alpha", 10, "one"), (2, "beta", 20, "two")]):
            _log(
                f"FAIL: typed true-Hive aggregate/join parity drifted: "
                f"schema={hive_schema_error} ray={hive_rows} duck={hive_oracle_rows}"
            )
            return 1
        _log(
            "PASS: native Parquet unifies physical footers, excludes a flat-root ancestor Hive key, "
            "and preserves typed numeric/string Hive columns through a real aggregate + join"
        )

    # (3) a BROADCAST join across nodes: the big fact (cat, v) ⋈ a small dim (cat, name), right side
    # broadcast to every worker, output written worker-direct to the object store — byte-identical to DuckDB.
    dim = f"s3://{bucket}/dim.parquet"
    db.conn().execute(f"COPY (SELECT i AS cat, ('g'||i) AS name FROM range(0,5) t(i)) TO '{dim}' (FORMAT PARQUET)")
    jout = f"s3://{bucket}/join_out.parquet"
    jg = Graph(**{"id": "c", "version": 1, "nodes": [
        {"id": "l", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": src}}},
        {"id": "r", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": dim}}},
        {"id": "j", "type": "join", "position": {"x": 0, "y": 0}, "data": {"config": {"on": "cat", "how": "inner"}}},
    ], "edges": [{"id": "e1", "source": "l", "target": "j", "data": {"wire": "dataset"}},
                 {"id": "e2", "source": "r", "target": "j", "data": {"wire": "dataset"}}]})
    stj = rr.run_unit(jg, "j", jout)
    for _ in range(1800):
        if rr.status(stj.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    stj = rr.status(stj.run_id)
    if stj.status != "done" or stj.placement != "distributed":  # same anti-local-fallback gate as #10
        _log(f"FAIL: cluster join not distributed — status={stj.status} placement={stj.placement} — {stj.error}")
        return 1
    with db.run_scope():
        joracle = BuildEngine(jg, deps.resolve_adapter, deps.registry, full=True,
                              node_specs=deps.node_specs, output_node="j").relation("j").to_arrow_table()
    jray = deps.resolve_adapter(stj.output_uri).scan(stj.output_uri).to_arrow_table()
    con.register("joracle", joracle)
    con.register("jrayout", jray)
    jq = "SELECT cat, v, name FROM {t} ORDER BY v, cat"
    jray_rows = con.execute(jq.format(t="jrayout")).fetchall()
    jduck_rows = con.execute(jq.format(t="joracle")).fetchall()
    if fault == "join" and jduck_rows:  # perturb the JOIN oracle's input so it MUST catch a mismatch
        jduck_rows = jduck_rows[1:]
    if jray_rows != jduck_rows:
        _log("FAIL: cluster broadcast join != DuckDB")
        return 1
    _log(f"PASS: broadcast join (placement=distributed) byte-identical to DuckDB; cluster shuffle spans {n_nodes} nodes")

    # (4) WHOLE-GRAPH overwrite: unlike run_unit above, this traverses the write-node sink contract. The
    # stable logical target must never receive shards; workers write an immutable attempt prefix, the
    # driver adds the manifest last, and only then does the hub register the actual physical URI.
    whole = Graph(**{"id": "whole", "version": 1, "nodes": [
        {"id": "src", "type": "source", "position": {"x": 0, "y": 0},
         "data": {"config": {"uri": src}}},
        {"id": "w", "type": "write", "position": {"x": 0, "y": 0},
         "data": {"config": {"name": "whole_out", "writeMode": "overwrite"}}},
    ], "edges": [{"id": "ew", "source": "src", "target": "w", "data": {"wire": "dataset"}}]})
    stw = rr.run(compile_plan(whole, "w", deps.registry, deps.node_specs), whole, "w", "local")
    for _ in range(1800):
        if rr.status(stw.run_id).status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    stw = rr.status(stw.run_id)
    expected_prefix = f"s3://{bucket}/outputs/whole_out.attempt-"
    if stw.status != "done" or stw.placement != "distributed":
        _log(f"FAIL: whole-graph overwrite not distributed — {stw.status}/{stw.placement}: {stw.error}")
        return 1
    if not (stw.output_uri or "").startswith(expected_prefix):
        _log(f"FAIL: whole-graph sink did not return an immutable attempt URI: {stw.output_uri}")
        return 1
    from hub.handoff import read_manifest, validate_shards
    manifest = read_manifest(stw.output_uri)
    registered = deps.catalog.get_table(stw.output_table)
    whole_tbl = deps.resolve_adapter(stw.output_uri).scan(stw.output_uri).to_arrow_table()
    if (manifest is None or manifest.get("runId") != stw.run_id or manifest.get("rows") != 8000
            or not validate_shards(stw.output_uri, manifest)):
        _log(f"FAIL: whole-graph success manifest is invalid: {manifest}")
        return 1
    if registered.uri != stw.output_uri or whole_tbl.num_rows != 8000:
        _log(f"FAIL: catalog/output did not publish the completed attempt: {registered.uri}")
        return 1
    _log("PASS: whole-graph Parquet overwrite is worker-direct, immutable, manifested, and cataloged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
