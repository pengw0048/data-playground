"""Package-version identity shared by runtime surfaces."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version


# This fallback identifies an uninstalled source checkout. Packaged builds always read their exact
# version from wheel metadata, so release closeout only changes the release-owned manifests.
DEVELOPMENT_VERSION = "0.3.0.dev0"


def current_version() -> str:
    try:
        return package_version("data-playground")
    except PackageNotFoundError:
        return DEVELOPMENT_VERSION
