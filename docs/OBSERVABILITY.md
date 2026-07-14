# Observability and audit contracts

This document is the **stable, versioned contract** for Data Playground metrics, audit events, and
trace/request-ID propagation (roadmap slice 12 / OPS-01, first PR). Core defines shapes and sink seams;
it ships **no** OpenTelemetry, Prometheus, StatsD, or vendor exporter. A future plugin implements those
adapters against the seams below.

Schema version for typed models in [`kernel/hub/observability.py`](../kernel/hub/observability.py):
**`schema_version = 1`**.

## Design rules

1. **Low cardinality by construction.** Metric *labels* may only use the allow-listed keys below, with
   short enum / bucket values. Raw IDs (`run_*`, canvas ids), URIs, user input, and error strings are
   **never** label values. Correlation IDs live on event fields (`request_id`, `run_id`, `attempt_id`),
   not labels.
2. **Redaction.** Audit `attrs` never carry passwords, tokens, API keys, credential URIs, or raw row /
   column sample values. Failed validation rejects the event (logged) rather than emitting a leak.
3. **Failure isolation.** A sink that raises or exceeds a short timeout is logged and swallowed. Run
   status, row counts, and stored outputs are identical with or without sinks.
4. **Offline-first.** Registering zero sinks is valid; nothing is exported until a plugin attaches.

## Trace / request ID propagation

| Hop | Behavior |
| --- | --- |
| HTTP entry | Middleware reads `X-Request-Id` (or mints `req_<hex>`), sets a contextvar, echoes the header on every handled response (a framework-level 500 short-circuits before the middleware). |
| WebSocket entry | Same mint/normalize from the handshake `X-Request-Id` header into the contextvar. |
| Run start | `start_run` stamps `RunStatus.request_id` and persists it on `run_states.request_id`. |
| Durable history | Finished runs store `run_records.request_id`. |
| Backend port | Optional kwargs `request_id` / `run_id` / `attempt_id` are forwarded when an `ExecutionBackend.run` implementation accepts them (`invoke_backend_run`). |
| Kernel | `KernelBackend` includes `request_id` in the kernel `/run` body; `RunBody` accepts it. |

Clients should treat `X-Request-Id` as opaque. Correlating a run uses `request_id` + `run_id` (+
`attempt_id` for managed object publication).

## Metric catalog

Each metric is a typed `MetricEvent`: `{schema_version, name, type, unit, value, labels, ts, request_id?, run_id?, attempt_id?}`.

### Allowed label keys

`status`, `outcome`, `placement`, `backend`, `method`, `route_class`, `action`, `kind`, `error_class`,
`probe`, `ready`.

`route_class` values are dotted buckets such as `api.run`, `api.auth`, `api.livez` (never raw URL paths
or path IDs).

Cardinality bound: every label value is a short enum/bucket (≤ 64 chars). Distinct canvases, datasets,
users, or URIs must **not** enlarge the label set.

| Name | Type | Unit | Labels | When emitted |
| --- | --- | --- | --- | --- |
| `dp.http.requests` | counter | `1` | `method`, `route_class`, `outcome` | Every HTTP response leave |
| `dp.http.duration_ms` | histogram | `ms` | `method`, `route_class`, `outcome` | Every HTTP response leave |
| `dp.run.state_transitions` | counter | `1` | `status`, `outcome`, `placement`, `error_class` | Run status transitions at finish (and queue→running when available) |
| `dp.run.duration_ms` | histogram | `ms` | `status`, `outcome`, `placement`, `error_class` | Finished run (`RunStatus.ms`) |
| `dp.run.queue_delay_ms` | histogram | `ms` | `placement`, `backend` | When queue delay is known (reserved; emitted when measured) |
| `dp.run.retries` | counter | `1` | `backend`, `error_class` | Retryable backend/storage retry (reserved for adapters) |
| `dp.run.cancel_latency_ms` | histogram | `ms` | `backend`, `outcome` | Cancel acknowledgement path (reserved when measured) |
| `dp.run.finished` | counter | `1` | `status`, `outcome`, `placement`, `error_class` | Once per finished logical run (alongside the legacy telemetry sink) |
| `dp.publication.events` | counter | `1` | `kind=publication`, `outcome`, `error_class` | Managed publication success/failure boundaries |
| `dp.storage.gc` | counter | `1` | `kind=gc`, `outcome`, `error_class` | Object-attempt GC batch outcomes |
| `dp.kernel.health` | gauge | `1` | `probe`, `ready` | `/api/livez` and `/api/readyz` probes |
| `dp.provider.errors` | counter | `1` | `error_class`, `kind` | Provider failures funneled through hub boundaries |

