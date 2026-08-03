"""Composition root — builds the plugin registries at startup.

The core depends only on the SPI. This wires the DEFAULT setup (DuckDB+Lance adapters,
local out-of-core runner, in-memory catalog, media/vector capabilities, node specs). Extra
plugin packs are discovered two ways (§8.0): a drop-in `plugins/<pack>/` folder in the
workspace, and pip-installed packages exposing a `dataplay.plugins` entry point. Each calls
`register(reg)` to add nodes / adapters / runners / capabilities / catalog.
"""

from __future__ import annotations

import importlib
import importlib.util
import importlib.resources
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping

from hub.backends import (
    CatalogProvider,
    NodeBuilder,
    NodePreparer,
    PreparedNodeBuilder,
    _PreparedNodeRegistration,
)
from hub.models import (
    BackendInfo,
    CapabilityView,
    ExecutionTargetInfo,
    KernelInfo,
    ResultStorageInfo,
    ResourceSpec,
    WorkerInfo,
)
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec
from hub.plugins.adapters import DuckDBAdapter, default_adapters
from hub.plugins.capabilities import BUILTIN_CAPABILITIES
from hub.plugins.processors import ProcessorRegistry
from hub.plugins.runner import LocalRunner
from hub.settings import settings

# Version of the plugin SPI this core exposes. A plugin's dataplay.toml may declare `min_core_api`
# (an int); a pack requiring a newer core than this is skipped at load with a clear error instead of
# being registered and crashing later. Bump whenever a plugin may require a newly added contract.
CORE_API_VERSION = 2
# The OLDEST plugin API major this core still supports. Bump alongside CORE_API_VERSION when an old
# major is dropped, so the check is a semantic RANGE (min ≤ need ≤ core), not just a floor: a plugin
# built for a now-removed major is rejected up front instead of registering and crashing later (OSS-01).
MIN_SUPPORTED_API = 1


def _core_api_error(min_core) -> str | None:
    """Validate a plugin's declared `min_core_api` against this core's supported range. Returns a
    human error string if incompatible, or None if OK / undeclared (an undeclared plugin loads, as
    before). Shared by all three load paths (drop-in manifest, DP_PLUGINS module, entry point)."""
    if min_core is None:
        return None
    try:
        need = int(str(min_core).split(".")[0])  # accept 1, "1", or the documented "1.0" (major only)
    except ValueError:
        return f"min_core_api must be a version number, got {min_core!r}"
    if need > CORE_API_VERSION:
        return f"requires core API >= {need}; this core is {CORE_API_VERSION}"
    if need < MIN_SUPPORTED_API:
        return f"targets core API {need}; this core supports core API >= {MIN_SUPPORTED_API} (breaking SPI change)"
    return None


