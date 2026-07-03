"""Processor registry (PRD §7.4) — a plugin extension point.

The generic core ships with an EMPTY registry. Processors are registered by:
  1. a user promoting an ad-hoc transform cell to the library ("Promote to library"), or
  2. an org plugin bundle registering domain processors (e.g. a Qwen-VL captioner) —
     which lives OUTSIDE the core and loads by configuration.

The core hardcodes no domain operators. A registered processor carries a declared I/O
signature + a mode; if it is code-backed (a promoted cell) its body compiles through the
same sandbox the ad-hoc form uses, so both forms run identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from kernel import sandbox
from kernel.models import (
    PREVIEWABLE_MODES,
    ColumnSchema,
    ProcessorDescriptor,
)


@dataclass
class RegisteredProcessor:
    """A library-form processor. Either code-backed (promoted cell) or plugin-supplied."""

    id: str
    title: str
    mode: str
    version: str = "v1"
    category: str = "compute"
    input_columns: list[str] = field(default_factory=list)
    output_schema: list[ColumnSchema] = field(default_factory=list)
    params_schema: dict[str, Any] = field(default_factory=dict)
    blurb: str = ""
    code: str | None = None                       # code-backed (promoted / inline)
    fn_factory: Callable[[dict], Callable] | None = None  # plugin-supplied operator

    @property
    def previewable(self) -> bool:
        return self.mode in PREVIEWABLE_MODES

    def descriptor(self) -> ProcessorDescriptor:
        return ProcessorDescriptor(
            id=self.id, version=self.version, title=self.title, mode=self.mode,
            category=self.category, input_columns=self.input_columns,
            output_schema=self.output_schema, params_schema=self.params_schema,
            previewable=self.previewable, blurb=self.blurb,
        )

    def build(self, params: dict) -> Callable:
        if self.fn_factory is not None:
            return self.fn_factory(params or {})
        if self.code is not None:
            return sandbox.compile_operator(self.code, self.mode)
        raise ValueError(f"processor {self.id} has neither code nor a factory")


class InMemoryProcessorRegistry:
    """Empty by default — an extension point, never a source of domain operators."""

    def __init__(self) -> None:
        self._procs: dict[str, RegisteredProcessor] = {}

    def register(self, p: RegisteredProcessor) -> None:
        self._procs[p.id] = p

    def promote(self, *, id: str, title: str, mode: str, code: str,
                input_columns: list[str], output_schema: list[ColumnSchema],
                blurb: str = "") -> RegisteredProcessor:
        """Register a code-backed processor from an ad-hoc cell (versioned)."""
        version = "v1"
        if id in self._procs:
            prev = self._procs[id].version
            try:
                version = f"v{int(prev.lstrip('v')) + 1}"
            except ValueError:
                version = "v2"
        p = RegisteredProcessor(
            id=id, title=title, mode=mode, version=version, code=code,
            input_columns=input_columns, output_schema=output_schema, blurb=blurb,
        )
        self.register(p)
        return p

    def list(self) -> list[ProcessorDescriptor]:
        return [p.descriptor() for p in self._procs.values()]

    def get(self, pid: str) -> RegisteredProcessor:
        return self._procs[pid]

    def has(self, pid: str) -> bool:
        return pid in self._procs
