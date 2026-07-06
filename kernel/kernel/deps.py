"""Composition root (PRD §6, §8.0) — builds the plugin registries at startup.

The core depends only on the SPI. This wires the DEFAULT setup (DuckDB+Lance adapters,
local out-of-core runner, in-memory catalog, media/vector capabilities, node specs). Extra
plugin packs are discovered two ways (§8.0): a drop-in `plugins/<pack>/` folder in the
workspace, and pip-installed packages exposing a `dataplay.plugins` entry point. Each calls
`register(reg)` to add nodes / adapters / runners / capabilities / catalog.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

from kernel.backends import NodeLowering
from kernel.models import KernelInfo
from kernel.nodespecs import BUILTIN_NODE_SPECS, NodeSpec
from kernel.plugins.adapters import DuckDBAdapter, default_adapters
from kernel.plugins.capabilities import BUILTIN_CAPABILITIES
from kernel.plugins.catalog import InMemoryCatalog
from kernel.plugins.processors import InMemoryProcessorRegistry
from kernel.plugins.runner import LocalRunner
from kernel.settings import settings

# Version of the plugin SPI this core exposes. A plugin's dataplay.toml may declare `min_core_api`
# (an int); a pack requiring a newer core than this is skipped at load with a clear error instead of
# being registered and crashing later. Bump when a breaking SPI change lands.
CORE_API_VERSION = 1


class Registry:
    """Passed to each plugin pack's register(reg) so it can add things (§8)."""

    def __init__(self, deps: "Deps"):
        self.deps = deps

    def add_node(self, spec: NodeSpec, lower: "NodeLowering | None" = None) -> None:
        # `lower` is the node's lowering callable — see kernel.backends.NodeLowering for its exact
        # signature/return contract (called by the engine as lower(engine, node, inputs)).
        # refuse to shadow a built-in OR an already-registered plugin kind — overwriting would
        # corrupt the /api/nodes contract and leave the original's lower() as dead code
        if spec.kind in self.deps.builtin_kinds:
            print(f"[deps] plugin node '{spec.kind}' collides with a built-in kind — refused")
            return
        if spec.kind in self.deps.node_specs:
            print(f"[deps] plugin node '{spec.kind}' already registered by another plugin — refused")
            return
        self.deps.node_specs[spec.kind] = spec
        if lower is not None:
            self.deps.node_lowerings[spec.kind] = lower

    def add_adapter(self, adapter) -> None:
        self.deps.adapters.insert(0, adapter)  # plugins claim uris before defaults

    def add_runner(self, runner) -> None:
        # runner should satisfy kernel.backends.ExecutionBackend; inserted first so it wins pick_runner
        self.deps.runners.insert(0, runner)

    def add_capability(self, cap) -> None:
        self.deps.capabilities.append(cap)

    def add_processor(self, proc) -> None:
        self.deps.registry.register(proc)

    def set_catalog(self, catalog) -> None:
        self.deps.catalog = catalog


def _persist_run(graph, target, status) -> None:
    """Runner on_complete hook: keep a finished run with its canvas (canvas id == graph.id)."""
    from kernel import metadb
    metadb.record_run(canvas_id=getattr(graph, "id", None), target_node_id=target, status=status.status,
                      rows=status.total_rows, ms=status.ms, error=status.error, output_table=status.output_table)


def _persist_run_state(graph, status) -> None:
    """Runner on_status hook: upsert the run's live status to the shared DB on every transition, so
    GET /run/{id} + the status WebSocket are answerable from ANY web instance and survive a restart
    (not just the in-memory dict of the instance that accepted the run)."""
    from kernel import metadb
    metadb.save_run_state(status.run_id, status.model_dump(), canvas_id=getattr(graph, "id", None))


