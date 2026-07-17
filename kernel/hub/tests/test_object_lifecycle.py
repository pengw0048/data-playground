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
from sqlalchemy import func, select, text

from hub import handoff, metadb
from hub.models import LineagePublication, RunOutput, RunStatus
from hub.process_scope import OwnedProcessScope
from hub.run_outputs import require_single_run_output, sole_committed_document_output, sole_output


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


def _lineage(
        *, key: str | None = None, producer: str = "object-lifecycle-test",
        mappings: list[dict] | None = None) -> dict:
    return {
        "idempotency_key": key or f"object-lifecycle-{uuid.uuid4().hex}",
        "run_id": None,
        "attempt_id": None,
        "producer": producer,
        "producer_version": 1,
        "step_id": None,
        "provenance": "manual",
        "field_mappings": mappings or [],
    }


def _managed_namespace_aliases(logical_uri: str) -> dict[str, str]:
    logical_id, catalog_key = metadb._catalog_managed_namespace_identity(
        logical_uri, "tbl_lifecycle_test")
    return {
        "logical_uri": logical_uri,
        "logical_id": logical_id,
        "catalog_key": catalog_key,
    }


def _unmanaged_namespace_doc(uri: str, token: str, *, version: str = "v1") -> dict:
    return {
        "id": f"tbl_unmanaged_namespace_{token}",
        "name": f"unmanaged-namespace-{token}",
        "uri": uri,
        "version": version,
        "columns": [],
        "tags": [],
    }


def _publish_unmanaged_namespace(
        event_key: str, uri: str, token: str, *, version: str = "v1") -> bool:
    return metadb.catalog_upsert_output_idempotent(
        event_key, uri, f"unmanaged-namespace-{token}",
        _unmanaged_namespace_doc(uri, token, version=version),
        requested_version=version,
        lineage=_lineage(key=event_key),
    )


def _lineage_publication_event_key(event_key: str) -> str:
    return "lineage-publication:v1:sha256:" + hashlib.sha256(event_key.encode()).hexdigest()


def _record_lineage(parent: str, child: str, *, producer: str = "object-lifecycle-test") -> int:
    destination = metadb.catalog_get(child)
    return metadb.catalog_record_lineage(
        child, destination.get("version") if destination is not None else None, [parent],
        _lineage(producer=producer),
    )


def _run_output(
        uri: str | None = None, *, rows: int | None = None,
        node_id: str = "source", port_id: str = "out", table: str | None = None,
        outcome: str = "committed", publication_kind: str | None = None,
        version: str | None = None) -> RunOutput:
    kind = publication_kind or ("catalog" if table is not None else "result")
    return RunOutput(
        node_id=node_id, port_id=port_id, wire="dataset", publication_kind=kind,
        outcome=outcome, uri=uri,
        table=table if outcome == "committed" else None,
        version=version if outcome == "committed" else None, rows=rows,
    )


def _object_result_owner(
        handle: dict | str, *, node_id: str = "source", port_id: str = "out",
        cache_key: str | None = None, run_state_owner: bool = True) -> dict:
    if isinstance(handle, dict):
        uri = handle["uri"]
        logical_uri = handle["logical_uri"]
        attempt_id = handle["attempt_id"]
        generation = handle["generation"]
        namespace = handle["storage_namespace"]
    else:
        uri = str(handle)
        logical_uri = uri.split(".attempt-", 1)[0]
        attempt_id = "test-attempt"
        generation = 1
        namespace = "test-namespace"
    return {
        "results": [{
            "nodeId": node_id, "portId": port_id, "uri": uri,
            "logicalUri": logical_uri, "attemptId": attempt_id,
            "generation": generation, "storageNamespace": namespace,
        }],
        "cache_key": cache_key,
        "run_state_owner": run_state_owner,
    }


def _cache_document(uri: str, rows: int = 1, *, node_id: str = "source") -> dict:
    return {"outputs": [_run_output(uri, rows=rows, node_id=node_id).model_dump()]}


def _cached_output(key: str) -> RunOutput:
    document = metadb.get_result(key)
    assert document is not None and set(document) == {"outputs"}
    output = sole_committed_document_output(document)
    assert output is not None and output.rows is not None
    return output


def _committed_output(status: RunStatus) -> RunOutput:
    output = sole_output(status, committed=True)
    assert output is not None
    return output


def _only_pin(pin_ids: list[str] | None) -> str:
    assert pin_ids is not None and len(pin_ids) == 1
    return pin_ids[0]


def _retire_result_cache(key: str) -> None:
    """Simulate bounded cache-row pruning without publishing a legacy null-URI tombstone."""
    with metadb.session() as session:
        row = session.get(metadb.ResultCache, str(key), with_for_update=True)
        if row is None:
            return
        metadb._replace_attempt_ref(session, "result_cache", str(key), None)
        session.delete(row)


def _run_state_document(
        run_id: str, uri: str, *, rows: int = 1,
        node_id: str = "source", table: str | None = None) -> dict:
    return RunStatus(
        run_id=run_id, status="done", target_node_id=node_id, total_rows=rows,
        outputs=[_run_output(uri, rows=rows, node_id=node_id, table=table)],
    ).model_dump()


def _pending_run_state_document(run_id: str, *, node_id: str = "source") -> dict:
    return RunStatus(
        run_id=run_id, status="running", target_node_id=node_id,
        outputs=[_run_output(node_id=node_id, outcome="pending")],
    ).model_dump()


def _state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


def _retire_terminal_run_state(run_id: str) -> None:
    """Simulate terminal RunState retention cleanup without an illegal status transition."""
    with metadb.session() as session:
        state = session.get(metadb.RunState, str(run_id), with_for_update=True)
        assert state is not None and state.status in metadb._TERMINAL_RUN
        backend = session.get(metadb.RunBackendJob, str(run_id), with_for_update=True)
        if backend is not None:
            session.delete(backend)
        metadb._replace_attempt_ref(session, "run_state", str(run_id), None)
        session.delete(state)


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


def test_usage_publication_bumps_popularity_without_touching_updated_at():
    """A read-popularity bump must not advance the 'recently updated' sort key."""
    uri = f"mem://usage-updated-at/{uuid.uuid4().hex}/parent.parquet"
    metadb.catalog_upsert_entry(uri, "usage_parent", {"id": "tbl_usage_parent", "name": "usage_parent"})
    old = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    with metadb.session() as s:
        s.get(metadb.CatalogEntry, uri).updated_at = old
    try:
        plan = metadb.catalog_prepare_usage_publication(
            f"run-{uuid.uuid4().hex}", f"event-{uuid.uuid4().hex}", [uri])
        assert metadb.catalog_apply_usage_publication(plan) is True
        with metadb.session() as s:
            entry = s.get(metadb.CatalogEntry, uri)
            assert entry.usage == 1
            assert entry.updated_at.replace(tzinfo=datetime.timezone.utc) == old
    finally:
        metadb.catalog_delete_prefix("mem://usage-updated-at/")


@pytest.mark.parametrize("alias_name", ["logical_uri", "logical_id", "catalog_key"])
@pytest.mark.parametrize("tombstoned", [False, True], ids=["active", "unregistered"])
def test_managed_logical_namespace_rejects_new_unmanaged_publication(
        alias_name: str, tombstoned: bool):
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/namespace.parquet"
    handle = _handle("sink", logical=logical)
    aliases = _managed_namespace_aliases(logical)
    event_key = f"managed-namespace-{alias_name}-{token}"
    try:
        if tombstoned:
            _commit(handle)
            metadb.catalog_upsert_entry(handle["uri"], "managed-namespace", {
                "id": "ignored", "name": "managed-namespace", "uri": handle["uri"],
                "version": "managed-v1", "columns": [], "tags": [],
            })
            metadb.catalog_delete_entry(handle["uri"])

        with pytest.raises(RuntimeError, match="reserved for a managed logical dataset"):
            _publish_unmanaged_namespace(
                event_key, aliases[alias_name], token)

        with metadb.session() as session:
            logical_row = session.get(
                metadb.CatalogLogicalDataset, aliases["logical_id"])
            assert logical_row is not None
            assert logical_row.state == ("unregistered" if tombstoned else "active")
            assert session.get(metadb.CatalogEntry, aliases[alias_name]) is None
            assert session.get(metadb.CatalogPublicationEvent, event_key) is None
            assert session.get(
                metadb.CatalogPublicationEvent,
                _lineage_publication_event_key(event_key)) is None
    finally:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


@pytest.mark.parametrize("alias_name", ["logical_uri", "logical_id", "catalog_key"])
def test_managed_allocation_rejects_existing_unmanaged_namespace_alias(alias_name: str):
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/occupied.parquet"
    aliases = _managed_namespace_aliases(logical)
    alias = aliases[alias_name]
    event_key = f"unmanaged-first-{alias_name}-{token}"
    allocation_key = f"managed-after-unmanaged-{alias_name}-{token}"
    assert _publish_unmanaged_namespace(event_key, alias, token) is True
    try:
        with pytest.raises(RuntimeError, match="occupied by an unmanaged catalog entry"):
            _handle(
                "sink", logical=logical,
                run_id=f"managed-after-unmanaged-{token}",
                allocation_key=allocation_key,
            )
        with metadb.session() as session:
            assert session.get(
                metadb.CatalogLogicalDataset, aliases["logical_id"]) is None
            assert session.get(metadb.ObjectAttemptAllocation, allocation_key) is None
            assert session.scalar(select(func.count()).select_from(
                metadb.ObjectAttempt).where(
                    metadb.ObjectAttempt.logical_uri == logical)) == 0
            assert session.get(metadb.CatalogEntry, alias) is not None
            assert session.get(metadb.CatalogPublicationEvent, event_key) is not None
            assert session.get(
                metadb.CatalogPublicationEvent,
                _lineage_publication_event_key(event_key)) is not None
    finally:
        metadb.catalog_delete_entry(alias)


def test_old_unmanaged_receipt_replays_after_managed_namespace_takeover():
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/replay.parquet"
    event_key = f"unmanaged-before-managed-{token}"
    assert _publish_unmanaged_namespace(event_key, logical, token) is True
    metadb.catalog_delete_entry(logical)

    handle = _handle("sink", logical=logical)
    try:
        assert _publish_unmanaged_namespace(event_key, logical, token) is False
        with pytest.raises(RuntimeError, match="publication key collision"):
            metadb.catalog_upsert_output_idempotent(
                event_key, logical, f"changed-unmanaged-namespace-{token}",
                {
                    **_unmanaged_namespace_doc(logical, token),
                    "name": f"changed-unmanaged-namespace-{token}",
                },
                requested_version="v1",
            )
        with metadb.session() as session:
            logical_id = _managed_namespace_aliases(logical)["logical_id"]
            logical_row = session.get(metadb.CatalogLogicalDataset, logical_id)
            pointer = session.get(
                metadb.ObjectAttemptAllocation, handle["allocation_key"])
            assert logical_row is not None and logical_row.current_uri is None
            assert pointer is not None and pointer.attempt_uri == handle["uri"]
            assert session.get(metadb.CatalogEntry, logical) is None
    finally:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_compatibility_receipt_rejects_reserved_managed_namespace_but_replays_old_receipt():
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/compat.parquet"
    old_event = f"compat-before-managed-{token}"
    doc = _unmanaged_namespace_doc(logical, token)
    metadb.catalog_upsert_entry(logical, doc["name"], doc)
    metadb.catalog_record_output_publication(old_event, logical, "v1")
    metadb.catalog_delete_entry(logical)

    handle = _handle("sink", logical=logical)
    blocked_event = f"compat-after-managed-{token}"
    try:
        metadb.catalog_record_output_publication(old_event, logical, "v1")

        # Simulate a legacy unmanaged projection that predates the namespace fence. It must never gain a
        # new durable receipt after the managed logical identity has reserved this alias.
        metadb.catalog_upsert_entry(logical, doc["name"], doc)
        with pytest.raises(RuntimeError, match="reserved for a managed logical dataset"):
            metadb.catalog_record_output_publication(blocked_event, logical, "v1")
        with metadb.session() as session:
            assert session.get(metadb.CatalogPublicationEvent, old_event) is not None
            assert session.get(metadb.CatalogPublicationEvent, blocked_event) is None
    finally:
        with metadb.session() as session:
            legacy = session.get(metadb.CatalogEntry, logical, with_for_update=True)
            if legacy is not None:
                session.delete(legacy)
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


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


