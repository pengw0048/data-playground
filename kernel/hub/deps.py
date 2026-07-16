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
import logging
import os
import sys

from hub.backends import CatalogProvider, NodeBuilder
from hub.models import BackendInfo, CapabilityView, KernelInfo, ResourceSpec, WorkerInfo
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec
from hub.plugins.adapters import DuckDBAdapter, default_adapters
from hub.plugins.capabilities import BUILTIN_CAPABILITIES
from hub.plugins.processors import InMemoryProcessorRegistry
from hub.plugins.runner import LocalRunner
from hub.settings import settings

# Version of the plugin SPI this core exposes. A plugin's dataplay.toml may declare `min_core_api`
# (an int); a pack requiring a newer core than this is skipped at load with a clear error instead of
# being registered and crashing later. Bump when a breaking SPI change lands.
CORE_API_VERSION = 1
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

    def config(self, key: str, default=None):
        """Read a config value for the CURRENTLY-registering pack. Precedence: a UI-set value (metadb
        setting `plugin.<pack>.<key>`) > the field's declared `env` var > its declared `default` > the
        `default` arg. Fields are declared in the pack's dataplay.toml `[[config]]`. Call this inside
        register() to configure the pack; a value changed in the UI takes effect on the next kernel
        start (plugins register once at startup — same as the env vars it falls back to).

        When the field is ``secret``, the stored setting is a secret reference (``env:…`` / ``file:…``)
        and is resolved here; the material value never lives in the settings row.
        """
        pack = self._pack
        schema = self.deps._manifests.get(pack, {}).get("config", []) if pack else []
        field = next((f for f in schema if isinstance(f, dict) and f.get("key") == key), None)
        secret = bool(field and field.get("secret"))
        if pack:
            from hub import metadb
            v = metadb.get_setting(f"plugin.{pack}.{key}", "global", default=None)
            if v not in (None, ""):
                if secret:
                    from hub.secrets import resolve_secret_value
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
        register_resolver(scheme, resolver)

    def add_node(self, spec: NodeSpec, build: "NodeBuilder | None" = None, ir=None) -> None:
        # `build` is the node's build callable — see hub.backends.NodeBuilder for its exact
        # signature/return contract (called by the engine as build(engine, node, inputs)).
        # `ir` is an OPTIONAL engine-neutral emit hook: ir(node) -> {"op", "config"} | None. When given,
        # the node lowers to that IR op (e.g. a clean `map` with inlined `code`) instead of `opaque:<kind>`,
        # so a distributed backend (dp_ray) can run it — NOT just DuckDB. The plugin guarantees its build()
        # and its ir op compute the same thing (like the built-in transform shares its operator).
        # refuse to shadow a built-in OR an already-registered plugin kind — overwriting would
        # corrupt the /api/nodes contract and leave the original's build() as dead code
        if spec.kind in self.deps.builtin_kinds:
            print(f"[deps] plugin node '{spec.kind}' collides with a built-in kind — refused")
            return
        if spec.kind in self.deps.node_specs:
            print(f"[deps] plugin node '{spec.kind}' already registered by another plugin — refused")
            return
        self.deps.node_specs[spec.kind] = spec
        if build is not None:
            self.deps.node_builders[spec.kind] = build
        if ir is not None:
            self.deps.node_ir[spec.kind] = ir

    def add_telemetry_sink(self, sink) -> None:
        """Register a callback invoked once per FINISHED run with a normalized telemetry record (a dict:
        canvas_id/run_id/request_id/job_type/status/rows/ms/error/outputs/placement/per_node). Core ships no
        exporter — an OTel/StatsD/log sink is a plugin. Delivery uses a finite per-sink queue; callback
        failures and overload are logged and never fail a run. See add_metric_sink / add_audit_sink."""
        if callable(sink):
            from hub.observability import register_sink_delivery
            self.deps.telemetry_sinks.append(register_sink_delivery(sink, kind="telemetry"))

    def add_metric_sink(self, sink) -> None:
        """Register a MetricEvent consumer (OPS-01). See docs/OBSERVABILITY.md. Isolation matches
        add_telemetry_sink — delivery never waits on plugin I/O in a request or run path."""
        from hub.observability import add_metric_sink
        add_metric_sink(sink)

    def add_audit_sink(self, sink) -> None:
        """Register an AuditEvent consumer (OPS-01). See docs/OBSERVABILITY.md."""
        from hub.observability import add_audit_sink
        add_audit_sink(sink)

    def add_adapter(self, adapter) -> None:
        self.deps.adapters.insert(0, adapter)  # plugins claim uris before defaults

    def add_runner(self, runner) -> None:
        # runner should satisfy hub.backends.ExecutionBackend; inserted first so it wins pick_runner
        self.deps.runners.insert(0, runner)

    def add_capability(self, cap) -> None:
        self.deps.capabilities.append(cap)
        detect = getattr(cap, "detect", None)  # optional column detector → tag_columns applies it (no core edit)
        if callable(detect):
            from hub.plugins import capabilities as caps
            caps.register_detector(getattr(cap, "id", ""), detect)

    def add_processor(self, proc) -> None:
        self.deps.registry.register(proc)

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

    def set_managed_object_provider(self, provider) -> None:
        """Install the proof-capable exact-object lifecycle provider for managed storage."""
        from hub.handoff import set_runtime_managed_object_provider
        self.deps.managed_object_provider = provider
        set_runtime_managed_object_provider(provider)

    def add_embedder(self, fn, model: str = "custom") -> None:
        """Register a text embedder — `fn(list[str]) -> list[list[float]]` — to power the catalog's
        semantic + hybrid search over dataset name/description/columns. Core ships NONE (an embedding
        model is a heavy, opinionated dependency); a plugin provides one (see examples/plugins/
        dp_semantic_catalog). The catalog reindexes existing entries best-effort in the background. A
        catalog provider that doesn't support embedding simply ignores this."""
        setter = getattr(self.deps.catalog, "set_embedder", None)
        if callable(setter):
            setter(fn, model)

    def set_importer(self, importer) -> None:
        # a pipeline importer (§5.6/§7.5). Without one, deps.importer stays the NullImporter → the
        # /pipelines/import endpoint reports 'not configured' (501), not a broken 500.
        self.deps.importer = importer

    def add_destination(self, backend) -> None:
        # a save/open-dialog "place" backend (a storage/warehouse browser+writer). Should satisfy
        # hub.destinations.DestinationBackend (kind + browse + target_uri); claims its `kind` so a
        # target uri of that scheme can be browsed/picked. The built-in local/s3/gs go through the
        # same registry — this seam just lets register(reg) add one instead of a module-level call.
        from hub import destinations
        destinations.register_backend(backend)


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
                      job_type=status.job_type, status=status.status,
                      rows=status.total_rows, ms=status.ms, error=status.error,
                      outputs=[output.model_dump() for output in status.outputs], per_node=per_node,
                      run_id=status.run_id, request_id=request_id)
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
        publish_region=status.status in ("done", "failed"))


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


