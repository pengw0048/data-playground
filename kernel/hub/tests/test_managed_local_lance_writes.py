"""Contract coverage for expected-head managed local Lance append writes."""

from __future__ import annotations

import os
import pathlib
import threading
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from hub import metadb
from hub.local_writes import write_managed_local_lance_append
from hub.models import (
    ExactDatasetRef,
    LineagePublication,
    WriteDestination,
    WriteIntent,
    WriteProvenance,
)
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'managed-local-lance.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.fixture
def lance_destination(tmp_path):
    lance = pytest.importorskip("lance")
    name = f"typed-lance-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    catalog = InMemoryCatalog(str(tmp_path / "catalog"), lambda value: (
        LanceAdapter() if str(value).lower().rstrip("/").endswith(".lance")
        else DuckDBAdapter()))
    table = catalog._add(name=name, uri=uri, strict_probe=True)
    binding = metadb.catalog_revision_binding_for_uri(uri)
    assert binding is not None
    return lance, catalog, table, binding


def _intent(table, binding: dict, key: str, *, revision_id: str | None = None) -> WriteIntent:
    head = revision_id or LanceAdapter().resolve_revision(table.uri)["revision_id"]
    return WriteIntent(
        destination=WriteDestination(
            logical_uri=table.uri,
            name=table.name,
            dataset_id=binding["dataset_id"],
            provider="managed-local-lance",
        ),
        mode="append",
        expected_schema=LanceAdapter().revision_detail(
            table.uri, head, preview_limit=1)["columns"],
        expected_head=ExactDatasetRef(
            kind="exact", dataset_id=binding["dataset_id"], revision_id=head),
        idempotency_key=key,
        provenance=WriteProvenance(publication=LineagePublication(
            idempotency_key=key, provenance="manual")),
    )


def test_lance_append_receipt_reopens_exact_version_after_head_move_and_restart(
        lance_destination, tmp_path):
    lance, _catalog, table, binding = lance_destination
    intent = _intent(table, binding, "lance-append")
    receipt = write_managed_local_lance_append(
        intent=intent, data=pa.table({"value": [2, 3]}))

    assert receipt.parent_head == intent.expected_head
    assert receipt.revision_id == "2"
    assert receipt.publication.provider == "managed-local-lance"
    assert receipt.publication.publish_sequence == 2
    assert receipt.publication.backend_version == lance.__version__
    assert receipt.rows == 3 and receipt.bytes > 0
    assert LanceAdapter().open_revision(table.uri, receipt.revision_id).fetchall() == [
        (1,), (2,), (3,)]

    lance.write_dataset(pa.table({"value": [4]}), table.uri, mode="append")
    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == "3"
    assert LanceAdapter().open_revision(table.uri, receipt.revision_id).fetchall() == [
        (1,), (2,), (3,)]

    restarted = InMemoryCatalog(str(tmp_path / "restarted"), lambda _uri: LanceAdapter())
    recovered = write_managed_local_lance_append(
        intent=intent, data=pa.table({"value": [999]}))
    assert recovered == receipt
    assert restarted.get_table(table.uri).id == table.id


def test_lance_append_rejects_invalid_destinations_and_schema_before_fragment_allocation(
        lance_destination, tmp_path, monkeypatch):
    lance, catalog, table, binding = lance_destination
    from lance.fragment import LanceFragment

    allocations = 0
    create = LanceFragment.create

    def track_create(*args, **kwargs):
        nonlocal allocations
        allocations += 1
        return create(*args, **kwargs)

    monkeypatch.setattr(LanceFragment, "create", track_create)
    missing_uri = str(tmp_path / "missing.lance")
    missing = _intent(table, binding, "missing").model_copy(deep=True)
    missing.destination.logical_uri = missing_uri
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="does not exist"):
        write_managed_local_lance_append(
            intent=missing, data=pa.table({"value": [2]}))

    parquet_uri = str(tmp_path / "registered.parquet")
    pq.write_table(pa.table({"value": [1]}), parquet_uri)
    parquet = catalog._add(name="registered-parquet", uri=parquet_uri, strict_probe=True)
    parquet_binding = metadb.catalog_revision_binding_for_uri(parquet_uri)
    assert parquet_binding is not None
    with pytest.raises(ValueError, match=".lance destination"):
        WriteIntent(
            destination=WriteDestination(
                logical_uri=parquet.uri,
                name=parquet.name,
                dataset_id=parquet_binding["dataset_id"],
                provider="managed-local-lance",
            ),
            mode="append",
            expected_schema=parquet.columns,
            expected_head=ExactDatasetRef(
                kind="exact", dataset_id=parquet_binding["dataset_id"], revision_id="1"),
            idempotency_key="non-lance",
            provenance=WriteProvenance(publication=LineagePublication(
                idempotency_key="non-lance", provenance="manual")),
        )

    incompatible = _intent(table, binding, "incompatible").model_copy(deep=True)
    incompatible.expected_schema[0].name = "other"
    with pytest.raises(ValueError, match="destination schema"):
        write_managed_local_lance_append(
            intent=incompatible, data=pa.table({"other": [2]}))

    with pytest.raises(ValueError, match="provider-compatible"):
        write_managed_local_lance_append(
            intent=_intent(table, binding, "physical-incompatible"),
            data=pa.table({"value": pa.array([2], type=pa.int32())}),
        )

    stale = _intent(table, binding, "stale")
    lance.write_dataset(pa.table({"value": [2]}), table.uri, mode="append")
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="stale"):
        write_managed_local_lance_append(
            intent=stale, data=pa.table({"value": [3]}))
    assert allocations == 0


