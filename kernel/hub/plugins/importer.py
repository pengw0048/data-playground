"""Pipeline importer — a bundle extension point.

`import = run the factory` is inherently backend-specific (there is no generic pipeline
format), so the generic core ships no importer. The SPI and the /pipelines/import endpoint
exist; an org bundle registers a real importer (e.g. one that runs load_pipeline(config,
params) and introspects its stages). Until then, import reports that it is not configured
rather than faking a decomposition.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hub.models import PipelineImport


class ImporterNotConfigured(Exception):
    pass


@runtime_checkable
class Importer(Protocol):
    """The pipeline-import SPI. A bundle registers one via `reg.set_importer(...)`. `import_pipeline`
    parses a foreign pipeline `config` (its own format) and returns a `PipelineImport`; populate its
    `graph` with a runnable canvas Graph (nodes/edges of built-in or plugin kinds) to make the import
    land on a canvas and run. Raise `ImporterNotConfigured` if this bundle can't import (the default)."""

    name: str

    def import_pipeline(self, config: str, params: dict | None) -> PipelineImport: ...


class NullImporter:
    name = "none"

    def import_pipeline(self, config: str, params: dict | None) -> PipelineImport:
        raise ImporterNotConfigured(
            "No pipeline importer is registered in this bundle. Import is a plugin "
            "capability — the generic core ships none. Use an ad-hoc transform "
            "cell or register an importer plugin."
        )