class Deps:
    def __init__(self, workspace: str, data_dir: str):
        self.workspace = workspace
        self.data_dir = data_dir
        self.adapters = default_adapters()
        self.default_adapter = DuckDBAdapter()
        self.registry = InMemoryProcessorRegistry()
        self.capabilities = list(BUILTIN_CAPABILITIES)
        self.node_specs: dict[str, NodeSpec] = {s.kind: s for s in BUILTIN_NODE_SPECS}
        self.builtin_kinds = {s.kind for s in BUILTIN_NODE_SPECS}
        self.node_lowerings: dict[str, object] = {}
        self.plugins: list[dict] = []
        self._manifests: dict[str, dict] = {}
        from kernel.storage import make_storage
        self.storage = make_storage(workspace)
        # The catalog is shared by every user (by design — one workspace, not one kernel per session);
        # per-user boundaries are enforced at the canvas/share/settings layer, not by isolating the data
        # engine. InMemoryCatalog is a per-instance CACHE that write-throughs to + loads from the shared
        # DB (catalog_entries/edges), so multiple stateless web instances stay consistent.
        self.catalog = InMemoryCatalog(data_dir, self.resolve_adapter)
        # re-register previously written outputs so committed tables survive a kernel restart
        # (they live in storage, separate from the seeded data_dir).
        for uri in self.storage.list_outputs():
            name = os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
            self.catalog.register_output(name=name, uri=uri, version="v1", parents=[], pipeline="canvas")
        self.runner = LocalRunner(self.resolve_adapter, self.registry, self.catalog, workspace,
                                  node_lowerings=self.node_lowerings, node_specs=self.node_specs,
                                  storage=self.storage)
        self.runner.on_complete = _persist_run  # keep finished runs with their canvas (run history)
        self.runner.on_status = _persist_run_state  # mirror live status to the DB (stateless-web reads)
        from kernel.subprocess_runner import SubprocessRunner
        # a second, real backend: run jobs in an isolated OS process (Settings → Execution). Selected
        # by name via pick_runner; pod/Ray runners install as plugins over the same protocol.
        sub = SubprocessRunner(workspace, data_dir, catalog=self.catalog)
        sub.on_complete = _persist_run  # record cancelled/crashed isolated runs the child couldn't
        sub.on_status = _persist_run_state
        self.runners = [self.runner, sub]
        self.run_index: dict[str, object] = {}  # run_id -> the runner that owns it
        self._load_plugins()

    def resolve_adapter(self, uri: str):
        for a in self.adapters:
            try:
                if a.matches(uri):
                    return a
            except Exception:  # noqa: BLE001
                continue
        return self.default_adapter

    def pick_runner(self, plan):
        # honor the chosen backend (Settings → Execution) when it's registered and can run this plan;
        # otherwise the first runner that can, else the default. Real once plugins add more runners.
        from kernel import metadb
        chosen = metadb.get_setting("backend", "global", default="") or ""
        if chosen:
            for r in self.runners:
                if getattr(r, "name", None) == chosen and r.can_run(plan):
                    return r
        for r in self.runners:
            if r.can_run(plan):
                return r
        return self.runner

    # -- plugin discovery (§8.0) ------------------------------------------- #
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
                    ep.load()(reg)
                    self.plugins.append({"name": ep.name, "source": "entry_point"})
                except Exception as e:  # noqa: BLE001
                    print(f"[deps] entry-point plugin '{ep.name}' failed: {e}")
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
            min_core = man.get("min_core_api")
            if min_core is not None and int(min_core) > CORE_API_VERSION:
                self.plugins.append({"name": name, "source": "drop-in",
                                     "error": f"requires core API >= {int(min_core)}; this core is {CORE_API_VERSION}"})
                return False
            self._manifests[name] = man
            return True
        except Exception as e:  # noqa: BLE001
            self.plugins.append({"name": name, "source": "drop-in", "error": f"bad dataplay.toml: {e}"})
            return False

    def _register_module(self, mod: str, reg: Registry) -> None:
        try:
            m = importlib.import_module(mod)
            if hasattr(m, "register"):
                m.register(reg)
            entry = {"name": mod, "source": "module", **({"version": self._manifests.get(mod, {}).get("version")} if mod in self._manifests else {})}
            self.plugins.append(entry)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[deps] failed to load plugin '{mod}': {e}")
            self.plugins.append({"name": mod, "source": "module", "error": f"{type(e).__name__}: {e}",
                                 "traceback": traceback.format_exc().splitlines()[-3:]})

    def info(self) -> KernelInfo:
        return KernelInfo(
            mode="local", backend="duckdb+polars+arrow", warm=True,
            adapters=[a.name for a in self.adapters],
            runners=[r.name for r in self.runners],
            processors=[p.id for p in self.registry.list()],
            capabilities=[c.id for c in self.capabilities],
        )


_deps: Deps | None = None
_deps_lock = __import__("threading").Lock()


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        with _deps_lock:  # double-checked: concurrent first requests must not build Deps twice
            if _deps is None:
                _deps = Deps(settings.workspace, settings.data_dir)
    return _deps


def set_workspace(workspace: str, data_dir: str | None = None) -> Deps:
    global _deps
    _deps = Deps(workspace, data_dir or os.path.join(workspace, "data"))
    return _deps