class Registry:
    """Passed to each plugin pack's register(reg) so it can add things (§8)."""

    def __init__(self, deps: "Deps"):
        self.deps = deps
        self._pack: str | None = None  # the pack currently registering — set by the loader for reg.config
        self._entry: dict | None = None

    def _activate(self, capability: str, placement: str, *, replace: bool = False) -> None:
        """Record only capabilities that actually entered this ``Deps`` instance's dispatch state."""
        if self._entry is not None:
            self.deps._activate_plugin_capability(
                self._entry, capability, placement, replace=replace)

    def _conflict(self, summary: str) -> None:
        if self._entry is not None:
            self.deps._record_plugin_problem(self._entry, summary, conflict=True)

    def _problem(self, summary: str) -> None:
        if self._entry is not None:
            self.deps._record_plugin_problem(self._entry, summary)

    def workspace_identity(self) -> str:
        """Return an opaque stable identity for instance-local deterministic plugin state."""
        import hashlib
        return hashlib.sha256(os.path.abspath(self.deps.workspace).encode()).hexdigest()[:16]

    def config(self, key: str, default=None):
        """Read a config value for the CURRENTLY-registering pack. Ordinary fields use a UI-set value
        (metadb setting `plugin.<pack>.<key>`) > declared `env` var > declared `default` > `default`
        arg. A ``secret = true, workload_env = true`` field is the narrow exception: its ``env`` names
        only the child target, and it uses a persisted SecretRef or an explicit
        ``headless_secret_ref_env`` SecretRef binding instead. Fields are declared in the pack's
        dataplay.toml `[[config]]`. Call this inside register() to configure the pack; a value changed
        in the UI takes effect on the next kernel start (plugins register once at startup — same as the
        env vars it falls back to).

        When the field is ``secret``, the stored setting is a secret reference (``env:…`` / ``file:…``)
        and is resolved here; the material value never lives in the settings row.
        """
        pack = self._pack
        if self._entry is not None:
            schema = self._entry.get("config") or []
        else:
            schema = self.deps._manifests.get(pack, {}).get("config", []) if pack else []
        field = next((f for f in schema if isinstance(f, dict) and f.get("key") == key), None)
        if self._entry is not None and self._entry.get("source") == "entry_point" and field is None:
            return default
        secret = bool(field and field.get("secret"))
        if field and field.get("workload_env") is True:
            from hub.workload_env import WORKLOAD_CHILD_MARKER, workload_child_allows_plugin
            marker = os.environ.get(WORKLOAD_CHILD_MARKER)
            if marker is not None:
                if (
                    pack
                    and self._entry is not None
                    and self._entry.get("source") == "entry_point"
                    and workload_child_allows_plugin(marker, pack)
                ):
                    inherited = os.environ.get(str(field["env"]))
                    return inherited if inherited not in (None, "") else default
                return default
            # A workload target is a child-process capability name, not an ambient Hub binding.
            # The first process may use either the persisted SecretRef or the explicitly declared
            # headless *reference* environment variable. Never consult ``field['env']`` here: doing
            # so would let a manifest claim an unrelated Hub credential by target name alone.
            if not pack:
                return default
            from hub import metadb
            from hub.secrets import resolve_secret_value
            reference = metadb.get_setting(f"plugin.{pack}.{key}", "global", default=None)
            if reference in (None, ""):
                headless_ref_env = field.get("headless_secret_ref_env")
                reference = (
                    os.environ.get(headless_ref_env)
                    if isinstance(headless_ref_env, str) else None
                )
            if reference in (None, ""):
                return default
            try:
                if not _is_workload_secret_ref(reference):
                    raise ValueError("workload bindings require env: or file: references")
                return resolve_secret_value(reference, allow_plaintext=False)
            except Exception:
                raise RuntimeError(
                    f"workload configuration for plugin '{pack}' could not be resolved") from None
        if pack:
            from hub import metadb
            v = metadb.get_setting(f"plugin.{pack}.{key}", "global", default=None)
            if v not in (None, ""):
                if secret:
                    from hub.secrets import resolve_secret_value
                    if field and field.get("workload_env") is True:
                        try:
                            return resolve_secret_value(v, allow_plaintext=False)
                        except Exception:
                            raise RuntimeError(
                                f"workload configuration for plugin '{pack}' "
                                "could not be resolved") from None
                    return resolve_secret_value(v)
                return v
        if field and field.get("env") and os.environ.get(field["env"]) not in (None, ""):
            return os.environ[field["env"]]
        if field and field.get("default") is not None:
            return field["default"]
        return default

    def add_secret_resolver(self, scheme: str, resolver) -> None:
        """Register a pluggable SecretResolver for ``scheme:…`` references (see ``hub.secrets``).

        Core ships ``env`` and ``file``. A third-party backend (such as a secret manager) is a plugin
        that calls this during ``register(reg)`` — core never imports a vendor client.
        """
        from hub.secrets import register_resolver
        try:
            register_resolver(scheme, resolver)
        except Exception as e:
            if "already registered" in str(e):
                self._conflict(f"Secret resolver '{scheme}' conflicts with an existing resolver.")
            else:
                self._problem(f"Secret resolver '{scheme}' is invalid; use a URI-style scheme name.")
            raise
        self._activate(f"secret-resolver:{scheme.lower()}", "application")

    def add_node(
            self, spec: NodeSpec, build: "NodeBuilder | PreparedNodeBuilder | None" = None,
            ir=None, *, prepare: "NodePreparer | None" = None) -> None:
        # `build` is the node's build callable — see hub.backends.NodeBuilder for its exact
        # signature/return contract (called by the engine as build(engine, node, inputs)).
        # `ir` is an OPTIONAL engine-neutral emit hook: ir(node) -> {"op", "config"} | None. When given,
        # the node lowers to that IR op (e.g. a clean `map` with inlined `code`) instead of `opaque:<kind>`,
        # so a distributed backend (dp_ray) can run it — NOT just DuckDB. The plugin guarantees its build()
        # and its ir op compute the same thing (like the built-in transform shares its operator).
        # Preparation is intentionally one local-engine lifecycle, not another portable-planning
        # mechanism. A prepared node therefore cannot also emit distributed IR.
        if prepare is not None and build is None:
            raise ValueError("a prepared node requires a builder")
        if prepare is not None and ir is not None:
            raise ValueError("a prepared node cannot also register distributed IR")
        if prepare is not None and not callable(prepare):
            raise TypeError("node preparer must be callable")
        # refuse to shadow a built-in OR an already-registered plugin kind — overwriting would
        # corrupt the /api/nodes contract and leave the original's build() as dead code
        if spec.kind in self.deps.builtin_kinds:
            print(f"[deps] plugin node '{spec.kind}' collides with a built-in kind — refused")
            self._conflict(f"Node '{spec.kind}' conflicts with a built-in node.")
            return
        if spec.kind in self.deps.node_specs:
            print(f"[deps] plugin node '{spec.kind}' already registered by another plugin — refused")
            self._conflict(f"Node '{spec.kind}' conflicts with another plugin.")
            return
        # Keep the registry's declared owner with the schema so consumers such as the node finder
        # can identify an active plugin without guessing from a node kind.
        spec = spec.model_copy(update={"source": f"plugin:{self._pack or 'unknown'}"})
        self.deps.node_specs[spec.kind] = spec
        if build is not None:
            self.deps.node_builders[spec.kind] = (
                _PreparedNodeRegistration(build=build, prepare=prepare)
                if prepare is not None else build
            )
        if ir is not None:
            self.deps.node_ir[spec.kind] = ir
        self._activate(f"node:{spec.kind}", "execution")

    def add_telemetry_sink(self, sink) -> None:
        """Register a callback invoked once per FINISHED run with a normalized telemetry record (a dict:
        canvas_id/run_id/request_id/job_type/status/rows/ms/error/outputs/placement/per_node). Core ships no
        exporter — an OTel/StatsD/log sink is a plugin. Delivery uses a finite per-sink queue; callback
        failures and overload are logged and never fail a run. See add_metric_sink / add_audit_sink."""
        if callable(sink):
            from hub.observability import register_sink_delivery
            self.deps.telemetry_sinks.append(register_sink_delivery(sink, kind="telemetry"))
            self._activate("telemetry-sink", "application")

    def add_metric_sink(self, sink) -> None:
        """Register a MetricEvent consumer (OPS-01). See docs/OBSERVABILITY.md. Isolation matches
        add_telemetry_sink — delivery never waits on plugin I/O in a request or run path."""
        from hub.observability import add_metric_sink
        add_metric_sink(sink)
        if callable(sink):
            self._activate("metric-sink", "application")

    def add_audit_sink(self, sink) -> None:
        """Register an AuditEvent consumer (OPS-01). See docs/OBSERVABILITY.md."""
        from hub.observability import add_audit_sink
        add_audit_sink(sink)
        if callable(sink):
            self._activate("audit-sink", "application")

    def add_adapter(self, adapter) -> None:
        self.deps.adapters.insert(0, adapter)  # plugins claim uris before defaults
        self._activate(f"adapter:{getattr(adapter, 'name', adapter.__class__.__name__)}", "execution")

    def add_runner(self, runner) -> None:
        # runner should satisfy hub.backends.ExecutionBackend; materialized in registration order after
        # the built-in local runner exists, then inserted first so it wins pick_runner.
        self.deps._runner_registrations.append((self._entry, self._pack, lambda _deps: runner))

    def add_runner_factory(self, factory) -> None:
        """Register ``factory(deps) -> ExecutionBackend`` for composition after the local runner exists.

        Catalog selection must finish before any runner captures it.  A backend that also delegates to
        the built-in runner (for example dp_ray) therefore registers a factory instead of constructing
        itself during plugin discovery.
        """
        self.deps._runner_registrations.append((self._entry, self._pack, factory))

    def add_capability(self, cap) -> None:
        self.deps.capabilities.append(cap)
        detect = getattr(cap, "detect", None)  # optional column detector → tag_columns applies it (no core edit)
        if callable(detect):
            from hub.plugins import capabilities as caps
            caps.register_detector(getattr(cap, "id", ""), detect)
        self._activate(f"column-capability:{getattr(cap, 'id', cap.__class__.__name__)}", "execution")

    def add_processor(self, proc) -> None:
        self.deps.registry.register(proc)
        # Processor dispatch is keyed by id and intentionally keeps the latest registration.
        # Transfer status ownership as well so an overridden plugin is not still reported as effective.
        self._activate(f"processor:{proc.id}", "execution", replace=True)

    def set_catalog(self, catalog) -> None:
        if not isinstance(catalog, CatalogProvider):
            # CatalogProvider is the single source of truth. Derive the diagnostic from its public
            # protocol methods instead of maintaining a second contract list that could drift.
            missing = sorted(
                name
                for name, member in CatalogProvider.__dict__.items()
                if not name.startswith("_")
                and callable(member)
                and not callable(getattr(catalog, name, None))
            )
            detail = f"; missing methods: {', '.join(missing)}" if missing else ""
            raise TypeError(f"catalog provider does not satisfy CatalogProvider{detail}")
        self.deps.catalog = catalog
        self._activate("catalog", "application", replace=True)

    def set_managed_object_provider(self, provider) -> None:
        """Install the proof-capable exact-object lifecycle provider for managed storage."""
        from hub.handoff import set_runtime_managed_object_provider
        self.deps.managed_object_provider = provider
        set_runtime_managed_object_provider(provider)
        self._activate("managed-object-lifecycle", "application", replace=True)

    def add_embedder(self, fn, model: str = "custom") -> None:
        """Register a text embedder — `fn(list[str]) -> list[list[float]]` — to power the catalog's
        semantic + hybrid search over dataset name/description/columns. Core ships NONE (an embedding
        model is a heavy, opinionated dependency); a plugin provides one (see examples/plugins/
        dp_semantic_catalog). The catalog reindexes existing entries best-effort in the background. A
        catalog provider that doesn't support embedding simply ignores this."""
        setter = getattr(self.deps.catalog, "set_embedder", None)
        if callable(setter):
            setter(fn, model)
            self._activate("embedder", "application", replace=True)

    def set_importer(self, importer) -> None:
        # a pipeline importer (§5.6/§7.5). Without one, deps.importer stays the NullImporter → the
        # /pipelines/import endpoint reports 'not configured' (501), not a broken 500.
        self.deps.importer = importer
        self._activate("pipeline-importer", "application", replace=True)

    def add_destination(self, backend) -> None:
        # a save/open-dialog "place" backend (a storage/warehouse browser+writer). Should satisfy
        # hub.destinations.DestinationBackend (kind + browse + target_uri); claims its `kind` so a
        # target uri of that scheme can be browsed/picked. The built-in local/s3/gs go through the
        # same registry — this seam just lets register(reg) add one instead of a module-level call.
        from hub import destinations
        destinations.register_backend(backend)
        self._activate(f"destination:{backend.kind}", "application", replace=True)

    def add_external_wait_adapter(self, adapter) -> None:
        """Register one provider-neutral external-wait adapter on this application instance."""
        from hub.external_wait import ExternalWaitAdapter, normalize_provider_kind
        try:
            kind = normalize_provider_kind(getattr(adapter, "provider_kind", None))
            valid = isinstance(adapter, ExternalWaitAdapter) and all(
                callable(getattr(adapter, method, None))
                for method in ("submit", "status", "cancel", "download"))
        except Exception:  # noqa: BLE001 — plugin values never enter public status
            kind = None
            valid = False
        if kind is None or not valid:
            self._problem("External-wait adapter registration is invalid.")
            return
        if kind in self.deps.external_wait_adapters:
            self._conflict(f"External-wait provider kind '{kind}' conflicts with another plugin.")
            return
        self.deps.external_wait_adapters[kind] = adapter
        self._activate(f"external-wait:{kind}", "application")

    def add_external_wait_node(self, spec: NodeSpec, provider_kind: str) -> None:
        """Bind one single-output plugin node to its provider without exposing provider code to core."""
        from hub.external_wait import normalize_provider_kind
        kind = normalize_provider_kind(provider_kind)
        if spec.inputs or len(spec.outputs) != 1 or kind not in self.deps.external_wait_adapters:
            raise ValueError("external-wait node requires one active adapter and one output")
        if spec.kind in self.deps.builtin_kinds or spec.kind in self.deps.node_specs:
            raise ValueError("external-wait node kind conflicts with an existing node")
        self.add_node(spec)
        self.deps.external_wait_nodes[spec.kind] = kind


