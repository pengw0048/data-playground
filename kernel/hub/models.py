"""Core wire DTOs and the canvas graph model.

Everything here is backend-agnostic. camelCase on the wire (to match the frontend),
snake_case in Python. These shapes ARE the contract.
"""

from __future__ import annotations

import datetime
import math
from typing import Annotated, Any, Literal

from pydantic import (
    UUID4, AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter,
    field_validator, model_serializer, model_validator,
)
from pydantic.alias_generators import to_camel

# dataset/selection/sample/sql-view are the data wires; metric/value are leaf/value wires
# (a metric or a node value driving another node's param). All must be representable on an edge.
WireType = Literal["dataset", "selection", "sample", "sql-view", "metric", "value"]
NodeStatus = Literal["draft", "latest", "stale", "queued", "running", "failed"]
Placement = Literal["local", "distributed"]
DataCompleteness = Literal["complete", "page", "sample", "capped", "unknown"]
DataLimitReason = Literal["preview-scan", "interactive-row-budget"]
DataLimitScope = Literal["each-source", "result-window"]
SampleStrategy = Literal["prefix", "reservoir"]
MAX_SAFE_INTEGER = 2**53 - 1
ProfileCompleteness = Literal["complete", "sample", "unknown"]
PlanDigest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
ProcessorMode = Literal[
    "map",
    "map_batches",
    "filter",
    "flat_map",
    "flat_map_generator",
    "callable",
    "aggregate",
]

PREVIEWABLE_MODES: set[str] = {
    "map",
    "map_batches",
    "filter",
    "flat_map",
    "flat_map_generator",
}


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------- #
# Credentials (first-class Cred entity — references only, never raw secret bytes)
# --------------------------------------------------------------------------- #
class Cred(Wire):
    id: str
    name: str
    kind: str  # 'object_store' | 'agent'
    fields: dict = {}
    created_at: str | None = None


class CredUpsert(Wire):
    id: str | None = None
    name: str
    kind: str
    fields: dict = {}


# --------------------------------------------------------------------------- #
# Schema / catalog
# --------------------------------------------------------------------------- #
SchemaProvenance = Literal["inferred", "declared", "provider"]
SchemaCompatibilityStatus = Literal["compatible", "breaking", "unknown"]


class ColumnSchema(Wire):
    """One current schema-field model.

    ``type`` is the logical type used by execution and the UI.  Adapters may also
    report their source physical type, but must leave facts they cannot prove as
    ``None`` rather than guessing.
    """
    field_id: str | None = Field(default=None, description="Stable source-supplied field identity, when available.")
    name: str
    type: str = Field(description="Logical field type.")
    physical_type: str | None = Field(default=None, description="Source physical type, when available.")
    nullable: bool | None = Field(default=None, description="Whether the field accepts null values; null means unknown.")
    has_default: bool | None = Field(default=None, description="Whether a non-null field has a source default; null means unknown.")
    provenance: SchemaProvenance = Field(default="inferred", description="Evidence source for this field metadata.")
    capabilities: list[str] = []


class SchemaFieldCompatibility(Wire):
    kind: Literal["unchanged", "renamed", "added", "removed", "changed"]
    status: SchemaCompatibilityStatus
    reason: str
    field_id: str | None = None
    old_name: str | None = None
    new_name: str | None = None


class SchemaCompatibility(Wire):
    status: SchemaCompatibilityStatus
    fields: list[SchemaFieldCompatibility] = []


class KeyInfo(Wire):
    """A candidate/known key of a dataset — the column(s) that identify a row (a primary key).
    Composite = more than one column. `confidence`: 'declared' (owner-asserted) | 'verified'
    (measured unique on the data) | 'inferred' (name heuristic, unmeasured)."""
    columns: list[str]
    confidence: Literal["declared", "verified", "inferred"] = "inferred"
    unique: bool | None = None  # measured: distinct(cols) == count; None = not measured


class CatalogTable(Wire):
    id: str
    # Stable Workspace identity for this exact registration. Unlike a URI/name/table id it is never
    # rebound when a dataset is unregistered and later registered again.
    registration_id: str | None = None
    name: str
    uri: str
    row_count: int | None = None
    version: str | None = None
    columns: list[ColumnSchema] = []
    keys: list[KeyInfo] = []  # candidate/known keys (primary-key candidates), composite-aware
    missing: bool = False  # a local-path dataset whose file no longer exists (grey out / offer removal)
    updated_at: str | None = None
    meta: str | None = None
    # --- organization primitives (what makes a catalog of thousands of tables navigable) --------- #
    # `folder` is a delimiter-joined path ("prod/images/curated") — the browse hierarchy (a namespace).
    # `tags` are free-form labels for faceted filtering; `owner` and `description` are curation metadata.
    # All are generic + owner-asserted; nothing here is tied to any particular external catalog — but
    # they map cleanly onto the namespace/tag/owner model every mature catalog exposes, so an external
    # provider (via the CatalogProvider seam) can round-trip them.
    folder: str = ""
    tags: list[str] = []
    owner: str | None = None
    description: str | None = None
    usage: int = 0  # how often this dataset has been read (popularity signal; drives "most used" sort)
    # Fixed-length CAS token for the staged built-in catalog editor. It changes whenever the editable
    # metadata or declared primary key changes, but deliberately does not expose storage internals.
    metadata_revision: str | None = None


class DatasetRevision(Wire):
    """One immutable provider-native dataset revision."""
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    committed_at: datetime.datetime | None = None
    retention_owner: Literal["provider", "core"] = "provider"


class DatasetRevisionLastKnown(Wire):
    """Bounded non-sensitive display evidence saved with an exact Source selection."""
    committed_at: datetime.datetime | None = None


class ExactDatasetRef(Wire):
    """Opaque, path-independent identity for one exact retained dataset revision."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    kind: Literal["exact"]
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    last_known: DatasetRevisionLastKnown | None = None


class DatasetRevisionPage(Wire):
    items: list[DatasetRevision] = Field(default_factory=list, max_length=100)
    next_cursor: str | None = Field(default=None, max_length=256)
    has_more: bool = False

    @model_validator(mode="after")
    def validate_cursor_state(self) -> "DatasetRevisionPage":
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("dataset revision page continuation state is inconsistent")
        return self


class DatasetRevisionResolution(Wire):
    """Evidence that a latest/as-of request resolved to an immutable native revision."""
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    committed_at: datetime.datetime | None = None
    retention_owner: Literal["provider", "core"] = "provider"
    selector: Literal["latest", "as_of", "exact"]


class AsOfDatasetRef(Wire):
    """As-of intent plus the immutable provider evidence selected exactly once."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    kind: Literal["as_of"]
    as_of: datetime.datetime
    resolved: DatasetRevisionResolution

    @model_validator(mode="after")
    def validate_resolution(self) -> "AsOfDatasetRef":
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as-of DatasetRef requires an explicit timezone")
        if self.resolved.selector != "as_of":
            raise ValueError("as-of DatasetRef requires as-of resolution evidence")
        committed_at = self.resolved.committed_at
        if committed_at is None or committed_at.tzinfo is None or committed_at.utcoffset() is None:
            raise ValueError("as-of DatasetRef requires timezone-aware provider ordering evidence")
        if committed_at > self.as_of:
            raise ValueError("as-of DatasetRef resolution is after the requested instant")
        return self


DatasetRef = Annotated[ExactDatasetRef | AsOfDatasetRef, Field(discriminator="kind")]
_DATASET_REF_ADAPTER = TypeAdapter(DatasetRef)


def dataset_ref_identity(value: object) -> tuple[str, str]:
    """Return the exact identity carried by either strict DatasetRef variant."""
    ref = _DATASET_REF_ADAPTER.validate_python(value)
    if isinstance(ref, AsOfDatasetRef):
        return ref.resolved.dataset_id, ref.resolved.revision_id
    return ref.dataset_id, ref.revision_id


class DatasetRevisionCapabilities(Wire):
    """Provider-advertised revision selectors and their portable ordering contract."""
    selectors: list[Literal["exact", "latest", "as_of"]]
    as_of_ordering: Literal["latest_committed_at_at_or_before"] | None = None
    timezone: Literal["UTC"] | None = None
    dataset_view_save: bool = False


class DatasetRevisionSummary(Wire):
    """Bounded provider facts for one exact revision; absent facts are not inferred."""
    row_count: int | None = Field(default=None, ge=0)
    data_file_count: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    fragment_count: int | None = Field(default=None, ge=0)


class DatasetRevisionPreview(Wire):
    """A fixed, exact-revision preview window; it is never a read of current head."""
    columns: list[ColumnSchema] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    has_more: bool
    row_limit: Literal[100] = 100


class DatasetRevisionDetail(Wire):
    """Inspectable facts and a bounded preview for one retained exact revision."""
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    committed_at: datetime.datetime | None = None
    retention_owner: Literal["provider", "core"] = "provider"
    parent_revision_id: str | None = Field(default=None, max_length=256)
    producer_operation: str | None = Field(default=None, max_length=128)
    summary: DatasetRevisionSummary
    preview: DatasetRevisionPreview


class CatalogPublicationReceipt(Wire):
    """Durable acknowledgement returned by an idempotent catalog output publication."""
    idempotency_key: str
    uri: str
    version: str | None = None
    durable: Literal[True] = True


Cardinality = Literal["1:1", "1:N", "N:1", "N:M", "unknown"]


class Relationship(Wire):
    """A declared relationship between two datasets — the user-asserted join edge shown in the ER
    view. `confidence='declared'` (owner-asserted, trusted like a real FK). Composite via the column
    lists. This is the escape hatch for opaque transforms: declare what the code produced."""
    left_uri: str
    left_columns: list[str]
    right_uri: str
    right_columns: list[str]
    cardinality: Cardinality = "unknown"
    confidence: Literal["declared", "verified", "inferred"] = "declared"


class JoinSuggestion(Wire):
    """A proposed way to join two datasets: matching key column(s) on each side + the measured
    join cardinality. Surfaced in the join node's inspector (catalog-driven join hints)."""
    left_columns: list[str]
    right_columns: list[str]
    cardinality: Cardinality = "unknown"
    confidence: Literal["declared", "verified", "inferred"] = "inferred"
    score: float = 0.0            # ranking (higher = more likely the intended join)
    reason: str = ""              # human-readable why


class JoinAnalysis(Wire):
    """Everything the join node's inspector needs: ranked key suggestions for its two inputs, and a
    fan-out warning when the join isn't 1:1 (the result lands at the finer grain — a later
    parent-grain metric would double-count unless you aggregate)."""
    suggestions: list[JoinSuggestion] = []
    warning: str | None = None
    note: str | None = None  # why suggestions are empty / cardinality unknown, when applicable


