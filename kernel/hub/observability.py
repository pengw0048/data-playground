"""Stable observability contracts — metrics, audit events, and request/trace IDs.

Core defines typed, versioned shapes and a pluggable sink seam. It ships no OpenTelemetry,
Prometheus, or vendor exporter (those are follow-up plugins). A sinking failure or timeout never
changes request results, run results, or stored data.

See ``docs/OBSERVABILITY.md`` for the authoritative catalog of metric names and audit actions.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
REQUEST_ID_HEADER = "X-Request-Id"
# Bound fan-out so a slow/stuck sink cannot stall runs or HTTP responses.
_SINK_TIMEOUT_S = 2.0
_SINK_WORKERS = 4

_log = logging.getLogger("hub.observability")

_request_id_var: ContextVar[str | None] = ContextVar("dp_request_id", default=None)
_metric_sinks: list = []
_audit_sinks: list = []
_sink_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Low-cardinality label keys only. Raw IDs, URIs, user input, and error strings are forbidden.
ALLOWED_METRIC_LABEL_KEYS = frozenset({
    "status",          # run/job status bucket
    "outcome",         # success | failure | denied | error
    "placement",       # local | subprocess | distributed | …
    "backend",         # local | subprocess | kernel | …
    "method",          # HTTP method
    "route_class",     # /api/run | /api/auth | /api/catalog | …
    "action",          # audit/action family bucket for counters
    "kind",            # publication | gc | health | …
    "error_class",     # auth | storage | timeout | cancelled | validation | internal | none
    "probe",           # livez | readyz
    "ready",           # true | false
})

_FORBIDDEN_IN_EVENTS = re.compile(
    r"(?i)(password|secret|api[_-]?key|token|credential|authorization)",
)


class MetricType(str, Enum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


class MetricUnit(str, Enum):
    UNIT = "1"
    MILLISECONDS = "ms"
    BYTES = "By"
    SECONDS = "s"


class MetricName(str, Enum):
    """Canonical metric names (``dp.*``). Keep in sync with ``docs/OBSERVABILITY.md``."""

    HTTP_REQUESTS = "dp.http.requests"
    HTTP_DURATION_MS = "dp.http.duration_ms"
    RUN_STATE = "dp.run.state_transitions"
    RUN_DURATION_MS = "dp.run.duration_ms"
    RUN_QUEUE_DELAY_MS = "dp.run.queue_delay_ms"
    RUN_RETRIES = "dp.run.retries"
    RUN_CANCEL_LATENCY_MS = "dp.run.cancel_latency_ms"
    RUN_FINISHED = "dp.run.finished"
    PUBLICATION = "dp.publication.events"
    STORAGE_GC = "dp.storage.gc"
    KERNEL_HEALTH = "dp.kernel.health"
    PROVIDER_ERRORS = "dp.provider.errors"


class AuditAction(str, Enum):
    """Canonical audit actions. Keep in sync with ``docs/OBSERVABILITY.md``."""

    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_PASSWORD_CHANGE = "auth.password_change"
    ADMIN_SETTINGS_CHANGE = "admin.settings_change"
    SHARING_CHANGE = "sharing.change"
    DATASET_ACCESS = "dataset.access"
    DATASET_MUTATION = "dataset.mutation"
    AGENT_EGRESS = "agent.egress"
    JOB_SUBMIT = "job.submit"
    JOB_CANCEL = "job.cancel"
    SECRET_REF_CHANGE = "secret_ref.change"
    POLICY_DENIAL = "policy.denial"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class MetricEvent(BaseModel):
    """A single low-cardinality metric observation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    name: MetricName
    type: MetricType
    unit: MetricUnit = MetricUnit.UNIT
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None

    def model_post_init(self, __context: Any) -> None:
        validate_metric_labels(self.labels)