@pytest.mark.parametrize("alias_name", ["logical_uri", "logical_id", "catalog_key"])
def test_postgres_managed_allocation_and_unmanaged_publication_share_namespace_fence(
        alias_name: str):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    token = uuid.uuid4().hex
    logical = f"s3://lifecycle-tests/{token}/namespace-race.parquet"
    aliases = _managed_namespace_aliases(logical)
    alias = aliases[alias_name]
    allocation_key = f"pg-namespace-race-{alias_name}-{token}"
    event_key = f"pg-unmanaged-namespace-race-{alias_name}-{token}"
    barrier = threading.Barrier(2)
    handles: list[dict] = []
    unmanaged_results: list[bool] = []
    errors: list[tuple[str, BaseException]] = []

    def allocate() -> None:
        try:
            barrier.wait(timeout=5)
            handles.append(_handle(
                "sink", logical=logical,
                run_id=f"pg-managed-namespace-race-{token}",
                allocation_key=allocation_key,
            ))
        except BaseException as exc:  # noqa: BLE001 - both race outcomes are asserted below
            errors.append(("managed", exc))

    def publish_unmanaged() -> None:
        try:
            barrier.wait(timeout=5)
            unmanaged_results.append(_publish_unmanaged_namespace(
                event_key, alias, token))
        except BaseException as exc:  # noqa: BLE001 - both race outcomes are asserted below
            errors.append(("unmanaged", exc))

    threads = [threading.Thread(target=allocate), threading.Thread(target=publish_unmanaged)]
    try:
        with _postgres_lock_timeout(seconds=5):
            with metadb.session() as blocker:
                metadb._lock_catalog_namespace_tokens(blocker, [alias])
                for thread in threads:
                    thread.start()
                time.sleep(0.1)
                assert all(thread.is_alive() for thread in threads)
            for thread in threads:
                thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert len(errors) == 1
        assert isinstance(errors[0][1], RuntimeError)

        with metadb.session() as session:
            logical_row = session.get(
                metadb.CatalogLogicalDataset, aliases["logical_id"])
            entry = session.get(metadb.CatalogEntry, alias)
            event = session.get(metadb.CatalogPublicationEvent, event_key)
            lineage_event = session.get(
                metadb.CatalogPublicationEvent,
                _lineage_publication_event_key(event_key))
            pointer = session.get(metadb.ObjectAttemptAllocation, allocation_key)
            if handles:
                assert unmanaged_results == []
                assert errors[0][0] == "unmanaged"
                assert "reserved for a managed logical dataset" in str(errors[0][1])
                assert logical_row is not None and pointer is not None
                assert entry is None and event is None and lineage_event is None
            else:
                assert unmanaged_results == [True]
                assert errors[0][0] == "managed"
                assert "occupied by an unmanaged catalog entry" in str(errors[0][1])
                assert logical_row is None and pointer is None
                assert entry is not None and event is not None and lineage_event is not None
                assert session.scalar(select(func.count()).select_from(
                    metadb.ObjectAttempt).where(
                        metadb.ObjectAttempt.logical_uri == logical)) == 0
    finally:
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)
        with metadb.session() as session:
            legacy = session.get(metadb.CatalogEntry, alias, with_for_update=True)
            if legacy is not None and legacy.logical_id is None:
                session.delete(legacy)
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
    metadb.put_result(cache_key, _cache_document(old["uri"]))
    barrier = threading.Barrier(2)
    acquired: list[tuple[dict | None, list[str] | None]] = []
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
            metadb.put_result(cache_key, _cache_document(new["uri"]))
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
    doc, pin_ids = acquired[0]
    pin_id = _only_pin(pin_ids)
    pinned_output = sole_committed_document_output(doc)
    assert pinned_output is not None
    pinned_uri = pinned_output.uri
    assert pinned_uri in (old["uri"], new["uri"]) and pin_id
    actions = metadb.object_attempt_gc_batch(0, 0)
    assert pinned_uri not in {action["uri"] for action in actions}
    assert _state(pinned_uri) == "published"

    run_id = f"pg-cache-reader-{token}"
    metadb.save_run_state(run_id, _pending_run_state_document(run_id))
    metadb.save_run_state(run_id, _run_state_document(run_id, pinned_uri))
    metadb.release_result_cache_pins([pin_id])
    assert _state(pinned_uri) == "published"
    _retire_terminal_run_state(run_id)
    _retire_result_cache(cache_key)
    for handle in (old, new):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_gc_rechecks_refs_after_waiting_for_attempt_lock():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    from sqlalchemy import event

    handle = _handle(logical=(
        f"s3://lifecycle-tests/{uuid.uuid4().hex}/gc-publication-race.parquet"))
    _commit(handle)
    metadb.release_object_attempt_lease(handle["write_lease_id"])
    metadb.release_object_attempt_lease(handle["publish_lease_id"])
    ref_key = f"gc-publication-{uuid.uuid4().hex}"
    select_started = threading.Event()
    actions: list[dict] = []
    errors: list[BaseException] = []
    thread_name = f"gc-publication-race-{uuid.uuid4().hex}"

    def before_cursor_execute(
            _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if (threading.current_thread().name == thread_name
                and "FROM object_attempts" in statement
                and "object_attempts.state =" in statement
                and "object_attempts.terminal_proof_at" in statement
                and "FOR UPDATE" in statement):
            select_started.set()

    def collect_gc() -> None:
        try:
            actions.extend(metadb.object_attempt_gc_batch(0, 0))
        except BaseException as exc:  # noqa: BLE001 - asserted after the interleaving
            errors.append(exc)

    thread = threading.Thread(target=collect_gc, name=thread_name)
    with _postgres_lock_timeout(5):
        engine = metadb.engine()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            with metadb.session() as publisher:
                attempt = publisher.get(
                    metadb.ObjectAttempt, handle["uri"], with_for_update=True)
                assert attempt is not None and attempt.state == "committed"
                publisher.add(metadb.ObjectAttemptRef(
                    ref_type="backend_publication", ref_key=ref_key,
                    attempt_uri=attempt.uri, generation=attempt.generation,
                ))
                publisher.flush()
                thread.start()
                assert select_started.wait(timeout=3)
                time.sleep(0.05)
                assert thread.is_alive(), "GC did not wait for the publication attempt lock"
            thread.join(timeout=8)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert not thread.is_alive()
    assert errors == []
    assert handle["uri"] not in {action["uri"] for action in actions}
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, handle["uri"])
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "backend_publication", "ref_key": ref_key, "ref_slot": "",
        })
        assert attempt is not None and attempt.state == "committed"
        assert ref is not None and ref.attempt_uri == handle["uri"]

    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, handle["uri"], with_for_update=True)
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "backend_publication", "ref_key": ref_key, "ref_slot": "",
        }, with_for_update=True)
        assert ref is not None
        session.delete(ref)
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_quarantine_rechecks_refs_after_waiting_for_attempt_lock():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    from sqlalchemy import event

    handle = _handle(logical=(
        f"s3://lifecycle-tests/{uuid.uuid4().hex}/quarantine-publication-race.parquet"))
    _commit(handle)
    ref_key = f"quarantine-publication-{uuid.uuid4().hex}"
    select_started = threading.Event()
    errors: list[BaseException] = []
    thread_name = f"quarantine-publication-race-{uuid.uuid4().hex}"

    def before_cursor_execute(
            _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if (threading.current_thread().name == thread_name
                and "FROM object_attempts" in statement
                and "object_attempts.uri =" in statement
                and "FOR UPDATE" in statement):
            select_started.set()

    def quarantine() -> None:
        try:
            metadb.quarantine_object_attempt(handle["uri"], "publication race")
        except BaseException as exc:  # noqa: BLE001 - the durable-reference rejection is asserted
            errors.append(exc)

    thread = threading.Thread(target=quarantine, name=thread_name)
    with _postgres_lock_timeout(5):
        engine = metadb.engine()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            with metadb.session() as publisher:
                attempt = publisher.get(
                    metadb.ObjectAttempt, handle["uri"], with_for_update=True)
                assert attempt is not None and attempt.state == "committed"
                publisher.add(metadb.ObjectAttemptRef(
                    ref_type="backend_publication", ref_key=ref_key,
                    attempt_uri=attempt.uri, generation=attempt.generation,
                ))
                publisher.flush()
                thread.start()
                assert select_started.wait(timeout=3)
                time.sleep(0.05)
                assert thread.is_alive(), "quarantine did not wait for the publication attempt lock"
            thread.join(timeout=8)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "durable reference" in str(errors[0])
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, handle["uri"])
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "backend_publication", "ref_key": ref_key, "ref_slot": "",
        })
        assert attempt is not None and attempt.state == "committed"
        assert ref is not None and ref.attempt_uri == handle["uri"]

    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, handle["uri"], with_for_update=True)
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "backend_publication", "ref_key": ref_key, "ref_slot": "",
        }, with_for_update=True)
        assert ref is not None
        session.delete(ref)
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
        metadb.put_result(cache_key, _cache_document(handle["uri"]))
        _doc, pin_ids = metadb.acquire_result_cache_pin(cache_key, f"expiry-{index}", 30)
        pin_id = _only_pin(pin_ids)
        _retire_result_cache(cache_key)
        with metadb.session() as session:
            session.get(metadb.ObjectAttemptLease, pin_id).expires_at = \
                metadb._db_now(session) - datetime.timedelta(seconds=1)
        barrier = threading.Barrier(2)

        def release() -> None:
            try:
                barrier.wait(timeout=3)
                metadb.release_result_cache_pins([pin_id])
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
                "ref_type": "result_reader", "ref_key": pin_id, "ref_slot": "",
            }) is None
            assert session.get(metadb.ObjectAttemptLease, pin_id) is None
            assert session.get(metadb.ObjectAttempt, handle["uri"]).state != "published"
    assert not any("lock timeout" in str(exc).lower() or "deadlock" in str(exc).lower()
                   for exc in errors)
    assert errors == []
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_sink_publish_and_expired_lease_cleanup_share_lock_order():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    errors: list[tuple[str, BaseException]] = []
    handles: list[dict] = []
    for index in range(5):
        token = uuid.uuid4().hex
        handle = _handle(
            "sink",
            logical=f"s3://lifecycle-tests/{token}/publish-expiry.parquet",
            allocation_key=f"pg-publish-expiry-{token}",
        )
        handles.append(handle)
        _commit(handle)
        with metadb.session() as session:
            session.get(
                metadb.ObjectAttemptLease, handle["publish_lease_id"],
            ).expires_at = metadb._db_now(session) - datetime.timedelta(seconds=1)
        barrier = threading.Barrier(2)

        def publish() -> None:
            try:
                barrier.wait(timeout=3)
                metadb.catalog_upsert_entry(
                    handle["uri"], f"publish-expiry-{index}", {
                        "id": "ignored", "name": f"publish-expiry-{index}",
                        "uri": handle["uri"],
                    })
            except BaseException as exc:  # noqa: BLE001 - losing the GC race is valid
                errors.append(("publish", exc))

        def expire() -> None:
            try:
                barrier.wait(timeout=3)
                metadb.object_attempt_gc_batch(0, 0)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(("expire", exc))

        threads = [threading.Thread(target=publish), threading.Thread(target=expire)]
        with _postgres_lock_timeout():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
        assert all(not thread.is_alive() for thread in threads)
        current = metadb.catalog_get(handle["uri"])
        if current is not None:
            assert current["uri"] == handle["uri"]
            metadb.catalog_delete_entry(handle["uri"])
        else:
            assert _state(handle["uri"]) != "published"
    assert not any(
        "lock timeout" in str(exc).lower() or "deadlock" in str(exc).lower()
        for _name, exc in errors
    )
    assert not any(name == "expire" for name, _exc in errors)
    assert all(
        name == "publish" and "terminal proof" in str(exc)
        for name, exc in errors
    )
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_expiry_releases_each_attempt_before_waiting_for_the_next():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    metadb.object_attempt_gc_batch(0, 0)
    handles = [
        _handle(
            "sink",
            logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/expiry-order.parquet",
            allocation_key=f"pg-expiry-order-{uuid.uuid4().hex}",
        )
        for _index in range(2)
    ]
    first, second = sorted(handles, key=lambda item: item["uri"])
    with metadb.session() as session:
        past = metadb._db_now(session) - datetime.timedelta(seconds=1)
        for handle in handles:
            session.get(
                metadb.ObjectAttemptLease, handle["write_lease_id"],
            ).expires_at = past
            session.get(
                metadb.ObjectAttemptLease, handle["publish_lease_id"],
            ).expires_at = past

    errors: list[BaseException] = []

    def expire() -> None:
        try:
            metadb.object_attempt_gc_batch(0, 0)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    with _postgres_lock_timeout(5):
        with metadb.session() as blocker:
            blocker.get(metadb.ObjectAttempt, second["uri"], with_for_update=True)
            thread = threading.Thread(target=expire)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with metadb.session() as observer:
                    if observer.get(
                            metadb.ObjectAttemptLease, first["publish_lease_id"]) is None:
                        break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    "expiry retained the first attempt while waiting for the second")
            assert thread.is_alive(), "expiry did not wait for the blocked second attempt"
            # NOWAIT is the structural assertion: expiry committed the first attempt's short transaction
            # before it tried to acquire the blocked second row.
            assert blocker.get(
                metadb.ObjectAttempt, first["uri"],
                with_for_update={"nowait": True},
            ) is not None
        thread.join(timeout=8)
    assert not thread.is_alive()
    assert errors == []
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_expiry_waits_for_attempt_before_locking_publish_lease():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    handle = _handle(
        "sink",
        logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/attempt-first-expiry.parquet",
        allocation_key=f"pg-attempt-first-{uuid.uuid4().hex}",
    )
    _commit(handle)
    with metadb.session() as session:
        session.get(
            metadb.ObjectAttemptLease, handle["publish_lease_id"],
        ).expires_at = metadb._db_now(session) - datetime.timedelta(seconds=1)
    errors: list[BaseException] = []

    def expire() -> None:
        try:
            metadb.object_attempt_gc_batch(0, 0)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = None
    with _postgres_lock_timeout(5):
        with metadb.session() as blocker:
            blocker.get(metadb.ObjectAttempt, handle["uri"], with_for_update=True)
            thread = threading.Thread(target=expire)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with metadb.session() as observer:
                    waiting = observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE NOT granted AND pid <> pg_backend_pid())"
                    ))
                if waiting:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("expiry did not wait for the locked attempt")
            # A lease-first expiry implementation holds this row while waiting above and NOWAIT fails.
            assert blocker.get(
                metadb.ObjectAttemptLease, handle["publish_lease_id"],
                with_for_update={"nowait": True},
            ) is not None
        thread.join(timeout=8)
    assert thread is not None and not thread.is_alive()
    assert errors == []
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_unregister_serializes_governance_mutations():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    operations = (
        lambda uri: metadb.catalog_set_metadata(uri, "ghost", None, None, ["ghost"]),
        lambda uri: metadb.catalog_set_declared_key(uri, ["ghost"]),
        lambda uri: metadb.catalog_set_embedding(uri, "model", 1, b"ghost"),
        lambda uri: _record_lineage("s3://external/ghost", uri, producer="ghost"),
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
            assert not list(session.scalars(select(metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.destination_key == catalog_key)))
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
            "ref_type": "catalog", "ref_key": logical_row.logical_id, "ref_slot": "",
        }) is None
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
            "ref_type": "catalog", "ref_key": logical_row.logical_id, "ref_slot": "",
        })
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
            }, parents=[parent["uri"]], pipeline="pg-lineage",
                lineage=_lineage(producer="pg-lineage"))
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
    child_parents = {edge["parent"] for edge in metadb.catalog_lineage_pairs()
                     if edge["child"] == child["uri"]}
    assert replacement["uri"] not in child_parents
    with metadb.session() as session:
        parent_attempt = session.get(metadb.ObjectAttempt, parent["uri"])
        parent_row = session.get(metadb.CatalogLogicalDataset, parent_attempt.logical_id)
        assert not list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.source_key == parent_row.catalog_key,
            metadb.CatalogLineageFact.destination_key == session.get(
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
    metadb.put_result(cache_a, _cache_document(uri))
    metadb.put_result(cache_b, _cache_document(uri))

    canvas_id = f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="refs", version=1, doc="{}"))
    monkeypatch.setattr(metadb, "_RUN_HISTORY_MAX", 1)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
    run_id = f"run-{uuid.uuid4().hex}"
    history_output = _run_output(uri, rows=1, node_id="n").model_dump()
    metadb.record_run(
        canvas_id, "n", "run", "done", rows=1,
        outputs=[history_output], run_id=run_id)
    metadb.save_run_state(
        run_id, _pending_run_state_document(run_id, node_id="n"), canvas_id=canvas_id)
    metadb.save_run_state(run_id, _run_state_document(run_id, uri, node_id="n"))

    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.attempt_uri == uri)))
        assert sorted(ref.ref_type for ref in refs) == [
            "result_cache", "result_cache", "run_record", "run_state"]

    # Pruning the owning SQL rows releases exactly their refs, but two cache owners still pin the data.
    metadb.record_run(canvas_id, None, "run", "done", run_id=f"new-{run_id}")
    metadb.save_run_state(
        f"new-{run_id}", {"run_id": f"new-{run_id}", "status": "done"})
    assert _state(uri) == "published"
    _retire_result_cache(cache_a)
    assert _state(uri) == "published", "one cache key cannot retire another key's artifact"
    with handoff.managed_read_lease(uri, owner="history-read"):
        assert _state(uri) == "published"
    _retire_result_cache(cache_b)
    assert _state(uri) == "superseded"
    metadb.quarantine_object_attempt(uri, "test cleanup")


def test_named_output_cache_pins_and_replaces_complete_attempt_set():
    old = [_handle(), _handle()]
    new = [_handle(), _handle()]
    for handle in (*old, *new):
        _commit(handle)
    key = f"named-cache-{uuid.uuid4().hex}"

    def document(handles):
        return {"outputs": [
            _run_output(
                handle["uri"], rows=index + 1, node_id="section",
                port_id=port_id).model_dump()
            for index, (handle, port_id) in enumerate(zip(handles, ("left", "right")))
        ]}

    metadb.put_result(key, document(old))
    cached, pin_ids = metadb.acquire_result_cache_pin(key, "named-reader", 30)
    assert cached == document(old)
    assert pin_ids is not None and len(pin_ids) == 2
    assert metadb.renew_result_cache_pins(pin_ids, 30) is True
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_cache",
            metadb.ObjectAttemptRef.ref_key == key,
        ).order_by(metadb.ObjectAttemptRef.ref_slot)))
        assert {ref.ref_slot: ref.attempt_uri for ref in refs} == {
            metadb.run_output_ref_slot("section", "left"): old[0]["uri"],
            metadb.run_output_ref_slot("section", "right"): old[1]["uri"],
        }
        reader_refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.ref_key.in_(pin_ids),
        )))
        assert {ref.attempt_uri for ref in reader_refs} == {
            old[0]["uri"], old[1]["uri"],
        }

    metadb.put_result(key, document(new))
    assert {_state(handle["uri"]) for handle in old} == {"published"}
    metadb.release_result_cache_pins(pin_ids)
    assert {_state(handle["uri"]) for handle in old} == {"superseded"}
    _retire_result_cache(key)
    assert {_state(handle["uri"]) for handle in new} == {"superseded"}
    for handle in (*old, *new):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_named_output_cache_member_failure_creates_no_partial_reader_pins():
    handles = [_handle(), _handle()]
    for handle in handles:
        _commit(handle)
    key = f"named-cache-miss-{uuid.uuid4().hex}"
    doc = {"outputs": [
        _run_output(
            handle["uri"], rows=1, node_id="section",
            port_id=port_id).model_dump()
        for handle, port_id in zip(handles, ("left", "right"))
    ]}
    metadb.put_result(key, doc)
    with metadb.session() as session:
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "result_cache",
            "ref_key": key,
            "ref_slot": metadb.run_output_ref_slot("section", "right"),
        }, with_for_update=True)
        assert ref is not None
        session.delete(ref)
    with pytest.raises(FileNotFoundError, match="incomplete lifecycle ownership"):
        metadb.acquire_result_cache_pin(key, "incomplete-reader", 30)
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri.in_([handle["uri"] for handle in handles]),
        )))
    metadb.put_result(key, doc)
    _retire_result_cache(key)
    for handle in handles:
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_failed_named_output_run_owns_committed_prefix_in_state_and_history():
    handle = _handle()
    _commit(handle)
    run_id = f"failed-named-{uuid.uuid4().hex}"
    canvas_id = f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
            name="failed named output", version=1, doc="{}"))
    pending = RunStatus(
        run_id=run_id, status="running", target_node_id="section",
        outputs=[
            _run_output(
                node_id="section", port_id=port_id, outcome="pending")
            for port_id in ("left", "right")
        ],
    )
    metadb.save_run_state(run_id, pending.model_dump(), canvas_id=canvas_id)
    failed = RunStatus(
        run_id=run_id, status="failed", target_node_id="section",
        error="right publication failed",
        outputs=[
            _run_output(
                handle["uri"], rows=3, node_id="section", port_id="left"),
            _run_output(
                node_id="section", port_id="right", outcome="failed"),
        ],
    )
    metadb.save_run_state(
        run_id, failed.model_dump(), canvas_id=canvas_id, publish_region=True)
    metadb.record_run(
        canvas_id, "section", "run", "failed",
        error=failed.error, outputs=[output.model_dump() for output in failed.outputs],
        run_id=run_id)
    assert _state(handle["uri"]) == "published"
    slot = metadb.run_output_ref_slot("section", "left")
    with metadb.session() as session:
        record = session.scalar(select(metadb.RunRecord).where(
            metadb.RunRecord.run_id == run_id))
        assert record is not None
        refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
        assert {(ref.ref_type, ref.ref_key, ref.ref_slot) for ref in refs} == {
            ("run_state", run_id, slot),
            ("run_record", record.id, slot),
        }

    _retire_terminal_run_state(run_id)
    assert _state(handle["uri"]) == "published"
    metadb.delete_canvas_cascade(canvas_id)
    assert _state(handle["uri"]) == "superseded"
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_kernel_status_wiring_publishes_failed_partial_region_output():
    from hub.kernel import _persist_kernel_run_state

    run_id = f"kernel-failed-named-{uuid.uuid4().hex}"
    handle = _handle(run_id=run_id)
    _commit(handle)
    pending = RunStatus(
        run_id=run_id, status="running", target_node_id="section",
        outputs=[
            _run_output(node_id="section", port_id=port_id, outcome="pending")
            for port_id in ("left", "right")
        ],
    )
    metadb.save_run_state(run_id, pending.model_dump())
    failed = RunStatus(
        run_id=run_id, status="failed", target_node_id="section",
        error="right publication failed",
        outputs=[
            _run_output(
                handle["uri"], rows=3, node_id="section", port_id="left"),
            _run_output(
                node_id="section", port_id="right", outcome="failed"),
        ],
    )

    _persist_kernel_run_state(
        metadb, object(), failed, kernel_id=f"kernel-{uuid.uuid4().hex}")

    assert _state(handle["uri"]) == "published"
    with metadb.session() as session:
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "run_state",
            "ref_key": run_id,
            "ref_slot": metadb.run_output_ref_slot("section", "left"),
        })
        assert ref is not None and ref.attempt_uri == handle["uri"]
    _retire_terminal_run_state(run_id)
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


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
    }, parents=["s3://source/one"], pipeline="canvas-v1",
        lineage=_lineage(producer="canvas-v1"))
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
    }, parents=["s3://source/two"], pipeline="canvas-v2",
        lineage=_lineage(producer="canvas-v2"))

    current = metadb.catalog_get(stable_id)
    assert current["uri"] == second["uri"]
    assert (current["folder"], current["owner"], current["description"], current["tags"]) == (
        "gold/team", "data", "curated contract", ["gold"])
    assert metadb.catalog_declared_keys([second["uri"]])[second["uri"]] == ["id"]
    rels = metadb.catalog_relationships()
    assert any(second["uri"] in (r.get("leftUri"), r.get("rightUri")) for r in rels)
    assert all(first["uri"] not in (r.get("leftUri"), r.get("rightUri")) for r in rels)
    pairs = metadb.catalog_lineage_pairs()
    assert {e["parent"] for e in pairs if e["child"] == second["uri"]} >= {
        "s3://source/one", "s3://source/two"}
    with metadb.session() as session:
        exact_facts = set(session.execute(select(
            metadb.CatalogLineageFact.source_uri,
            metadb.CatalogLineageFact.destination_uri,
        )).all())
    assert ("s3://source/one", first["uri"]) in exact_facts
    assert ("s3://source/two", second["uri"]) in exact_facts
    assert metadb.catalog_embeddings_for("model") == [(second["uri"], b"\x00\x00\x80?")]
    with metadb.session() as session:
        logical_row = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.current_uri == second["uri"]))
        assert logical_row is not None
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical_row.logical_id, "ref_slot": "",
        })
        assert ref.attempt_uri == second["uri"]
    assert _state(first["uri"]) == "superseded"
    metadb.catalog_delete_entry(second["uri"])
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