class GrainInfo(Wire):
    """The grain of a relation on the canvas: the key column(s) at which each row is distinct,
    propagated through relational ops. `known=False` means the grain couldn't be determined (an
    opaque transform, an un-keyed source). This is what lets a filtered/sampled/aggregated dataset
    still be recognized as joinable — its grain still carries the key."""
    columns: list[str] | None = None
    known: bool = False
    verified: bool = False        # the grain columns are measured-unique at the source
    note: str = ""


class LineageNode(Wire):
    id: str
    name: str
    uri: str
    kind: str = "dataset"


class LineageEdge(Wire):
    parent: str
    child: str
    fact_count: int = Field(ge=1)


class LineageFieldMapping(Wire):
    source_field: str = Field(min_length=1, max_length=512)
    destination_field: str = Field(min_length=1, max_length=512)


class LineagePublication(Wire):
    """Catalog-local provenance supplied when one output becomes visible.

    This is deliberately narrower than an execution request: it contains only the immutable identity
    needed to create one fact per source in the catalog transaction. ``attempt_id`` remains null when a
    backend has no execution attempt distinct from the logical run.
    """

    idempotency_key: str = Field(min_length=1, max_length=2048)
    run_id: str | None = Field(default=None, min_length=1, max_length=512)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=512)
    producer: str | None = Field(default=None, min_length=1, max_length=512)
    producer_version: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER)
    step_id: str | None = Field(default=None, min_length=1, max_length=512)
    provenance: Literal["run", "manual", "imported"]
    field_mappings: list[LineageFieldMapping] = Field(default_factory=list, max_length=256)

    @field_validator("producer_version", mode="before")
    @classmethod
    def validate_producer_version(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("lineage producer version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "LineagePublication":
        for field_name in ("idempotency_key", "run_id", "attempt_id", "producer", "step_id"):
            value = getattr(self, field_name)
            if value is not None and (not value or value != value.strip()):
                raise ValueError(
                    f"lineage {field_name} cannot be blank or contain surrounding whitespace")
        if self.attempt_id is not None and self.run_id is None:
            raise ValueError("lineage attempt requires a run identity")
        if self.producer_version is not None and self.producer is None:
            raise ValueError("lineage producer version requires a producer identity")
        if self.provenance == "run" and not (
                self.run_id and self.producer and self.step_id):
            raise ValueError("run lineage requires run, producer, and step identities")
        self.field_mappings = [
            LineageFieldMapping(source_field=source, destination_field=destination)
            for source, destination in sorted({
                (mapping.source_field, mapping.destination_field)
                for mapping in self.field_mappings
            })
        ]
        return self


class WriteDestination(Wire):
    """Stable logical identity of one managed write destination."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    logical_uri: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=512)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=128)
    provider: Literal["managed-local-file", "managed-local-lance"] = "managed-local-file"

    @model_validator(mode="after")
    def validate_identity(self) -> "WriteDestination":
        for field_name in ("logical_uri", "name", "dataset_id"):
            value = getattr(self, field_name)
            if value is not None and value != value.strip():
                raise ValueError(
                    f"write destination {field_name} cannot contain surrounding whitespace")
        self.logical_uri = self.logical_uri.rstrip("/")
        if not self.logical_uri:
            raise ValueError("write destination logical_uri cannot be blank")
        return self


class WritePartitionExpectation(Wire):
    """One bounded partition field requested by a write intent."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    field: str = Field(min_length=1, max_length=512)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("write partition field cannot contain surrounding whitespace")
        return value


class WriteProvenance(Wire):
    """Frozen producer evidence and the ordered source identities used by one write."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    publication: LineagePublication
    parents: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_parents(self) -> "WriteProvenance":
        canonical: list[str] = []
        seen: set[str] = set()
        for parent in self.parents:
            token = str(parent).rstrip("/")
            if not token or token != str(parent):
                raise ValueError("write provenance parents must be canonical non-empty identities")
            if token in seen:
                raise ValueError("write provenance parents must be unique")
            seen.add(token)
            canonical.append(token)
        self.parents = sorted(canonical)
        return self


class WriteIntent(Wire):
    """Frozen pre-1.0 contract for one managed local create, replace, or Lance append."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    destination: WriteDestination
    mode: Literal["create", "replace", "append"]
    expected_schema: list[ColumnSchema] = Field(default_factory=list, max_length=1024)
    expected_head: ExactDatasetRef | None = None
    idempotency_key: str = Field(min_length=1, max_length=2048)
    partitions: list[WritePartitionExpectation] = Field(default_factory=list, max_length=32)
    provenance: WriteProvenance

    @model_validator(mode="after")
    def validate_contract(self) -> "WriteIntent":
        if self.idempotency_key != self.idempotency_key.strip():
            raise ValueError("write idempotency_key cannot contain surrounding whitespace")
        names = [column.name for column in self.expected_schema]
        if any(not name or name != name.strip() for name in names):
            raise ValueError("write expected schema field names must be canonical")
        if len(names) != len(set(names)):
            raise ValueError("write expected schema field names must be unique")
        partition_fields = [item.field for item in self.partitions]
        if len(partition_fields) != len(set(partition_fields)):
            raise ValueError("write partition fields must be unique")
        if any(field not in set(names) for field in partition_fields):
            raise ValueError("write partition fields must exist in the expected schema")
        if self.provenance.publication.idempotency_key != self.idempotency_key:
            raise ValueError("write provenance identity must match the write idempotency key")
        if self.mode == "create":
            if self.destination.dataset_id is not None or self.expected_head is not None:
                raise ValueError("create write cannot claim an existing dataset or expected head")
        else:
            if self.destination.dataset_id is None or self.expected_head is None:
                raise ValueError(
                    f"{self.mode} write requires destination dataset and expected head")
            if self.expected_head.dataset_id != self.destination.dataset_id:
                raise ValueError(
                    f"{self.mode} expected head must belong to the destination dataset")
        if self.mode == "append":
            if self.destination.provider != "managed-local-lance":
                raise ValueError("append write requires the managed-local-lance provider")
            if not self.destination.logical_uri.lower().endswith(".lance"):
                raise ValueError("append write requires a .lance destination")
        elif self.destination.provider != "managed-local-file":
            raise ValueError("create/replace writes require the managed-local-file provider")
        return self


class WritePublicationIdentity(Wire):
    """Durable provider-side identity of one managed local publication."""

    provider: Literal["managed-local-file", "managed-local-lance"] = "managed-local-file"
    logical_uri: str
    artifact_uri: str
    publish_sequence: int = Field(ge=1, le=MAX_SAFE_INTEGER)
    idempotency_key: str
    catalog_version: str | None = Field(default=None, min_length=1, max_length=512)
    backend_version: str | None = Field(default=None, min_length=1, max_length=512)


class WriteReceipt(Wire):
    """Durable evidence for one exact revision-producing write."""

    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    parent_head: ExactDatasetRef | None = None
    head: DatasetRevision
    rows: int = Field(ge=0, le=MAX_SAFE_INTEGER)
    bytes: int = Field(ge=0, le=MAX_SAFE_INTEGER)
    schema_facts: list[ColumnSchema] = Field(
        default_factory=list, max_length=1024, alias="schema")
    partitions: list[WritePartitionExpectation] = Field(default_factory=list, max_length=32)
    publication: WritePublicationIdentity
    provenance: WriteProvenance
    execution_manifest_sha256: PlanDigest | None = None
    durable: Literal[True] = True

    @property
    def schema(self) -> list[ColumnSchema]:
        """Expose the natural Python spelling while keeping Pydantic's legacy method unshadowed."""
        return self.schema_facts


def _lineage_fact_cursor(value: str) -> str:
    if int(value) >= 2**63:
        raise ValueError("lineage fact cursor exceeds the signed BIGINT range")
    return value


LineageFactCursor = Annotated[
    str,
    Field(min_length=1, max_length=19, pattern=r"^[1-9][0-9]*$"),
    AfterValidator(_lineage_fact_cursor),
]


class LineageFact(Wire):
    id: LineageFactCursor
    fact_key: str = Field(min_length=1, max_length=512)
    publication_key: str = Field(min_length=1, max_length=96)
    source_key: str = Field(min_length=1, max_length=8192)
    source_uri: str = Field(min_length=1, max_length=8192)
    source_version: str | None = Field(default=None, min_length=1, max_length=512)
    destination_key: str = Field(min_length=1, max_length=8192)
    destination_uri: str = Field(min_length=1, max_length=8192)
    destination_version: str | None = Field(default=None, min_length=1, max_length=512)
    run_id: str | None = Field(default=None, min_length=1, max_length=512)
    execution_manifest_sha256: PlanDigest | None = None
    attempt_id: str | None = Field(default=None, min_length=1, max_length=512)
    producer: str | None = Field(default=None, min_length=1, max_length=512)
    producer_version: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER)
    step_id: str | None = Field(default=None, min_length=1, max_length=512)
    provenance: Literal["run", "manual", "imported"]
    field_mappings: list[LineageFieldMapping] = Field(default_factory=list, max_length=256)
    created_at: datetime.datetime

    @field_validator("producer_version", mode="before")
    @classmethod
    def validate_producer_version(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("lineage producer version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> "LineageFact":
        for field_name in (
                "fact_key", "publication_key", "source_key", "source_uri",
                "source_version", "destination_key", "destination_uri",
                "destination_version", "run_id", "attempt_id", "producer", "step_id"):
            value = getattr(self, field_name)
            if value is not None and value != value.strip():
                raise ValueError(
                    f"lineage fact {field_name} cannot contain surrounding whitespace")
        if self.attempt_id is not None and self.run_id is None:
            raise ValueError("lineage fact attempt requires a run identity")
        if self.producer_version is not None and self.producer is None:
            raise ValueError("lineage fact producer version requires a producer identity")
        if self.provenance == "run" and not (
                self.run_id and self.producer and self.step_id):
            raise ValueError("run lineage fact requires run, producer, and step identities")
        if self.created_at.utcoffset() is None:
            raise ValueError("lineage fact creation time must include a timezone")
        return self


class LineageFactsPage(Wire):
    items: list[LineageFact] = Field(default_factory=list, max_length=500)
    next_after_id: LineageFactCursor | None = None
    has_more: bool = False

    @model_validator(mode="after")
    def validate_cursor_state(self) -> "LineageFactsPage":
        if self.has_more != (self.next_after_id is not None):
            raise ValueError("lineage fact page continuation state is inconsistent")
        return self


class LineageResult(Wire):
    root_uri: str = Field(min_length=1, max_length=8192)
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []
    truncated: bool = False  # the connected component was larger than max_nodes / deeper than depth


# --------------------------------------------------------------------------- #
# Catalog browse / search / facets — the discovery surface that scales to
# thousands of tables (server-side filter + paginate + facet, never "load all").
# --------------------------------------------------------------------------- #
class CatalogQuery(Wire):
    """A filter/sort/paginate request over the catalog. Every field is optional; the empty query is
    'the first page of everything, by name'. This is the ONE shape a CatalogProvider answers for
    browsing — a `q` substring, a `folder` subtree, `tags` (ALL must match), an `owner`, required
    `has_columns`, plus sort + a bounded window. Pushed down to the store (indexed), never realized
    into an in-memory list first."""
    q: str | None = None
    folder: str | None = None          # a folder path; matches that folder AND its subtree
    tags: list[str] = []               # every listed tag must be present (AND)
    owner: str | None = None
    uris: list[str] = []               # restrict to these exact uris (a batch "get these", no 404 on a miss)
    has_columns: list[str] = []        # dataset must expose every listed column (by name)
    sort: Literal["name", "rows", "updated", "usage", "folder"] = "name"
    order: Literal["asc", "desc"] = "asc"
    limit: int = 50
    offset: int = 0


class FacetValue(Wire):
    value: str
    count: int


class Facets(Wire):
    """Distinct values + counts for each facetable dimension, computed over the ACTIVE filter set
    (drill-down semantics) — what powers the facet rail's clickable, counted filters.
    `semantic_available` rides along so a UI knows whether search-by-meaning exists (an embedder
    plugin is installed) without a separate capability round-trip."""
    folders: list[FacetValue] = []
    tags: list[FacetValue] = []
    owners: list[FacetValue] = []
    semantic_available: bool = False


class CatalogPage(Wire):
    """One window of a filtered catalog: the page's items plus the totals a UI needs to paginate
    (total match count, whether more follow) — so the client shows '1–50 of 4,213' and loads the
    next page on demand instead of holding every table in memory."""
    items: list[CatalogTable] = []
    total: int = 0
    offset: int = 0
    limit: int = 50
    has_more: bool = False


class FolderNode(Wire):
    """A folder in the browse tree: its leaf name, full path, and how many tables live in its subtree."""
    name: str
    path: str
    table_count: int = 0


class CatalogBrowse(Wire):
    """One level of the browse tree at a prefix: the immediate child folders (with subtree counts) and
    the tables filed directly at this prefix — a bounded sample (`total_tables`/`truncated` signal
    when there are more; the full listing is the paginated list query with folder=prefix). Lets the
    UI lazily expand a folder tree of any size."""
    prefix: str = ""
    folders: list[FolderNode] = []
    tables: list[CatalogTable] = []
    total_tables: int = 0
    truncated: bool = False


class WorkspaceLocalPlacementCapability(Wire):
    """A local-only mutation destination paired with a provider resource read reference."""
    writable: bool
    can_create_canvas: bool
    can_move_canvas: bool
    container_id: str | None = None
    container_version: int | None = None
    recovery_state: Literal["ready", "unavailable"]


class WorkspaceResource(Wire):
    """One Workspace child addressed by an opaque, path-independent reference."""
    id: str
    kind: Literal["container", "canvas", "dataset", "dataset_view"]
    name: str
    parent_id: str | None = None
    placement_id: str | None = None
    version: int | None = None
    catalog_folder_id: str | None = None
    catalog_folder_state: Literal["current", "detached"] | None = None
    catalog_folder_path: str | None = None
    detached: bool = False
    source: Literal["local", "provider"] = "local"
    mount_id: str | None = None
    provider: str | None = None
    resource_id: str | None = None
    binding_id: str | None = None
    reference_state: Literal[
        "current", "offline", "permission_lost", "detached", "provider_error"
    ] = "current"
    last_known: bool = False
    last_resolved_at: datetime.datetime | None = None
    local_placement: WorkspaceLocalPlacementCapability | None = None
    # Folder authority is local and explicit.  A provider/overlay location may still expose a
    # local Canvas placement capability, but it must never be mistaken for Folder mutation rights.
    can_create_folder: bool = False
    can_rename_folder: bool = False
    can_delete_folder: bool = False
    folder_mutation_unavailable_reason: str | None = Field(default=None, max_length=256)
    # Provider mounts stay source-only.  This explicit false is intentionally independent of the
    # local placement capability so clients cannot mistake a local Canvas move for provider writeback.
    provider_mutation: bool = False


class WorkspaceSourceStatus(Wire):
    """Completeness of one independently bounded source in a mixed Workspace page."""
    id: str
    kind: Literal["local", "provider", "configuration"]
    completeness: Literal[
        "complete", "page", "pending", "partial", "unavailable", "unsupported"
    ]
    mount_id: str | None = None
    provider: str | None = None
    error: str | None = None
    reference_state: Literal[
        "current", "offline", "permission_lost", "detached", "provider_error"
    ] | None = None


class WorkspaceBrowsePage(Wire):
    container: WorkspaceResource | None
    items: list[WorkspaceResource] = []
    next_cursor: str | None = None
    has_more: bool = False
    completeness: Literal["complete", "page", "partial"] = "complete"
    sources: list[WorkspaceSourceStatus] = []

    @model_validator(mode="after")
    def validate_cursor_state(self) -> "WorkspaceBrowsePage":
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("Workspace browse continuation state is inconsistent")
        if self.completeness != "partial" and self.completeness != (
                "page" if self.has_more else "complete"):
            raise ValueError("Workspace browse completeness is inconsistent")
        return self


class WorkspaceResourceResolution(Wire):
    resource: WorkspaceResource | None
    ancestors: list[WorkspaceResource] = []
    source: WorkspaceSourceStatus = WorkspaceSourceStatus(
        id="local", kind="local", completeness="complete")


class WorkspaceProviderRelinkRequest(Wire):
    mount_id: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=512)


class WorkspaceProviderRelinkResult(Wire):
    ok: bool = True
    resource: WorkspaceResource
    previous_resource: WorkspaceResource


class WorkspaceSearchSourceStatus(WorkspaceSourceStatus):
    freshness: Literal["current", "stale", "unknown"]
    search_mode: Literal["native", "fallback", "unsupported"]


class WorkspaceSearchGroup(Wire):
    source: WorkspaceSearchSourceStatus
    items: list[WorkspaceResource] = []


class WorkspaceSearchPage(Wire):
    query: str
    groups: list[WorkspaceSearchGroup] = []
    next_cursor: str | None = None
    has_more: bool = False
    completeness: Literal["complete", "page", "partial"] = "complete"

    @model_validator(mode="after")
    def validate_cursor_state(self) -> "WorkspaceSearchPage":
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("Workspace search continuation state is inconsistent")
        if self.completeness != "partial" and self.completeness != (
                "page" if self.has_more else "complete"):
            raise ValueError("Workspace search completeness is inconsistent")
        return self


class CatalogFolder(Wire):
    """A first-class browse folder. Additive to the per-dataset `folder` path string: it lets an EMPTY
    folder exist and be renamed/deleted, so a folder can be created up front and filled later."""
    path: str


class CatalogMetadata(Wire):
    """The owner-editable organization fields of a dataset (everything but the probed schema/rows).
    A PUT of this is how a table gets filed into a folder, tagged, owned, or described."""
    folder: str | None = None
    tags: list[str] | None = None
    owner: str | None = None
    description: str | None = None
    name: str | None = None          # optional friendly rename; blank keeps the current name


class CatalogEdit(Wire):
    """One complete staged built-in catalog edit, guarded by its read revision."""
    expected_revision: str
    folder: str = ""
    tags: list[str] = []
    owner: str | None = None
    description: str | None = None
    name: str | None = None
    declared_key: list[str] = []


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
class SampleProvenance(Wire):
    """Evidence for the population a visible preview/sample page represents.

    ``scanned_rows`` and ``total_rows`` are intentionally nullable: a bounded adapter may prove its
    work limit without knowing how many rows it actually inspected or how large the population is.
    ``identity`` changes when any known sampling input changes, so a client can distinguish samples
    that happen to contain similar rows.
    """

    strategy: SampleStrategy
    seed: int | None = None
    requested_rows: int = Field(ge=0)
    scanned_rows: int | None = Field(default=None, ge=0)
    returned_rows: int = Field(ge=0)
    total_rows: int | None = Field(default=None, ge=0)
    dataset_identity: str | None = None
    dataset_revision: str | None = None
    identity: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    limitations: list[str] = Field(default_factory=list)


class DatasetViewAllSampling(Wire):
    """Keep every row in the exact filtered population."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    kind: Literal["all"] = "all"


class DatasetViewReservoirSampling(Wire):
    """One deterministic reservoir over the exact filtered population."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    kind: Literal["reservoir"] = "reservoir"
    size: int = Field(ge=1, le=100_000)
    # DuckDB 1.5.x parses the reservoir seed as a signed 32-bit integer.
    seed: int = Field(ge=0, le=2_147_483_647)


DatasetViewSampling = Annotated[
    DatasetViewAllSampling | DatasetViewReservoirSampling,
    Field(discriminator="kind"),
]


class TemporalWindowV1(Wire):
    """One bounded half-open interval over an integer field in a named time domain."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True,
    )

    time_field: str = Field(min_length=1, max_length=512)
    time_domain: str = Field(min_length=1, max_length=512)
    start_tick: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^(?:0|[1-9][0-9]*|-[1-9][0-9]*)$",
        description="Canonical signed-int64 decimal string.",
    )
    end_tick: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^(?:0|[1-9][0-9]*|-[1-9][0-9]*)$",
        description="Canonical signed-int64 decimal string.",
    )

    @field_validator("time_field", "time_domain")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value or "\x00" in value:
            raise ValueError("temporal window names must be trimmed, non-empty, and NUL-free")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> "TemporalWindowV1":
        start_tick, end_tick = int(self.start_tick), int(self.end_tick)
        if not (-(1 << 63) <= start_tick <= (1 << 63) - 1
                and -(1 << 63) <= end_tick <= (1 << 63) - 1):
            raise ValueError("temporal window ticks must be signed 64-bit integers")
        if start_tick >= end_tick:
            raise ValueError("temporal window startTick must be less than endTick")
        return self


class DatasetViewCreateRequest(Wire):
    """Immutable DatasetView intent. Workspace placement is derived by the server."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    submission_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    dataset_ref: ExactDatasetRef
    selected_columns: list[str] = Field(min_length=1, max_length=500)
    predicate: str | None = Field(default=None, max_length=65_536)
    temporal_window: TemporalWindowV1 | None = None
    sampling: DatasetViewSampling = DatasetViewAllSampling()

    @model_validator(mode="after")
    def validate_intent(self) -> "DatasetViewCreateRequest":
        if self.submission_id != self.submission_id.strip():
            raise ValueError("DatasetView submission id cannot contain surrounding whitespace")
        if self.name != self.name.strip() or "\x00" in self.name:
            raise ValueError("DatasetView name must be trimmed and cannot contain NUL")
        columns = [column.strip() for column in self.selected_columns]
        if any(not column or "\x00" in column for column in columns):
            raise ValueError("DatasetView columns must be non-blank and cannot contain NUL")
        if len(set(columns)) != len(columns):
            raise ValueError("DatasetView columns must be unique")
        self.selected_columns = columns
        if self.predicate is not None:
            predicate = self.predicate.strip()
            self.predicate = predicate or None
        return self


class DatasetViewPlacement(Wire):
    """Stable Workspace location selected atomically beside the source registration."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    container_id: str = Field(min_length=1, max_length=512)
    placement_id: str = Field(min_length=1, max_length=512)
    source_registration_id: str = Field(min_length=1, max_length=512)


class DatasetViewDefinitionV1(Wire):
    """The complete immutable, schema-versioned definition of one reusable DatasetView."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=32)
    creator_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=256)
    dataset_ref: ExactDatasetRef
    placement: DatasetViewPlacement
    selected_columns: list[str] = Field(min_length=1, max_length=500)
    predicate: str | None = None
    temporal_window: TemporalWindowV1 | None = None
    sampling: DatasetViewSampling
    sample_provenance: SampleProvenance | None = None
    retention_owner: Literal["provider", "core"]
    created_at: datetime.datetime
    semantic_sha256: PlanDigest
    definition_sha256: PlanDigest

    def definition_digest_payload(self) -> dict[str, Any]:
        """Return the one canonical wire payload covered by definitionSha256."""
        payload = self.model_dump(by_alias=True, mode="json")
        payload.pop("definitionSha256")
        if payload.get("temporalWindow") is None:
            payload.pop("temporalWindow", None)
        return payload

    @model_validator(mode="after")
    def validate_sampling_evidence(self) -> "DatasetViewDefinitionV1":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("DatasetView createdAt must be timezone-aware")
        if self.sampling.kind == "all":
            if self.sample_provenance is not None:
                raise ValueError("an all-rows DatasetView cannot carry sample provenance")
            return self
        evidence = self.sample_provenance
        if (evidence is None or evidence.strategy != "reservoir"
                or evidence.seed != self.sampling.seed
                or evidence.requested_rows != self.sampling.size
                or evidence.dataset_identity != self.dataset_ref.dataset_id
                or evidence.dataset_revision != self.dataset_ref.revision_id):
            raise ValueError("DatasetView reservoir provenance does not match its immutable definition")
        return self


class DatasetViewPreview(Wire):
    """One bounded replay of an immutable DatasetView definition."""

    columns: list[ColumnSchema] = Field(default_factory=list, max_length=500)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    row_count: int | None = Field(default=None, ge=0)
    has_more: bool
    row_limit: Literal[100] = 100
    sample_provenance: SampleProvenance | None = None


class DatasetViewDeleteResult(Wire):
    ok: bool = True
    deleted: bool


class SampleResult(Wire):
    """One page of rows plus an explicit statement of what that page represents.

    ``row_count`` is an exact total when present, never the number of rows in this response. A limit
    applies either to EACH upstream source scan or to the browsable RESULT window; it is deliberately
    separate from the caller-selected page size. This prevents a bounded multi-source preview from
    being described as the first N output rows.
    """

    columns: list[ColumnSchema] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int | None = Field(
        default=None, ge=0,
        description="Exact total rows in the dataset/result when known; not this page's row count.",
    )
    has_more: bool | None = Field(
        default=None,
        description=(
            "Whether another page exists within the current interactive scope: true/false is "
            "proven by lookahead, exact metadata, or a result-window boundary; null is unknown. "
            "It never asserts whether more rows exist in the full dataset beyond that scope."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Whether this response is not a proven complete dataset/result. Every successful "
            "non-complete response is truncated; unavailable responses are not."
        ),
    )
    completeness: DataCompleteness = Field(
        default="unknown",
        description=(
            "complete=all rows; page=one page of a known/continuable result; sample=derived from "
            "bounded source scans; capped=result window ended at a server limit; unknown=no proof."
        ),
    )
    row_limit: int | None = Field(
        default=None, ge=1,
        description="Server row limit whose meaning is defined by limitScope; never the page size.",
    )
    limit_reason: DataLimitReason | None = Field(
        default=None,
        description="Why rowLimit applies. Present only together with rowLimit and limitScope.",
    )
    limit_scope: DataLimitScope | None = Field(
        default=None,
        description=(
            "each-source means every upstream source scan is independently bounded; "
            "result-window means result pagination cannot continue beyond rowLimit."
        ),
    )
    sample_provenance: SampleProvenance | None = None
    preview_ref: str | None = None
    # The exact, secret-free Source revisions used for this preview. Inner keys intentionally remain
    # snake_case because the same minimal dict is persisted verbatim in run admission/history.
    input_manifest: list[dict[str, str]] | None = None
    not_previewable: bool = False
    error: bool = False        # a real failure (bad code / bad query), distinct from P8 not_previewable
    reason: str | None = None
    wire: WireType = "dataset"

    @model_validator(mode="after")
    def _truthful_scope(self) -> "SampleResult":
        limit_parts = (self.row_limit, self.limit_reason, self.limit_scope)
        if any(part is not None for part in limit_parts) and not all(
                part is not None for part in limit_parts):
            raise ValueError("rowLimit, limitReason, and limitScope must be provided together")
        if self.error and self.not_previewable:
            raise ValueError("a sample result cannot be both an error and not previewable")
        unavailable = self.error or self.not_previewable
        if unavailable:
            if self.completeness != "unknown":
                raise ValueError("an unavailable sample must have unknown completeness")
            if self.rows or self.row_count is not None or self.has_more is not None:
                raise ValueError("an unavailable sample cannot carry rows, rowCount, or hasMore")
            if self.truncated:
                raise ValueError("an unavailable sample cannot be truncated")
            if self.row_limit is not None:
                raise ValueError("an unavailable sample cannot carry an active row limit")
            if not self.reason or not self.reason.strip():
                raise ValueError("an unavailable sample requires a non-empty reason")
            return self
        if self.limit_reason == "preview-scan" and self.limit_scope != "each-source":
            raise ValueError("preview-scan limits must use limitScope=each-source")
        if self.limit_reason == "interactive-row-budget" and self.limit_scope != "result-window":
            raise ValueError("interactive-row-budget limits must use limitScope=result-window")
        if self.row_count is not None and self.row_count < len(self.rows):
            raise ValueError("rowCount cannot be smaller than the returned page")
        if self.has_more is True and not self.truncated:
            raise ValueError("hasMore requires truncated=true")
        if self.completeness == "complete":
            if self.row_count is None or self.row_count != len(self.rows):
                raise ValueError("complete data requires rowCount to equal the returned row count")
            if self.has_more is not False or self.truncated:
                raise ValueError("complete data cannot be truncated or have another page")
            if self.row_limit is not None:
                raise ValueError("complete data cannot carry an active row limit")
        if self.completeness == "sample":
            if not self.truncated:
                raise ValueError("sample data requires truncated=true")
            reservoir = self.sample_provenance and self.sample_provenance.strategy == "reservoir"
            if not reservoir and (self.row_limit is None or self.limit_reason != "preview-scan"
                                  or self.limit_scope != "each-source"):
                raise ValueError("sample data requires an each-source preview-scan limit")
        if self.completeness == "capped":
            if not self.truncated:
                raise ValueError("capped data requires truncated=true")
            if self.row_limit is None:
                raise ValueError("capped data requires rowLimit, limitReason, and limitScope")
            if self.has_more is not False:
                raise ValueError("capped data requires hasMore=false at the interactive boundary")
            if (self.limit_reason != "interactive-row-budget"
                    or self.limit_scope != "result-window"):
                raise ValueError("capped data requires a result-window interactive-row-budget limit")
        if self.completeness != "complete" and not self.truncated:
            raise ValueError("successful non-complete data requires truncated=true")
        return self


class ColumnProfile(Wire):
    name: str
    type: str
    non_null: int = Field(default=0, ge=0)
    nulls: int = Field(default=0, ge=0)
    distinct: int | None = Field(
        default=None, ge=0,
        description="Measured distinct count, or null when the column type cannot be compared.",
    )
    distinct_is_approximate: bool = Field(
        default=False,
        description="Whether distinct was estimated (for example, whole-dataset HLL) rather than exact.",
    )
    min: str | None = None         # stringified (numeric / temporal / text); None if not applicable
    max: str | None = None
    mean: float | None = None      # numeric columns only

    @model_validator(mode="after")
    def _distinct_shape(self) -> "ColumnProfile":
        if self.distinct is None and self.distinct_is_approximate:
            raise ValueError("distinctIsApproximate requires a distinct value")
        return self


class ProfileResult(Wire):
    """Column statistics with an explicit, non-inferred measurement scope."""

    # The relation that was measured. Durable full-profile jobs always populate this field; keeping it
    # on ProfileResult (rather than manufacturing a RunOutput) preserves the inspection/job boundary.
    target_port_id: str | None = Field(default=None, min_length=1, max_length=128)
    columns: list[ColumnProfile] = Field(default_factory=list)
    row_count: int = Field(
        default=0, ge=0,
        description="Rows actually profiled; a full dataset total only when completeness=complete.",
    )
    sampled: bool = Field(
        default=True,
        description="Whether statistics were computed from a bounded sample instead of all rows.",
    )
    completeness: ProfileCompleteness = Field(
        default="unknown",
        description="complete=whole dataset, sample=bounded rows, unknown=no statistics are available.",
    )
    # Present only when the profile was measured from a bounded preview or an explicit Sample node.
    # A complete profile over an unsampled graph must not acquire sample evidence by implication.
    sample_provenance: SampleProvenance | None = None
    # Exact Source revisions measured by this profile. The established local-run manifest shape is
    # reused verbatim so preview, run, and profile recovery have one durable input identity contract.
    input_manifest: list[dict[str, str]] | None = None
    not_previewable: bool = False
    error: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def _truthful_scope(self) -> "ProfileResult":
        if self.error and self.not_previewable:
            raise ValueError("a profile result cannot be both an error and not previewable")
        unavailable = self.error or self.not_previewable
        if unavailable:
            if self.completeness != "unknown":
                raise ValueError("an unavailable profile must have unknown completeness")
            if self.columns or self.row_count:
                raise ValueError("an unavailable profile cannot carry statistics")
            if not self.reason or not self.reason.strip():
                raise ValueError("an unavailable profile requires a non-empty reason")
            if self.sample_provenance is not None:
                raise ValueError("an unavailable profile cannot carry sample provenance")
            return self
        if self.completeness == "unknown":
            raise ValueError("a successful profile must declare complete or sample completeness")
        if self.completeness == "complete" and self.sampled:
            raise ValueError("a complete profile cannot be marked sampled")
        if self.completeness == "sample" and not self.sampled:
            raise ValueError("a sample profile must be marked sampled")
        if any(column.non_null + column.nulls != self.row_count for column in self.columns):
            raise ValueError("each profile column's nonNull + nulls must equal rowCount")
        return self


# --------------------------------------------------------------------------- #
# Pipeline import
# --------------------------------------------------------------------------- #
class ImportStage(Wire):
    name: str
    processor: str
    mode: ProcessorMode
    previewable: bool


class DriverStep(Wire):
    kind: str  # read | op | write | commit | error_gate
    label: str
    node_type: str | None = None


class PipelineImport(Wire):
    config: str
    params: dict[str, Any] = {}
    input_columns: list[str] = []
    output_columns: list[str] = []
    data_filter: str | None = None
    stages: list[ImportStage] = []
    driver_steps: list[DriverStep] = []
    # A runnable canvas graph the importer decomposed the foreign pipeline into. When present, the SPA
    # drops it straight onto a fresh canvas (via applyAgentGraph) and it runs like any other graph —
    # this is what makes "import an external pipeline → runnable canvas" real. stages/driver_steps stay
    # as the human-readable description. None ⇒ the importer only described the pipeline, didn't build it.
    graph: "Graph | None" = None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class RunEstimate(Wire):
    rows: int | None = None   # real source-row count; None when no source is countable (size unknown)
    bytes: int | None = None  # estimated peak data volume (rows × row width); the confirm gate's cost signal
    placement: Placement
    needs_confirm: bool
    breakdown: str | None = None


class ProfileEstimate(RunEstimate):
    """Whole-profile preflight plus the server-minted identity required by submission."""
    target_port_id: str = Field(min_length=1, max_length=128)
    plan_digest: PlanDigest
    input_manifest: list[dict[str, str]] | None = None


class ProfileIdentity(Wire):
    """Current server identity for recovery without re-running the size estimate."""
    target_port_id: str = Field(min_length=1, max_length=128)
    plan_digest: PlanDigest
    input_manifest: list[dict[str, str]] | None = None


class PerNodeStatus(Wire):
    node_id: str
    status: str  # per-step run state: queued | running | done | failed (not a NodeStatus)
    rows: int | None = None
    ms: int | None = None
    label: str | None = None
    error: str | None = None  # set on the step that failed — the error (+ a fix hint) attributed to its node


class RunBackendRef(Wire):
    """Durable handle for a run owned by an external execution control plane.

    The handle is intentionally provider-neutral and contains no credentials. A backend can reconstruct
    its supervisor from this record plus its own operator configuration after the hub/kernel restarts.
    """
    backend: str
    cluster_ref: str | None = None
    submission_id: str
    attempt_id: str
    job_uri: str
    result_uri: str
    code_ref: str | None = None
    durable: bool = True


class RunOutput(Wire):
    """One declared output port and its durable publication state.

    Port metadata is a snapshot, not a live lookup: history remains intelligible after a node spec or
    Section declaration changes.  A URI becomes public only after publication commits; pending and
    terminal non-committed outcomes therefore cannot carry storage or catalog identities.
    """

    node_id: str = Field(min_length=1, max_length=256)
    port_id: str = Field(min_length=1, max_length=128)
    port_label: str | None = Field(default=None, max_length=256)
    wire: WireType
    publication_kind: Literal["result", "catalog"]
    outcome: Literal["pending", "committed", "failed", "skipped", "cancelled"]
    uri: str | None = Field(default=None, max_length=8192)
    table: str | None = Field(default=None, max_length=512)
    version: str | None = Field(
        default=None, min_length=1, max_length=512,
        exclude_if=lambda value: value is None,
    )
    rows: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=4096)
    # Evidence travels with the durable output snapshot so result history survives restart.
    sample_provenance: SampleProvenance | None = None
    write_receipt: WriteReceipt | None = None

    @model_validator(mode="after")
    def _publication_shape(self) -> "RunOutput":
        if self.port_id != self.port_id.strip():
            raise ValueError("run output portId cannot contain surrounding whitespace")
        if self.version is not None and self.version != self.version.strip():
            raise ValueError("run output catalog version cannot contain surrounding whitespace")
        if self.outcome == "committed":
            if not self.uri:
                raise ValueError("a committed run output requires a URI")
            if self.publication_kind == "catalog" and not self.table:
                raise ValueError("a committed catalog output requires a table identity")
            if self.publication_kind == "result" and self.table is not None:
                raise ValueError("a non-catalog run output cannot carry a table identity")
            if self.publication_kind == "result" and self.version is not None:
                raise ValueError("a non-catalog run output cannot carry a catalog version")
            if self.write_receipt is not None:
                if self.publication_kind != "catalog":
                    raise ValueError("a write receipt requires a catalog output")
                if (self.write_receipt.publication.artifact_uri != self.uri
                        or self.write_receipt.publication.catalog_version != self.version
                        or self.write_receipt.rows != self.rows):
                    raise ValueError("a write receipt must describe the exact run output")
        elif (self.uri is not None or self.table is not None or self.version is not None
              or self.write_receipt is not None):
            raise ValueError(
                "a non-committed run output cannot expose a URI, table, or catalog version")
        return self


