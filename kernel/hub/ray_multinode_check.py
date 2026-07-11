"""Multi-node byte-identical validation for the dp_ray distributed backend (ARC3).

Runs INSIDE the `driver` container of docker-compose.ray.yml, against a REAL Ray cluster (a head + 2
worker containers, each its own filesystem) plus MinIO as the shared object store. Proves two things a
single-host multiprocess live test cannot:

  1. the cluster is genuinely MULTI-NODE — >=2 distinct Ray node ids execute work;
  2. a distributed GROUP BY over the cluster's hash shuffle, written WORKER-DIRECT to object storage, is
     byte-identical to single-node DuckDB (schema + rows as a sorted multiset, NULLs included).

Exit 0 = PASS. This is the "no distributed op is trusted until byte-identical on a real cluster" gate.
`ray.init` must never share a process with DuckDB (the dp_ray deadlock), so the node probe runs in its
own subprocess and the aggregate runs via the dp_ray subprocess driver — this process only does DuckDB.
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


# a self-contained node-diversity probe (own process — ray.init must not coexist with DuckDB)
_NODE_PROBE = """
import os, ray
os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
ray.init(address=os.environ["RAY_ADDRESS"], configure_logging=False, log_to_driver=False)
import ray.data
ds = ray.data.range(4000, override_num_blocks=16)
def _nid(b):
    import ray as _r
    nid = _r.get_runtime_context().get_node_id()
    return {"node": [nid] * len(b["id"])}
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
    _log(f"multi-node OK: {n_nodes} distinct Ray node ids executed work")

    # (2) byte-identical differential — DuckDB oracle vs the dp_ray cluster run (no ray.init here)
    from hub import db, metadb
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
    if st.status != "done":
        _log(f"FAIL: cluster run did not complete: {st.status} — {st.error}")
        return 1
    _log(f"cluster GROUP BY done → {st.output_uri}")

    # DuckDB oracle: the same aggregate on the single-node engine, reading the same object-store source
    with db.run_scope():
        oracle = BuildEngine(g, deps.resolve_adapter, deps.registry, full=True,
                             node_specs=deps.node_specs, output_node="a").relation("a").to_arrow_table()
    ray_tbl = deps.resolve_adapter(st.output_uri).scan(st.output_uri).to_arrow_table()  # union_by_name in the adapter
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
    # a deliberate FAILING control (DP_MULTINODE_FAULT=1): perturb the oracle so a green run can only
    # mean the differential actually compares — a run with the fault set MUST exit nonzero (OPS-12).
    if os.environ.get("DP_MULTINODE_FAULT") == "1" and duck_rows:
        duck_rows = duck_rows[1:]
    if ray_rows != duck_rows:
        _log(f"FAIL: cluster aggregate != DuckDB\nray ={ray_rows}\nduck={duck_rows}")
        return 1
    _log(f"PASS: distributed GROUP BY byte-identical to DuckDB across {n_nodes} nodes "
         f"({len(ray_rows)} groups incl. NULL); worker-direct output in object storage")

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
    if stj.status != "done":
        _log(f"FAIL: cluster join did not complete: {stj.status} — {stj.error}")
        return 1
    with db.run_scope():
        joracle = BuildEngine(jg, deps.resolve_adapter, deps.registry, full=True,
                              node_specs=deps.node_specs, output_node="j").relation("j").to_arrow_table()
    jray = deps.resolve_adapter(stj.output_uri).scan(stj.output_uri).to_arrow_table()
    con.register("joracle", joracle)
    con.register("jrayout", jray)
    jq = "SELECT cat, v, name FROM {t} ORDER BY v, cat"
    if con.execute(jq.format(t="jrayout")).fetchall() != con.execute(jq.format(t="joracle")).fetchall():
        _log("FAIL: cluster broadcast join != DuckDB")
        return 1
    _log(f"PASS: broadcast join byte-identical to DuckDB across {n_nodes} nodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