@pytest.mark.parametrize(
    "alias_name", ["logical_uri", "logical_id", "catalog_key", "friendly_name"])
def test_managed_self_overwrite_freezes_stable_alias_source_version(alias_name):
    token = uuid.uuid4().hex
    logical_uri = f"s3://lifecycle-tests/{token}/self-overwrite.parquet"
    friendly_name = f"self-overwrite-{token}"
    first = _handle("sink", logical=logical_uri)
    second = _handle("sink", logical=logical_uri)
    _commit(first)
    metadb.catalog_upsert_entry(first["uri"], friendly_name, {
        "id": "ignored", "name": friendly_name, "uri": first["uri"],
        "version": "v1",
    })
    aliases = _managed_namespace_aliases(logical_uri)
    aliases["friendly_name"] = friendly_name

    _commit(second)
    metadb.catalog_upsert_entry(second["uri"], friendly_name, {
        "id": "ignored", "name": friendly_name, "uri": second["uri"],
        "version": "v2",
    }, parents=[aliases[alias_name]], lineage=_lineage(
        key=f"managed-self-overwrite-{alias_name}-{uuid.uuid4().hex}"))

    with metadb.session() as session:
        facts = list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == second["uri"])))
    assert len(facts) == 1
    assert (facts[0].source_key, facts[0].destination_key) == (
        aliases["catalog_key"], aliases["catalog_key"])
    assert (facts[0].source_uri, facts[0].destination_uri) == (
        first["uri"], second["uri"])
    assert (facts[0].source_version, facts[0].destination_version) == ("v1", "v2")

    metadb.catalog_delete_entry(second["uri"])
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


@pytest.mark.parametrize("alias_name", ["logical_uri", "catalog_key"])
def test_managed_lineage_alias_projects_to_one_physical_graph_root(tmp_path, alias_name):
    from hub.plugins.catalog import InMemoryCatalog

    token = uuid.uuid4().hex
    parent_logical = f"s3://lifecycle-tests/{token}/parent.parquet"
    child_logical = f"s3://lifecycle-tests/{token}/child.parquet"
    parent = _handle("sink", logical=parent_logical)
    child = _handle("sink", logical=child_logical)
    _commit(parent)
    metadb.catalog_upsert_entry(parent["uri"], "lineage-parent", {
        "id": "ignored", "name": "lineage-parent", "uri": parent["uri"], "version": "v1",
    })
    parent_aliases = _managed_namespace_aliases(parent_logical)
    _commit(child)
    metadb.catalog_upsert_entry(child["uri"], "lineage-child", {
        "id": "ignored", "name": "lineage-child", "uri": child["uri"], "version": "v1",
    }, parents=[parent["uri"]], lineage=_lineage())
    catalog = InMemoryCatalog(str(tmp_path), lambda _uri: None)

    full = catalog.lineage(parent_aliases[alias_name], depth=2, max_nodes=10)
    assert full.root_uri == parent["uri"]
    assert {node.uri for node in full.nodes} == {parent["uri"], child["uri"]}
    assert {(edge.parent, edge.child) for edge in full.edges} == {
        (parent["uri"], child["uri"])}
    capped = catalog.lineage(parent_aliases[alias_name], depth=2, max_nodes=1)
    assert capped.root_uri == parent["uri"]
    assert [node.uri for node in capped.nodes] == [parent["uri"]]
    assert capped.edges == [] and capped.truncated is True

    metadb.catalog_delete_entry(child["uri"])
    metadb.catalog_delete_entry(parent["uri"])
    metadb.quarantine_object_attempt(parent["uri"], "test cleanup")
    metadb.quarantine_object_attempt(child["uri"], "test cleanup")


def test_managed_lineage_graph_uses_one_projection_after_interleaved_overwrite(
        tmp_path, monkeypatch):
    from hub.plugins.catalog import InMemoryCatalog

    token = uuid.uuid4().hex
    parent_logical = f"s3://lifecycle-tests/{token}/parent.parquet"
    child_logical = f"s3://lifecycle-tests/{token}/child.parquet"
    first = _handle("sink", logical=parent_logical)
    second = _handle("sink", logical=parent_logical)
    child = _handle("sink", logical=child_logical)
    _commit(first)
    metadb.catalog_upsert_entry(first["uri"], "lineage-parent", {
        "id": "ignored", "name": "lineage-parent", "uri": first["uri"], "version": "v1",
    })
    parent_aliases = _managed_namespace_aliases(parent_logical)
    _commit(child)
    metadb.catalog_upsert_entry(child["uri"], "lineage-child", {
        "id": "ignored", "name": "lineage-child", "uri": child["uri"], "version": "v1",
    }, parents=[first["uri"]], lineage=_lineage())
    _commit(second)
    original_touching = metadb.catalog_lineage_key_pairs_touching
    overwritten = False

    def overwrite_before_first_hop(keys, limit):
        nonlocal overwritten
        if not overwritten:
            overwritten = True
            metadb.catalog_upsert_entry(second["uri"], "lineage-parent", {
                "id": "ignored", "name": "lineage-parent",
                "uri": second["uri"], "version": "v2",
            })
        return original_touching(keys, limit)

    monkeypatch.setattr(
        metadb, "catalog_lineage_key_pairs_touching", overwrite_before_first_hop)
    graph = InMemoryCatalog(str(tmp_path), lambda _uri: None).lineage(
        parent_aliases["logical_uri"], depth=2, max_nodes=10)

    assert overwritten is True
    assert graph.root_uri == second["uri"]
    assert {node.uri for node in graph.nodes} == {second["uri"], child["uri"]}
    assert [(edge.parent, edge.child, edge.fact_count) for edge in graph.edges] == [
        (second["uri"], child["uri"], 1),
    ]

    metadb.catalog_delete_entry(child["uri"])
    metadb.catalog_delete_entry(second["uri"])
    for handle in (first, second, child):
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_postgres_lineage_projection_materializes_pointer_and_entry_together(
        monkeypatch):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    token = uuid.uuid4().hex
    logical_uri = f"s3://lifecycle-tests/{token}/projection.parquet"
    first = _handle("sink", logical=logical_uri)
    second = _handle("sink", logical=logical_uri)
    _commit(first)
    metadb.catalog_upsert_entry(first["uri"], "projection", {
        "id": "ignored", "name": "projection", "uri": first["uri"], "version": "v1",
    })
    catalog_key = _managed_namespace_aliases(logical_uri)["catalog_key"]
    _commit(second)
    original_tags = metadb._tags_for
    overwritten = False

    def overwrite_after_joined_projection(session, uris):
        nonlocal overwritten
        if not overwritten:
            overwritten = True
            metadb.catalog_upsert_entry(second["uri"], "projection", {
                "id": "ignored", "name": "projection",
                "uri": second["uri"], "version": "v2",
            })
        return original_tags(session, uris)

    monkeypatch.setattr(metadb, "_tags_for", overwrite_after_joined_projection)
    projection, docs = metadb.catalog_lineage_project_keys([catalog_key])

    assert overwritten is True
    assert projection == {catalog_key: first["uri"]}
    assert docs[first["uri"]]["version"] == "v1"
    assert metadb.catalog_get(catalog_key)["uri"] == second["uri"]

    metadb.catalog_delete_entry(second["uri"])
    metadb.quarantine_object_attempt(first["uri"], "test cleanup")
    metadb.quarantine_object_attempt(second["uri"], "test cleanup")


def test_durable_usage_follows_logical_identity_across_aliases_and_generations(tmp_path):
    token = uuid.uuid4().hex
    logical_uri = f"s3://lifecycle-tests/{token}/usage.parquet"
    first = _handle("sink", logical=logical_uri)
    second = _handle("sink", logical=logical_uri)
    unmanaged_uri = str(tmp_path / f"unmanaged-{token}.parquet")
    _commit(first)
    metadb.catalog_upsert_entry(first["uri"], "usage", {
        "id": "ignored", "name": "usage", "uri": first["uri"],
        "version": "v1", "columns": [], "tags": [],
    })
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, first["uri"])
        logical_id = attempt.logical_id
        logical_row = session.get(metadb.CatalogLogicalDataset, logical_id)
        catalog_key = logical_row.catalog_key

    first_event = f"usage-first-{token}"
    second_event = f"usage-second-{token}"
    managed_output_event = f"managed-output-{token}"
    try:
        with pytest.raises(RuntimeError, match="core object-lifecycle"):
            metadb.catalog_record_output_publication(
                managed_output_event, first["uri"], "v1")
        with metadb.session() as session:
            assert session.get(metadb.CatalogPublicationEvent, managed_output_event) is None

        assert metadb.catalog_bump_usage_once(first_event, [
            first["uri"], logical_uri, catalog_key, logical_id,
        ]) is True
        assert metadb.catalog_bump_usage_once(first_event, [logical_id]) is False
        with metadb.session() as session:
            logical_row = session.get(metadb.CatalogLogicalDataset, logical_id)
            entry = session.get(metadb.CatalogEntry, logical_row.current_uri)
            event = session.get(metadb.CatalogPublicationEvent, first_event)
            assert logical_row.usage == entry.usage == 1
            assert event.effect_type == "usage"
            assert event.uri.startswith("usage:v1:sha256:") and event.version is None

        _commit(second)
        metadb.catalog_upsert_entry(second["uri"], "usage", {
            "id": "ignored", "name": "usage", "uri": second["uri"],
            "version": "v2", "columns": [], "tags": [],
        })
        assert metadb.catalog_get(second["uri"])["usage"] == 1
        assert metadb.catalog_bump_usage_once(first_event, [second["uri"]]) is False

        assert metadb.catalog_bump_usage_once(second_event, [
            first["uri"], second["uri"], catalog_key,
        ]) is True
        with metadb.session() as session:
            logical_row = session.get(metadb.CatalogLogicalDataset, logical_id)
            entry = session.get(metadb.CatalogEntry, logical_row.current_uri)
            assert logical_row.usage == entry.usage == 2

        metadb.catalog_upsert_entry(unmanaged_uri, "unmanaged", {
            "id": f"tbl_unmanaged_{token}", "name": "unmanaged", "uri": unmanaged_uri,
            "version": "v1", "columns": [], "tags": [],
        })
        with pytest.raises(RuntimeError, match="publication key collision"):
            metadb.catalog_bump_usage_once(second_event, [unmanaged_uri])
        assert metadb.catalog_get(unmanaged_uri)["usage"] == 0

        metadb.catalog_delete_entry(second["uri"])
        assert metadb.catalog_bump_usage_once(second_event, [logical_uri]) is False
        with pytest.raises(RuntimeError, match="publication key collision"):
            metadb.catalog_bump_usage_once(
                second_event, [logical_uri, unmanaged_uri])
        with pytest.raises(RuntimeError, match="catalog governance target is inactive"):
            metadb.catalog_prepare_usage_publication(
                f"run-after-unregister-{token}", f"usage-after-unregister-{token}",
                [logical_uri],
            )
    finally:
        if metadb.catalog_get(unmanaged_uri) is not None:
            metadb.catalog_delete_entry(unmanaged_uri)
        if metadb.catalog_get(logical_id) is not None:
            metadb.catalog_delete_entry(logical_id)
        metadb.quarantine_object_attempt(first["uri"], "test cleanup")
        metadb.quarantine_object_attempt(second["uri"], "test cleanup")


def test_durable_usage_inactive_replay_does_not_hide_active_catalog_corruption():
    token = uuid.uuid4().hex
    logical_uri = f"s3://lifecycle-tests/{token}/usage-corrupt.parquet"
    handle = _handle("sink", logical=logical_uri)
    _commit(handle)
    metadb.catalog_upsert_entry(handle["uri"], "usage-corrupt", {
        "id": "ignored", "name": "usage-corrupt", "uri": handle["uri"],
        "version": "v1", "columns": [], "tags": [],
    })
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, handle["uri"])
        logical_id = attempt.logical_id
        logical = session.get(metadb.CatalogLogicalDataset, logical_id)
        assert logical.state == "active" and logical.current_uri == handle["uri"]
        logical.current_uri = None

    event_key = f"usage-corrupt-{token}"
    try:
        with pytest.raises(RuntimeError, match="catalog governance target is inactive"):
            metadb.catalog_bump_usage_once(event_key, [logical_uri])
        with metadb.session() as session:
            assert session.get(metadb.CatalogPublicationEvent, event_key) is None
    finally:
        with metadb.session() as session:
            logical = session.get(metadb.CatalogLogicalDataset, logical_id)
            logical.current_uri = handle["uri"]
        metadb.catalog_delete_entry(handle["uri"])
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_read_lease_wins_or_observes_explicit_gc_miss():
    reader_first = _handle()
    _commit(reader_first)
    key = f"lease-{uuid.uuid4().hex}"
    metadb.put_result(key, _cache_document(reader_first["uri"]))
    lease = metadb.acquire_object_attempt_lease(reader_first["uri"], "read", "reader", 30)
    _retire_result_cache(key)
    assert not any(item["uri"] == reader_first["uri"] for item in
                   metadb.object_attempt_gc_batch(0, 0))
    metadb.release_object_attempt_lease(lease)
    action = next(item for item in metadb.object_attempt_gc_batch(0, 0)
                  if item["uri"] == reader_first["uri"])
    assert action["action"] == "delete"

    gc_first = _handle()
    _commit(gc_first)
    key2 = f"lease-{uuid.uuid4().hex}"
    metadb.put_result(key2, _cache_document(gc_first["uri"]))
    _retire_result_cache(key2)
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
    metadb.put_result(cache_key, _cache_document(old["uri"]))
    doc, pin_ids = metadb.acquire_result_cache_pin(cache_key, "pin-reader", 30)
    pin_id = _only_pin(pin_ids)
    cached_output = sole_committed_document_output(doc)
    assert cached_output is not None and cached_output.uri == old["uri"] and pin_id

    metadb.put_result(cache_key, _cache_document(new["uri"]))
    assert _state(old["uri"]) == "published"
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttemptRef, {
            "ref_type": "result_reader", "ref_key": pin_id, "ref_slot": "",
        }).attempt_uri == old["uri"]

    run_id = f"cache-pin-run-{uuid.uuid4().hex}"
    metadb.save_run_state(run_id, _pending_run_state_document(run_id))
    metadb.save_run_state(run_id, _run_state_document(run_id, old["uri"]))
    metadb.release_result_cache_pins([pin_id])
    assert _state(old["uri"]) == "published", "terminal RunState replaced the temporary owner"
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttemptRef, {
            "ref_type": "result_reader", "ref_key": pin_id, "ref_slot": "",
        }) is None
        assert session.get(metadb.ObjectAttemptLease, pin_id) is None

    _retire_terminal_run_state(run_id)
    assert _state(old["uri"]) == "superseded"
    _retire_result_cache(cache_key)
    metadb.quarantine_object_attempt(old["uri"], "test cleanup")
    metadb.quarantine_object_attempt(new["uri"], "test cleanup")


