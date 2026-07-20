"""Typed coordination for the default managed local-file and local Lance consumers."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager, suppress
from collections.abc import Callable

from hub import metadb
from hub.models import (
    DatasetRevision,
    WriteIntent,
    WritePublicationIdentity,
    WriteReceipt,
)


def _finish_candidate(storage, artifact_uri: str, run_id: str, receipt: WriteReceipt) -> None:
    if receipt.publication.artifact_uri == artifact_uri:
        if not storage.release_result(artifact_uri, run_id):
            raise RuntimeError("managed local write receipt is missing its durable artifact owner")
    else:
        storage.abort_result(artifact_uri, run_id)


def write_managed_local_file(
        *, storage, catalog, intent: WriteIntent,
        write_artifact: Callable[[str], object],
        before_publish: Callable[[], None] | None = None,
        merge_publication: metadb.MergeColumnsPublicationContext | None = None) -> WriteReceipt:
    """Execute one frozen local write with abortable staging and receipt-based recovery.

    Admission touches no artifact. Once publication starts, a successful receipt read distinguishes a
    committed response-loss case from a rolled-back transaction; if metadata itself is unavailable,
    the writer fence is deliberately retained instead of guessing that an exact artifact is abortable.
    """
    frozen = WriteIntent.model_validate(intent)
    frozen_doc = frozen.model_dump(by_alias=True, mode="json")
    prior = metadb.catalog_admit_managed_local_write(
        frozen_doc, merge_publication=merge_publication)
    if prior is not None:
        return WriteReceipt.model_validate(prior)

    run_id = "managed-write:" + hashlib.sha256(
        frozen.idempotency_key.encode("utf-8")).hexdigest()[:32]
    artifact_uri = storage.begin_result(
        f"managed-write:{frozen.destination.logical_uri}", run_id)
    publication_started = False
    try:
        write_artifact(artifact_uri)
        storage.commit_result(artifact_uri, run_id)
        if before_publish is not None:
            before_publish()
        total_bytes = os.stat(artifact_uri, follow_symlinks=True).st_size
        publication_started = True
        receipt = catalog.publish_managed_local_write(
            frozen, artifact_uri, total_bytes=total_bytes,
            merge_publication=merge_publication)
        if merge_publication is not None:
            semantic = metadb.catalog_managed_local_write_receipt(
                frozen_doc, merge_publication=merge_publication)
            if semantic is None:
                raise RuntimeError("managed publication has no durable semantic receipt")
            receipt = WriteReceipt.model_validate(semantic)
    except Exception:
        if publication_started:
            try:
                recovered = metadb.catalog_managed_local_write_receipt(
                    frozen_doc, merge_publication=merge_publication)
            except Exception:
                # Commit outcome is unknown. Retain the exact writer fence until a retry/restart can
                # discover either the durable receipt or an unreferenced ready artifact.
                raise
            if recovered is not None:
                receipt = WriteReceipt.model_validate(recovered)
                _finish_candidate(storage, artifact_uri, run_id, receipt)
                return receipt
        storage.abort_result(artifact_uri, run_id)
        raise
    _finish_candidate(storage, artifact_uri, run_id, receipt)
    return receipt


_LANCE_INTENT_PROPERTY = "data_playground_write_intent_sha256"
_LANCE_KEY_PROPERTY = "data_playground_write_key_sha256"


def _lance_intent_digest(intent: WriteIntent) -> tuple[str, str]:
    payload = intent.model_dump(by_alias=True, mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        hashlib.sha256(intent.idempotency_key.encode("utf-8")).hexdigest(),
    )


def _lance_append_receipt(intent: WriteIntent, adapter, backend_version: str) -> WriteReceipt | None:
    """Reconcile the only CAS-eligible native version from durable Lance transaction evidence."""
    expected = intent.expected_head
    if expected is None:  # guarded by WriteIntent; keeps this helper independently fail-closed
        raise ValueError("managed local Lance append requires an expected head")
    expected_version = int(adapter._revision_id(expected.revision_id))
    committed_version = expected_version + 1
    dataset = adapter._dataset(intent.destination.logical_uri)
    if int(dataset.version) < committed_version:
        return None
    transaction = dataset.read_transaction(committed_version)
    import lance

    intent_digest, key_digest = _lance_intent_digest(intent)
    properties = transaction.transaction_properties or {}
    if (int(transaction.read_version) != expected_version
            or not isinstance(transaction.operation, lance.LanceOperation.Append)
            or properties.get(_LANCE_INTENT_PROPERTY) != intent_digest
            or properties.get(_LANCE_KEY_PROPERTY) != key_digest):
        return None
    detail = adapter.revision_detail(
        intent.destination.logical_uri, str(committed_version), preview_limit=1)
    total_bytes = detail.get("total_bytes")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        raise RuntimeError("managed local Lance revision has no bounded byte evidence")
    committed_at = detail.get("committed_at")
    if committed_at is None:
        raise RuntimeError("managed local Lance revision has no commit timestamp")
    dataset_id = intent.destination.dataset_id
    if dataset_id is None:  # guarded by WriteIntent
        raise ValueError("managed local Lance append requires a destination dataset")
    return WriteReceipt(
        dataset_id=dataset_id,
        revision_id=str(committed_version),
        parent_head=expected,
        head=DatasetRevision(
            dataset_id=dataset_id,
            revision_id=str(committed_version),
            committed_at=committed_at,
            retention_owner="provider",
        ),
        rows=int(detail["row_count"]),
        bytes=total_bytes,
        schema=detail["columns"],
        partitions=intent.partitions,
        publication=WritePublicationIdentity(
            provider="managed-local-lance",
            logical_uri=intent.destination.logical_uri,
            artifact_uri=intent.destination.logical_uri,
            publish_sequence=committed_version,
            idempotency_key=intent.idempotency_key,
            catalog_version=f"lance-v{committed_version}",
            backend_version=backend_version,
        ),
        provenance=intent.provenance,
    )


def _arrow_columns(data) -> list[tuple[str, str]]:
    import pyarrow as pa

    schema = getattr(data, "schema", None)
    if not isinstance(schema, pa.Schema):
        raise ValueError("managed local Lance append data must expose an Arrow schema")
    from hub import db
    from hub.plugins.adapters import relation_columns

    empty = pa.Table.from_batches([], schema=schema)
    with db.base_guard():
        columns = relation_columns(db.conn().from_arrow(empty))
    return [(column.name, column.type) for column in columns]


def _cleanup_lance_fragment(
        dataset_path: str, fragment, transaction_file: str | None = None) -> None:
    """Remove only the uncommitted data files named by one locally-created append fragment."""
    data_root = os.path.realpath(os.path.join(dataset_path, "data"))
    for data_file in getattr(fragment, "files", ()):
        candidate = os.path.realpath(os.path.join(data_root, str(data_file.path)))
        if os.path.commonpath((candidate, data_root)) != data_root:
            raise RuntimeError("Lance append fragment escaped its dataset data directory")
        with suppress(FileNotFoundError):
            os.remove(candidate)
    if transaction_file is not None:
        with suppress(FileNotFoundError):
            os.remove(transaction_file)


def write_managed_local_lance_append(
        *, intent: WriteIntent, data,
        before_publish: Callable[[], None] | None = None) -> WriteReceipt:
    """Append one Arrow payload through Lance's exact-head native commit contract.

    Metadata admission happens before provider I/O. The append fragment is deliberately uncommitted
    staging; Lance's commit callback rejects any candidate version other than ``expectedHead + 1``.
    Durable transaction properties let a retry recover the one exact version after response loss.
    """
    frozen = WriteIntent.model_validate(intent)
    frozen_doc = frozen.model_dump(by_alias=True, mode="json")
    prior = metadb.catalog_admit_managed_local_lance_write(frozen_doc)
    if prior is not None:
        return WriteReceipt.model_validate(prior)

    from hub.paths import checked_local_path
    from hub.plugins.adapters import LanceAdapter

    dataset_path = checked_local_path(frozen.destination.logical_uri)
    if dataset_path is None or not os.path.isdir(dataset_path):
        raise metadb.ManagedLocalWriteConflict("append destination does not exist")
    adapter = LanceAdapter()
    expected = frozen.expected_head
    if expected is None:  # guarded by WriteIntent
        raise ValueError("managed local Lance append requires an expected head")
    expected_version = int(adapter._revision_id(expected.revision_id))
    import lance

    backend_version = str(getattr(lance, "__version__", "unknown"))

    def persist_recovered(receipt: WriteReceipt) -> WriteReceipt:
        saved = metadb.catalog_publish_managed_local_lance_write(
            frozen_doc, lambda: receipt.model_dump(by_alias=True, mode="json"))
        return WriteReceipt.model_validate(saved)

    # A process can die after Lance commits expected+1 but before the SQL receipt is durable. Every retry
    # must reconcile the exact native transaction evidence before treating the advanced head as stale.
    recovered = _lance_append_receipt(frozen, adapter, backend_version)
    if recovered is not None:
        return persist_recovered(recovered)
    observed = adapter.resolve_revision(frozen.destination.logical_uri)
    if observed["revision_id"] != str(expected_version):
        raise metadb.ManagedLocalWriteConflict("append expected head is stale")
    destination_detail = adapter.revision_detail(
        frozen.destination.logical_uri, str(expected_version), preview_limit=1)
    expected_schema = [(column.name, column.type) for column in frozen.expected_schema]
    destination_schema = [
        (column.name, column.type) for column in destination_detail["columns"]
    ]
    if expected_schema != destination_schema:
        raise ValueError("managed local Lance destination schema does not match its intent")
    if _arrow_columns(data) != expected_schema:
        raise ValueError("managed local Lance append data schema does not match its intent")
    destination_arrow_schema = adapter._dataset(
        frozen.destination.logical_uri, version=expected_version).schema
    if not data.schema.equals(destination_arrow_schema, check_metadata=False):
        raise ValueError("managed local Lance append data schema is not provider-compatible")

    try:
        import lance
        from lance.dataset import Transaction
        from lance.fragment import LanceFragment
    except ModuleNotFoundError as exc:  # pragma: no cover - adapter admission already imports Lance
        raise ModuleNotFoundError(
            "Lance support is not installed — run: uv pip install -e 'kernel[lance]'") from exc

    fragment = None
    transaction_file = None

    def publish() -> dict[str, object]:
        nonlocal fragment, transaction_file
        current = adapter.resolve_revision(frozen.destination.logical_uri)
        if current["revision_id"] != str(expected_version):
            raise metadb.ManagedLocalWriteConflict("append expected head is stale")
        fragment = LanceFragment.create(dataset_path, data, mode="append")
        if before_publish is not None:
            before_publish()
        boundary_stale = threading.Event()

        @contextmanager
        def expected_head_lock(candidate_version: int):
            if int(candidate_version) != expected_version + 1:
                boundary_stale.set()
                raise RuntimeError("managed local Lance append expected head is stale")
            yield

        intent_digest, key_digest = _lance_intent_digest(frozen)
        try:
            transaction = Transaction(
                expected_version,
                lance.LanceOperation.Append([fragment]),
                transaction_properties={
                    _LANCE_INTENT_PROPERTY: intent_digest,
                    _LANCE_KEY_PROPERTY: key_digest,
                    "__lance_commit_message": "Data Playground managed local append",
                },
            )
            transaction_file = os.path.join(
                dataset_path, "_transactions", f"{expected_version}-{transaction.uuid}.txn")
            committed = lance.LanceDataset.commit(
                dataset_path,
                transaction,
                commit_lock=expected_head_lock,
                max_retries=0,
            )
        except Exception as exc:
            recovered = _lance_append_receipt(frozen, adapter, backend_version)
            if recovered is not None:
                return recovered.model_dump(by_alias=True, mode="json")
            if boundary_stale.is_set() or int(adapter._dataset(dataset_path).version) != expected_version:
                raise metadb.ManagedLocalWriteConflict(
                    "append expected head is stale at publication") from exc
            raise
        if int(committed.version) != expected_version + 1:
            raise RuntimeError("managed local Lance append committed an unexpected version")
        recovered = _lance_append_receipt(frozen, adapter, backend_version)
        if recovered is None:
            raise RuntimeError("managed local Lance append lost its exact transaction evidence")
        return recovered.model_dump(by_alias=True, mode="json")

    try:
        published = metadb.catalog_publish_managed_local_lance_write(frozen_doc, publish)
        return WriteReceipt.model_validate(published)
    except Exception:
        prior = metadb.catalog_managed_local_lance_write_receipt(frozen_doc)
        if prior is not None:
            return WriteReceipt.model_validate(prior)
        try:
            recovered = _lance_append_receipt(frozen, adapter, backend_version)
        except Exception:
            # Provider outcome is ambiguous. Retain the fragment instead of deleting data that may be
            # referenced by a committed version whose evidence is temporarily unavailable.
            raise
        if recovered is not None:
            return persist_recovered(recovered)
        if fragment is not None:
            _cleanup_lance_fragment(dataset_path, fragment, transaction_file)
        raise
