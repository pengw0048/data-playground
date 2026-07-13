from __future__ import annotations

import datetime
import contextlib
import hashlib
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select

from hub import handoff, metadb


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _handle(kind: str = "region", *, logical: str | None = None,
            run_id: str | None = None, allocation_key: str | None = None) -> dict:
    token = uuid.uuid4().hex
    logical = logical or f"s3://lifecycle-tests/{token}/out.parquet"
    run_id = run_id or f"run-{token}"
    allocation_key = allocation_key or f"allocation-{token}"
    return metadb.allocate_object_attempt(
        logical_uri=logical, kind=kind, run_id=run_id, allocation_key=allocation_key,
        catalog_key_base=("tbl_lifecycle_test" if kind == "sink" else None),
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )


def _inventory(handle: dict, *, version_id: str | None = None) -> list[dict]:
    parsed = urlsplit(handle["uri"])
    root = f"{parsed.netloc}/{parsed.path.lstrip('/')}"
    return [
        {"member_id": handoff._member_id(
            "object_version" if version_id else "unversioned_object",
            f"{root}/part-00000.parquet", version_id or "null"),
         "key": f"{root}/part-00000.parquet",
         "member_type": "object_version" if version_id else "unversioned_object",
         "size": 10, "etag": "data", "version_id": version_id, "upload_id": None,
         "is_latest": True, "is_commit": False},
        {"member_id": handoff._member_id(
            "object_version" if version_id else "unversioned_object",
            handoff._object_manifest_path(root), version_id or "null"),
         "key": handoff._object_manifest_path(root),
         "member_type": "object_version" if version_id else "unversioned_object",
         "size": 20, "etag": "commit", "version_id": version_id, "upload_id": None,
         "is_latest": True, "is_commit": True},
    ]


def _commit(handle: dict, *, version_id: str | None = None) -> list[dict]:
    inventory = _inventory(handle, version_id=version_id)
    metadb.record_object_attempt_commit(handle["uri"], inventory)
    return inventory


def _state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


@contextlib.contextmanager
def _postgres_lock_timeout(seconds: int = 2):
    if metadb.engine().dialect.name != "postgresql":
        yield
        return
    from sqlalchemy import event

    engine = metadb.engine()

    def set_timeout(dbapi_connection, _connection_record, _connection_proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = '{int(seconds)}s'")

    event.listen(engine, "checkout", set_timeout)
    engine.dispose()
    try:
        yield
    finally:
        event.remove(engine, "checkout", set_timeout)
        engine.dispose()


def test_postgres_concurrent_first_sink_publishers_reserve_one_logical_identity():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/concurrent.parquet"
    barrier = threading.Barrier(2)
    handles: list[dict] = []
    errors: list[BaseException] = []

    def allocate(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            handles.append(metadb.allocate_object_attempt(
                logical_uri=logical,
                kind="sink",
                run_id=f"pg-concurrent-{token}-{index}",
                allocation_key=f"pg-concurrent-{token}-{index}",
                catalog_key_base="tbl_pg_concurrent",
                uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
                    logical, namespace, generation, attempt_id),
            ))
        except BaseException as exc:  # noqa: BLE001 - preserve both thread failures for assertion
            errors.append(exc)

    threads = [threading.Thread(target=allocate, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    with metadb.session() as session:
        attempts = list(session.scalars(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.uri.in_([handle["uri"] for handle in handles]))))
        assert sorted(attempt.publish_seq for attempt in attempts) == [1, 2]
        logical_id = attempts[0].logical_id
        assert {attempt.logical_id for attempt in attempts} == {logical_id}
        rows = list(session.scalars(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == logical)))
        assert len(rows) == 1
        assert rows[0].next_publish_seq == 2
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_committed_allocation_retry_uses_publish_compatible_lock_order():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/allocation-retry.parquet"
    allocation_key = f"pg-allocation-retry-{token}"
    first = _handle(
        "sink", logical=logical, run_id=f"pg-first-{token}", allocation_key=allocation_key)
    _commit(first)
    with metadb.session() as session:
        logical_id = session.get(metadb.ObjectAttempt, first["uri"]).logical_id

    started = threading.Event()
    handles: list[dict] = []
    errors: list[BaseException] = []

    def retry_allocation() -> None:
        started.set()
        try:
            handles.append(_handle(
                "sink", logical=logical, run_id=f"pg-retry-{token}",
                allocation_key=allocation_key))
        except BaseException as exc:  # noqa: BLE001 - asserted after deterministic interleaving
            errors.append(exc)

    with _postgres_lock_timeout():
        with metadb.session() as session:
            session.get(metadb.CatalogLogicalDataset, logical_id, with_for_update=True)
            thread = threading.Thread(target=retry_allocation)
            thread.start()
            assert started.wait(timeout=2)
            time.sleep(0.1)
            assert thread.is_alive(), "allocation retry did not wait for the logical publication lock"
            # The retry must not own the attempt while waiting for the logical row. A publisher uses
            # this exact logical -> attempt order; the former attempt -> logical order deadlocked here.
            assert session.get(metadb.ObjectAttempt, first["uri"], with_for_update=True) is not None
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert len(handles) == 1 and handles[0]["generation"] == first["generation"] + 1
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(handles[0]["uri"], "test cleanup")


def test_postgres_cache_replacement_reader_pin_and_gc_are_atomic():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/cache-race.parquet"
    old = _handle(logical=logical)
    new = _handle(logical=logical)
    _commit(old)
    _commit(new)
    cache_key = f"pg-cache-race-{token}"
    metadb.put_result(cache_key, {"uri": old["uri"], "rows": 1})
    barrier = threading.Barrier(2)
    acquired: list[tuple[dict | None, str | None]] = []
    errors: list[BaseException] = []

    def acquire() -> None:
        try:
            barrier.wait(timeout=3)
            acquired.append(metadb.acquire_result_cache_pin(
                cache_key, f"pg-reader-{token}", 30))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def replace() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.put_result(cache_key, {"uri": new["uri"], "rows": 1})
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=acquire), threading.Thread(target=replace)]
    with _postgres_lock_timeout():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and len(acquired) == 1
    doc, pin_id = acquired[0]
    pinned_uri = doc["uri"]
    assert pinned_uri in (old["uri"], new["uri"]) and pin_id
    actions = metadb.object_attempt_gc_batch(0, 0)
    assert pinned_uri not in {action["uri"] for action in actions}
    assert _state(pinned_uri) == "published"

    run_id = f"pg-cache-reader-{token}"
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "output_uri": pinned_uri})
    metadb.release_result_cache_pin(pin_id)
    assert _state(pinned_uri) == "published"
    metadb.save_run_state(run_id, {"run_id": run_id, "status": "failed"})
    metadb.put_result(cache_key, {"uri": None})
    for handle in (old, new):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_result_pin_release_and_expiry_cleanup_share_lock_order():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    errors: list[BaseException] = []
    handles: list[dict] = []
    for index in range(5):
        token = uuid.uuid4().hex
        handle = _handle(logical=f"s3://lifecycle-tests/{token}/pin-expiry.parquet")
        handles.append(handle)
        _commit(handle)
        cache_key = f"pg-pin-expiry-{token}"
        metadb.put_result(cache_key, {"uri": handle["uri"]})
        _doc, pin_id = metadb.acquire_result_cache_pin(cache_key, f"expiry-{index}", 30)
        metadb.put_result(cache_key, {"uri": None})
        with metadb.session() as session:
            session.get(metadb.ObjectAttemptLease, pin_id).expires_at = \
                metadb._db_now(session) - datetime.timedelta(seconds=1)
        barrier = threading.Barrier(2)

        def release() -> None:
            try:
                barrier.wait(timeout=3)
                metadb.release_result_cache_pin(pin_id)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        def expire() -> None:
            try:
                barrier.wait(timeout=3)
                metadb.object_attempt_gc_batch(0, 0)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=release), threading.Thread(target=expire)]
        with _postgres_lock_timeout():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
        assert all(not thread.is_alive() for thread in threads)
        with metadb.session() as session:
            assert session.get(metadb.ObjectAttemptRef, {
                "ref_type": "result_reader", "ref_key": pin_id}) is None
            assert session.get(metadb.ObjectAttemptLease, pin_id) is None
            assert session.get(metadb.ObjectAttempt, handle["uri"]).state != "published"
    assert not any("lock timeout" in str(exc).lower() or "deadlock" in str(exc).lower()
                   for exc in errors)
    assert errors == []
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_unregister_serializes_governance_mutations():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    operations = (
        lambda uri: metadb.catalog_set_metadata(uri, "ghost", None, None, ["ghost"]),
        lambda uri: metadb.catalog_set_declared_key(uri, ["ghost"]),
        lambda uri: metadb.catalog_set_embedding(uri, "model", 1, b"ghost"),
        lambda uri: metadb.catalog_add_edge("s3://external/ghost", uri, "ghost"),
        lambda uri: metadb.catalog_upsert_relationship("ghost", {
            "leftUri": uri, "leftColumns": ["ghost"],
            "rightUri": "s3://external/ghost", "rightColumns": ["ghost"],
        }),
        lambda uri: metadb.catalog_delete_entry(uri),
    )
    for index, mutate in enumerate(operations):
        logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/pg-fence-{index}.parquet"
        handle = _handle("sink", logical=logical)
        _commit(handle)
        metadb.catalog_upsert_entry(
            handle["uri"], f"pg-fence-{index}", {
                "id": "ignored", "name": f"pg-fence-{index}", "uri": handle["uri"]})
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, handle["uri"])
            logical_id = attempt.logical_id
            catalog_key = session.get(metadb.CatalogLogicalDataset, logical_id).catalog_key

        started = threading.Event()
        errors: list[BaseException] = []

        def late_mutation() -> None:
            started.set()
            try:
                mutate(handle["uri"])
            except BaseException as exc:  # noqa: BLE001 - asserted after the interleaving
                errors.append(exc)

        with metadb.session() as session:
            logical_row = session.scalar(select(metadb.CatalogLogicalDataset).where(
                metadb.CatalogLogicalDataset.logical_id == logical_id).with_for_update())
            entry = session.get(metadb.CatalogEntry, logical_row.current_uri, with_for_update=True)
            thread = threading.Thread(target=late_mutation)
            thread.start()
            assert started.wait(timeout=2)
            time.sleep(0.05)
            assert thread.is_alive(), "governance mutation bypassed the logical-row lock"
            for ref in list(session.scalars(select(metadb.ObjectAttemptRef).where(
                    metadb.ObjectAttemptRef.ref_type == "catalog",
                    metadb.ObjectAttemptRef.ref_key == logical_id))):
                session.delete(ref)
            session.delete(entry)
            logical_row.current_uri = None
            logical_row.catalog_epoch += 1
            logical_row.state = "unregistered"
            logical_row.governance_doc = "{}"
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and "inactive" in str(errors[0])
        with metadb.session() as session:
            assert session.get(metadb.CatalogDeclaredKey, catalog_key) is None
            assert session.get(metadb.CatalogEmbedding, catalog_key) is None
            assert not list(session.scalars(select(metadb.CatalogEdge).where(
                metadb.CatalogEdge.child == catalog_key)))
            assert not any(catalog_key in row.doc for row in
                           session.scalars(select(metadb.CatalogRelationship)))
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_publish_and_unregister_share_one_lock_order():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/pg-unregister.parquet"
    old = _handle("sink", logical=logical, allocation_key=f"pg-old-{uuid.uuid4().hex}")
    new = _handle("sink", logical=logical, allocation_key=f"pg-new-{uuid.uuid4().hex}")
    _commit(old)
    metadb.catalog_upsert_entry(
        old["uri"], "pg-unregister", {
            "id": "ignored", "name": "pg-unregister", "uri": old["uri"]})
    stable_id = metadb.catalog_get(old["uri"])["id"]
    _commit(new)
    barrier = threading.Barrier(2)
    errors: list[tuple[str, BaseException]] = []

    def publish() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_upsert_entry(
                new["uri"], "pg-unregister", {
                    "id": "ignored", "name": "pg-unregister", "uri": new["uri"]})
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("publish", exc))

    def unregister() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_delete_entry(old["uri"])
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("unregister", exc))

    threads = [threading.Thread(target=publish), threading.Thread(target=unregister)]
    with _postgres_lock_timeout():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    assert not any("lock timeout" in str(exc).lower() or "deadlock" in str(exc).lower()
                   for _name, exc in errors)
    assert not any(name == "unregister" for name, _exc in errors)
    assert metadb.catalog_get(stable_id) is None
    with metadb.session() as session:
        logical_row = session.get(
            metadb.CatalogLogicalDataset, session.get(metadb.ObjectAttempt, old["uri"]).logical_id)
        assert logical_row.current_uri is None and logical_row.state == "unregistered"
        assert session.get(metadb.ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical_row.logical_id}) is None
    metadb.abandon_committed_object_attempt(new["uri"])
    metadb.quarantine_object_attempt(old["uri"], "test cleanup")
    metadb.quarantine_object_attempt(new["uri"], "test cleanup")


