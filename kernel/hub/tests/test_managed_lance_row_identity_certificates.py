"""Private exact-Lance row identity fence and certificate contracts."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import func, select

from hub import metadb
from hub.models import ExactDatasetRef
from hub.plugins import adapters as adapter_module
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import (
    RowIdentityRevisionMismatch,
    RowIdentityUnavailable,
    RowIdentityValueTooLarge,
    certify_and_commit_managed_local_lance_row_identity,
    certify_and_persist_managed_local_lance_row_identity,
    managed_local_lance_row_identity_certificate,
)


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    old_url, old_engine, old_session = settings.database_url, metadb._engine, metadb._Session
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'lance-row-identity.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = old_url, old_engine, old_session


def _registered_lance(tmp_path, table: pa.Table):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / f"registered-{uuid.uuid4().hex}.lance")
    lance.write_dataset(table, uri)
    catalog = InMemoryCatalog(str(tmp_path / "catalog"), lambda value: (
        LanceAdapter() if str(value).lower().rstrip("/").endswith(".lance")
        else DuckDBAdapter()))
    registered = catalog._add(name="registered-lance", uri=uri, strict_probe=True)
    binding = metadb.catalog_revision_binding_for_uri(uri)
    assert binding is not None
    revision = LanceAdapter().resolve_revision(uri)["revision_id"]
    return lance, catalog, registered, binding, ExactDatasetRef(
        kind="exact", dataset_id=binding["dataset_id"], revision_id=revision)


def _exact_local_tracked_file(lance, uri: str, revision_id: str, member_type: str) -> Path:
    rows = lance.dataset(uri, version=int(revision_id)).tracked_files().read_all().to_pylist()
    matches = [
        row for row in rows
        if row["version"] == int(revision_id) and row["type"] == member_type
    ]
    assert len(matches) == 1
    return Path(str(matches[0]["base_uri"])) / str(matches[0]["path"])


def test_lance_certificate_replays_exactly_and_persists_no_raw_source_data(tmp_path):
    _lance, _catalog, _registered, binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
        "label": pa.array(["literal-key-one", "literal-key-two"], type=pa.string()),
    }))

    first = certify_and_persist_managed_local_lance_row_identity(exact, ["id", "label"])
    second = certify_and_persist_managed_local_lance_row_identity(exact, ["id", "label"])
    loaded = managed_local_lance_row_identity_certificate(exact, ["id", "label"])

    assert first == second
    assert loaded is not None and loaded.status == "complete"
    with metadb.session() as s:
        fence = s.get(metadb.ManagedLocalLanceRowIdentityFence, {
            "registration_id": binding["dataset_id"], "revision_id": exact.revision_id,
        })
        certificate = s.get(metadb.ManagedLocalLanceRowIdentityCertificate, {
            "registration_id": binding["dataset_id"], "revision_id": exact.revision_id,
        })
        assert fence is not None and certificate is not None
        stored = "|".join((
            fence.physical_incarnation_sha256, fence.schema_sha256,
            fence.row_identity_spec_sha256, certificate.certificate_doc,
            certificate.certificate_sha256,
        ))
    assert "literal-key-one" not in stored
    assert "literal-key-two" not in stored
    assert str(_registered.uri) not in stored
    assert "_rowid" not in stored


def test_lance_certificate_rejects_a_conflicting_spec_for_the_same_exact_revision(tmp_path):
    _lance, _catalog, _registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
        "label": pa.array(["one", "two"], type=pa.string()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id", "label"])

    with pytest.raises(metadb.ManagedLocalLanceRowIdentityCertificateConflict):
        certify_and_persist_managed_local_lance_row_identity(exact, ["id"])


def test_lance_certificate_does_not_commit_after_its_exact_fence_changes(tmp_path, monkeypatch):
    _lance, _catalog, _registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    original = LanceAdapter.exact_revision_incarnation
    calls = 0

    def changed_after_scan(adapter, uri, revision_id):
        nonlocal calls
        schema, digest = original(adapter, uri, revision_id)
        calls += 1
        return schema, digest if calls == 1 else "0" * 64

    monkeypatch.setattr(LanceAdapter, "exact_revision_incarnation", changed_after_scan)
    committed = False

    def record_commit(*_args):
        nonlocal committed
        committed = True

    with pytest.raises(RowIdentityRevisionMismatch):
        certify_and_commit_managed_local_lance_row_identity(
            exact, ["id"], commit=record_commit)
    assert calls == 2
    assert committed is False


def test_lance_certificate_load_revalidates_the_fence_without_a_row_scan(tmp_path, monkeypatch):
    _lance, _catalog, _registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    original = LanceAdapter.exact_revision_incarnation

    def changed_incarnation(adapter, uri, revision_id):
        schema, _digest = original(adapter, uri, revision_id)
        return schema, "0" * 64

    def must_not_scan(*_args, **_kwargs):
        raise AssertionError("certificate loading must not scan Lance rows")

    monkeypatch.setattr(LanceAdapter, "exact_revision_incarnation", changed_incarnation)
    monkeypatch.setattr(LanceAdapter, "open_revision_projection", must_not_scan)
    assert managed_local_lance_row_identity_certificate(exact, ["id"]) is None


def test_lance_certificate_rejects_a_missing_tracked_data_file(tmp_path):
    lance, _catalog, registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    _exact_local_tracked_file(lance, registered.uri, exact.revision_id, "data file").unlink()

    with pytest.raises(Exception):
        lance.dataset(registered.uri, version=int(exact.revision_id)).to_table()
    with pytest.raises(RowIdentityUnavailable):
        managed_local_lance_row_identity_certificate(exact, ["id"])


def test_lance_certificate_rejects_a_same_path_data_file_rewrite(tmp_path):
    lance, _catalog, registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    replacement_uri = str(tmp_path / f"replacement-{uuid.uuid4().hex}.lance")
    lance.write_dataset(pa.table({"id": pa.array([9, 10], type=pa.int64())}), replacement_uri)
    target = _exact_local_tracked_file(lance, registered.uri, exact.revision_id, "data file")
    replacement = _exact_local_tracked_file(lance, replacement_uri, "1", "data file")
    shutil.copyfile(replacement, target)

    assert lance.dataset(registered.uri, version=int(exact.revision_id)).to_table()["id"].to_pylist() == [9, 10]
    assert managed_local_lance_row_identity_certificate(exact, ["id"]) is None


def test_lance_certificate_rejects_tracked_evidence_over_its_object_bound(tmp_path, monkeypatch):
    _lance, _catalog, _registered, binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    monkeypatch.setattr(adapter_module, "_LANCE_TRACKED_EVIDENCE_MAX_ROWS", 2)

    with pytest.raises(RowIdentityUnavailable):
        certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityFence).where(
                metadb.ManagedLocalLanceRowIdentityFence.registration_id
                == binding["dataset_id"])) == 0


def test_lance_certificate_keeps_retained_old_exact_revision_after_append(tmp_path):
    lance, _catalog, registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])

    lance.write_dataset(pa.table({"id": pa.array([3], type=pa.int64())}), registered.uri,
                        mode="append")
    newer = ExactDatasetRef(
        kind="exact", dataset_id=exact.dataset_id,
        revision_id=LanceAdapter().resolve_revision(registered.uri)["revision_id"])

    assert newer.revision_id != exact.revision_id
    assert managed_local_lance_row_identity_certificate(exact, ["id"]) is not None
    assert managed_local_lance_row_identity_certificate(newer, ["id"]) is None


def test_lance_certificate_keeps_retained_old_exact_revision_after_compaction(tmp_path):
    lance, _catalog, registered, _binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    for value in (2, 3, 4):
        lance.write_dataset(pa.table({"id": pa.array([value], type=pa.int64())}),
                            registered.uri, mode="append")
    metrics = lance.dataset(registered.uri).optimize.compact_files(
        target_rows_per_fragment=100)
    assert metrics.fragments_removed > 0
    newer = ExactDatasetRef(
        kind="exact", dataset_id=exact.dataset_id,
        revision_id=LanceAdapter().resolve_revision(registered.uri)["revision_id"])

    assert newer.revision_id != exact.revision_id
    assert managed_local_lance_row_identity_certificate(exact, ["id"]) is not None
    assert managed_local_lance_row_identity_certificate(newer, ["id"]) is None


def test_lance_certificate_registration_aba_cascades_and_cannot_retarget(tmp_path):
    _lance, catalog, registered, binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array([1, 2], type=pa.int64()),
    }))
    certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    assert metadb.catalog_delete_entry(registered.uri, report_result=True) is True
    catalog._add(name="registered-again", uri=registered.uri, strict_probe=True)
    rebound = metadb.catalog_revision_binding_for_uri(registered.uri)
    assert rebound is not None and rebound["dataset_id"] != binding["dataset_id"]
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityFence).where(
                metadb.ManagedLocalLanceRowIdentityFence.registration_id
                == binding["dataset_id"])) == 0
    with pytest.raises(RowIdentityUnavailable):
        managed_local_lance_row_identity_certificate(exact, ["id"])


def test_lance_certificate_rejects_a_string_identity_over_the_public_value_limit(tmp_path):
    _lance, _catalog, _registered, binding, exact = _registered_lance(tmp_path, pa.table({
        "id": pa.array(["x" * 8193], type=pa.string()),
    }))

    with pytest.raises(RowIdentityValueTooLarge) as caught:
        certify_and_persist_managed_local_lance_row_identity(exact, ["id"])
    assert caught.value.reason == "identity_value_over_limit"
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityFence).where(
                metadb.ManagedLocalLanceRowIdentityFence.registration_id
                == binding["dataset_id"])) == 0
