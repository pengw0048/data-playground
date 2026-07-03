"""Plugin SPI — the extensibility contract (PRD §7).

The kernel core depends only on these Protocols. Every concrete backend (a format,
an engine, a capability, a processor, a catalog) is a plugin implementing one of them.
The default bundle (DuckDB adapter, in-memory catalog, local runner, mock processors)
lives alongside; org bundles are additive and loaded by configuration.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from kernel.models import (
    CatalogTable,
    ColumnSchema,
    CompilePlan,
    LineageResult,
    PipelineImport,
    ProcessorDescriptor,
    RunEstimate,
    RunStatus,
    SampleResult,
)


@runtime_checkable
class DatasetAdapter(Protocol):
    """What a `dataset` can be — a file format, a table format, a warehouse table."""

    name: str

    def matches(self, uri: str) -> bool: ...
    def schema(self, uri: str) -> list[ColumnSchema]: ...
    def sample(self, uri: str, k: int, columns: list[str] | None) -> SampleResult: ...
    def count(self, uri: str) -> int | None: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    """predicate(schema) -> bool + the viewer tabs / actions it adds. Never changes ports."""

    id: str
    label: str

    def predicate(self, columns: list[ColumnSchema]) -> bool: ...
    def columns(self, columns: list[ColumnSchema]) -> list[str]: ...


@runtime_checkable
class Processor(Protocol):
    """A reusable operator with a declared I/O signature (the library-form node)."""

    id: str
    version: str
    mode: str
    input_columns: list[str]
    output_schema: list[ColumnSchema]

    def descriptor(self) -> ProcessorDescriptor: ...
    def build(self, params: dict) -> Callable[[dict], Any]: ...


@runtime_checkable
class ProcessorRegistry(Protocol):
    def register(self, p: Processor) -> None: ...
    def list(self) -> list[ProcessorDescriptor]: ...
    def get(self, pid: str) -> Processor: ...


@runtime_checkable
class PipelineImporter(Protocol):
    """import = run the factory, then introspect (PRD §7.5)."""

    def import_pipeline(self, config: str, params: dict | None) -> PipelineImport: ...


@runtime_checkable
class CatalogProvider(Protocol):
    def list_tables(self, q: str | None) -> list[CatalogTable]: ...
    def get_table(self, id_or_name: str) -> CatalogTable: ...
    def lineage(self, uri: str) -> LineageResult: ...
    def register(self, table: CatalogTable, parents: list[str] | None = None,
                 pipeline: str | None = None) -> None: ...


@runtime_checkable
class Runner(Protocol):
    """Where/how a graph executes. Declares what it can run + an estimate."""

    name: str

    def can_run(self, plan: CompilePlan) -> bool: ...
    def estimate(self, plan: CompilePlan, rows: int) -> RunEstimate: ...
    def run(self, plan: CompilePlan, graph: Any) -> RunStatus: ...
    def status(self, run_id: str) -> RunStatus: ...
    def cancel(self, run_id: str) -> RunStatus: ...