def _normalize_config(raw) -> list[dict]:
    """dataplay.toml `[[config]]` → a clean list of UI fields. Keeps only entries with a non-empty string
    `key`; fills `type` (default 'string'; unknown → 'string') and `label` (default = key); passes through
    default/env/secret/options/help/placeholder. Malformed entries are dropped (never fatal)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        if not isinstance(f, dict) or not isinstance(f.get("key"), str) or not f["key"]:
            continue
        field = {"key": f["key"], "type": f.get("type") if f.get("type") in _CONFIG_TYPES else "string",
                 "label": str(f.get("label") or f["key"])}
        for k in ("default", "env", "secret", "options", "help", "placeholder"):
            if k in f:
                field[k] = f[k]
        out.append(field)
    return out


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
        self.registry = InMemoryProcessorRegistry()
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
        self.plugins: list[dict] = []
        self._manifests: dict[str, dict] = {}
        # Plugins register before services are constructed.  Keep the collection available now so a
        # plugin backend can register itself, then append the built-ins after they bind the final catalog.
        self.runners: list = []
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
            self.storage.recover_orphans()
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
        # Whole-dataset profiles are inspection jobs, not materialized graph runs, but they share
        # the same durable RunState status/cancel/recovery contract.
        from hub.profile_jobs import ProfileProcessRunner
        self.profile_runner = ProfileProcessRunner(
            workspace, data_dir, storage=self.storage, node_specs=self.node_specs)
        self.profile_runner.on_complete = _on_complete
        self.profile_runner.on_status = _persist_run_state
        from hub.subprocess_runner import SubprocessRunner
        # a second, real backend: run jobs in an isolated OS process (Settings → Execution). Selected
        # by name via pick_runner; pod/Ray runners install as plugins over the same protocol.
        sub = SubprocessRunner(
            workspace, data_dir, catalog=self.catalog, storage=self.storage,
            resolve_adapter=self.resolve_adapter, node_builders=self.node_builders,
            node_specs=self.node_specs)
        sub.on_complete = _on_complete  # record cancelled/crashed isolated runs the child couldn't
        sub.on_status = _persist_run_state
        sub.result_put = _result_put
        self.runners.extend([self.runner, sub])
        # opt-in reference multi-worker pool (DP_POOL_WORKERS): capability-based placement without a
        # cluster — pods are processes with configured capacities. Shows in the Compute view + is
        # selectable/placeable. Absent → default behavior unchanged. (k8s/Ray = plugins over the same API.)
        from hub.pool_runner import PoolRunner, pool_workers_from_env
        pool_cfg = pool_workers_from_env()
        if pool_cfg:
            pool = PoolRunner(
                workspace, data_dir, pool_cfg, node_specs=self.node_specs, catalog=self.catalog,
                storage=self.storage, resolve_adapter=self.resolve_adapter,
                node_builders=self.node_builders)
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

    def resolve_adapter(self, uri: str):
        for a in self.adapters:
            try:
                if a.matches(uri):
                    return a
            except Exception:  # noqa: BLE001
                continue
        return self.default_adapter

    def chosen_backend(self, uid: str | None = None) -> str:
        """The selected execution backend NAME: per-user preference > workspace default > DP_EXECUTION >
        the default (the per-canvas KERNEL). Kernel-only: with no explicit choice, execution runs on the
        canvas's kernel — process isolation (a runaway transform only wedges that canvas, restartably) +
        durability (survives a hub restart) + warm reuse. Also drives preview/profile routing."""
        from hub import metadb
        chosen = (metadb.get_setting("backend", "user", uid, default="") if uid else "") or ""
        if not chosen:
            chosen = metadb.get_setting("backend", "global", default="") or ""
        if not chosen:
            chosen = settings.execution or "kernel"   # DP_EXECUTION overrides; else the kernel is default
        return chosen

    def kernel_backend(self):
        """The registered per-canvas KernelBackend (for preview/profile routing), or None."""
        from hub.kernel_backend import KernelBackend
        return next((r for r in self.runners if isinstance(r, KernelBackend)), None)

    def pick_runner(self, plan, uid: str | None = None):
        # honor the chosen backend (Settings → Execution) when it's registered and can run this plan;
        # otherwise the first runner that can, else the default.
        chosen = self.chosen_backend(uid)
        if chosen and chosen not in {getattr(r, "name", None) for r in self.runners}:
            chosen = "kernel"  # a stale / uninstalled-plugin selection → the kernel DEFAULT, not the
            #                    generic first-capable runner (which silently was local-out-of-core)
        if chosen:
            for r in self.runners:
                if getattr(r, "name", None) == chosen and r.can_run(plan):
                    return r
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
        try:
            default_catalog.register(reg)
            self.plugins.append({"name": "default-catalog", "source": "builtin"})
        finally:
            reg._pack = None

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
                        self._register_module(name, reg)
        # 2) configured modules (DP_PLUGINS) + installed entry points
        for mod in settings.plugin_modules:
            self._register_module(mod, reg)
        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group="dataplay.plugins"):
                try:
                    fn = ep.load()
                    mod = sys.modules.get(getattr(fn, "__module__", "") or "")
                    err = _core_api_error(getattr(mod, "MIN_CORE_API", getattr(mod, "min_core_api", None))) if mod else None
                    if err:  # entry-point plugin declares an unsupported core → skip before register (OSS-01)
                        self.plugins.append({"name": ep.name, "source": "entry_point", "error": err})
                        continue
                    reg._pack = ep.name
                    fn(reg)
                    self.plugins.append({"name": ep.name, "source": "entry_point"})
                except Exception as e:  # noqa: BLE001
                    print(f"[deps] entry-point plugin '{ep.name}' failed: {e}")
                finally:
                    reg._pack = None
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
                self.plugins.append({"name": name, "source": "drop-in", "error": f"dataplay.toml missing: {', '.join(missing)}"})
                return False
            err = _core_api_error(man.get("min_core_api"))
            if err:
                self.plugins.append({"name": name, "source": "drop-in", "error": err})
                return False
            man["config"] = _normalize_config(man.get("config"))  # [[config]] → clean UI-field list (may be [])
            self._manifests[name] = man
            return True
        except Exception as e:  # noqa: BLE001
            self.plugins.append({"name": name, "source": "drop-in", "error": f"bad dataplay.toml: {e}"})
            return False

    def _register_module(self, mod: str, reg: Registry) -> None:
        try:
            m = importlib.import_module(mod)
            # a DP_PLUGINS module (pip package, no dataplay.toml) declares compat via a module attribute;
            # gate it through the same range check so it can't register against an unsupported core (OSS-01).
            # Harmless no-op for a drop-in pack (already manifest-gated; sets no such attr).
            err = _core_api_error(getattr(m, "MIN_CORE_API", getattr(m, "min_core_api", None)))
            if err:
                self.plugins.append({"name": mod, "source": "module", "error": err})
                return
            reg._pack = mod  # so reg.config() resolves plugin.<mod>.<key> for THIS pack
            if hasattr(m, "register"):
                m.register(reg)
            entry = {"name": mod, "source": "module", **({"version": self._manifests.get(mod, {}).get("version")} if mod in self._manifests else {})}
            schema = self._manifests.get(mod, {}).get("config")  # dataplay.toml [[config]] → UI-configurable fields
            if schema:
                entry["config"] = schema
            self.plugins.append(entry)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[deps] failed to load plugin '{mod}': {e}")
            self.plugins.append({"name": mod, "source": "module", "error": f"{type(e).__name__}: {e}",
                                 "traceback": traceback.format_exc().splitlines()[-3:]})
        finally:
            reg._pack = None

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

    def info(self) -> KernelInfo:
        return KernelInfo(
            mode="local", backend="duckdb+polars+arrow", warm=True,
            adapters=[a.name for a in self.adapters],
            runners=[r.name for r in self.runners],
            processors=[p.id for p in self.registry.list()],
            capabilities=[c.id for c in self.capabilities]
            + (["catalog.folder_mutation"] if getattr(self.catalog, "folders_mutable", False) else []),
            capability_views=[CapabilityView(id=c.id, label=getattr(c, "label", c.id), viewer=getattr(c, "viewer"))
                              for c in self.capabilities if isinstance(getattr(c, "viewer", None), dict)],
            backends=self._backends(),
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