def _persist_run(deps, graph, target, status) -> None:
    """Runner on_complete hook (bound to the owning deps): keep a finished run with its canvas
    (canvas id == graph.id), including the per-node breakdown (durable telemetry), then fan the
    finished-run telemetry record out to any plugin sinks."""
    from hub import metadb
    from hub.observability import (
        MetricName, MetricUnit, emit_metric, finished_run_metric_labels, get_request_id,
    )
    # Region runners complete an internal implementation detail, not a logical user run.  Do not send
    # their deliberately partial status through either the durable history contract or telemetry; the
    # controller publishes the one complete logical run after all regions settle.
    if getattr(graph, "id", None) == "_region":
        return
    per_node = [p.model_dump() for p in (status.per_node or [])] or None
    request_id = getattr(status, "request_id", None) or get_request_id()
    persisted_target = status.target_node_id or target
    metadb.record_run(canvas_id=getattr(graph, "id", None), target_node_id=persisted_target,
                      target_port_id=status.target_port_id,
                      job_type=status.job_type, status=status.status,
                      rows=status.total_rows, ms=status.ms, error=status.error,
                      outputs=[output.model_dump() for output in status.outputs], per_node=per_node,
                      profile=status.profile.model_dump() if status.profile else None,
                      run_id=status.run_id, request_id=request_id,
                      execution_manifest_sha256=getattr(
                          graph, "_execution_manifest_sha256", None),
                      execution_manifest_doc=getattr(graph, "_execution_manifest_doc", None))
    _emit_telemetry(deps, graph, persisted_target, status, per_node, request_id=request_id)
    labels = finished_run_metric_labels(status.status, status.placement)
    emit_metric(MetricName.RUN_FINISHED, 1.0, labels=labels,
                request_id=request_id, run_id=status.run_id)
    emit_metric(MetricName.RUN_STATE, 1.0, labels=labels,
                request_id=request_id, run_id=status.run_id)
    if status.ms is not None:
        emit_metric(MetricName.RUN_DURATION_MS, float(status.ms), unit=MetricUnit.MILLISECONDS,
                    labels=labels, request_id=request_id, run_id=status.run_id)


def _emit_telemetry(deps, graph, target, status, per_node, *, request_id=None) -> None:
    """Fan a finished run's normalized telemetry record out to registered sinks (reg.add_telemetry_sink).
    Core ships NO exporter — an OTel/StatsD/etc. sink is a plugin. A broken or slow sink never breaks a run."""
    from hub.observability import fanout_sinks

    sinks = getattr(deps, "telemetry_sinks", None)
    if not sinks:
        return
    rid = request_id if request_id is not None else getattr(status, "request_id", None)
    record = {"canvas_id": getattr(graph, "id", None), "target_node_id": target, "run_id": status.run_id,
              "request_id": rid,
              "job_type": status.job_type,
              "status": status.status, "rows": status.total_rows, "ms": status.ms, "error": status.error,
              "outputs": [output.model_dump() for output in status.outputs],
              "placement": status.placement, "per_node": per_node}
    fanout_sinks(list(sinks), record, kind="telemetry")


def _persist_run_state(graph, status) -> None:
    """Runner on_status hook: upsert the run's live status to the shared DB on every transition, so
    GET /run/{id} + the status WebSocket are answerable from ANY web instance and survive a restart
    (not just the in-memory dict of the instance that accepted the run)."""
    from hub import metadb
    metadb.save_run_state(
        status.run_id, status.model_dump(), canvas_id=getattr(graph, "id", None),
        publish_region=status.status in ("done", "failed"),
        execution_manifest_sha256=getattr(
            graph, "_execution_manifest_sha256", None),
        execution_manifest_doc=getattr(graph, "_execution_manifest_doc", None),
    )


def _result_get(key):
    """Runner result-cache read hook: the DB-backed content-addressed result index (survives restart +
    shared across stateless instances), replacing the runner's per-process dict."""
    from hub import metadb
    return metadb.get_result(key)


def _result_acquire(key, owner, ttl_seconds):
    from hub import metadb
    return metadb.acquire_result_cache_pin(key, owner, ttl_seconds)


def _result_put(key, doc) -> None:
    from hub import metadb
    from hub.handoff import prepare_attempt_commit
    from hub.run_outputs import committed_document_outputs
    for output in committed_document_outputs(doc):
        prepare_attempt_commit(str(output.uri))
    metadb.put_result(key, doc)


_CONFIG_TYPES = {"string", "text", "int", "float", "bool", "select", "password"}
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_workload_secret_ref(value: object) -> bool:
    """Whether one workload binding uses the supported portable ``env:`` / ``file:`` model.

    Plugin-defined resolvers remain available to ordinary in-process secret settings. Workload
    forwarding intentionally stays on the two built-in reference types so a child receives material
    only after the Hub has resolved an inspectable operator binding.
    """
    from hub.secrets import SecretResolveError, parse_secret_ref

    try:
        scheme, _rest = parse_secret_ref(str(value))
    except (SecretResolveError, TypeError):
        return False
    return scheme in {"env", "file"}


