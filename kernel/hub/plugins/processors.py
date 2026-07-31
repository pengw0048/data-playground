"""Processor registry — a plugin extension point.

The generic core ships with an EMPTY registry. Processors are registered by:
  1. a user promoting an ad-hoc transform cell to the library ("Promote to library"), or
  2. an org plugin bundle registering domain processors (e.g. a vision-language captioner) —
     which lives OUTSIDE the core and loads by configuration.

The core hardcodes no domain operators. A registered processor carries a declared I/O
signature + a mode; if it is code-backed (a promoted cell) its body compiles through the
same sandbox the ad-hoc form uses, so both forms run identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import hashlib
import re
from typing import Any, Callable

from hub import sandbox
from hub.models import (
    PREVIEWABLE_MODES,
    ColumnSchema,
    InstalledProcessorSource,
    ProcessorDescriptor,
)

MAX_INSTALLED_SOURCE_BYTES = 1_000_000
_SOURCE_LANGUAGE = re.compile(r"^[a-z][a-z0-9_+-]{0,31}$")


@dataclass(frozen=True)
class ProcessorSourceProvider:
    """Explicit opt-in for publishing one exact installed source resource."""

    language: str
    read: Callable[[], bytes]


class ProcessorSourceUnavailable(RuntimeError):
    """The opted-in installed resource could not be read safely."""


@dataclass
class RegisteredProcessor:
    """A library-form processor. Either code-backed (promoted cell) or plugin-supplied."""

    id: str
    title: str
    mode: str
    version: str = "v1"
    category: str = "compute"
    input_columns: list[str] = field(default_factory=list)
    input_schema: list[ColumnSchema] = field(default_factory=list)
    output_schema: list[ColumnSchema] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    params_schema: dict[str, Any] = field(default_factory=dict)
    blurb: str = ""
    provenance: str = "plugin"
    creator_id: str | None = None
    created_at: datetime.datetime | None = None
    semantic_digest: str | None = None
    code: str | None = None                       # code-backed (promoted / inline)
    fn_factory: Callable[[dict], Callable] | None = None  # plugin-supplied operator
    source: ProcessorSourceProvider | None = None  # plugin opt-in; never part of descriptors/manifests

    @property
    def previewable(self) -> bool:
        return self.mode in PREVIEWABLE_MODES

    def descriptor(self) -> ProcessorDescriptor:
        return ProcessorDescriptor(
            id=self.id, version=self.version, title=self.title, mode=self.mode,
            category=self.category, input_columns=self.input_columns,
            input_schema=self.input_schema, output_schema=self.output_schema,
            requirements=self.requirements, params_schema=self.params_schema,
            previewable=self.previewable, blurb=self.blurb,
            provenance=self.provenance, creator_id=self.creator_id,
            created_at=self.created_at, semantic_digest=self.semantic_digest,
        )

    def build(self, params: dict) -> Callable:
        if self.fn_factory is not None:
            return self.fn_factory(params or {})
        if self.code is not None:
            return sandbox.compile_operator(self.code, self.mode)
        raise ValueError(f"processor {self.id} has neither code nor a factory")


class ProcessorRegistry:
    """Plugin descriptors plus the separate durable promoted Transform store."""

    def __init__(self) -> None:
        self._procs: dict[str, RegisteredProcessor] = {}

    def register(self, p: RegisteredProcessor) -> None:
        if p.id.startswith("tr_"):
            raise ValueError("plugin processor ids cannot use the reserved promoted Transform namespace")
        if p.source is not None and (
            not _SOURCE_LANGUAGE.fullmatch(p.source.language)
            or not callable(p.source.read)
        ):
            raise ValueError("processor source requires a canonical language and byte reader")
        self._procs[p.id] = p

    def promote(self, *, owner_id: str, id: str, title: str, mode: str, code: str,
                input_schema: list[ColumnSchema], output_schema: list[ColumnSchema],
                requirements: list[str], category: str = "compute",
                blurb: str = "") -> RegisteredProcessor:
        """Persist or idempotently reopen one immutable code-backed version."""
        from hub import metadb

        # Reject invalid code before making an immutable definition durable. Execution still compiles
        # through this same ordinary code-node seam when the exact version is used.
        sandbox.compile_operator(code, mode)
        return self._from_promoted(metadb.promote_transform(
            owner_id=owner_id, key=id, title=title, mode=mode, code=code,
            input_schema=input_schema, output_schema=output_schema,
            requirements=requirements, category=category, blurb=blurb,
        ))

    def list(self, owner_id: str | None = None) -> list[ProcessorDescriptor]:
        result = [p.descriptor() for p in self._procs.values()]
        if owner_id is not None:
            from hub import metadb
            result.extend(
                self._from_promoted(item).descriptor()
                for item in metadb.list_promoted_transforms(owner_id)
            )
        return result

    def get(self, pid: str, version: str | None = None) -> RegisteredProcessor:
        plugin = self._procs.get(pid)
        if plugin is not None:
            if version is not None and plugin.version != version:
                raise KeyError((pid, version))
            return plugin
        if version is None:
            raise KeyError(pid)
        from hub import metadb
        item = metadb.promoted_transform_version(pid, version)
        if item is None:
            raise KeyError((pid, version))
        return self._from_promoted(item)

    def has(self, pid: str, version: str | None = None) -> bool:
        try:
            self.get(pid, version)
        except KeyError:
            return False
        return True

    def installed_source(self, pid: str, version: str) -> InstalledProcessorSource:
        processor = self.get(pid, version)
        provider = processor.source
        if processor.provenance != "plugin" or provider is None:
            raise KeyError((pid, version, "source"))
        try:
            payload = provider.read()
            if not isinstance(payload, bytes):
                raise TypeError("processor source reader must return bytes")
            if len(payload) > MAX_INSTALLED_SOURCE_BYTES:
                raise ValueError("processor source exceeds the response limit")
            source = payload.decode("utf-8")
        except Exception as exc:
            raise ProcessorSourceUnavailable(
                f"installed source for {pid}@{version} is unavailable") from exc
        return InstalledProcessorSource(
            processor_id=pid,
            version=version,
            language=provider.language,
            source=source,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @staticmethod
    def _from_promoted(item: dict) -> RegisteredProcessor:
        input_schema = [ColumnSchema.model_validate(value) for value in item["input_schema"]]
        return RegisteredProcessor(
            id=item["id"], version=item["version"], title=item["title"],
            mode=item["mode"], category=item["category"], code=item["code"],
            input_columns=[field.name for field in input_schema],
            input_schema=input_schema,
            output_schema=[ColumnSchema.model_validate(value) for value in item["output_schema"]],
            requirements=list(item["requirements"]), blurb=item["blurb"],
            provenance="promoted", creator_id=item["creator_id"],
            created_at=item["created_at"], semantic_digest=item["semantic_digest"],
        )


# Public plugin SPI name retained for existing packs; core uses the explicit name to distinguish it
# from the composition Registry in ``hub.deps``.
Registry = ProcessorRegistry
