#!/usr/bin/env python3
"""Certify a measured local performance and memory envelope (issue #640).

Runs a small set of named supported local workflows headlessly against the real in-process
engine and records, for each one, the machine spec, dataset manifest, wall time, peak RSS,
observed spill bytes, and the exact outcome — including the honest behavior at and beyond the
configured DuckDB memory budget (``DP_MEMORY_LIMIT``, the product's per-kernel RAM cap; DuckDB's
own default is ~80% of physical RAM, so a physical-RAM shortfall degrades the same way).

This is a bounded certification harness, not a benchmark platform: three workflows plus one
out-of-memory boundary case, one script, one evidence doc.

Named workflows (see ``WORKFLOWS``):
  * ``under_budget_write``  — CSV source → rollup → managed-local write, well under the budget.
  * ``over_budget_spill``   — the same shape at a size that exceeds the budget (DuckDB spills to
                              disk and the run still completes correctly).
  * ``profile_over_budget`` — one full profile pass over the larger dataset.
  * ``oom_boundary``        — the same oversized rollup with the spill volume also bounded: the
                              run fails with a truthful error and commits nothing (never a silent
                              wrong result). This is the workflow #489 ("measured workflow") builds on.

Usage::

    python3 scripts/perf_envelope.py                       # full envelope → stdout summary + JSON
    python3 scripts/perf_envelope.py --output envelope.json
    python3 scripts/perf_envelope.py --workflows under_budget_write over_budget_spill

Each workflow runs in its own subprocess so its peak RSS and memory/spill caps are isolated;
``run-one`` is that per-workflow worker and is not meant to be called directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Datasets — deterministic DuckDB range() COPY, same schema at two sizes.
# ---------------------------------------------------------------------------
# key is a wide, high-cardinality string; the large rollup groups on it so the hash table exceeds
# a tight memory budget and DuckDB must spill. category/amount give the rollup something to reduce.
_SELECT = (
    "SELECT i AS id, "
    "((i % {groups})::VARCHAR || '-' || repeat('k', 40)) AS gkey, "
    "(i % 16) AS category, "
    "(i * 1.5) AS amount "
    "FROM range(0, {rows}) t(i)"
)

DATASETS: dict[str, dict] = {
    "orders_small": {"rows": 200_000, "groups": 200_000, "format": "csv"},
    "orders_large": {"rows": 6_000_000, "groups": 3_000_000, "format": "parquet"},
}


def _dataset_path(data_dir: Path, name: str) -> Path:
    return data_dir / f"{name}.{DATASETS[name]['format']}"


def build_datasets(data_dir: Path) -> dict[str, dict]:
    """Materialize every dataset (idempotent) and return a manifest with content checksums."""
    import duckdb

    data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    manifest: dict[str, dict] = {}
    for name, spec in DATASETS.items():
        path = _dataset_path(data_dir, name)
        select = _SELECT.format(rows=spec["rows"], groups=spec["groups"])
        fmt = "FORMAT CSV, HEADER" if spec["format"] == "csv" else "FORMAT PARQUET"
        con.execute(f"COPY ({select}) TO '{path}' ({fmt})")
        reader = "read_csv_auto" if spec["format"] == "csv" else "read_parquet"
        rows, xor = con.execute(
            f"SELECT count(*), bit_xor(hash(id, gkey, category, amount))::VARCHAR "
            f"FROM {reader}('{path}')"
        ).fetchone()
        manifest[name] = {
            "format": spec["format"],
            "rows": int(rows),
            "columns": ["id", "gkey", "category", "amount"],
            "file_bytes": path.stat().st_size,
            "content_xxhash": hashlib.sha256(str(xor).encode()).hexdigest()[:32],
        }
    return manifest


# ---------------------------------------------------------------------------
# Measurement primitives.
# ---------------------------------------------------------------------------
def peak_rss_bytes() -> int:
    """Peak resident set of this process. ru_maxrss is bytes on macOS, KiB on Linux."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class SpillSampler(threading.Thread):
    """Poll the spill directory and record the peak bytes on disk during a run."""

    def __init__(self, spill_dir: Path, interval: float = 0.02):
        super().__init__(daemon=True)
        self._dir = spill_dir
        self._interval = interval
        self._stop_event = threading.Event()
        self.peak_bytes = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.peak_bytes = max(self.peak_bytes, _dir_bytes(self._dir))
            except OSError:
                pass
            time.sleep(self._interval)

    def stop(self) -> int:
        self._stop_event.set()
        self.join(timeout=2.0)
        return self.peak_bytes