def test_postgres_publish_and_delete_prefix_fail_closed_without_deadlock():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    prefix = f"s3://lifecycle-tests/{token}/"
    logical = prefix + "pg-prefix.parquet"
    old = _handle("sink", logical=logical, allocation_key=f"pg-prefix-old-{token}")
    new = _handle("sink", logical=logical, allocation_key=f"pg-prefix-new-{token}")
    _commit(old)
    metadb.catalog_upsert_entry(
        old["uri"], "pg-prefix", {
            "id": "ignored", "name": "pg-prefix", "uri": old["uri"]})
    stable_id = metadb.catalog_get(old["uri"])["id"]
    _commit(new)
    barrier = threading.Barrier(2)
    errors: list[tuple[str, BaseException]] = []

    def publish() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_upsert_entry(
                new["uri"], "pg-prefix", {
                    "id": "ignored", "name": "pg-prefix", "uri": new["uri"]})
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("publish", exc))

    def delete_prefix() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_delete_prefix(prefix)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("delete-prefix", exc))

    threads = [threading.Thread(target=publish), threading.Thread(target=delete_prefix)]
    with _postgres_lock_timeout():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    assert not any("lock timeout" in str(exc).lower() or "deadlock" in str(exc).lower()
                   for _name, exc in errors)
    assert all("fenced" in str(exc) or "changed concurrently" in str(exc)
               for _name, exc in errors)
    current = metadb.catalog_get(stable_id)
    with metadb.session() as session:
        logical_row = session.get(
            metadb.CatalogLogicalDataset, session.get(metadb.ObjectAttempt, old["uri"]).logical_id)
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical_row.logical_id})
        if current is None:
            assert logical_row.current_uri is None and ref is None
        else:
            assert current["uri"] == logical_row.current_uri == new["uri"]
            assert ref is not None and ref.attempt_uri == new["uri"]
            assert session.get(metadb.CatalogEntry, new["uri"]) is not None
    if current is not None:
        metadb.catalog_delete_entry(new["uri"])
    metadb.abandon_committed_object_attempt(new["uri"])
    metadb.quarantine_object_attempt(old["uri"], "test cleanup")
    metadb.quarantine_object_attempt(new["uri"], "test cleanup")


def test_postgres_parent_unregister_and_child_publish_do_not_leave_stable_lineage_ghost():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    parent_logical = f"s3://lifecycle-tests/{token}/parent.parquet"
    child_logical = f"s3://lifecycle-tests/{token}/child.parquet"
    parent = _handle("sink", logical=parent_logical, allocation_key=f"pg-parent-{token}")
    child = _handle("sink", logical=child_logical, allocation_key=f"pg-child-{token}")
    _commit(parent)
    metadb.catalog_upsert_entry(parent["uri"], "pg-parent", {
        "id": "ignored", "name": "pg-parent", "uri": parent["uri"]})
    parent_stable_id = metadb.catalog_get(parent["uri"])["id"]
    _commit(child)

    barrier = threading.Barrier(2)
    errors: list[tuple[str, BaseException]] = []

    def publish_child() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_upsert_entry(child["uri"], "pg-child", {
                "id": "ignored", "name": "pg-child", "uri": child["uri"],
            }, parents=[parent["uri"]], pipeline="pg-lineage")
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("publish", exc))

    def unregister_parent() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.catalog_delete_entry(parent["uri"])
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(("unregister", exc))

    threads = [threading.Thread(target=publish_child), threading.Thread(target=unregister_parent)]
    with _postgres_lock_timeout():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert metadb.catalog_get(parent_stable_id) is None
    assert metadb.catalog_get(child["uri"])["uri"] == child["uri"]

    replacement = _handle(
        "sink", logical=parent_logical, allocation_key=f"pg-parent-replacement-{token}")
    _commit(replacement)
    metadb.catalog_upsert_entry(replacement["uri"], "pg-parent", {
        "id": "ignored", "name": "pg-parent", "uri": replacement["uri"]})
    child_parents = {edge["parent"] for edge in metadb.catalog_edges()
                     if edge["child"] == child["uri"]}
    assert replacement["uri"] not in child_parents
    with metadb.session() as session:
        parent_attempt = session.get(metadb.ObjectAttempt, parent["uri"])
        parent_row = session.get(metadb.CatalogLogicalDataset, parent_attempt.logical_id)
        assert not list(session.scalars(select(metadb.CatalogEdge).where(
            metadb.CatalogEdge.parent == parent_row.catalog_key,
            metadb.CatalogEdge.child == session.get(
                metadb.CatalogLogicalDataset,
                session.get(metadb.ObjectAttempt, child["uri"]).logical_id).catalog_key,
        )))

    metadb.catalog_delete_entry(child["uri"])
    metadb.catalog_delete_entry(replacement["uri"])
    metadb.quarantine_object_attempt(parent["uri"], "test cleanup")
    metadb.quarantine_object_attempt(child["uri"], "test cleanup")
    metadb.quarantine_object_attempt(replacement["uri"], "test cleanup")


def test_r2_managed_write_fails_before_attempt_allocation_without_provider():
    logical = f"r2://lifecycle-tests/{uuid.uuid4().hex}/out.parquet"
    with pytest.raises(
            handoff.ManagedProviderCapabilityError,
            match="cannot prove complete version and multipart inventory"):
        handoff.allocate_attempt(
            logical_uri=logical,
            kind="sink",
            run_id=f"r2-{uuid.uuid4().hex}",
            allocation_key=f"r2-{uuid.uuid4().hex}",
            catalog_key_base="tbl_r2_fail_closed",
            uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
                logical, namespace, generation, attempt_id),
        )
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.logical_uri == logical)) == 0


def test_invalid_managed_uri_is_rejected_before_provider_side_effects():
    provider = _ExactProvider([])
    handoff.set_managed_object_provider(provider)
    try:
        invalid = (
            "s3://user:private-secret@bucket/path/out.parquet",
            "s3://bucket/path/out.parquet?credential=private-secret",
            "s3://bucket/path/out.parquet#private-secret",
        )
        for uri in invalid:
            with pytest.raises(ValueError) as caught:
                handoff.allocate_attempt(
                    logical_uri=uri, kind="sink", run_id="invalid-uri",
                    allocation_key=f"invalid-{uuid.uuid4().hex}",
                    catalog_key_base="tbl_invalid",
                    uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
                        uri, namespace, generation, attempt_id),
                )
            assert "private-secret" not in str(caught.value)
        assert provider._claims == {}
    finally:
        handoff.set_managed_object_provider(None)


def test_attempt_refs_cover_cache_history_and_state_pruning(monkeypatch):
    handle = _handle()
    _commit(handle)
    uri = handle["uri"]
    cache_a, cache_b = f"cache-{uuid.uuid4().hex}", f"cache-{uuid.uuid4().hex}"
    metadb.put_result(cache_a, {"uri": uri, "rows": 1})
    metadb.put_result(cache_b, {"uri": uri, "rows": 1})

    canvas_id = f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="refs", version=1, doc="{}"))
    monkeypatch.setattr(metadb, "_RUN_HISTORY_MAX", 1)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
    run_id = f"run-{uuid.uuid4().hex}"
    metadb.record_run(canvas_id, "n", "done", run_id=run_id, output_uri=uri)
    metadb.save_run_state(run_id, {"run_id": run_id, "status": "done", "output_uri": uri})

    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.attempt_uri == uri)))
        assert sorted(ref.ref_type for ref in refs) == [
            "result_cache", "result_cache", "run_record", "run_state"]

    # Pruning the owning SQL rows releases exactly their refs, but two cache owners still pin the data.
    metadb.record_run(canvas_id, "n", "done", run_id=f"new-{run_id}")
    metadb.save_run_state(
        f"new-{run_id}", {"run_id": f"new-{run_id}", "status": "done"})
    assert _state(uri) == "published"
    metadb.put_result(cache_a, {"uri": None})
    assert _state(uri) == "published", "one cache key cannot retire another key's artifact"
    with handoff.managed_read_lease(uri, owner="history-read"):
        assert _state(uri) == "published"
    metadb.put_result(cache_b, {"uri": None})
    assert _state(uri) == "superseded"
    metadb.quarantine_object_attempt(uri, "test cleanup")


