"""Ad-hoc cell sandbox — compiles a user operator body to a callable.

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
        "IndexError", "Exception", "repr", "ord", "chr", "hex", "bin",
    )  # NOTE: no "format" — str.format("{0.__class__...}") reaches dunders through a format field
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


def _reject_dunder(code: str) -> None:
    """Block the classic `().__class__.__mro__[-1].__subclasses__()` sandbox escape.

    This is a *soft* guard, not a real boundary: it rejects access to dunder attributes/names at
    the AST level so a cell can't walk up to `object` and reach os/subprocess. Untrusted input
    still needs OS-level isolation (a subprocess sandbox); this only closes the obvious hole.
    """
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return  # the compile() below will surface the syntax error
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            raise SandboxError(f"access to '{node.attr}' is not allowed in an ad-hoc cell")
        # dunders reached through a STRING instead of an attribute node — `"{0.__class__.__mro__...}"
        # .format(x)` or `getattr(x, "__class__")`. A data-transform cell has no need for '__' in a
        # string literal, so reject it (closes the format-field / getattr escape in this soft guard).
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            raise SandboxError("string literals containing '__' are not allowed in an ad-hoc cell")
        if isinstance(node, ast.Name) and node.id.startswith("__") and node.id.endswith("__"):
            raise SandboxError(f"access to '{node.id}' is not allowed in an ad-hoc cell")


def compile_operator(code: str, mode: str) -> Callable:
    """Compile a cell body into an operator callable for the given mode."""
    _reject_dunder(code)
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


def run_with_timeout(fn: Callable[[], Any], seconds: float, on_timeout: Callable[[], None] | None = None) -> Any:
    """Run fn() in a worker thread, enforcing a wall-clock budget. On timeout, `on_timeout` (e.g.
    db.interrupt) is fired to abort in-flight work so the worker can unwind and release any lock it
    holds; we then give it a brief grace to do so before raising."""
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
        if on_timeout is not None:
            try:
                on_timeout()  # abort the stuck query so the worker releases the process-global lock
            except Exception:  # noqa: BLE001
                pass
            t.join(2.0)  # grace for the interrupted worker to unwind its finally (lock release)
        raise SandboxError(f"cell exceeded the {seconds:g}s time budget")
    if error:
        raise error[0]
    return result[0] if result else None


class SandboxError(Exception):
    pass