def test_done_run_state_is_primary_owner_for_noncacheable_committed_region():
    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/noncacheable.parquet")
    _commit(handle)
    run_id = f"noncacheable-{uuid.uuid4().hex}"
    metadb.save_run_state(run_id, _pending_run_state_document(run_id))
    metadb.save_run_state(run_id, _run_state_document(run_id, handle["uri"]),
        publish_region=True)
    assert _state(handle["uri"]) == "published"
    with metadb.session() as session:
        ref = session.get(metadb.ObjectAttemptRef, {
            "ref_type": "run_state", "ref_key": run_id,
            "ref_slot": metadb.run_output_ref_slot("source", "out"),
        })
        assert ref is not None and ref.attempt_uri == handle["uri"]
    _retire_terminal_run_state(run_id)
    assert _state(handle["uri"]) == "superseded"
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_local_runner_terminal_owner_rejection_releases_cache_pin_before_terminal(
    tmp_path, caplog, monkeypatch
):
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
    metadb.put_result(phash, _cache_document(handle["uri"]))
    runner.result_acquire = metadb.acquire_result_cache_pin
    persistence_started = threading.Event()
    allow_failure = threading.Event()
    release_started = threading.Event()
    allow_first_release_failure = threading.Event()
    release_retry_started = threading.Event()
    allow_release = threading.Event()
    release_calls = 0

    release_pins = metadb.release_result_cache_pins

    def release_after_observation(pin_ids):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            release_started.set()
            assert allow_first_release_failure.wait(timeout=5)
            raise RuntimeError("transient pin release failure")
        release_retry_started.set()
        assert allow_release.wait(timeout=5)
        release_pins(pin_ids)

    monkeypatch.setattr(metadb, "release_result_cache_pins", release_after_observation)

    def persist(_graph, status):
        if status.status != "done":
            return
        persistence_started.set()
        assert allow_failure.wait(timeout=5)
        raise metadb.RunStatePublicationRejected("run owner was deleted")

    runner.on_status = persist
    caplog.set_level("ERROR", logger="hub")
    plan = compiler.compile_plan(
        graph, "source", deps.registry, deps.node_specs, deps.node_ir)
    started = runner.run(plan, graph, "source", "local")
    assert persistence_started.wait(timeout=5)
    assert runner.status(started.run_id).status == "running"
    allow_failure.set()
    assert release_started.wait(timeout=5)
    # The worker has entered terminal cleanup, but the durable reader still exists.  Pollers must
    # continue to observe a live run until that ownership receipt is actually released.
    assert runner.status(started.run_id).status == "running"
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
    allow_first_release_failure.set()
    assert release_retry_started.wait(timeout=5)
    assert runner.status(started.run_id).status == "running"
    allow_release.set()
    deadline = time.monotonic() + 5
    while runner.status(started.run_id).status not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = runner.status(started.run_id)
    assert final.status == "failed"
    assert final.outputs[0].outcome == "failed" and final.outputs[0].uri is None
    assert "run owner was deleted" in (final.error or "")
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
    _retire_result_cache(phash)
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_local_runner_cancel_releases_cache_pin_before_terminal(
    tmp_path, monkeypatch
):
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
        "id": "local-cancel-cache-pin", "version": 1,
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
    metadb.put_result(phash, _cache_document(handle["uri"]))
    runner.result_acquire = metadb.acquire_result_cache_pin
    cache_acquired = threading.Event()
    allow_step = threading.Event()
    release_started = threading.Event()
    allow_release = threading.Event()

    cache_acquire = runner._cache_acquire

    def acquire_before_step(*args, **kwargs):
        result = cache_acquire(*args, **kwargs)
        cache_acquired.set()
        assert allow_step.wait(timeout=5)
        return result

    release_pins = metadb.release_result_cache_pins

    def release_after_observation(pin_ids):
        release_started.set()
        assert allow_release.wait(timeout=5)
        release_pins(pin_ids)

    monkeypatch.setattr(runner, "_cache_acquire", acquire_before_step)
    monkeypatch.setattr(metadb, "release_result_cache_pins", release_after_observation)
    plan = compiler.compile_plan(
        graph, "source", deps.registry, deps.node_specs, deps.node_ir)
    started = runner.run(plan, graph, "source", "local")
    assert cache_acquired.wait(timeout=5)
    assert runner.cancel(started.run_id).status == "running"
    allow_step.set()
    assert release_started.wait(timeout=5)
    assert runner.status(started.run_id).status == "running"
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
    allow_release.set()
    deadline = time.monotonic() + 5
    while runner.status(started.run_id).status != "cancelled":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "result_reader",
            metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
        )))
    _retire_result_cache(phash)
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_long_reader_renews_beyond_initial_ttl_and_blocks_gc():
    handle = _handle()
    _commit(handle)
    key = f"renew-{uuid.uuid4().hex}"
    metadb.put_result(key, _cache_document(handle["uri"]))
    with handoff.managed_read_lease(handle["uri"], owner="slow-reader", ttl_seconds=1) as guard:
        _retire_result_cache(key)
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
            metadb.put_result(key, _cache_document(handle["uri"]))
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


def test_sqlite_cache_replacement_and_reader_pin_are_serialized():
    if metadb.engine().dialect.name != "sqlite":
        pytest.skip("requires a SQLite metadata database")
    old, new = _handle(), _handle()
    _commit(old)
    _commit(new)
    cache_key = f"sqlite-cache-race-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, _cache_document(old["uri"]))
    barrier = threading.Barrier(2)
    acquired: list[tuple[dict | None, list[str] | None]] = []
    errors: list[BaseException] = []

    def acquire() -> None:
        try:
            barrier.wait(timeout=3)
            acquired.append(metadb.acquire_result_cache_pin(
                cache_key, f"sqlite-reader-{uuid.uuid4().hex}", 30))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def replace() -> None:
        try:
            barrier.wait(timeout=3)
            metadb.put_result(cache_key, _cache_document(new["uri"]))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=acquire), threading.Thread(target=replace)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and len(acquired) == 1
    doc, pin_ids = acquired[0]
    pin_id = _only_pin(pin_ids)
    acquired_output = sole_committed_document_output(doc)
    assert acquired_output is not None
    assert acquired_output.uri in (old["uri"], new["uri"]) and pin_id
    assert _state(acquired_output.uri) == "published"
    metadb.release_result_cache_pins([pin_id])
    _retire_result_cache(cache_key)
    metadb.quarantine_object_attempt(old["uri"], "test cleanup")
    metadb.quarantine_object_attempt(new["uri"], "test cleanup")


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
    metadb.put_result(key, _cache_document(handle["uri"]))
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
        metadb.record_run(
            canvas_id, "n", "run", "done", rows=1,
            outputs=[_run_output(uri, rows=1, node_id="n").model_dump()],
            run_id="secondary")
    metadb.save_run_state(
        "secondary", _pending_run_state_document("secondary", node_id="n"),
        canvas_id=canvas_id)
    with pytest.raises(RuntimeError, match="only a published"):
        metadb.save_run_state("secondary", _run_state_document(
            "secondary", uri, node_id="n"))

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
    assert len([edge for edge in metadb.catalog_lineage_pairs()
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
    }, parents=[stable_id], pipeline="historical",
        lineage=_lineage(producer="historical"))
    assert {edge["parent"] for edge in metadb.catalog_lineage_pairs()
            if edge["child"] == child["uri"]} == {parent_logical}

    replacement = _handle("sink", logical=parent_logical)
    _commit(replacement)
    metadb.catalog_upsert_entry(replacement["uri"], "parent", {
        "id": "ignored", "name": "parent", "uri": replacement["uri"]})
    assert replacement["uri"] not in {edge["parent"] for edge in metadb.catalog_lineage_pairs()
                                      if edge["child"] == child["uri"]}
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.source_key == stable_id)))

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
    }, parents=[old_parent["uri"]], pipeline="late-old-epoch",
        lineage=_lineage(producer="late-old-epoch"))

    child_parents = {edge["parent"] for edge in metadb.catalog_lineage_pairs()
                     if edge["child"] == child["uri"]}
    assert child_parents == {old_parent["uri"]}
    assert fresh_parent["uri"] not in child_parents
    with metadb.session() as session:
        child_key = session.get(
            metadb.CatalogLogicalDataset,
            session.get(metadb.ObjectAttempt, child["uri"]).logical_id).catalog_key
        assert not list(session.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.source_key == stable_id,
            metadb.CatalogLineageFact.destination_key == child_key,
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
    with pytest.raises(RuntimeError, match="stale for the current publication"):
        _record_lineage("s3://external/source", first["uri"], producer="late")
    _record_lineage("s3://external/source", second["uri"], producer="current")
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
    assert any(edge["child"] == second["uri"] for edge in metadb.catalog_lineage_pairs())
    assert any((rel.get("leftUri") or rel.get("left_uri")) == second["uri"]
               for rel in metadb.catalog_relationships())

    # A stale unregister token removes the current logical dataset and fences pre-existing attempts.
    metadb.catalog_delete_entry(first["uri"])
    assert metadb.catalog_get(stable_id) is None
    assert not any(edge["child"] in (first["uri"], second["uri"], stable_id)
                   for edge in metadb.catalog_lineage_pairs())
    assert not any(stable_id in (rel.get("leftUri"), rel.get("rightUri"))
                   for rel in metadb.catalog_relationships())
    stale_mutations = (
        lambda: metadb.catalog_set_metadata(first["uri"], "ghost", None, None, []),
        lambda: metadb.catalog_set_declared_key(first["uri"], ["ghost"]),
        lambda: metadb.catalog_set_embedding(second["uri"], "model", 1, b"ghost"),
        lambda: _record_lineage("s3://external/ghost", first["uri"], producer="ghost"),
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
    assert not any(edge["child"] == fresh["uri"]
                   for edge in metadb.catalog_lineage_pairs())
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
        metadb.put_result(cache_key, _cache_document(region["uri"]))
        canvas_id = f"clone-canvas-{uuid.uuid4().hex}"
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
                name="clone history", version=1, doc="{}"))
        clone_run_id = f"clone-run-{uuid.uuid4().hex}"
        metadb.record_run(
            canvas_id, "n", "run", "done", rows=1,
            outputs=[_run_output(
                region["uri"], rows=1, node_id="n").model_dump()],
            run_id=clone_run_id)
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
                metadb.RunRecord.run_id == clone_run_id))
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
        assert _cached_output(cache_key).uri == region["uri"]
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


def test_app_lifespan_starts_and_stops_background_reapers():
    from fastapi.testclient import TestClient
    from hub import main

    def live_reapers(name: str):
        return [thread for thread in threading.enumerate()
                if thread.name == name and thread.is_alive()]

    object_before = len(live_reapers("dp-object-attempt-reaper"))
    local_before = len(live_reapers("dp-local-result-reaper"))
    with TestClient(main.app):
        assert len(live_reapers("dp-object-attempt-reaper")) == object_before + 1
        assert len(live_reapers("dp-local-result-reaper")) == local_before + 1
        assert main._object_attempt_reaper_thread is not None
        assert main._local_result_reaper_thread is not None
    assert len(live_reapers("dp-object-attempt-reaper")) == object_before
    assert len(live_reapers("dp-local-result-reaper")) == local_before
    assert main._object_attempt_reaper_thread is None
    assert main._local_result_reaper_thread is None


def test_overlapping_app_lifespans_share_background_reapers_until_last_exit():
    from fastapi.testclient import TestClient
    from hub import main

    def live_reapers(name: str):
        return [thread for thread in threading.enumerate()
                if thread.name == name and thread.is_alive()]

    object_before = len(live_reapers("dp-object-attempt-reaper"))
    local_before = len(live_reapers("dp-local-result-reaper"))
    with TestClient(main.app):
        object_thread = main._object_attempt_reaper_thread
        local_thread = main._local_result_reaper_thread
        assert object_thread is not None
        assert local_thread is not None
        assert len(live_reapers("dp-object-attempt-reaper")) == object_before + 1
        assert len(live_reapers("dp-local-result-reaper")) == local_before + 1
        with TestClient(main.app):
            assert main._object_attempt_reaper_thread is object_thread
            assert main._local_result_reaper_thread is local_thread
            assert len(live_reapers("dp-object-attempt-reaper")) == object_before + 1
            assert len(live_reapers("dp-local-result-reaper")) == local_before + 1
        assert main._object_attempt_reaper_thread is object_thread
        assert main._local_result_reaper_thread is local_thread
        assert object_thread.is_alive()
        assert local_thread.is_alive()
    assert len(live_reapers("dp-object-attempt-reaper")) == object_before
    assert len(live_reapers("dp-local-result-reaper")) == local_before
    assert main._object_attempt_reaper_thread is None
    assert main._local_result_reaper_thread is None


@pytest.mark.parametrize("versioning_enabled", [False, True], ids=["unversioned", "versioned"])
def test_moto_exact_root_marker_is_inventoried_and_exactly_reaped(
        versioning_enabled, object_store_cred):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("pyarrow")
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)
    server.start()
    handle = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        mode = "versioned" if versioning_enabled else "unversioned"
        bucket = f"lifecycle-root-marker-{mode}"
        client.create_bucket(Bucket=bucket)
        if versioning_enabled:
            client.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })

        token = uuid.uuid4().hex
        logical = f"s3://{bucket}/results/{token}.parquet"
        handle = _handle(
            "sink", logical=logical, allocation_key=f"root-marker-{mode}-{token}")
        attempt_key = urlsplit(handle["uri"]).path.lstrip("/").rstrip("/")
        root_marker_key = attempt_key + "/"
        shard_key = attempt_key + "/part-00000.parquet"
        sibling_key = f"results/manual-sibling-{token}.parquet"
        client.put_object(Bucket=bucket, Key=root_marker_key, Body=b"")
        client.put_object(Bucket=bucket, Key=shard_key, Body=b"parquet-test-data")
        client.put_object(Bucket=bucket, Key=sibling_key, Body=b"keep")
        handoff.write_manifest(
            handle["uri"], run_id=handle["run_id"], rows=1, schema="value: int64")

        handoff.prepare_attempt_commit(handle["uri"])
        inventory = metadb.object_attempt_inventory(handle["uri"])
        root_marker = [
            item for item in inventory
            if item["key"] == f"{bucket}/{root_marker_key}"
        ]
        assert len(root_marker) == 1
        assert root_marker[0]["size"] == 0
        assert root_marker[0]["member_type"] == (
            "object_version" if versioning_enabled else "unversioned_object")

        name = f"root-marker-{mode}-{token}"
        metadb.catalog_upsert_entry(handle["uri"], name, {
            "id": "ignored", "name": name, "uri": handle["uri"],
        })
        assert _state(handle["uri"]) == "published"
        with metadb.session() as session:
            assert session.scalar(select(metadb.ObjectAttemptRef).where(
                metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
                metadb.ObjectAttemptRef.ref_type == "catalog",
            )) is not None

        metadb.catalog_delete_entry(handle["uri"])
        assert _state(handle["uri"]) == "superseded"
        with metadb.session() as session:
            assert session.scalar(select(metadb.ObjectAttemptRef).where(
                metadb.ObjectAttemptRef.attempt_uri == handle["uri"],
            )) is None

        first = handoff.reap_attempts(retention_seconds=0, delete_grace_seconds=0)
        assert handle["uri"] in first["observed"]
        assert _state(handle["uri"]) == "delete_verifying"
        assert metadb.object_attempt_inventory(handle["uri"], pending_only=True) == []
        _force_delete_verification_due(handle["uri"])
        assert handle["uri"] in handoff.reap_attempts(
            retention_seconds=0, delete_grace_seconds=0)["deleted"]

        assert client.list_objects_v2(Bucket=bucket, Prefix=attempt_key).get("Contents", []) == []
        if versioning_enabled:
            history = client.list_object_versions(Bucket=bucket, Prefix=attempt_key)
            assert history.get("Versions", []) == []
            assert history.get("DeleteMarkers", []) == []
        assert client.get_object(Bucket=bucket, Key=sibling_key)["Body"].read() == b"keep"
        assert _state(handle["uri"]) == "deleted"
        assert any(item["key"] == f"{bucket}/{root_marker_key}"
                   for item in metadb.object_attempt_inventory(handle["uri"]))
    finally:
        if handle is not None and _state(handle["uri"]) != "deleted":
            with contextlib.suppress(Exception):
                metadb.catalog_delete_entry(handle["uri"])
            metadb.quarantine_object_attempt(handle["uri"], "test cleanup")
        object_store_cred(None)
        server.stop()