def test_catalog_overwrite_keeps_stable_identity_and_governance():
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/curated.parquet"
    first = _handle("sink", logical=logical)
    second = _handle("sink", logical=logical)
    _commit(first)
    metadb.catalog_upsert_entry(first["uri"], "curated", {
        "id": "tbl_curated_stable", "name": "curated", "uri": first["uri"],
        "folder": "gold/team", "owner": "data", "description": "curated contract",
        "tags": ["gold"], "columns": [{"name": "id", "type": "int64"}],
        "version": "v1",
    }, parents=["s3://source/one"], pipeline="canvas-v1")
    stable_id = metadb.catalog_get(first["uri"])["id"]
    metadb.catalog_set_declared_key(first["uri"], ["id"])
    metadb.catalog_set_embedding(first["uri"], "model", 1, b"\x00\x00\x80?")
    relationship = {
        "leftUri": first["uri"], "leftColumns": ["id"],
        "rightUri": "s3://other/table", "rightColumns": ["id"],
    }
    metadb.catalog_upsert_relationship("legacy-key", relationship)

    _commit(second)
    metadb.catalog_upsert_entry(second["uri"], "curated", {
        "id": "must-not-replace-stable-id", "name": "curated", "uri": second["uri"],
        "columns": [{"name": "id", "type": "int64"}, {"name": "v", "type": "string"}],
        "version": "v2",
    }, parents=["s3://source/two"], pipeline="canvas-v2")

    current = metadb.catalog_get(stable_id)
    assert current["uri"] == second["uri"]
    assert (current["folder"], current["owner"], current["description"], current["tags"]) == (
        "gold/team", "data", "curated contract", ["gold"])
    assert metadb.catalog_declared_keys([second["uri"]])[second["uri"]] == ["id"]
    rels = metadb.catalog_relationships()
    assert any(second["uri"] in (r.get("leftUri"), r.get("rightUri")) for r in rels)
    assert all(first["uri"] not in (r.get("leftUri"), r.get("rightUri")) for r in rels)
    edges = metadb.catalog_edges()
    assert {e["parent"] for e in edges if e["child"] == second["uri"]} >= {
        "s3://source/one", "s3://source/two"}
    assert metadb.catalog_embeddings_for("model") == [(second["uri"], b"\x00\x00\x80?")]
    with metadb.session() as session:
        logical_row = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.current_uri == second["uri"]))
        assert logical_row is not None
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical_row.logical_id})
        assert ref.attempt_uri == second["uri"]
    assert _state(first["uri"]) == "superseded"
    metadb.catalog_delete_entry(second["uri"])
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


def test_read_lease_wins_or_observes_explicit_gc_miss():
    reader_first = _handle()
    _commit(reader_first)
    key = f"lease-{uuid.uuid4().hex}"
    metadb.put_result(key, {"uri": reader_first["uri"]})
    lease = metadb.acquire_object_attempt_lease(reader_first["uri"], "read", "reader", 30)
    metadb.put_result(key, {"uri": None})
    assert not any(item["uri"] == reader_first["uri"] for item in
                   metadb.object_attempt_gc_batch(0, 0))
    metadb.release_object_attempt_lease(lease)
    action = next(item for item in metadb.object_attempt_gc_batch(0, 0)
                  if item["uri"] == reader_first["uri"])
    assert action["action"] == "delete"

    gc_first = _handle()
    _commit(gc_first)
    key2 = f"lease-{uuid.uuid4().hex}"
    metadb.put_result(key2, {"uri": gc_first["uri"]})
    metadb.put_result(key2, {"uri": None})
    next(item for item in metadb.object_attempt_gc_batch(0, 0) if item["uri"] == gc_first["uri"])
    with pytest.raises(FileNotFoundError, match="unavailable"):
        metadb.acquire_object_attempt_lease(gc_first["uri"], "read", "late-reader", 30)
    metadb.quarantine_object_attempt(reader_first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(gc_first["uri"], "test cleanup")


def test_result_cache_pin_keeps_replaced_generation_published_until_run_owns_it():
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/cached-region.parquet"
    old = _handle(logical=logical)
    new = _handle(logical=logical)
    _commit(old)
    _commit(new)
    cache_key = f"cache-pin-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, {"uri": old["uri"], "rows": 1})
    doc, pin_id = metadb.acquire_result_cache_pin(cache_key, "pin-reader", 30)
    assert doc["uri"] == old["uri"] and pin_id

    metadb.put_result(cache_key, {"uri": new["uri"], "rows": 1})
    assert _state(old["uri"]) == "published"
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttemptRef, {
            "ref_type": "result_reader", "ref_key": pin_id}).attempt_uri == old["uri"]

    run_id = f"cache-pin-run-{uuid.uuid4().hex}"
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "output_uri": old["uri"]})
    metadb.release_result_cache_pin(pin_id)
    assert _state(old["uri"]) == "published", "terminal RunState replaced the temporary owner"
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttemptRef, {
            "ref_type": "result_reader", "ref_key": pin_id}) is None
        assert session.get(metadb.ObjectAttemptLease, pin_id) is None

    metadb.save_run_state(run_id, {"run_id": run_id, "status": "failed"})
    assert _state(old["uri"]) == "superseded"
    metadb.put_result(cache_key, {"uri": None})
    metadb.quarantine_object_attempt(old["uri"], "test cleanup")
    metadb.quarantine_object_attempt(new["uri"], "test cleanup")


def test_done_run_state_is_primary_owner_for_noncacheable_committed_region():
    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/noncacheable.parquet")
    _commit(handle)
    run_id = f"noncacheable-{uuid.uuid4().hex}"
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "output_uri": handle["uri"]},
        publish_region=True)
    assert _state(handle["uri"]) == "published"
    with metadb.session() as session:
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "run_state", "ref_key": run_id})
        assert ref is not None and ref.attempt_uri == handle["uri"]
    metadb.save_run_state(run_id, {"run_id": run_id, "status": "failed"})
    assert _state(handle["uri"]) == "superseded"
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_local_runner_terminal_persistence_failure_never_exposes_done(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from hub import compiler
    from hub.deps import get_deps
    from hub.models import ColumnSchema, Graph
    from hub.plugins.runner import LocalRunner

    deps = get_deps()
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1]}), source)
    graph = Graph.model_validate({
        "id": "local-persist-failure", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"config": {"uri": str(source)}}}],
        "edges": [],
    })
    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/cached.parquet")
    _commit(handle)

    class CachedAdapter:
        @staticmethod
        def schema(_uri):
            return [ColumnSchema(name="value", type="BIGINT")]

    runner = LocalRunner(
        lambda uri: CachedAdapter() if uri == handle["uri"] else deps.resolve_adapter(uri),
        deps.registry, deps.catalog, str(tmp_path), node_builders=deps.node_builders,
        node_specs=deps.node_specs)
    phash = runner._plan_hash(graph, "source")
    metadb.put_result(phash, {"uri": handle["uri"], "rows": 1, "table": None})
    runner.result_acquire = metadb.acquire_result_cache_pin
    runner.on_status = lambda _graph, status: (
        (_ for _ in ()).throw(RuntimeError("terminal persistence unavailable"))
        if status.status == "done" else None)
    plan = compiler.compile_plan(
        graph, "source", deps.registry, deps.node_specs, deps.node_ir)
    started = runner.run(plan, graph, "source", "local")
    deadline = time.monotonic() + 5
    while runner.status(started.run_id).status not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = runner.status(started.run_id)
    assert final.status == "failed" and final.output_uri is None
    assert "terminal persistence unavailable" in (final.error or "")
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
    metadb.put_result(phash, {"uri": None})
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_long_reader_renews_beyond_initial_ttl_and_blocks_gc():
    handle = _handle()
    _commit(handle)
    key = f"renew-{uuid.uuid4().hex}"
    metadb.put_result(key, {"uri": handle["uri"]})
    with handoff.managed_read_lease(handle["uri"], owner="slow-reader", ttl_seconds=1) as guard:
        metadb.put_result(key, {"uri": None})
        time.sleep(2.35)  # past the requested TTL and SQLite's one-second DB-clock resolution margin
        guard.check()
        assert not any(item["uri"] == handle["uri"] for item in
                       metadb.object_attempt_gc_batch(0, 0))
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_allocation_generation_and_stale_reaper_epoch_are_aba_safe():
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/aba.parquet"
    run_id, key = f"run-{uuid.uuid4().hex}", f"allocation-{uuid.uuid4().hex}"
    first = _handle(logical=logical, run_id=run_id, allocation_key=key)
    same = _handle(logical=logical, run_id=run_id, allocation_key=key)
    assert same["uri"] == first["uri"] and same["generation"] == 1
    inventory = _inventory(first)
    assert metadb.mark_object_attempt_terminal(first["uri"], quiet_seconds=0)
    assert metadb.observe_object_attempt_inventory(first["uri"], inventory, quiet_seconds=0) == "observed"
    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, first["uri"]).quiet_until = \
            metadb._db_now(session) - datetime.timedelta(seconds=1)
    assert metadb.observe_object_attempt_inventory(first["uri"], inventory, quiet_seconds=0) == "complete"
    stale = next(item for item in metadb.object_attempt_gc_batch(0, 0)
                 if item["uri"] == first["uri"])

    second = _handle(logical=logical, run_id=run_id, allocation_key=key)
    assert second["generation"] == 2 and second["uri"] != first["uri"]
    assert second["namespace"] in second["uri"] and "-g2-" in second["uri"]

    with metadb.session() as session:
        old = session.get(metadb.ObjectAttempt, first["uri"])
        old.delete_lease_expires_at = metadb._db_now(session) - datetime.timedelta(seconds=1)
        for lease in session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == first["uri"])):
            lease.expires_at = old.delete_lease_expires_at
    current = next(item for item in metadb.object_attempt_gc_batch(0, 0)
                   if item["uri"] == first["uri"])
    assert current["delete_epoch"] > stale["delete_epoch"]
    assert metadb.validate_object_attempt_delete(stale) is False
    assert metadb.validate_object_attempt_delete(current) is True
    assert _state(second["uri"]) == "writing"
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


