"""Reference prepared node for one bounded exact-Source native-row read."""

from hub.sdk import (
    ExactSourceRowRestriction,
    NodePreparation,
    NodeSpec,
    ParamSpec,
    PortSpec,
    UnsupportedUpstreamError,
)


SPEC = NodeSpec(
    kind="exact-row-fixture",
    title="exact row fixture",
    category="compute",
    inputs=[PortSpec(id="in", wire="dataset")],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[ParamSpec(name="nativeRowIds", type="string", default="")],
    previewable=False,
    blurb="reference lifecycle for a bounded exact-Source native-row read",
)


def prepare(params, immediate_inputs):
    upstream = immediate_inputs.port("in")
    if upstream.count != 1 or upstream.inputs[0].kind != "source":
        raise UnsupportedUpstreamError(
            "exact row fixture requires one directly wired Source")
    dataset = upstream.inputs[0].dataset
    if dataset is None or dataset.revision_id is None:
        raise UnsupportedUpstreamError(
            "exact row fixture requires one admitted exact Source")
    text = str(params["nativeRowIds"] or "").strip()
    row_ids = tuple(int(value.strip()) for value in text.split(",") if value.strip())
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("exact row fixture requires deduplicated native row ids")
    return NodePreparation(
        state={"requested": row_ids},
        restriction=ExactSourceRowRestriction(
            input_port="in", native_row_ids=row_ids),
    )


def build(_engine, _node, inputs, prepared_state):
    if prepared_state is None or "requested" not in prepared_state:
        raise RuntimeError("exact row fixture requires full-pass prepared state")
    return inputs[0]


def register(reg) -> None:
    reg.add_node(SPEC, build, prepare=prepare)
