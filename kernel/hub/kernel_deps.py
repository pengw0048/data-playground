"""Per-canvas Python dependencies for a kernel — the canvas declares `requirements` (pip specs, e.g.
["pandas", "scikit-learn==1.5"]); the kernel installs them into a per-canvas target dir on sys.path
(like a notebook kernel's packages) and returns the top-level module names so the sandbox can allow
importing them. Idempotent + cached per target dir. This is why the deps travel WITH the canvas: open
it anywhere → its kernel installs what it needs.

Trust note: installing pip packages is arbitrary code + network egress. A per-canvas kernel already
runs the user's arbitrary Python, so this is consistent for a trusted (local) or single-tenant kernel;
a locked-down deployment should disable it or pre-bake an image (a deployment concern, like the rest of
the soft sandbox).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path

_lock = threading.Lock()
_installed: dict[str, frozenset[str]] = {}  # target dir -> the requirements set already installed there
INSTALL_TIMEOUT_S = 600.0


def deps_dir(workspace: str, canvas_id: str) -> str:
    """A stable per-canvas dependency directory under the workspace."""
    h = hashlib.sha1(canvas_id.encode()).hexdigest()[:16]
    return str(Path(workspace) / "kernel_deps" / h)


def _top_level_modules(target: Path) -> set[str]:
    """Importable top-level names present in a --target dir (packages + top-level modules)."""
    if not target.exists():
        return set()
    mods: set[str] = set()
    for p in target.iterdir():
        n = p.name
        if n.startswith(("_", ".")) or n.endswith((".dist-info", ".data", ".egg-info")):
            continue
        if p.is_dir():
            mods.add(n)
        elif p.suffix == ".py":
            mods.add(p.stem)
    return mods


def ensure(reqs: list[str], target_dir: str) -> set[str]:
    """Install `reqs` into target_dir (idempotent — skipped if the same set is already there), put the
    dir on sys.path, and return the top-level importable module names for the sandbox to allow. A failed
    install is CACHED against this reqset (so a permanently-bad requirement isn't re-run on every
    run/preview/profile — that could hang up to INSTALL_TIMEOUT_S each time) and logged to stderr (not
    swallowed silently); editing the requirements changes the reqset and re-triggers the install."""
    target = Path(target_dir)
    reqset = frozenset(reqs or [])
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))  # user deps take precedence, notebook-style
    with _lock:
        if reqs and (_installed.get(target_dir) != reqset or not target.exists()):
            target.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(target),
                                "--no-input", "--disable-pip-version-check", *reqs],
                               check=True, capture_output=True, timeout=INSTALL_TIMEOUT_S)
                _installed[target_dir] = reqset
                import importlib
                importlib.invalidate_caches()  # so a freshly-installed package is importable now
            except Exception as e:  # noqa: BLE001
                _installed[target_dir] = reqset  # cache the attempt — don't re-run pip on every call
                err = getattr(e, "stderr", None)
                if isinstance(err, bytes):
                    err = err.decode("utf-8", "replace")
                sys.stderr.write(f"[kernel_deps] pip install failed for {list(reqs)}: {err or e}\n")
        return _top_level_modules(target)