def test_unrelated_attempt_writes_do_not_take_installation_lock(monkeypatch):
    left, right = _handle(), _handle()
    _commit(left)
    _commit(right)
    monkeypatch.setattr(
        metadb, "_lock_object_attempt_registry",
        lambda _session: (_ for _ in ()).throw(AssertionError("global installation lock used")),
    )
    errors = []

    def publish(handle, key):
        try:
            metadb.put_result(key, {"uri": handle["uri"]})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=publish, args=(left, f"left-{uuid.uuid4().hex}")),
        threading.Thread(target=publish, args=(right, f"right-{uuid.uuid4().hex}")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _state(left["uri"]) == _state(right["uri"]) == "published"


class _ExactProvider:
    complete_inventory = True
    conditional_namespace_claims = True
    _claims = {}
    _claim_counter = 0

    def __init__(self, members: list[dict], fail_after_delete: int | None = None):
        self.members = {item["member_id"]: dict(item) for item in members}
        self.fail_after_delete = fail_after_delete
        self.calls = []

    def inventory(self, _uri: str) -> list[dict]:
        return [dict(item) for item in self.members.values()]

    def delete_exact(self, _uri: str, member: dict) -> None:
        self.calls.append(member["member_id"])
        self.members.pop(member["member_id"], None)
        if self.fail_after_delete and len(self.calls) == self.fail_after_delete:
            raise RuntimeError("simulated crash after exact delete")

    def read_namespace_claim(self, uri: str, namespace: str):
        return self._claims.get((urlsplit(uri).netloc, namespace))

    def write_namespace_claim(self, uri: str, namespace: str, body: bytes,
                              expected_etag: str | None):
        key = (urlsplit(uri).netloc, namespace)
        current = self._claims.get(key)
        if (current is None and expected_etag is not None) or (
                current is not None and current["etag"] != expected_etag):
            raise handoff.NamespaceClaimConflict("claim conflict")
        if current is not None and expected_etag is None:
            raise handoff.NamespaceClaimConflict("claim conflict")
        type(self)._claim_counter += 1
        etag = f'"claim-{type(self)._claim_counter}"'
        type(self)._claims[key] = {"doc": json.loads(body), "etag": etag}
        return etag


def _make_delete_pending(handle: dict, inventory: list[dict]) -> None:
    assert metadb.mark_object_attempt_terminal(handle["uri"], quiet_seconds=0)
    assert metadb.observe_object_attempt_inventory(handle["uri"], inventory, quiet_seconds=0) == "observed"
    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, handle["uri"]).quiet_until = \
            metadb._db_now(session) - datetime.timedelta(seconds=1)
    assert metadb.observe_object_attempt_inventory(handle["uri"], inventory, quiet_seconds=0) == "complete"


def _force_delete_verification_due(uri: str) -> None:
    with metadb.session() as session:
        row = session.get(metadb.ObjectAttempt, uri)
        past = metadb._db_now(session) - datetime.timedelta(seconds=2)
        row.next_delete_at = row.delete_empty_observed_at = past


def test_exact_delete_restarts_after_nth_key_without_prefix_delete():
    handle = _handle("sink")
    inventory = _inventory(handle)
    _make_delete_pending(handle, inventory)
    provider = _ExactProvider(inventory, fail_after_delete=2)
    handoff.set_managed_object_provider(provider)
    try:
        first = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert first["deleted"] == [] and len(provider.members) == 0
        with metadb.session() as session:
            row = session.get(metadb.ObjectAttempt, handle["uri"])
            assert row.state == "delete_pending"
            row.next_delete_at = metadb._db_now(session) - datetime.timedelta(seconds=1)
        second = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert second["deleted"] == [] and _state(handle["uri"]) == "delete_verifying"
        _force_delete_verification_due(handle["uri"])
        third = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert third["deleted"] == [handle["uri"]]
        with metadb.session() as session:
            row = session.get(metadb.ObjectAttempt, handle["uri"])
            assert row.state == "deleted" and row.deleted_at is not None
            assert session.get(metadb.ObjectAttemptAllocation, handle["allocation_key"]) is not None
        assert provider.calls == [inventory[1]["member_id"], inventory[0]["member_id"]]
        assert not hasattr(provider, "delete_dir") and not hasattr(provider, "delete_prefix")
    finally:
        handoff.set_managed_object_provider(None)


@pytest.mark.parametrize("mutation", ["extra", "etag", "incomplete-provider"])
def test_inventory_uncertainty_quarantines_without_deletion(mutation):
    handle = _handle()
    inventory = _inventory(handle, version_id="v1")
    _make_delete_pending(handle, inventory)
    actual = [dict(item) for item in inventory]
    provider = _ExactProvider(actual)
    if mutation == "extra":
        extra = dict(actual[0])
        extra["key"] = extra["key"].rsplit("/", 1)[0] + "/unexpected.parquet"
        extra["member_id"] = handoff._member_id(
            extra["member_type"], extra["key"], extra["version_id"])
        provider.members[extra["member_id"]] = extra
    elif mutation == "etag":
        provider.members[actual[0]["member_id"]]["etag"] = "changed"
    else:
        provider.complete_inventory = False
    handoff.set_managed_object_provider(provider)
    try:
        result = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert result["quarantined"] == [handle["uri"]]
        assert _state(handle["uri"]) == "quarantined"
        assert provider.calls == []
    finally:
        handoff.set_managed_object_provider(None)


def test_namespace_mismatch_fails_closed_and_clone_isolation_is_explicit(tmp_path, monkeypatch):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    isolated_url = f"sqlite:///{tmp_path / 'namespace.db'}"
    monkeypatch.setattr(settings, "database_url", isolated_url)
    metadb._engine = metadb._Session = None
    try:
        metadb.init_db()
        current = metadb.object_storage_namespace()
        original_owner = metadb.object_attempt_owner_id()
        monkeypatch.setenv("DP_STORAGE_NAMESPACE", "wrong-clone-namespace")
        with pytest.raises(RuntimeError, match="does not match"):
            metadb.object_storage_namespace()
        monkeypatch.delenv("DP_STORAGE_NAMESPACE")

        active = _handle()
        replacement = f"isolated-{uuid.uuid4().hex[:16]}"
        assert metadb.isolate_cloned_object_storage(current, replacement) == replacement
        assert _state(active["uri"]) == "quarantined"
        assert metadb.object_attempt_owner_id() != original_owner
        monkeypatch.setenv("DP_STORAGE_NAMESPACE", replacement)
        assert metadb.object_storage_namespace() == replacement
        monkeypatch.delenv("DP_STORAGE_NAMESPACE")
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        metadb._engine, metadb._Session = original_engine, original_session


def test_namespace_claim_response_loss_converges_to_committed_marker():
    class ResponseLostProvider(_ExactProvider):
        lost = True

        def write_namespace_claim(self, uri, namespace, body, expected_etag):
            etag = super().write_namespace_claim(uri, namespace, body, expected_etag)
            if self.lost:
                self.lost = False
                raise ConnectionError("marker response lost")
            return etag

    provider = ResponseLostProvider([])
    uri = f"s3://namespace-response-loss-{uuid.uuid4().hex}/root/out.attempt-test"
    namespace = metadb.object_storage_namespace()
    scope = hashlib.sha256(
        f"s3://{urlsplit(uri).netloc}".encode()).hexdigest()
    handoff.set_managed_object_provider(provider)
    try:
        handoff.ensure_storage_namespace_claim(uri, namespace)
        claim = metadb.object_storage_claim(namespace, scope)
        assert claim is not None
        marker = provider.read_namespace_claim(uri, namespace)
        assert marker["etag"] == claim["marker_etag"]
        assert marker["doc"]["claimToken"] == claim["claim_token"]
        handoff.ensure_storage_namespace_claim(uri, namespace)
    finally:
        handoff.set_managed_object_provider(None)


def test_repeated_published_commit_validation_never_regresses_state():
    handle = _handle()
    inventory = _commit(handle)
    key = f"repeat-{uuid.uuid4().hex}"
    metadb.put_result(key, {"uri": handle["uri"]})
    metadb.record_object_attempt_commit(handle["uri"], inventory)
    assert _state(handle["uri"]) == "published"
    changed = [dict(item) for item in inventory]
    changed[0]["etag"] = "changed"
    with pytest.raises(RuntimeError, match="inventory changed"):
        metadb.record_object_attempt_commit(handle["uri"], changed)
    assert _state(handle["uri"]) == "published"


def test_failed_partial_late_key_and_escaped_inventory_quarantine():
    late = _handle()
    original = _inventory(late)
    assert metadb.mark_object_attempt_terminal(late["uri"], quiet_seconds=0)
    assert metadb.observe_object_attempt_inventory(late["uri"], original, quiet_seconds=0) == "observed"
    changed = [dict(item) for item in original]
    extra = dict(changed[0])
    extra["key"] = extra["key"].rsplit("/", 1)[0] + "/late.parquet"
    changed.append(extra)
    assert metadb.observe_object_attempt_inventory(late["uri"], changed, quiet_seconds=0) == "quarantined"

    escaped = _handle()
    exact = _inventory(escaped)
    _make_delete_pending(escaped, exact)
    outside = dict(exact[0])
    outside["key"] = "lifecycle-tests/a-different-generation/foreign.parquet"
    provider = _ExactProvider([*exact, outside])
    handoff.set_managed_object_provider(provider)
    try:
        result = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert result["quarantined"] == [escaped["uri"]]
        assert _state(escaped["uri"]) == "quarantined"
        assert provider.calls == []
    finally:
        handoff.set_managed_object_provider(None)