def _normalize_config(raw) -> list[dict]:
    """dataplay.toml `[[config]]` → a clean list of UI fields. Keeps only entries with a non-empty string
    `key`; fills `type` (default 'string'; unknown → 'string') and `label` (default = key); passes through
    default/env/secret/workload_env/headless_secret_ref_env/options/help/placeholder. Malformed entries
    are dropped (never fatal)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        if not isinstance(f, dict) or not isinstance(f.get("key"), str) or not f["key"]:
            continue
        field = {"key": f["key"], "type": f.get("type") if f.get("type") in _CONFIG_TYPES else "string",
                 "label": str(f.get("label") or f["key"])}
        for k in ("default", "env", "secret", "workload_env", "headless_secret_ref_env", "options",
                  "help", "placeholder"):
            if k in f:
                field[k] = f[k]
        out.append(field)
    return out


def _validate_workload_env_config(config: list[dict]) -> str | None:
    """Keep the one plugin-to-workload declaration path narrow and non-overriding."""
    from hub.workload_env import is_core_workload_env_key

    keys: set[str] = set()
    env_targets: set[str] = set()
    all_declared_envs = {
        field["env"] for field in config if isinstance(field.get("env"), str)
    }
    headless_ref_envs: set[str] = set()
    for field in config:
        key = str(field["key"])
        if key in keys:
            return f"config key '{key}' is declared more than once"
        keys.add(key)
        declared_env = field.get("env")
        if isinstance(declared_env, str):
            if declared_env in env_targets:
                return f"config environment target '{declared_env}' is declared more than once"
            env_targets.add(declared_env)
        headless_ref_env = field.get("headless_secret_ref_env")
        if "workload_env" not in field:
            if headless_ref_env is not None:
                return (
                    f"config '{key}' may declare headless_secret_ref_env only with workload_env = true")
            continue
        if field["workload_env"] is not True:
            return f"workload_env config '{key}' must be true"
        env = declared_env
        if field.get("secret") is not True:
            return f"workload_env config '{key}' must set secret = true"
        if field.get("default") not in (None, ""):
            return f"workload_env config '{key}' cannot declare a default"
        if not isinstance(env, str) or not _ENVIRONMENT_NAME.fullmatch(env):
            return f"workload_env config '{key}' must declare an environment variable name"
        if is_core_workload_env_key(env):
            return f"workload_env config '{key}' cannot override core environment '{env}'"
        if headless_ref_env is not None:
            if (not isinstance(headless_ref_env, str)
                    or not _ENVIRONMENT_NAME.fullmatch(headless_ref_env)):
                return (
                    f"workload_env config '{key}' must declare headless_secret_ref_env as an "
                    "environment variable name")
            if headless_ref_env == env:
                return (
                    f"workload_env config '{key}' headless_secret_ref_env must differ from its "
                    "workload target")
            if headless_ref_env in all_declared_envs:
                return (
                    f"workload_env config '{key}' headless_secret_ref_env cannot reuse another "
                    "config environment name")
            if headless_ref_env in headless_ref_envs:
                return (
                    f"workload_env config '{key}' headless_secret_ref_env is declared more than once")
            if is_core_workload_env_key(headless_ref_env):
                return (
                    f"workload_env config '{key}' headless_secret_ref_env cannot use core environment "
                    f"'{headless_ref_env}'")
            headless_ref_envs.add(headless_ref_env)
    return None


def _host_capacity() -> ResourceSpec:
    """The local machine's resources, advertised as the capacity of the built-in local backends."""
    cpu = float(os.cpu_count() or 1)
    mem = None
    try:  # best-effort total RAM (Linux/macOS); GPUs unknown to the local backend
        mem = f"{os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') // (1024 ** 3)}GB"
    except (ValueError, OSError, AttributeError):
        pass
    return ResourceSpec(cpu=cpu, mem=mem)


def _make_spawner(workspace: str, data_dir: str):
    """The per-canvas kernel substrate (KernelSpawner). Built-ins: 'local' (a detached process) and 'pod'
    (a k8s Pod + Service). Anything else is a dotted path to a plugin spawner class
    (DP_KERNEL_SPAWNER=pkg.mod:Cls) instantiated as Cls(workspace, data_dir) — so a third substrate
    (ECS/Nomad/…) is a config value, not a core patch. The built-ins are just the two default paths here."""
    spec = settings.kernel_spawner
    low = spec.lower()
    if low in ("", "local"):
        from hub.kernel_backend import LocalProcessSpawner
        return LocalProcessSpawner(workspace, data_dir)
    if low == "pod":
        from hub.pod_spawner import PodSpawner
        return PodSpawner(workspace, data_dir)
    from hub.settings import import_dotted
    return import_dotted(spec)(workspace, data_dir)


