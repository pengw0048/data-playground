"""Local full-run admission binds Sources to one immutable provider revision."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pyarrow as pa
import pytest

from hub import db, metadb
from hub.models import Graph
from hub.plugins.adapters import LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import runs


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, session, url = metadb._engine, metadb._Session, settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'admission.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine, metadb._Session = engine, session


def _graph(uri: str) -> Graph:
    return Graph.model_validate({
        "id": "local-admission", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }], "edges": [],
    })


def test_manifest_is_ordered_secret_free_and_reopens_the_original_lance_head(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "input.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="input", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _graph(uri)

    manifest = runs._resolve_local_run_manifest(graph, "source", deps)
    assert list(manifest[0]) == ["node_id", "dataset_id", "revision_id", "provider", "resolved_at"]
    assert "uri" not in manifest[0] and "secret" not in str(manifest[0]).lower()
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=str(uuid.uuid4()), target_node_id="source",
        intent_sha256="a" * 64, manifest=manifest,
    )
    assert created is True

    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    bound = runs._bind_local_run_manifest(graph, metadb.local_run_input_manifest(run_id) or [], deps)
    cfg = bound.nodes[0].data["config"]
    assert cfg["_input_revision_id"] == manifest[0]["revision_id"]
    with db.run_scope():
        assert LanceAdapter().open_revision(cfg["uri"], cfg["_input_revision_id"]).fetchall() == [(1,)]


def test_same_submission_adopts_its_original_manifest_after_the_lance_head_moves(tmp_path):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / "retry.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: LanceAdapter())
    catalog._add(name="retry", uri=uri, strict_probe=True)
    deps = SimpleNamespace(resolve_adapter=lambda _uri: LanceAdapter())
    graph = _graph(uri)
    submission = str(uuid.uuid4())
    first = runs._resolve_local_run_manifest(graph, "source", deps)
    run_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=submission, target_node_id="source",
        intent_sha256="b" * 64, manifest=first,
    )
    assert created is True
    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    moved = runs._resolve_local_run_manifest(graph, "source", deps)
    adopted_id, created = metadb.admit_local_run_inputs(
        uid="local", canvas_id=None, submission_id=submission, target_node_id="source",
        intent_sha256="b" * 64, manifest=moved,
    )
    assert (adopted_id, created) == (run_id, False)
    assert metadb.local_run_input_manifest(run_id) == first


def test_manifest_rejects_secret_or_noncanonical_fields():
    with pytest.raises(ValueError, match="manifest is invalid"):
        metadb.admit_local_run_inputs(
            uid="local", canvas_id=None, submission_id=str(uuid.uuid4()), target_node_id="source",
            intent_sha256="c" * 64,
            manifest=[{"node_id": "source", "dataset_id": "dataset", "revision_id": "1",
                       "provider": "lance", "resolved_at": "now", "secret": "nope"}],
        )
