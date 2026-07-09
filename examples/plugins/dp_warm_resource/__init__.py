"""Reference plugin — a `warm-map` node that reuses an expensive-to-construct handle across batches/runs.

Shows the `ctx.resource` seam. A node that needs a costly object — a loaded model, a media decoder, a DB
connection pool, a GPU context — builds it ONCE via `ctx.resource(key, factory)` and reuses it for every
batch and every subsequent run on the same warm per-canvas kernel, instead of paying the construction cost
per batch (the exact fragility distributed media/inference pipelines hit). Generic: the handle is any
stateful costly resource, not model-specific.

Here the "model" is a stand-in whose construction we can observe (it counts how many rows it has seen), so
a test can prove it's built once and reused across two runs. A real plugin would load an actual model in
the factory. Drop this folder into `<workspace>/plugins/`.
"""

from __future__ import annotations

from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx

SPEC = NodeSpec(
    kind="warm-map", title="warm map", category="compute", tag="warm",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[ParamSpec(name="column", type="string", label="text column to normalize")],
    blurb="normalize a column with a warm, load-once resource (ctx.resource) reused across batches + runs",
)

_RESOURCE_KEY = "dp_warm_resource:model"  # namespaced so it can't collide with another plugin's resource


class _Model:
    """Stands in for an EXPENSIVE-to-construct handle (a loaded model / decoder). Construction here is
    cheap, but `calls` lets a test see that ONE instance is reused across batches + runs (never rebuilt)."""

    def __init__(self) -> None:
        self.calls = 0

    def apply(self, s) -> str:
        self.calls += 1
        return (str(s) if s is not None else "").strip().lower()


def _column(node) -> str:
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    return (cfg.get("column") or "").strip()


def build(engine, node, inputs):
    col = _column(node)
    if not col:
        return inputs[0]  # unconfigured → passthrough
    model = ctx.resource(_RESOURCE_KEY, _Model)  # built once; warm across every batch AND every run

    def fn(batch):
        rows = batch.to_pylist()
        for r in rows:
            r[col] = model.apply(r.get(col))
        return rows

    return ctx.arrow_map(inputs[0], fn)


def register(reg) -> None:
    reg.add_node(SPEC, build)