def test_moto_versioned_s3_history_read_and_exact_sibling_safe_gc(
        monkeypatch, object_store_cred):
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
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })

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
        metadb.put_result(cache_key, _cache_document(first["uri"]))
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="moto history",
                version=1, doc="{}"))
        run_id = f"moto-run-{uuid.uuid4().hex}"
        metadb.record_run(
            canvas_id, "n", "run", "done", rows=1,
            outputs=[_run_output(
                first["uri"], rows=1, node_id="n").model_dump()],
            run_id=run_id)
        metadb.save_run_state(
            run_id, _pending_run_state_document(run_id, node_id="n"),
            canvas_id=canvas_id)
        metadb.save_run_state(
            run_id, _run_state_document(run_id, first["uri"], node_id="n"),
            canvas_id=canvas_id)

        second = _handle(logical=logical, allocation_key=f"moto-second-{uuid.uuid4().hex}")
        land(second, 2)
        metadb.put_result(cache_key, _cache_document(second["uri"]))
        assert _state(first["uri"]) == "published", "run history must keep the old version readable"
        class _HistoryAdapter:
            def preview_scan(self, _uri, _columns=None, limit=None):
                rel = db.conn().from_arrow(pa.table({"value": [1]}))
                return rel.limit(limit) if limit is not None else rel

            def metadata_count(self, _uri):
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
        _retire_result_cache(cache_key)
        if second is not None:
            metadb.quarantine_object_attempt(second["uri"], "test cleanup")
        object_store_cred(None)
        server.stop()


def test_moto_terminal_manifest_cleans_leftover_multipart_or_schedules_exact_gc(
        monkeypatch, object_store_cred):
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
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })

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
        object_store_cred(None)
        server.stop()


@pytest.mark.parametrize("backend", ["local-subprocess", "local-pool"])
def test_moto_subprocess_backends_publish_parent_owned_object_full_result(
        tmp_path, monkeypatch, backend, object_store_cred):
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
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
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
            "nodes": [
                {
                    "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                    "data": {"config": {"uri": str(source)}},
                },
                {
                    "id": "branches", "type": "section", "position": {"x": 200, "y": 0},
                    "data": {"config": {
                        "script": (
                            "emit('first', inputs['in'])\n"
                            "emit('second', inputs['in'])\n"),
                        "outputs": ["first", "second"], "params": {}, "maxRuns": 10,
                    }},
                },
            ],
            "edges": [{"id": "source-branches", "source": "source", "target": "branches"}],
        })
        plan = compiler.compile_plan(
            graph, "branches", deps.registry, deps.node_specs, deps.node_ir)
        status = runner.run(plan, graph, "branches", "local")
        deadline = time.monotonic() + 30
        while runner.status(status.run_id).status not in ("done", "failed", "cancelled"):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        final = runner.status(status.run_id)
        assert final.status == "done", final.error
        assert [(output.port_id, output.outcome, output.rows) for output in final.outputs] == [
            ("first", "committed", 3), ("second", "committed", 3)]
        assert len({output.uri for output in final.outputs}) == 2
        for output in final.outputs:
            assert output.uri and output.table is None
            result_key = urlsplit(output.uri).path.lstrip("/").rstrip("/") \
                + "/part-00000.parquet"
            result_bytes = client.get_object(
                Bucket="subprocess-full-result", Key=result_key)["Body"].read()
            assert pq.read_table(pa.BufferReader(result_bytes)).num_rows == 3
        with metadb.session() as session:
            for output in final.outputs:
                attempt = session.get(metadb.ObjectAttempt, output.uri)
                assert attempt is not None and attempt.state == "published"
                ref = session.get(metadb.ObjectAttemptRef, {
                    "ref_type": "run_state", "ref_key": final.run_id,
                    "ref_slot": metadb.run_output_ref_slot(output.node_id, output.port_id),
                })
                assert ref is not None and ref.attempt_uri == output.uri
        restarted = RunStatus.model_validate(metadb.get_run_state(final.run_id))
        assert [output.model_dump() for output in restarted.outputs] == [
            output.model_dump() for output in final.outputs]
        phash = deps.runner._plan_hash(graph, "branches")
        cache_deadline = time.monotonic() + 2
        while metadb.get_result(phash) is None and time.monotonic() < cache_deadline:
            # The durable RunState is the terminal publication point; the reusable cache pointer is a
            # best-effort secondary write performed immediately afterward.
            time.sleep(0.01)
        cached = metadb.get_result(phash)
        assert cached is not None
        assert [output["uri"] for output in cached["outputs"]] == [
            output.uri for output in final.outputs]

        _retire_terminal_run_state(final.run_id)
        _retire_result_cache(phash)
        for output in final.outputs:
            metadb.quarantine_object_attempt(output.uri, "test cleanup")
    finally:
        if runner is not None:
            runner._terminate_all()
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        server.stop()


@pytest.mark.parametrize("backend", ["local-subprocess", "local-pool"])
def test_moto_subprocess_backends_publish_parent_owned_managed_sink(
        tmp_path, monkeypatch, backend, object_store_cred):
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
    isolated_url = f"sqlite:///{tmp_path / 'parent-sink-metadata.db'}"
    server = ThreadedMotoServer(port=0)
    server.start()
    runner = None
    published_uri = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket="subprocess-managed-sink")
        client.put_bucket_versioning(
            Bucket="subprocess-managed-sink", VersioningConfiguration={"Status": "Enabled"})

        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = isolated_url
        metadb._engine = metadb._Session = None
        metadb.init_db()
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
        monkeypatch.setenv("DP_STORAGE_URL", "s3://subprocess-managed-sink/results")
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

        class ParquetProbe:
            @staticmethod
            def _metadata(uri):
                parsed = urlsplit(uri)
                key = parsed.path.lstrip("/").rstrip("/") + "/part-00000.parquet"
                body = client.get_object(Bucket=parsed.netloc, Key=key)["Body"].read()
                return pq.ParquetFile(pa.BufferReader(body)).metadata

            def schema(self, uri):
                from hub.models import ColumnSchema

                metadata = self._metadata(uri)
                return [ColumnSchema(name=metadata.schema.column(i).name,
                                     type=str(metadata.schema.column(i).physical_type))
                        for i in range(metadata.num_columns)]

            def count(self, uri):
                return self._metadata(uri).num_rows

            @staticmethod
            def fingerprint(_uri):
                return "subprocess-managed-sink-test"

        # Filesystem clients can stall on Moto's S3 glob transport. The ownership contract under test still
        # performs the real child S3 write, parent inventory proof, and metadata/catalog transaction;
        # use Moto's boto3 client only for the catalog's schema/count read-back.
        monkeypatch.setattr(deps.catalog, "resolve", lambda _uri: ParquetProbe())
        monkeypatch.setattr(deps.catalog, "_object_stat_sig", lambda _uri: "")
        runner = next(candidate for candidate in deps.runners if candidate.name == backend)
        graph = Graph.model_validate({
            "id": f"{backend}-managed-sink", "version": 1,
            "nodes": [
                {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                 "data": {"config": {"uri": str(source)}}},
                {"id": "write", "type": "write", "position": {"x": 200, "y": 0},
                 "data": {"config": {"filename": "daily.parquet",
                                       "writeMode": "overwrite"}}},
            ],
            "edges": [{"id": "source-write", "source": "source", "target": "write",
                       "data": {"wire": "dataset"}}],
        })
        plan = compiler.compile_plan(
            graph, "write", deps.registry, deps.node_specs, deps.node_ir)
        status = runner.run(plan, graph, "write", "local")
        deadline = time.monotonic() + 30
        while runner.status(status.run_id).status not in ("done", "failed", "cancelled"):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        final = runner.status(status.run_id)
        assert final.status == "done", final.error
        assert len(final.outputs) == 1
        output = final.outputs[0]
        assert output.outcome == "committed" and output.uri and output.table == "daily"
        published_uri = output.uri
        logical_uri = "s3://subprocess-managed-sink/results/daily.parquet"
        assert metadb.catalog_get(logical_uri)["uri"] == published_uri
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, published_uri)
            assert attempt is not None and attempt.kind == "sink" and attempt.state == "published"
            assert session.scalar(select(metadb.ObjectAttemptRef).where(
                metadb.ObjectAttemptRef.ref_type == "catalog",
                metadb.ObjectAttemptRef.attempt_uri == published_uri,
            )) is not None
        assert any(edge["parent"] == str(source) and edge["child"] == published_uri
                   for edge in metadb.catalog_lineage_pairs())
        result_key = urlsplit(published_uri).path.lstrip("/").rstrip("/") \
            + "/part-00000.parquet"
        result_bytes = client.get_object(
            Bucket="subprocess-managed-sink", Key=result_key)["Body"].read()
        assert pq.read_table(pa.BufferReader(result_bytes)).num_rows == 3
    finally:
        if runner is not None:
            runner._terminate_all()
        if published_uri is not None:
            with contextlib.suppress(Exception):
                metadb.catalog_delete_entry(published_uri)
            with contextlib.suppress(Exception):
                metadb.quarantine_object_attempt(published_uri, "test cleanup")
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        server.stop()


def test_subprocess_parent_attests_and_prepares_managed_sink_before_publish(
        tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.plugins import catalog as catalog_mod
    from hub.plugins.catalog import lineage_for_output
    from hub.subprocess_runner import SubprocessRunner

    attempt_uri = "s3://managed/results/daily.attempt-parent"
    events = []
    monkeypatch.setattr(
        handoff, "prepare_attempt_commit",
        lambda uri: events.append(("prepare", uri)))

    def publish(**kwargs):
        events.append(("publish", kwargs))
        return {
            "uri": kwargs["uri"],
            "table": {
                "uri": kwargs["uri"], "name": kwargs["name"],
                "version": "v-parent-managed",
            },
        }

    monkeypatch.setattr(catalog_mod, "core_managed_publisher", lambda _catalog: publish)
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=object())
    lineage = lineage_for_output(Graph(id="canvas", version=1), "run", "write")
    sinks = {"write": {
        "uri": attempt_uri, "logical_uri": "s3://managed/results/daily.parquet",
        "name": "daily", "parents": ["s3://managed/source.parquet"],
        "lineage": lineage,
    }}
    status = RunStatus(
        run_id="run", status="done", per_node=[], target_node_id="write",
        outputs=[_run_output(
            attempt_uri, rows=1, node_id="write", table="daily",
            version="v-child-untrusted")])
    runner._publish_object_sinks(sinks, status)
    assert events == [
        ("prepare", attempt_uri),
        ("publish", {
            "name": "daily", "uri": attempt_uri, "version": None,
            "parents": ["s3://managed/source.parquet"], "pipeline": "canvas",
            "lineage": lineage,
        }),
    ]
    assert status.outputs[0].version == "v-parent-managed"

    with pytest.raises(RuntimeError, match="unexpected output binding"):
        runner._publish_object_sinks(sinks, RunStatus(
            run_id="run", status="done", per_node=[], target_node_id="write",
            outputs=[_run_output(
                attempt_uri, rows=1, node_id="write", table="wrong")]))
    assert len(events) == 2


@pytest.mark.parametrize("object_backed", [False, True], ids=["unmanaged", "managed"])
def test_subprocess_multi_sink_fails_before_allocation_or_dispatch(
        tmp_path, monkeypatch, object_backed):
    from hub.models import CompilePlan, Graph, PlanStep
    from hub.plugins import catalog as catalog_mod
    from hub.plugins.adapters import DuckDBAdapter
    from hub.run_outputs import UnsupportedRunOutputs
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    calls = []

    class ObjectStorage:
        @staticmethod
        def output_uri(name, ext):
            if object_backed:
                return f"s3://managed/results/{name}{ext}"
            return str(tmp_path / f"{name}{ext}")

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            return None

    monkeypatch.setattr(
        catalog_mod, "core_managed_publisher", lambda _catalog: lambda **_kwargs: None)
    monkeypatch.setattr(
        handoff, "allocate_attempt", lambda **_kwargs: calls.append("allocate"))
    monkeypatch.setattr(
        subprocess_runner.subprocess, "Popen", lambda *_args, **_kwargs: calls.append("popen"))
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), catalog=Catalog(), storage=ObjectStorage(),
        resolve_adapter=lambda _uri: DuckDBAdapter())
    graph = Graph.model_validate({
        "id": "multi-sink", "version": 1,
        "nodes": [
            {"id": "first", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {"filename": "first.parquet"}}},
            {"id": "second", "type": "write", "position": {"x": 0, "y": 100},
             "data": {"config": {"filename": "second.parquet"}}},
        ],
        "edges": [],
    })
    plan = CompilePlan(target_node_id="first", steps=[
        PlanStep(node_id="first", kind="write", label="first"),
        PlanStep(node_id="second", kind="write", label="second"),
    ])
    with pytest.raises(
            UnsupportedRunOutputs, match="do not yet support multiple write outputs"):
        runner.run(plan, graph, "first", "local")
    assert calls == []


@pytest.mark.parametrize("catalog", [None, object()], ids=["missing", "not-callable"])
def test_subprocess_unmanaged_sink_requires_parent_catalog_before_dispatch(
        tmp_path, monkeypatch, catalog):
    from hub.models import CompilePlan, Graph, PlanStep
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    calls = []

    class LocalStorage:
        @staticmethod
        def output_uri(name, ext):
            return str(tmp_path / f"{name}{ext}")

    monkeypatch.setattr(
        subprocess_runner.subprocess, "Popen", lambda *_args, **_kwargs: calls.append("popen"))
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), catalog=catalog, storage=LocalStorage())
    graph = Graph.model_validate({
        "id": "unmanaged-sink", "version": 1,
        "nodes": [
            {"id": "write", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {"filename": "out.csv"}}},
        ],
        "edges": [],
    })
    plan = CompilePlan(target_node_id="write", steps=[
        PlanStep(node_id="write", kind="write", label="write"),
    ])
    with pytest.raises(RuntimeError, match="require parent catalog registration"):
        runner.run(plan, graph, "write", "local")
    assert calls == []


@pytest.mark.parametrize("case", [
    "local-append", "local-partitioned", "object-append", "local-overwrite",
])
def test_subprocess_parent_contract_uses_shared_published_sink_uri(tmp_path, case):
    from hub.models import CompilePlan, Graph, PlanStep
    from hub.plugins.adapters import DuckDBAdapter
    from hub.subprocess_runner import SubprocessRunner

    if case == "local-append":
        target = str(tmp_path / "out.csv")
        config = {"filename": "out.csv", "writeMode": "append"}
        expected = str(tmp_path / "out")
    elif case == "local-partitioned":
        target = str(tmp_path / "out.parquet")
        config = {"filename": "out.parquet", "partitionBy": "category"}
        expected = str(tmp_path / "out")
    elif case == "object-append":
        target = "s3://contract-tests/results/out.parquet"
        config = {"filename": "out.parquet", "writeMode": "append"}
        expected = "s3://contract-tests/results/out"
    else:
        target = str(tmp_path / "out.csv")
        config = {"filename": "out.csv", "writeMode": "overwrite"}
        expected = target

    class Storage:
        @staticmethod
        def output_uri(_name, _ext):
            return target

    class Catalog:
        register_output = get_table = staticmethod(lambda **_kwargs: None)

    graph = Graph.model_validate({
        "id": f"contract-{case}", "version": 1,
        "nodes": [{"id": "write", "type": "write", "position": {"x": 0, "y": 0},
                   "data": {"config": config}}],
        "edges": [],
    })
    plan = CompilePlan(target_node_id="write", steps=[
        PlanStep(node_id="write", kind="write", label="write"),
    ])
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), catalog=Catalog(), storage=Storage(),
        resolve_adapter=lambda _uri: DuckDBAdapter())
    status = RunStatus(
        run_id="run", status="queued", target_node_id="write",
        outputs=[require_single_run_output(graph, "write", runner.node_specs)],
    )
    targets, attempts, contracts = runner._claim_sink_contracts(
        plan, graph, "run", status)
    assert targets == {"write": target} and attempts == {}
    assert contracts["write"]["logical_uri"] == target
    assert contracts["write"]["published_uri"] == expected


def test_core_strict_publication_ignores_post_commit_usage_failure(
        tmp_path, monkeypatch):
    from hub.models import ColumnSchema
    from hub.plugins.catalog import InMemoryCatalog

    output_uri = str(tmp_path / "strict-usage.parquet")
    parent_uri = str(tmp_path / "parent.parquet")

    class Probe:
        schema = staticmethod(lambda _uri: [ColumnSchema(name="value", type="BIGINT")])
        count = staticmethod(lambda _uri: 1)
        fingerprint = staticmethod(lambda _uri: "strict-usage")

    catalog = InMemoryCatalog(str(tmp_path / "catalog-data"), lambda _uri: Probe())
    monkeypatch.setattr(
        metadb, "catalog_bump_usage",
        lambda _uri: (_ for _ in ()).throw(RuntimeError("usage unavailable")))
    table = catalog.publish_output_strict(
        name="strict-usage", uri=output_uri, parents=[parent_uri], pipeline="canvas")
    assert table.uri == output_uri
    assert metadb.catalog_get(output_uri)["uri"] == output_uri
    assert any(edge["parent"] == parent_uri and edge["child"] == output_uri
               for edge in metadb.catalog_lineage_pairs())
    metadb.catalog_delete_entry(output_uri)


