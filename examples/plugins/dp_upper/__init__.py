"""Reference plugin — a node that runs on a DISTRIBUTED backend too, not just DuckDB.

Shows the engine-neutral emit path: `reg.add_node(spec, build, ir=…)`. The `upper` node uppercases a
text column — a pure per-row map. Its DuckDB `build()` and its `ir` hook run the SAME generated operator
code, so a distributed backend (the `dp_ray` runner) gets a clean `map` op with inlined code — NOT the
`opaque` fallback — and produces byte-identical results. (Contrast `dp_example`'s `redact`, which is
SQL-based and has no `ir` hook, so it stays DuckDB-only — that's fine, it just can't run on Ray.)

The plugin GUARANTEES its two expressions agree by generating the operator code once (`_code`) and using
it in both paths — the same discipline the built-in `transform` follows (one operator, run locally by the
engine and remotely by Ray). Drop this folder into `<workspace>/plugins/`.
"""

from __future__ import annotations

from hub import sandbox
from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx

SPEC = NodeSpec(
    kind="upper", title="uppercase", category="compute", tag="upper",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[ParamSpec(name="column", type="string", label="text column to uppercase")],
    blurb="uppercase a text column — a per-row map that also runs on a distributed backend",
)


def _column(node) -> str:
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    return (cfg.get("column") or "").strip()


def _code(node) -> str:
    # generated ONCE and run by BOTH build() (DuckDB) and node_ir() (a distributed backend) → identical
    col = _column(node)
    return f"def fn(row):\n    row[{col!r}] = str(row.get({col!r})).upper()\n    return row"


def build(engine, node, inputs):
    if not _column(node):
        return inputs[0]  # not configured → passthrough
    fn = sandbox.compile_operator(_code(node), "map")
    return ctx.arrow_map(inputs[0], lambda batch: [fn(dict(r)) for r in batch.to_pylist()])


def node_ir(node):
    """Engine-neutral emit: a clean `map` op with the SAME code build() runs, so dp_ray can execute it."""
    if not _column(node):
        return None  # unconfigured → opaque (falls back to DuckDB, which passes through)
    return {"op": "map", "config": {"mode": "map", "code": _code(node), "onError": "raise"}}


def register(reg) -> None:
    reg.add_node(SPEC, build, ir=node_ir)  # the `ir` hook is what lets it run distributed, not just in DuckDB