def test_two_reapers_claim_one_delete_epoch_owner():
    handle = _handle()
    inventory = _inventory(handle)
    _make_delete_pending(handle, inventory)
    barrier = threading.Barrier(3)
    batches = []

    def claim():
        barrier.wait()
        batches.append(metadb.object_attempt_gc_batch(0, 0))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    actions = [item for batch in batches for item in batch if item["uri"] == handle["uri"]]
    assert len(actions) == 1
    assert actions[0]["delete_epoch"] == 1 and actions[0]["delete_owner"]
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_terminal_allocation_never_reopens_writer_and_new_run_advances_generation():
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/new-run.parquet"
    allocation_key = f"same-allocation-{uuid.uuid4().hex}"
    first = _handle(logical=logical, run_id="run-one", allocation_key=allocation_key)
    _commit(first)
    second = _handle(logical=logical, run_id="run-two", allocation_key=allocation_key)
    assert second["generation"] == 2 and second["uri"] != first["uri"]
    assert _state(first["uri"]) == "committed"
    lookup = metadb.lookup_object_attempt(
        allocation_key=allocation_key, logical_uri=logical, kind="region", run_id="run-two")
    assert lookup["uri"] == second["uri"] and lookup["write_lease_id"] is None
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


def test_commit_crash_publish_lease_expires_and_secondary_pointers_fail_closed():
    handle = _handle()
    _commit(handle)
    uri = handle["uri"]
    with metadb.session() as session:
        leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == uri)))
        assert [lease.lease_type for lease in leases] == ["publish"]

    canvas_id = f"commit-crash-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="commit crash",
            version=1, doc="{}"))
    with pytest.raises(RuntimeError, match="only a published"):
        metadb.record_run(canvas_id, "n", "done", run_id="secondary", output_uri=uri)
    with pytest.raises(RuntimeError, match="only a published"):
        metadb.save_run_state(
            "secondary", {"run_id": "secondary", "status": "done", "output_uri": uri})

    with metadb.session() as session:
        row = session.get(metadb.ObjectAttempt, uri)
        past = metadb._db_now(session) - datetime.timedelta(seconds=100)
        row.terminal_proof_at = past
        for lease in session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == uri)):
            lease.expires_at = past
    assert metadb.object_attempt_gc_batch(10, 1000) == []
    assert _state(uri) == "abandoned"
    metadb.quarantine_object_attempt(uri, "test cleanup")


def test_attempt_shaped_uri_without_registry_row_is_never_unmanaged():
    uri = f"s3://lifecycle-tests/{uuid.uuid4().hex}/out.attempt-unregistered"
    inventory = [{
        "member_id": handoff._member_id("unversioned_object", "x/y", "null"),
        "key": "x/y", "member_type": "unversioned_object", "etag": None,
        "version_id": None, "upload_id": None, "size": 1,
        "is_latest": True, "is_commit": False,
    }]
    with pytest.raises(RuntimeError, match="no lifecycle ownership row"):
        metadb.record_object_attempt_commit(uri, inventory)
    with pytest.raises(RuntimeError, match="no lifecycle ownership row"):
        handoff.prepare_attempt_commit(uri)
    with pytest.raises(RuntimeError, match="no lifecycle ownership row"):
        metadb.catalog_upsert_entry(uri, "out", {"id": "tbl_out", "name": "out", "uri": uri})


def test_published_receipt_retry_converges_without_reprobe(monkeypatch, tmp_path):
    from hub.plugins.catalog import InMemoryCatalog

    handle = _handle("sink")
    _commit(handle)
    metadb.catalog_upsert_entry(
        handle["uri"], "receipt", {"id": "ignored", "name": "receipt", "uri": handle["uri"]})
    catalog = InMemoryCatalog(str(tmp_path), lambda _uri: None)
    monkeypatch.setattr(
        catalog, "_add",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry reprobed")))
    receipt = catalog.publish_managed_output("receipt", handle["uri"])
    assert receipt["uri"] == handle["uri"] and receipt["generation"] == handle["generation"]
    assert _state(handle["uri"]) == "published"
    metadb.catalog_delete_entry(handle["uri"])
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_public_read_rejects_committed_but_internal_validation_can_read():
    from fastapi import HTTPException
    from hub.models import SampleRequest
    from hub.routers.catalog import data_sample

    handle = _handle()
    _commit(handle)
    with pytest.raises(FileNotFoundError, match="state=committed"):
        with handoff.managed_read_lease(handle["uri"], owner="public"):
            pass
    with handoff.managed_read_lease(
            handle["uri"], owner="region-validation", allow_committed=True):
        pass
    with pytest.raises(HTTPException) as caught:
        data_sample(SampleRequest(uri=handle["uri"], k=1))
    assert caught.value.status_code == 410
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_publication_response_loss_converges_to_exact_receipt(monkeypatch, tmp_path):
    from hub.models import ColumnSchema
    from hub.plugins.catalog import InMemoryCatalog

    handle = _handle("sink")
    _commit(handle)
    calls = {"schema": 0, "count": 0, "fingerprint": 0}

    class Probe:
        def schema(self, _uri):
            calls["schema"] += 1
            return [ColumnSchema(name="x", type="BIGINT")]

        def count(self, _uri):
            calls["count"] += 1
            return 1

        def fingerprint(self, _uri):
            calls["fingerprint"] += 1
            return "response-loss"

    catalog = InMemoryCatalog(str(tmp_path), lambda _uri: Probe())
    monkeypatch.setattr(handoff, "prepare_attempt_commit", lambda _uri: None)
    original_add = catalog._add

    def commit_then_lose_response(*args, **kwargs):
        original_add(*args, **kwargs)
        raise ConnectionError("commit response lost")

    monkeypatch.setattr(catalog, "_add", commit_then_lose_response)
    receipt = catalog.publish_managed_output(
        "response-loss", handle["uri"], parents=["s3://external/parent"])
    assert receipt["uri"] == handle["uri"]
    assert receipt["generation"] == handle["generation"]
    assert calls == {"schema": 1, "count": 1, "fingerprint": 1}
    assert _state(handle["uri"]) == "published"
    assert len([edge for edge in metadb.catalog_edges()
                if edge["child"] == handle["uri"]]) == 1
    metadb.catalog_delete_entry(handle["uri"])
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_inactive_parent_stable_key_is_preserved_only_as_raw_historical_lineage():
    token = uuid.uuid4().hex
    parent_logical = f"s3://lifecycle-tests/{token}/parent.parquet"
    child_logical = f"s3://lifecycle-tests/{token}/child.parquet"
    parent = _handle("sink", logical=parent_logical)
    child = _handle("sink", logical=child_logical)
    _commit(parent)
    metadb.catalog_upsert_entry(parent["uri"], "parent", {
        "id": "ignored", "name": "parent", "uri": parent["uri"]})
    stable_id = metadb.catalog_get(parent["uri"])["id"]
    metadb.catalog_delete_entry(parent["uri"])

    _commit(child)
    metadb.catalog_upsert_entry(child["uri"], "child", {
        "id": "ignored", "name": "child", "uri": child["uri"],
    }, parents=[stable_id], pipeline="historical")
    assert {edge["parent"] for edge in metadb.catalog_edges()
            if edge["child"] == child["uri"]} == {parent_logical}

    replacement = _handle("sink", logical=parent_logical)
    _commit(replacement)
    metadb.catalog_upsert_entry(replacement["uri"], "parent", {
        "id": "ignored", "name": "parent", "uri": replacement["uri"]})
    assert replacement["uri"] not in {edge["parent"] for edge in metadb.catalog_edges()
                                      if edge["child"] == child["uri"]}
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.CatalogEdge).where(
            metadb.CatalogEdge.parent == stable_id)))

    metadb.catalog_delete_entry(child["uri"])
    metadb.catalog_delete_entry(replacement["uri"])
    for handle in (parent, child, replacement):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_old_parent_attempt_epoch_never_attaches_lineage_to_fresh_registration():
    token = uuid.uuid4().hex
    parent_logical = f"s3://lifecycle-tests/{token}/epoch-parent.parquet"
    child_logical = f"s3://lifecycle-tests/{token}/epoch-child.parquet"
    old_parent = _handle("sink", logical=parent_logical)
    _commit(old_parent)
    metadb.catalog_upsert_entry(old_parent["uri"], "epoch-parent", {
        "id": "ignored", "name": "epoch-parent", "uri": old_parent["uri"]})
    stable_id = metadb.catalog_get(old_parent["uri"])["id"]
    metadb.catalog_delete_entry(old_parent["uri"])

    fresh_parent = _handle("sink", logical=parent_logical)
    _commit(fresh_parent)
    metadb.catalog_upsert_entry(fresh_parent["uri"], "epoch-parent", {
        "id": "ignored", "name": "epoch-parent", "uri": fresh_parent["uri"]})
    child = _handle("sink", logical=child_logical)
    _commit(child)
    metadb.catalog_upsert_entry(child["uri"], "epoch-child", {
        "id": "ignored", "name": "epoch-child", "uri": child["uri"],
    }, parents=[old_parent["uri"]], pipeline="late-old-epoch")

    child_parents = {edge["parent"] for edge in metadb.catalog_edges()
                     if edge["child"] == child["uri"]}
    assert child_parents == {old_parent["uri"]}
    assert fresh_parent["uri"] not in child_parents
    with metadb.session() as session:
        child_key = session.get(
            metadb.CatalogLogicalDataset,
            session.get(metadb.ObjectAttempt, child["uri"]).logical_id).catalog_key
        assert not list(session.scalars(select(metadb.CatalogEdge).where(
            metadb.CatalogEdge.parent == stable_id,
            metadb.CatalogEdge.child == child_key,
        )))

    metadb.catalog_delete_entry(child["uri"])
    metadb.catalog_delete_entry(fresh_parent["uri"])
    for handle in (old_parent, fresh_parent, child):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_logical_governance_late_writes_and_unregister_epoch_are_linearizable():
    logical = f"s3://lifecycle-tests/{uuid.uuid4().hex}/linear.parquet"
    first = _handle("sink", logical=logical, allocation_key=f"first-{uuid.uuid4().hex}")
    second = _handle("sink", logical=logical, allocation_key=f"second-{uuid.uuid4().hex}")
    stale = _handle("sink", logical=logical, allocation_key=f"stale-{uuid.uuid4().hex}")
    _commit(first)
    metadb.catalog_upsert_entry(
        first["uri"], "linear", {"id": "ignored", "name": "linear", "uri": first["uri"]})
    stable_id = metadb.catalog_get(first["uri"])["id"]
    _commit(second)
    metadb.catalog_upsert_entry(
        second["uri"], "linear", {"id": "ignored", "name": "linear", "uri": second["uri"]})

    # Requests that resolved the old physical URI before overwrite still mutate the logical dataset.
    metadb.catalog_set_metadata(first["uri"], "gold", "late-owner", "late", ["late"])
    metadb.catalog_set_declared_key(first["uri"], ["id"])
    with pytest.raises(RuntimeError, match="stale for the current publication"):
        metadb.catalog_set_embedding(first["uri"], "model", 1, b"old")
    metadb.catalog_set_embedding(second["uri"], "model", 1, b"v")
    metadb.catalog_add_edge("s3://external/source", first["uri"], "late")
    metadb.catalog_upsert_relationship("ignored", {
        "leftUri": first["uri"], "leftColumns": ["id"],
        "rightUri": "s3://external/right", "rightColumns": ["id"],
    })
    current = metadb.catalog_get(stable_id)
    assert current["uri"] == second["uri"]
    assert (current["folder"], current["owner"], current["tags"]) == (
        "gold", "late-owner", ["late"])
    assert metadb.catalog_declared_keys([second["uri"]])[second["uri"]] == ["id"]
    assert (second["uri"], b"v") in metadb.catalog_embeddings_for("model")
    assert any(edge["child"] == second["uri"] for edge in metadb.catalog_edges())
    assert any((rel.get("leftUri") or rel.get("left_uri")) == second["uri"]
               for rel in metadb.catalog_relationships())

    # A stale unregister token removes the current logical dataset and fences pre-existing attempts.
    metadb.catalog_delete_entry(first["uri"])
    assert metadb.catalog_get(stable_id) is None
    assert not any(edge["child"] in (first["uri"], second["uri"], stable_id)
                   for edge in metadb.catalog_edges())
    assert not any(stable_id in (rel.get("leftUri"), rel.get("rightUri"))
                   for rel in metadb.catalog_relationships())
    stale_mutations = (
        lambda: metadb.catalog_set_metadata(first["uri"], "ghost", None, None, []),
        lambda: metadb.catalog_set_declared_key(first["uri"], ["ghost"]),
        lambda: metadb.catalog_set_embedding(second["uri"], "model", 1, b"ghost"),
        lambda: metadb.catalog_add_edge("s3://external/ghost", first["uri"], "ghost"),
        lambda: metadb.catalog_upsert_relationship("ghost", {
            "leftUri": first["uri"], "leftColumns": ["ghost"],
            "rightUri": "s3://external/ghost", "rightColumns": ["ghost"],
        }),
    )
    for mutate in stale_mutations:
        with pytest.raises(RuntimeError, match="inactive"):
            mutate()
    _commit(stale)
    with pytest.raises(RuntimeError, match="fenced by catalog unregister"):
        metadb.catalog_upsert_entry(
            stale["uri"], "linear", {"id": "ignored", "name": "linear", "uri": stale["uri"]})
    metadb.abandon_committed_object_attempt(stale["uri"])

    fresh = _handle("sink", logical=logical, allocation_key=f"fresh-{uuid.uuid4().hex}")
    _commit(fresh)
    metadb.catalog_upsert_entry(
        fresh["uri"], "linear", {"id": "ignored", "name": "linear", "uri": fresh["uri"]})
    assert metadb.catalog_get(stable_id)["uri"] == fresh["uri"]
    for mutate in stale_mutations:
        with pytest.raises(RuntimeError, match="fenced by unregister"):
            mutate()
    with pytest.raises(RuntimeError, match="fenced by unregister"):
        metadb.catalog_delete_entry(first["uri"])
    from hub.deps import get_deps
    with pytest.raises(RuntimeError, match="fenced by unregister"):
        get_deps().catalog.unregister(first["uri"])
    assert metadb.catalog_get(stable_id)["uri"] == fresh["uri"]
    assert metadb.catalog_declared_keys([fresh["uri"]]) == {}
    assert not any(vec == b"ghost" for _uri, vec in metadb.catalog_embeddings_for("model"))
    assert not any(edge["child"] == fresh["uri"] for edge in metadb.catalog_edges())
    assert not any(fresh["uri"] in (
        rel.get("leftUri"), rel.get("left_uri"), rel.get("rightUri"), rel.get("right_uri"))
        for rel in metadb.catalog_relationships())
    metadb.catalog_delete_entry(fresh["uri"])
    for handle in (first, second, stale, fresh):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_final_empty_barrier_quarantines_a_late_member():
    handle = _handle()
    inventory = _inventory(handle)
    _make_delete_pending(handle, inventory)
    provider = _ExactProvider(inventory)
    handoff.set_managed_object_provider(provider)
    try:
        first = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert handle["uri"] in first["observed"]
        assert _state(handle["uri"]) == "delete_verifying"
        late = dict(inventory[0])
        late["key"] = late["key"].rsplit("/", 1)[0] + "/late.parquet"
        late["member_id"] = handoff._member_id(
            late["member_type"], late["key"], late.get("version_id") or "null")
        provider.members[late["member_id"]] = late
        _force_delete_verification_due(handle["uri"])
        second = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert second["quarantined"] == [handle["uri"]]
        assert _state(handle["uri"]) == "quarantined"
    finally:
        handoff.set_managed_object_provider(None)


