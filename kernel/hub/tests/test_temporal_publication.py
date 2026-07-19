"""Acceptance coverage for the private #589 temporal publication transaction."""
from __future__ import annotations

import os
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select, update

from hub import db, metadb
from hub.compound_datasets import CompoundDatasetRef, open_compound_manifest
from hub.local_writes import write_managed_local_file
from hub.models import (
    ColumnSchema, ExactDatasetRef, LineagePublication, WriteDestination, WriteIntent, WriteProvenance,
)
from hub.plugins.adapters import DuckDBAdapter, relation_columns
from hub.plugins.catalog import InMemoryCatalog
from hub.storage import LocalStorage
from hub.temporal_publication import publish_candidate, register_parent
from hub.temporal_resample import ManagedOutputRevision, build_resample_candidate, compose_child_manifest


@pytest.fixture(autouse=True)
def isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'temporal.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


def _case(tmp_path):
    # Reuse the pure-contract fixture; publication itself still uses a fresh database and artifact root.
    from hub.tests.test_temporal_resample import _fixture_manifest, _source_points, _spec, _target_points

    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    manifest = _fixture_manifest(tmp_path)
    candidate = build_resample_candidate(
        manifest, _spec(manifest, tolerance=500), _source_points(), _target_points())
    owner = f"owner-{uuid.uuid4().hex}"
    with metadb.session() as s:
        s.add(metadb.User(id=owner, name=owner))
    register_parent(owner_id=owner, manifest=manifest)
    return storage, catalog, owner, manifest, candidate


def _table(candidate):
    rows = []
    for row in candidate.rows:
        rows.append({"observation_id": row.target_observation_id, "episode_id": "episode-1",
                     "target_tick": row.target_tick, "source_observation_id": row.source_observation_id,
                     "source_tick": row.source_tick, "mapped_source_tick": row.mapped_source_tick,
                     "signed_delta_ticks": row.signed_delta_ticks,
                     "absolute_delta_ticks": row.absolute_delta_ticks, **dict(row.values)})
    return pa.Table.from_pylist(rows)


def _intent(tmp_path, table, key, *, head=None):
    with db.base_guard():
        columns = relation_columns(db.conn().from_arrow(pa.Table.from_batches([], schema=table.schema)))
    schema = [ColumnSchema(name=item.name, type=item.type) for item in columns]
    return WriteIntent(
        destination=WriteDestination(
            logical_uri=str(tmp_path / "derived.parquet"), name="derived",
            dataset_id=None if head is None else head.dataset_id),
        mode="create" if head is None else "replace", expected_schema=schema,
        expected_head=None if head is None else ExactDatasetRef(
            kind="exact", dataset_id=head.dataset_id, revision_id=head.revision_id),
        idempotency_key=key,
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]))


def _counts(owner):
    with metadb.session() as s:
        revisions = int(s.scalar(select(func.count()).select_from(
            metadb.ManagedLocalFileRevision).where(
                metadb.ManagedLocalFileRevision.write_idempotency_key.like(f"%:{owner}:%"))) or 0)
        publications = int(s.scalar(select(func.count()).select_from(
            metadb.TemporalResamplePublication).where(
                metadb.TemporalResamplePublication.owner_id == owner)) or 0)
        compound = int(s.scalar(select(func.count()).select_from(
            metadb.CompoundDatasetRevision).where(
                metadb.CompoundDatasetRevision.owner_id == owner)) or 0)
        heads = int(s.scalar(select(func.count()).select_from(
            metadb.CompoundDatasetHead).where(metadb.CompoundDatasetHead.owner_id == owner)) or 0)
        return revisions, publications, compound, heads


