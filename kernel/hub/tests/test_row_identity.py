"""Focused SQLite contracts for revision-scoped logical row identity."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hub import db, metadb
from hub.models import ExactDatasetRef
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import (
    RowIdentityFieldV1,
    RowIdentityUnavailable,
    RowIdentityValidationError,
    _encode_identity,
    _spec_digest,
    certify_row_identity_coverage,
    validate_row_identity_coverage,
)
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'row-identity.db'}"
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
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


def _publish(storage, catalog, logical_uri: str, table: pa.Table) -> dict:
    run_id = f"row-identity-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="row_identity", logical_uri=logical_uri, artifact_uri=artifact)
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return published


def _exact(published: dict) -> ExactDatasetRef:
    return ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])


def _candidate(table: pa.Table):
    return db.conn().from_arrow(table)


def _validate(certificate, published: dict, expected_digest: str | None = None) -> None:
    validate_row_identity_coverage(
        certificate, _exact(published),
        certificate.spec.digest if expected_digest is None else expected_digest)


def test_complete_composite_identity_has_stable_type_tagged_evidence(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({
        "signed": pa.array([-1, 2], type=pa.int16()),
        "unsigned": pa.array([1, 2], type=pa.uint32()),
        "label": pa.array(["é", "e\u0301"], type=pa.string()),
    })
    published = _publish(storage, catalog, str(tmp_path / "logical.parquet"), base)
    first = certify_row_identity_coverage(storage, _exact(published), ["signed", "unsigned", "label"], _candidate(base))
    second = certify_row_identity_coverage(storage, _exact(published), ["signed", "unsigned", "label"], _candidate(base))

    assert first.status == "complete"
    assert first.base.rows == first.candidate.rows == 2
    assert first.matched_identities == 2
    assert first.base.key_set_digest == first.candidate.key_set_digest == second.base.key_set_digest
    assert [(field.name, field.arrow_type) for field in first.spec.fields] == [
        ("signed", "int16"), ("unsigned", "uint32"), ("label", "string")]


def test_canonical_encoding_and_spec_type_facts_have_no_dataset_revision_confound():
    fields = (RowIdentityFieldV1("signed", "int16"), RowIdentityFieldV1("label", "string"))
    reversed_fields = tuple(reversed(fields))
    widened_fields = (RowIdentityFieldV1("signed", "int32"), fields[1])
    digest = "a" * 64

    assert _encode_identity(fields, (-1, "é")).hex() == (
        "5249310002036931360000000000000002ffff"
        "04757466380000000000000002c3a9")
    assert _spec_digest("dataset", "revision", fields, digest) != _spec_digest(
        "dataset", "revision", reversed_fields, digest)
    assert _spec_digest("dataset", "revision", fields, digest) != _spec_digest(
        "dataset", "revision", widened_fields, digest)


@pytest.mark.parametrize("candidate", [
    pa.table({"id": pa.array([1, 1], type=pa.int32())}),
    pa.table({"id": pa.array([1, None], type=pa.int32())}),
    pa.table({"id": pa.array([1, 1, None], type=pa.int32())}),
])
def test_null_or_duplicate_keys_are_invalid_but_return_coherent_evidence(local_catalog, tmp_path, candidate):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "invalid.parquet"), candidate)
    result = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(candidate))

    assert result.status == "invalid"
    assert result.base.unique_identities == result.candidate.unique_identities
    assert result.base.duplicate_rows == result.candidate.duplicate_rows
    assert result.base.null_rows == result.candidate.null_rows
    assert result.base.key_set_digest is None if result.base.null_rows else result.base.key_set_digest


def test_raw_typed_distinct_anti_joins_distinguish_equal_count_missing_and_extra(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1, 2], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "coverage.parquet"), base)
    candidate = pa.table({"id": pa.array([1, 3], type=pa.int32())})

    result = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(candidate))

    assert result.status == "partial"
    assert (result.matched_identities, result.missing_identities, result.extra_identities) == (1, 1, 1)
    assert result.base.rows == result.candidate.rows
    assert result.base.key_set_digest != result.candidate.key_set_digest


def test_candidate_key_schema_must_exactly_match_base_spec(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "types.parquet"), base)
    unsigned = pa.table({"id": pa.array([1], type=pa.uint32())})
    unsupported = pa.table({"id": pa.array([1.0], type=pa.float64())})

    with pytest.raises(RowIdentityValidationError, match="row identity schema is invalid"):
        certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(unsigned))
    with pytest.raises(RowIdentityValidationError, match="row identity schema is invalid"):
        certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(unsupported))


def test_exact_revision_survives_head_movement_without_resolving_latest(local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "head-moves.parquet")
    first_table = pa.table({"id": pa.array([1], type=pa.int32())})
    first = _publish(storage, catalog, logical_uri, first_table)
    second = _publish(storage, catalog, logical_uri, pa.table({"id": pa.array([2], type=pa.int32())}))

    result = certify_row_identity_coverage(storage, _exact(first), ["id"], _candidate(first_table))

    assert first["revision_id"] != second["revision_id"]
    assert result.status == "complete"
    assert result.spec.revision_id == first["revision_id"]


def test_reopened_shared_managed_storage_owner_reads_the_same_exact_ledger_artifact(
        local_catalog, tmp_path):
    """The same lifecycle/ledger boundary is exercised by the PostgreSQL CI matrix when enabled."""
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "shared-owner.parquet"), base)
    peer = LocalStorage(storage.root)
    try:
        result = certify_row_identity_coverage(peer, _exact(published), ["id"], _candidate(base),
                                                owner="row-identity-peer")
    finally:
        peer.close()

    assert result.status == "complete"
    assert result.spec.revision_id == published["revision_id"]


def test_missing_ledger_or_guard_failure_is_stable_and_never_moves_the_head(local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "guard.parquet")
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, logical_uri, base)
    exact = _exact(published)
    before = metadb.catalog_managed_local_write_head(logical_uri)

    with pytest.raises(RowIdentityUnavailable, match="exact row identity source is unavailable"):
        certify_row_identity_coverage(storage, ExactDatasetRef(
            kind="exact", dataset_id=exact.dataset_id, revision_id="not-a-real-revision"), ["id"], _candidate(base))

    monkeypatch.setattr(storage, "acquire_result_read", lambda _uri, _owner: (_ for _ in ()).throw(OSError()))
    with pytest.raises(RowIdentityUnavailable, match="exact row identity source is unavailable"):
        certify_row_identity_coverage(storage, exact, ["id"], _candidate(base))
    assert metadb.catalog_managed_local_write_head(logical_uri) == before


def test_artifact_replacement_during_exact_read_fails_closed_without_head_mutation(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "replaced.parquet")
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, logical_uri, base)
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    before = metadb.catalog_managed_local_write_head(logical_uri)
    original_scan = DuckDBAdapter.scan

    def replace_after_open(adapter, uri, *args, **kwargs):
        relation = original_scan(adapter, uri, *args, **kwargs)
        replacement = f"{uri}.replacement"
        pq.write_table(base, replacement)
        os.replace(replacement, uri)
        return relation

    monkeypatch.setattr(DuckDBAdapter, "scan", replace_after_open)
    with pytest.raises(RowIdentityUnavailable, match="exact row identity source is unavailable"):
        certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))
    assert metadb.catalog_managed_local_write_head(logical_uri) == before


def test_malformed_certificate_fails_closed(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "evidence.parquet"), base)
    certificate = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))
    malformed = object.__new__(type(certificate))
    for name in ("spec", "base", "candidate", "missing_identities", "extra_identities", "status"):
        object.__setattr__(malformed, name, getattr(certificate, name))
    object.__setattr__(malformed, "matched_identities", -1)

    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        _validate(malformed, published, certificate.spec.digest)


def test_tampered_evidence_invariants_fail_closed(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1, 2], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "tamper.parquet"), base)
    certificate = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))
    cases = [
        replace(certificate, status="partial"),
        replace(certificate, matched_identities=1),
        replace(certificate, base=replace(certificate.base, duplicate_groups=1)),
        replace(certificate, spec=replace(certificate.spec, fields=())),
        replace(certificate, spec=replace(certificate.spec, schema_digest="0" * 63)),
    ]

    for malformed in cases:
        with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
            _validate(malformed, published, certificate.spec.digest)


def test_frozen_exact_ref_and_spec_digest_reject_coherent_redigests(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "frozen-binding.parquet"), base)
    certificate = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))
    frozen_digest = certificate.spec.digest
    wrong_revision = replace(certificate.spec, revision_id="other-revision")
    wrong_revision = replace(wrong_revision, digest=_spec_digest(
        wrong_revision.dataset_id, wrong_revision.revision_id,
        wrong_revision.fields, wrong_revision.schema_digest))
    wrong_fields = (RowIdentityFieldV1("id", "int64"),)
    wrong_spec = replace(certificate.spec, fields=wrong_fields)
    wrong_spec = replace(wrong_spec, digest=_spec_digest(
        wrong_spec.dataset_id, wrong_spec.revision_id, wrong_spec.fields, wrong_spec.schema_digest))

    _validate(certificate, published, frozen_digest)
    for malformed in (replace(certificate, spec=wrong_revision), replace(certificate, spec=wrong_spec)):
        with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
            _validate(malformed, published, frozen_digest)
    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        _validate(certificate, published, "0" * 64)


def test_key_set_digests_must_agree_exactly_with_coverage_counts(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1, 2], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "digest-binding.parquet"), base)
    complete = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))
    partial = certify_row_identity_coverage(
        storage, _exact(published), ["id"],
        _candidate(pa.table({"id": pa.array([1, 3], type=pa.int32())})))
    changed = ("0" if complete.base.key_set_digest[0] != "0" else "1") + complete.base.key_set_digest[1:]

    _validate(complete, published, complete.spec.digest)
    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        _validate(replace(complete, candidate=replace(
            complete.candidate, key_set_digest=changed)), published, complete.spec.digest)
    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        _validate(replace(partial, candidate=replace(
            partial.candidate, key_set_digest=partial.base.key_set_digest)), published, partial.spec.digest)


@pytest.mark.parametrize("mutate", [
    lambda certificate: replace(certificate, base=object()),
    lambda certificate: replace(certificate, base=replace(certificate.base, rows=True)),
    lambda certificate: replace(certificate, candidate=replace(certificate.candidate, rows=1.5)),
    lambda certificate: replace(certificate, matched_identities=False),
    lambda certificate: replace(certificate, spec=replace(certificate.spec, fields=(
        RowIdentityFieldV1(1, "int32"),))),
])
def test_malformed_nested_evidence_types_raise_stable_validation_error(local_catalog, tmp_path, mutate):
    storage, catalog = local_catalog
    base = pa.table({"id": pa.array([1], type=pa.int32())})
    published = _publish(storage, catalog, str(tmp_path / "nested-types.parquet"), base)
    certificate = certify_row_identity_coverage(storage, _exact(published), ["id"], _candidate(base))

    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        _validate(mutate(certificate), published, certificate.spec.digest)
