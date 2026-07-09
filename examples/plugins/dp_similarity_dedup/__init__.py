"""Reference plugin — a `similarity-dedup` node that groups near-duplicate rows by an embedding column.

The pain it addresses: near-duplicate detection is a per-team primitive that keeps getting rewritten. This
ships it as a REUSABLE node, so a canvas can preview the duplicate rate on a sample before committing full
compute (drop this node after a `sample`, read the `is_representative` count).

What it does: reads a fixed-width embedding column (a list-of-floats), greedily clusters rows whose cosine
distance is within `threshold`, and adds two columns — `dup_group` (the row index that leads each cluster)
and `is_representative` (True for one row per cluster). Filter `is_representative` downstream to keep one
row per cluster.

Honest limits — read before trusting it at scale:
  * Brute-force O(n²) time. Fine for a preview sample (a few thousand rows); a full run over millions is
    slow. For scale, back it with an approximate-nearest-neighbour index (e.g. a Lance source's native ANN)
    instead of this reference implementation.
  * Global grouping is not streamable, so build() materialises the input in memory (via ctx.polars). That's
    inherent to dedup, not a shortcut — size the input accordingly.
  * Accuracy is only as good as the embeddings. Categories that are visually/semantically repetitive but
    NOT true duplicates will over-merge; distinct rows with collided embeddings will merge wrongly. Tune
    `threshold` against a labelled sample; do not treat the output as ground truth.

Drop this folder into `<workspace>/plugins/`.
"""

from __future__ import annotations

from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx

SPEC = NodeSpec(
    kind="similarity-dedup", title="similarity dedup", category="compute", tag="dedup",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[
        ParamSpec(name="column", type="string", default="embedding",
                  label="embedding column (list of floats)"),
        ParamSpec(name="threshold", type="float", default=0.05,
                  label="max cosine distance to call two rows duplicates"),
    ],
    blurb="cluster near-duplicate rows by embedding similarity → adds dup_group + is_representative "
          "(filter is_representative downstream to keep one per cluster; brute-force O(n²), preview first)",
)


def _cfg(node) -> dict:
    return node.data.get("config", {}) if isinstance(node.data, dict) else {}


def _dedup(df, column: str, threshold: float):
    """fn for ctx.polars: polars.DataFrame -> polars.DataFrame with dup_group + is_representative."""
    import numpy as np
    import polars as pl

    n = df.height
    if column not in df.columns:
        return df  # unconfigured → passthrough (the column-reference warning flags a bad column on the card)
    if n == 0:  # correctly configured but empty → still emit the columns so downstream filter() finds them
        return df.with_columns(
            pl.Series("dup_group", [], dtype=pl.Int64),
            pl.Series("is_representative", [], dtype=pl.Boolean),
        )
    try:
        vecs = np.asarray(df[column].to_list(), dtype="float64")
    except (ValueError, TypeError):
        return df  # ragged / variable-length / non-numeric list column → can't compute, passthrough
    if vecs.ndim != 2 or vecs.shape[1] == 0:
        return df  # not a fixed-width vector column → passthrough rather than guess

    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vecs / norms
    cut = 1.0 - float(threshold)  # cosine SIMILARITY at/above cut ⇒ within `threshold` distance ⇒ duplicate

    group = np.full(n, -1, dtype="int64")
    for i in range(n):
        if group[i] != -1:
            continue
        group[i] = i  # row i leads its own cluster
        sims = unit @ unit[i]  # (n,) similarities to row i — O(n·d) memory, never the full O(n²) matrix
        for j in np.nonzero(sims >= cut)[0]:
            if group[j] == -1:
                group[j] = i  # only claim rows not already led by an earlier cluster (greedy, stable)

    reps = (group == np.arange(n))
    return df.with_columns(
        pl.Series("dup_group", group.tolist()),
        pl.Series("is_representative", reps.tolist()),
    )


def build(engine, node, inputs):
    cfg = _cfg(node)
    column = (cfg.get("column") or "embedding").strip()
    try:
        threshold = float(cfg.get("threshold", 0.05))
    except (TypeError, ValueError):
        threshold = 0.05
    return ctx.polars(inputs[0], lambda df: _dedup(df, column, threshold))


def register(reg) -> None:
    # Global grouping has no clean per-row/per-batch emit, so there's no engine-neutral `ir` hook: the node
    # stays a DuckDB build() and falls back to DuckDB on a distributed backend (contrast dp_upper's map hook).
    reg.add_node(SPEC, build)