class AuditEvent(BaseModel):
    """A structured security/ops audit event. Never carries secrets or raw row values."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    action: AuditAction
    outcome: AuditOutcome
    principal_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    # Small redacted attributes only — never passwords, tokens, URIs with credentials, or row payloads.
    attrs: dict[str, str] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_post_init(self, __context: Any) -> None:
        validate_audit_attrs(self.attrs)


MetricSink = Callable[[MetricEvent], Any]
AuditSink = Callable[[AuditEvent], Any]


def mint_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_request_id(request_id: str | None) -> Token:
    return _request_id_var.set(request_id)


def reset_request_id(token: Token) -> None:
    _request_id_var.reset(token)


def normalize_request_id(raw: str | None) -> str:
    """Accept a client-supplied request id when it is a short safe token; otherwise mint one."""
    if not raw:
        return mint_request_id()
    value = raw.strip()
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9._+:-]+", value):
        return mint_request_id()
    return value


def route_class(path: str) -> str:
    """Map a URL path to a low-cardinality route class (no path IDs)."""
    if not path:
        return "other"
    if path.startswith("/api/run"):
        return "/api/run"
    if path.startswith("/api/auth"):
        return "/api/auth"
    if path.startswith("/api/catalog"):
        return "/api/catalog"
    if path.startswith("/api/canvas"):
        return "/api/canvas"
    if path.startswith("/api/settings"):
        return "/api/settings"
    if path.startswith("/api/data"):
        return "/api/data"
    if path in ("/api/livez", "/api/health"):
        return "/api/livez"
    if path == "/api/readyz":
        return "/api/readyz"
    if path == "/api/version":
        return "/api/version"
    if path.startswith("/api/"):
        return "/api/other"
    if path.startswith("/ws/"):
        return "/ws"
    if path == "/mcp":
        return "/mcp"
    return "other"


def error_class(exc: BaseException | str | None) -> str:
    if exc is None:
        return "none"
    text = str(exc).lower()
    if "cancel" in text:
        return "cancelled"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "auth" in text or "forbidden" in text or "unauthorized" in text or "password" in text:
        return "auth"
    if "storage" in text or "s3" in text or "object" in text or "uri" in text:
        return "storage"
    if "valid" in text or "schema" in text or "cycle" in text:
        return "validation"
    return "internal"


def validate_metric_labels(labels: dict[str, str]) -> None:
    for key, value in labels.items():
        if key not in ALLOWED_METRIC_LABEL_KEYS:
            raise ValueError(f"metric label key {key!r} is not in the low-cardinality allow-list")
        if not isinstance(value, str):
            raise ValueError(f"metric label {key!r} must be a string")
        if len(value) > 64:
            raise ValueError(f"metric label {key!r} exceeds cardinality-safe length")
        if _looks_like_raw_id(value):
            raise ValueError(f"metric label {key!r} must not carry a raw id/uri")


def validate_audit_attrs(attrs: dict[str, str]) -> None:
    for key, value in attrs.items():
        if not isinstance(value, str):
            raise ValueError(f"audit attr {key!r} must be a string")
        if len(value) > 256:
            raise ValueError(f"audit attr {key!r} too long")
        if _FORBIDDEN_IN_EVENTS.search(key) or _FORBIDDEN_IN_EVENTS.search(value):
            raise ValueError(f"audit attr {key!r} appears to carry a secret")


def _looks_like_raw_id(value: str) -> bool:
    if "://" in value or "/" in value or "\\" in value:
        return True
    if value.startswith(("run_", "req_", "att_", "canvas_", "tbl_")):
        return True
    # Long hex-ish tokens are unbounded cardinality.
    if len(value) >= 20 and re.fullmatch(r"[0-9a-fA-F_-]+", value):
        return True
    return False


def assert_event_redacted(obj: Any, *, forbidden: Iterable[str]) -> None:
    """Raise ``AssertionError`` if any forbidden secret/row sample appears in a serialized event."""
    blob = obj if isinstance(obj, str) else _stable_dump(obj)
    for item in forbidden:
        if item and item in blob:
            raise AssertionError(f"forbidden value leaked into observability event: {item!r}")


def _stable_dump(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return repr(obj)


def add_metric_sink(sink: MetricSink) -> None:
    if callable(sink):
        with _sink_lock:
            _metric_sinks.append(sink)


def add_audit_sink(sink: AuditSink) -> None:
    if callable(sink):
        with _sink_lock:
            _audit_sinks.append(sink)


def clear_sinks() -> None:
    """Test helper — drop all registered metric/audit sinks."""
    with _sink_lock:
        _metric_sinks.clear()
        _audit_sinks.clear()


def list_metric_sinks() -> list[MetricSink]:
    with _sink_lock:
        return list(_metric_sinks)


def list_audit_sinks() -> list[AuditSink]:
    with _sink_lock:
        return list(_audit_sinks)


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=_SINK_WORKERS, thread_name_prefix="dp-obs")
        return _executor


def fanout_sinks(sinks: list[Callable], payload: Any, *, kind: str = "observability") -> None:
    """Invoke sinks with timeout + exception isolation (OPS-01 failure isolation)."""
    for sink in sinks:
        try:
            future = _pool().submit(sink, payload)
            future.result(timeout=_SINK_TIMEOUT_S)
        except FuturesTimeout:
            _log.warning("%s sink timed out after %.1fs", kind, _SINK_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — isolation: never fail the caller
            _log.warning("%s sink failed", kind, exc_info=True)


def emit_metric(
    name: MetricName,
    value: float = 1.0,
    *,
    type: MetricType | None = None,
    unit: MetricUnit = MetricUnit.UNIT,
    labels: dict[str, str] | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> MetricEvent | None:
    """Build + fan out a metric event. Returns the event (or None if construction failed)."""
    try:
        event = MetricEvent(
            name=name,
            type=type or _default_type(name),
            unit=unit,
            value=float(value),
            labels=dict(labels or {}),
            request_id=request_id if request_id is not None else get_request_id(),
            run_id=run_id,
            attempt_id=attempt_id,
        )
    except Exception:  # noqa: BLE001 — bad labels must not break the caller
        _log.warning("metric event rejected", exc_info=True)
        return None
    fanout_sinks(list_metric_sinks(), event, kind="metric")
    return event


def emit_audit(
    action: AuditAction,
    outcome: AuditOutcome,
    *,
    principal_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    attrs: dict[str, str] | None = None,
) -> AuditEvent | None:
    try:
        event = AuditEvent(
            action=action,
            outcome=outcome,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id if request_id is not None else get_request_id(),
            run_id=run_id,
            attempt_id=attempt_id,
            attrs=dict(attrs or {}),
        )
    except Exception:  # noqa: BLE001
        _log.warning("audit event rejected", exc_info=True)
        return None
    fanout_sinks(list_audit_sinks(), event, kind="audit")
    return event


def _default_type(name: MetricName) -> MetricType:
    if name in (
        MetricName.HTTP_DURATION_MS,
        MetricName.RUN_DURATION_MS,
        MetricName.RUN_QUEUE_DELAY_MS,
        MetricName.RUN_CANCEL_LATENCY_MS,
    ):
        return MetricType.HISTOGRAM
    if name == MetricName.KERNEL_HEALTH:
        return MetricType.GAUGE
    return MetricType.COUNTER


class InMemoryObservabilitySink:
    """Test / in-process sink that records metric and audit events."""

    def __init__(self) -> None:
        self.metrics: list[MetricEvent] = []
        self.audits: list[AuditEvent] = []
        self._lock = threading.Lock()

    def on_metric(self, event: MetricEvent) -> None:
        with self._lock:
            self.metrics.append(event)

    def on_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self.audits.append(event)

    def register(self) -> "InMemoryObservabilitySink":
        add_metric_sink(self.on_metric)
        add_audit_sink(self.on_audit)
        return self

    def clear(self) -> None:
        with self._lock:
            self.metrics.clear()
            self.audits.clear()

    def label_sets(self, name: MetricName) -> list[frozenset[tuple[str, str]]]:
        with self._lock:
            return [frozenset(e.labels.items()) for e in self.metrics if e.name == name]


def finished_run_metric_labels(status: str | None, placement: str | None) -> dict[str, str]:
    return {
        "status": _bucket_status(status),
        "placement": _bucket_placement(placement),
        "outcome": "success" if status == "done" else ("denied" if status == "cancelled" else "failure"),
        "error_class": "none" if status == "done" else ("cancelled" if status == "cancelled" else "internal"),
    }


def _bucket_status(status: str | None) -> str:
    if status in ("queued", "running", "done", "failed", "cancelled"):
        return status
    return "other"


def _bucket_placement(placement: str | None) -> str:
    if placement in ("local", "subprocess", "distributed", "ray", "kernel"):
        return placement
    if not placement:
        return "local"
    return "other"


def invoke_backend_run(backend, plan, graph, target_node_id, placement, *,
                       run_id: str | None = None, request_id: str | None = None,
                       attempt_id: str | None = None):
    """Call ``backend.run`` forwarding optional correlation kwargs when the backend accepts them.

    The ``ExecutionBackend`` Protocol keeps the four positional parameters; optional ``run_id``,
    ``request_id``, and ``attempt_id`` are feature-detected (same pattern as existing LocalRunner
    ``run_id`` / ``cancel_check`` kwargs).
    """
    import inspect

    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(backend.run).parameters
    except (TypeError, ValueError):
        params = {}
    if run_id is not None and "run_id" in params:
        kwargs["run_id"] = run_id
    if request_id is not None and "request_id" in params:
        kwargs["request_id"] = request_id
    if attempt_id is not None and "attempt_id" in params:
        kwargs["attempt_id"] = attempt_id
    status = backend.run(plan, graph, target_node_id, placement, **kwargs)
    if request_id and getattr(status, "request_id", None) in (None, ""):
        try:
            status.request_id = request_id
        except Exception:  # noqa: BLE001
            pass
    return status


# Re-export Literal helpers used by docs/tests
HttpOutcome = Literal["success", "failure", "denied", "error"]
