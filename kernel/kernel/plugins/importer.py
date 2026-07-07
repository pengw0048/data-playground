"""Pipeline importer — a bundle extension point.

`import = run the factory` is inherently backend-specific (there is no generic pipeline
format), so the generic core ships no importer. The SPI and the /pipelines/import endpoint
exist; an org bundle registers a real importer (e.g. one that runs load_pipeline(config,
params) and introspects its stages). Until then, import reports that it is not configured
rather than faking a decomposition.
"""

from __future__ import annotations

from kernel.models import PipelineImport


class ImporterNotConfigured(Exception):
    pass


class NullImporter:
    name = "none"

    def import_pipeline(self, config: str, params: dict | None) -> PipelineImport:
        raise ImporterNotConfigured(
            "No pipeline importer is registered in this bundle. Import is a plugin "
            "capability — the generic core ships none. Use an ad-hoc transform "
            "cell or register an importer plugin."
        )
