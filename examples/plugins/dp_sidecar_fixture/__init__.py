"""Neutral sidecar fixture plugin used by the managed-column-merge conformance journey.

The node intentionally emits only its configured identity and one derived payload column. It never
selects a destination, requests a merge, or receives publication authority: a researcher publishes
its ordinary output as a managed-local sidecar, then the core-owned Write admission may merge that
exact revision into a separately chosen base.
"""

from hub.sdk import NodeSpec, ParamSpec, PortSpec, UnsupportedUpstreamError, ctx
from hub.sqlpolicy import identifier, quote_identifier, validate_identifier_alias


SPEC = NodeSpec(
    kind="derive_sidecar_column", title="derive sidecar column", category="compute", tag="sidecar",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[
        ParamSpec(name="identity", type="string", label="logical identity column"),
        ParamSpec(name="value", type="string", label="numeric source column"),
        ParamSpec(name="output", type="string", label="derived payload column"),
        ParamSpec(name="sourceDatasetId", type="string",
                  label="required canonical source dataset id (optional)"),
    ],
    blurb="retain one logical identity and emit one neutral derived sidecar column",
)


def build(engine, node, inputs):
    upstream = ctx.immediate_inputs(engine, node).port("in")
    if upstream.count != 1:
        raise UnsupportedUpstreamError("sidecar fixture requires exactly one immediate input")
    if upstream.inputs[0].kind != "source":
        raise UnsupportedUpstreamError("sidecar fixture requires a direct Source input")
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    expected_dataset_id = str(cfg.get("sourceDatasetId") or "").strip()
    if expected_dataset_id:
        dataset = upstream.inputs[0].dataset
        if dataset is None:
            raise UnsupportedUpstreamError(
                "sidecar fixture requires a direct Source with a proved dataset identity")
        if dataset.dataset_id != expected_dataset_id:
            raise UnsupportedUpstreamError(
                "sidecar fixture input does not match the required source dataset identity")
    identity = str(cfg.get("identity") or "").strip()
    value = str(cfg.get("value") or "").strip()
    output = str(cfg.get("output") or "").strip()
    if not identity or not value or not output:
        raise ValueError("sidecar fixture requires identity, value, and output columns")
    key = quote_identifier(identifier(identity, inputs[0].columns, label="sidecar identity"))
    source = quote_identifier(identifier(value, inputs[0].columns, label="sidecar source"))
    target = quote_identifier(validate_identifier_alias(output, label="sidecar output"))
    return ctx.sql(inputs[0], f"SELECT {key}, CAST({source} AS DOUBLE) * 2 AS {target} FROM input")


def register(reg) -> None:
    reg.add_node(SPEC, build)
