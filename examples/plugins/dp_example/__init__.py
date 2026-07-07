"""Example Data Playground plugin — a `redact` node. See docs/PLUGINS.md.

Drop this folder into `<workspace>/plugins/` (or add it via `DP_PLUGINS`), restart, and a `redact`
node appears on the canvas — typed, wired, and previewable — with **no change to the core**. That is
the whole point of the plugin SPI: a stranger's node shows up first-class.

`redact` masks a (PII-ish) text column, keeping only the first N characters and replacing the rest
with `*`. It builds plain SQL via `ctx.sql`, so it pushes down and runs out-of-core like any
built-in relational node.
"""

from kernel.sdk import NodeSpec, ParamSpec, PortSpec, ctx

SPEC = NodeSpec(
    kind="redact",
    title="redact",
    category="compute",
    tag="redact",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[
        ParamSpec(name="column", type="string", label="column to redact"),
        ParamSpec(name="keep", type="int", default=0, label="keep first N chars (rest → *)"),
    ],
    blurb="mask a text column (PII) — keep the first N chars, replace the rest with *",
)


def build(engine, node, inputs):
    """Contribute one step to the logical plan: SELECT * with the target column replaced by a mask.

    `inputs[0]` is the upstream relation. `ctx.sql` runs SQL over it, referencing it as the
    placeholder token `{input}`, and returns a lazy relation (no materialization).
    """
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    col = (cfg.get("column") or "").strip()
    if not col:
        return inputs[0]  # nothing configured yet → passthrough (keeps the node valid/previewable)
    keep = int(cfg.get("keep") or 0)
    s = f'CAST("{col}" AS VARCHAR)'
    # first `keep` chars kept; the remainder becomes that many '*' (length-preserving-ish mask)
    masked = f"left({s}, {keep}) || repeat('*', greatest(length({s}) - {keep}, 0))"
    return ctx.sql(inputs[0], f'SELECT * REPLACE ({masked} AS "{col}") FROM {{input}}')


def register(reg):
    """Discovery entry point — the kernel calls this once with the plugin Registry."""
    reg.add_node(SPEC, build)
