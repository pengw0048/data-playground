"""Out-of-core benchmark — evidence for the headline claim ("processes data larger than memory").

Generates a synthetic Parquet dataset, then runs a real pipeline through the ACTUAL kernel engine
(source -> filter -> sort, evaluated exactly as a run does — inside a `db.run_scope()`) with the
DuckDB memory budget (DP_MEMORY_LIMIT, honored by db._apply_session) capped WELL BELOW the working
set. A full sort can't be done with a bounded top-N heap, so if the engine is out-of-core it spills
an external merge sort to disk and the process's peak RSS stays near the cap — not the data size.

    uv run python -m bench.out_of_core                      # 40M rows, 512MB cap
    uv run python -m bench.out_of_core --rows 120000000     # push the working set higher
    uv run python -m bench.out_of_core --mem 1GB --keep

Reports peak process RSS and peak bytes spilled to disk during the run. The proof is twofold:
peak spill > 0 (it went to disk) AND peak RSS stays near the cap as --rows grows (RSS doesn't scale
with data). Run it at a few sizes and watch RSS stay flat while spill grows.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time

import duckdb


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024


def _rss_now(pid: int) -> int:
    """Current resident set size in bytes (Linux via /proc, else `ps`)."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
        return int(out.stdout.strip() or 0) * 1024  # ps reports KB


def _dir_size(d: str) -> int:
    total = 0
    for root, _, files in os.walk(d):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


class _Sampler(threading.Thread):
    """Samples peak process RSS and peak spill-dir size while the run executes."""
    def __init__(self, pid: int, spill: str):
        super().__init__(daemon=True)
        self.pid, self.spill = pid, spill
        self.peak_rss = self.peak_spill = 0
        self._ev = threading.Event()  # NB: don't name this `_stop` — it shadows Thread._stop()

    def run(self) -> None:
        while not self._ev.is_set():
            self.peak_rss = max(self.peak_rss, _rss_now(self.pid))
            self.peak_spill = max(self.peak_spill, _dir_size(self.spill))
            self._ev.wait(0.1)

    def stop(self) -> None:
        self._ev.set()
        self.join(timeout=2)


def _gen(path: str, rows: int, groups: int) -> None:
    con = duckdb.connect()  # throwaway; COPY streams, so generation stays memory-bounded
    con.execute(
        f"COPY (SELECT i AS id, i % {groups} AS grp, "
        f"((i * 2654435761) % 1000000) / 1000000.0 AS value, 'item-' || (i % 100000) AS label "
        f"FROM range({rows}) t(i)) TO '{path}' (FORMAT PARQUET)"
    )
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Out-of-core validation benchmark")
    ap.add_argument("--rows", type=int, default=40_000_000, help="synthetic row count (default 40M)")
    ap.add_argument("--groups", type=int, default=5_000_000, help="group-by cardinality of the data")
    ap.add_argument("--mem", default="512MB", help="DuckDB memory_limit via DP_MEMORY_LIMIT (default 512MB)")
    ap.add_argument("--keep", action="store_true", help="keep generated data + output")
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="dp-bench-")
    spill = os.path.join(work, "spill")
    os.makedirs(spill, exist_ok=True)
    # Set the knobs BEFORE the kernel opens its connection — db._apply_session reads these env vars.
    os.environ["DP_SPILL_DIR"] = spill
    os.environ["DP_MEMORY_LIMIT"] = args.mem
    data_path = os.path.join(work, "data.parquet")
    out_path = os.path.join(work, "out.parquet")

    print(f"duckdb {duckdb.__version__} · python {platform.python_version()} · {platform.system()}")
    print(f"generating {args.rows:,} rows …", flush=True)
    t0 = time.time()
    _gen(data_path, args.rows, args.groups)
    data_sz = os.path.getsize(data_path)
    print(f"  → {_human(data_sz)} parquet in {time.time() - t0:.1f}s")

    # Import the engine only AFTER the env is set (import is cheap; the connection opens lazily on use).
    from kernel import db
    from kernel.deps import Deps
    from kernel.executors.engine import LoweringEngine
    from kernel.models import Graph

    deps = Deps(work, work)
    with db.lock():  # confirm the cap the engine actually applied, straight from the connection
        applied_mem = db.conn().sql("SELECT current_setting('memory_limit')").fetchone()[0]
        applied_tmp = db.conn().sql("SELECT current_setting('temp_directory')").fetchone()[0]
    print(f"engine memory_limit={applied_mem} · temp_directory={applied_tmp}")

    # source -> filter(~half) -> sort(full external merge — the canonical out-of-core spiller).
    graph = Graph(**{
        "id": "bench", "version": 1,
        "nodes": [
            {"id": "src", "type": "source", "position": {"x": 0, "y": 0}, "data": {"config": {"uri": data_path}}},
            {"id": "flt", "type": "filter", "position": {"x": 0, "y": 0}, "data": {"config": {"predicate": "value > 0.5"}}},
            {"id": "srt", "type": "sort", "position": {"x": 0, "y": 0}, "data": {"config": {"by": "value DESC, id"}}},
        ],
        "edges": [
            {"id": "e1", "source": "src", "target": "flt", "data": {"wire": "dataset"}},
            {"id": "e2", "source": "flt", "target": "srt", "data": {"wire": "dataset"}},
        ],
    })

    print(f"running source→filter→sort (full external sort) …", flush=True)
    sampler = _Sampler(os.getpid(), spill)
    sampler.start()
    t0 = time.time()
    with db.run_scope():  # exactly how a real run evaluates — its own cursor, shared settings
        eng = LoweringEngine(graph, deps.resolve_adapter, deps.registry, full=True,
                             node_lowerings=deps.node_lowerings, node_specs=deps.node_specs)
        eng.relation("srt").write_parquet(out_path)  # forces the full out-of-core sort to disk
    run_dt = time.time() - t0
    sampler.stop()

    out_rows = duckdb.connect().sql(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]

    spilled = sampler.peak_spill > 0
    print("\n─── result ───────────────────────────────────────────────")
    print(f"dataset (input)      {_human(data_sz)}  ({args.rows:,} rows, compressed parquet)")
    print(f"memory cap           {applied_mem}")
    print(f"output rows (sorted) {out_rows:,}")
    print(f"wall time (run)      {run_dt:.1f}s")
    print(f"peak process RSS     {_human(sampler.peak_rss)}")
    print(f"peak spilled to disk {_human(sampler.peak_spill)}")
    print("──────────────────────────────────────────────────────────")
    # The direct out-of-core signal is that intermediates went to DISK. Peak RSS is reported for the
    # "flat across sizes" story (run several --rows: RSS stays ~cap + a fixed runtime overhead while
    # spill grows). Comparing RSS to the COMPRESSED parquet size would be misleading, so we don't.
    print(f"OUT-OF-CORE ✓  spilled {_human(sampler.peak_spill)} to disk under a {applied_mem} cap "
          "(external sort went to disk, not RAM)" if spilled else
          "no spill — the working set fit the cap; raise --rows or lower --mem")

    if args.keep:
        print(f"kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
