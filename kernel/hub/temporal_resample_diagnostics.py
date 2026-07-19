"""Pure diagnostic contract for durable temporal-resample tasks."""

from __future__ import annotations

from enum import StrEnum


class TemporalResampleDiagnosticCode(StrEnum):
    """Finite public diagnostics for temporal-resample admission and execution."""

    PERMISSION_DENIED = "temporal_permission_denied"
    PROVIDER_OFFLINE = "temporal_provider_offline"
    REVISION_UNAVAILABLE = "temporal_revision_unavailable"
    INPUT_PARTIAL = "temporal_input_partial"
    INPUT_CORRUPT = "temporal_input_corrupt"
    INPUT_TRUNCATED = "temporal_input_truncated"
    INPUT_DUPLICATE = "temporal_input_duplicate"
    INPUT_SAMPLED = "temporal_input_sampled"
    MISSING_FIELD = "temporal_missing_field"
    SPEC_INVALID = "temporal_spec_invalid"
    STALE_PARENT = "temporal_stale_parent"
    PUBLICATION_KEY_CONFLICT = "temporal_publication_key_conflict"
    EXACT_REVISION_LOST = "temporal_exact_revision_lost"
    PUBLICATION_FAILED = "temporal_publication_failed"
    ATTEMPTS_EXHAUSTED = "temporal_attempts_exhausted"


def temporal_failure_retryable(code: object) -> bool:
    """Return whether the same frozen temporal task can recover on another attempt."""
    try:
        diagnostic = TemporalResampleDiagnosticCode(code)
    except (TypeError, ValueError):
        return False
    return diagnostic in {
        TemporalResampleDiagnosticCode.PROVIDER_OFFLINE,
        TemporalResampleDiagnosticCode.PUBLICATION_FAILED,
    }