def _publish_two_generations(tmp_path, storage, catalog, owner, manifest, candidate):
    first_table = _table(candidate)
    first_intent = _intent(
        tmp_path / "first", first_table, f"temporal:{owner}:{uuid.uuid4().hex}")
    first = publish_candidate(
        storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
        candidate=candidate, output_member_id="derived-member-1", intent=first_intent,
        write_artifact=lambda uri: pq.write_table(first_table, uri))
    child = open_compound_manifest(json.dumps(
        first.child["manifest"], separators=(",", ":")).encode())
    second_spec = replace(
        candidate.spec, compound_revision_id=child.ref.revision_id,
        output_stream_id="derived-resample-second")
    second_candidate = build_resample_candidate(
        child, second_spec, candidate.source_points, candidate.target_points)
    second_table = _table(second_candidate)
    second_intent = _intent(
        tmp_path / "second", second_table, f"temporal:{owner}:{uuid.uuid4().hex}")
    second = publish_candidate(
        storage=storage, catalog=catalog, owner_id=owner, parent_manifest=child,
        candidate=second_candidate, output_member_id="derived-member-2", intent=second_intent,
        write_artifact=lambda uri: pq.write_table(second_table, uri))
    return first_intent, first, child, second_intent, second, second_candidate, second_table


def test_temporal_candidate_commits_complete_tuple_and_replays(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table, key = _table(candidate), f"temporal:{owner}:{uuid.uuid4().hex}"
        intent = _intent(tmp_path, table, key)
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        replay = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pytest.fail("replay must not allocate an artifact"))
        assert replay == first
        assert first.child["revisionId"] != manifest.ref.revision_id
        assert _counts(owner) == (1, 1, 2, 1)
    finally:
        storage.close()


def test_stale_parent_and_injected_companion_failure_leave_no_output(tmp_path, monkeypatch):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table, key = _table(candidate), f"temporal:{owner}:{uuid.uuid4().hex}"
        intent = _intent(tmp_path, table, key)
        with metadb.session() as s:
            s.get(metadb.CompoundDatasetHead, {"owner_id": owner, "dataset_id": manifest.ref.dataset_id}).revision_id = "f" * 64
        with pytest.raises(metadb.ManagedLocalWriteConflict, match="stale"):
            publish_candidate(storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                              candidate=candidate, output_member_id="derived-member", intent=intent,
                              write_artifact=lambda uri: pq.write_table(table, uri))
        assert _counts(owner) == (0, 0, 1, 1)
        with metadb.session() as s:
            s.get(metadb.CompoundDatasetHead, {"owner_id": owner,
                  "dataset_id": manifest.ref.dataset_id}).revision_id = manifest.ref.revision_id
        original = metadb._commit_temporal_publication_in_session
        monkeypatch.setattr(metadb, "_commit_temporal_publication_in_session",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")))
        with pytest.raises(RuntimeError, match="injected"):
            publish_candidate(storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                              candidate=candidate, output_member_id="derived-member", intent=intent,
                              write_artifact=lambda uri: pq.write_table(table, uri))
        monkeypatch.setattr(metadb, "_commit_temporal_publication_in_session", original)
        assert _counts(owner) == (0, 0, 1, 1)
    finally:
        storage.close()


def test_concurrent_children_from_one_parent_advance_exactly_once(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)

        def publish(index):
            key = f"temporal-race:{owner}:{index}:{uuid.uuid4().hex}"
            intent = _intent(tmp_path / str(index), table, key)
            try:
                return publish_candidate(
                    storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                    candidate=candidate, output_member_id=f"derived-member-{index}", intent=intent,
                    write_artifact=lambda uri: pq.write_table(table, uri))
            except metadb.ManagedLocalWriteConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, (1, 2)))
        assert sum(result is not None for result in results) == 1
        assert _counts(owner) == (1, 1, 2, 1)
    finally:
        storage.close()


