"""Compatibility shims for running Ray Data as a distributed engine backend (dp_ray).

Kept in `hub` (not the plugin) so it is importable on Ray WORKER processes — a worker runs the same
interpreter/venv as the driver (the dp_ray driver disables Ray's uv-run worker hook), and `hub` is
installed there, whereas the plugin module and the `__main__` driver are not importable by reference.
"""

from __future__ import annotations


def patch_hash_shuffle() -> None:
    """Work around a Ray 2.56 + pandas-2.x/numpy-2.5 hash-shuffle crash.

    `ray.data._internal.arrow_ops.transform_pyarrow._hash_partition` does
    `hashes = pandas.util.hash_pandas_object(df, index=False).values` then `np.mod(hashes, P, out=hashes)`.
    On pandas 2.x `Series.values` is READ-ONLY, so the in-place `np.mod` raises
    `ValueError: output array is read-only` and every GROUP BY / join / hash-shuffle stage dies on the
    worker. Replace `_hash_partition` with the SAME logic over a writable copy of the hashes (the
    partitioning is unchanged — a copy of the identical hash values).

    Must run on the driver AND on every worker (the shuffle executes in worker processes) — dp_ray applies
    it directly on the driver and via the worker-process-setup-hook env var on workers. Idempotent, scoped
    to exactly the buggy function, and a no-op if that function is absent (other Ray versions)."""
    try:
        import numpy as np
        from ray.data._internal.arrow_ops import transform_pyarrow as T
    except Exception:  # noqa: BLE001 — Ray/numpy not importable ⇒ nothing to patch
        return
    if getattr(T._hash_partition, "_dp_patched", False) or not hasattr(T, "_hash_partition"):
        return

    def _hash_partition(table, num_partitions):  # faithful to Ray 2.56 _hash_partition + writable hashes
        if T._has_unhashable_pandas_types(table.schema):
            partitions = np.zeros((table.num_rows,), dtype=np.int64)
            for i, _tuple in enumerate(zip(*table.columns)):
                partitions[i] = hash(_tuple) % num_partitions
            return partitions
        import pandas as pd
        # np.array(..., copy=True) makes the read-only Series.values writable, so np.mod(out=) can write.
        hashes = np.array(pd.util.hash_pandas_object(
            table.to_pandas(types_mapper=pd.ArrowDtype), index=False).values)
        np.mod(hashes, num_partitions, out=hashes)
        return hashes

    _hash_partition._dp_patched = True
    T._hash_partition = _hash_partition