def machine_spec() -> dict:
    try:
        total_ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        total_ram = None
    import duckdb

    return {
        "platform": sys.platform,
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "total_ram_bytes": total_ram,
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "git_sha": _git_sha(),
    }


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ---------------------------------------------------------------------------
# Workflow definitions.
# ---------------------------------------------------------------------------
# memory_limit is the configured DuckDB budget; temp_cap bounds the spill volume (simulating an
# exhausted/full spill disk) and is only set for the out-of-memory boundary case.
WORKFLOWS: dict[str, dict] = {
    "under_budget_write": {
        "dataset": "orders_small", "memory_limit": "2GB", "temp_cap": None,
        "kind": "write", "expect": "committed",
        "description": "CSV source → rollup → managed-local write, well under the memory budget.",
    },
    "over_budget_spill": {
        "dataset": "orders_large", "memory_limit": "256MB", "temp_cap": None,
        "kind": "write", "expect": "committed_with_spill",
        "description": "Same shape at a size exceeding the budget; DuckDB spills to disk and the "
                       "run still completes correctly.",
    },
    "profile_over_budget": {
        "dataset": "orders_large", "memory_limit": "256MB", "temp_cap": None,
        "kind": "profile", "expect": "profiled",
        "description": "One full profile pass over every row of the larger dataset.",
    },
    "oom_boundary": {
        "dataset": "orders_large", "memory_limit": "256MB", "temp_cap": "32MB",
        "kind": "oom", "expect": "failed_truthfully",
        "description": "Oversized rollup with the spill volume also bounded: the run fails with a "
                       "truthful error and commits nothing.",
    },
}


def _rollup_graph(source_uri: str):
    from hub.models import Graph

    return Graph.model_validate({
        "id": "cv-perf-envelope", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": source_uri}}},
            {"id": "rollup", "type": "aggregate", "position": {"x": 1, "y": 0},
             "data": {"config": {"groupBy": "gkey",
                                 "aggs": "sum(amount) AS total, count(*) AS n"}}},
            {"id": "write", "type": "write", "position": {"x": 2, "y": 0},
             "data": {"title": "envelope_out",
                      "config": {"filename": "envelope_out.parquet", "writeMode": "overwrite"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "rollup"},
            {"id": "e2", "source": "rollup", "target": "write"},
        ],
    })


# ---------------------------------------------------------------------------
# Per-workflow worker (its own process for an isolated RSS + memory/spill cap).
# ---------------------------------------------------------------------------
def run_one(name: str, data_dir: Path) -> dict:
    spec = WORKFLOWS[name]
    source = _dataset_path(data_dir, spec["dataset"])

    tmp = Path(tempfile.mkdtemp(prefix=f"perf-{name}-"))
    workspace, spill = tmp / "workspace", tmp / "spill"
    workspace.mkdir()
    spill.mkdir()
    os.environ.update(
        DP_WORKSPACE=str(workspace),
        DP_DATA_DIR=str(data_dir),
        DP_DATABASE_URL=f"sqlite:///{workspace / 'meta.db'}",
        DP_SPILL_DIR=str(spill),
        DP_MEMORY_LIMIT=spec["memory_limit"],
        DP_MIN_MEM_PER_THREAD_MB="96",
        DP_RUN_DEADLINE_S="1800",
    )

    from hub import compiler, db, metadb
    from hub.deps import Deps

    metadb.init_db()
    deps = Deps(str(workspace), str(data_dir))
    threads = int(db.conn().execute("SELECT current_setting('threads')").fetchone()[0])
    if spec["temp_cap"]:
        db.conn().execute("SET max_temp_directory_size = ?", [spec["temp_cap"]])

    result: dict = {
        "workflow": name,
        "kind": spec["kind"],
        "dataset": spec["dataset"],
        "memory_limit": spec["memory_limit"],
        "spill_cap": spec["temp_cap"],
        "threads": threads,
        "description": spec["description"],
    }
    sampler = SpillSampler(spill)
    sampler.start()
    started = time.perf_counter()

    if spec["kind"] == "profile":
        from hub.executors.profile import profile_node

        graph = _rollup_graph(str(source))
        prof = profile_node(graph, "source", deps.resolve_adapter, deps.registry,
                            node_specs=deps.node_specs, full=True)
        result.update(status="done", rows_scanned=prof.row_count, sampled=prof.sampled,
                      columns_profiled=len(prof.columns))
        ok = prof.sampled is False and prof.row_count == DATASETS[spec["dataset"]]["rows"]
    else:
        graph = _rollup_graph(str(source))
        plan = compiler.compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
        run = deps.runner.run(plan, graph, "write", "local")
        deps.runner.wait_for_worker(run.run_id, timeout=1800)
        final = deps.runner.status(run.run_id)
        committed = [o.uri for o in (final.outputs or []) if o.outcome == "committed"]
        result.update(status=final.status, total_rows=final.total_rows,
                      committed_outputs=len(committed), run_error=(final.error or None))
        if spec["kind"] == "write":
            verified = final.status == "done" and len(committed) == 1 and _verify_rollup(
                committed[0], DATASETS[spec["dataset"]]["rows"])
            result["output_verified"] = verified
            ok = verified
        else:  # oom: a truthful failure that commits nothing, plus a memory-named root cause.
            root = _oom_root_cause(db, source)
            result["oom_root_cause"] = root
            ok = (final.status == "failed" and len(committed) == 0
                  and root["is_out_of_memory"])

    result["wall_seconds"] = round(time.perf_counter() - started, 3)
    result["peak_spill_bytes"] = sampler.stop()
    result["peak_rss_bytes"] = peak_rss_bytes()
    result["ok"] = bool(ok)
    return result


def _verify_rollup(committed_uri: str, input_rows: int) -> bool:
    """Confirm the committed rollup is arithmetically complete — every input row is accounted for in
    the per-group counts — so a success is never a silent wrong result."""
    import duckdb

    accounted = duckdb.connect().execute(
        f"SELECT sum(n) FROM read_parquet('{committed_uri}')").fetchone()[0]
    return accounted == input_rows


def _oom_root_cause(db, source: Path) -> dict:
    """Force the rollup's blocking forcing point on the product's own session config and capture the
    truthful out-of-memory error (proving the failure is the memory boundary, not an unrelated fault,
    and that no rows are returned)."""
    import duckdb

    reader = "read_csv_auto" if source.suffix == ".csv" else "read_parquet"
    sql = (f"SELECT count(*) FROM (SELECT gkey, sum(amount) t, count(*) n "
           f"FROM {reader}('{source}') GROUP BY gkey)")
    try:
        with db.run_scope():
            rows = db.conn().execute(sql).fetchall()
        return {"is_out_of_memory": False, "exception": None,
                "message": None, "rows_returned": len(rows)}
    except duckdb.OutOfMemoryException as exc:
        return {"is_out_of_memory": True, "exception": "OutOfMemoryException",
                "message": str(exc).splitlines()[0][:200], "rows_returned": 0}
    except duckdb.Error as exc:  # any other DuckDB failure is still truthful, just not memory-named
        return {"is_out_of_memory": False, "exception": type(exc).__name__,
                "message": str(exc).splitlines()[0][:200], "rows_returned": 0}


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------
def orchestrate(names: list[str], data_dir: Path) -> dict:
    manifest = build_datasets(data_dir)
    workflows = []
    for name in names:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "run-one",
             "--name", name, "--data-dir", str(data_dir)],
            capture_output=True, text=True,
        )
        payload = _extract_result(proc.stdout)
        if proc.returncode != 0 or payload is None:
            workflows.append({"workflow": name, "ok": False, "status": "harness_error",
                              "stderr": proc.stderr[-2000:]})
        else:
            workflows.append(payload)

    return {
        "schema_version": 1,
        "issue": 640,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": machine_spec(),
        "datasets": manifest,
        "workflows": workflows,
        "all_ok": all(w.get("ok") for w in workflows),
    }