def test_isolated_clone_cannot_take_original_namespace_or_inherited_visibility(tmp_path):
    import shutil

    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    url_a = f"sqlite:///{tmp_path / 'clone-a.db'}"
    url_b = f"sqlite:///{tmp_path / 'clone-b.db'}"
    provider = _ExactProvider([])
    claim_uri = "s3://clone-claim-bucket/root/out.attempt-clone-cas"

    def switch(url):
        if metadb._engine is not None and metadb._engine is not original_engine:
            metadb._engine.dispose()
        settings.database_url = url
        metadb._engine = metadb._Session = None

    handoff.set_managed_object_provider(provider)
    try:
        switch(url_a)
        metadb.init_db()
        namespace = metadb.object_storage_namespace()
        owner = metadb.object_attempt_owner_id()
        handoff.ensure_storage_namespace_claim(claim_uri, namespace)
        scope = hashlib.sha256(b"s3://clone-claim-bucket").hexdigest()
        sink = _handle(
            "sink", logical="s3://clone-claim-bucket/root/sink.parquet",
            allocation_key=f"clone-sink-{uuid.uuid4().hex}")
        _commit(sink)
        metadb.catalog_upsert_entry(
            sink["uri"], "clone-sink",
            {"id": "clone-sink", "name": "clone-sink", "uri": sink["uri"]})
        region = _handle(
            logical="s3://clone-claim-bucket/root/region.parquet",
            allocation_key=f"clone-region-{uuid.uuid4().hex}")
        _commit(region)
        cache_key = f"clone-cache-{uuid.uuid4().hex}"
        metadb.put_result(cache_key, {"uri": region["uri"], "rows": 1})
        canvas_id = f"clone-canvas-{uuid.uuid4().hex}"
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
                name="clone history", version=1, doc="{}"))
        metadb.record_run(
            canvas_id, "n", "done", run_id=f"clone-run-{uuid.uuid4().hex}",
            output_uri=region["uri"])
        inherited_writing = _handle(
            logical="s3://clone-claim-bucket/root/writing.parquet",
            allocation_key=f"clone-writing-{uuid.uuid4().hex}")
        inherited_committed = _handle(
            logical="s3://clone-claim-bucket/root/committed.parquet",
            allocation_key=f"clone-committed-{uuid.uuid4().hex}")
        _commit(inherited_committed)
        legacy_namespace = f"legacy-{uuid.uuid4().hex[:20]}"
        with metadb.session() as session:
            session.get(
                metadb.ObjectAttempt, inherited_committed["uri"]).storage_namespace = legacy_namespace
            claim = session.get(metadb.ObjectStorageClaim, {
                "storage_namespace": namespace, "storage_scope": scope})
            assert claim is not None

        if metadb._engine is not None:
            metadb._engine.dispose()
        metadb._engine = metadb._Session = None
        shutil.copyfile(tmp_path / "clone-a.db", tmp_path / "clone-b.db")

        switch(url_b)
        metadb.init_db()
        replacement = f"clone-{uuid.uuid4().hex[:20]}"
        assert metadb.isolate_cloned_object_storage(namespace, replacement) == replacement
        with metadb.session() as session:
            ident = session.get(metadb.InstallationIdentity, 1)
            assert (ident.owner_token, ident.storage_namespace) != (owner, namespace)
            assert session.scalar(select(func.count()).select_from(
                metadb.ObjectStorageClaim)) == 0
            inherited = list(session.scalars(select(metadb.ObjectAttempt)))
            assert inherited and {row.state for row in inherited} == {"quarantined"}
            assert {inherited_writing["uri"], inherited_committed["uri"]} <= {
                row.uri for row in inherited}
            assert any(row.storage_namespace == legacy_namespace for row in inherited)
            assert session.scalar(select(func.count()).select_from(
                metadb.ObjectAttemptRef)) == 0
            record = session.scalar(select(metadb.RunRecord).where(
                metadb.RunRecord.output_uri == region["uri"]))
            assert record is not None
        assert metadb.get_result(cache_key) is None
        assert metadb.catalog_get(sink["uri"]) is None
        with pytest.raises(RuntimeError, match="not owned"):
            handoff.ensure_storage_namespace_claim(claim_uri, namespace)
        clone_uri = "s3://clone-claim-bucket/root/out.attempt-isolated-clone"
        handoff.ensure_storage_namespace_claim(clone_uri, replacement)

        # Clone isolation writes only a new marker. The original DB retains its old marker, data
        # visibility, and allocation authority.
        switch(url_a)
        handoff.ensure_storage_namespace_claim(claim_uri, namespace)
        assert metadb.object_attempt_owner_id() == owner
        assert metadb.catalog_get(sink["uri"])["uri"] == sink["uri"]
        assert metadb.get_result(cache_key)["uri"] == region["uri"]
        new_logical = f"s3://clone-claim-bucket/root/{uuid.uuid4().hex}.parquet"
        new_handle = handoff.allocate_attempt(
            logical_uri=new_logical, kind="region", run_id="original-still-active",
            allocation_key=f"original-{uuid.uuid4().hex}",
            uri_factory=lambda ns, generation, attempt_id: handoff.physical_attempt_uri(
                new_logical, ns, generation, attempt_id),
        )
        assert new_handle["namespace"] == namespace
        metadb.quarantine_object_attempt(new_handle["uri"], "test cleanup")
        assert provider._claims[("clone-claim-bucket", namespace)]["doc"]["ownerToken"] == owner
        assert provider._claims[(
            "clone-claim-bucket", replacement)]["doc"]["ownerToken"] != owner
    finally:
        if metadb._engine is not None and metadb._engine is not original_engine:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        handoff.set_managed_object_provider(None)


