# Out-of-core benchmark — evidence for the headline claim

Data Playground's headline is a *real out-of-core engine* — it should process datasets **larger than
memory** by spilling to disk, not by loading everything into RAM. This document is the evidence.
The harness is `kernel/bench/out_of_core.py`; the numbers below are from one real run (reproduce with
the commands shown — absolute times depend on your machine and disk).

## What it measures

It generates a synthetic Parquet dataset, then runs a real pipeline **through the actual kernel
engine** — `source → filter → sort`, evaluated exactly as a run is (inside `db.run_scope()`, the
same per-run cursor a real execution uses) — with DuckDB's memory budget deliberately capped **well
below** the data via `DP_MEMORY_LIMIT`. A full `ORDER BY` can't be answered from a bounded top-N
heap, so if the engine is out-of-core it performs an **external merge sort to disk**. While the run
executes, a sampler thread records **peak process RSS** and **peak bytes spilled to disk**.

```bash
cd kernel
uv run python -m bench.out_of_core                     # 40M rows, 512MB cap
uv run python -m bench.out_of_core --rows 240000000    # push the working set higher
uv run python -m bench.out_of_core --mem 1500MB --rows 240000000 --keep
```

## Results (macOS · DuckDB 1.5.4 · Python 3.12)

| rows | dataset (parquet) | memory cap | spilled to disk | peak RSS | wall (run) | result |
|-----:|------------------:|-----------:|----------------:|---------:|-----------:|:------|
| 40M  | 798 MB            | 488 MiB    | 540 MB          | 864 MB   | 0.8 s      | ✅ out-of-core |
| 120M | 2.3 GB            | 488 MiB    | 2.8 GB          | 1.2 GB   | 4.2 s      | ✅ out-of-core |
| 240M | 4.7 GB            | 488 MiB    | —               | —        | —          | ❌ OOM on the final write (see below) |
| 240M | 4.7 GB            | 1.3 GiB    | 4.9 GB          | 2.3 GB   | 8.7 s      | ✅ out-of-core |

## What this proves

- **The query pipeline is genuinely out-of-core.** At 120M rows it sorted a 2.3 GB dataset under a
  488 MiB memory cap, spilling **2.8 GB** to disk; at 240M rows it sorted 4.7 GB under a 1.3 GiB cap,
  spilling **4.9 GB**. The data doesn't have to fit in memory.
- **Peak RSS tracks the memory *cap*, not the data size.** 488 MiB cap → ~0.9–1.2 GB RSS regardless
  of whether the dataset is 0.8 GB or 2.3 GB; raising the cap to 1.3 GiB raised RSS to ~2.3 GB. RSS is
  `cap + a roughly fixed runtime overhead` (Python + DuckDB + Arrow + the Parquet writer), **not**
  proportional to the dataset. That is the definition of out-of-core.
- Spilled bytes scale with the data (0.5 → 2.8 → 4.9 GB); the work moved to disk, as intended.

## The honest limit it surfaced

At **240M rows under a tight 488 MiB cap**, the run failed — but not in the sort. It failed on the
**final order-preserving write** of the 120M sorted rows:

```
_duckdb.OutOfMemoryException: failed to pin block of size 256.0 KiB (483.6 MiB/488.2 MiB used)
```

DuckDB's default `preserve_insertion_order = true` makes the Parquet writer buffer to emit row groups
in order; materializing a *very large fully-ordered* result needs headroom proportional to that
buffering. Giving it modest headroom (a 1.3 GiB cap) let the same 240M-row job complete. So:

- The scan / filter / **sort / aggregate** operators are out-of-core (bounded by the cap).
- Materializing a **huge, fully-ordered** result is the memory-bound step — raise `DP_MEMORY_LIMIT`
  for it (headroom is a few GB, **not** proportional to the dataset). Most real pipelines write an
  aggregated or top-N result (small) rather than a fully-sorted 120M-row dump, so this is a stress
  case, not the common path. We deliberately do **not** silently set `preserve_insertion_order=false`
  (it would scramble a sorted write's on-disk order).

## Engine configuration this relies on

`db._apply_session` sets DuckDB's `temp_directory` to `DP_SPILL_DIR` (the same dir the Python-transform
spill uses) so spills go to an explicit, operator-controllable location — put it on fast, roomy disk
in a deployment. `DP_MEMORY_LIMIT` (unset by default → DuckDB's default of ~80% RAM) caps per-kernel
memory; set it to bound RAM in a multi-tenant deployment (see the README "Multi-user isolation" note).
