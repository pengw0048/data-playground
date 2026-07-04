"""Composition root (PRD §6, §8.0) — builds the plugin registries at startup.

The core depends only on the SPI. This wires the DEFAULT setup (DuckDB+Lance adapters,
local out-of-core runner, in-memory catalog, media/vector capabilities, node specs). Extra
plugin packs are discovered two ways (§8.0): a drop-in `plugins/<pack>/` folder in the
workspace, and pip-installed packages exposing a `dataplay.plugins` entry point. Each calls
`register(reg)` to add nodes / adapters / runners / capabilities / catalog / planner.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

from kernel.models import KernelInfo
from kernel.nodespecs import BUILTIN_NODE_SPECS, NodeSpec
from kernel.plugins.adapters import DuckDBAdapter, default_adapters
from kernel.plugins.capabilities import BUILTIN_CAPABILITIES
from kernel.plugins.catalog import InMemoryCatalog
from kernel.plugins.processors import InMemoryProcessorRegistry
from kernel.plugins.runner import LocalRunner
from kernel.settings import settings


class Registry:
    """Passed to each plugin pack's register(reg) so it can add things (§8)."""

    def __init__(self, deps: "Deps"):
        self.deps = deps

    def add_node(self, spec: NodeSpec, lower=None) -> None:
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
        self.deps.runners.insert(0, runner)

    def add_capability(self, cap) -> None:
        self.deps.capabilities.append(cap)

    def add_processor(self, proc) -> None:
        self.deps.registry.register(proc)

    def set_catalog(self, catalog) -> None:
        self.deps.catalog = catalog

    def set_planner(self, planner) -> None:
        self.deps.planner = planner


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
        self.planner = None
        self.plugins: list[dict] = []
        self._manifests: dict[str, dict] = {}
        from kernel.storage import make_storage
        self.storage = make_storage(workspace)
        self.catalog = InMemoryCatalog(data_dir, self.resolve_adapter)
        # re-register previously written outputs so committed tables survive a kernel restart
        # (they live in storage, separate from the seeded data_dir).
        for uri in self.storage.list_outputs():
            name = os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
            self.catalog.register_output(name=name, uri=uri, version="v1", parents=[], pipeline="canvas")
        self.runner = LocalRunner(self.resolve_adapter, self.registry, self.catalog, workspace,
                                  node_lowerings=self.node_lowerings, node_specs=self.node_specs,
                                  storage=self.storage)
        self.runners = [self.runner]
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
                    self._read_manifest(pack, name)
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

    def _read_manifest(self, pack_dir: str, name: str) -> None:
        """Read + validate dataplay.toml (name/version required) and record it (§8.0)."""
        path = os.path.join(pack_dir, "dataplay.toml")
        if not os.path.exists(path):
            return
        try:
            import tomllib
            with open(path, "rb") as f:
                man = tomllib.load(f)
            missing = [k for k in ("name", "version") if k not in man]
            if missing:
                self.plugins.append({"name": name, "source": "drop-in", "error": f"dataplay.toml missing: {', '.join(missing)}"})
            else:
                self._manifests[name] = man
        except Exception as e:  # noqa: BLE001
            self.plugins.append({"name": name, "source": "drop-in", "error": f"bad dataplay.toml: {e}"})

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