def test_app_lifespan_starts_and_stops_object_reaper():
    from fastapi.testclient import TestClient
    from hub import main

    def live_reapers():
        return [thread for thread in threading.enumerate()
                if thread.name == "dp-object-attempt-reaper" and thread.is_alive()]

    before = len(live_reapers())
    with TestClient(main.app):
        assert len(live_reapers()) == before + 1
        assert main._object_attempt_reaper_thread is not None
    assert len(live_reapers()) == before
    assert main._object_attempt_reaper_thread is None


def test_moto_versioned_s3_history_read_and_exact_sibling_safe_gc(monkeypatch):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from moto.server import ThreadedMotoServer

    from hub.models import SampleRequest
    from hub import db
    from hub.deps import get_deps
    from hub.plugins.adapters import object_fs
    from hub.routers.catalog import data_sample

    server = ThreadedMotoServer(port=0)
    server.start()
    cache_key = f"moto-cache-{uuid.uuid4().hex}"
    canvas_id = f"moto-canvas-{uuid.uuid4().hex}"
    second = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket="lifecycle-versioned")
        client.put_bucket_versioning(
            Bucket="lifecycle-versioned", VersioningConfiguration={"Status": "Enabled"})
        metadb.set_setting("objectStore", {
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s", "useSsl": False,
        }, "global")

        logical = f"s3://lifecycle-versioned/results/{uuid.uuid4().hex}.parquet"

        def land(handle, value, *, with_version_history=False):
            fs, path = object_fs(handle["uri"])
            table = pa.table({"value": [value]})
            if with_version_history:
                with fs.open_output_stream(path.rstrip("/") + "/part-00000.parquet") as stream:
                    pq.write_table(pa.table({"value": [-1]}), stream)
                client.delete_object(
                    Bucket="lifecycle-versioned",
                    Key=urlsplit(handle["uri"]).path.lstrip("/").rstrip("/")
                    + "/part-00000.parquet")
            with fs.open_output_stream(path.rstrip("/") + "/part-00000.parquet") as stream:
                pq.write_table(table, stream)
            handoff.write_manifest(
                handle["uri"], run_id=handle["run_id"], rows=1, schema=table.schema)
            handoff.prepare_attempt_commit(handle["uri"])

        first = _handle(logical=logical, allocation_key=f"moto-first-{uuid.uuid4().hex}")
        land(first, 1, with_version_history=True)
        captured = metadb.object_attempt_inventory(first["uri"])
        assert sum(item["member_type"] == "object_version" for item in captured) >= 3
        assert any(item["member_type"] == "delete_marker" for item in captured)
        metadb.put_result(cache_key, {"uri": first["uri"], "rows": 1})
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="moto history",
                version=1, doc="{}"))
        run_id = f"moto-run-{uuid.uuid4().hex}"
        metadb.record_run(canvas_id, "n", "done", run_id=run_id, output_uri=first["uri"])
        metadb.save_run_state(
            run_id, {"run_id": run_id, "status": "done", "output_uri": first["uri"]},
            canvas_id=canvas_id)

        second = _handle(logical=logical, allocation_key=f"moto-second-{uuid.uuid4().hex}")
        land(second, 2)
        metadb.put_result(cache_key, {"uri": second["uri"], "rows": 1})
        assert _state(first["uri"]) == "published", "run history must keep the old version readable"
        class _HistoryAdapter:
            def scan(self, _uri, _columns=None, limit=None):
                rel = db.conn().from_arrow(pa.table({"value": [1]}))
                return rel.limit(limit) if limit is not None else rel

            def count(self, _uri):
                return 1

        deps = get_deps()
        original_resolve = deps.resolve_adapter
        monkeypatch.setattr(
            deps, "resolve_adapter",
            lambda uri: _HistoryAdapter() if uri == first["uri"] else original_resolve(uri))
        sample = data_sample(SampleRequest(uri=first["uri"], k=10))
        assert sample.rows == [{"value": 1}]

        sibling = logical.rsplit("/", 1)[0] + "/manual-sibling.parquet"
        sibling_key = urlsplit(sibling).path.lstrip("/")
        client.put_object(Bucket="lifecycle-versioned", Key=sibling_key, Body=b"keep")
        metadb.delete_canvas_cascade(canvas_id)
        assert _state(first["uri"]) == "superseded"
        result = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert first["uri"] in result["observed"]
        assert _state(first["uri"]) == "delete_verifying"
        _force_delete_verification_due(first["uri"])
        assert first["uri"] in handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"]
        assert client.get_object(Bucket="lifecycle-versioned", Key=sibling_key)["Body"].read() == b"keep"

        first_prefix = urlsplit(first["uri"]).path.lstrip("/")
        versions = client.list_object_versions(
            Bucket="lifecycle-versioned", Prefix=first_prefix)
        assert versions.get("Versions", []) == [] and versions.get("DeleteMarkers", []) == []
        with metadb.session() as session:
            row = session.get(metadb.ObjectAttempt, first["uri"])
            assert row.state == "deleted", "ownership row remains as the tombstone"

        # Failed partial writers retain and abort exact multipart UploadIds before tombstoning.
        partial = _handle(
            logical=f"s3://lifecycle-versioned/results/{uuid.uuid4().hex}.parquet",
            allocation_key=f"moto-multipart-{uuid.uuid4().hex}")
        partial_key = urlsplit(partial["uri"]).path.lstrip("/") + "/part-00000.parquet"
        upload_id = client.create_multipart_upload(
            Bucket="lifecycle-versioned", Key=partial_key)["UploadId"]
        assert metadb.mark_object_attempt_terminal(partial["uri"], quiet_seconds=0)
        assert partial["uri"] in handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["observed"]
        with metadb.session() as session:
            session.get(metadb.ObjectAttempt, partial["uri"]).quiet_until = \
                metadb._db_now(session) - datetime.timedelta(seconds=1)
        handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        multipart_inventory = metadb.object_attempt_inventory(partial["uri"])
        assert any(item["upload_id"] == upload_id for item in multipart_inventory)
        handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        _force_delete_verification_due(partial["uri"])
        assert partial["uri"] in handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"]
        uploads = client.list_multipart_uploads(
            Bucket="lifecycle-versioned", Prefix=partial_key).get("Uploads", [])
        assert uploads == []
    finally:
        metadb.put_result(cache_key, {"uri": None})
        if second is not None:
            metadb.quarantine_object_attempt(second["uri"], "test cleanup")
        metadb.set_setting("objectStore", {}, "global")
        server.stop()


def test_moto_terminal_manifest_cleans_leftover_multipart_or_schedules_exact_gc(monkeypatch):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from moto.server import ThreadedMotoServer

    from hub.plugins.adapters import object_fs

    server = ThreadedMotoServer(port=0)
    server.start()
    published = None
    response_lost = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket="lifecycle-multipart-terminal")
        metadb.set_setting("objectStore", {
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s", "useSsl": False,
        }, "global")

        def land_with_leftover_upload(handle, value):
            prefix = urlsplit(handle["uri"]).path.lstrip("/").rstrip("/")
            upload_key = prefix + "/leftover.bin"
            upload_id = client.create_multipart_upload(
                Bucket="lifecycle-multipart-terminal", Key=upload_key)["UploadId"]
            fs, path = object_fs(handle["uri"])
            table = pa.table({"value": [value]})
            with fs.open_output_stream(path.rstrip("/") + "/part-00000.parquet") as stream:
                pq.write_table(table, stream)
            handoff.write_manifest(
                handle["uri"], run_id=handle["run_id"], rows=1, schema=table.schema)
            return upload_key, upload_id

        published = _handle(
            "sink",
            logical=f"s3://lifecycle-multipart-terminal/results/{uuid.uuid4().hex}.parquet",
            allocation_key=f"multipart-publish-{uuid.uuid4().hex}")
        published_key, _published_upload = land_with_leftover_upload(published, 1)
        handoff.prepare_attempt_commit(published["uri"])
        assert client.list_multipart_uploads(
            Bucket="lifecycle-multipart-terminal", Prefix=published_key).get("Uploads", []) == []
        metadb.catalog_upsert_entry(
            published["uri"], "multipart-publish", {
                "id": "ignored", "name": "multipart-publish", "uri": published["uri"]})
        assert _state(published["uri"]) == "published"

        response_lost = _handle(
            "sink",
            logical=f"s3://lifecycle-multipart-terminal/results/{uuid.uuid4().hex}.parquet",
            allocation_key=f"multipart-response-loss-{uuid.uuid4().hex}")
        response_lost_key, _response_lost_upload = land_with_leftover_upload(response_lost, 2)
        original_delete = handoff.Boto3ManagedObjectProvider.delete_exact

        def abort_then_lose_response(self, uri, member):
            original_delete(self, uri, member)
            if member.get("member_type") == "multipart_upload":
                raise ConnectionError("abort response lost")

        monkeypatch.setattr(
            handoff.Boto3ManagedObjectProvider, "delete_exact", abort_then_lose_response)
        handoff.prepare_attempt_commit(response_lost["uri"])
        assert _state(response_lost["uri"]) == "committed"
        assert client.list_multipart_uploads(
            Bucket="lifecycle-multipart-terminal",
            Prefix=response_lost_key).get("Uploads", []) == []

        failed = _handle(
            "sink",
            logical=f"s3://lifecycle-multipart-terminal/results/{uuid.uuid4().hex}.parquet",
            allocation_key=f"multipart-cleanup-{uuid.uuid4().hex}")
        failed_key, failed_upload = land_with_leftover_upload(failed, 2)

        def fail_abort(self, uri, member):
            if member.get("member_type") == "multipart_upload":
                raise ConnectionError("abort failed")
            return original_delete(self, uri, member)

        monkeypatch.setattr(
            handoff.Boto3ManagedObjectProvider, "delete_exact", fail_abort)
        with pytest.raises(RuntimeError, match="exact cleanup was scheduled"):
            handoff.prepare_attempt_commit(failed["uri"])
        with metadb.session() as session:
            row = session.get(metadb.ObjectAttempt, failed["uri"])
            assert row.state == "abandoned" and row.inventory_complete
        assert any(item["upload_id"] == failed_upload
                   for item in metadb.object_attempt_inventory(failed["uri"]))

        monkeypatch.setattr(
            handoff.Boto3ManagedObjectProvider, "delete_exact", original_delete)
        first = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert failed["uri"] in first["observed"]
        assert _state(failed["uri"]) == "delete_verifying"
        _force_delete_verification_due(failed["uri"])
        assert failed["uri"] in handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"]
        assert client.list_multipart_uploads(
            Bucket="lifecycle-multipart-terminal", Prefix=failed_key).get("Uploads", []) == []
    finally:
        if response_lost is not None:
            with contextlib.suppress(Exception):
                metadb.abandon_committed_object_attempt(response_lost["uri"])
            metadb.quarantine_object_attempt(response_lost["uri"], "test cleanup")
        if published is not None:
            with contextlib.suppress(Exception):
                metadb.catalog_delete_entry(published["uri"])
            metadb.quarantine_object_attempt(published["uri"], "test cleanup")
        metadb.set_setting("objectStore", {}, "global")
        server.stop()