## Audit-event catalog

Each event is a typed `AuditEvent`: `{schema_version, action, outcome, principal_id?, resource_type?,
resource_id?, request_id?, run_id?, attempt_id?, attrs, ts}`.

| Action | Outcome | Principal / resource | Notes |
| --- | --- | --- | --- |
| `auth.login` | success / failure | principal = user id | Password never present |
| `auth.logout` | success | principal = user id | |
| `auth.password_change` | success / failure | principal = user id | |
| `admin.settings_change` | success / denied | principal = admin; resource = setting key | Values redacted; secret keys noted only as `sensitive=true` |
| `sharing.change` | success / denied | principal = owner; resource = canvas id | `attrs.op` = `share` / `unshare` / `visibility` |
| `dataset.access` | success / denied | resource = dataset id | Emitted when access checks exist |
| `dataset.mutation` | success / failure / denied | resource = dataset id | Register / delete / upload |
| `agent.egress` | success / denied | — | **Schema only** until egress policy (#106) |
| `job.submit` | success / failure / denied | principal = caller; `run_id` | |
| `job.cancel` | success / failure / denied | principal = caller; `run_id` | |
| `secret_ref.change` | success / failure | — | **Schema only** until secret references (#107) |
| `policy.denial` | denied | principal + resource | **Schema only** until policy surfaces land |

Redaction rules: never include raw row values, plaintext secrets, or credential-bearing URIs in
`attrs` or metric labels. Use resource identity (dataset id / canvas id / setting key) only.

## Sink seams

| Seam | Registrar | Payload | Notes |
| --- | --- | --- | --- |
| Finished-run telemetry (legacy) | `Registry.add_telemetry_sink(fn)` | `dict` with `canvas_id`, `target_node_id`, `run_id`, `status`, `rows`, `ms`, `error`, `output_table`, `placement`, `per_node`, and **`request_id`** | Unchanged compatibility callback. Reference consumer: [`examples/plugins/dp_run_log`](../examples/plugins/dp_run_log/). |
| Metrics | `Registry.add_metric_sink(fn)` / `hub.observability.add_metric_sink` | `MetricEvent` | In-memory sink for tests: `InMemoryObservabilitySink`. |
| Audit | `Registry.add_audit_sink(fn)` / `hub.observability.add_audit_sink` | `AuditEvent` | Same isolation guarantees. |

### Relationship to `add_telemetry_sink`

`add_telemetry_sink` is **retained as-is** for the finished-run JSONL / custom-integration callback. New
ops tooling should prefer `MetricEvent` / `AuditEvent`. On each finished run the hub:

1. Persists history (`run_records`, including `request_id`).
2. Fans out the legacy telemetry dict (now with `request_id`).
3. Emits `dp.run.finished` + `dp.run.duration_ms` (and a state-transition counter) through metric sinks.

A plugin may implement only the legacy sink, only the new sinks, or both.

## In-memory test sink

```python
from hub.observability import InMemoryObservabilitySink, clear_sinks

clear_sinks()
sink = InMemoryObservabilitySink().register()
# … exercise HTTP / runs …
assert sink.metrics and sink.audits
```

Contract tests cover event shape validity, redaction, bounded label cardinality, request-ID
propagation to a fake `ExecutionBackend`, and fault isolation (raising + blocking sinks).

## Out of scope (follow-ups)

- OpenTelemetry / Prometheus / StatsD adapters and any hosted backend.
- Dashboards, SLO definitions, alert policies, log retention.
- Emitting `agent.egress`, `secret_ref.change`, or `policy.denial` until those features land.
- Ray operator metrics and job links (planned issue on top of this contract).