def test_candidate_shape_is_bound_before_any_publication(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        wrong = pa.table({"wrong": [1, 2]})
        intent = _intent(tmp_path, wrong, f"temporal:{owner}:{uuid.uuid4().hex}")
        with pytest.raises(ValueError, match="temporal output (schema|row count)"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pq.write_table(wrong, uri))
        assert _counts(owner) == (0, 0, 1, 1)
    finally:
        storage.close()


def test_old_publication_replays_after_a_descendant_advances_the_head(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        first_intent, first, *_rest = _publish_two_generations(
            tmp_path, storage, catalog, owner, manifest, candidate)

        replay = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member-1", intent=first_intent,
            write_artifact=lambda uri: pytest.fail("old replay must not allocate"))
        assert replay == first
        assert _counts(owner) == (2, 2, 3, 1)
    finally:
        storage.close()


def test_temporal_output_is_retained_after_catalog_head_replacement(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        first_intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=first_intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        replacement = _intent(
            tmp_path, table, f"ordinary:{owner}:{uuid.uuid4().hex}", head=first.receipt.head)
        write_managed_local_file(
            storage=storage, catalog=catalog, intent=replacement,
            write_artifact=lambda uri: pq.write_table(table, uri))
        metadb.managed_local_file_revision_gc_batch(0, limit=50)
        assert metadb.managed_local_file_revision_artifact(
            first.receipt.dataset_id, first.receipt.revision_id) is not None
        assert publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=first_intent,
            write_artifact=lambda uri: pytest.fail("retained replay must not allocate")) == first
    finally:
        storage.close()


def test_register_parent_rejects_a_forged_revision_without_state(tmp_path):
    from hub.tests.test_temporal_resample import _fixture_manifest

    manifest = _fixture_manifest(tmp_path)
    owner = f"owner-{uuid.uuid4().hex}"
    with metadb.session() as s:
        s.add(metadb.User(id=owner, name=owner))
    forged = replace(manifest, ref=CompoundDatasetRef(manifest.ref.dataset_id, "f" * 64))
    with pytest.raises(ValueError, match="parent"):
        register_parent(owner_id=owner, manifest=forged)
    assert _counts(owner) == (0, 0, 0, 0)


def test_replay_revalidates_stored_child_and_ancestry(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        child_id = first.child["revisionId"]
        with metadb.session() as s:
            child = s.get(metadb.CompoundDatasetRevision, {
                "owner_id": owner, "dataset_id": manifest.ref.dataset_id,
                "revision_id": child_id})
            original_doc, original_parent = child.manifest_doc, child.parent_revision_id
            child.manifest_doc = "{}"
        with pytest.raises(RuntimeError, match="compound revision"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("corrupt replay must not allocate"))
        with metadb.session() as s:
            child = s.get(metadb.CompoundDatasetRevision, {
                "owner_id": owner, "dataset_id": manifest.ref.dataset_id,
                "revision_id": child_id})
            child.manifest_doc, child.parent_revision_id = original_doc, "e" * 64
        with pytest.raises(RuntimeError, match="canonical parent or output"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("corrupt replay must not allocate"))
        with metadb.session() as s:
            child = s.get(metadb.CompoundDatasetRevision, {
                "owner_id": owner, "dataset_id": manifest.ref.dataset_id,
                "revision_id": child_id})
            child.parent_revision_id = original_parent
            s.execute(update(metadb.CompoundDatasetHead).where(
                metadb.CompoundDatasetHead.owner_id == owner,
                metadb.CompoundDatasetHead.dataset_id == manifest.ref.dataset_id,
            ).values(revision_id="d" * 64))
        with pytest.raises(RuntimeError, match="child revision or head"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("wrong-branch replay must not allocate"))
    finally:
        storage.close()


def test_replay_rejects_canonical_child_with_forged_transform_identity(tmp_path):
    from hub.temporal_resample import _manifest_digest

    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        old_child = first.child["revisionId"]
        forged = dict(first.child["manifest"])
        stream = next(item for item in forged["streams"]
                      if item["id"] == candidate.spec.output_stream_id)
        stream["transformChain"] = ["temporal-resample-v1", "evidence-sha256:" + "0" * 64,
                                    "candidate-sha256:" + candidate.digest]
        forged["revisionId"] = _manifest_digest(forged)
        new_child = forged["revisionId"]
        forged_doc = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        with metadb.session() as s:
            s.execute(update(metadb.CompoundDatasetRevision).where(
                metadb.CompoundDatasetRevision.owner_id == owner,
                metadb.CompoundDatasetRevision.dataset_id == manifest.ref.dataset_id,
                metadb.CompoundDatasetRevision.revision_id == old_child,
            ).values(revision_id=new_child, manifest_doc=forged_doc))
            s.execute(update(metadb.TemporalResamplePublication).where(
                metadb.TemporalResamplePublication.owner_id == owner,
            ).values(child_revision_id=new_child))
            s.execute(update(metadb.CompoundDatasetHead).where(
                metadb.CompoundDatasetHead.owner_id == owner,
                metadb.CompoundDatasetHead.dataset_id == manifest.ref.dataset_id,
            ).values(revision_id=new_child))
        with pytest.raises(RuntimeError, match="canonical parent or output"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("forged replay must not allocate"))
    finally:
        storage.close()


def test_replay_rejects_an_unpublished_canonical_descendant(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        child = open_compound_manifest(json.dumps(
            first.child["manifest"], separators=(",", ":")).encode())
        second_spec = replace(
            candidate.spec, compound_revision_id=child.ref.revision_id,
            output_stream_id="unpublished-derived")
        second_candidate = build_resample_candidate(
            child, second_spec, candidate.source_points, candidate.target_points)
        unpublished = compose_child_manifest(child, second_candidate, ManagedOutputRevision(
            member_id="unpublished-member", dataset_id="unmanaged-output",
            revision_id="f" * 32))
        unpublished_doc = json.dumps(unpublished, sort_keys=True, separators=(",", ":"))
        with metadb.session() as s:
            s.add(metadb.CompoundDatasetRevision(
                owner_id=owner, dataset_id=manifest.ref.dataset_id,
                revision_id=unpublished["revisionId"], manifest_doc=unpublished_doc,
                parent_revision_id=child.ref.revision_id))
            s.get(metadb.CompoundDatasetHead, {
                "owner_id": owner,
                "dataset_id": manifest.ref.dataset_id}).revision_id = unpublished["revisionId"]
        with pytest.raises(RuntimeError, match="child revision or head"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("unpublished replay must not allocate"))
    finally:
        storage.close()


def test_descendant_replay_rejects_mutated_spec_and_output_revision(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        first_intent, _first, _child, second_intent, second, _candidate, table = (
            _publish_two_generations(tmp_path, storage, catalog, owner, manifest, candidate))
        with metadb.session() as s:
            row = s.get(metadb.TemporalResamplePublication, second_intent.idempotency_key)
            original_spec = row.spec_doc
            mutated = json.loads(row.spec_doc)
            mutated["outputCap"] += 1
            row.spec_doc = json.dumps(mutated, sort_keys=True, separators=(",", ":"))
        with pytest.raises(RuntimeError, match="spec, evidence, and receipt"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member-1", intent=first_intent,
                write_artifact=lambda uri: pytest.fail("mutated replay must not allocate"))
        with metadb.session() as s:
            s.get(metadb.TemporalResamplePublication,
                  second_intent.idempotency_key).spec_doc = original_spec
        replacement = _intent(
            tmp_path / "second", table, f"ordinary:{owner}:{uuid.uuid4().hex}",
            head=second.receipt.head)
        replacement_receipt = write_managed_local_file(
            storage=storage, catalog=catalog, intent=replacement,
            write_artifact=lambda uri: pq.write_table(table, uri))
        with metadb.session() as s:
            s.get(metadb.TemporalResamplePublication,
                  second_intent.idempotency_key).output_revision_id = replacement_receipt.revision_id
        with pytest.raises(RuntimeError, match="spec, evidence, and receipt"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member-1", intent=first_intent,
                write_artifact=lambda uri: pytest.fail("mismatched replay must not allocate"))
    finally:
        storage.close()


def test_descendant_replay_rejects_inherited_parent_member_drift(tmp_path):
    from hub.temporal_resample import _manifest_digest

    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        first_intent, _first, _child, second_intent, second, _candidate, _table_value = (
            _publish_two_generations(tmp_path, storage, catalog, owner, manifest, candidate))
        old_child = second.child["revisionId"]
        forged = dict(second.child["manifest"])
        inherited = next(item for item in forged["members"] if item["id"] == "derived-member-1")
        inherited["revisionId"] = "forged-inherited-output"
        forged["revisionId"] = _manifest_digest(forged)
        new_child = forged["revisionId"]
        with metadb.session() as s:
            s.execute(update(metadb.CompoundDatasetRevision).where(
                metadb.CompoundDatasetRevision.owner_id == owner,
                metadb.CompoundDatasetRevision.dataset_id == manifest.ref.dataset_id,
                metadb.CompoundDatasetRevision.revision_id == old_child,
            ).values(revision_id=new_child, manifest_doc=json.dumps(
                forged, sort_keys=True, separators=(",", ":"))))
            s.get(metadb.TemporalResamplePublication,
                  second_intent.idempotency_key).child_revision_id = new_child
            s.get(metadb.CompoundDatasetHead, {
                "owner_id": owner,
                "dataset_id": manifest.ref.dataset_id}).revision_id = new_child
        with pytest.raises(RuntimeError, match="inherited parent facts"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member-1", intent=first_intent,
                write_artifact=lambda uri: pytest.fail("drift replay must not allocate"))
    finally:
        storage.close()


def test_compound_child_revision_identity_is_owner_scoped(tmp_path):
    from sqlalchemy import inspect

    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        second_owner = f"owner-{uuid.uuid4().hex}"
        with metadb.session() as s:
            s.add(metadb.User(id=second_owner, name=second_owner))
        register_parent(owner_id=second_owner, manifest=manifest)
        with metadb.session() as s:
            s.add(metadb.CompoundDatasetRevision(
                owner_id=second_owner, dataset_id=manifest.ref.dataset_id,
                revision_id=first.child["revisionId"],
                manifest_doc=json.dumps(first.child["manifest"], sort_keys=True,
                                        separators=(",", ":")),
                parent_revision_id=manifest.ref.revision_id))
            s.get(metadb.CompoundDatasetHead, {
                "owner_id": second_owner,
                "dataset_id": manifest.ref.dataset_id}).revision_id = first.child["revisionId"]
        uniques = {tuple(item["column_names"]) for item in inspect(
            metadb.engine()).get_unique_constraints("temporal_resample_publications")}
        assert ("owner_id", "parent_dataset_id", "child_revision_id") in uniques
        assert ("child_revision_id",) not in uniques
    finally:
        storage.close()


def test_unicode_candidate_identity_commits_and_replays(tmp_path):
    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        unicode_spec = replace(candidate.spec, source_view=replace(
            candidate.spec.source_view, view_id="传感器视图"))
        unicode_candidate = build_resample_candidate(
            manifest, unicode_spec, candidate.source_points, candidate.target_points)
        table = _table(unicode_candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=unicode_candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        assert publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=unicode_candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pytest.fail("unicode replay must not allocate")) == first
    finally:
        storage.close()


def test_replay_rejects_canonical_child_with_raw_parent_member_drift(tmp_path):
    from hub.temporal_resample import _manifest_digest

    storage, catalog, owner, manifest, candidate = _case(tmp_path)
    try:
        table = _table(candidate)
        intent = _intent(tmp_path, table, f"temporal:{owner}:{uuid.uuid4().hex}")
        first = publish_candidate(
            storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
            candidate=candidate, output_member_id="derived-member", intent=intent,
            write_artifact=lambda uri: pq.write_table(table, uri))
        old_child = first.child["revisionId"]
        forged = dict(first.child["manifest"])
        inherited = next(item for item in forged["members"] if item["id"] != "derived-member")
        inherited["revisionId"] = "forged-parent-revision"
        forged["revisionId"] = _manifest_digest(forged)
        new_child = forged["revisionId"]
        with metadb.session() as s:
            s.execute(update(metadb.CompoundDatasetRevision).where(
                metadb.CompoundDatasetRevision.owner_id == owner,
                metadb.CompoundDatasetRevision.dataset_id == manifest.ref.dataset_id,
                metadb.CompoundDatasetRevision.revision_id == old_child,
            ).values(revision_id=new_child, manifest_doc=json.dumps(
                forged, sort_keys=True, separators=(",", ":"))))
            s.execute(update(metadb.TemporalResamplePublication).where(
                metadb.TemporalResamplePublication.owner_id == owner,
            ).values(child_revision_id=new_child))
            s.execute(update(metadb.CompoundDatasetHead).where(
                metadb.CompoundDatasetHead.owner_id == owner,
                metadb.CompoundDatasetHead.dataset_id == manifest.ref.dataset_id,
            ).values(revision_id=new_child))
        with pytest.raises(RuntimeError, match="canonical parent or output"):
            publish_candidate(
                storage=storage, catalog=catalog, owner_id=owner, parent_manifest=manifest,
                candidate=candidate, output_member_id="derived-member", intent=intent,
                write_artifact=lambda uri: pytest.fail("drift replay must not allocate"))
    finally:
        storage.close()
