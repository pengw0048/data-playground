"""Direct keyed-upsert service coverage (issue #636).

These tests ARE the workflow's measured verification: the compare-and-swap probe
(``test_stale_base_fails_before_publication``) and the response-loss probe
(``test_response_loss_replay_returns_original_receipt``) certify the managed-local Parquet
full-rewrite workflow's CAS and lost-response semantics empirically, not in prose.
"""
from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hub import keyed_upsert, metadb
from hub.deps import Deps
from hub.keyed_upsert import KeyedUpsertError, build_upsert_intent, upsert_managed_local_file
from hub.models import ExactDatasetRef, WriteDestination


@pytest.fixture(autouse=True)
def _metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'metadata.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


def _publish(deps, logical_uri: str, name: str, run_id: str, table: pa.Table) -> dict:
    artifact = deps.storage.begin_result(run_id, run_id)
    pq.write_table(table, artifact)
    deps.storage.commit_result(artifact, run_id)
    published = deps.catalog.publish_managed_local_file_output(
        name=name, logical_uri=logical_uri, artifact_uri=artifact)
    assert deps.storage.release_result(artifact, run_id)
    return {**published, "logical_uri": logical_uri}


def _deps(tmp_path):
    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"), maintain_storage=False)
    with metadb.session() as session:
        session.add(metadb.User(id="owner", name="Owner"))
    return deps


def _ref(published: dict) -> ExactDatasetRef:
    return ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])


def _output_schema(deps, published: dict):
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    return keyed_upsert._schema_columns(pq.read_table(artifact).schema)


def _intent(deps, base_pub: dict, head_pub: dict, keys: list[str]):
    return build_upsert_intent(
        base=_ref(base_pub), head=_ref(head_pub), keys=keys,
        destination=WriteDestination(
            logical_uri=base_pub["logical_uri"], name="target",
            dataset_id=base_pub["dataset_id"]),
        output_schema=_output_schema(deps, base_pub))


def _table(ids: list[int], values: list[str]) -> pa.Table:
    return pa.table({"id": pa.array(ids, type=pa.int64()), "value": values})


def _base(deps):
    return _publish(deps, deps.storage.output_uri("target", ".parquet"), "target", "base-1",
                    _table([1, 2, 3], ["a", "b", "c"]))


def _payload(deps, table: pa.Table, name: str = "payload") -> dict:
    return _publish(deps, deps.storage.output_uri(name, ".parquet"), name, f"{name}-1", table)


def _revision_count(deps, dataset_id: str) -> int:
    rows, _cursor = metadb.managed_local_file_revision_history(
        metadb.catalog_revision_binding(dataset_id)["uri"], limit=50)
    return len(rows)


def test_golden_upsert_updates_and_inserts(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([2, 3, 4], ["B", "C", "D"]))
    outcome = upsert_managed_local_file(
        storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert outcome.evidence.model_dump() == {
        "matched": 2, "inserted": 1, "unchanged": 1,
        "rejected": 0, "duplicate": 0, "conflict": 0}
    assert outcome.receipt.parent_head is not None
    assert outcome.receipt.parent_head.revision_id == base["revision_id"]
    child = outcome.receipt.revision_id
    assert child not in (base["revision_id"], head["revision_id"])
    artifact = metadb.managed_local_file_revision_artifact(base["dataset_id"], child)
    published = pq.read_table(artifact).to_pydict()
    assert dict(zip(published["id"], published["value"])) == {1: "a", 2: "B", 3: "C", 4: "D"}


def test_all_insert_keeps_every_base_row(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([7, 8], ["g", "h"]))
    outcome = upsert_managed_local_file(
        storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert (outcome.evidence.matched, outcome.evidence.inserted, outcome.evidence.unchanged) == (0, 2, 3)
    artifact = metadb.managed_local_file_revision_artifact(base["dataset_id"], outcome.receipt.revision_id)
    assert set(pq.read_table(artifact).to_pydict()["id"]) == {1, 2, 3, 7, 8}


def test_all_update_replaces_matched_rows(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([1, 2, 3], ["x", "y", "z"]))
    outcome = upsert_managed_local_file(
        storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert (outcome.evidence.matched, outcome.evidence.inserted, outcome.evidence.unchanged) == (3, 0, 0)
    artifact = metadb.managed_local_file_revision_artifact(base["dataset_id"], outcome.receipt.revision_id)
    published = pq.read_table(artifact).to_pydict()
    assert dict(zip(published["id"], published["value"])) == {1: "x", 2: "y", 3: "z"}


def test_duplicate_payload_keys_fail_before_publication(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([2, 2], ["B", "B2"]))
    with pytest.raises(KeyedUpsertError):
        upsert_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert _revision_count(deps, base["dataset_id"]) == 1  # head never moved


def test_null_keys_fail_before_publication(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, pa.table(
        {"id": pa.array([2, None], type=pa.int64()), "value": ["B", "N"]}))
    with pytest.raises(KeyedUpsertError):
        upsert_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert _revision_count(deps, base["dataset_id"]) == 1


def test_schema_mismatch_fails_before_publication(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, pa.table({"id": pa.array([2], type=pa.int64()), "other": ["B"]}))
    with pytest.raises(KeyedUpsertError):
        upsert_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert _revision_count(deps, base["dataset_id"]) == 1


def test_stale_base_fails_before_publication(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    # Advance the target head so the intent's base ref is now stale.
    logical_uri = deps.storage.output_uri("target", ".parquet")
    _publish(deps, logical_uri, "target", "base-2", _table([1, 2, 3, 5], ["a", "b", "c", "e"]))
    head = _payload(deps, _table([2], ["B"]))
    with pytest.raises(KeyedUpsertError):
        upsert_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=_intent(deps, base, head, ["id"]))
    assert _revision_count(deps, base["dataset_id"]) == 2  # no third revision


def test_response_loss_replay_returns_original_receipt(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([2, 4], ["B", "D"]))
    intent = _intent(deps, base, head, ["id"])
    first = upsert_managed_local_file(storage=deps.storage, catalog=deps.catalog, intent=intent)
    after_first = _revision_count(deps, base["dataset_id"])
    replay = upsert_managed_local_file(storage=deps.storage, catalog=deps.catalog, intent=intent)
    assert replay.receipt.revision_id == first.receipt.revision_id
    assert replay.evidence.model_dump() == first.evidence.model_dump()
    assert _revision_count(deps, base["dataset_id"]) == after_first  # head never moved twice


def test_idempotency_key_must_bind_the_upsert_digest(tmp_path):
    deps = _deps(tmp_path)
    base = _base(deps)
    head = _payload(deps, _table([2], ["B"]))
    intent = _intent(deps, base, head, ["id"])
    tampered = intent.model_copy(deep=True)
    object.__setattr__(tampered, "keys", ["value"])  # changed meaning, key no longer bound
    with pytest.raises(ValueError):
        keyed_upsert.UpsertIntentV1.model_validate(tampered.model_dump(by_alias=True))
