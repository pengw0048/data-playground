"""Ad-hoc cell sandbox — compiles a user operator body to a callable (PRD NFR-4).

The generic escape hatch: the user writes an operator inline (`def fn(row): ...`) and we
run it over a small sample. This is a *soft* sandbox — a curated builtins whitelist, a
module import allow-list, and a wall-clock budget enforced by the caller. It is not a hard
security boundary (CPython `exec` never is); an org deployment should run the kernel with
OS-level isolation. Honest about the limit rather than pretending otherwise.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

# Modules a cell may import (pre-bound in the namespace so `import` is optional).
_ALLOWED_MODULES = {
    "math": __import__("math"),
    "statistics": __import__("statistics"),
    "re": __import__("re"),
    "json": __import__("json"),
    "hashlib": __import__("hashlib"),
    "random": __import__("random"),
    "datetime": __import__("datetime"),
    "itertools": __import__("itertools"),
    "collections": __import__("collections"),
    "string": __import__("string"),
}

_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
        "frozenset", "int", "isinstance", "issubclass", "len", "list", "map", "max",
        "min", "print", "range", "reversed", "round", "set", "sorted", "str", "sum",
        "tuple", "zip", "True", "False", "None", "ValueError", "KeyError", "TypeError",
        "IndexError", "Exception", "repr", "ord", "chr", "hex", "bin", "format",
    )
}

_ENTRY_NAMES = ["fn", "transform", "process", "map", "op"]


def _guarded_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in _ALLOWED_MODULES:
        return __import__(name, *args, **kwargs)
    raise ImportError(f"import of '{name}' is not allowed in an ad-hoc cell")


def _namespace() -> dict:
    builtins = dict(_SAFE_BUILTINS)
    builtins["__import__"] = _guarded_import
    ns: dict[str, Any] = {"__builtins__": builtins}
    ns.update(_ALLOWED_MODULES)
    return ns


def compile_operator(code: str, mode: str) -> Callable:
    """Compile a cell body into an operator callable for the given mode."""
    ns = _namespace()
    try:
        compiled = compile(code, "<adhoc-cell>", "exec")
        exec(compiled, ns)  # noqa: S102 — intentional, sandboxed namespace
    except Exception as e:  # noqa: BLE001
        raise SandboxError(f"cell failed to compile: {type(e).__name__}: {e}") from e

    entry = None
    for nm in _ENTRY_NAMES:
        if callable(ns.get(nm)):
            entry = ns[nm]
            break
    if entry is None:
        funcs = [v for k, v in ns.items() if callable(v) and not k.startswith("_")
                 and k not in _ALLOWED_MODULES]
        if len(funcs) == 1:
            entry = funcs[0]
    if entry is None:
        raise SandboxError(
            "cell must define a function named fn / transform / process (e.g. `def fn(row): ...`)"
        )
    return entry


def run_with_timeout(fn: Callable[[], Any], seconds: float) -> Any:
    """Run fn() in a worker thread, enforcing a wall-clock budget."""
    result: list[Any] = []
    error: list[BaseException] = []

    def target():
        try:
            result.append(fn())
        except BaseException as e:  # noqa: BLE001
            error.append(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise SandboxError(f"cell exceeded the {seconds:g}s time budget")
    if error:
        raise error[0]
    return result[0] if result else None


class SandboxError(Exception):
    pass