class Deps:
    def __init__(self, workspace: str, data_dir: str, *, maintain_storage: bool = True):
        self.workspace = workspace
        self.data_dir = data_dir
        self.adapters = default_adapters()
        self.default_adapter = DuckDBAdapter()
        self.registry = ProcessorRegistry()
        from hub.plugins.importer import NullImporter
        self.importer = NullImporter()  # replaced by a plugin via reg.set_importer; else /import → 501
        self.capabilities = list(BUILTIN_CAPABILITIES)
        self.node_specs: dict[str, NodeSpec] = {s.kind: s for s in BUILTIN_NODE_SPECS}
        self.builtin_kinds = {s.kind for s in BUILTIN_NODE_SPECS}
        self.node_builders: dict[str, object] = {}
        self.node_ir: dict[str, object] = {}  # kind -> ir(node) hook: an engine-neutral emit path (§ IR unify B)
        # Registered delivery handles for reg.add_telemetry_sink callbacks (OTel/exporters stay plugins).
        self.telemetry_sinks: list = []
        self.managed_object_provider = None
        self.external_wait_adapters: dict[str, object] = {}
        self.external_wait_nodes: dict[str, str] = {}
        self.plugins: list[dict] = []
        # Status is instance-owned: constructing a second app/kernel must not reuse the first one's
        # discoveries, failures, or effective capability ownership.
        self._plugin_capability_owners: dict[str, dict] = {}
        self._manifests: dict[str, dict] = {}
        self._plugin_workload_fields: tuple[tuple[str, dict], ...] = ()
        # Plugins register before services are constructed.  Keep the collection available now so a
        # plugin backend can register itself, then append the built-ins after they bind the final catalog.
        self.runners: list = []
        self._runner_registrations: list[
            tuple[dict | None, str | None, Callable[[Deps], object]]
        ] = []
        from hub.storage import make_storage
        self.storage = make_storage(workspace)
        # The catalog is shared by every user (by design — one workspace, not one kernel per session);
        # per-user boundaries are enforced at the canvas/share/settings layer, not by isolating the data
        # engine. The DEFAULT catalog (the DB-backed InMemoryCatalog) is not instantiated directly here —
        # it is registered through the public reg.set_catalog seam by a bundled FIRST-PARTY plugin
        # (hub.plugins.default_catalog), loaded before any workspace/entry-point plugin. So the built-in
        # is the first implementation through the seam (not a privileged core path), and a plugin loaded
        # later can still replace it (set_catalog). See _load_bundled.
        self.catalog = None  # set by the bundled default-catalog plugin immediately below
        self._load_bundled()
        if self.catalog is None:
            raise RuntimeError("bundled default-catalog plugin did not install a catalog")
        # Catalog selection is a composition-time decision.  Do not construct a runner, profile
        # supervisor, or run controller until every plugin has had its one registration opportunity.
        self._load_plugins()
        # recover/clean any temp siblings an interrupted append/compaction left behind BEFORE re-cataloging,
        # so a crash can't surface a half-written staging file as a dataset or leave a compacting one absent.
        if maintain_storage:
            from hub import metadb
            self.storage.recover_orphans()
            try:
                metadb.reconcile_canvas_result_history_batch(limit=100)
            except Exception:  # retryable metadata failure must not block serving the workspace
                logging.getLogger("hub").warning(
                    "Canvas result retention failed at startup", exc_info=True)
            prune_results = getattr(self.storage, "prune_results", None)
            if callable(prune_results):
                try:
                    prune_results()  # bounded startup reconciliation for prior process crashes
                except Exception:  # retryable retention failure must not block serving the workspace
                    logging.getLogger("hub").warning(
                        "local result retention failed at startup", exc_info=True)
        # re-register previously written outputs so committed tables survive a kernel restart
        # (they live in storage, separate from the seeded data_dir).
        for uri in self.storage.list_outputs():
            try:
                self.catalog.get_table(uri)
                continue  # preserve the user-facing name, folder, tags, and description already stored
            except KeyError:
                pass  # an orphaned physical output still needs to be recovered into the catalog
            name = os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
            self.catalog.register_output(name=name, uri=uri, parents=[], pipeline="canvas")  # content-addressed version
        self.runner = LocalRunner(self.resolve_adapter, self.registry, self.catalog, workspace,
                                  node_builders=self.node_builders, node_specs=self.node_specs,
                                  storage=self.storage)
        # on_complete is bound to THIS deps so the finished-run telemetry fans out to sinks registered on
        # it (plugins load into the same deps/process — incl. the per-canvas kernel's own deps).
        _on_complete = lambda g, t, s: _persist_run(self, g, t, s)  # noqa: E731
        self.runner.on_complete = _on_complete  # keep finished runs with their canvas (run history)
        self.runner.on_status = _persist_run_state  # mirror live status to the DB (stateless-web reads)
        self.runner.result_get = _result_get  # DB-backed content-addressed result reuse (cross-run/restart)
        self.runner.result_acquire = _result_acquire
        self.runner.result_put = _result_put
        self.runners = [self.runner]
        self._materialize_plugin_runners()
        self._finalize_plugin_workload_env()
        # Whole-dataset profiles are inspection jobs, not materialized graph runs, but they share
        # the same durable RunState status/cancel/recovery contract.
        from hub.profile_jobs import ProfileProcessRunner
        self.profile_runner = ProfileProcessRunner(
            workspace, data_dir, storage=self.storage, node_specs=self.node_specs,
            registry=self.registry)
        self.profile_runner.on_complete = _on_complete
        self.profile_runner.on_status = _persist_run_state
        from hub.subprocess_runner import SubprocessRunner
        # a second, real backend: run jobs in an isolated OS process (Settings → Execution). Selected
        # by name via pick_runner; pod/Ray runners install as plugins over the same protocol.
        sub = SubprocessRunner(
            workspace, data_dir, catalog=self.catalog, storage=self.storage,
            resolve_adapter=self.resolve_adapter, node_builders=self.node_builders,
            node_specs=self.node_specs, registry=self.registry)
        sub.on_complete = _on_complete  # record cancelled/crashed isolated runs the child couldn't
        sub.on_status = _persist_run_state
        sub.result_put = _result_put
        self.runners.append(sub)
        # opt-in reference multi-worker pool (DP_POOL_WORKERS): capability-based placement without a
        # cluster — pods are processes with configured capacities. Shows in the Compute view + is
        # selectable/placeable. Absent → default behavior unchanged. (k8s/Ray = plugins over the same API.)
        from hub.pool_runner import PoolRunner, pool_workers_from_env
        pool_cfg = pool_workers_from_env()
        if pool_cfg:
            pool = PoolRunner(
                workspace, data_dir, pool_cfg, node_specs=self.node_specs, catalog=self.catalog,
                storage=self.storage, resolve_adapter=self.resolve_adapter,
                node_builders=self.node_builders, registry=self.registry)
            pool.on_complete = _on_complete
            pool.on_status = _persist_run_state
            pool.result_put = _result_put
            self.runners.append(pool)
        # per-canvas kernel: runs go to a long-lived, restart-surviving kernel process (one per canvas).
        # Always REGISTERED so it's selectable from Settings → Execution; only the DEFAULT is opt-in
        # (DP_EXECUTION=kernel, honored in pick_runner). The kernel writes run_states itself, so no
        # on_status/complete wiring here; estimate/can_run delegate to the base runner (hub-side gate).
        from hub.kernel_backend import KernelBackend
        self.runners.append(KernelBackend(self.runner, _make_spawner(workspace, data_dir)))
        # the local/kernel memory budget — cost-based placement routes a region whose estimated working
        # set EXCEEDS this to a backend with more memory (a no-op when none is registered). From the
        # DuckDB cap DP_MEMORY_LIMIT / DP_KERNEL_MEM, default 4GB. Set at spawn time for a pod/process.
        from hub.placement import _mem_gb
        _lm = os.environ.get("DP_MEMORY_LIMIT") or os.environ.get("DP_KERNEL_MEM") or "4GB"
        self.local_mem_bytes = int((_mem_gb(_lm) or 4.0) * (1 << 30))
        # RunController owns a logical run across placement regions (multi-region = a placed node /
        # checkpoint / fan-out); a single default region delegates to the base runner unchanged.
        from hub.run_controller import RunController
        self.controller = RunController(self, self.runner, self._place)
        self.controller.on_status = _persist_run_state
        self.controller.on_complete = _on_complete
        self.run_index: dict[str, object] = {}  # run_id -> the runner that owns it
        self.run_owner: dict[str, str] = {}  # run_id -> creator uid, to authorize ad-hoc (no-canvas) runs

    def _new_plugin_status(
        self,
        name: str,
        source: str,
        *,
        package: str | None = None,
        version: str | None = None,
        config: list[dict] | None = None,
        required: bool = False,
    ) -> dict:
        entry: dict = {
            "name": name,
            "package": package or name,
            "source": source,
            "state": "inactive",
            "required": required,
            "failure_impact": "startup-blocking" if required else "optional-degradation",
            "effective_capabilities": [],
            "process_placement": [],
            "_placements": {},
            "_problems": [],
            "_conflict": False,
        }
        if version:
            entry["version"] = version
        if config:
            entry["config"] = config
        self.plugins.append(entry)
        return entry

    def _refresh_plugin_status(self, entry: dict) -> None:
        capabilities = entry.get("effective_capabilities", [])
        problems = entry.get("_problems", [])
        if entry.get("_conflict"):
            state = "conflict"
        elif problems:
            state = "degraded" if capabilities else "failed"
        else:
            state = "active" if capabilities else "inactive"
        entry["state"] = state
        placements = entry.get("_placements", {})
        entry["process_placement"] = sorted(set(placements.values()))
        if problems:
            entry["failure_summary"] = problems[-1]
            # Preserve the pre-existing API field while ensuring only the sanitized summary is exposed.
            entry["error"] = problems[-1]
        else:
            entry.pop("failure_summary", None)
            entry.pop("error", None)

    def _record_plugin_problem(self, entry: dict, summary: str, *, conflict: bool = False) -> None:
        entry.setdefault("_problems", []).append(summary)
        if conflict:
            entry["_conflict"] = True
        self._refresh_plugin_status(entry)

    def _activate_plugin_capability(
        self,
        entry: dict,
        capability: str,
        placement: str,
        *,
        replace: bool = False,
    ) -> None:
        previous = self._plugin_capability_owners.get(capability)
        if replace and previous is not None and previous is not entry:
            if capability in previous.get("effective_capabilities", []):
                previous["effective_capabilities"].remove(capability)
            previous.get("_placements", {}).pop(capability, None)
            self._refresh_plugin_status(previous)
        self._plugin_capability_owners[capability] = entry
        if capability not in entry["effective_capabilities"]:
            entry["effective_capabilities"].append(capability)
            entry["effective_capabilities"].sort()
        entry["_placements"][capability] = placement
        self._refresh_plugin_status(entry)

    def resolve_adapter(self, uri: str):
        from hub import workspace_providers

        if workspace_providers.is_provider_dataset_uri(uri):
            return workspace_providers.provider_dataset_adapter(
                uri, self.resolve_physical_adapter)
        return self._resolve_registered_adapter(uri)

    def resolve_physical_adapter(self, uri: str):
        """Resolve an installed physical adapter without interpreting a Workspace binding URI."""
        return self._resolve_registered_adapter(uri)

    def _resolve_registered_adapter(self, uri: str):
        """Resolve only installed DatasetAdapters, without re-entering provider binding lookup."""
        for a in self.adapters:
            try:
                if a.matches(uri):
                    return a
            except Exception:  # noqa: BLE001
                continue
        return self.default_adapter

    def _external_wait_adapter(self, provider_kind: object):
        """Internal lookup reserved for the later durable Task consumer."""
        from hub.external_wait import normalize_provider_kind
        try:
            return self.external_wait_adapters.get(normalize_provider_kind(provider_kind))
        except ValueError:
            return None

    def _materialize_plugin_runners(self) -> None:
        """Construct registered plugin backends after catalog selection and the local base runner."""
        for entry, pack, factory in self._runner_registrations:
            try:
                runner = factory(self)
                self.runners.insert(0, runner)
                if entry is not None:
                    name = getattr(runner, "name", pack or runner.__class__.__name__)
                    self._activate_plugin_capability(
                        entry, f"runner:{name}", f"backend:{name}")
            except Exception as e:  # noqa: BLE001 — optional plugin failure remains non-fatal
                name = pack or getattr(factory, "__module__", "plugin-runner")
                print(f"[deps] plugin runner '{name}' failed: {e}")
                if entry is not None:
                    self._record_plugin_problem(
                        entry,
                        f"Runner activation failed ({type(e).__name__}); check plugin configuration and server logs.",
                    )

    def chosen_backend(self, uid: str | None = None, requested: str | None = None) -> str:
        """The selected execution backend NAME: per-user preference > workspace default > DP_EXECUTION >
        the default (the per-canvas KERNEL). Kernel-only: with no explicit choice, execution runs on the
        canvas's kernel — process isolation (a runaway transform only wedges that canvas, restartably) +
        durability (survives a hub restart) + warm reuse. A Canvas-level request wins over every default.
        Also drives preview/profile routing."""
        from hub import metadb
        if requested:
            return requested
        chosen = (metadb.get_setting("backend", "user", uid, default="") if uid else "") or ""
        if not chosen:
            chosen = metadb.get_setting("backend", "global", default="") or ""
        if not chosen:
            chosen = settings.execution or "kernel"   # DP_EXECUTION overrides; else the kernel is default
        return chosen

    def _finalize_plugin_workload_env(self) -> None:
        """Freeze conflict-free declarations from successfully active installed plugins.

        A declaration is eligible independently of whether an operator currently binds it: changing a
        Settings SecretRef takes effect for the next workload launch without a Hub restart.  Startup
        records only the names of bindings that are present, never their references or material.
        """
        candidates: list[tuple[str, dict, dict]] = []
        for entry in self.plugins:
            if entry.get("source") != "entry_point" or entry.get("state") != "active":
                continue
            pack = str(entry["name"])
            for field in entry.get("config") or []:
                if field.get("workload_env") is True:
                    candidates.append((pack, field, entry))

        by_target: dict[str, list[tuple[str, dict, dict]]] = {}
        for candidate in candidates:
            by_target.setdefault(str(candidate[1]["env"]), []).append(candidate)
        conflicts = {
            target for target, owners in by_target.items()
            if len(owners) > 1
        }
        for target in sorted(conflicts):
            for _pack, _field, entry in sorted(
                    by_target[target], key=lambda item: (item[0], str(item[1]["key"]))):
                self._record_plugin_problem(
                    entry,
                    f"Workload environment target '{target}' conflicts with another active plugin.",
                    conflict=True,
                )
        self._plugin_workload_fields = tuple(
            (pack, field)
            for pack, field, entry in sorted(
                candidates, key=lambda item: (item[0], str(item[1]["key"])))
            if str(field["env"]) not in conflicts and entry.get("state") == "active"
        )
        bound_names: list[str] = []
        for pack, field in self._plugin_workload_fields:
            reference = self._plugin_workload_secret_ref(pack, field)
            if reference in (None, ""):
                continue
            if _is_workload_secret_ref(reference):
                bound_names.append(f"{pack}.{field['key']}")
                continue
            entry = next((item for item in self.plugins if item.get("name") == pack), None)
            if entry is not None:
                self._record_plugin_problem(
                    entry,
                    f"Workload secret binding for plugin '{pack}' must be an env: or file: SecretRef.",
                )
        if bound_names:
            print("[deps] workload secret bindings enabled: " + ", ".join(sorted(bound_names)))

    @staticmethod
    def _plugin_workload_secret_ref(pack: str, field: Mapping[str, object]) -> object:
        """Return the explicit SecretRef configured for one workload field, never its material.

        ``env`` is intentionally absent from this lookup: it names the allowed child target only.
        ``headless_secret_ref_env`` names a Hub environment variable whose *value* is a SecretRef.
        """
        from hub import metadb

        reference = metadb.get_setting(
            f"plugin.{pack}.{field['key']}", "global", default=None)
        if reference not in (None, ""):
            return reference
        headless_ref_env = field.get("headless_secret_ref_env")
        return os.environ.get(headless_ref_env) if isinstance(headless_ref_env, str) else None

    def plugin_workload_env(
        self,
        *,
        inherited: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Resolve the operator-enabled installed-plugin credentials for a workload launch.

        This is deliberately not a general environment bridge: only a manifest field that is both
        ``secret = true`` and ``workload_env = true`` may contribute, under its declared ``env`` name.
        UI values remain SecretRefs in hub metadata; a manifest can alternatively declare a dedicated
        headless variable whose value is that SecretRef. Both resolve only at the first parent-side
        boundary. A marked workload child reuses only the already-forwarded declared target values.
        """
        from hub.secrets import resolve_secret_value

        forwarded: dict[str, str] = {}
        entries = {str(entry["name"]): entry for entry in self.plugins}
        for pack, field in self._plugin_workload_fields:
            entry = entries.get(pack)
            if entry is None or entry.get("state") != "active":
                continue
            target = str(field["env"])
            if inherited is not None:
                value = inherited.get(target)
            else:
                value = self._plugin_workload_secret_ref(pack, field)
                if value not in (None, ""):
                    try:
                        if not _is_workload_secret_ref(value):
                            raise ValueError("workload bindings require env: or file: references")
                        value = resolve_secret_value(value, allow_plaintext=False)
                    except Exception:  # never retain a resolver exception chain or configured reference
                        # An optional plugin binding must not make every Canvas unable to start. Reject
                        # this field, surface a sanitized plugin-status problem, and leave the workload
                        # without the target instead of leaking its reference/material in an exception.
                        summary = (
                            f"Workload secret binding for plugin '{pack}' could not be resolved; "
                            "set a valid env: or file: SecretRef.")
                        if summary not in entry.get("_problems", []):
                            self._record_plugin_problem(entry, summary)
                        value = None
            if value not in (None, ""):
                forwarded[target] = str(value)
        return forwarded

    def plugin_workload_names(self) -> tuple[str, ...]:
        """Installed plugin identities authorized by the hub marker for this workload."""
        active = {str(entry["name"]) for entry in self.plugins if entry.get("state") == "active"}
        return tuple(sorted({pack for pack, _field in self._plugin_workload_fields if pack in active}))

    def kernel_backend(self):
        """The registered per-canvas KernelBackend (for preview/profile routing), or None."""
        from hub.kernel_backend import KernelBackend
        return next((r for r in self.runners if isinstance(r, KernelBackend)), None)

    def pick_runner(self, plan, uid: str | None = None, requested: str | None = None):
        # An explicit Canvas target is a user-visible execution contract: reject an unavailable or
        # incompatible target instead of silently dispatching the job somewhere else. Defaults retain
        # the old stale-setting recovery and first-capable fallback for compatibility.
        chosen = self.chosen_backend(uid, requested)
        registered = {getattr(r, "name", None) for r in self.runners}
        if requested and chosen not in registered:
            raise ValueError(f"Canvas execution target '{chosen}' is not configured")
        if chosen and chosen not in registered:
            chosen = "kernel"  # a stale / uninstalled-plugin selection → the kernel DEFAULT, not the
            #                    generic first-capable runner (which silently was local-out-of-core)
        if chosen:
            for r in self.runners:
                if getattr(r, "name", None) != chosen:
                    continue
                if r.can_run(plan):
                    return r
                if requested:
                    raise ValueError(f"Canvas execution target '{chosen}' cannot run this graph")
        for r in self.runners:
            if r.can_run(plan):
                return r
        return self.runner

    # -- plugin discovery (§8.0) ------------------------------------------- #
    def _load_bundled(self) -> None:
        """Register the first-party DEFAULTS through the public plugin seam, BEFORE any external plugin.
        Today that's the default catalog: the built-in installs itself via reg.set_catalog exactly like a
        third-party catalog would, so it's the first implementation through the seam — not a privileged
        core instantiation — and a plugin loaded later can still replace it. This required plugin must
        install a catalog; startup cannot continue with an ambiguous composition root."""
        reg = Registry(self)
        from hub.plugins import default_catalog
        reg._pack = "default-catalog"
        entry = self._new_plugin_status(
            "default-catalog", "builtin", package="data-playground", required=True)
        reg._entry = entry
        try:
            default_catalog.register(reg)
        except Exception as e:
            self._record_plugin_problem(
                entry,
                f"Required plugin activation failed ({type(e).__name__}); startup cannot continue.",
            )
            raise
        finally:
            reg._pack = None
            reg._entry = None

    def _load_plugins(self) -> None:
        reg = Registry(self)
        # 1) drop-in folder: <workspace>/plugins/<pack>/ (a package with register(reg))
        plugins_dir = os.path.join(self.workspace, "plugins")
        if os.path.isdir(plugins_dir):
            if plugins_dir not in sys.path:
                sys.path.insert(0, plugins_dir)
            for name in sorted(os.listdir(plugins_dir)):
                pack = os.path.join(plugins_dir, name)
                if os.path.isdir(pack) and os.path.exists(os.path.join(pack, "__init__.py")):
                    if self._read_manifest(pack, name):  # skip a pack with a missing/bad/incompatible manifest
                        self._register_module(name, reg, source="drop-in")
        # 2) configured modules (DP_PLUGINS) + installed entry points
        for mod in settings.plugin_modules:
            self._register_module(mod, reg)
        try:
            from importlib.metadata import entry_points
            installed_entry_points = list(entry_points(group="dataplay.plugins"))
            duplicate_names = {
                name for name, count in Counter(
                    ep.name for ep in installed_entry_points).items()
                if count > 1
            }
            for ep in installed_entry_points:
                package = getattr(getattr(ep, "dist", None), "name", None) or ep.name
                version = getattr(getattr(ep, "dist", None), "version", None)
                entry = self._new_plugin_status(
                    ep.name, "entry_point", package=package, version=version)
                if ep.name in duplicate_names:
                    self._record_plugin_problem(
                        entry,
                        f"Installed plugin entry point name '{ep.name}' is declared more than once.",
                        conflict=True,
                    )
                    continue
                try:
                    fn = ep.load()
                    mod = sys.modules.get(getattr(fn, "__module__", "") or "")
                    manifest, manifest_error = self._read_installed_manifest(mod)
                    if manifest_error:
                        self._record_plugin_problem(entry, manifest_error)
                        continue
                    entry["config"] = (manifest.get("config") or None) if manifest else None
                    err = _core_api_error(getattr(mod, "MIN_CORE_API", getattr(mod, "min_core_api", None))) if mod else None
                    if err:  # entry-point plugin declares an unsupported core → skip before register (OSS-01)
                        self._record_plugin_problem(entry, err)
                        continue
                    reg._pack = ep.name
                    reg._entry = entry
                    fn(reg)
                except Exception as e:  # noqa: BLE001
                    print(f"[deps] entry-point plugin '{ep.name}' failed: {e}")
                    self._record_plugin_problem(
                        entry,
                        f"Plugin import or registration failed ({type(e).__name__}); check compatibility, configuration, and server logs.",
                    )
                finally:
                    reg._pack = None
                    reg._entry = None
        except Exception:  # noqa: BLE001
            pass

    def _read_manifest(self, pack_dir: str, name: str) -> bool:
        """Read + validate dataplay.toml (name/version required; optional `min_core_api`) and record
        it (§8.0). Returns whether the pack is OK to load: a missing/malformed manifest, or one whose
        `min_core_api` exceeds this core's CORE_API_VERSION, is recorded as an error and NOT loaded —
        an honest compat failure instead of a register()-time crash later."""
        path = os.path.join(pack_dir, "dataplay.toml")
        if not os.path.exists(path):
            return True  # no manifest is allowed (loads unversioned); only a PRESENT-but-bad one blocks
        try:
            import tomllib
            with open(path, "rb") as f:
                man = tomllib.load(f)
            missing = [k for k in ("name", "version") if k not in man]
            if missing:
                entry = self._new_plugin_status(name, "drop-in")
                self._record_plugin_problem(
                    entry, f"Manifest is missing required fields: {', '.join(missing)}.")
                return False
            err = _core_api_error(man.get("min_core_api"))
            if err:
                entry = self._new_plugin_status(
                    name, "drop-in", package=str(man["name"]), version=str(man["version"]))
                self._record_plugin_problem(entry, err)
                return False
            man["config"] = _normalize_config(man.get("config"))  # [[config]] → clean UI-field list (may be [])
            config_error = _validate_workload_env_config(man["config"])
            if config_error:
                entry = self._new_plugin_status(name, "drop-in")
                self._record_plugin_problem(entry, config_error)
                return False
            self._manifests[name] = man
            return True
        except Exception as e:  # noqa: BLE001
            entry = self._new_plugin_status(name, "drop-in")
            self._record_plugin_problem(
                entry,
                f"Manifest is invalid ({type(e).__name__}); fix dataplay.toml and restart.",
            )
            return False

    def _read_installed_manifest(self, module) -> tuple[dict | None, str | None]:
        """Return only this installed package's manifest, explicitly distinguishing absence."""
        package = getattr(module, "__package__", None) or getattr(module, "__name__", None)
        if not package:
            return None, None
        try:
            resource = importlib.resources.files(package).joinpath("dataplay.toml")
            if not resource.is_file():
                return None, None
            import tomllib
            man = tomllib.loads(resource.read_text(encoding="utf-8"))
        except Exception:
            return None, "Installed plugin manifest is invalid; fix dataplay.toml and restart."
        missing = [key for key in ("name", "version") if key not in man]
        if missing:
            return None, f"Manifest is missing required fields: {', '.join(missing)}."
        err = _core_api_error(man.get("min_core_api"))
        if err:
            return None, err
        man["config"] = _normalize_config(man.get("config"))
        config_error = _validate_workload_env_config(man["config"])
        if config_error:
            return None, config_error
        return man, None

    def _register_module(self, mod: str, reg: Registry, *, source: str = "module") -> None:
        manifest = self._manifests.get(mod, {})
        entry = self._new_plugin_status(
            mod,
            source,
            package=str(manifest.get("name") or mod),
            version=str(manifest["version"]) if manifest.get("version") is not None else None,
            config=manifest.get("config") or None,
        )
        try:
            m = importlib.import_module(mod)
            # a DP_PLUGINS module (pip package, no dataplay.toml) declares compat via a module attribute;
            # gate it through the same range check so it can't register against an unsupported core (OSS-01).
            # Harmless no-op for a drop-in pack (already manifest-gated; sets no such attr).
            err = _core_api_error(getattr(m, "MIN_CORE_API", getattr(m, "min_core_api", None)))
            if err:
                self._record_plugin_problem(entry, err)
                return
            reg._pack = mod  # so reg.config() resolves plugin.<mod>.<key> for THIS pack
            reg._entry = entry
            if hasattr(m, "register"):
                m.register(reg)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[deps] failed to load plugin '{mod}': {e}")
            traceback.print_exc()
            self._record_plugin_problem(
                entry,
                f"Plugin import or registration failed ({type(e).__name__}); check compatibility, configuration, and server logs.",
            )
        finally:
            reg._pack = None
            reg._entry = None

    def _place(self, requires):
        """First (backend_name, worker_id) across the registered backends that satisfies `requires`,
        or None → the default in-process backend. Used by the placement planner / RunController."""
        for r in self.runners:
            if hasattr(r, "place"):
                w = r.place(requires)
                if w:
                    return (r.name, w)
        return None

    def _backends(self) -> list[BackendInfo]:
        """Real backend/worker topology + capacities. A backend that advertises workers() (a pod/Ray
        pool — Phase C) reports them; the built-in local runners don't, so each shows one local slot
        whose capacity is the host. This is the honest data behind the Compute view."""
        cap = _host_capacity()
        out: list[BackendInfo] = []
        for r in self.runners:
            workers = None
            if hasattr(r, "workers"):
                try:
                    workers = list(r.workers())
                except Exception:  # noqa: BLE001
                    workers = None
            out.append(BackendInfo(name=r.name, workers=workers if workers is not None
                                   else [WorkerInfo(id=f"{r.name}:local", capacity=cap)]))
        return out

    def _execution_targets(self) -> list[ExecutionTargetInfo]:
        """Human presentation for the runners that are actually registered in this process."""
        from hub.kernel_backend import KernelBackend

        out: list[ExecutionTargetInfo] = []
        for runner in self.runners:
            name = str(getattr(runner, "name", "") or "")
            if not name:
                continue
            custom = getattr(runner, "execution_target_info", None)
            if callable(custom):
                try:
                    info = ExecutionTargetInfo.model_validate(custom())
                    if info.name == name:
                        out.append(info)
                        continue
                except Exception:  # noqa: BLE001 — a bad optional label must not hide a working runner
                    pass
            if isinstance(runner, KernelBackend):
                substrate = str(getattr(getattr(runner, "spawner", None), "name", "configured"))
                if substrate == "pod":
                    description = "Reusable worker for this Canvas in a configured Kubernetes pod."
                elif substrate == "local-process":
                    description = "Reusable worker for this Canvas on this machine."
                else:
                    description = "Reusable worker for this Canvas on the configured compute substrate."
                out.append(ExecutionTargetInfo(
                    name=name, label="Canvas worker", kind="interactive",
                    description=description, substrate=substrate,
                ))
            elif name == "local-out-of-core":
                out.append(ExecutionTargetInfo(
                    name=name, label="This machine", kind="job",
                    description="Run here with streaming and disk spill for data larger than memory.",
                    substrate="local",
                ))
            elif name == "local-subprocess":
                out.append(ExecutionTargetInfo(
                    name=name, label="Isolated process", kind="job",
                    description="Run once in a separate local process that can be cancelled independently.",
                    substrate="local",
                ))
            elif name == "local-pool":
                out.append(ExecutionTargetInfo(
                    name=name, label="Local worker pool", kind="job",
                    description="Run on one of the configured local worker slots.", substrate="local-pool",
                ))
            elif name == "ray-data":
                remote = bool(str(getattr(runner, "jobs_address", "") or "").strip())
                out.append(ExecutionTargetInfo(
                    name=name,
                    label="Ray Jobs" if remote else "Ray Data on this machine",
                    kind="job",
                    description=(
                        "Submit a durable whole-graph job to the configured Ray cluster."
                        if remote else
                        "Use Ray Data locally; no remote Ray Jobs endpoint is configured."
                    ),
                    substrate="ray-jobs" if remote else "local-ray",
                ))
            else:
                label = name.replace("_", " ").replace("-", " ").strip().title()
                out.append(ExecutionTargetInfo(
                    name=name, label=label or "Configured runner", kind="job",
                    description="Run through a compute provider configured for this workspace.",
                    substrate="plugin",
                ))
        return out

    def info(self) -> KernelInfo:
        from hub.plugins.catalog import InMemoryCatalog
        from hub.storage import LocalStorage, ObjectStorage

        # These mutations are implemented by the bundled metadata store, not by the generic catalog
        # SPI. A subclass may reuse read behavior for an external provider, but must not inherit a
        # capability that would route deletes or atomic edits into the local metadata database.
        built_in_catalog = type(self.catalog) is InMemoryCatalog
        result_storage_kind = (
            "local" if isinstance(self.storage, LocalStorage)
            else "object" if isinstance(self.storage, ObjectStorage)
            else "plugin"
        )
        result_storage_label = {
            "local": "Local workspace",
            "object": "Shared object storage",
            "plugin": "Plugin-managed storage",
        }[result_storage_kind]
        return KernelInfo(
            mode="local", backend="duckdb+polars+arrow", warm=True,
            adapters=[a.name for a in self.adapters],
            runners=[r.name for r in self.runners],
            processors=[p.id for p in self.registry.list()],
            capabilities=[c.id for c in self.capabilities]
            + (["catalog.folder_mutation"] if getattr(self.catalog, "folders_mutable", False) else [])
            + (["catalog.atomic_metadata_edit"] if built_in_catalog else [])
            + (["catalog.cas_unregister"] if built_in_catalog else []),
            capability_views=[CapabilityView(id=c.id, label=getattr(c, "label", c.id), viewer=getattr(c, "viewer"))
                              for c in self.capabilities if isinstance(getattr(c, "viewer", None), dict)],
            backends=self._backends(),
            execution_targets=self._execution_targets(),
            result_storage=ResultStorageInfo(
                label=result_storage_label, kind=result_storage_kind),
        )


_deps: Deps | None = None
_deps_lock = __import__("threading").Lock()


def _note_unhandled_backend_jobs(deps: Deps) -> None:
    """Run the shared-run diagnostic only in the global control-plane composition root.

    Kernel and one-shot driver ``Deps`` instances can point at private metadata or represent a single
    canvas. They must never diagnose or mutate ownership of unrelated shared backend runs.
    """
    from hub import metadb
    metadb.note_unhandled_backend_jobs({
        str(r.durable_backend) for r in deps.runners if getattr(r, "durable_backend", None)
    })


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        with _deps_lock:  # double-checked: concurrent first requests must not build Deps twice
            if _deps is None:
                _deps = Deps(settings.workspace, settings.data_dir)
                _note_unhandled_backend_jobs(_deps)
    return _deps


def set_workspace(
        workspace: str, data_dir: str | None = None, *, maintain_storage: bool = True) -> Deps:
    global _deps
    _deps = Deps(
        workspace, data_dir or os.path.join(workspace, "data"),
        maintain_storage=maintain_storage)
    return _deps