@pytest.mark.parametrize("managed", [False, True], ids=["unmanaged", "managed"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["failed", "cancelled"])
def test_subprocess_done_status_with_nonzero_exit_never_publishes(
        tmp_path, monkeypatch, managed, cancelled):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    calls = []
    expected_uri = ("s3://managed/results/out.attempt-parent" if managed
                    else str(tmp_path / "out.csv"))
    job_dir = tmp_path / f"nonzero-{managed}-{cancelled}"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1,
        outputs=[_run_output(
            expected_uri, rows=1, node_id="write", table="out")],
    ).model_dump()))

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            calls.append("register")

        @staticmethod
        def get_table(_uri):
            calls.append("readback")

    class FinishedProcess:
        returncode = 7

        @staticmethod
        def poll():
            return 7

        @staticmethod
        def wait(timeout=None):
            return 7

    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=Catalog())
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(
            node_id="write", table="out", outcome="pending")])
    runner._sink_contracts["run"] = {"write": {
        "logical_uri": ("s3://managed/results/out.parquet" if managed else expected_uri),
        "published_uri": expected_uri, "name": "out", "parents": [],
    }}
    if managed:
        runner._object_sinks["run"] = {"write": {
            "uri": expected_uri, "logical_uri": "s3://managed/results/out.parquet",
            "name": "out", "parents": [],
        }}
        monkeypatch.setattr(
            runner, "_publish_object_sinks", lambda *_args: calls.append("publish"))
        monkeypatch.setattr(
            runner, "_discard_object_sinks", lambda _sinks: calls.append("discard"))
    if cancelled:
        runner._cancelled.add("run")
    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "nonzero", "version": 1,
                              "nodes": [], "edges": []}), "write")
    final = runner.status("run")
    assert final.status == ("cancelled" if cancelled else "failed")
    assert final.outputs[0].outcome == final.status
    assert final.outputs[0].uri is None and final.outputs[0].table is None
    assert calls == (["discard"] if managed else [])


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
    child_output = (_run_output(handle["uri"], rows=1)
                    if child_status == "done" else _run_output(outcome="failed"))
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status=child_status, per_node=[], target_node_id="source",
        total_rows=1 if child_status == "done" else None, outputs=[child_output],
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
        run_id=handle["run_id"], status="running", per_node=[],
        target_node_id="source", outputs=[_run_output(outcome="pending")])
    runner._published_statuses[handle["run_id"]] = runner.runs[
        handle["run_id"]].model_copy(deep=True)
    runner._object_results[handle["run_id"]] = _object_result_owner(handle)
    process = FinishedProcess()
    runner._process_scopes[handle["run_id"]] = OwnedProcessScope(
        process, owns_process_group=False)
    if cancelled:
        runner._cancelled.add(handle["run_id"])
    original_discard = handoff.discard_attempt

    def discard_after_reap(uri):
        assert events == ["reaped"]
        original_discard(uri)

    handoff.discard_attempt = discard_after_reap
    try:
        runner._watch(
            handle["run_id"], process, str(status_file), str(job_dir),
            Graph.model_validate({"id": "subprocess-fail", "version": 1,
                                  "nodes": [], "edges": []}), "source")
        final = runner.status(handle["run_id"])
        assert final.status == expected
        assert final.outputs[0].outcome == expected and final.outputs[0].uri is None
        assert _state(handle["uri"]) == "abandoned"
    finally:
        handoff.discard_attempt = original_discard
        handoff.set_managed_object_provider(None)
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


@pytest.mark.parametrize("child_status,cancelled,expected", [
    ("failed", False, "failed"),
    ("done", True, "cancelled"),
])
def test_subprocess_parent_discards_managed_sink_only_after_child_reaped(
        tmp_path, child_status, cancelled, expected):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    handle = _handle(
        "sink", logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/subprocess-sink.parquet")
    provider = _ExactProvider([])
    handoff.set_managed_object_provider(provider)
    job_dir = tmp_path / f"sink-job-{child_status}-{cancelled}"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    child_output = (_run_output(
        handle["uri"], rows=1, node_id="write", table="daily")
        if child_status == "done" else _run_output(
            node_id="write", table="daily", outcome="failed"))
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status=child_status, per_node=[], target_node_id="write",
        total_rows=1 if child_status == "done" else None, outputs=[child_output],
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
        run_id=handle["run_id"], status="running", per_node=[],
        target_node_id="write", outputs=[_run_output(
            node_id="write", table="daily", outcome="pending")])
    runner._object_sinks[handle["run_id"]] = {"write": {
        "uri": handle["uri"], "logical_uri": handle["logical_uri"],
        "name": "daily", "parents": [],
    }}
    process = FinishedProcess()
    runner._process_scopes[handle["run_id"]] = OwnedProcessScope(
        process, owns_process_group=False)
    if cancelled:
        runner._cancelled.add(handle["run_id"])
    original_discard = handoff.discard_attempt

    def discard_after_reap(uri):
        assert events == ["reaped"]
        original_discard(uri)

    handoff.discard_attempt = discard_after_reap
    try:
        runner._watch(
            handle["run_id"], process, str(status_file), str(job_dir),
            Graph.model_validate({"id": "subprocess-sink", "version": 1,
                                  "nodes": [], "edges": []}), "write")
        final = runner.status(handle["run_id"])
        assert final.status == expected
        assert final.outputs[0].outcome == expected
        assert final.outputs[0].uri is None and final.outputs[0].table is None
        assert _state(handle["uri"]) == "abandoned"
    finally:
        handoff.discard_attempt = original_discard
        handoff.set_managed_object_provider(None)
        metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_subprocess_parent_catalog_failure_is_generic_and_clears_output(
        tmp_path, caplog):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    job_dir = tmp_path / "unmanaged-catalog-failure"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1, outputs=[_run_output(
            str(tmp_path / "out.parquet"), rows=1, node_id="write", table="out")],
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    class FailingCatalog:
        @staticmethod
        def register_output(**_kwargs):
            raise RuntimeError("secret-provider-detail")

        @staticmethod
        def get_table(_uri):
            raise AssertionError("read-back must not run after registration fails")

    caplog.set_level("ERROR", logger="hub")
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=FailingCatalog())
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(node_id="write", table="out", outcome="pending")])
    runner._sink_contracts["run"] = {"write": {
        "logical_uri": str(tmp_path / "out.parquet"),
        "published_uri": str(tmp_path / "out.parquet"), "name": "out", "parents": [],
        "lineage": LineagePublication.model_validate(_lineage()),
    }}
    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "catalog-failure", "version": 1,
                              "nodes": [], "edges": []}), "write")
    final = runner.status("run")
    assert final.status == "failed"
    assert final.error == "parent catalog registration failed"
    assert final.outputs[0].outcome == "failed"
    assert final.outputs[0].uri is None and final.outputs[0].table is None
    assert "secret-provider-detail" not in final.error
    assert "secret-provider-detail" in caplog.text


def test_subprocess_builtin_catalog_strict_publish_rejects_same_uri_persist_failure(
        tmp_path, monkeypatch, caplog):
    from hub.models import ColumnSchema, Graph, RunStatus
    from hub.plugins.catalog import InMemoryCatalog
    from hub.subprocess_runner import SubprocessRunner

    output_uri = str(tmp_path / "durable-readback.parquet")
    job_dir = tmp_path / "durable-readback-job"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1, outputs=[_run_output(
            output_uri, rows=1, node_id="write", table="durable-readback")],
    ).model_dump()))

    class Probe:
        @staticmethod
        def schema(_uri):
            return [ColumnSchema(name="value", type="BIGINT")]

        @staticmethod
        def count(_uri):
            return 1

        @staticmethod
        def fingerprint(_uri):
            return "durable-readback"

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    catalog = InMemoryCatalog(str(tmp_path / "catalog-data"), lambda _uri: Probe())
    prior = catalog.register_output(name="durable-readback", uri=output_uri, parents=[])
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=catalog)
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(
            node_id="write", table="durable-readback", outcome="pending")])
    runner._sink_contracts["run"] = {"write": {
        "logical_uri": output_uri, "published_uri": output_uri,
        "name": "durable-readback", "parents": [],
        "lineage": LineagePublication.model_validate(_lineage()),
    }}
    monkeypatch.setattr(
        metadb, "catalog_upsert_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret-metadb-persist-detail")))
    caplog.set_level("WARNING", logger="hub")

    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "durable-readback", "version": 1,
                              "nodes": [], "edges": []}), "write")
    final = runner.status("run")
    assert final.status == "failed"
    assert final.error == "parent catalog registration failed"
    assert final.outputs[0].outcome == "failed"
    assert final.outputs[0].uri is None and final.outputs[0].table is None
    assert "secret-metadb-persist-detail" not in final.error
    assert "secret-metadb-persist-detail" in caplog.text
    assert catalog.get_table(output_uri).version == prior.version


def test_subprocess_unmanaged_uri_mismatch_never_calls_catalog(tmp_path):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    calls = []
    expected_uri = str(tmp_path / "expected.csv")
    returned_uri = str(tmp_path / "unexpected.csv")
    job_dir = tmp_path / "unmanaged-uri-mismatch"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1, outputs=[_run_output(
            returned_uri, rows=1, node_id="write", table="expected")],
    ).model_dump()))

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            calls.append("register")
            raise AssertionError("mismatched output must not reach catalog")

        @staticmethod
        def get_table(_uri):
            calls.append("readback")
            raise AssertionError("mismatched output must not reach catalog")

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=Catalog())
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(
            node_id="write", table="expected", outcome="pending")])
    runner._sink_contracts["run"] = {"write": {
        "logical_uri": expected_uri, "published_uri": expected_uri,
        "name": "expected", "parents": [],
    }}
    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "uri-mismatch", "version": 1,
                              "nodes": [], "edges": []}), "write")
    final = runner.status("run")
    assert final.status == "failed"
    assert final.error == "parent catalog registration failed"
    assert final.outputs[0].outcome == "failed"
    assert final.outputs[0].uri is None and final.outputs[0].table is None
    assert calls == []


def test_subprocess_parent_registers_unmanaged_sink_with_source_lineage_across_region_cut(tmp_path):
    from hub import graph as graph_mod
    from hub.models import CompilePlan, Graph, PlanStep, ResourceSpec, RunStatus
    from hub.planner import Region
    from hub.run_controller import RunController
    from hub.subprocess_runner import SubprocessRunner

    source_a = str(tmp_path / "source-a.parquet")
    source_b = str(tmp_path / "source-b.parquet")
    registered = {}

    class LocalStorage:
        @staticmethod
        def output_uri(name, ext):
            return str(tmp_path / f"{name}{ext}")

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            registered.update(kwargs)
            return {"uri": kwargs["uri"], "name": kwargs["name"], "version": "v1"}

        @staticmethod
        def get_table(uri):
            return {"uri": uri, "name": registered["name"], "version": "v1"}

    graph = Graph.model_validate({
        "id": "unmanaged-lineage", "version": 1,
        "nodes": [
            {"id": "source-a", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": source_a}}},
            {"id": "source-b", "type": "source", "position": {"x": 0, "y": 100},
             "data": {"config": {"uri": source_b}}},
            {"id": "join", "type": "join", "position": {"x": 100, "y": 50},
             "data": {"config": {"leftOn": "id", "rightOn": "id"}}},
            {"id": "transform", "type": "transform", "position": {"x": 100, "y": 0},
             "data": {"config": {"mode": "map", "code": "def fn(row): return row"}}},
            {"id": "write", "type": "write", "position": {"x": 200, "y": 0},
             "data": {"config": {"filename": "derived.csv"}}},
        ],
        "edges": [
            {"id": "a-join", "source": "source-a", "target": "join", "targetHandle": "a"},
            {"id": "b-join", "source": "source-b", "target": "join", "targetHandle": "b"},
            {"id": "join-transform", "source": "join", "target": "transform"},
            {"id": "transform-write", "source": "transform", "target": "write"},
        ],
    })
    expected_parents = graph_mod.all_upstream_source_uris(graph, "write")
    region = Region(
        id="final", node_ids={"write"}, output_node="write", backend="default",
        worker=None, requires=ResourceSpec(),
        cut_inputs=[("transform", None, "write", None)],
    )
    region_ref = str(tmp_path / "region-ref.parquet")
    graph = RunController._subgraph(
        None, graph, region, {"transform": region_ref})
    ref_node_id = next(
        node.id for node in graph.nodes
        if (node.data.get("config") or {}).get("uri") == region_ref
    )
    plan = CompilePlan(target_node_id="write", steps=[
        PlanStep(node_id=ref_node_id, kind="read", label="source"),
        PlanStep(node_id="write", kind="write", label="write"),
    ])
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), catalog=Catalog(), storage=LocalStorage())
    claim_status = RunStatus(
        run_id="run", status="queued", target_node_id="write",
        outputs=[require_single_run_output(graph, "write", runner.node_specs)],
    )
    _targets, _attempts, contracts = runner._claim_sink_contracts(
        plan, graph, "run", claim_status)
    assert set(expected_parents) == {source_a, source_b}
    assert graph_mod.execution_source_uris(graph, "write") == [region_ref]
    assert contracts["write"]["parents"] == metadb.catalog_lineage_parent_tokens(
        expected_parents)
    runner._sink_contracts["run"] = contracts
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(
            node_id="write", table="derived", outcome="pending")])
    job_dir = tmp_path / "lineage-job"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1, outputs=[_run_output(
            str(tmp_path / "derived.csv"), rows=1, node_id="write", table="derived",
            version="v-child-untrusted")],
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir), graph, "write")
    final = runner.status("run")
    assert final.status == "done"
    assert final.outputs[0].version == "v1"
    assert registered["parents"] == metadb.catalog_lineage_parent_tokens(
        expected_parents)