def validate_run_output_rows(
        outputs: list[RunOutput], rows: int | None, *, field_name: str) -> None:
    """Validate the one scalar row-count projection shared by live status and history."""
    if rows is None:
        return
    committed = [output for output in outputs if output.outcome == "committed"]
    if (len(outputs) != 1 or len(committed) != 1 or committed[0].rows is None
            or rows != committed[0].rows):
        raise ValueError(
            f"{field_name} requires one committed output and must equal its row count")


class RunStatus(Wire):
    run_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    # ``run`` materializes a graph result; ``profile`` is a whole-dataset inspection job.
    # Both share one durable status/cancel/recovery lifecycle, but consumers must not mistake a
    # profile completion for a newly materialized node result.
    job_type: Literal["run", "profile"] = "run"
    target_node_id: str | None = None   # the run's sink — lets a reattaching client re-bind the run to its node
    target_port_id: str | None = Field(default=None, min_length=1, max_length=128)
    rows_processed: int = 0
    total_rows: int | None = None
    ms: int = 0
    placement: Placement = "local"
    per_node: list[PerNodeStatus] = []
    progress: float | None = None       # 0..1 fraction of steps complete (deterministic; any backend can report)
    stalled: bool = False               # running but no step has completed for a while (a soft "stuck?" hint)
    error: str | None = None
    # Ordinary runs publish the declaration-ordered expected port set from their first live status.
    # Backends that cannot preserve the complete set reject multi-output targets before allocation.
    # Profile jobs are inspection jobs and deliberately keep this collection empty.
    outputs: list[RunOutput] = Field(default_factory=list, max_length=64)
    # A profile result is present only on a successful full-profile job. ``plan_digest`` is the fixed-size
    # SHA-256 of the server-authoritative execution/source identity, so durable status never duplicates
    # the raw graph while still fencing results to the exact data revision that was profiled.
    profile: ProfileResult | None = None
    plan_digest: PlanDigest | None = None
    # Monotonic per-canvas submission order allocated by the metadata DB. Recovery uses it instead of
    # host clocks or random run ids; the parent stamps it on statuses and workers cannot choose it.
    profile_attempt_order: int | None = Field(default=None, ge=1)
    # HTTP/WebSocket request id that started this run (OPS-01). Optional so legacy/plugin backends
    # that omit it still deserialize; durable copy also lives on run_states / run_records.
    request_id: str | None = None
    backend_ref: RunBackendRef | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_singular_output_fields(cls, value):
        if isinstance(value, dict) and any(
                key in value for key in ("output_uri", "outputUri", "output_table", "outputTable")):
            raise ValueError("singular run output fields are not part of the public contract")
        return value

    @model_validator(mode="after")
    def _output_collection(self) -> "RunStatus":
        keys = [(output.node_id, output.port_id) for output in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError("run outputs must have unique (nodeId, portId) identities")
        if self.job_type == "profile":
            if self.outputs:
                raise ValueError("profile jobs cannot publish run outputs")
            if self.total_rows is not None:
                raise ValueError("profile jobs report result rows only through profile.rowCount")
            durable_identity = self.plan_digest is not None or self.profile_attempt_order is not None
            if durable_identity and (
                    self.target_node_id is None or self.target_port_id is None):
                raise ValueError("profile jobs require target node and output port identities")
            if (self.profile is not None and self.target_port_id is not None
                    and self.profile.target_port_id != self.target_port_id):
                raise ValueError("profile result target port must match the durable job identity")
        if self.job_type == "run":
            if self.target_port_id is not None:
                raise ValueError("ordinary runs do not have one profile target port")
            if self.outputs and self.target_node_id is None:
                raise ValueError("run outputs require a targetNodeId")
            if self.target_node_id is not None and any(
                    output.node_id != self.target_node_id for output in self.outputs):
                raise ValueError("every run output nodeId must match targetNodeId")
            if self.status == "done" and self.target_node_id is not None:
                if not self.outputs or any(
                        output.outcome != "committed" for output in self.outputs):
                    raise ValueError(
                        "a successful targeted run requires committed outputs")
            validate_run_output_rows(
                self.outputs, self.total_rows, field_name="totalRows")
            if self.status in ("done", "failed", "cancelled") and any(
                    output.outcome == "pending" for output in self.outputs):
                raise ValueError("a terminal run cannot retain pending outputs")
        return self


class RunHistoryRecord(Wire):
    """Bounded, explicit response model for one durable run-history row."""

    id: str
    run_id: str | None = None
    request_id: str | None = None
    job_type: Literal["run", "profile"]
    status: Literal["done", "failed", "cancelled"]
    target_node_id: str | None = None
    target_port_id: str | None = Field(default=None, min_length=1, max_length=128)
    rows: int | None = Field(default=None, ge=0)
    ms: int | None = Field(default=None, ge=0)
    error: str | None = None
    input_manifest: list[dict[str, str]] | None = None
    # Null is an explicit legacy/non-reconstructable outcome. #504 adds the authorized detail surface;
    # readers must never substitute the current Canvas when this identity is absent.
    execution_manifest_sha256: PlanDigest | None = None
    execution_manifest_schema_version: int | None = Field(default=None, ge=1)
    execution_manifest_availability: Literal[
        "available", "pruned", "not_recorded", "unavailable", "corrupt",
    ] = "not_recorded"
    execution_manifest_reconstructable: bool = False
    outputs: list[RunOutput] = Field(default_factory=list, max_length=64)
    # Full-profile jobs have no materialized outputs. Keep their measured profile as a separate,
    # bounded history payload instead of abusing result-output fields.
    profile: ProfileResult | None = None
    per_node: list[PerNodeStatus] | None = None
    created_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_singular_output_fields(cls, value):
        if isinstance(value, dict) and any(
                key in value for key in ("output_uri", "outputUri", "output_table", "outputTable")):
            raise ValueError("singular run-history output fields are not part of the public contract")
        return value

    @model_validator(mode="after")
    def _unique_outputs(self) -> "RunHistoryRecord":
        if self.execution_manifest_reconstructable != (
                self.execution_manifest_availability == "available"):
            raise ValueError("run history reconstructability must match manifest availability")
        if self.execution_manifest_sha256 is None and self.execution_manifest_schema_version is not None:
            raise ValueError("run history manifest schema requires a manifest identity")
        if (self.execution_manifest_availability == "available"
                and (self.execution_manifest_sha256 is None
                     or self.execution_manifest_schema_version is None)):
            raise ValueError("available run history requires manifest identity and schema")
        if (self.execution_manifest_availability == "not_recorded"
                and self.execution_manifest_sha256 is not None):
            raise ValueError("unrecorded run history cannot have a manifest identity")
        keys = [(output.node_id, output.port_id) for output in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError("run-history outputs must have unique (nodeId, portId) identities")
        if any(output.outcome == "pending" for output in self.outputs):
            raise ValueError("finished run history cannot retain pending outputs")
        if self.job_type == "profile":
            if self.outputs or self.rows is not None:
                raise ValueError("profile history stores result rows only in the profile status")
            if self.target_node_id is None or self.target_port_id is None:
                raise ValueError("profile history requires target node and output port identities")
            if self.status == "done" and self.profile is None:
                raise ValueError("successful profile history requires a profile result")
            if self.status != "done" and self.profile is not None:
                raise ValueError("unsuccessful profile history cannot carry a profile result")
            if (self.profile is not None
                    and self.profile.target_port_id != self.target_port_id):
                raise ValueError("profile history result target port does not match its job")
        else:
            if self.target_port_id is not None:
                raise ValueError("ordinary run history cannot carry a profile target port")
            if self.profile is not None:
                raise ValueError("run history cannot carry a profile result")
            if self.outputs and self.target_node_id is None:
                raise ValueError("run-history outputs require a targetNodeId")
            if self.target_node_id is not None and any(
                    output.node_id != self.target_node_id for output in self.outputs):
                raise ValueError("every history output nodeId must match targetNodeId")
            if self.status == "done" and self.target_node_id is not None:
                if not self.outputs or any(
                        output.outcome != "committed" for output in self.outputs):
                    raise ValueError(
                        "successful targeted run history requires committed outputs")
            validate_run_output_rows(self.outputs, self.rows, field_name="history rows")
        return self


class ExecutionManifestDetail(Wire):
    """One subject-authorized immutable execution-manifest read."""

    sha256: PlanDigest | None = None
    schema_version: int | None = Field(default=None, ge=1)
    availability: Literal[
        "available", "pruned", "not_recorded", "unavailable", "corrupt",
    ]
    document: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _availability_matches_document(self) -> "ExecutionManifestDetail":
        if (self.document is not None) != (self.availability == "available"):
            raise ValueError("only an available execution manifest can include its document")
        if self.sha256 is None and self.schema_version is not None:
            raise ValueError("execution manifest schema requires an identity")
        if self.availability == "not_recorded" and self.sha256 is not None:
            raise ValueError("an unrecorded execution manifest cannot have an identity")
        if self.availability == "available" and (
                self.sha256 is None or self.schema_version is None):
            raise ValueError("an available execution manifest requires identity and schema")
        return self


class DurableTaskAttemptView(Wire):
    id: str
    attempt_number: int = Field(ge=1)
    execution_manifest_sha256: PlanDigest | None = None
    execution_manifest_reconstructable: bool = False
    status: Literal["queued", "running", "done", "failed", "cancelled", "fenced"]
    progress: float | None = Field(default=None, ge=0, le=1)
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str


class DurableExternalWaitView(Wire):
    provider_kind: str = Field(min_length=1, max_length=64)
    phase: Literal["unsubmitted", "submitting", "accepted", "running", "provider_succeeded",
                   "downloading", "downloaded", "publishing", "published", "provider_failed",
                   "provider_cancelled", "finalization_failed", "cancelled_before_submit",
                   "cancelled_after_success"]
    next_poll_at: str
    deadline_at: str
    poll_count: int = Field(ge=0, le=64)
    attempt_number: int = Field(ge=1, le=3)
    cancel_requested: bool
    can_retry: bool
    diagnostic_code: str | None = Field(default=None, max_length=64)


class DurableCheckpointView(Wire):
    """Sanitized, path-free checkpoint projection for Workspace Jobs."""

    phase: Literal["pending", "materializing", "committed", "publishing", "terminal"]
    checkpoint_node_id: str = Field(min_length=1, max_length=256)
    output_port_id: str = Field(min_length=1, max_length=128)
    committed_at: str | None = None
    rows: int | None = Field(default=None, ge=0)
    bytes: int | None = Field(default=None, ge=0)
    content_digest: str | None = Field(default=None, max_length=64)
    resume_eligible: bool = False
    retry_label: str | None = Field(default=None, max_length=64)
    client_key: str = Field(min_length=1, max_length=128)
    diagnostic_code: str | None = Field(default=None, max_length=64)


class DurableBoundedFanoutView(Wire):
    """Sanitized parent-only bounded fan-out projection for Workspace Jobs."""

    stage: Literal[
        "checkpointing", "planning", "running_partitions", "gathering",
        "publishing", "terminal",
    ]
    partition_count: int | None = Field(default=None, ge=1, le=4)
    completed_partitions: int = Field(ge=0, le=4)
    failed_partitions: int = Field(ge=0, le=4)
    checkpoint: Literal["pending", "committed", "reused"]
    gather: Literal["pending", "running", "committed"]
    diagnostic_code: str | None = Field(default=None, max_length=64)


class DurableMergeColumnsView(Wire):
    """Sanitized exact-candidate merge projection for Workspace Jobs."""

    phase: Literal[
        "validating", "merging", "candidate_committed", "publishing",
        "done", "failed", "cancelled",
    ]
    base_dataset_id: str = Field(min_length=1, max_length=128)
    base_revision_id: str = Field(min_length=1, max_length=128)
    candidate: Literal["pending", "committed"]
    reused: bool
    candidate_rows: int | None = Field(default=None, ge=0)
    candidate_bytes: int | None = Field(default=None, ge=0)
    candidate_digest: str | None = Field(default=None, min_length=12, max_length=12)
    can_retry: bool
    can_cancel: bool
    diagnostic_code: str | None = Field(default=None, max_length=64)


class DurableDistributionReportView(Wire):
    """Sanitized exact-report identity and coverage summary for Workspace Jobs."""

    report_id: str = Field(min_length=32, max_length=32)
    dataset_view_id: str = Field(min_length=1, max_length=32)
    computation_version: str = Field(min_length=1, max_length=64)
    measured_rows: int | None = Field(default=None, ge=0)
    complete: bool | None = None
    reported_column_count: int | None = Field(default=None, ge=0, le=64)
    deep_link: str = Field(min_length=1, max_length=256)


class RestoreRevisionRequestV1(Wire):
    """One intent to publish a retained revision's contents as a new head."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    submission_id: str = Field(min_length=1, max_length=128)
    expected_head_revision_id: str = Field(min_length=1, max_length=256)

    @field_validator("submission_id")
    @classmethod
    def _submission_id(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("submissionId must be trimmed and NUL-free")
        return value.lower()


class RestoreRevisionTaskV1(Wire):
    """Owner-scoped status of one durable restore-as-new-head Task."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    task_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    source_dataset_id: str = Field(min_length=1, max_length=128)
    source_revision_id: str = Field(min_length=1, max_length=256)
    expected_head_revision_id: str = Field(min_length=1, max_length=256)
    child_revision_id: str | None = Field(default=None, min_length=1, max_length=256)
    diagnostic_code: str | None = Field(default=None, max_length=64)
    receipt: WriteReceipt | None = None


class DurableTaskDatasetContextView(Wire):
    """Dataset revision-history subject for a canvas-less durable Task in Jobs / Inbox."""

    task_kind: Literal["restore_revision_write", "keyed_upsert_write"]
    dataset_id: str = Field(min_length=1, max_length=128)
    name: str | None = None


class DurableTaskInboxItemView(Wire):
    """One personal Inbox outcome for a terminal certified durable TaskAttempt."""

    id: str
    task_id: str
    canvas_id: str | None = None
    canvas_name: str | None = None
    dataset_context: DurableTaskDatasetContextView | None = None
    task_kind: Literal[
        "managed_local_write", "external_wait", "linear_checkpoint_write",
        "bounded_fanout_write", "merge_columns_write",
        "restore_revision_write", "keyed_upsert_write",
    ]
    execution_manifest_sha256: PlanDigest | None = None
    execution_manifest_reconstructable: bool = False
    outcome: Literal["completed", "failed", "cancelled"]
    diagnostic_code: str | None = Field(default=None, max_length=64)
    terminal_at: datetime.datetime
    read_at: datetime.datetime | None = None
    job_available: bool

    @model_validator(mode="after")
    def _manifest_identity_matches_reconstructability(self) -> "DurableTaskInboxItemView":
        if self.execution_manifest_reconstructable != (
                self.execution_manifest_sha256 is not None):
            raise ValueError("Inbox reconstructability must match its manifest identity")
        if self.dataset_context is not None and self.canvas_id is not None:
            raise ValueError("a canvas-less dataset Inbox item cannot also carry a canvas")
        return self

    @model_serializer(mode="wrap")
    def _omit_null_dataset_context(self, handler):
        # Canvas-scoped items stay byte-identical: the dataset subject is present only for the
        # canvas-less restore/upsert kinds.
        data = handler(self)
        if self.dataset_context is None:
            data.pop("datasetContext", None)
        return data


class DurableTaskInboxPage(Wire):
    items: list[DurableTaskInboxItemView]
    next_cursor: str | None = None
    has_more: bool


class DurableTaskInboxUnreadCount(Wire):
    count: int = Field(ge=0)


class WorkspaceRunRecord(Wire):
    """One visible run in the workspace-wide, read-only Jobs projection."""

    id: str
    run_id: str | None = None
    request_id: str | None = None
    job_type: Literal["run", "profile", "distribution_report"]
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    target_node_id: str | None = None
    target_port_id: str | None = Field(default=None, min_length=1, max_length=128)
    rows: int | None = Field(default=None, ge=0)
    ms: int | None = Field(default=None, ge=0)
    progress: float | None = Field(default=None, ge=0, le=1)
    error: str | None = None
    input_manifest: list[dict[str, str]] | None = None
    execution_manifest_sha256: PlanDigest | None = None
    execution_manifest_schema_version: int | None = Field(default=None, ge=1)
    execution_manifest_availability: Literal[
        "available", "pruned", "not_recorded", "unavailable", "corrupt",
    ] = "not_recorded"
    execution_manifest_reconstructable: bool = False
    outputs: list[RunOutput] = Field(default_factory=list, max_length=64)
    profile: ProfileResult | None = None
    per_node: list[PerNodeStatus] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    canvas_id: str | None = None
    canvas_name: str | None = None
    node_label: str | None = None
    backend: str
    placement: Placement
    attempt: str
    task_id: str | None = None
    task_attempts: list[DurableTaskAttemptView] = Field(default_factory=list, max_length=16)
    cancel_requested: bool = False
    can_retry: bool = False
    can_cancel: bool = False
    write_intent: WriteIntent | None = None
    output_receipt: WriteReceipt | None = None
    external_wait: DurableExternalWaitView | None = None
    checkpoint: DurableCheckpointView | None = None
    bounded_fanout: DurableBoundedFanoutView | None = None
    merge_columns: DurableMergeColumnsView | None = None
    distribution_report: DurableDistributionReportView | None = None
    dataset_context: DurableTaskDatasetContextView | None = None

    @model_validator(mode="after")
    def _unique_workspace_outputs(self) -> "WorkspaceRunRecord":
        if self.execution_manifest_reconstructable != (
                self.execution_manifest_availability == "available"):
            raise ValueError("workspace run reconstructability must match manifest availability")
        if self.execution_manifest_sha256 is None and self.execution_manifest_schema_version is not None:
            raise ValueError("workspace run manifest schema requires a manifest identity")
        if (self.execution_manifest_availability == "available"
                and (self.execution_manifest_sha256 is None
                     or self.execution_manifest_schema_version is None)):
            raise ValueError("available workspace run requires manifest identity and schema")
        if (self.execution_manifest_availability == "not_recorded"
                and self.execution_manifest_sha256 is not None):
            raise ValueError("unrecorded workspace run cannot have a manifest identity")
        keys = [(output.node_id, output.port_id) for output in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError("workspace run outputs must have unique (nodeId, portId) identities")
        if self.status in ("done", "failed", "cancelled") and any(
                output.outcome == "pending" for output in self.outputs):
            raise ValueError("terminal workspace runs cannot retain pending outputs")
        if self.job_type == "distribution_report":
            if self.distribution_report is None or self.canvas_id is not None:
                raise ValueError("distribution report Jobs rows require their report projection only")
        elif self.distribution_report is not None:
            raise ValueError("ordinary Jobs rows cannot carry a distribution report projection")
        if self.dataset_context is not None and self.canvas_id is not None:
            raise ValueError("a dataset-scoped Jobs row cannot also carry a canvas")
        return self

    @model_serializer(mode="wrap")
    def _omit_null_checkpoint(self, handler):
        # Nested task projections are omitted when absent so other run kinds stay compact.
        data = handler(self)
        if self.checkpoint is None:
            data.pop("checkpoint", None)
        if self.bounded_fanout is None:
            data.pop("boundedFanout", None)
        if self.merge_columns is None:
            data.pop("mergeColumns", None)
        if self.distribution_report is None:
            data.pop("distributionReport", None)
        if self.dataset_context is None:
            data.pop("datasetContext", None)
        return data


class WorkspaceRunPage(Wire):
    items: list[WorkspaceRunRecord]
    next_cursor: str | None = None
    has_more: bool


class PlanStep(Wire):
    node_id: str
    kind: str
    mode: str | None = None
    previewable: bool = True
    label: str
    op: str = ""  # the engine-neutral IR op (hub.ir) — lets ExecutionBackend.can_run gate on the clean subset


class CompilePlan(Wire):
    target_node_id: str | None = None
    steps: list[PlanStep] = []
    acyclic: bool = True
    error: str | None = None


class ResourceSpec(Wire):
    """A compute-resource shape, used BOTH ways: a worker advertises its `capacity`, a step declares
    its `requires`. A worker satisfies a step when its capacity ⊇ the requirement (hub.placement,
    Phase C). All fields optional — an empty spec means "no particular requirement / unspecified"."""
    cpu: float | None = None       # cores
    mem: str | None = None         # e.g. "64GB"
    gpu: int | None = None         # gpu count
    gpu_type: str | None = None    # e.g. "a100"
    labels: dict[str, str] = {}


class WorkerInfo(Wire):
    """One execution slot in a backend — a pod, a process, or the local host (the backend decides
    which). `capacity` is what it advertises; Phase C's scheduler matches a step's requires against it."""
    id: str
    capacity: ResourceSpec = ResourceSpec()
    state: Literal["idle", "busy", "down"] = "idle"


class BackendInfo(Wire):
    """An execution backend and the workers it currently offers (powers the real Compute view — the
    honest replacement for the hardcoded `warm`)."""
    name: str
    workers: list[WorkerInfo] = []


class CapabilityView(Wire):
    """A plugin capability that contributes a VIEWER TAB, declaratively. `viewer.kind` names a generic
    renderer the SPA ships (e.g. 'grid' = media/image grid, 'json' = pretty-printed cell) — so a plugin
    adds a viewer tab (for columns it tags via its detector) with NO frontend code, the same way a
    NodeSpec renders a node card. See kernel/hub/plugins/capabilities.py + web/src/nodes/capabilities."""
    id: str
    label: str
    viewer: dict[str, Any]  # {kind: str, ...} — the generic frontend renderer + its params


class KernelInfo(Wire):
    mode: Literal["local", "distributed"] = "local"
    backend: str = "duckdb"
    warm: bool = True
    version: str = "0.1.0"
    adapters: list[str] = []
    runners: list[str] = []
    processors: list[str] = []
    capabilities: list[str] = []
    capability_views: list[CapabilityView] = []  # plugin capabilities that declare a viewer tab (additive)
    backends: list[BackendInfo] = []  # real backend/worker topology + capacities (additive; runners kept)


class ProcessorDescriptor(Wire):
    id: str
    version: str
    title: str
    mode: ProcessorMode
    category: str = "processor"
    input_columns: list[str] = []
    input_schema: list[ColumnSchema] = []
    output_schema: list[ColumnSchema] = []
    requirements: list[str] = []
    params_schema: dict[str, Any] = {}
    previewable: bool = True
    blurb: str = ""
    provenance: Literal["plugin", "promoted"] = "plugin"
    creator_id: str | None = None
    created_at: datetime.datetime | None = None
    semantic_digest: str | None = None


TransformAvailability = Literal["active", "deleted", "missing"]


class TransformRetention(Wire):
    canvas: int = 0
    canvas_version: int = 0
    execution_manifest: int = 0


class TransformLibraryEntry(ProcessorDescriptor):
    """Public metadata for one exact Transform version; executable code is never exposed."""

    availability: TransformAvailability = "active"
    deleted_at: datetime.datetime | None = None
    version_count: int = 1
    retention: TransformRetention = TransformRetention()


class TransformLibraryPage(Wire):
    items: list[TransformLibraryEntry]
    next_cursor: str | None = None
    has_more: bool = False


class TransformLibraryDetail(Wire):
    id: str
    provenance: Literal["plugin", "promoted"]
    requested_version: str | None = None
    versions: list[TransformLibraryEntry]


class CanvasTransformReference(Wire):
    id: str
    version: str
    node_ids: list[str]
    availability: TransformAvailability
    descriptor: ProcessorDescriptor | None = None


# --------------------------------------------------------------------------- #
# Canvas graph
# --------------------------------------------------------------------------- #
class Position(Wire):
    x: float
    y: float


# SEC-10: bound graph complexity + per-node code/SQL so one request can't carry a runaway blob or a
# pathological graph. Generous vs. any real canvas; raise via a new release if a real workload needs more.
MAX_GRAPH_NODES = 5000
MAX_GRAPH_EDGES = 10000
MAX_CODE_LEN = 200_000  # chars, per code/SQL field on a node


class GraphNode(Wire):
    # RunOutput snapshots persist this identity with the same bound. Enforce it at graph ingress so a
    # structurally valid graph cannot fail only after execution output initialization.
    id: str = Field(min_length=1, max_length=256)
    type: str
    position: Position = Position(x=0, y=0)
    data: dict[str, Any] = {}
    parent_id: str | None = None  # visual containment: this node lives inside a section (its parent)

    @field_validator("id")
    @classmethod
    def _canonical_node_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("graph node id cannot contain surrounding whitespace")
        return value

    @field_validator("data")
    @classmethod
    def _cap_embedded_code(cls, v):
        if isinstance(v, dict):
            cfg = v.get("config")
            if isinstance(cfg, dict):
                for key in ("code", "sql"):
                    val = cfg.get(key)
                    if isinstance(val, str) and len(val) > MAX_CODE_LEN:
                        raise ValueError(f"node {key} exceeds the {MAX_CODE_LEN}-char limit")
                if "datasetRef" in cfg:
                    value = cfg["datasetRef"]
                    if not (isinstance(value, dict) and set(value) == {"parameterRef"}):
                        _DATASET_REF_ADAPTER.validate_python(value)
        return v


class GraphEdgeData(Wire):
    wire: WireType = "dataset"


class GraphEdge(Wire):
    id: str
    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None
    data: GraphEdgeData = GraphEdgeData()


class ParameterConstraints(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = Field(default=None, ge=0, le=4096)
    max_length: int | None = Field(default=None, ge=0, le=4096)

    @model_validator(mode="after")
    def _ordered_bounds(self) -> "ParameterConstraints":
        if any(value is not None and not math.isfinite(value)
               for value in (self.minimum, self.maximum)):
            raise ValueError("parameter numeric constraints must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum cannot exceed maximum")
        if (self.min_length is not None and self.max_length is not None
                and self.min_length > self.max_length):
            raise ValueError("parameter minLength cannot exceed maxLength")
        return self


class ParameterDeclaration(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    type: Literal["string", "integer", "float", "boolean", "date", "datetime", "dataset"]
    required: bool = False
    default: Any | None = None
    label: str | None = Field(default=None, max_length=128)
    help: str | None = Field(default=None, max_length=1024)
    constraints: ParameterConstraints | None = None

    @model_validator(mode="after")
    def _valid_declaration(self) -> "ParameterDeclaration":
        if self.required and self.default is not None:
            raise ValueError("a required parameter cannot also declare a default")
        limits = self.constraints
        if limits is not None:
            numeric = self.type in ("integer", "float")
            textual = self.type == "string"
            if (not numeric and (limits.minimum is not None or limits.maximum is not None)
                    or not textual and (limits.min_length is not None
                                           or limits.max_length is not None)):
                raise ValueError(f"parameter constraints are incompatible with type '{self.type}'")
        if self.default is None:
            return self
        value = self.default
        from hub.secrets import is_registered_secret_ref
        if self.type == "string":
            if not isinstance(value, str):
                raise ValueError("string parameter default must be a string")
            if is_registered_secret_ref(value):
                raise ValueError("parameter defaults cannot contain a SecretRef")
            if limits and limits.min_length is not None and len(value) < limits.min_length:
                raise ValueError("string parameter default is shorter than minLength")
            if limits and limits.max_length is not None and len(value) > limits.max_length:
                raise ValueError("string parameter default is longer than maxLength")
        elif self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_SAFE_INTEGER:
                raise ValueError("integer parameter default must be a safe integer")
        elif self.type == "float":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))):
                raise ValueError("float parameter default must be finite")
        elif self.type == "boolean":
            if not isinstance(value, bool):
                raise ValueError("boolean parameter default must be a boolean")
        elif self.type == "date":
            if not isinstance(value, str):
                raise ValueError("date parameter default must be YYYY-MM-DD")
            try:
                datetime.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date parameter default must be YYYY-MM-DD") from exc
        elif self.type == "datetime":
            if not isinstance(value, str):
                raise ValueError("datetime parameter default must include a timezone")
            try:
                parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("datetime parameter default must include a timezone") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("datetime parameter default must include a timezone")
        elif (not isinstance(value, dict)
              or value.get("kind") not in ("exact", "latest")
              or not isinstance(value.get("datasetId"), str)
              or not value["datasetId"]
              or (value["kind"] == "exact" and (
                  set(value) != {"kind", "datasetId", "revisionId"}
                  or not isinstance(value.get("revisionId"), str) or not value["revisionId"]))
              or (value["kind"] == "latest" and set(value) != {"kind", "datasetId"})):
            raise ValueError("dataset parameter default must be an exact or latest DatasetRef")
        elif self.type == "dataset" and (is_registered_secret_ref(value["datasetId"])
                                         or is_registered_secret_ref(value.get("revisionId"))):
            raise ValueError("dataset parameter defaults cannot contain a SecretRef")
        if self.type in ("integer", "float") and limits:
            numeric_value = float(value)
            if limits.minimum is not None and numeric_value < limits.minimum:
                raise ValueError("numeric parameter default is below minimum")
            if limits.maximum is not None and numeric_value > limits.maximum:
                raise ValueError("numeric parameter default is above maximum")
        return self


class ParameterBinding(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    value: Any


class Graph(Wire):
    id: str = Field(default="canvas", min_length=1, max_length=512)
    version: int = Field(default=1, ge=0, le=MAX_SAFE_INTEGER)
    nodes: Annotated[list[GraphNode], Field(max_length=MAX_GRAPH_NODES)] = []
    edges: Annotated[list[GraphEdge], Field(max_length=MAX_GRAPH_EDGES)] = []
    requirements: list[str] = []  # pip specs the canvas needs; the kernel installs them + allows importing them
    parameters: Annotated[list[ParameterDeclaration], Field(max_length=128)] = []
    # Parent-owned provenance for synthetic region ref-sources. PrivateAttr keeps this control-plane
    # metadata out of the client wire model, workload serialization, and user-controlled node data.
    _publication_source_uris: dict[str, tuple[str, ...]] = PrivateAttr(default_factory=dict)
    _input_artifact_uris: dict[str, str] = PrivateAttr(default_factory=dict)
    _publication_run_id: str | None = PrivateAttr(default=None)
    _publication_attempt_id: str | None = PrivateAttr(default=None)
    _publication_producer_id: str | None = PrivateAttr(default=None)
    _publication_producer_version: int | None = PrivateAttr(default=None)
    # Parent-minted execution identity. Private attrs keep the manifest out of worker payloads while
    # allowing every existing status/history callback to attach the same durable reference.
    _execution_manifest_sha256: str | None = PrivateAttr(default=None)
    _execution_manifest_doc: str | None = PrivateAttr(default=None)
    _parameter_bindings: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @field_validator("parameters")
    @classmethod
    def _unique_parameter_names(cls, value: list[ParameterDeclaration]):
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("canvas parameter names must be unique")
        return value

    @model_validator(mode="after")
    def _valid_parameter_references(self) -> "Graph":
        declarations = {item.name: item for item in self.parameters}

        def visit(value: Any, *, node: GraphNode, path: tuple[str, ...]) -> None:
            if isinstance(value, dict) and "parameterRef" in value:
                if set(value) != {"parameterRef"} or not isinstance(value["parameterRef"], str):
                    raise ValueError("parameterRef sentinel must contain only one string name")
                name = value["parameterRef"]
                declaration = declarations.get(name)
                if declaration is None:
                    raise ValueError(f"canvas config references undeclared parameter '{name}'")
                is_dataset_ref = (
                    node.type == "source" and path == ("datasetRef",)
                )
                if declaration.type == "dataset" and not is_dataset_ref:
                    raise ValueError(
                        f"dataset parameter '{name}' must be the complete datasetRef of a Source")
                if is_dataset_ref and declaration.type != "dataset":
                    raise ValueError(
                        f"Source datasetRef parameter '{name}' must have type 'dataset'")
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, node=node, path=(*path, str(key)))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, node=node, path=(*path, str(index)))

        for node in self.nodes:
            config = node.data.get("config") if isinstance(node.data, dict) else None
            if isinstance(config, dict):
                visit(config, node=node, path=())
        return self

    @field_validator("id")
    @classmethod
    def _canonical_graph_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("graph id cannot contain surrounding whitespace")
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _strict_graph_version(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("graph version must be an integer")
        return value


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class SampleRequest(Wire):
    uri: str
    k: int = 50
    columns: list[str] | None = None
    offset: int = 0


class ImportRequest(Wire):
    config: str
    params: dict[str, Any] | None = None


class CompileRequest(Wire):
    graph: Graph
    target_node_id: str | None = None
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class PreviewRequest(Wire):
    model_config = ConfigDict(extra="forbid")

    graph: Graph
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    k: int | None = None  # None → fall back to settings.preview_k (DP_PREVIEW_K); an explicit int wins
    offset: int = 0
    # Reusing this binding makes pagination/refresh read the same preview population. Omitting it is
    # the explicit request to resolve a new latest binding.
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class InputDriftRequest(Wire):
    """Compare a retained preview binding with current provider heads without changing either."""

    graph: Graph
    target_node_id: str
    input_manifest: list[dict[str, str]]
    parameter_bindings: list[ParameterBinding] = []


class InputDriftSource(Wire):
    node_id: str
    dataset_id: str
    preview_revision_id: str
    latest_revision_id: str | None = None
    old_revision_readable: bool
    compatibility: SchemaCompatibility | None = None


class InputDrift(Wire):
    drifted: bool
    sources: list[InputDriftSource] = []


class ProfileEstimateRequest(Wire):
    """Estimate a whole-dataset profile before the user chooses whether to submit it."""
    graph: Graph
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class ProfileIdentityRequest(Wire):
    """Compute the current server identity for one node without starting work."""
    graph: Graph
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class ProfileJobRequest(Wire):
    """Submit a whole-dataset profile through the durable job lifecycle.

    ``plan_digest`` is minted by the server preflight and checked again at submission. The fixed wire
    value is persisted so a late result cannot be presented for a newer graph or source revision without
    duplicating the submitted graph in durable status rows.
    """
    graph: Graph
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    plan_digest: PlanDigest
    submission_id: UUID4
    confirmed: bool = False
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class EstimateRequest(Wire):
    graph: Graph
    target_node_id: str | None = None
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class RunRequest(Wire):
    graph: Graph
    target_node_id: str | None = None
    confirmed: bool = False
    # A browser retains this UUID across a response-loss retry.  The hub derives the durable run
    # identity from it rather than treating a repeated POST as another dispatch.
    submission_id: UUID4 | None = None
    # A full run launched from a preview admits the preview's exact Source set instead of resolving
    # mutable heads again. The server validates graph coverage and reopens every revision.
    input_manifest: list[dict[str, str]] | None = None
    # The default local Write card obtains this frozen, side-effect-free admission before execution.
    # The server revalidates it against the submitted graph and current destination head.
    write_intent: WriteIntent | None = None
    parameter_bindings: list[ParameterBinding] = []


class WriteAdmissionRequest(Wire):
    graph: Graph
    node_id: str
    submission_id: UUID4
    input_manifest: list[dict[str, str]] | None = None
    parameter_bindings: list[ParameterBinding] = []


class WriteAdmission(Wire):
    node_id: str
    managed: bool
    destination: str
    mode: Literal["create", "replace", "overwrite", "append"]
    provider: str
    expected_schema: list[ColumnSchema] = Field(default_factory=list, max_length=1024)
    partitions: list[WritePartitionExpectation] = Field(default_factory=list, max_length=32)
    expected_head: ExactDatasetRef | None = None
    intent: WriteIntent | None = None
    recovered_receipt: WriteReceipt | None = None
    blocker: str | None = Field(default=None, max_length=4096)
