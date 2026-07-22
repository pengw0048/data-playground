# Observability for operators and plugin authors

Data Playground can report process health, emit typed metric and audit events, and fan out one
normalized record when a run finishes. Core provides those event and plugin contracts. It does **not**
ship a Prometheus or OpenTelemetry collector, a dashboard, alerting, a log backend, or retention.
Choose and operate those services outside Data Playground, then connect a trusted plugin to the
appropriate sink.

This guide starts with the operational task. The [reference contracts](#reference-contracts) at the end
define the stable event shapes and inventories.

## Choose the right path

| Need | Use | Do not assume |
| --- | --- | --- |
| Tell whether this process can serve traffic | `/api/livez` and `/api/readyz` | That either probe checks a sink, collector, dashboard, or alert route |
| Retain a record after each completed run | A trusted `add_telemetry_sink` plugin | That the Jobs history is an external log backend |
| Export typed counters, timings, or audit actions | A trusted metric or audit sink | That core exports Prometheus, OTLP, or vendor data by default |
| Run a shared observability service | Your collector, storage, access policy, retention, and alerts | That a green core health probe certifies that service |

## 1. Inspect the built-in health and run evidence

Use the probes from the process that serves the API:

- `GET /api/livez` returns `{"ok": true}` when that process is serving. It deliberately does not test
  the database, storage, a plugin, or an external observability service.
- `GET /api/readyz` checks that the metadata database responds, its schema is at this build's head, and
  DuckDB is responsive. It returns `503` with the individual checks when the process is not ready.
- `GET /api/version` reports redacted deployment identity such as package version, commit SHA, selected
  backend names, and core-library versions. It does not return database credentials or a storage path.

The Jobs UI and run APIs retain the run's status, result metadata, and `request_id`; they are the
application's run history. They are not a general event store. With no registered sink, metric, audit,
and finished-run telemetry events stay local to the process and are not exported or replayed later.

Every handled HTTP or WebSocket request is assigned an opaque request ID (or supplies a safe one). HTTP
responses echo it as `X-Request-Id`; the same identifier follows a submitted run into its durable
history. Use it with `run_id` and, for managed publication, `attempt_id` when correlating evidence
across a system you operate.

## 2. Attach a telemetry sink

For one bounded record after every finished run, start with the task-first
[finished-run telemetry guide](PLUGIN_ONBOARDING.md#record-finished-run-telemetry). Its smallest
reference, [`dp_run_log`](../examples/plugins/dp_run_log/), registers a trusted telemetry sink and
appends one JSON line per completed run to its configured `DP_RUN_LOG` path.

The sink is for observation only. If an external system must schedule, execute, cancel, or publish a
run, it belongs behind an execution backend instead. If it needs typed metrics or audit actions, use
`Registry.add_metric_sink` or `Registry.add_audit_sink`; their exact registration contracts are in the
[plugin reference](PLUGINS.md#the-rest-of-the-spi).

Before relying on a wheel, run the installed-wheel conformance check. It builds only the core and
`dp_run_log` candidates, discovers the plugin through its entry point in a clean environment, delivers
a finished-run record, and stops the sink worker:

```bash
cd kernel
uv run pytest -q hub/tests/test_plugin_wheel_conformance.py::test_run_log_wheel_conformance_uses_only_its_entry_point
```

Use the same pattern for a third-party wheel, then add integration tests for its actual collector or
storage. The core check proves plugin activation and the bounded callback contract; it cannot prove a
network service, credentials, retention policy, or alert route. The [full telemetry conformance
reference](PLUGINS.md#verifying-it) explains the installed-wheel boundary.

## 3. Verify delivery, redaction, and shutdown expectations

Sink delivery is intentionally best-effort and isolated from the request and run paths:

- Each registered metric, audit, or telemetry sink has one daemon worker and a queue of at most 256
  events. At most 32 sink workers are registered process-wide.
- Producers enqueue and continue. A slow or raising callback cannot change a request result, run result,
  or stored data. When a queue is full, the newest event is dropped and core logs the first drop and
  later powers of two.
- Queues are in memory. Core makes no durable-delivery, replay, ordering-across-processes, or lossless
  delivery promise. On application shutdown it gives healthy workers one shared second to drain; a
  wedged daemon worker cannot hold the process open.
- Metric labels are restricted to short, low-cardinality buckets. Audit attributes reject
  secret-shaped keys or values and must not contain row samples. A rejected metric or audit event is
  logged rather than delivered.

The finished-run telemetry record has a different trust boundary: it intentionally contains run and
canvas identifiers, status, error text, and output metadata. Committed output metadata can include a
URI or catalog version. Treat a sink and its destination as trusted with that information; do not expose
the `dp_run_log` file or a custom sink endpoint as a public event feed.

Run the core contract suite when changing an observability plugin or relying on these limits:

```bash
cd kernel
uv run pytest -q hub/tests/test_observability_contracts.py
```

It covers event validation and redaction, request-ID propagation, bounded queues and workers, overload
logging, fault isolation, and bounded shutdown. It is not a substitute for testing the service that
receives the events.

## 4. Operate a trusted-team shared service

The supported shared-service boundary is defined in [Supported deployments and trust model](SUPPORT.md).
In that profile, the operator supplies authentication, TLS, Postgres, durable storage, backups, network
controls, capacity, and the external observability system. Collector availability, storage retention,
access control, alerting, incident response, and service-level objectives are operator responsibilities.

Plugins and their dependencies are trusted code: registration hooks run in every trusted Data Playground
process that loads them, and can use that process's available capabilities. Review the package, its
configuration, its credential access, and the destination that receives events before installing it.
Neither a sink plugin nor its queue is a tenant-isolation boundary.

Registrations and delivery workers are process-scoped. A multi-process deployment must load and configure
the sink in each process that should emit to the external service; core does not aggregate events across
hubs. Monitor the collector and its credentials separately from `/api/livez` and `/api/readyz`.

## Reference contracts

This section is a reference, not a setup guide. The typed models use `schema_version = 1` in
[`kernel/hub/observability.py`](../kernel/hub/observability.py). A model change is a contract change.

### Event shapes and correlation

| Event | Shape |
| --- | --- |
| `MetricEvent` | `schema_version`, `name`, `type`, `unit`, `value`, `labels`, `ts`, and optional `request_id`, `run_id`, `attempt_id` |
| `AuditEvent` | `schema_version`, `action`, `outcome`, optional principal/resource/request/run/attempt identifiers, `attrs`, and `ts` |
| Finished-run telemetry | `canvas_id`, `target_node_id`, `run_id`, `request_id`, `job_type`, `status`, `rows`, `ms`, `error`, declaration-ordered `outputs`, `placement`, and `per_node` |

`X-Request-Id` is accepted only as a short safe token; otherwise core mints `req_<hex>`. It is forwarded
when an execution backend accepts the optional `request_id`, `run_id`, and `attempt_id` arguments. A
finished logical run is persisted first, then its telemetry record and finished-run metrics are fanned
out. Internal region runs do not create separate logical telemetry records.

### Sink registrations

| Contract | Registration | Intended payload |
| --- | --- | --- |
| Finished-run telemetry | `Registry.add_telemetry_sink(fn)` | One normalized `dict` after a finished logical run |
| Metrics | `Registry.add_metric_sink(fn)` or `hub.observability.add_metric_sink(fn)` | `MetricEvent` |
| Audit | `Registry.add_audit_sink(fn)` or `hub.observability.add_audit_sink(fn)` | `AuditEvent` |

All three registrations use the delivery limits described above. A plugin can implement one or more of
them, but should claim only the capabilities it actually tests. `InMemoryObservabilitySink` is a core test
helper, not an operator-facing collector.

### Metric inventory

Every metric label key is one of `status`, `outcome`, `placement`, `backend`, `method`, `route_class`,
`action`, `kind`, `error_class`, `probe`, or `ready`. Label values must be short buckets; never put a
raw URI, canvas ID, user input, error message, or `run_` / `req_` / `att_` identifier in a label.

| Metric | Type / unit | Current emission boundary |
| --- | --- | --- |
| `dp.http.requests` | counter / `1` | Each handled HTTP response |
| `dp.http.duration_ms` | histogram / `ms` | Each handled HTTP response |
| `dp.run.finished` | counter / `1` | Each finished logical run |
| `dp.run.state_transitions` | counter / `1` | Each finished logical run |
| `dp.run.duration_ms` | histogram / `ms` | Finished runs with a measured duration |
| `dp.publication.events` | counter / `1` | Managed publication success or failure boundary |
| `dp.storage.gc` | counter / `1` | Managed object-attempt GC outcomes |
| `dp.provider.errors` | counter / `1` | Provider failures surfaced by managed GC |
| `dp.kernel.health` | gauge / `1` | `/api/livez` and `/api/readyz` |
| `dp.run.queue_delay_ms` | histogram / `ms` | Declared for a backend that measures queue delay; core does not emit it yet |
| `dp.run.retries` | counter / `1` | Declared for retrying adapters/backends; core does not emit it yet |
| `dp.run.cancel_latency_ms` | histogram / `ms` | Declared for a measured cancellation acknowledgement; core does not emit it yet |

### Audit inventory

`AuditEvent.attrs` is small, redacted metadata, not a general payload field. The following actions are
currently emitted by their corresponding application paths: `auth.login`, `auth.logout`,
`auth.password_change`, `admin.settings_change`, `sharing.change`, `dataset.access`,
`dataset.mutation`, `job.submit`, `job.cancel`, and `workspace.relink`. Their outcomes are `success`,
`failure`, or `denied` as appropriate to that path.

`agent.egress`, `secret_ref.change`, and `policy.denial` are schema members only; current core does not
emit them. Do not build an alert or compliance claim around those actions until the owning product path
exists.

### Deliberate non-goals

Core does not provide an exporter, collector, dashboard, alert policy, retention system, hosted
observability service, or a production readiness assertion for a third-party backend. A plugin can
connect to such a system, but its owner must provide the deployment, credentials, access controls, and
capability-specific integration evidence.