def test_isolated_child_adapter_disagreement_never_writes_assigned_sink(tmp_path):
    from hub.models import Graph, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    writes = []

    class ChildPluginAdapter:
        @staticmethod
        def write(*_args, **_kwargs):
            writes.append(True)
            return {"rows": 1}

    class Engine:
        @staticmethod
        def relation(_node_id, _source_handle=None):
            return type("Relation", (), {"columns": ["value"], "types": ["BIGINT"]})()

    class Storage:
        @staticmethod
        def output_uri(name, ext):
            return f"s3://managed/results/{name}{ext}"

    graph = Graph.model_validate({
        "id": "adapter-disagreement", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": str(tmp_path / "source.parquet")}}},
            {"id": "write", "type": "write", "position": {"x": 100, "y": 0},
             "data": {"config": {"filename": "out.parquet"}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    runner = LocalRunner(
        lambda _uri: ChildPluginAdapter(), None, object(), str(tmp_path), storage=Storage())
    runner.forced_sink_targets = {"write": "s3://managed/results/out.parquet"}
    runner.forced_sink_attempts = {
        "write": "s3://managed/results/out.attempt-parent",
    }
    with pytest.raises(RuntimeError, match="parent and child disagree"):
        runner._commit_write(
            next(node for node in graph.nodes if node.id == "write"), graph, Engine(),
            RunStatus(
                run_id="run", status="running", target_node_id="write", per_node=[],
                outputs=[_run_output(
                    node_id="write", outcome="pending", publication_kind="catalog")]),
            None, _CancelToken())
    assert writes == []


def test_subprocess_managed_sink_cleanup_outage_does_not_block_terminalization(
        tmp_path, monkeypatch, caplog):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    attempt_uri = "s3://managed/results/daily.attempt-parent"
    job_dir = tmp_path / "managed-cleanup-outage"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="write",
        total_rows=1, outputs=[_run_output(
            attempt_uri, rows=1, node_id="write", table="daily")],
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    caplog.set_level("ERROR", logger="hub")
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner.runs["run"] = RunStatus(
        run_id="run", status="running", per_node=[], target_node_id="write",
        outputs=[_run_output(
            node_id="write", table="daily", outcome="pending")])
    runner._procs["run"] = FinishedProcess()
    runner._object_sinks["run"] = {"write": {
        "uri": attempt_uri, "logical_uri": "s3://managed/results/daily.parquet",
        "name": "daily", "parents": [],
    }}
    monkeypatch.setattr(
        runner, "_publish_object_sinks",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("secret-publish-detail")))
    monkeypatch.setattr(
        metadb, "abandon_committed_object_attempt",
        lambda _uri: (_ for _ in ()).throw(RuntimeError("metadata unavailable")))

    runner._watch(
        "run", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "cleanup-outage", "version": 1,
                              "nodes": [], "edges": []}), "write")
    final = runner.status("run")
    assert final.status == "failed"
    assert final.error == "parent managed-sink publication failed"
    assert final.outputs[0].outcome == "failed"
    assert final.outputs[0].uri is None and final.outputs[0].table is None
    assert "secret-publish-detail" not in final.error
    assert "secret-publish-detail" in caplog.text
    assert "metadata unavailable" in caplog.text
    assert "run" not in runner._procs and "run" not in runner._object_sinks
    assert not job_dir.exists()


def test_subprocess_object_result_commit_unknown_uses_exact_run_state_receipt(
        tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    handle = _handle(logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/persist-fail.parquet")
    job_dir = tmp_path / "job-persist-fail"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", per_node=[], target_node_id="source",
        total_rows=1, outputs=[_run_output(handle["uri"], rows=1)],
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
        run_id=handle["run_id"], status="running", per_node=[],
        target_node_id="source", outputs=[_run_output(outcome="pending")])
    runner._published_statuses[handle["run_id"]] = runner.runs[
        handle["run_id"]].model_copy(deep=True)
    runner._object_results[handle["run_id"]] = _object_result_owner(handle)
    metadb.save_run_state(
        handle["run_id"], runner.runs[handle["run_id"]].model_dump())

    def persist_then_lose_response(_graph, status):
        metadb.save_run_state(
            handle["run_id"], status.model_dump(),
            publish_region=status.status in ("done", "failed"))
        if status.status == "done":
            raise RuntimeError("database response lost after commit")

    runner.on_status = persist_then_lose_response
    monkeypatch.setattr(
        "hub.subprocess_runner._validate_object_result_commit", lambda *_args: None)
    monkeypatch.setattr(handoff, "prepare_attempt_commit", lambda _uri: _commit(handle))
    runner._watch(
        handle["run_id"], FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "persist-fail", "version": 1,
                              "nodes": [], "edges": []}), "source")
    final = runner.status(handle["run_id"])
    assert final.status == "done"
    assert final.outputs[0].outcome == "committed"
    assert final.outputs[0].uri == handle["uri"]
    assert _state(handle["uri"]) == "published"
    _retire_terminal_run_state(handle["run_id"])
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_object_result_run_state_receipt_requires_complete_multi_port_generation_set():
    run_id = f"receipt-{uuid.uuid4().hex}"
    handles = [
        _handle(
            logical=f"s3://lifecycle-tests/{uuid.uuid4().hex}/{port}.parquet",
            run_id=run_id, allocation_key=f"{run_id}:{port}")
        for port in ("first", "second")
    ]
    for handle in handles:
        _commit(handle)
    pending = RunStatus(
        run_id=run_id, status="running", target_node_id="branches", outputs=[
            _run_output(node_id="branches", port_id=port, outcome="pending")
            for port in ("first", "second")
        ])
    metadb.save_run_state(run_id, pending.model_dump())
    done = RunStatus(
        run_id=run_id, status="done", target_node_id="branches", outputs=[
            _run_output(
                handle["uri"], rows=3, node_id="branches", port_id=port)
            for handle, port in zip(handles, ("first", "second"))
        ])
    doc = done.model_dump()
    metadb.save_run_state(run_id, doc, publish_region=True)

    assert metadb.object_result_run_state_receipt(
        [handle["uri"] for handle in handles], run_id, doc)
    assert not metadb.object_result_run_state_receipt(
        [handles[0]["uri"]], run_id, doc)
    changed = done.model_copy(deep=True)
    changed.outputs[1].rows = 4
    assert not metadb.object_result_run_state_receipt(
        [handle["uri"] for handle in handles], run_id, changed.model_dump())

    _retire_terminal_run_state(run_id)
    for handle in handles:
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
    monkeypatch.setattr(handoff, "allocate_attempt", lambda **kwargs: {
        "uri": attempt_uri, "logical_uri": kwargs["logical_uri"],
        "attempt_id": "setup", "generation": 1,
        "storage_namespace": "installation",
    })
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
    process = Process()
    runner._procs[run_id] = process
    runner._process_scopes[run_id] = OwnedProcessScope(
        process, owns_process_group=False)
    runner._object_results[run_id] = _object_result_owner(
        "s3://shutdown/out.attempt-owned")
    monkeypatch.setattr(
        handoff, "discard_attempt", lambda _uri: events.append("discard"))
    runner._terminate_all()
    assert events == ["terminate", "wait", "discard"]
    assert run_id in runner._cancelled


@pytest.mark.parametrize("object_backed", [False, True], ids=["local", "object"])
def test_subprocess_run_unit_allocates_exact_object_attempt_before_spawn(
        tmp_path, monkeypatch, object_backed):
    from hub.models import Graph
    from hub.subprocess_runner import SubprocessRunner

    logical = (
        "s3://region-contract/results/out.parquet" if object_backed
        else str(tmp_path / "out.parquet"))
    attempt = "s3://region-contract/results/out.attempt-parent"
    allocations = []
    monkeypatch.setattr(
        handoff, "allocate_attempt",
        lambda **kwargs: allocations.append(kwargs) or {
            "uri": attempt, "logical_uri": kwargs["logical_uri"],
            "attempt_id": "region", "generation": 1,
            "storage_namespace": "installation",
        })
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), resolve_adapter=lambda _uri: object())
    spawned = {}

    def spawn(status, job_extra, graph, target):
        spawned.update(status=status, job_extra=job_extra, graph=graph, target=target)
        if object_backed:
            assert runner._object_results[status.run_id]["results"][0]["uri"] == attempt
        return status

    monkeypatch.setattr(runner, "_spawn", spawn)
    graph = Graph.model_validate({
        "id": "region", "version": 1,
        "nodes": [{
            "id": "result", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": str(tmp_path / "source.parquet")}},
        }],
        "edges": [],
    })
    status = runner.run_unit(graph, "result", logical)

    assert spawned["job_extra"]["runId"] == status.run_id
    assert spawned["job_extra"]["materializeUri"] == (attempt if object_backed else logical)
    if object_backed:
        assert len(allocations) == 1
        assert allocations[0]["kind"] == "region"
        assert allocations[0]["logical_uri"] == logical
        assert runner._object_results[status.run_id]["run_state_owner"] is False
    else:
        assert allocations == [] and status.run_id not in runner._object_results


@pytest.mark.parametrize("managed", [False, True], ids=["local", "managed-object"])
def test_subrun_region_materializer_writes_managed_shard_and_manifest_last(
        tmp_path, monkeypatch, managed):
    from types import SimpleNamespace

    from hub.subrun import _materialize_region

    target = (
        "s3://region-contract/results/out.attempt-parent" if managed
        else str(tmp_path / "out.parquet"))
    writes = []
    manifests = []
    relation = SimpleNamespace(columns=["value"], types=["BIGINT"])

    class Runner:
        @staticmethod
        def _adapter_write(adapter, uri, rel, mode, cancel):
            writes.append((adapter, uri, rel, mode, cancel))
            return {"rows": 3}

    deps = SimpleNamespace(
        runner=Runner(), resolve_adapter=lambda uri: f"adapter:{uri}")
    monkeypatch.setattr(
        handoff, "write_manifest",
        lambda uri, **kwargs: manifests.append((uri, kwargs)))
    cancel = object()
    rows = _materialize_region(
        deps, relation, target, cancel, lambda: False, "unit-parent")

    physical = target.rstrip("/") + "/part-00000.parquet" if managed else target
    assert rows == 3
    assert writes == [(f"adapter:{physical}", physical, relation, "overwrite", cancel)]
    assert manifests == ([(target, {
        "run_id": "unit-parent", "rows": 3, "schema": [("value", "BIGINT")],
    })] if managed else [])


def test_subprocess_region_attempt_is_returned_without_run_state_ownership(
        tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    attempt = "s3://region-contract/results/out.attempt-parent"
    job_dir = tmp_path / "region-job"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child", status="done", target_node_id="result", total_rows=1,
        per_node=[], outputs=[_run_output(attempt, rows=1, node_id="result")],
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    prepared = []
    persisted = []
    monkeypatch.setattr(handoff, "prepare_attempt_commit", prepared.append)
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    parent = RunStatus(
        run_id="unit", status="running", target_node_id="result", per_node=[],
        outputs=[_run_output(node_id="result", outcome="pending")])
    runner.runs["unit"] = parent
    runner._published_statuses["unit"] = parent.model_copy(deep=True)
    runner._internal_runs.add("unit")
    runner._object_results["unit"] = _object_result_owner(
        attempt, node_id="result", run_state_owner=False)
    monkeypatch.setattr(
        "hub.subprocess_runner._validate_object_result_commit", lambda *_args: None)
    runner.on_status = lambda _graph, status: persisted.append(status.model_copy(deep=True))
    runner._watch(
        "unit", FinishedProcess(), str(status_file), str(job_dir),
        Graph.model_validate({"id": "region", "version": 1, "nodes": [], "edges": []}),
        "result")

    final = runner.status("unit")
    output = _committed_output(final)
    assert final.status == "done" and final.target_node_id == "result"
    assert output.uri == attempt and output.rows == 1
    assert prepared == [attempt]
    assert persisted == []
    assert "unit" not in runner._object_results and not job_dir.exists()


def test_subprocess_malformed_child_status_fails_generically_after_reap_and_cleans(
        tmp_path, monkeypatch, caplog):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    job_dir = tmp_path / "malformed-child"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text('{"status":"done","per_node":"SECRET_BAD_SHAPE"}')
    events = []

    class Process:
        returncode = 0
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

        @staticmethod
        def wait(timeout=None):
            events.append("reaped")
            return 0

        @staticmethod
        def terminate():
            raise AssertionError("malformed interim status must not terminate a live child")

    caplog.set_level("ERROR", logger="hub")
    monkeypatch.setattr(
        subprocess_runner, "_safe_abandon_attempt",
        lambda _uri, **_kwargs: events.append("cleanup"))
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner.runs["run"] = RunStatus(run_id="run", status="running", per_node=[])
    process = Process()
    runner._procs["run"] = process
    runner._process_scopes["run"] = OwnedProcessScope(
        process, owns_process_group=False)
    runner._object_results["run"] = _object_result_owner(
        "s3://region-contract/out.attempt-parent")
    runner._watch(
        "run", process, str(status_file), str(job_dir),
        Graph.model_validate({"id": "malformed", "version": 1,
                              "nodes": [], "edges": []}), None)

    final = runner.status("run")
    assert final.status == "failed"
    assert final.error == "execution process exited without a valid terminal status"
    assert "SECRET_BAD_SHAPE" not in final.error
    assert events == ["reaped", "cleanup"]
    assert not job_dir.exists() and runner._procs == {} and runner._object_results == {}


def test_local_cleanup_outages_do_not_replace_primary_failure_or_skip_finally(
        tmp_path, monkeypatch, caplog):
    from hub.models import CompilePlan, Graph, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    attempt = "s3://local-cleanup/results/out.attempt-parent"

    class Adapter:
        @staticmethod
        def fingerprint(_uri):
            return "source"

    class Guard:
        @staticmethod
        def check():
            return None

    class Lease:
        def __enter__(self):
            return Guard()

        def __exit__(self, *_args):
            raise RuntimeError("lease cleanup unavailable")

    runner = LocalRunner(lambda _uri: Adapter(), {}, object(), str(tmp_path))
    graph = Graph.model_validate({
        "id": "cleanup", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"config": {"uri": "s3://local-cleanup/source.parquet"}}}],
        "edges": [],
    })
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", target_node_id="source", per_node=[],
        outputs=[_run_output(node_id="source", outcome="pending")])
    runner._cancel["run"] = _CancelToken()

    def fail_results(_node_id, _engine, _status, *_args):
        runner._owned_object_result_uris["run"] = {attempt}
        raise RuntimeError("primary data failure")

    runner._materialize_results = fail_results
    monkeypatch.setattr(handoff, "managed_read_lease", lambda *_args, **_kwargs: Lease())
    monkeypatch.setattr(
        metadb, "abandon_committed_object_attempt",
        lambda _uri: (_ for _ in ()).throw(RuntimeError("metadata cleanup unavailable")))
    discarded = []
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    caplog.set_level("ERROR", logger="hub")

    runner._execute(
        "run", CompilePlan(target_node_id="source", steps=[]), graph, "source")
    final = runner.status("run")
    assert final.status == "failed" and "primary data failure" in (final.error or "")
    assert "metadata cleanup unavailable" not in (final.error or "")
    assert discarded == []
    assert "metadata cleanup unavailable" in caplog.text
    assert "lease cleanup unavailable" in caplog.text
    assert "run" not in runner._cancel and "run" not in runner._scopes


