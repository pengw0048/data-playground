"""Installed fixture for the public NodeSpec and PortSpec descriptor contract."""

from __future__ import annotations

import json
from importlib.resources import files

from hub import db
from hub.sdk import NodeSpec, identifier, quote_identifier


DESCRIPTORS = json.loads(files(__package__).joinpath("descriptor.json").read_text(encoding="utf-8"))
SPECS = [NodeSpec.model_validate(descriptor) for descriptor in DESCRIPTORS]


def _build(_engine, node, inputs):
    config = node.data["config"]
    selected = config["columns"]
    columns = ", ".join(
        quote_identifier(identifier(column, inputs[0].columns, label="descriptor contract column"))
        for column in selected
    )
    queries: list[str] = []
    for input_order, relation in enumerate(inputs):
        view = db.unique_view("descriptor_contract")
        relation.create_view(view, replace=True)
        queries.append(
            f"SELECT {columns}, {input_order}::INTEGER AS input_order, "
            f"{config['count']}::BIGINT AS configured_count, "
            f"{config.get('ratio', 0.5)!r}::DOUBLE AS configured_ratio "
            f"FROM {quote_identifier(view)}"
        )
    return db.conn().sql(" UNION ALL ".join(queries))


def _must_not_execute(*_args):
    raise AssertionError("an unavailable descriptor contract node must not execute")


def register(reg) -> None:
    reg.add_node(SPECS[0], _build)
    reg.add_node(SPECS[1], _must_not_execute)
