"""Small mechanical guards for public documentation inventories."""

import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]


def test_plugin_guide_inventory_matches_reference_plugins():
    """Keep the public reference-plugin inventory honest as examples are added or removed."""
    packages = sorted(path.name for path in (_ROOT / "examples" / "plugins").iterdir() if path.is_dir())
    guide = (_ROOT / "docs" / "PLUGINS.md").read_text(encoding="utf-8")

    documented = sorted(set(re.findall(r"\.\./examples/plugins/([^/]+)/", guide)))
    assert documented == packages