@pytest.mark.parametrize("backend", ["local-subprocess", "local-pool"])
def test_moto_subprocess_backends_publish_parent_owned_object_full_result(
        tmp_path, monkeypatch, backend):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from moto.server import ThreadedMotoServer

    from hub import compiler
    from hub.deps import Deps
    from hub.models import Graph
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    isolated_url = f"sqlite:///{tmp_path / 'parent-metadata.db'}"
    server = ThreadedMotoServer(port=0)
    server.start()
    runner = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket="subprocess-full-result")
        client.put_bucket_versioning(
            Bucket="subprocess-full-result", VersioningConfiguration={"Status": "Enabled"})

        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = isolated_url
        metadb._engine = metadb._Session = None
        metadb.init_db()
        metadb.set_setting("objectStore", {
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s", "useSsl": False,
        }, "global")
        monkeypatch.setenv("DP_STORAGE_URL", "s3://subprocess-full-result/results")
        monkeypatch.setenv("DP_S3_ENDPOINT", endpoint)
        monkeypatch.setenv("DP_S3_KEY", "k")
        monkeypatch.setenv("DP_S3_SECRET", "s")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        if backend == "local-pool":
            monkeypatch.setenv("DP_POOL_WORKERS", '[{"name":"worker","cpu":2}]')

        workspace = tmp_path / "workspace"
        data_dir = tmp_path / "data"
        workspace.mkdir()
        data_dir.mkdir()
        source = tmp_path / "source.parquet"
        pq.write_table(pa.table({"value": [1, 2, 3]}), source)
        deps = Deps(str(workspace), str(data_dir))
        runner = next(candidate for candidate in deps.runners if candidate.name == backend)
        graph = Graph.model_validate({
            "id": f"{backend}-object-result", "version": 1,
            "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": str(source)}},
            }],
            "edges": [],
        })
        plan = compiler.compile_plan(
            graph, "source", deps.registry, deps.node_specs, deps.node_ir)
        status = runner.run(plan, graph, "source", "local")
        deadline = time.monotonic() + 30
        while runner.status(status.run_id).status not in ("done", "failed", "cancelled"):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        final = runner.status(status.run_id)
        assert final.status == "done", final.error
        assert final.output_uri and final.output_table is None
        result_key = urlsplit(final.output_uri).path.lstrip("/").rstrip("/") \
            + "/part-00000.parquet"
        result_bytes = client.get_object(
            Bucket="subprocess-full-result", Key=result_key)["Body"].read()
        assert pq.read_table(pa.BufferReader(result_bytes)).num_rows == 3
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, final.output_uri)
            assert attempt is not None and attempt.state == "published"
            ref = session.get(metadb.ObjectAttemptRef, {
                "ref_type": "run_state", "ref_key": final.run_id})
            assert ref is not None and ref.attempt_uri == final.output_uri
        phash = deps.runner._plan_hash(graph, "source")
        assert metadb.get_result(phash)["uri"] == final.output_uri

        metadb.save_run_state(final.run_id, {"run_id": final.run_id, "status": "failed"})
        metadb.put_result(phash, {"uri": None})
        metadb.quarantine_object_attempt(final.output_uri, "test cleanup")
    finally:
        if runner is not None:
            runner._terminate_all()
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        server.stop()


@pytest.mark.parametrize("child_status,cancelled,expected", [
    ("failed", False, "failed"),
    ("done", True, "cancelled"),
])
def test_subprocess_parent_discards_object_result_only_after_child_reaped(
        tmp_path, child_status, cancelled, expected):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/subprocess-fail.parquet")
    provider = _ExactProvider([])
    handoff.set_managed_object_provider(provider)
    job_dir = tmp_path / f"job-{child_status}-{cancelled}"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status=child_status, per_node=[], output_uri=handle["uri"],
    ).model_dump()))
    events: list[str] = []

    class FinishedProcess:
        returncode = 0 if child_status == "done" else 1

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            events.append("reaped")
            return 0

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner.runs[handle["run_id"]] = RunStatus(
        run_id=handle["run_id"], status="running", per_node=[])
    runner._object_results[handle["run_id"]] = {"uri": handle["uri"], "cache_key": None}
    if cancelled:
        runner._cancelled.add(handle["run_id"])
    original_discard = handoff.discard_attempt

    def discard_after_reap(uri):
        assert events == ["reaped"]
        original_discard(uri)

    handoff.discard_attempt = discard_after_reap
    try:
        runner._watch(
            handle["run_id"], FinishedProcess(), str(status_file), str(job_dir),
            Graph.model_validate({"id": "subprocess-fail", "version": 1,
                                  "nodes": [], "edges": []}), None)
        final = runner.status(handle["run_id"])
        assert final.status == expected and final.output_uri is None
        assert _state(handle["uri"]) == "abandoned"
    finally:
        handoff.discard_attempt = original_discard
        handoff.set_managed_object_provider(None)
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_subprocess_parent_persistence_failure_never_publishes_done(tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/persist-fail.parquet")
    job_dir = tmp_path / "job-persist-fail"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], output_uri=handle["uri"],
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner.runs[handle["run_id"]] = RunStatus(
        run_id=handle["run_id"], status="running", per_node=[])
    runner._object_results[handle["run_id"]] = {"uri": handle["uri"], "cache_key": None}
    runner.on_status = lambda _graph, status: (
        (_ for _ in ()).throw(RuntimeError("metadata unavailable"))
        if status.status == "done" else None)
    monkeypatch.setattr(handoff, "prepare_attempt_commit", lambda _uri: _commit(handle))
    runner._watch(
        handle["run_id"], FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "persist-fail", "version": 1,
                              "nodes": [], "edges": []}), None)
    final = runner.status(handle["run_id"])
    assert final.status == "failed" and final.output_uri is None
    assert "publication failed" in (final.error or "")
    assert _state(handle["uri"]) == "abandoned"
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_subprocess_post_popen_setup_failure_reaps_before_discard(tmp_path, monkeypatch):
    from hub.models import CompilePlan, Graph
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    events: list[str] = []
    attempt_uri = "s3://setup-failure/results/out.attempt-parent-owned"

    class ObjectStorage:
        @staticmethod
        def output_uri(name, ext):
            return f"s3://setup-failure/results/{name}{ext}"

    class Adapter:
        @staticmethod
        def fingerprint(_uri):
            return "source"

    class Process:
        returncode = None
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            events.append("terminate")
            self.stopped = True

        def wait(self, timeout=None):
            assert self.stopped
            events.append("wait")
            self.returncode = -15
            return self.returncode

        def kill(self):
            raise AssertionError("graceful setup cleanup should be sufficient")

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread setup failed")

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(subprocess_runner.threading, "Thread", BrokenThread)
    monkeypatch.setattr(handoff, "allocate_attempt", lambda **_kwargs: {"uri": attempt_uri})
    monkeypatch.setattr(
        handoff, "discard_attempt",
        lambda uri: events.append("discard") if uri == attempt_uri else None)
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), storage=ObjectStorage(),
        resolve_adapter=lambda _uri: Adapter())
    runner.on_status = lambda _graph, _status: None
    graph = Graph.model_validate({
        "id": "setup-failure", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"config": {"uri": str(tmp_path / 'source.parquet')}}}],
        "edges": [],
    })
    with pytest.raises(RuntimeError, match="thread setup failed"):
        runner.run(CompilePlan(target_node_id="source", steps=[]), graph, "source", "local")
    assert events == ["terminate", "wait", "discard"]
    assert runner._procs == {} and runner._object_results == {}


def test_subprocess_orderly_shutdown_fences_watcher_before_reap_and_discard(tmp_path, monkeypatch):
    from hub.subprocess_runner import SubprocessRunner

    run_id = "shutdown-object-result"
    events: list[str] = []

    class Process:
        returncode = None
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            assert run_id in runner._cancelled
            events.append("terminate")
            self.stopped = True

        def wait(self, timeout=None):
            events.append("wait")
            self.returncode = -15
            return self.returncode

        def kill(self):
            raise AssertionError("orderly shutdown should terminate within grace")

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner._procs[run_id] = Process()
    runner._object_results[run_id] = {"uri": "s3://shutdown/out.attempt-owned"}
    monkeypatch.setattr(
        handoff, "discard_attempt", lambda _uri: events.append("discard"))
    runner._terminate_all()
    assert events == ["terminate", "wait", "discard"]
    assert run_id in runner._cancelled