def test_local_sink_prepublication_fence_blocks_catalog_and_abandons_attempt(
        tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.plugins import catalog as catalog_mod
    from hub.plugins.runner import LocalRunner, _CancelToken

    logical = "s3://lease-fence/results/out.parquet"
    attempt = "s3://lease-fence/results/out.attempt-parent"
    lease_alive = True
    published = []
    discarded = []

    class CoreAdapter:
        @staticmethod
        def write(_uri, _rel, _mode, cancelled=None):
            nonlocal lease_alive
            lease_alive = False
            return {"rows": 1}

    CoreAdapter.__module__ = "hub.plugins.adapters"
    adapter = CoreAdapter()

    class Storage:
        @staticmethod
        def output_uri(_name, _ext):
            return logical

    class Engine:
        @staticmethod
        def relation(_node_id, _source_handle=None):
            return type("Relation", (), {"columns": ["value"], "types": ["BIGINT"]})()

    graph = Graph.model_validate({
        "id": "lease-fence", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": str(tmp_path / "source.parquet")}}},
            {"id": "write", "type": "write", "position": {"x": 100, "y": 0},
             "data": {"config": {"filename": "out.parquet"}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    runner = LocalRunner(
        lambda _uri: adapter, {}, object(), str(tmp_path), storage=Storage())
    monkeypatch.setattr(handoff, "allocate_attempt", lambda **_kwargs: {"uri": attempt})
    monkeypatch.setattr(handoff, "write_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    monkeypatch.setattr(metadb, "abandon_committed_object_attempt", lambda _uri: False)
    monkeypatch.setattr(
        catalog_mod, "core_managed_publisher",
        lambda _catalog: lambda **kwargs: published.append(kwargs))

    def fence(**_kwargs):
        if not lease_alive:
            raise RuntimeError("managed source lease lost during write")

    with pytest.raises(RuntimeError, match="lease lost"):
        runner._commit_write(
            next(node for node in graph.nodes if node.id == "write"), graph, Engine(),
            RunStatus(
                run_id="run", status="running", target_node_id="write", per_node=[],
                outputs=[_run_output(
                    node_id="write", outcome="pending", publication_kind="catalog")]),
            None,
            _CancelToken(), pre_publish=fence)
    assert published == []
    assert discarded == [attempt]


def test_local_successful_sink_publication_is_not_reversed_by_later_lease_renewal(
        tmp_path, monkeypatch):
    from hub.models import CompilePlan, Graph, PerNodeStatus, PlanStep, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    checks = []

    class Adapter:
        @staticmethod
        def fingerprint(_uri):
            return "source"

    class Guard:
        @staticmethod
        def check():
            checks.append("check")
            if len(checks) > 2:
                raise RuntimeError("lease renewal failed after publication")

    class Lease:
        def __enter__(self):
            return Guard()

        @staticmethod
        def __exit__(*_args):
            return None

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            return {"uri": kwargs["uri"], "name": kwargs["name"], "version": "v1"}

        @staticmethod
        def get_table(uri):
            return {"uri": uri, "name": "out", "version": "v1"}

    graph = Graph.model_validate({
        "id": "post-publish-lease", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": str(tmp_path / "source.parquet")}}},
            {"id": "write", "type": "write", "position": {"x": 100, "y": 0},
             "data": {"config": {"filename": "out.csv"}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    step = PlanStep(node_id="write", kind="write", label="write")
    plan = CompilePlan(target_node_id="write", steps=[step])
    runner = LocalRunner(lambda _uri: Adapter(), {}, Catalog(), str(tmp_path))
    runner.runs["run"] = RunStatus(
        run_id="run", status="queued", target_node_id="write",
        outputs=[_run_output(
            node_id="write", outcome="pending", publication_kind="catalog")],
        per_node=[PerNodeStatus(node_id="write", status="queued", label="write")])
    runner._cancel["run"] = _CancelToken()
    monkeypatch.setattr(handoff, "managed_read_lease", lambda *_args, **_kwargs: Lease())

    def commit(_node, _graph, _engine, status, _cached, _cancel, pre_publish=None):
        pre_publish(check_cancel=False)
        status.outputs = [_run_output(
            str(tmp_path / "out.csv"), rows=1, node_id="write", table="out")]
        return 1

    runner._commit_write = commit
    runner._execute("run", plan, graph, "write")
    assert runner.status("run").status == "done"
    assert checks == ["check", "check"]


@pytest.mark.parametrize("managed", [False, True], ids=["unmanaged", "managed"])
def test_local_cancel_race_respects_sink_commit_point(
        tmp_path, monkeypatch, managed):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from hub import compiler
    from hub.deps import Deps
    from hub.models import Graph
    from hub.plugins import catalog as catalog_mod
    from hub.plugins.adapters import DuckDBAdapter
    from hub.plugins.runner import LocalRunner

    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1]}), source)
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    workspace.mkdir()
    data_dir.mkdir()
    deps = Deps(str(workspace), str(data_dir))
    logical = (
        "s3://cancel-race/results/out.parquet" if managed
        else "custom://cancel-race/out.csv")
    attempt = "s3://cancel-race/results/out.attempt-parent"
    registered = []
    managed_published = []
    discarded = []

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            registered.append(kwargs)
            return {"uri": kwargs["uri"], "name": kwargs["name"], "version": "v1"}

        @staticmethod
        def get_table(uri):
            return {"uri": uri, "name": registered[-1]["name"], "version": "v1"}

    class SinkAdapter:
        @staticmethod
        def write(uri, _rel, _mode, cancelled=None):
            next(iter(runner._cancel.values())).set()
            return {"uri": uri, "rows": 1}

    if managed:
        SinkAdapter.__module__ = "hub.plugins.adapters"
    sink_adapter = SinkAdapter()
    source_adapter = DuckDBAdapter()

    class Storage:
        @staticmethod
        def output_uri(_name, _ext):
            return logical

    def resolve(uri):
        return sink_adapter if uri == logical or uri.startswith(attempt) else source_adapter

    runner = LocalRunner(
        resolve, deps.registry, Catalog(), str(workspace),
        node_builders=deps.node_builders, node_specs=deps.node_specs, storage=Storage())
    monkeypatch.setattr(handoff, "allocate_attempt", lambda **_kwargs: {"uri": attempt})
    monkeypatch.setattr(handoff, "write_manifest", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    monkeypatch.setattr(metadb, "abandon_committed_object_attempt", lambda _uri: False)
    monkeypatch.setattr(
        catalog_mod, "core_managed_publisher",
        lambda _catalog: lambda **kwargs: managed_published.append(kwargs) or {"uri": kwargs["uri"]})
    graph = Graph.model_validate({
        "id": "cancel-race", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "position": {"x": 100, "y": 0},
             "data": {"config": {"filename": "out.parquet" if managed else "out.csv"}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    plan = compiler.compile_plan(
        graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    started = runner.run(plan, graph, "write", "local")
    deadline = time.monotonic() + 5
    while runner.status(started.run_id).status not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = runner.status(started.run_id)
    if managed:
        assert final.status == "cancelled"
        assert managed_published == [] and registered == []
        assert discarded == [attempt]
    else:
        assert final.status == "done"
        output = _committed_output(final)
        assert output.uri == logical and output.table == "out" and output.rows == 1
        assert len(registered) == 1 and managed_published == [] and discarded == []


def test_local_runner_rejects_multiple_writes_before_starting_worker(tmp_path):
    from hub.models import CompilePlan, Graph, PlanStep
    from hub.plugins.runner import LocalRunner
    from hub.run_outputs import UnsupportedRunOutputs

    runner = LocalRunner(lambda _uri: object(), {}, object(), str(tmp_path))
    plan = CompilePlan(target_node_id="second", steps=[
        PlanStep(node_id="first", kind="write", label="first"),
        PlanStep(node_id="second", kind="write", label="second"),
    ])
    with pytest.raises(
            UnsupportedRunOutputs, match="do not yet support multiple write outputs"):
        runner.run(
            plan, Graph.model_validate({"id": "multi", "version": 1,
                                        "nodes": [], "edges": []}), "second", "local")
    assert runner.runs == {}


def test_local_unmanaged_sink_registers_all_join_sources_across_region_cut(tmp_path, monkeypatch):
    from hub import graph as graph_mod, sinks
    from hub.models import Graph, ResourceSpec, RunStatus
    from hub.planner import Region
    from hub.plugins.runner import LocalRunner, _CancelToken
    from hub.run_controller import RunController

    source_a = str(tmp_path / "a.parquet")
    source_b = str(tmp_path / "b.parquet")
    output = str(tmp_path / "joined.csv")
    registered = {}

    class Catalog:
        @staticmethod
        def register_output(**kwargs):
            registered.update(kwargs)
            return {"uri": kwargs["uri"], "name": kwargs["name"], "version": "v1"}

        @staticmethod
        def get_table(uri):
            return {"uri": uri, "name": registered["name"], "version": "v1"}

    class Storage:
        @staticmethod
        def output_uri(_name, _ext):
            return output

    class Adapter:
        @staticmethod
        def write(*_args, **_kwargs):
            raise AssertionError("commit_sink is isolated in this lineage test")

    class Engine:
        @staticmethod
        def relation(_node_id, _source_handle=None):
            return object()

    graph = Graph.model_validate({
        "id": "local-lineage", "version": 1,
        "nodes": [
            {"id": "a", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": source_a}}},
            {"id": "b", "type": "source", "position": {"x": 0, "y": 100},
             "data": {"config": {"uri": source_b}}},
            {"id": "join", "type": "join", "position": {"x": 100, "y": 50},
             "data": {"config": {}}},
            {"id": "transform", "type": "transform", "position": {"x": 200, "y": 50},
             "data": {"config": {}}},
            {"id": "write", "type": "write", "position": {"x": 300, "y": 50},
             "data": {"config": {"filename": "joined.csv"}}},
        ],
        "edges": [
            {"id": "a-join", "source": "a", "target": "join", "targetHandle": "a"},
            {"id": "b-join", "source": "b", "target": "join", "targetHandle": "b"},
            {"id": "join-transform", "source": "join", "target": "transform"},
            {"id": "transform-write", "source": "transform", "target": "write"},
        ],
    })
    expected = graph_mod.all_upstream_source_uris(graph, "write")
    region = Region(
        id="final", node_ids={"write"}, output_node="write", backend="default",
        worker=None, requires=ResourceSpec(),
        cut_inputs=[("transform", None, "write", None)],
    )
    graph = RunController._subgraph(
        None, graph, region, {"transform": str(tmp_path / "region-ref.parquet")})
    monkeypatch.setattr(
        sinks, "commit_sink",
        lambda *_args, **_kwargs: sinks.SinkCommit(name="joined", uri=output, rows=2))
    runner = LocalRunner(
        lambda _uri: Adapter(), {}, Catalog(), str(tmp_path), storage=Storage())
    runner._commit_write(
        next(node for node in graph.nodes if node.id == "write"), graph, Engine(),
        RunStatus(
            run_id="run", status="running", target_node_id="write", per_node=[],
            outputs=[_run_output(
                node_id="write", outcome="pending", publication_kind="catalog")]),
        None,
        _CancelToken(), pre_publish=lambda **_kwargs: None)
    assert set(expected) == {source_a, source_b}
    assert graph_mod.execution_source_uris(graph, "write") == [str(tmp_path / "region-ref.parquet")]
    assert registered["parents"] == metadb.catalog_lineage_parent_tokens(expected)


def test_local_sink_catalog_detail_is_logged_but_status_is_generic(
        tmp_path, caplog):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from hub import compiler
    from hub.deps import Deps
    from hub.models import Graph
    from hub.plugins.runner import LocalRunner

    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    workspace.mkdir()
    data_dir.mkdir()
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1]}), source)
    deps = Deps(str(workspace), str(data_dir))

    class FailingCatalog:
        @staticmethod
        def register_output(**_kwargs):
            raise RuntimeError("SECRET_LOCAL_CATALOG_DETAIL")

        @staticmethod
        def get_table(_uri):
            raise AssertionError("read-back must not run after registration fails")

    graph = Graph.model_validate({
        "id": "local-secret", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "position": {"x": 100, "y": 0},
             "data": {"config": {"filename": "out.csv"}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    runner = LocalRunner(
        deps.resolve_adapter, deps.registry, FailingCatalog(), str(workspace),
        node_builders=deps.node_builders, node_specs=deps.node_specs,
        storage=deps.storage)
    plan = compiler.compile_plan(
        graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    caplog.set_level("ERROR", logger="hub")
    started = runner.run(plan, graph, "write", "local")
    deadline = time.monotonic() + 5
    while runner.status(started.run_id).status not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = runner.status(started.run_id)
    assert final.status == "failed"
    assert "sink publication failed" in (final.error or "")
    assert "SECRET_LOCAL_CATALOG_DETAIL" not in (final.error or "")
    assert "SECRET_LOCAL_CATALOG_DETAIL" in caplog.text


def test_strict_unmanaged_publication_requires_readable_output_and_subclass_persistence(
        tmp_path, monkeypatch):
    from hub.models import ColumnSchema
    from hub.plugins.catalog import (
        InMemoryCatalog,
        publish_unmanaged_output_attested,
    )

    missing_uri = str(tmp_path / "missing.parquet")

    class MissingAdapter:
        @staticmethod
        def schema(_uri):
            raise FileNotFoundError("missing output")

        @staticmethod
        def count(_uri):
            raise FileNotFoundError("missing output")

    catalog = InMemoryCatalog(str(tmp_path / "empty"), lambda _uri: MissingAdapter())
    with pytest.raises(RuntimeError, match="schema/count probe failed"):
        catalog.publish_output_strict("missing", missing_uri)
    assert metadb.catalog_get(missing_uri) is None

    class Probe:
        @staticmethod
        def schema(_uri):
            return [ColumnSchema(name="value", type="BIGINT")]

        @staticmethod
        def count(_uri):
            return 1

        @staticmethod
        def fingerprint(_uri):
            return "strict-subclass"

    class ReadOnlyCatalog(InMemoryCatalog):
        def register_output(self, **_kwargs):
            raise AssertionError("subclass must retain the inherited strict authority")

        def publish_output_strict(self, **_kwargs):
            return {"uri": strict_uri, "name": "strict", "version": "forged"}

    strict_uri = str(tmp_path / "strict.parquet")
    subclass = ReadOnlyCatalog(str(tmp_path / "empty-subclass"), lambda _uri: Probe())
    monkeypatch.setattr(
        metadb, "catalog_upsert_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("persist unavailable")))
    with pytest.raises(RuntimeError, match="persist unavailable"):
        publish_unmanaged_output_attested(
            subclass, name="strict", uri=strict_uri, parents=[], pipeline="canvas")
    assert metadb.catalog_get(strict_uri) is None


def test_post_popen_setup_failure_retains_writer_when_reap_is_unproved(
        tmp_path, monkeypatch):
    from hub.models import CompilePlan, Graph
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    attempt = "s3://setup-retained/results/out.attempt-parent"
    discarded = []

    class Storage:
        @staticmethod
        def output_uri(_name, _ext):
            return "s3://setup-retained/results/out.parquet"

    class Adapter:
        @staticmethod
        def fingerprint(_uri):
            return "source"

    class Process:
        returncode = None

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def wait(timeout=None):
            raise OSError("wait proof unavailable")

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread setup failed")

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(subprocess_runner.threading, "Thread", BrokenThread)
    monkeypatch.setattr(handoff, "allocate_attempt", lambda **kwargs: {
        "uri": attempt, "logical_uri": kwargs["logical_uri"],
        "attempt_id": "retained", "generation": 1,
        "storage_namespace": "installation",
    })
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), storage=Storage(),
        resolve_adapter=lambda _uri: Adapter())
    runner.on_status = lambda _graph, _status: None
    graph = Graph.model_validate({
        "id": "setup-retained", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"config": {"uri": str(tmp_path / "source.parquet")}}}],
        "edges": [],
    })
    with pytest.raises(RuntimeError, match="thread setup failed"):
        runner.run(
            CompilePlan(target_node_id="source", steps=[]), graph, "source", "local",
            run_id="setup-retained")

    assert discarded == []
    assert "setup-retained" in runner._procs
    assert "setup-retained" in runner._object_results
    assert runner.status("setup-retained").status == "running"
    assert runner.status("setup-retained").stalled is True
    cancel_file = runner._cancel_files["setup-retained"]
    assert (job_dir := __import__("pathlib").Path(cancel_file).parent).is_dir()
    runner._procs.clear()
    runner._cancel_files.clear()
    runner._object_results.clear()
    runner.runs.clear()
    __import__("shutil").rmtree(job_dir, ignore_errors=True)


def test_metadata_cleanup_outage_never_discards_uncertain_object(
        monkeypatch, caplog):
    from hub.plugins import runner as runner_mod
    import hub.subprocess_runner as subprocess_runner

    attempt = "s3://cleanup-outage/results/out.attempt-owned"
    discarded = []
    monkeypatch.setattr(
        metadb, "abandon_committed_object_attempt",
        lambda _uri: (_ for _ in ()).throw(RuntimeError("metadata unavailable")))
    monkeypatch.setattr(handoff, "discard_attempt", discarded.append)
    caplog.set_level("ERROR", logger="hub")

    runner_mod._safe_abandon_attempt(attempt)
    subprocess_runner._safe_abandon_attempt(attempt, context="test cleanup")
    assert discarded == []
    assert caplog.text.count("metadata unavailable") >= 2


def test_supervisor_reap_failure_retains_attempt_jobdir_and_process_tracking(
        tmp_path, monkeypatch):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    job_dir = tmp_path / "unreaped-supervisor"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    attempt = "s3://supervisor-retained/results/out.attempt-parent"
    cleanup = []

    class Process:
        returncode = None

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def wait(timeout=None):
            raise OSError("reap unavailable")

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    runner.runs["run"] = RunStatus(run_id="run", status="running", per_node=[])
    runner._procs["run"] = Process()
    runner._cancel_files["run"] = str(job_dir / "cancel.requested")
    runner._object_results["run"] = _object_result_owner(attempt)
    runner._sink_contracts["run"] = {"write": {"name": "out"}}
    completed = []
    retries = []
    runner.on_complete = lambda *_args: completed.append(True)
    monkeypatch.setattr(
        runner, "_watch_inner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watcher bug")))
    monkeypatch.setattr(
        runner, "_schedule_watch_retry",
        lambda *_args, **_kwargs: retries.append("retry"))
    monkeypatch.setattr(
        subprocess_runner, "_safe_abandon_attempt",
        lambda *_args, **_kwargs: cleanup.append("cleanup"))
    runner._watch(
        "run", runner._procs["run"], str(status_file), str(job_dir),
        Graph.model_validate({"id": "supervisor", "version": 1,
                              "nodes": [], "edges": []}), None)

    assert runner.status("run").status == "running"
    assert runner.status("run").stalled is True
    assert "retrying writer reconciliation" in (runner.status("run").error or "")
    assert completed == [] and retries == ["retry"]
    assert cleanup == []
    assert runner._procs.get("run") is not None
    assert runner._object_results["run"]["results"][0]["uri"] == attempt
    assert "run" in runner._sink_contracts
    assert job_dir.is_dir()
    runner._procs.clear()
    runner._cancel_files.clear()
    runner._object_results.clear()
    runner._sink_contracts.clear()
    runner.runs.clear()
    __import__("shutil").rmtree(job_dir, ignore_errors=True)


def test_managed_catalog_probe_holds_read_lease_against_gc(
        tmp_path, monkeypatch):
    from hub.models import ColumnSchema
    from hub.plugins.catalog import InMemoryCatalog

    handle = _handle(
        "sink", logical=f"s3://catalog-probe/{uuid.uuid4().hex}/out.parquet")
    _commit(handle)
    entered = threading.Event()
    release = threading.Event()
    result = []
    errors = []

    class BlockingProbe:
        @staticmethod
        def schema(_uri):
            entered.set()
            assert release.wait(timeout=5)
            return [ColumnSchema(name="value", type="BIGINT")]

        @staticmethod
        def count(_uri):
            return 1

        @staticmethod
        def fingerprint(_uri):
            return "blocked-probe"

    catalog = InMemoryCatalog(str(tmp_path / "catalog"), lambda _uri: BlockingProbe())
    monkeypatch.setattr(handoff, "prepare_attempt_commit", lambda _uri: None)

    def publish():
        try:
            result.append(catalog.publish_managed_output("out", handle["uri"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    assert entered.wait(timeout=5)
    with metadb.session() as session:
        publish_lease = session.get(
            metadb.ObjectAttemptLease, handle["publish_lease_id"])
        publish_lease.expires_at = metadb._db_now(session) - datetime.timedelta(seconds=1)
        read_leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == handle["uri"],
            metadb.ObjectAttemptLease.lease_type == "read")))
        assert len(read_leases) == 1

    assert handoff.reap_attempts(
        retention_seconds=0, delete_grace_seconds=0) == {
            "observed": [], "deleted": [], "quarantined": [],
        }
    assert _state(handle["uri"]) == "committed"
    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive() and errors == []
    assert result[0]["uri"] == handle["uri"]
    assert _state(handle["uri"]) == "published"
    metadb.catalog_delete_entry(handle["uri"])
    metadb.quarantine_object_attempt(handle["uri"], "test cleanup")


def test_pool_worker_slot_stays_fenced_until_child_stop_is_proven(
        tmp_path, monkeypatch):
    from hub.pool_runner import PoolRunner
    from hub.subprocess_runner import SubprocessRunner

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

    runner = PoolRunner(
        str(tmp_path), str(tmp_path), [{"name": "worker", "cpu": 1}])
    proc = Process()
    runner._assigned["run"] = "worker"
    runner._procs["run"] = proc
    monkeypatch.setattr(SubprocessRunner, "_watch", lambda *_args, **_kwargs: None)

    runner._watch("run")
    assert runner._assigned == {"run": "worker"}
    assert runner.workers()[0].state == "busy"

    proc.stopped = True
    runner._watch("run")
    assert runner._assigned == {}
    assert runner.workers()[0].state == "idle"