_RESULT_PREFIX = "PERF_ENVELOPE_RESULT "


def _extract_result(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith(_RESULT_PREFIX):
            return json.loads(line[len(_RESULT_PREFIX):])
    return None


def _summary(envelope: dict) -> str:
    lines = [f"Perf envelope @ {envelope['machine']['git_sha'][:12]} "
             f"({envelope['machine']['platform']}/{envelope['machine']['machine']}, "
             f"{envelope['machine']['cpu_count']} cpu)"]
    for w in envelope["workflows"]:
        tag = "OK " if w.get("ok") else "FAIL"
        detail = (f"status={w.get('status')} rows={w.get('total_rows') or w.get('rows_scanned')} "
                  f"spill={_mib(w.get('peak_spill_bytes'))} rss={_mib(w.get('peak_rss_bytes'))} "
                  f"{w.get('wall_seconds')}s")
        lines.append(f"  [{tag}] {w['workflow']:<20} {detail}")
    lines.append(f"all_ok={envelope['all_ok']}")
    return "\n".join(lines)


def _mib(value) -> str:
    return f"{value / 2 ** 20:.0f}MiB" if isinstance(value, int) else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    one = sub.add_parser("run-one", help=argparse.SUPPRESS)
    one.add_argument("--name", required=True, choices=list(WORKFLOWS))
    one.add_argument("--data-dir", required=True, type=Path)

    parser.add_argument("--workflows", nargs="+", choices=list(WORKFLOWS), default=list(WORKFLOWS))
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="dataset directory (default: a fresh temp dir)")
    parser.add_argument("--output", type=Path, default=None,
                        help="write the envelope JSON here (default: stdout only)")
    args = parser.parse_args(argv)

    if args.cmd == "run-one":
        result = run_one(args.name, args.data_dir.resolve())
        print(_RESULT_PREFIX + json.dumps(result))
        return 0 if result.get("ok") else 1

    data_dir = (args.data_dir or Path(tempfile.mkdtemp(prefix="perf-data-"))).resolve()
    envelope = orchestrate(args.workflows, data_dir)
    text = json.dumps(envelope, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(_summary(envelope))
    if not args.output:
        print(text)
    return 0 if envelope["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
