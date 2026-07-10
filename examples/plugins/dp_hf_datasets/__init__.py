"""Reference plugin — a **Hugging Face datasets** source adapter.

Read any dataset on the HF Hub as a Data Playground source: point a `source` node at
`hf://<dataset_id>[@<config>][:<split>]` (e.g. `hf://stanfordnlp/imdb:test`, `hf://glue@mrpc:train`;
split defaults to `train`). The dataset is loaded via the `datasets` library, handed to DuckDB as an
Arrow table, and flows through the graph like any other dataset — preview, filter, transform, join, etc.

It demonstrates the `DatasetAdapter` seam (`reg.add_adapter`): `matches` claims the `hf://` scheme, `scan`
returns a LAZY DuckDB relation (with column/limit pushdown), `schema`/`count`/`fingerprint` round it out.
It's read-only (`write` raises) — HF datasets are sources. `datasets` is imported lazily (like the built-in
Lance adapter), so the plugin loads fine without it installed and only errors with a clear message when a
`hf://` uri is actually used.

Install: `uv pip install -e 'kernel[hf]'` (adds the `datasets` optional extra). Drop this folder into
`<workspace>/plugins/` or install it as a `dataplay.plugins` entry point.
"""

from __future__ import annotations

import hashlib

from hub import db
from hub.plugins.adapters import Relation, relation_columns


def _parse(uri: str) -> tuple[str, str | None, str]:
    """hf://<dataset_id>[@<config>][:<split>] → (dataset_id, config|None, split). The id keeps its '/'
    (org/name); '@' picks a config (avoids the org/name-vs-config ambiguity); ':' picks a split."""
    rest = uri[len("hf://"):]
    path, _, split = rest.partition(":")
    name, _, config = path.partition("@")
    return name, (config or None), (split or "train")


def _load_arrow(uri: str):
    try:
        from datasets import load_dataset  # lazy — only when a hf:// uri is used
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError("Hugging Face support is not installed — run: uv pip install -e 'kernel[hf]'") from e
    name, config, split = _parse(uri)
    ds = load_dataset(name, config, split=split)
    return ds.with_format("arrow")[:]  # the whole split as a pyarrow.Table


class HfDatasetsAdapter:
    name = "hf-datasets"

    def matches(self, uri: str) -> bool:
        return uri.startswith("hf://")

    def scan(self, uri: str, columns: list[str] | None = None, predicate: str | None = None,
             limit: int | None = None, options: dict | None = None) -> Relation:
        rel = db.conn().from_arrow(_load_arrow(uri))
        if columns:
            rel = rel.project(", ".join(f'"{c}"' for c in columns))
        if predicate:
            rel = rel.filter(predicate)
        if limit is not None:
            rel = rel.limit(int(limit))
        return rel

    def schema(self, uri: str):
        with db.base_guard():
            return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            with db.base_guard():
                return int(self.scan(uri).aggregate("count(*) AS n").fetchone()[0])
        except Exception:  # noqa: BLE001
            return None

    def fingerprint(self, uri: str) -> str:
        return "hf:" + hashlib.sha256(uri.encode()).hexdigest()[:12]  # can't cheaply stat the hub; key by uri

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
        raise NotImplementedError("hf:// datasets are read-only sources; write to a local/object-store uri instead")


def register(reg) -> None:
    reg.add_adapter(HfDatasetsAdapter())  # claims hf:// — safe to register even without `datasets` (lazy)