def test_two_lance_appends_admitted_from_one_head_have_one_cas_winner(lance_destination):
    _lance, _catalog, table, binding = lance_destination
    head = LanceAdapter().resolve_revision(table.uri)["revision_id"]
    receipts = []
    errors = []

    def append(key: str, value: int) -> None:
        try:
            receipts.append(write_managed_local_lance_append(
                intent=_intent(table, binding, key, revision_id=head),
                data=pa.table({"value": [value]}),
            ))
        except Exception as exc:  # noqa: BLE001 - assert the exact typed conflict below
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=("concurrent-a", 2)),
        threading.Thread(target=append, args=("concurrent-b", 3)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert len(receipts) == 1
    assert len(errors) == 1 and isinstance(errors[0], metadb.ManagedLocalWriteConflict)
    assert LanceAdapter()._dataset(table.uri).count_rows() == 2
    with metadb.session() as session:
        assert len(list(session.scalars(select(
            metadb.ManagedLocalLanceWriteReceipt)))) == 1


def test_lance_append_response_loss_replays_without_duplicate_version(
        lance_destination, monkeypatch):
    lance, _catalog, table, binding = lance_destination
    intent = _intent(table, binding, "response-loss")
    commit = lance.LanceDataset.commit

    def commit_then_lose_response(*args, **kwargs):
        commit(*args, **kwargs)
        raise OSError("append response lost")

    monkeypatch.setattr(lance.LanceDataset, "commit", commit_then_lose_response)
    receipt = write_managed_local_lance_append(
        intent=intent, data=pa.table({"value": [2]}))
    monkeypatch.setattr(lance.LanceDataset, "commit", commit)
    replayed = write_managed_local_lance_append(
        intent=intent, data=pa.table({"value": [999]}))

    assert replayed == receipt
    assert LanceAdapter()._dataset(table.uri).version == 2
    assert LanceAdapter()._dataset(table.uri).to_table().to_pylist() == [
        {"value": 1}, {"value": 2}]


def test_lance_append_precommit_failure_cleans_fragment_and_preserves_head(
        lance_destination):
    _lance, _catalog, table, binding = lance_destination
    intent = _intent(table, binding, "precommit-failure")
    data_dir = pathlib.Path(table.uri) / "data"
    before = {path.name for path in data_dir.iterdir()}

    def fail_before_commit() -> None:
        raise RuntimeError("fail before Lance commit")

    with pytest.raises(RuntimeError, match="fail before Lance commit"):
        write_managed_local_lance_append(
            intent=intent,
            data=pa.table({"value": [2]}),
            before_publish=fail_before_commit,
        )

    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == "1"
    assert LanceAdapter()._dataset(table.uri).to_table().to_pylist() == [{"value": 1}]
    assert {path.name for path in data_dir.iterdir()} == before


def test_lance_append_detects_head_change_at_publication_boundary_and_cleans_fragment(
        lance_destination):
    lance, _catalog, table, binding = lance_destination
    intent = _intent(table, binding, "boundary-stale")
    data_dir = pathlib.Path(table.uri) / "data"
    transaction_dir = pathlib.Path(table.uri) / "_transactions"
    before = {path.name for path in data_dir.iterdir()}
    before_transactions = {path.name for path in transaction_dir.iterdir()}

    def move_head() -> None:
        lance.write_dataset(pa.table({"value": [9]}), table.uri, mode="append")

    with pytest.raises(metadb.ManagedLocalWriteConflict, match="publication"):
        write_managed_local_lance_append(
            intent=intent,
            data=pa.table({"value": [2]}),
            before_publish=move_head,
        )

    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == "2"
    assert LanceAdapter()._dataset(table.uri).to_table().to_pylist() == [
        {"value": 1}, {"value": 9}]
    assert len({path.name for path in data_dir.iterdir()} - before) == 1
    assert len({path.name for path in transaction_dir.iterdir()} - before_transactions) == 1
