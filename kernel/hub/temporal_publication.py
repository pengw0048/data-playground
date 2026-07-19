"""Private, synchronous transaction boundary for one temporal-resample candidate."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable

from hub import metadb
from hub.local_writes import write_managed_local_file
from hub.models import WriteIntent, WriteReceipt
from hub.temporal_resample import ResampleCandidate


@dataclass(frozen=True)
class TemporalPublicationResult:
    """The complete durable result; a generic write receipt alone is not enough for replay."""

    receipt: WriteReceipt
    evidence: dict
    child: dict


def register_parent(*, owner_id: str, manifest: object) -> None:
    """Register an externally-read exact parent before deriving its first temporal child."""
    metadb.catalog_register_temporal_compound_parent(owner_id, manifest)


def publish_candidate(
        *, storage, catalog, owner_id: str, parent_manifest: object,
        candidate: ResampleCandidate, output_member_id: str, intent: WriteIntent,
        write_artifact: Callable[[str], object], before_publish: Callable[[], None] | None = None,
) -> TemporalPublicationResult:
    """Stage and atomically publish one frozen candidate and its child compound revision."""
    context = metadb.TemporalResamplePublicationContext(
        owner_id=owner_id, parent_manifest=parent_manifest, candidate=candidate,
        output_member_id=output_member_id, output_revision_id=uuid.uuid4().hex)
    receipt = write_managed_local_file(
        storage=storage, catalog=catalog, intent=intent, write_artifact=write_artifact,
        before_publish=before_publish, temporal_publication=context)
    recovered = metadb.catalog_temporal_resample_publication_receipt(
        intent.model_dump(by_alias=True, mode="json"), temporal_publication=context)
    if recovered is None:
        raise RuntimeError("temporal publication has no durable semantic receipt")
    result = TemporalPublicationResult(
        receipt=WriteReceipt.model_validate(recovered["receipt"]),
        evidence=dict(recovered["evidence"]), child=dict(recovered["child"]),
    )
    if result.receipt != receipt:
        raise RuntimeError("temporal publication receipt changed after commit")
    return result
