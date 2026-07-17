"""Typed create/replace coordination for the default managed local-file consumer."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable

from hub import metadb
from hub.models import WriteIntent, WriteReceipt


def _finish_candidate(storage, artifact_uri: str, run_id: str, receipt: WriteReceipt) -> None:
    if receipt.publication.artifact_uri == artifact_uri:
        if not storage.release_result(artifact_uri, run_id):
            raise RuntimeError("managed local write receipt is missing its durable artifact owner")
    else:
        storage.abort_result(artifact_uri, run_id)


def write_managed_local_file(
        *, storage, catalog, intent: WriteIntent,
        write_artifact: Callable[[str], object],
        before_publish: Callable[[], None] | None = None) -> WriteReceipt:
    """Execute one frozen local write with abortable staging and receipt-based recovery.

    Admission touches no artifact. Once publication starts, a successful receipt read distinguishes a
    committed response-loss case from a rolled-back transaction; if metadata itself is unavailable,
    the writer fence is deliberately retained instead of guessing that an exact artifact is abortable.
    """
    frozen = WriteIntent.model_validate(intent)
    frozen_doc = frozen.model_dump(by_alias=True, mode="json")
    prior = metadb.catalog_admit_managed_local_write(frozen_doc)
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
            frozen, artifact_uri, total_bytes=total_bytes)
    except Exception:
        if publication_started:
            try:
                recovered = metadb.catalog_managed_local_write_receipt(frozen_doc)
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
