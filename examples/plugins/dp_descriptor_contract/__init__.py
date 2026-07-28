"""Installed fixture for the public NodeSpec and PortSpec descriptor contract."""

from __future__ import annotations

import json
import os
import datetime
from importlib.resources import files

from hub import db
from hub.plugins.adapters import DuckDBAdapter
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


class _E2EFullRunOnlyAdapter:
    """Remote-shaped exact fixture whose source cannot promise a bounded interactive read."""

    name = "e2e-full-run-only"
    _revision = "fixture-v1"

    def matches(self, uri: str) -> bool:
        return uri == "e2e-full-run-only://events"

    @staticmethod
    def _source() -> str:
        return os.path.join(os.getcwd(), "data", "movies.csv")

    def scan(self, _uri, columns=None, predicate=None, limit=None, options=None):
        return DuckDBAdapter().scan(
            self._source(), columns=columns, predicate=predicate, limit=limit, options=options)

    def schema(self, _uri):
        return DuckDBAdapter().schema(self._source())

    def count(self, _uri):
        return DuckDBAdapter().count(self._source())

    def fingerprint(self, _uri):
        return self._revision

    def write(self, *_args, **_kwargs):
        raise RuntimeError("the E2E full-run-only fixture is read-only")

    def revision_history(self, _uri, *, limit, cursor=None):
        item = {"revision_id": self._revision, "committed_at": datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc)}
        return ([item] if limit > 0 and cursor is None else []), None

    def resolve_revision(self, _uri, *, as_of=None):
        del as_of
        return {"revision_id": self._revision}

    def open_revision(self, _uri, revision_id):
        if revision_id != self._revision:
            raise FileNotFoundError("revision unavailable")
        return self.scan(_uri)

    def revision_detail(self, _uri, revision_id, *, preview_limit):
        del preview_limit
        if revision_id != self._revision:
            raise FileNotFoundError("revision unavailable")
        import pyarrow as pa

        return {
            "revision_id": self._revision,
            "committed_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            "columns": [],
            "row_count": None,
            "data_file_count": None,
            "total_bytes": None,
            "fragment_count": None,
            "preview_table": pa.table({}),
        }


def register(reg) -> None:
    reg.add_adapter(_E2EFullRunOnlyAdapter())
    reg.add_node(SPECS[0], _build)
    reg.add_node(SPECS[1], _must_not_execute)
