"""Metadata store — users, canvases (per-user files), and settings.

A small SQLAlchemy layer, separate from `db.py` (which is the DuckDB data engine). Dev uses a
bundled SQLite file; deployment points DP_DATABASE_URL at Postgres. Only the connection string is
config; all metadata lives in this instance's DB. Per-user authentication is implemented in
`hub.auth` + `current_user` (signed session cookies gated by DP_AUTH_SECRET, verifying each
user's own scrypt password hash); with no secret set, an open X-DP-User dev mode defaults to a
seeded local user.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String,
    Text, UniqueConstraint, and_, create_engine, exists, func, or_, select, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from hub.settings import settings

DEFAULT_USER_ID = "local"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # per-user credential (auth mode)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # gates global settings + user management
    token_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # bump → revoke sessions
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Canvas(Base):
    __tablename__ = "canvases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String, default="untitled")
    version: Mapped[int] = mapped_column(Integer, default=1)
    doc: Mapped[str] = mapped_column(Text, default="{}")  # the full CanvasDoc as JSON
    visibility: Mapped[str] = mapped_column(String, default="private")  # 'private' | 'workspace' (edit) | 'workspace_view' (read-only)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CanvasShare(Base):
    """An explicit collaborator on a canvas (beyond the owner)."""
    __tablename__ = "canvas_shares"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String, default="editor")  # 'editor' | 'viewer' — never 'owner'
    # keep this in lockstep with migration 0015; ownership is by Canvas.owner_id alone, never a share
    __table_args__ = (
        UniqueConstraint("canvas_id", "user_id", name="uq_share"),
        CheckConstraint("role IN ('editor', 'viewer')", name="ck_share_role"),
    )


class RunRecord(Base):
    """A finished run, kept with its canvas (run history survives restarts). One row per run."""
    __tablename__ = "run_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    # The runner's real id links durable history back to the logical run. Nullable for records written
    # before migration 0018; `id` remains the history row's own primary key.
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_table: Mapped[str | None] = mapped_column(String, nullable=True)
    output_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    per_node: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: durable per-node breakdown
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("canvas_id", "run_id", name="uq_run_record_canvas_run"),)


class CanvasVersion(Base):
    """A point-in-time snapshot of a canvas doc, for restore-after-a-bad-edit. Auto-captured (throttled)
    on save, plus explicit named snapshots. One row per snapshot; oldest auto-snapshots are pruned."""
    __tablename__ = "canvas_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    doc: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String, nullable=True)  # set for explicit named snapshots
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RunState(Base):
    """Live / last-known status of a run, keyed by run_id, so GET /run/{id} + the status WebSocket are
    served from the shared DB: ANY (stateless) web instance can answer, and status survives a kernel
    restart instead of 404-ing. Distinct from RunRecord (the per-canvas run HISTORY). One row per run.
    (This is the run-state half of making the web tier stateless — see hub.deps.)"""
    __tablename__ = "run_states"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    canvas_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, index=True)  # queued | running | done | failed | cancelled
    doc: Mapped[str] = mapped_column(Text)  # the full RunStatus as JSON
    kernel_id: Mapped[str | None] = mapped_column(String, nullable=True)  # owning kernel (None = in-process/subprocess run, dies with the hub)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)  # the run's creator uid (durable owner, for authz)
    auth_canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the real canvas it was authorized against (None = ad-hoc)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class RunBackendJob(Base):
    """Durable binding between one logical run and one external backend attempt.

    ``run_id`` and ``(backend, submission_id)`` are unique, so concurrent submitters converge on the same
    attempt. The publication lease elects one reattaching supervisor to register terminal outputs/history;
    another supervisor can take over only after the lease expires.
    """
    __tablename__ = "run_backend_jobs"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    backend: Mapped[str] = mapped_column(String, index=True)
    cluster_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_id: Mapped[str] = mapped_column(String)
    submission_id: Mapped[str] = mapped_column(String)
    job_uri: Mapped[str] = mapped_column(Text)
    result_uri: Mapped[str] = mapped_column(Text)
    code_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    control_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_state: Mapped[str] = mapped_column(String, default="queued", server_default="queued")
    submission_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    submission_lease_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    publication_state: Mapped[str] = mapped_column(String, default="pending")
    publication_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_control_observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("backend", "submission_id", name="uq_run_backend_submission"),)


class RunTerminalFence(Base):
    """Compact permanent identity fence; terminal status/history detail is retained separately."""
    __tablename__ = "run_terminal_fences"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ActiveBackendJobsError(RuntimeError):
    """A canvas cannot be deleted while external work can still produce side effects."""


class TerminalRunIdError(RuntimeError):
    """A completed logical run id cannot be rebound after its retained detail is pruned."""


class SchemaContract(Base):
    """A named, versioned schema contract — a workspace artifact multiple pipelines reference by name
    (a node's config.outputSchema = {"ref": name}) instead of each carrying a private inline copy.
    Saving under an existing name mints a new version, so drift is a diff between versions."""
    __tablename__ = "schema_contracts"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)  # JSON: [{name, type}, ...]
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Kernel(Base):
    """Lease for a per-canvas execution kernel (a long-lived process, or a pod). `canvas_id` PK makes
    the INSERT the single-spawner claim; `kernel_id` fences a replaced/zombie kernel out of
    heartbeating / dropping the row / writing run status — the guard the whole no-split-brain
    invariant rests on. Any hub reaches a kernel by resolving canvas_id → endpoint (no sticky LB)."""
    __tablename__ = "kernels"
    canvas_id: Mapped[str] = mapped_column(String, primary_key=True)
    kernel_id: Mapped[str] = mapped_column(String)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    token: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)  # starting | ready
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CatalogEntry(Base):
    """A registered dataset / written output, shared across instances. The in-memory catalog write-throughs
    here on register and loads from here on read, so a dataset registered on one (stateless) web instance
    is visible to the others + survives a restart without re-probing. Keyed by uri; `doc` is the full
    CatalogTable (incl. probed schema) as JSON so no re-probe is needed to serve it.

    The `tbl_id`, `folder`, and `owner` columns are a filterable/sortable PROJECTION of `doc` (the doc
    stays authoritative). Promoting + indexing them is what lets browse/search/facet PUSH DOWN to the
    DB — the catalog answers a filtered page with an indexed query instead of loading every row into
    memory and filtering in Python (the old O(n)-per-read model that didn't scale past a few hundred)."""
    __tablename__ = "catalog_entries"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    doc: Mapped[str] = mapped_column(Text)  # the full CatalogTable as JSON
    tbl_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # CatalogTable.id (stable browse id)
    folder: Mapped[str] = mapped_column(String, default="", server_default="", index=True)  # browse-path namespace
    owner: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # promoted for sort/display
    usage: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)  # read-count popularity signal
    logical_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, index=True)


class CatalogTag(Base):
    """One (dataset uri → tag) membership. A join table (not a JSON blob on the entry) so a tag filter
    / a tag facet is an indexed query, and two instances tagging concurrently can't clobber each other."""
    __tablename__ = "catalog_tags"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (Index("ix_catalog_tags_tag", "tag"),)


class CatalogColumn(Base):
    """One (dataset uri → column name) pair, mirrored from the probed schema. Powers 'has column X'
    filtering + a column facet + full-text over column names without cracking every doc's JSON."""
    __tablename__ = "catalog_columns"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    column: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (Index("ix_catalog_columns_column", "column"),)


class CatalogEmbedding(Base):
    """A dataset's semantic embedding (over name + description + columns), for semantic/hybrid search.
    Written only when an embedder is registered (reg.add_embedder) — the vector is opaque float32
    bytes + its dim, so the store stays engine-neutral and the search does cosine in the catalog."""
    __tablename__ = "catalog_embeddings"
    catalog_key: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String)
    dim: Mapped[int] = mapped_column(Integer)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CatalogEdge(Base):
    """A lineage edge (parent uri → child uri), shared like CatalogEntry so lineage is cross-instance.
    `column` records column-level provenance when known (which output column derived from the input)."""
    __tablename__ = "catalog_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent: Mapped[str] = mapped_column(String, index=True)
    child: Mapped[str] = mapped_column(String, index=True)
    column: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (UniqueConstraint("parent", "child", name="uq_catalog_edge"),)


class CatalogPublicationEvent(Base):
    """One durable catalog effect from an idempotent external-run publication."""
    __tablename__ = "catalog_publication_events"
    event_key: Mapped[str] = mapped_column(String, primary_key=True)
    effect_type: Mapped[str] = mapped_column(String, default="usage")
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ResultCache(Base):
    """Content-addressed result index: a run plan's content hash → where its output landed (uri /
    table / rows / fmt, as JSON). Persisted + shared so a completed run's output is REUSED across
    kernel restarts AND across stateless web instances — the old in-process dict was per-process and
    lost on restart. Not authoritative data: a miss just recomputes, so it's safe to prune (newest N)."""
    __tablename__ = "result_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)  # {uri, table, rows, fmt}
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LocalResultArtifact(Base):
    """One exact built-in local full-result file with a fenced writer lifecycle."""
    __tablename__ = "local_result_artifacts"
    uri: Mapped[str] = mapped_column(Text, primary_key=True)
    namespace_id: Mapped[str] = mapped_column(String, nullable=False)
    storage_root: Mapped[str] = mapped_column(Text, nullable=False)
    lock_name: Mapped[str] = mapped_column(String, nullable=False)
    lock_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lock_protected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true")
    state: Mapped[str] = mapped_column(String, nullable=False, default="writing", server_default="writing")
    writer_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    writer_token: Mapped[str | None] = mapped_column(String, nullable=True)
    delete_token: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    committed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "namespace_id", "lock_name",
            name="uq_local_result_artifact_namespace_lock"),
        CheckConstraint(
            "state IN ('writing', 'ready', 'deleting')",
            name="ck_local_result_artifact_state"),
        CheckConstraint(
            "((writer_run_id IS NULL AND writer_token IS NULL) OR "
            "(writer_run_id IS NOT NULL AND writer_token IS NOT NULL))",
            name="ck_local_result_artifact_writer_pair"),
        CheckConstraint(
            "((lock_protected AND lock_token IS NOT NULL) OR "
            "(NOT lock_protected AND lock_token IS NULL))",
            name="ck_local_result_artifact_lock_pair"),
        CheckConstraint(
            "((state = 'deleting' AND delete_token IS NOT NULL "
            "AND delete_attempted_at IS NOT NULL) OR "
            "(state <> 'deleting' AND delete_token IS NULL "
            "AND delete_attempted_at IS NULL))",
            name="ck_local_result_artifact_delete_state"),
        CheckConstraint(
            "state <> 'ready' OR committed_at IS NOT NULL",
            name="ck_local_result_artifact_ready_commit"),
        Index(
            "ix_local_result_artifacts_reclaim", "namespace_id", "state",
            "delete_attempted_at", "created_at"),
        Index("ix_local_result_artifacts_writer", "writer_run_id", "writer_token"),
    )


class LocalResultReference(Base):
    """One exact durable owner or temporary reader of a managed local full result."""
    __tablename__ = "local_result_references"
    uri: Mapped[str] = mapped_column(
        Text, ForeignKey("local_result_artifacts.uri"), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (
        Index("ix_local_result_references_owner", "owner_kind", "owner_key"),)


class LocalResultRegistry(Base):
    """Singleton transaction lock for local artifact and reference mutations."""
    __tablename__ = "local_result_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    lock_cursor_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    reclaim_cursor_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_local_result_registry_singleton"),)


class InstallationIdentity(Base):
    """One durable identity for every hub instance sharing this metadata database."""
    __tablename__ = "installation_identity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    storage_namespace: Mapped[str] = mapped_column(String, nullable=False)
    storage_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_installation_identity_singleton"),
        UniqueConstraint("owner_token", name="uq_installation_identity_owner_token"),
        UniqueConstraint("storage_namespace", name="uq_installation_identity_storage_namespace"),
    )


class ObjectAttempt(Base):
    """Authoritative lifecycle registry for immutable object-store write attempts.

    Object storage holds only data and commit markers. Publication ownership, logical replacement, and
    bounded GC selection live here so every hub instance observes one transactionally ordered state.
    """
    __tablename__ = "object_attempts"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    allocation_key: Mapped[str] = mapped_column(String, nullable=False)
    storage_namespace: Mapped[str] = mapped_column(String, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_uri: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    logical_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    catalog_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publish_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="writing", server_default="writing", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gc_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_proof_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quiet_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inventory_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    inventory_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    inventory_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    delete_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    delete_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    delete_lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_delete_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_empty_observations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    delete_empty_observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("kind IN ('region', 'sink')", name="ck_object_attempt_kind"),
        CheckConstraint(
            "state IN ('allocated', 'writing', 'committed', 'published', 'superseded', "
            "'abandoned', 'delete_pending', 'deleting', 'delete_verifying', 'deleted', 'quarantined')",
            name="ck_object_attempt_state",
        ),
        UniqueConstraint("allocation_key", "generation", name="uq_object_attempt_allocation_generation"),
        UniqueConstraint(
            "logical_id", "catalog_epoch", "publish_seq",
            name="uq_object_attempt_logical_publication"),
        Index("ix_object_attempts_gc", "state", "gc_attempted_at", "retired_at", "created_at", "uri"),
        Index("ix_object_attempts_sink_target", "kind", "logical_uri", "state"),
        Index("ix_object_attempts_eligibility", "state", "quiet_until", "next_delete_at", "created_at"),
    )


class ObjectAttemptAllocation(Base):
    """Current generation for one durable allocation key; terminal retry advances this pointer."""
    __tablename__ = "object_attempt_allocations"
    allocation_key: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_uri: Mapped[str] = mapped_column(String, ForeignKey("object_attempts.uri"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ObjectAttemptRef(Base):
    """One durable owning pointer to an immutable attempt generation."""
    __tablename__ = "object_attempt_refs"
    ref_type: Mapped[str] = mapped_column(String, primary_key=True)
    ref_key: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_uri: Mapped[str] = mapped_column(String, ForeignKey("object_attempts.uri"), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ObjectAttemptLease(Base):
    """DB-clock lease that fences readers, writers, and exact-key deleters."""
    __tablename__ = "object_attempt_leases"
    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_uri: Mapped[str] = mapped_column(String, ForeignKey("object_attempts.uri"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_type: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        CheckConstraint(
            "lease_type IN ('read', 'write', 'publish', 'delete')",
            name="ck_object_attempt_lease_type"),
        Index("ix_object_attempt_leases_active", "attempt_uri", "lease_type", "expires_at"),
    )


class ObjectAttemptInventory(Base):
    """Exact provider member identity; one object key may own many versions and uploads."""
    __tablename__ = "object_attempt_inventory"
    attempt_uri: Mapped[str] = mapped_column(
        String, ForeignKey("object_attempts.uri"), primary_key=True)
    member_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    member_type: Mapped[str] = mapped_column(String, nullable=False)
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    is_commit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "member_type IN ('object_version', 'delete_marker', 'multipart_upload', "
            "'unversioned_object')", name="ck_object_attempt_inventory_member_type"),
    )


class ObjectStorageClaim(Base):
    """Provider-side conditional ownership marker mirrored for one storage namespace."""
    __tablename__ = "object_storage_claims"
    storage_namespace: Mapped[str] = mapped_column(String, primary_key=True)
    storage_scope: Mapped[str] = mapped_column(String, primary_key=True)
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    marker_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class CatalogLogicalDataset(Base):
    """Stable catalog identity and governance, independent from a physical attempt URI."""
    __tablename__ = "catalog_logical_datasets"
    logical_id: Mapped[str] = mapped_column(String, primary_key=True)
    catalog_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    logical_uri: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    current_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    current_publish_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0")
    next_publish_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0")
    catalog_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    state: Mapped[str] = mapped_column(String, nullable=False, default="active", server_default="active")
    governance_doc: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    metadata_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    __table_args__ = (
        CheckConstraint("state IN ('active', 'unregistered')", name="ck_catalog_logical_state"),
    )


class Setting(Base):
    __tablename__ = "settings"
    # scope 'global' (scope_id='') for system settings; scope 'user' (scope_id=user id) for prefs
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String, default="")
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded
    __table_args__ = (UniqueConstraint("scope", "scope_id", "key", name="uq_setting"),)


class CatalogRelationship(Base):
    """An owner-declared join relationship, ONE ROW each (keyed by an orientation-insensitive rel_key)
    — not a single JSON blob, so two instances declaring different relationships can't clobber each
    other (each add/remove touches only its own row). `doc` is the full Relationship model_dump."""
    __tablename__ = "catalog_relationships"
    rel_key: Mapped[str] = mapped_column(String, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)


class CatalogDeclaredKey(Base):
    """An owner-declared primary key, ONE ROW per stable catalog key (columns as JSON) — same
    per-row isolation as CatalogRelationship (no shared-blob lost update)."""
    __tablename__ = "catalog_declared_keys"
    catalog_key: Mapped[str] = mapped_column(String, primary_key=True)
    columns: Mapped[str] = mapped_column(Text)  # JSON list of column names


_engine = None
_Session = None
_sqlite_memory_migration_lock = threading.RLock()


def _database_url():
    return make_url(settings.database_url)


def _is_sqlite_database() -> bool:
    return _database_url().get_backend_name() == "sqlite"


def _sqlite_is_memory_or_temporary() -> bool:
    """Whether this process owns the complete lifetime of the SQLite database.

    ``sqlite://`` and ``:memory:`` have no cross-process identity. SQLite URI databases using
    ``mode=memory`` are likewise process-local even when they have a name, so an in-process lock is
    the strongest meaningful serialization for them.
    """
    url = _database_url()
    database = url.database
    query = {str(key).lower(): str(value).lower() for key, value in url.query.items()}
    uri = query.get("uri") in ("1", "true", "yes", "on") and bool(
        database and database.startswith("file:"))
    return (
        database in (None, "", ":memory:")
        or bool(uri and (
            query.get("mode") == "memory"
            or database.startswith("file::memory:")
        ))
    )


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings.database_url
        sqlite = _is_sqlite_database()
        kw = {"connect_args": {"check_same_thread": False}} if sqlite else {}
        if sqlite and _sqlite_is_memory_or_temporary():
            # A process-local SQLite DB must use one shared DBAPI connection; otherwise each worker
            # thread gets a different empty database even though migration succeeded on startup.
            from sqlalchemy.pool import StaticPool
            kw["poolclass"] = StaticPool
        _engine = create_engine(url, **kw)
        if sqlite:
            # The bundled default is a local SQLite file, but run status is upserted from daemon runner
            # threads on every per-node step, concurrent with autosave PUTs, catalog writes, and fast
            # GET-run polling. With the default rollback journal + busy_timeout=0 that intermittently
            # raises SQLITE_BUSY ("database is locked") → lost status updates / 500s. WAL lets readers
            # and one writer proceed without blocking each other; busy_timeout makes a contended writer
            # wait-and-retry instead of failing immediately. (No-op for the Postgres deployment path.)
            from sqlalchemy import event

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA busy_timeout=5000")  # ms
                    cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; avoids fsync per commit
                finally:
                    cur.close()
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


class SchemaNotReadyError(RuntimeError):
    """The metadata database cannot safely serve this application revision."""


def _alembic_cfg(connection=None):
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def expected_schema_head() -> str:
    """Return the repository's one valid Alembic head, rejecting a branched migration graph."""
    from alembic.script import ScriptDirectory

    heads = tuple(ScriptDirectory.from_config(_alembic_cfg()).get_heads())
    if len(heads) != 1:
        found = ", ".join(heads) if heads else "none"
        raise SchemaNotReadyError(
            f"metadata migration graph must have exactly one Alembic head; found {found}")
    return heads[0]


def _current_schema_heads(connection=None) -> tuple[str, ...]:
    from alembic.runtime.migration import MigrationContext

    if connection is not None:
        return tuple(MigrationContext.configure(connection).get_current_heads())
    with engine().connect() as current_connection:
        return tuple(MigrationContext.configure(current_connection).get_current_heads())


def schema_at_head() -> bool:
    """Whether the connected database is at this build's exact, unique schema head."""
    try:
        return _current_schema_heads() == (expected_schema_head(),)
    except Exception:  # noqa: BLE001 - readiness is false for an unreachable or malformed DB
        return False


def require_schema_at_head() -> str:
    """Fail closed unless the database is at this build's exact Alembic head; never run DDL."""
    expected = expected_schema_head()
    try:
        current = _current_schema_heads()
    except Exception as exc:
        raise SchemaNotReadyError(f"cannot inspect metadata schema: {exc}") from exc
    if current != (expected,):
        found = ", ".join(current) if current else "unversioned"
        raise SchemaNotReadyError(
            f"metadata schema is not at required Alembic head {expected!r} (current: {found}); "
            "run 'dataplay migrate' as a one-shot release step before starting services")
    return expected


def _sqlite_file_lock_path() -> str | None:
    """Canonical cross-process migration lock for a file-backed SQLite URL.

    The key follows the real resolved database path, not the spelling of ``DP_DATABASE_URL``. That
    makes relative paths, symlinked workspace paths, and SQLite ``file:`` URI forms converge on the
    same lock. Process-local temporary and memory databases deliberately return ``None``.
    """
    if not _is_sqlite_database() or _sqlite_is_memory_or_temporary():
        return None
    database = _database_url().database
    if database is None:
        return None
    query = {str(key).lower(): str(value).lower() for key, value in _database_url().query.items()}
    uri = query.get("uri") in ("1", "true", "yes", "on") and database.startswith("file:")
    if uri:
        uri = urlsplit(database)
        if uri.netloc not in ("", "localhost"):
            raise SchemaNotReadyError(
                f"unsupported SQLite file URI authority {uri.netloc!r}; use a local file path")
        database = unquote(uri.path)
    path = Path(database).expanduser().resolve(strict=False)
    return f"{path}.migrate.lock"


@contextlib.contextmanager
def _sqlite_migration_guard():
    lock_path = _sqlite_file_lock_path()
    if lock_path is None:
        with _sqlite_memory_migration_lock:
            yield
        return

    from filelock import FileLock, Timeout

    lock = FileLock(lock_path, timeout=120)
    try:
        with lock:
            yield
    except Timeout as exc:
        raise SchemaNotReadyError(
            f"timed out waiting for SQLite metadata migration lock {lock_path!r}") from exc


def _bootstrap_metadata() -> None:
    """Seed first-run control-plane data. Call only from the serialized migration path."""
    from hub import auth

    bootstrap = auth.bootstrap_password()
    with session() as s:
        u = s.get(User, DEFAULT_USER_ID)
        if u is None:
            u = User(id=DEFAULT_USER_ID, name="Local", is_admin=True)
            s.add(u)
        elif not u.is_admin and s.query(User).filter(User.is_admin).count() == 0:
            u.is_admin = True
        if os.environ.get("DP_AUTH_SECRET", "").strip() and not u.password_hash and bootstrap:
            u.password_hash = auth.hash_password(bootstrap)
        if os.environ.get("DP_AUTH_SECRET", "").strip():
            login_capable_admin = s.query(User).filter(
                User.is_admin.is_(True),
                User.password_hash.is_not(None),
                User.password_hash != "",
            ).first()
            if login_capable_admin is None:
                raise SchemaNotReadyError(
                    "session auth is enabled but no administrator has a login credential; provide "
                    "DP_AUTH_PASSWORD to the one-shot 'dataplay migrate' command for first bootstrap")
    # The cleartext value is one-shot migration input. Never let a subsequently spawned workload
    # inherit it after the bootstrap transaction commits.
    if bootstrap:
        os.environ.pop("DP_AUTH_PASSWORD", None)


def _validate_migration_auth_inputs() -> None:
    """Reject bootstrap input that cannot produce a usable, securely signed login."""
    from hub import auth

    raw_secret = os.environ.get("DP_AUTH_SECRET")
    if raw_secret is not None and not raw_secret.strip():
        raise SchemaNotReadyError("DP_AUTH_SECRET is configured but blank")
    if raw_secret is not None:
        try:
            auth.reject_weak_secret()
        except RuntimeError as exc:
            raise SchemaNotReadyError(str(exc)) from exc
    if auth.bootstrap_password() and raw_secret is None:
        raise SchemaNotReadyError(
            "DP_AUTH_PASSWORD requires a non-empty DP_AUTH_SECRET; refusing to discard a bootstrap "
            "password while session auth is disabled")


def _require_login_capable_admin() -> None:
    """Fail closed when real session auth has no administrator who can log in."""
    raw_secret = os.environ.get("DP_AUTH_SECRET")
    if raw_secret is None:
        return
    if not raw_secret.strip():
        raise SchemaNotReadyError("DP_AUTH_SECRET is configured but blank")
    with session() as s:
        login_capable_admin = s.query(User).filter(
            User.is_admin.is_(True),
            User.password_hash.is_not(None),
            User.password_hash != "",
        ).first()
    if login_capable_admin is None:
        raise SchemaNotReadyError(
            "session auth is enabled but no administrator has a login credential; run 'dataplay "
            "migrate' with DP_AUTH_PASSWORD to bootstrap the first administrator")


def _upgrade_schema_and_bootstrap() -> str:
    """Run the explicit migration contract on the current database connection."""
    from alembic import command
    from sqlalchemy import inspect

    expected = expected_schema_head()
    with engine().connect() as connection:
        current = _current_schema_heads(connection)
        names = set(inspect(connection).get_table_names())
        if not current and names:
            raise SchemaNotReadyError(
                "refusing to migrate a non-empty metadata database without a valid Alembic "
                "revision; restore a versioned backup or migrate it with an audited conversion")
        if current != (expected,):
            # The inspection queries opened an implicit transaction. End it before Alembic owns the
            # connection's migration transaction.
            connection.rollback()
            try:
                command.upgrade(_alembic_cfg(connection), expected)
            except Exception as exc:
                raise SchemaNotReadyError(f"metadata migration failed: {exc}") from exc
            current = _current_schema_heads(connection)
            if current != (expected,):
                found = ", ".join(current) if current else "unversioned"
                raise SchemaNotReadyError(
                    f"metadata migration did not reach required head {expected!r} (current: {found})")
    _bootstrap_metadata()
    return expected


def migrate_db() -> str:
    """Upgrade metadata explicitly and run bootstrap data writes.

    File-backed SQLite is serialized across processes by a canonical file lock. Production databases
    intentionally have no application-level process lock: operators run this as one stopped-service,
    one-shot release step.
    """
    _validate_migration_auth_inputs()
    if _is_sqlite_database():
        with _sqlite_migration_guard():
            return _upgrade_schema_and_bootstrap()
    return _upgrade_schema_and_bootstrap()


def init_db() -> None:
    """Prepare metadata for a service process without unsafe production DDL.

    Local SQLite keeps the zero-config behavior, but migrations and bootstrap are serialized. Any
    non-SQLite deployment is immutable at service startup: it must already be at the exact unique
    Alembic head produced by ``dataplay migrate``.
    """
    if _is_sqlite_database():
        migrate_db()
    else:
        require_schema_at_head()
        from hub import auth
        if auth.bootstrap_password():
            raise SchemaNotReadyError(
                "DP_AUTH_PASSWORD is accepted only by the one-shot 'dataplay migrate' command; "
                "remove it from the service environment")
        _require_login_capable_admin()
    reap_kernels()        # drop leases whose kernel is dead (stale heartbeat)
    reap_orphaned_runs()  # fail in-flight runs whose owning kernel is gone; live-kernel runs survive (reattach)


@contextlib.contextmanager
def session():
    engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ping(timeout_s: float = 3.0) -> bool:
    """True if the metadata DB answers a trivial query within the budget — the readiness probe's DB check.
    A timeout/failure (DB down, connection pool exhausted, migrations never ran) returns False."""
    import threading
    from sqlalchemy import text
    ok: list[bool] = []

    def _q() -> None:
        try:
            with session() as s:
                s.execute(text("SELECT 1"))
            ok.append(True)
        except Exception:  # noqa: BLE001 — any failure = not-ready
            pass
    t = threading.Thread(target=_q, daemon=True)
    t.start()
    t.join(timeout_s)
    return bool(ok)


def is_admin(user_id: str | None) -> bool:
    """Whether this user may change instance-wide config (global settings, user management)."""
    if not user_id:
        return False
    with session() as s:
        u = s.get(User, user_id)
        return bool(u and u.is_admin)


def resolve_user(user_id: str | None) -> str:
    """Return a valid user id for the request — the header's user if it exists, else the default
    local user (created on demand). Light by design; a real auth layer replaces this later."""
    with session() as s:
        if user_id and s.get(User, user_id) is not None:
            return user_id
        if s.get(User, DEFAULT_USER_ID) is None:
            s.add(User(id=DEFAULT_USER_ID, name="Local"))
        return DEFAULT_USER_ID


def user_auth_snapshot(user_id: str) -> tuple[str | None, int] | None:
    """Read the credential hash and session epoch from one database snapshot."""
    with session() as s:
        row = s.execute(
            select(User.password_hash, User.token_epoch).where(User.id == user_id)
        ).one_or_none()
        return (row.password_hash, row.token_epoch or 0) if row is not None else None


def user_password_hash(user_id: str) -> str | None:
    snapshot = user_auth_snapshot(user_id)
    return snapshot[0] if snapshot is not None else None


def user_auth_snapshot_matches(user_id: str, expected_hash: str | None, expected_epoch: int) -> bool:
    """Confirm that an earlier credential snapshot is still current without holding a scrypt lock."""
    expected_password = (
        User.password_hash.is_(None) if expected_hash is None else User.password_hash == expected_hash
    )
    with session() as s:
        return s.scalar(
            select(User.id).where(
                User.id == user_id,
                expected_password,
                User.token_epoch == expected_epoch,
            )
        ) is not None


def set_user_password(user_id: str, pw_hash: str | None) -> bool:
    """Unconditionally set a credential for explicit provisioning/admin use."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(password_hash=pw_hash, token_epoch=func.coalesce(User.token_epoch, 0) + 1)
        .returning(User.id)
    )
    with session() as s:
        return s.scalar(stmt) is not None


def compare_and_set_user_password(user_id: str, expected_hash: str | None, expected_epoch: int,
                                  pw_hash: str) -> int | None:
    """Rotate a credential only if its exact previously verified value is still current.

    The hash and admission epoch conditions, replacement, and epoch increment are one database
    statement. This keeps request-level scrypt work outside the transaction while making concurrent
    rotation semantics identical on SQLite and PostgreSQL. Return the winning epoch, or ``None`` for a
    stale/revoked/missing user.
    """
    expected = User.password_hash.is_(None) if expected_hash is None else User.password_hash == expected_hash
    stmt = (
        update(User)
        .where(User.id == user_id, expected, User.token_epoch == expected_epoch)
        .values(password_hash=pw_hash, token_epoch=func.coalesce(User.token_epoch, 0) + 1)
        .returning(User.token_epoch)
    )
    with session() as s:
        return s.scalar(stmt)


def user_token_epoch(user_id: str) -> int | None:
    """The user's current session epoch, or None if the user doesn't exist (→ a token for a deleted /
    unknown user fails to verify). Read on each authed request by auth.verify."""
    with session() as s:
        u = s.get(User, user_id)
        return (u.token_epoch or 0) if u is not None else None


def bump_token_epoch(user_id: str) -> None:
    """Invalidate all outstanding sessions for a user (call on disable / delete / forced logout)."""
    with session() as s:
        s.execute(
            update(User)
            .where(User.id == user_id)
            .values(token_epoch=func.coalesce(User.token_epoch, 0) + 1)
        )


def get_setting(key: str, scope: str = "global", scope_id: str = "", default=None):
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        return json.loads(row.value) if row else default


SHARE_ROLES = ("editor", "viewer")  # the ONLY roles a share may grant; ownership is by owner_id alone


def _effective_canvas_role(c: Canvas, uid: str, explicit_role: str | None = None) -> str | None:
    """Resolve one role consistently: owner > explicit share > workspace visibility baseline."""
    if c.owner_id == uid:
        return "owner"
    if explicit_role is not None:
        # A share can NEVER confer ownership. Clamp a legacy/out-of-band value rather than letting it
        # override Canvas.owner_id; this also keeps list_canvases_for aligned with canvas_role.
        return explicit_role if explicit_role in SHARE_ROLES else "viewer"
    if c.visibility == "workspace":
        return "editor"
    if c.visibility == "workspace_view":
        return "viewer"
    return None


def canvas_role(canvas_id: str, uid: str) -> str | None:
    """The user's access to a canvas: 'owner' | 'editor' | 'viewer' | None."""
    with session() as s:
        c = s.get(Canvas, canvas_id)
        if c is None:
            return None
        explicit_role = s.scalar(
            select(CanvasShare.role).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == uid)
        )
        return _effective_canvas_role(c, uid, explicit_role)


def canvas_exists(canvas_id: str) -> bool:
    """True if a canvas row exists (regardless of who can reach it). Used to tell an authorized run
    against a real canvas apart from an ad-hoc, never-saved graph id."""
    with session() as s:
        return s.get(Canvas, canvas_id) is not None


def run_canvas_id(run_id: str) -> str | None:
    """The canvas a run was launched against (from its persisted run_state), or None if the run is
    unknown / was an ad-hoc graph. This is the DB-backed owner signal for run-object authorization."""
    with session() as s:
        r = s.get(RunState, run_id)
        return r.canvas_id if r else None


def bind_run_owner(run_id: str, uid: str, auth_canvas_id: str | None) -> None:
    """Persist a run's creator (authoritative, unspoofable owner) and the real canvas it was authorized
    against (None for an ad-hoc graph). Upserts so it works whether the run_state row exists yet."""
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None:
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            s.add(RunState(run_id=run_id, canvas_id=auth_canvas_id, status="queued", doc="{}",
                           created_by=uid, auth_canvas_id=auth_canvas_id))
        else:
            r.created_by = uid
            r.auth_canvas_id = auth_canvas_id


def run_auth(run_id: str) -> tuple[str | None, str | None]:
    """(creator uid, authorized real-canvas id) for a run, or (None, None) if unknown / a legacy run
    persisted before these columns existed."""
    with session() as s:
        r = s.get(RunState, run_id)
        return (r.created_by, r.auth_canvas_id) if r else (None, None)


def share_canvas(canvas_id: str, user_id: str, role: str = "editor") -> None:
    if role not in SHARE_ROLES:  # reject 'owner' or any junk at the write boundary, not just the API layer
        raise ValueError(f"invalid share role {role!r}; must be one of {SHARE_ROLES}")
    with session() as s:
        sh = s.scalar(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == user_id))
        if sh:
            sh.role = role
        else:
            s.add(CanvasShare(canvas_id=canvas_id, user_id=user_id, role=role))


def unshare_canvas(canvas_id: str, user_id: str) -> None:
    with session() as s:
        sh = s.scalar(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == user_id))
        if sh:
            s.delete(sh)


def list_shares(canvas_id: str) -> list[dict]:
    with session() as s:
        rows = s.execute(
            select(CanvasShare, User.name).join(User, User.id == CanvasShare.user_id)
            .where(CanvasShare.canvas_id == canvas_id)
        ).all()
        return [{"userId": sh.user_id, "name": name, "role": sh.role} for sh, name in rows]


def set_visibility(canvas_id: str, visibility: str) -> None:
    with session() as s:
        c = s.get(Canvas, canvas_id)
        if c:
            c.visibility = visibility


def _canvas_row(c: "Canvas", role: str, shared: bool) -> dict:
    return {"id": c.id, "name": c.name, "version": c.version, "role": role, "shared": shared,
            "visibility": c.visibility, "updatedAt": c.updated_at.isoformat() if c.updated_at else None}


def list_canvases_for(uid: str) -> list[dict]:
    """Canvases a user can see: owned + explicitly shared + workspace-visible (deduped)."""
    with session() as s:
        out: dict[str, dict] = {}
        for c in s.scalars(select(Canvas).where(Canvas.owner_id == uid)):
            out[c.id] = _canvas_row(c, _effective_canvas_role(c, uid), False)
        for c, explicit_role in s.execute(select(Canvas, CanvasShare.role)
                                          .join(CanvasShare, CanvasShare.canvas_id == Canvas.id)
                                          .where(CanvasShare.user_id == uid)).all():
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid, explicit_role), True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid), True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace_view", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid), True))
        return sorted(out.values(), key=lambda r: r["updatedAt"] or "", reverse=True)


_RUN_HISTORY_MAX = 500  # per-canvas run_records cap — bound the local DB (older history is pruned)


def _upsert_run_record(s, *, canvas_id: str | None, target_node_id: str | None, status: str,
                       rows: int | None = None, ms: int | None = None, error: str | None = None,
                       output_table: str | None = None, per_node: list[dict] | None = None,
                       run_id: str | None = None, output_uri: str | None = None) -> bool:
    """Session-scoped history upsert shared by normal completion and backend publication."""
    if not canvas_id:
        return False
    if status != "done" and _local_result_candidate(output_uri) is not None:
        output_uri = None
        output_table = None
    if s.get(Canvas, canvas_id, with_for_update=True) is None:
        return False
    rec = (s.scalar(select(RunRecord).where(
        RunRecord.run_id == run_id, RunRecord.canvas_id == canvas_id
    ).limit(1).with_for_update())
           if run_id else None)
    if rec is None:
        rec = RunRecord(id=_uid(), canvas_id=canvas_id, run_id=run_id)
        s.add(rec)
    rid = rec.id
    rec.target_node_id, rec.status = target_node_id, status
    rec.rows, rec.ms, rec.error = rows, ms, error
    rec.output_table, rec.output_uri = output_table, output_uri
    rec.per_node = json.dumps(per_node, default=str) if per_node else None
    s.flush()
    stale = list(s.scalars(select(RunRecord).where(
        RunRecord.canvas_id == canvas_id, RunRecord.id != rid
    ).order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
      .offset(max(0, _RUN_HISTORY_MAX - 1)).with_for_update()))
    _replace_attempt_ref(s, "run_record", rid, output_uri)
    for obj in stale:
        _replace_attempt_ref(s, "run_record", obj.id, None)
        s.delete(obj)
    if stale:
        _lock_local_result_registry(s)
    sync_local_result_owner(s, "run_record", rid, output_uri)
    for obj in stale:
        _drop_local_result_owner_locked(s, "run_record", obj.id)
    return True


def record_run(canvas_id: str | None, target_node_id: str | None, status: str,
               rows: int | None = None, ms: int | None = None, error: str | None = None,
               output_table: str | None = None, per_node: list[dict] | None = None,
               run_id: str | None = None, output_uri: str | None = None) -> bool:
    """Persist a finished run under its canvas. No-op (returns False) without a real canvas — an ad-hoc
    API run or an internal region sub-run (graph id '_region'). Returns True when a row was written.
    Prunes this canvas's history to the newest _RUN_HISTORY_MAX rows so the local DB can't grow without
    bound (one row per run, forever) — mirrors the ResultCache / CanvasVersion caps."""
    if not canvas_id:
        return False
    if status != "done" and _local_result_candidate(output_uri) is not None:
        output_uri = None
        output_table = None
    with session() as s:
        return _upsert_run_record(
            s, canvas_id=canvas_id, target_node_id=target_node_id, status=status,
            rows=rows, ms=ms, error=error, output_table=output_table, per_node=per_node,
            run_id=run_id, output_uri=output_uri,
        )


def delete_canvas_cascade(canvas_id: str) -> None:
    """Delete a canvas and its children (shares, run history) — FKs don't cascade (SQLite FK off,
    Postgres would error), so clean them explicitly."""
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            return
        active = s.scalar(
            select(RunBackendJob.run_id)
            .join(RunState, RunState.run_id == RunBackendJob.run_id)
            .where(
                or_(RunState.canvas_id == canvas_id, RunState.auth_canvas_id == canvas_id),
                RunState.status.in_(("queued", "running")),
            )
            .order_by(RunBackendJob.run_id).limit(1)
        )
        if active:
            raise ActiveBackendJobsError(
                f"canvas '{canvas_id}' has active external run '{active}'; "
                "cancel it and wait for terminal status"
            )
        shares = list(s.scalars(select(CanvasShare).where(
            CanvasShare.canvas_id == canvas_id
        ).order_by(CanvasShare.user_id).with_for_update()))
        runs = list(s.scalars(select(RunRecord).where(
            RunRecord.canvas_id == canvas_id
        ).order_by(RunRecord.id).with_for_update()))
        versions = list(s.scalars(select(CanvasVersion).where(
            CanvasVersion.canvas_id == canvas_id
        ).order_by(CanvasVersion.id).with_for_update()))
        run_states = list(s.scalars(select(RunState).where(
            (RunState.canvas_id == canvas_id) | (RunState.auth_canvas_id == canvas_id)
        ).order_by(RunState.run_id).with_for_update()))
        local_owners: list[tuple[str, str]] = [("canvas", canvas_id)]
        for sh in shares:
            s.delete(sh)
        for r in runs:
            _replace_attempt_ref(s, "run_record", r.id, None)
            local_owners.append(("run_record", r.id))
            s.delete(r)
        for v in versions:
            local_owners.append(("canvas_version", v.id))
            s.delete(v)
        # also drop this canvas's run_states — else auth_canvas_id/canvas_id dangle into a reusable id
        # namespace and a later canvas re-created under the same id could re-grant its old runs (P0-AUTH-02)
        for rs in run_states:
            job = s.get(RunBackendJob, rs.run_id)
            if job is not None:
                s.delete(job)
            _replace_attempt_ref(s, "run_state", rs.run_id, None)
            local_owners.append(("run_state", rs.run_id))
            s.delete(rs)
        _lock_local_result_registry(s)
        for owner_kind, owner_key in sorted(local_owners):
            _drop_local_result_owner_locked(s, owner_kind, owner_key)
        s.delete(canvas)


def latest_actuals(canvas_id: str | None) -> dict[str, int]:
    """Node measured row counts from recent SUCCESSFUL runs of this canvas — feeds the size estimator (as
    `actuals`) so nodes whose output is statically unknowable (join / aggregate / sql / code) carry a real
    count on the next estimate instead of 'unknown'. The primary source is each run's TARGET count
    (RunRecord.rows — the per_node breakdown leaves a lazy relation's own rows null); any per_node entry
    that DID capture a count (a materialized checkpoint) is folded in too. Scans the last several runs so
    different targets are covered, most-recent wins. The caller guards staleness (only nodes still
    'latest')."""
    if not canvas_id:
        return {}
    out: dict[str, int] = {}
    with session() as s:
        recs = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id, RunRecord.status == "done")
                         .order_by(RunRecord.created_at.desc()).limit(20)).all()
        for r in recs:  # most-recent first — never let an older run overwrite a fresher measurement
            if r.target_node_id and r.rows is not None and r.target_node_id not in out:
                out[r.target_node_id] = int(r.rows)
            if r.per_node:
                try:
                    pn = json.loads(r.per_node)
                except (ValueError, TypeError):
                    pn = []
                for p in pn:
                    if isinstance(p, dict) and p.get("node_id") and p.get("rows") is not None and p["node_id"] not in out:
                        out[p["node_id"]] = int(p["rows"])
    return out


def list_runs(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)
                         .order_by(RunRecord.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "runId": r.run_id, "status": r.status,
                 "targetNodeId": r.target_node_id, "rows": r.rows,
                 "ms": r.ms, "error": r.error, "outputTable": r.output_table,
                 "outputUri": r.output_uri,
                 "perNode": json.loads(r.per_node) if r.per_node else None,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


_RUN_STATE_MAX = 2000  # cap on TERMINAL run_states — live (queued/running) rows are never pruned
_TERMINAL_RUN = ("done", "failed", "cancelled")


class RunStatePublicationRejected(RuntimeError):
    """A definitive owner-row race loss, never an unknown database commit outcome."""


def _terminal_fence_status(s, run_id: str) -> str | None:
    return s.scalar(select(RunTerminalFence.status).where(RunTerminalFence.run_id == run_id))


def _record_terminal_fence(s, run_id: str, status: str) -> None:
    current = _terminal_fence_status(s, run_id)
    if current is not None and current != status:
        raise RuntimeError(
            f"run '{run_id}' is permanently fenced as {current}, not {status}"
        )
    if current is None:
        s.add(RunTerminalFence(run_id=run_id, status=status))
        s.flush()


def save_run_state(run_id: str, status: dict, canvas_id: str | None = None,
                   kernel_id: str | None = None, *, publish_region: bool = False) -> None:
    """Upsert a run's live status (the runner calls this on each transition). `status` is a RunStatus
    model_dump; stored whole as JSON so GET /run/{id} can rebuild it on any instance. `kernel_id`
    stamps the owning kernel so the boot-time reaper fails a run only when its kernel is gone. When a run
    reaches a terminal status, prunes finished run_states to the newest _RUN_STATE_MAX (each row holds a
    full RunStatus JSON, so unbounded growth is a real local-DB leak) — live rows are never touched, so
    the reaper and in-flight status lookups are unaffected; an evicted OLD run just 404s on GET /run/{id}
    (its durable per-canvas history and compact terminal identity fence remain)."""
    status = dict(status)
    st = str(status.get("status", "running"))
    if st != "done" and _local_result_candidate(_result_doc_uri(status)) is not None:
        for key in ("uri", "outputUri", "output_uri", "outputTable", "output_table"):
            status.pop(key, None)
    with session() as s:
        stale_candidate_ids: list[str] = []
        locked: dict[str, RunState] = {}
        if st not in _TERMINAL_RUN:
            # No retention rows participate in a progress update, so the current row can keep the
            # original direct lock. This also serializes a canvas cascade with every live update.
            r = s.get(RunState, run_id, with_for_update=True)
        else:
            existing_was_present = s.get(RunState, run_id) is not None
            if (st == "done" and _local_result_candidate(_result_doc_uri(status)) is not None
                    and not existing_was_present):
                # Local publication must attach to the run identity minted before execution. Upserting
                # a missing row here could resurrect a canvas-deleted run and fabricate its first owner.
                raise RunStatePublicationRejected(
                    "managed local result has no pre-existing run state")
            stale_candidate_ids = list(s.scalars(select(RunState.run_id).where(
                RunState.status.in_(_TERMINAL_RUN), RunState.run_id != str(run_id)
            ).order_by(RunState.updated_at.desc(), RunState.run_id.desc())
              .offset(max(0, _RUN_STATE_MAX - 1))))
            lock_ids = set(stale_candidate_ids)
            if existing_was_present:
                lock_ids.add(str(run_id))
            locked = {row.run_id: row for row in s.scalars(select(RunState).where(
                RunState.run_id.in_(sorted(lock_ids))
            ).order_by(RunState.run_id).with_for_update())} if lock_ids else {}
            r = locked.get(str(run_id))
            if existing_was_present and r is None:
                # A canvas cascade deleted the initially-observed row before this union lock. Never
                # resurrect a dangling RunState or its local-result reference after that deletion.
                raise RunStatePublicationRejected(
                    "run state was deleted before terminal publication")
        payload = json.dumps(status, default=str)
        fenced = _terminal_fence_status(s, run_id)
        if fenced is not None and (r is None or st != fenced):
            return
        if r is None:
            s.add(RunState(run_id=run_id, canvas_id=canvas_id, status=st, doc=payload, kernel_id=kernel_id))
        else:
            # Make terminal monotonicity an UPDATE predicate, not a prior ORM read. A transaction can load
            # queued, pause while another supervisor atomically publishes done, then otherwise flush its
            # stale queued object over the terminal result.
            values = {"status": st, "doc": payload}
            if canvas_id:
                values["canvas_id"] = func.coalesce(RunState.canvas_id, canvas_id)
            if kernel_id:
                values["kernel_id"] = func.coalesce(RunState.kernel_id, kernel_id)
            updated = s.execute(update(RunState).where(
                RunState.run_id == run_id,
                or_(RunState.status.not_in(_TERMINAL_RUN), RunState.status == st),
            ).values(**values))
            if not updated.rowcount:
                return
        s.flush()
        if st in _TERMINAL_RUN:
            _record_terminal_fence(s, run_id, st)
        stale = []
        if st in _TERMINAL_RUN:
            # Re-evaluate age after every candidate lock; delete only rows included in the one
            # deterministic PK-ordered acquisition above. A row refreshed while we waited is retained.
            stale_now = set(s.scalars(select(RunState.run_id).where(
                RunState.status.in_(_TERMINAL_RUN)
            ).order_by(RunState.updated_at.desc(), RunState.run_id.desc())
              .offset(_RUN_STATE_MAX)))
            stale = [locked[key] for key in sorted(stale_now & set(stale_candidate_ids))
                     if key != str(run_id) and key in locked
                     and locked[key].status in _TERMINAL_RUN]
        output_uri = _result_doc_uri(status)
        output_attempt = s.get(ObjectAttempt, output_uri) if output_uri else None
        publish_region = bool(
            publish_region and st == "done"
            and output_attempt is not None and output_attempt.kind == "region")
        _replace_attempt_ref(
            s, "run_state", run_id, output_uri, publish=publish_region)
        if st in _TERMINAL_RUN:
            for obj in stale:
                job = s.get(RunBackendJob, obj.run_id)
                if job is not None:
                    s.delete(job)
                _replace_attempt_ref(s, "run_state", obj.run_id, None)
                s.delete(obj)
            _lock_local_result_registry(s)
        # The RunState transaction is the primary local-result publication boundary. Object attempt
        # locks above always precede the local registry lock.
        sync_local_result_owner(s, "run_state", run_id, status)
        if st == "done":
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=False)
        if st in _TERMINAL_RUN:
            for obj in stale:
                _drop_local_result_owner_locked(s, "run_state", obj.run_id)


def local_result_run_state_receipt(
        run_id: str, namespace_id: str, expected_doc: dict) -> bool:
    """Prove an exact local full-result terminal transaction committed.

    A database driver may raise after PostgreSQL committed.  Callers must not turn that unknown result
    into ``failed`` and delete a now-published artifact.  This read-back receipt validates every part of
    the same publication boundary: byte-for-byte RunState JSON, the exact durable reference, the ready
    artifact in this filesystem namespace, and release of the writer identity.  Connectivity errors are
    intentionally allowed to raise so they can never be mistaken for an authoritative negative answer.
    """
    if not namespace_id or not isinstance(expected_doc, dict):
        raise ValueError("local result receipt requires a namespace and status document")
    expected = dict(expected_doc)
    if str(expected.get("status")) != "done":
        return False
    expected_payload = json.dumps(expected, default=str)
    uri = _local_result_candidate(_result_doc_uri(expected))
    if uri is None:
        return False
    with session() as s:
        state = s.get(RunState, str(run_id), with_for_update=True)
        if (state is None or state.status != "done"
                or state.doc != expected_payload):
            return False
        # Owner rows precede the registry in the global lifecycle lock order.
        _lock_local_result_registry(s)
        ref = s.get(LocalResultReference, {
            "uri": uri, "owner_kind": "run_state", "owner_key": str(run_id),
        })
        artifact = s.get(LocalResultArtifact, uri, with_for_update=True)
        return bool(
            ref is not None and artifact is not None
            and artifact.namespace_id == namespace_id
            and artifact.state == "ready"
            and artifact.writer_run_id is None
            and artifact.writer_token is None)
def get_run_state(run_id: str) -> dict | None:
    """The last-persisted RunStatus dict for a run, or None if unknown to this instance's DB."""
    with session() as s:
        r = s.get(RunState, run_id)
        return json.loads(r.doc) if r else None


def bind_backend_job(run_id: str, ref: dict, status: dict,
                     canvas_id: str | None = None) -> tuple[dict, bool]:
    """Atomically bind a logical run to one external attempt and its recoverable queued state.

    Returns ``(stored_ref, created)``. A caller whose deterministic attempt differs from ``stored_ref``
    must not submit: another request already owns this logical run id. The backend row and ``run_states``
    handoff commit together, so a process cannot die in between and leave a binding recovery cannot join.
    """
    row = RunBackendJob(
        run_id=run_id,
        backend=str(ref["backend"]),
        cluster_ref=str(ref.get("cluster_ref") or "") or None,
        attempt_id=str(ref["attempt_id"]),
        submission_id=str(ref["submission_id"]),
        job_uri=str(ref["job_uri"]),
        result_uri=str(ref["result_uri"]),
        code_ref=str(ref.get("code_ref") or "") or None,
        control_address=str(ref.get("control_address") or "") or None,
        cancel_requested=bool(ref.get("cancel_requested", False)),
        submission_state="queued",
        publication_state="pending",
        # Allocation starts the liveness grace period. Only successful control observations advance it;
        # RunState error writes deliberately do not.
        last_control_observed_at=_now(),
    )
    try:
        with session() as s:
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            s.add(row)
            s.flush()
            # Re-check after the insert fence: a concurrent terminal transaction may have deleted the
            # previous backend detail and committed its permanent run-id fence while this insert waited.
            s.expire_all()
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            state = s.get(RunState, run_id)
            st = str(status.get("status") or "queued")
            payload = json.dumps(status, default=str)
            if state is None:
                s.add(RunState(run_id=run_id, canvas_id=canvas_id, status=st, doc=payload))
            elif state.status not in _TERMINAL_RUN:
                state.status, state.doc = st, payload
                if canvas_id and not state.canvas_id:
                    state.canvas_id = canvas_id
        return backend_job(run_id), True
    except IntegrityError:
        with session() as s:
            fenced = _terminal_fence_status(s, run_id)
        if fenced is not None:
            raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
        existing = backend_job(run_id)
        if existing is None:
            raise
        # Rows created by this version are atomic. This repair also makes a pre-upgrade/manual binding
        # recoverable if it predates that invariant.
        if get_run_state(run_id) is None:
            save_run_state(run_id, status, canvas_id=canvas_id)
        return existing, False


def _backend_job_doc(row: RunBackendJob) -> dict:
    return {
        "run_id": row.run_id,
        "backend": row.backend,
        "cluster_ref": row.cluster_ref,
        "attempt_id": row.attempt_id,
        "submission_id": row.submission_id,
        "job_uri": row.job_uri,
        "result_uri": row.result_uri,
        "code_ref": row.code_ref,
        "control_address": row.control_address,
        "cancel_requested": bool(row.cancel_requested),
        "quarantine_reason": row.quarantine_reason,
        "submission_state": row.submission_state,
        "submission_owner": row.submission_owner,
        "submission_lease_until": (
            row.submission_lease_until.isoformat() if row.submission_lease_until else None
        ),
        "durable": True,
        "publication_state": row.publication_state,
        "last_control_observed_at": (
            row.last_control_observed_at.isoformat() if row.last_control_observed_at else None
        ),
        "recovery_blocked_reason": row.recovery_blocked_reason,
        "result": json.loads(row.result_doc) if row.result_doc else None,
    }


def backend_job(run_id: str) -> dict | None:
    with session() as s:
        row = s.get(RunBackendJob, run_id)
        return _backend_job_doc(row) if row else None


def note_backend_control_observed(
        run_id: str, attempt_id: str, min_interval_s: float = 10.0) -> bool:
    """Throttle the durable liveness clock advanced only by successful backend observations."""
    now = _now()
    cutoff = now - datetime.timedelta(seconds=max(0.0, float(min_interval_s)))
    with session() as s:
        updated = s.execute(update(RunBackendJob).where(
            RunBackendJob.run_id == run_id,
            RunBackendJob.attempt_id == attempt_id,
            or_(RunBackendJob.last_control_observed_at.is_(None),
                RunBackendJob.last_control_observed_at <= cutoff),
        ).values(last_control_observed_at=now))
        return bool(updated.rowcount)


def mark_backend_recovery_blocked(
        run_id: str, backend: str, status: dict, reason: str) -> bool:
    """Persist a non-terminal, operator-visible fence for a malformed active backend row."""
    bounded = str(reason)[:2000]
    payload = json.dumps(status, default=str)
    with session() as s:
        job = s.get(RunBackendJob, run_id)
        state = s.get(RunState, run_id)
        if job is None or state is None or job.backend != backend or state.status not in ("queued", "running"):
            return False
        job.recovery_blocked_reason = bounded
        state.status = str(status.get("status") or state.status)
        state.doc = payload
        return True


def active_backend_jobs(backend: str) -> list[tuple[dict, dict]]:
    """Return ``(backend_ref, RunStatus doc)`` for reattachable non-terminal jobs."""
    out: list[tuple[dict, dict]] = []
    with session() as s:
        rows = s.execute(
            select(RunBackendJob, RunState).join(RunState, RunState.run_id == RunBackendJob.run_id)
            .where(RunBackendJob.backend == backend, RunState.status.in_(("queued", "running")))
        ).all()
        for job, state in rows:
            try:
                doc = json.loads(state.doc)
                if not isinstance(doc, dict):
                    raise TypeError("stored RunStatus is not a JSON object")
            except (TypeError, ValueError):
                doc = {
                    "run_id": state.run_id,
                    "status": state.status,
                    "per_node": [],
                    "_recovery_error": "stored RunStatus is not a valid JSON object",
                }
            out.append((_backend_job_doc(job), doc))
    return out


def request_backend_cancel(run_id: str) -> bool:
    """Persist cancel intent before any process-local event or remote stop request is issued."""
    with session() as s:
        updated = s.execute(
            update(RunBackendJob).where(RunBackendJob.run_id == run_id)
            .values(cancel_requested=True)
        )
        return bool(updated.rowcount)


def request_backend_quarantine(run_id: str, reason: str) -> bool:
    """Persist a contract-corruption fence so restart cannot resume or publish the attempt."""
    with session() as s:
        updated = s.execute(
            update(RunBackendJob).where(RunBackendJob.run_id == run_id)
            .values(quarantine_reason=str(reason)[:4000])
        )
        return bool(updated.rowcount)


def claim_backend_submission_after_missing(run_id: str, attempt_id: str, owner: str,
                                           lease_seconds: float = 30.0) -> str:
    """Linearize or reclaim one submit after Ray status+list authoritatively report it missing.

    A single CAS claims a fresh queued attempt, a metadata-lost submitted attempt, or an expired
    submitting owner. Reclaim never exposes an intermediate queued state where concurrent cancellation
    could forget that an older, already-linearized request may still arrive at Ray.
    """
    with session() as s:
        now = s.scalar(select(func.current_timestamp()))
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.cancel_requested.is_(False),
                RunBackendJob.quarantine_reason.is_(None),
                or_(
                    RunBackendJob.submission_state == "queued",
                    RunBackendJob.submission_state == "submitted",
                    and_(
                        RunBackendJob.submission_state == "submitting",
                        or_(RunBackendJob.submission_lease_until.is_(None),
                            RunBackendJob.submission_lease_until < now),
                    ),
                ),
            ).values(submission_state="submitting", submission_owner=owner,
                     submission_lease_until=lease)
        )
        if updated.rowcount:
            return "claimed"
        row = s.get(RunBackendJob, run_id)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        if row.quarantine_reason:
            return "quarantined"
        if row.cancel_requested:
            return "cancelled"
        return "busy"


def renew_backend_submission(run_id: str, attempt_id: str, owner: str,
                             lease_seconds: float = 30.0) -> bool:
    """Keep a live, already-linearized external submit from being reclaimed mid-request."""
    with session() as s:
        now = s.scalar(select(func.current_timestamp()))
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.submission_state == "submitting",
                RunBackendJob.submission_owner == owner,
            ).values(submission_lease_until=lease)
        )
        return bool(updated.rowcount)


def note_backend_submission_observed(run_id: str, attempt_id: str) -> bool:
    """Persist that Ray authoritatively exposes the deterministic submission ID."""
    with session() as s:
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.submission_state != "stop_fenced",
            ).values(submission_state="submitted", submission_owner=None,
                     submission_lease_until=None)
        )
        return bool(updated.rowcount)


def claim_backend_stop_fence(run_id: str, attempt_id: str, owner: str,
                             lease_seconds: float = 30.0) -> str:
    """Claim an expired uncertain submit so stop intent can reserve its deterministic Ray ID.

    The fixed remote fence job and a delayed original submit race on the same Ray submission ID; only
    one can be accepted. This turns an otherwise unknowable crashed-owner state into stoppable evidence.
    """
    with session() as s:
        now = s.scalar(select(func.current_timestamp()))
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                or_(RunBackendJob.cancel_requested.is_(True),
                    RunBackendJob.quarantine_reason.is_not(None)),
                RunBackendJob.submission_state.in_(("submitting", "fencing")),
                or_(RunBackendJob.submission_lease_until.is_(None),
                    RunBackendJob.submission_lease_until < now),
            ).values(submission_state="fencing", submission_owner=owner,
                     submission_lease_until=lease)
        )
        if updated.rowcount:
            return "claimed"
        row = s.get(RunBackendJob, run_id)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        if not (row.cancel_requested or row.quarantine_reason):
            return "lost"
        if row.submission_state == "queued":
            return "not_needed"
        if row.submission_state in ("submitted", "stop_fenced"):
            return "settled_missing"
        return "busy"


def note_backend_stop_fence_accepted(run_id: str, attempt_id: str) -> bool:
    """Record that the fixed stop fence, rather than the original workload, reserved the Ray ID."""
    with session() as s:
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                or_(RunBackendJob.cancel_requested.is_(True),
                    RunBackendJob.quarantine_reason.is_not(None)),
                RunBackendJob.submission_state == "fencing",
            ).values(submission_state="stop_fenced", submission_owner=None,
                     submission_lease_until=None)
        )
        return bool(updated.rowcount)


def note_unhandled_backend_jobs(available_backends: set[str]) -> int:
    """Keep external runs live but make a missing plugin/configuration visible after process restart."""
    changed = 0
    with session() as s:
        rows = s.execute(
            select(RunBackendJob, RunState).join(RunState, RunState.run_id == RunBackendJob.run_id)
            .where(RunState.status.in_(("queued", "running")))
        ).all()
        for job, state in rows:
            if job.backend in available_backends:
                continue
            try:
                doc = json.loads(state.doc)
            except (TypeError, ValueError):
                doc = {"run_id": state.run_id, "status": state.status, "per_node": []}
            doc["error"] = (
                f"durable backend '{job.backend}' is unavailable in this process; "
                "restore its plugin and control-plane configuration to reattach or cancel"
            )
            state.doc = json.dumps(doc, default=str)
            changed += 1
    return changed


def claim_backend_publication(run_id: str, attempt_id: str, owner: str,
                              lease_seconds: float = 30.0) -> str:
    """Try to become the one terminal-result publisher: claimed | busy | published | lost."""
    with session() as s:
        # Derive every deadline from the metadata DB server's clock. Hub/pod wall-clock skew must not
        # let one supervisor steal a live lease early or write a deadline far into the future.
        now = s.scalar(select(func.current_timestamp()))
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        result = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_state != "published",
                or_(RunBackendJob.publication_owner == owner,
                    RunBackendJob.publication_owner.is_(None),
                    RunBackendJob.publication_lease_until.is_(None),
                    RunBackendJob.publication_lease_until < now),
            ).values(publication_owner=owner, publication_lease_until=lease)
        )
        if result.rowcount:
            return "claimed"
        row = s.get(RunBackendJob, run_id)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        return "published" if row.publication_state == "published" else "busy"


def finish_backend_publication(run_id: str, attempt_id: str, owner: str, result: dict) -> bool:
    """Atomically publish backend evidence, public RunState, and durable run history.

    Catalog projection is performed before this transaction by an idempotent provider. This transaction
    is the terminal visibility barrier: readers can never observe a published backend row paired with a
    stale live RunState or missing SQL run history.
    """
    published = dict(result)
    terminal = str(published.get("status") or "")
    if terminal not in _TERMINAL_RUN:
        raise ValueError("backend publication requires a terminal RunStatus")
    if terminal != "done" and _local_result_candidate(_result_doc_uri(published)) is not None:
        for key in ("uri", "outputUri", "output_uri", "outputTable", "output_table"):
            published.pop(key, None)
    with session() as s:
        canvas_id = s.scalar(select(RunState.canvas_id).where(RunState.run_id == run_id))
        if canvas_id and s.get(Canvas, canvas_id, with_for_update=True) is None:
            return False
        stale_candidate_ids = list(s.scalars(select(RunState.run_id).where(
            RunState.status.in_(_TERMINAL_RUN), RunState.run_id != str(run_id)
        ).order_by(RunState.updated_at.desc(), RunState.run_id.desc())
          .offset(max(0, _RUN_STATE_MAX - 1))))
        lock_ids = sorted({str(run_id), *stale_candidate_ids})
        locked = {row.run_id: row for row in s.scalars(select(RunState).where(
            RunState.run_id.in_(lock_ids)
        ).order_by(RunState.run_id).with_for_update())}
        state = locked.get(str(run_id))
        if state is None:
            return False
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_owner == owner,
                RunBackendJob.publication_state != "published",
            ).values(publication_state="published", publication_owner=None,
                     publication_lease_until=None, result_doc=json.dumps(published, default=str))
        )
        if not updated.rowcount:
            return False
        payload = json.dumps(published, default=str)
        state.status, state.doc = terminal, payload
        s.flush()
        _record_terminal_fence(s, run_id, terminal)
        stale_now = set(s.scalars(select(RunState.run_id).where(
            RunState.status.in_(_TERMINAL_RUN)
        ).order_by(RunState.updated_at.desc(), RunState.run_id.desc())
          .offset(_RUN_STATE_MAX)))
        prune_current = str(run_id) in stale_now
        stale = [locked[key] for key in sorted(stale_now & set(stale_candidate_ids))
                 if key != str(run_id) and key in locked
                 and locked[key].status in _TERMINAL_RUN]
        pruned = [*stale, *([state] if prune_current else [])]
        output_uri = _result_doc_uri(published)
        _replace_attempt_ref(s, "run_state", run_id, output_uri)
        for obj in pruned:
            job = s.get(RunBackendJob, obj.run_id)
            if job is not None:
                s.delete(job)
            _replace_attempt_ref(s, "run_state", obj.run_id, None)
            s.delete(obj)
        per_node = published.get("per_node") or None
        _upsert_run_record(
            s, canvas_id=state.canvas_id, target_node_id=published.get("target_node_id"),
            status=terminal, rows=published.get("total_rows"), ms=published.get("ms"),
            error=published.get("error"), output_table=published.get("output_table"),
            per_node=per_node, run_id=run_id, output_uri=output_uri,
        )
        if pruned:
            _lock_local_result_registry(s)
        if not prune_current:
            sync_local_result_owner(s, "run_state", run_id, published)
        if terminal == "done" and not prune_current:
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=False)
        elif terminal == "done":
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=True)
        for obj in pruned:
            _drop_local_result_owner_locked(s, "run_state", obj.run_id)
        return True


def renew_backend_publication(run_id: str, attempt_id: str, owner: str,
                              lease_seconds: float = 30.0) -> bool:
    """Extend an active publication lease while catalog/history side effects are in flight."""
    with session() as s:
        now = s.scalar(select(func.current_timestamp()))
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_owner == owner,
                RunBackendJob.publication_state != "published",
            ).values(publication_lease_until=lease)
        )
        return bool(updated.rowcount)


def backend_publication_owned(run_id: str, attempt_id: str, owner: str) -> bool:
    """Fence an external side effect immediately before it is issued by a lease holder."""
    with session() as s:
        return s.scalar(select(RunBackendJob.run_id).where(
            RunBackendJob.run_id == run_id,
            RunBackendJob.attempt_id == attempt_id,
            RunBackendJob.publication_state != "published",
            RunBackendJob.publication_owner == owner,
            RunBackendJob.publication_lease_until >= func.current_timestamp(),
        ).limit(1)) is not None


def run_stalled(run_id: str, threshold_s: float) -> bool:
    """True when neither local status progress nor external control liveness was observed recently.

    Durable backend error text updates must not hide a dead control plane, while successful same-state
    polls must keep a healthy long-running job from looking stalled. Local runs retain RunState time.
    """
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None or r.updated_at is None:
            return False
        job = s.get(RunBackendJob, run_id)
        observed_at = (
            job.last_control_observed_at or job.updated_at or r.updated_at
            if job is not None else r.updated_at
        )
        if observed_at is None:
            return False
        return _stale_secs(observed_at) > threshold_s  # normalizes SQLite's naive datetimes


def save_schema_contract(name: str, columns: list[dict]) -> int:
    """Save a named schema contract as a NEW version (max existing + 1). `columns` = [{name, type}, ...].
    Returns the new version number. The max+1 read-then-insert isn't atomic, so a concurrent save of the
    SAME name can collide on the (name, version) PK — retry a few times (each recomputes the next version)."""
    from sqlalchemy import func
    from sqlalchemy.exc import IntegrityError
    doc = json.dumps([{"name": c["name"], "type": c.get("type", "")} for c in columns])
    for _ in range(5):
        try:
            with session() as s:
                cur = s.query(func.max(SchemaContract.version)).filter(SchemaContract.name == name).scalar()
                version = (cur or 0) + 1
                s.add(SchemaContract(name=name, version=version, doc=doc))
            return version
        except IntegrityError:
            continue  # someone else grabbed this version — recompute and retry
    raise RuntimeError(f"could not save schema contract '{name}' after retries (version contention)")


def get_schema_contract(name: str, version: int | None = None) -> dict | None:
    """A contract by name — the latest version, or a specific one. None if unknown."""
    with session() as s:
        q = select(SchemaContract).where(SchemaContract.name == name)
        q = q.where(SchemaContract.version == version) if version is not None \
            else q.order_by(SchemaContract.version.desc()).limit(1)
        r = s.scalars(q).first()
        return {"name": r.name, "version": r.version, "columns": json.loads(r.doc)} if r else None


def list_schema_contracts() -> list[dict]:
    """Every contract's LATEST version (name/version/columns), for the registry view + a reference picker."""
    with session() as s:
        rows = s.scalars(select(SchemaContract).order_by(SchemaContract.name, SchemaContract.version)).all()
        latest: dict[str, dict] = {}
        for r in rows:  # ordered ascending → the last seen per name is the latest
            latest[r.name] = {"name": r.name, "version": r.version, "columns": json.loads(r.doc)}
        return list(latest.values())


def schema_contract_versions(name: str) -> list[int]:
    with session() as s:
        return sorted(v for (v,) in s.query(SchemaContract.version).filter(SchemaContract.name == name).all())


def diff_columns(a: list[dict], b: list[dict]) -> dict:
    """Structural diff of two column lists (contract vs contract, or contract vs actual). Reports columns
    added / removed / whose type changed, going a → b (b is the newer / actual)."""
    am = {c["name"]: str(c.get("type", "")) for c in a}
    bm = {c["name"]: str(c.get("type", "")) for c in b}
    added = [n for n in bm if n not in am]
    removed = [n for n in am if n not in bm]
    changed = [{"name": n, "from": am[n], "to": bm[n]} for n in am if n in bm and am[n] != bm[n]]
    return {"added": added, "removed": removed, "changed": changed,
            "match": not (added or removed or changed)}


_RESULT_CACHE_MAX = 1000  # persistent equivalent of the old in-process _MAX_RUNS cache cap
_INSTALLATION_ID = 1
_OBJECT_ATTEMPT_KINDS = ("region", "sink")
_LOCAL_RESULT_REGISTRY_ID = 1
_LOCAL_RESULT_OWNER_KINDS = {
    "canvas", "canvas_version", "catalog_entry", "result_cache", "run_record", "run_state",
}
_LOCAL_RESULT_EPHEMERAL_OWNER_KIND = "read_lease"
_LOCAL_RESULT_DIR = ".dp-results"
_LOCAL_RESULT_PREFIX = "__result_"
_LOCAL_RESULT_MAX_URI = 4096
_LOCAL_RESULT_QUERY_CHUNK = 200
_WRITABLE_ATTEMPT_STATES = ("allocated", "writing")
_TERMINAL_ATTEMPT_STATES = (
    "superseded", "abandoned", "delete_pending", "deleting", "delete_verifying", "deleted",
    "quarantined",
)
_OBJECT_SCHEMES = ("s3", "r2", "gs", "gcs")
_ATTEMPT_MARKER = ".attempt-"


def _local_result_candidate(value) -> str | None:
    """Cheap shape filter before a value is ever admitted to a lifecycle database query."""
    if not isinstance(value, str) or not value or len(value) > _LOCAL_RESULT_MAX_URI:
        return None
    if "://" in value:
        try:
            scheme = urlsplit(value).scheme.lower()
        except ValueError:
            return None
        if scheme != "file":
            return None
    if _LOCAL_RESULT_PREFIX not in value or _LOCAL_RESULT_DIR not in value:
        return None
    path = value[len("file://"):] if value.startswith("file://") else value
    if "\x00" in path:
        return None
    name = os.path.basename(path)
    if (not name.startswith(_LOCAL_RESULT_PREFIX) or not name.endswith(".parquet")
            or os.path.basename(os.path.dirname(path)) != _LOCAL_RESULT_DIR):
        return None
    return path


def _canvas_local_result_candidates(value) -> set[str]:
    """Extract bounded executable source bindings, including legacy nested section bodies."""
    if not isinstance(value, dict):
        raise ValueError("canvas document must be an object")
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 5000:
        raise ValueError("canvas document has an invalid or oversized node list")
    children: dict[str, list[dict]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        parent = node.get("parentId") or node.get("parent_id")
        if parent:
            children.setdefault(str(parent), []).append(node)
    candidates: set[str] = set()
    queue: list[tuple[dict, int, bool]] = [
        (node, 0, True) for node in nodes if isinstance(node, dict)]
    seen: set[int] = set()
    cursor = 0
    while cursor < len(queue):
        if cursor >= 10_000:
            raise ValueError("canvas section source traversal exceeds the supported node limit")
        node, depth, visual = queue[cursor]
        cursor += 1
        if depth > 64:
            raise ValueError("canvas legacy section nesting exceeds the supported depth")
        if id(node) in seen:
            continue
        seen.add(id(node))
        data = node.get("data") if visual else None
        config = ((data.get("config") if isinstance(data, dict) else None)
                  if visual else node.get("config"))
        if not isinstance(config, dict):
            config = {}
        if node.get("type") == "source":
            candidate = _local_result_candidate(config.get("uri") or config.get("table"))
            if candidate is not None:
                candidates.add(candidate)
        if node.get("type") != "section":
            continue
        direct = children.get(str(node.get("id")), []) if visual else []
        if direct:  # identical precedence to section._collect_subnodes
            queue.extend((child, depth, True) for child in direct)
            continue
        subnodes = config.get("subnodes") or []
        if not isinstance(subnodes, list):
            raise ValueError("canvas legacy section subnodes must be a list")
        queue.extend((child, depth + 1, False)
                     for child in subnodes if isinstance(child, dict))
    return candidates


def _local_result_owner_candidates(owner_kind: str, values: tuple) -> list[str]:
    """Owner-aware exact extraction avoids scanning arbitrary caller-controlled JSON strings."""
    candidates: set[str] = set()
    if owner_kind in ("run_state", "result_cache"):
        for value in values:
            candidate = _local_result_candidate(_result_doc_uri(value))
            if candidate is not None:
                candidates.add(candidate)
    elif owner_kind in ("run_record", "catalog_entry"):
        for value in values:
            candidate = _local_result_candidate(value)
            if candidate is not None:
                candidates.add(candidate)
    elif owner_kind in ("canvas", "canvas_version"):
        for value in values:
            candidates.update(_canvas_local_result_candidates(value))
    else:
        raise ValueError(f"unknown local-result owner kind {owner_kind!r}")
    return sorted(candidates)


def _lock_local_result_registry(s) -> LocalResultRegistry:
    """Serialize local lifecycle changes across PostgreSQL and SQLite processes.

    Callers that also mutate object attempts must acquire all object-attempt locks first. The local
    registry is deliberately the final lifecycle lock in the global order.
    """
    # ORM mutation is not lock order until SQL reaches the database. Flush every pending durable-owner
    # and object-attempt/ref/lease mutation before taking the final local registry lock; no_autoflush
    # below then prevents the registry statement itself from inverting that physical order.
    s.flush()
    with s.no_autoflush:
        result = s.execute(
            update(LocalResultRegistry)
            .where(LocalResultRegistry.id == _LOCAL_RESULT_REGISTRY_ID)
            .values(owner_token=LocalResultRegistry.owner_token),
            execution_options={"autoflush": False},
        )
        row = s.get(LocalResultRegistry, _LOCAL_RESULT_REGISTRY_ID, with_for_update=True)
    if result.rowcount != 1 or row is None:
        raise RuntimeError("local result registry identity is missing")
    return row


def _drop_local_result_owner_locked(s, owner_kind: str, owner_key: str) -> None:
    refs = list(s.scalars(select(LocalResultReference).where(
        LocalResultReference.owner_kind == owner_kind,
        LocalResultReference.owner_key == owner_key,
    ).order_by(LocalResultReference.uri)))
    for ref in refs:
        s.get(LocalResultArtifact, ref.uri, with_for_update=True)
        s.delete(ref)


def _drop_local_result_owner(s, owner_kind: str, owner_key: str) -> None:
    _lock_local_result_registry(s)
    _drop_local_result_owner_locked(s, owner_kind, owner_key)


def sync_local_result_owner(s, owner_kind: str, owner_key: str, *values) -> None:
    """Replace one durable owner's exact local-result references in the owner's transaction."""
    if owner_kind not in _LOCAL_RESULT_OWNER_KINDS:
        raise ValueError(f"unknown local-result owner kind {owner_kind!r}")
    candidates = _local_result_owner_candidates(owner_kind, values)
    if not candidates and s.scalar(select(LocalResultReference.uri).where(
            LocalResultReference.owner_kind == owner_kind,
            LocalResultReference.owner_key == str(owner_key)).limit(1)) is None:
        # The durable owner row/key is already serialized by its caller. Avoid turning every ordinary
        # progress/autosave/catalog write into a global local-registry UPDATE on object-only workloads.
        return
    _lock_local_result_registry(s)
    _drop_local_result_owner_locked(s, owner_kind, str(owner_key))
    artifacts: list[LocalResultArtifact] = []
    for start in range(0, len(candidates), _LOCAL_RESULT_QUERY_CHUNK):
        chunk = candidates[start:start + _LOCAL_RESULT_QUERY_CHUNK]
        artifacts.extend(s.scalars(
            select(LocalResultArtifact).where(LocalResultArtifact.uri.in_(chunk))
            .order_by(LocalResultArtifact.uri).with_for_update()))
    if {artifact.uri for artifact in artifacts} != set(candidates):
        raise RuntimeError("managed local-result owner references an unknown artifact")
    for artifact in artifacts:
        if artifact.state == "deleting":
            raise RuntimeError("managed local result is already being reclaimed")
        if artifact.state != "ready":
            raise RuntimeError("managed local result is not ready for durable publication")
        if artifact.writer_run_id is not None or artifact.writer_token is not None:
            if (owner_kind != "run_state"
                    or str(owner_key) != artifact.writer_run_id):
                # Only the exact writer's RunState transaction may establish the primary ref and clear
                # its writer pair. Secondary owners cannot pin a guessed/provisional URI before that.
                raise RuntimeError(
                    "managed local result must be published by its exact writer run first")
        s.add(LocalResultReference(
            uri=artifact.uri, owner_kind=owner_kind, owner_key=str(owner_key)))


def begin_local_result(uri: str, namespace_id: str, storage_root: str, lock_name: str,
                       lock_protected: bool,
                       run_id: str, writer_token: str, lock_token: str | None) -> None:
    """Reserve a never-reused exact local result before the first file side effect."""
    if not uri or not namespace_id or not storage_root or not lock_name or not run_id or not writer_token:
        raise ValueError(
            "local result uri, namespace, root, lock, run id, and writer token are required")
    if _local_result_candidate(uri) != uri:
        raise ValueError("local result uri is outside the reserved namespace")
    if bool(lock_protected) != bool(lock_token):
        raise ValueError("protected local results require an exact lock token")
    with session() as s:
        _lock_local_result_registry(s)
        existing = s.get(LocalResultArtifact, uri, with_for_update=True)
        if existing is not None:
            if (
                existing.namespace_id == namespace_id
                and existing.storage_root == storage_root
                and existing.lock_name == lock_name
                and existing.lock_token == lock_token
                and bool(existing.lock_protected) == bool(lock_protected)
                and existing.state == "writing"
                and existing.writer_run_id == str(run_id)
                and existing.writer_token == str(writer_token)
                and existing.committed_at is None
                and existing.delete_token is None
                and existing.delete_attempted_at is None
            ):
                # Exact replay after an unknown commit outcome. The registry lock serializes this
                # transaction behind the original one, so returning proves that one reservation won.
                return
            raise RuntimeError("managed local result uri already exists")
        s.add(LocalResultArtifact(
            uri=uri, namespace_id=namespace_id, storage_root=storage_root, lock_name=lock_name,
            lock_token=lock_token,
            lock_protected=bool(lock_protected), state="writing", writer_run_id=str(run_id),
            writer_token=str(writer_token), created_at=_db_now(s)))


def commit_local_result(uri: str, namespace_id: str, run_id: str, writer_token: str,
                        lock_token: str | None) -> None:
    """Fence the ready transition to the writer that reserved this exact file."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if (row is None or row.namespace_id != namespace_id or row.state != "writing"
                or row.writer_run_id != str(run_id)
                or row.writer_token != str(writer_token)
                or bool(row.lock_protected) != bool(lock_token)
                or (lock_token is not None and row.lock_token != lock_token)):
            raise RuntimeError("local result writer lost ownership before commit")
        row.state = "ready"
        row.committed_at = _db_now(s)


def release_local_result_writer(
        uri: str, namespace_id: str, run_id: str, writer_token: str) -> bool:
    """Release a successful process fence only after a durable reference exists."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if row is None:
            return True
        if row.namespace_id != namespace_id:
            raise RuntimeError("local result belongs to a different filesystem namespace")
        if (row.state in ("ready", "deleting")
                and row.writer_run_id is None and row.writer_token is None):
            # Retention may claim an unreferenced artifact after the terminal transaction cleared its
            # writer identity but before this process closes its SH descriptor. Closing that exact
            # process fence is both safe and necessary for the already-claimed delete to obtain EX.
            return True
        if row.writer_run_id != str(run_id) or row.writer_token != str(writer_token):
            raise RuntimeError("local result writer lost ownership before release")
        if row.state != "ready":
            raise RuntimeError("cannot release an uncommitted local result writer")
        if s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == uri).limit(1)) is None:
            return False
        row.writer_run_id = row.writer_token = None
        return True


def _release_terminal_local_result_writers(
        s, run_id: str, *, allow_unreferenced: bool = False) -> None:
    rows = list(s.scalars(select(LocalResultArtifact).where(
        LocalResultArtifact.writer_run_id == str(run_id)).with_for_update()))
    for row in rows:
        if not allow_unreferenced and s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == row.uri).limit(1)) is None:
            continue
        row.writer_run_id = row.writer_token = None


def acquire_local_result_read(
        uri: str, namespace_id: str, lock_name: str, reader_id: str,
        lock_token: str | None) -> bool:
    """Add an ephemeral exact reader ref after its process acquired the shared OS lock."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if (row is None or row.namespace_id != namespace_id
                or row.state != "ready" or row.lock_name != lock_name
                or bool(row.lock_protected) != bool(lock_token)
                or (lock_token is not None and row.lock_token != lock_token)):
            return False
        key = {
            "uri": uri,
            "owner_kind": _LOCAL_RESULT_EPHEMERAL_OWNER_KIND,
            "owner_key": str(reader_id),
        }
        if s.get(LocalResultReference, key) is None:
            s.add(LocalResultReference(**key))
        return True


def local_result_read_active(uri: str, namespace_id: str, reader_id: str) -> bool:
    """Check that the exact ephemeral reader identity is still registered."""
    with session() as s:
        row = s.get(LocalResultArtifact, uri)
        return row is not None and row.namespace_id == namespace_id and s.get(LocalResultReference, {
            "uri": uri,
            "owner_kind": _LOCAL_RESULT_EPHEMERAL_OWNER_KIND,
            "owner_key": str(reader_id),
        }) is not None


def release_local_result_read(uri: str, namespace_id: str, reader_id: str) -> None:
    """Unconditionally release a stopped reader; no replacement durable owner is required."""
    with session() as s:
        _lock_local_result_registry(s)
        key = {
            "uri": uri,
            "owner_kind": _LOCAL_RESULT_EPHEMERAL_OWNER_KIND,
            "owner_key": str(reader_id),
        }
        ref = s.get(LocalResultReference, key)
        if ref is not None:
            row = s.get(LocalResultArtifact, uri, with_for_update=True)
            if row is None or row.namespace_id != namespace_id:
                raise RuntimeError("local result belongs to a different filesystem namespace")
            s.delete(ref)


def local_result_lock_candidates(
        namespace_id: str, *, limit: int = 50) -> list[tuple[str, str, str]]:
    """Bounded, rotating set of process-owned artifacts needing death reconciliation."""
    if limit <= 0:
        return []
    lease_exists = select(LocalResultReference.uri).where(
        LocalResultReference.uri == LocalResultArtifact.uri,
        LocalResultReference.owner_kind == _LOCAL_RESULT_EPHEMERAL_OWNER_KIND,
    ).exists()
    with session() as s:
        registry = _lock_local_result_registry(s)
        predicates = (
            LocalResultArtifact.namespace_id == namespace_id,
            LocalResultArtifact.lock_protected.is_(True),
            LocalResultArtifact.state.in_(("writing", "ready")),
            or_(LocalResultArtifact.writer_run_id.is_not(None), lease_exists),
        )
        cursor = registry.lock_cursor_uri
        rows = list(s.scalars(select(LocalResultArtifact).where(
            *predicates,
            LocalResultArtifact.uri > cursor if cursor else True,
        ).order_by(LocalResultArtifact.uri).limit(limit)))
        if cursor and len(rows) < limit:
            rows.extend(s.scalars(select(LocalResultArtifact).where(
                *predicates, LocalResultArtifact.uri <= cursor,
            ).order_by(LocalResultArtifact.uri).limit(limit - len(rows))))
        registry.lock_cursor_uri = rows[-1].uri if rows else None
        return [(row.uri, row.lock_name, row.lock_token) for row in rows if row.lock_token]


def reconcile_dead_local_result(uri: str, namespace_id: str, lock_name: str) -> None:
    """Clear process fences only while the caller holds the exact exclusive OS lock."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if (row is None or row.namespace_id != namespace_id
                or row.lock_name != lock_name or row.state == "deleting"):
            return
        row.writer_run_id = row.writer_token = None
        for ref in s.scalars(select(LocalResultReference).where(
                LocalResultReference.uri == uri,
                LocalResultReference.owner_kind == _LOCAL_RESULT_EPHEMERAL_OWNER_KIND)):
            s.delete(ref)


def abandon_local_result(
        uri: str, namespace_id: str, run_id: str, writer_token: str) -> str | None:
    """Fence an aborted writer and authorize deletion of only its exact reserved file."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if row is None:
            return None
        if row.namespace_id != namespace_id:
            raise RuntimeError("local result belongs to a different filesystem namespace")
        if row.state == "deleting":
            if (row.writer_run_id == str(run_id)
                    and row.writer_token == str(writer_token)
                    and row.delete_token):
                return row.delete_token
            raise RuntimeError("local result writer lost ownership before abort")
        if row.writer_run_id != str(run_id) or row.writer_token != str(writer_token):
            raise RuntimeError("local result writer lost ownership before abort")
        if s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == uri).limit(1)) is not None:
            raise RuntimeError("cannot abort a referenced local result")
        row.state = "deleting"
        row.delete_token = uuid.uuid4().hex
        row.delete_attempted_at = _db_now(s)
        return row.delete_token


def claim_local_result_reclaims(
        namespace_id: str, *, limit: int = 50,
        prefer_fresh: bool = False) -> list[tuple[str, str, str | None]]:
    """Claim exact unreferenced files whose process locks prove every user stopped."""
    if not namespace_id or limit <= 0:
        return []
    claimed: list[tuple[str, str, str | None]] = []
    with session() as s:
        registry = _lock_local_result_registry(s)
        deleting_budget = (0 if limit == 1 and prefer_fresh
                           else (1 if limit == 1 else max(1, (limit + 1) // 2)))
        deleting = _rotating_deleting_local_results(
            s, registry, namespace_id, deleting_budget, lock_protected=True)
        now = _db_now(s)
        for row in deleting:
            row.delete_token = row.delete_token or uuid.uuid4().hex
            row.delete_attempted_at = now
            claimed.append((row.uri, row.delete_token, row.lock_token))
        remaining = limit - len(claimed)
        no_reference = ~select(LocalResultReference.uri).where(
            LocalResultReference.uri == LocalResultArtifact.uri).exists()
        rows = list(s.scalars(select(LocalResultArtifact).where(
            LocalResultArtifact.namespace_id == namespace_id,
            LocalResultArtifact.lock_protected.is_(True),
            LocalResultArtifact.state.in_(("writing", "ready")),
            LocalResultArtifact.writer_run_id.is_(None), no_reference,
        ).order_by(LocalResultArtifact.created_at, LocalResultArtifact.uri)
          .limit(remaining).with_for_update()))
        for row in rows:
            if s.scalar(select(LocalResultReference.uri).where(
                    LocalResultReference.uri == row.uri).limit(1)) is not None:
                continue
            row.state = "deleting"
            row.writer_run_id = row.writer_token = None
            row.delete_token = uuid.uuid4().hex
            row.delete_attempted_at = now
            claimed.append((row.uri, row.delete_token, row.lock_token))
        if not claimed and deleting_budget == 0:
            # A limit=1 fresh turn should not stall deletion retries when there is no fresh work.
            fallback = _rotating_deleting_local_results(
                s, registry, namespace_id, 1, lock_protected=True)
            if fallback:
                row = fallback[0]
                row.delete_token = row.delete_token or uuid.uuid4().hex
                row.delete_attempted_at = now
                claimed.append((row.uri, row.delete_token, row.lock_token))
    return claimed


def _rotating_deleting_local_results(
        s, registry: LocalResultRegistry, namespace_id: str, limit: int,
        *, lock_protected: bool | None) -> list[LocalResultArtifact]:
    """Select a strict persistent URI rotation independent of database timestamp precision."""
    if limit <= 0:
        return []
    predicates = [
        LocalResultArtifact.namespace_id == namespace_id,
        LocalResultArtifact.state == "deleting",
    ]
    if lock_protected is not None:
        predicates.append(LocalResultArtifact.lock_protected.is_(lock_protected))
    cursor = registry.reclaim_cursor_uri
    rows = list(s.scalars(select(LocalResultArtifact).where(
        *predicates, LocalResultArtifact.uri > cursor if cursor else True,
    ).order_by(LocalResultArtifact.uri).limit(limit).with_for_update()))
    if cursor and len(rows) < limit:
        rows.extend(s.scalars(select(LocalResultArtifact).where(
            *predicates, LocalResultArtifact.uri <= cursor,
        ).order_by(LocalResultArtifact.uri).limit(limit - len(rows)).with_for_update()))
    if rows:
        registry.reclaim_cursor_uri = rows[-1].uri
    return rows


def claim_deleting_local_results(
        namespace_id: str, *, limit: int = 50) -> list[tuple[str, str, str | None]]:
    """Retry only durable explicit-abort deletions, including no-flock platforms.

    A deleting row is terminal proof that its exact writer stopped and no reference existed. Windows
    cannot safely discover or claim fresh abandoned writers, but it can safely finish this already-
    authorized queue after a transient synchronous delete failure.
    """
    if not namespace_id or limit <= 0:
        return []
    with session() as s:
        registry = _lock_local_result_registry(s)
        rows = _rotating_deleting_local_results(
            s, registry, namespace_id, limit, lock_protected=False)
        now = _db_now(s)
        out = []
        for row in rows:
            row.delete_token = row.delete_token or uuid.uuid4().hex
            row.delete_attempted_at = now
            out.append((row.uri, row.delete_token, row.lock_token))
        return out


def delete_local_result(uri: str, namespace_id: str, delete_token: str, delete_file) -> bool:
    """Revalidate a claim, durably unlink data, then commit retirement of its ownership row.

    The caller keeps the exact lock FD open and removes the lock pathname only after this transaction
    commits. Thus a failed commit leaves a retryable deleting row plus its deletion fence.
    """
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if row is None:
            return False
        if (row.namespace_id != namespace_id or row.state != "deleting"
                or row.delete_token != delete_token):
            raise RuntimeError("local result delete claim is stale")
        if s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == uri).limit(1)) is not None:
            raise RuntimeError("cannot delete a referenced local result")
        delete_file()
        s.delete(row)
        return True


def local_result_artifact_absent(
        uri: str, namespace_id: str, lock_name: str, lock_token: str) -> bool:
    """Confirm an exact lock is orphaned after a committed artifact-row deletion."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        if row is None:
            return True
        if (row.namespace_id != namespace_id or row.lock_name != lock_name
                or row.lock_token != lock_token):
            return False
        return False


def local_result_lock_row_absent(
        uri: str, namespace_id: str, lock_name: str) -> bool:
    """Prove a malformed pre-DB lock has no exact lifecycle row before unlinking it."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.scalars(select(LocalResultArtifact).where(or_(
            LocalResultArtifact.uri == uri,
            (LocalResultArtifact.namespace_id == namespace_id)
            & (LocalResultArtifact.lock_name == lock_name),
        )).order_by(LocalResultArtifact.uri).limit(1).with_for_update()).first()
        return row is None


def local_result_uri_absent(uri: str, namespace_id: str) -> bool:
    """Confirm an exact unique result URI has no lifecycle row before temp cleanup."""
    with session() as s:
        _lock_local_result_registry(s)
        row = s.get(LocalResultArtifact, uri, with_for_update=True)
        return row is None


def object_attempt_uri_shape(uri: str | None) -> bool:
    """Recognize an attempt-shaped object URI without treating its shape as ownership proof."""
    if not uri:
        return False
    try:
        parsed = urlsplit(str(uri))
    except ValueError:
        return False
    return (parsed.scheme.lower() in _OBJECT_SCHEMES and bool(parsed.netloc)
            and _ATTEMPT_MARKER in parsed.path.rstrip("/").rsplit("/", 1)[-1])


def object_attempt_namespace_path(uri: str | None) -> bool:
    """Recognize any object URI inside the reserved attempt namespace, including member paths."""
    if not uri:
        return False
    try:
        parsed = urlsplit(str(uri))
    except ValueError:
        return False
    return (parsed.scheme.lower() in _OBJECT_SCHEMES and bool(parsed.netloc)
            and any(_ATTEMPT_MARKER in part for part in parsed.path.split("/") if part))


def _validated_object_uri(uri: str, *, attempt: bool) -> str:
    """Normalize one managed URI and reject authority/query forms that could leak or alias."""
    raw = str(uri).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        invalid_authority = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ValueError("managed object URI is invalid") from exc
    if (parsed.scheme.lower() not in _OBJECT_SCHEMES or not parsed.netloc or invalid_authority
            or parsed.query or parsed.fragment or not parsed.path.strip("/")):
        raise ValueError("managed object URI must use a plain object-store authority and key")
    shaped = _ATTEMPT_MARKER in parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if shaped != attempt:
        required = "attempt" if attempt else "logical target"
        raise ValueError(f"managed object URI is not a valid {required}")
    return raw


def validate_managed_object_uri(uri: str, *, attempt: bool = False) -> str:
    """Pure validation for control-plane callers before any provider or database side effect."""
    return _validated_object_uri(uri, attempt=attempt)


def get_result(key: str) -> dict | None:
    """The stored result pointer ({uri, table, rows, fmt}) for a plan's content hash, or None."""
    with session() as s:
        r = s.get(ResultCache, key)
        return json.loads(r.doc) if r else None


def acquire_result_cache_pin(key: str, owner: str, ttl_seconds: float = 300) -> tuple[dict | None, str | None]:
    """Atomically read the current cache pointer and pin its managed region generation.

    The temporary ref is paired with a DB-time lease: it prevents a concurrent cache replacement from
    superseding the generation before terminal run-state/history refs are durable, while an abandoned
    reader is reaped after its lease expires.
    """
    with session() as s:
        # SQLite ignores FOR UPDATE. A no-op write to THIS cache key upgrades the transaction before the
        # pointer read, so replacement cannot slip between that read and result_reader publication. Do
        # not use the installation singleton here: unrelated cache keys are independent ownership paths.
        if s.get_bind().dialect.name == "sqlite":
            s.execute(
                update(ResultCache)
                .where(ResultCache.key == str(key))
                .values(doc=ResultCache.doc)
            )
        cache = s.get(ResultCache, str(key), with_for_update=True)
        if cache is None:
            return None, None
        try:
            doc = json.loads(cache.doc)
        except (TypeError, ValueError):
            return None, None
        uri = _result_doc_uri(doc)
        attempt = s.get(ObjectAttempt, uri, with_for_update=True) if uri else None
        if attempt is None:
            if uri and object_attempt_uri_shape(uri):
                raise FileNotFoundError("cached object attempt has no lifecycle ownership row")
            return doc, None
        if attempt.kind != "region" or attempt.state != "published":
            raise FileNotFoundError("cached managed result is not currently published")
        pin_id = uuid.uuid4().hex
        _put_lease(s, attempt, "read", str(owner), ttl_seconds, lease_id=pin_id)
        s.add(ObjectAttemptRef(
            ref_type="result_reader", ref_key=pin_id, attempt_uri=attempt.uri,
            generation=attempt.generation,
        ))
        return doc, pin_id


def _db_now(s) -> datetime.datetime:
    """The database server's transaction clock, normalized for SQLite's string result."""
    value = s.scalar(select(func.now()))
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime.datetime):
        raise RuntimeError("metadata database did not return a timestamp")
    return value


def _lock_object_attempt_registry(s) -> InstallationIdentity:
    """Serialize lifecycle pointer swaps across hub instances, including SQLite's no-op FOR UPDATE."""
    result = s.execute(
        update(InstallationIdentity)
        .where(InstallationIdentity.id == _INSTALLATION_ID)
        .values(owner_token=InstallationIdentity.owner_token)
    )
    if result.rowcount != 1:
        raise RuntimeError("object-attempt installation identity is missing")
    row = s.get(InstallationIdentity, _INSTALLATION_ID, with_for_update=True)
    if row is None:
        raise RuntimeError("object-attempt installation identity is missing")
    return row


def _installation_identity(s) -> InstallationIdentity:
    row = _lock_object_attempt_registry(s)
    configured = os.environ.get("DP_STORAGE_NAMESPACE", "").strip()
    if configured and configured != row.storage_namespace:
        raise RuntimeError(
            "DP_STORAGE_NAMESPACE does not match this metadata database; isolate an offline metadata "
            "clone explicitly before allocating object attempts"
        )
    return row


def object_attempt_owner_id() -> str:
    """The durable non-secret owner token shared by every hub using this metadata database."""
    with session() as s:
        row = s.get(InstallationIdentity, _INSTALLATION_ID)
        if row is None or not row.owner_token:
            raise RuntimeError("object-attempt installation identity is missing")
        return row.owner_token


def object_attempt_namespace(uri: str) -> str:
    normalized = _validated_object_uri(uri, attempt=True)
    with session() as s:
        row = s.get(ObjectAttempt, normalized)
        if row is None:
            raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
        return row.storage_namespace


def attest_object_attempt(uri: str, *, logical_uri: str, kind: str,
                          expected_run_id: str | None = None,
                          allowed_states: tuple[str, ...] | None = None) -> dict:
    """Return immutable attempt identity only when the current installation owns the exact write."""
    normalized = _validated_object_uri(uri, attempt=True)
    logical = _validated_object_uri(logical_uri, attempt=False)
    if kind not in _OBJECT_ATTEMPT_KINDS:
        raise ValueError("object attempt attestation requires a supported kind")
    with session() as s:
        identity = _installation_identity(s)
        row = s.get(ObjectAttempt, normalized)
        if row is None:
            raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
        _validate_object_attempt_identity(
            row, logical_uri=logical, kind=kind,
            run_id=str(expected_run_id) if expected_run_id is not None else None)
        if row.storage_namespace != identity.storage_namespace:
            raise RuntimeError("object attempt belongs to another storage namespace")
        if allowed_states is not None and row.state not in allowed_states:
            raise RuntimeError(f"object attempt is not attestable in state {row.state!r}")
        return _attempt_handle(row)


def object_storage_namespace() -> str:
    """Stable installation namespace embedded into every new physical object-attempt URI."""
    with session() as s:
        return _installation_identity(s).storage_namespace


def object_attempt_is_managed(uri: str) -> bool:
    normalized = str(uri).rstrip("/")
    if object_attempt_uri_shape(normalized):
        normalized = _validated_object_uri(normalized, attempt=True)
    with session() as s:
        return s.get(ObjectAttempt, normalized) is not None


def bind_object_storage_root(storage_root: str) -> None:
    """Bind the metadata DB to one canonical managed root and fail closed on a different root.

    This catches accidental reuse of a metadata database against another bucket/prefix. An offline DB
    clone that also copies the configured namespace cannot be distinguished without a provider-side
    conditional ownership marker; providers may implement that additional claim independently.
    """
    root = str(storage_root).strip().rstrip("/")
    if not root:
        raise ValueError("managed object storage root is required")
    fingerprint = hashlib.sha256(root.encode()).hexdigest()
    with session() as s:
        row = _installation_identity(s)
        if row.storage_fingerprint and row.storage_fingerprint != fingerprint:
            raise RuntimeError("metadata database is already bound to a different managed storage root")
        row.storage_fingerprint = fingerprint


def activate_object_storage_claim(namespace: str, storage_scope: str,
                                  activation_id: str, writer) -> dict:
    """CAS one provider marker while holding the shared DB installation lock."""
    namespace, activation_id = str(namespace), str(activation_id)
    with session() as s:
        ident = _lock_object_attempt_registry(s)
        if ident.storage_namespace != namespace:
            raise RuntimeError("storage namespace is not owned by this metadata database")
        claim_key = {"storage_namespace": namespace, "storage_scope": str(storage_scope)}
        row = s.get(ObjectStorageClaim, claim_key, with_for_update=True)
        prior_token = row.claim_token if row is not None else None
        prior_etag = row.marker_etag if row is not None else None
        marker_etag = str(writer(
            ident.owner_token, namespace, activation_id, prior_token, prior_etag) or "")
        if not marker_etag:
            raise RuntimeError("provider did not return a namespace claim identity")
        if row is None:
            row = ObjectStorageClaim(
                storage_namespace=namespace, storage_scope=str(storage_scope),
                claim_token=activation_id,
                marker_etag=marker_etag)
            s.add(row)
        else:
            row.claim_token, row.marker_etag = activation_id, marker_etag
        return {
            "owner_token": ident.owner_token, "namespace": namespace,
            "claim_token": activation_id, "marker_etag": marker_etag,
        }


def object_storage_claim(namespace: str, storage_scope: str) -> dict | None:
    with session() as s:
        ident = s.get(InstallationIdentity, _INSTALLATION_ID)
        row = s.get(ObjectStorageClaim, {
            "storage_namespace": str(namespace), "storage_scope": str(storage_scope)})
        if (ident is None or ident.storage_namespace != str(namespace)
                or row is None or not row.claim_token or not row.marker_etag):
            return None
        return {
            "owner_token": ident.owner_token, "namespace": row.storage_namespace,
            "claim_token": row.claim_token, "marker_etag": row.marker_etag,
        }


def isolate_cloned_object_storage(expected: str, replacement: str) -> str:
    """Isolate an offline metadata clone under a new owner and storage namespace.

    This deliberately revokes the clone's authority and public visibility for every inherited managed
    attempt. It is not a disaster-recovery takeover of the original namespace.
    """
    replacement = str(replacement).strip()
    if not replacement or len(replacement.encode()) > 80:
        raise ValueError("storage namespace must be 1..80 UTF-8 bytes")
    if replacement == str(expected):
        raise ValueError("clone isolation requires a new storage namespace")
    with session() as s:
        row = _lock_object_attempt_registry(s)
        if row.storage_namespace != expected:
            raise RuntimeError("storage namespace changed concurrently")
        # Every lifecycle row in the copied metadata DB is inherited, including attempts from a
        # namespace used before the current identity. Leaving any one published would preserve a
        # public read path into storage the clone no longer owns.
        inherited = list(s.scalars(select(ObjectAttempt).with_for_update()))
        inherited_uris = {attempt.uri for attempt in inherited}

        if inherited_uris:
            for cache in list(s.scalars(select(ResultCache).with_for_update())):
                if _result_doc_uri(cache.doc) in inherited_uris:
                    s.delete(cache)
            for ref in list(s.scalars(select(ObjectAttemptRef).where(
                    ObjectAttemptRef.attempt_uri.in_(inherited_uris)).with_for_update())):
                s.delete(ref)

            logical_rows = list(s.scalars(select(CatalogLogicalDataset).where(
                CatalogLogicalDataset.current_uri.in_(inherited_uris))
                .order_by(CatalogLogicalDataset.logical_id).with_for_update()))
            for logical in logical_rows:
                current_uri = logical.current_uri
                if current_uri:
                    _delete_catalog_children(s, [current_uri])
                    entry = s.get(CatalogEntry, current_uri, with_for_update=True)
                    if entry is not None:
                        s.delete(entry)
                _delete_catalog_governance(s, logical.catalog_key)
                logical.current_uri = None
                logical.catalog_epoch += 1
                logical.state = "unregistered"
                logical.metadata_version += 1
                logical.governance_doc = "{}"

            remaining_entries = list(s.scalars(select(CatalogEntry).where(
                CatalogEntry.uri.in_(inherited_uris)).order_by(CatalogEntry.uri).with_for_update()))
            for entry in remaining_entries:
                _delete_catalog_children(s, [entry.uri])
                s.delete(entry)

            for lease in list(s.scalars(select(ObjectAttemptLease).where(
                    ObjectAttemptLease.attempt_uri.in_(inherited_uris)).with_for_update())):
                s.delete(lease)
            for attempt in inherited:
                if attempt.state != "deleted":
                    attempt.state = "quarantined"
                    attempt.quarantine_reason = "inherited attempt revoked by clone isolation"
                attempt.delete_owner = attempt.delete_lease_expires_at = None

        for claim in list(s.scalars(select(ObjectStorageClaim).with_for_update())):
            s.delete(claim)
        row.owner_token = uuid.uuid4().hex
        row.storage_namespace = replacement
        return replacement


def _validate_object_attempt_identity(row: ObjectAttempt, *, logical_uri: str, kind: str,
                                      run_id: str | None = None) -> None:
    if (row.logical_uri, row.kind) != (logical_uri, kind) or (
            run_id is not None and row.run_id != run_id):
        raise RuntimeError("object attempt URI is already claimed by a different logical write")


def _attempt_handle(row: ObjectAttempt, write_lease_id: str | None = None,
                    publish_lease_id: str | None = None) -> dict:
    return {
        "attempt_id": row.attempt_id,
        "allocation_key": row.allocation_key,
        "namespace": row.storage_namespace,
        "generation": row.generation,
        "uri": row.uri,
        "logical_uri": row.logical_uri,
        "kind": row.kind,
        "run_id": row.run_id,
        "storage_namespace": row.storage_namespace,
        "state": row.state,
        "write_lease_id": write_lease_id,
        "publish_lease_id": publish_lease_id,
    }


def _put_lease(s, row: ObjectAttempt, lease_type: str, owner: str, ttl_seconds: float,
               lease_id: str | None = None) -> str:
    ttl = max(1.0, _gc_seconds(ttl_seconds, "lease ttl"))
    now = _db_now(s)
    lid = lease_id or uuid.uuid4().hex
    current = s.get(ObjectAttemptLease, lid, with_for_update=True)
    # SQLite CURRENT_TIMESTAMP has one-second precision. Without one clock tick of headroom, a renewal
    # inside the same second can write the same expiry and race a reaper at the boundary. The lease still
    # uses DB time; this only accounts for that backend's observable clock resolution.
    precision_margin = 1.0 if s.get_bind().dialect.name == "sqlite" else 0.0
    expires = now + datetime.timedelta(seconds=ttl + precision_margin)
    if current is None:
        s.add(ObjectAttemptLease(
            lease_id=lid, attempt_uri=row.uri, generation=row.generation,
            lease_type=lease_type, owner=str(owner), expires_at=expires, created_at=now,
        ))
    else:
        if (current.attempt_uri, current.generation, current.lease_type, current.owner) != (
                row.uri, row.generation, lease_type, str(owner)):
            raise RuntimeError("lease ID is already bound to another object attempt")
        current.expires_at = expires
    return lid


def _reserve_catalog_publication(s, logical_uri: str, catalog_key_base: str) -> tuple[str, int, int]:
    logical_id = _catalog_logical_id(logical_uri)
    logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=True)
    if logical is None:
        base = str(catalog_key_base).strip() or logical_id
        catalog_key = f"{base}_{hashlib.sha256(logical_uri.encode()).hexdigest()[:16]}"
        values = {
            "logical_id": logical_id, "catalog_key": catalog_key, "logical_uri": logical_uri,
            "current_uri": None, "current_publish_seq": 0, "next_publish_seq": 0,
            "catalog_epoch": 0, "state": "active", "governance_doc": "{}",
            "metadata_version": 0, "usage": 0,
        }
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
            s.execute(dialect_insert(CatalogLogicalDataset).values(**values).on_conflict_do_nothing(
                index_elements=[CatalogLogicalDataset.logical_id]))
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            s.execute(dialect_insert(CatalogLogicalDataset).values(**values).on_conflict_do_nothing(
                index_elements=[CatalogLogicalDataset.logical_id]))
        else:
            s.add(CatalogLogicalDataset(**values))
        s.flush()
        logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=True)
        if logical is None:
            raise RuntimeError("catalog logical identity reservation failed")
    elif logical.logical_uri != logical_uri:
        raise RuntimeError("catalog logical identity collision")
    logical.next_publish_seq += 1
    return logical.logical_id, logical.catalog_epoch, logical.next_publish_seq


def allocate_object_attempt(*, logical_uri: str, kind: str, run_id: str, allocation_key: str,
                            uri_factory, write_lease_seconds: float = 3600,
                            publish_lease_seconds: float | None = None,
                            expected_namespace: str | None = None,
                            catalog_key_base: str | None = None) -> dict:
    """Allocate or recover one durable attempt handle before any object write starts.

    An active allocation key reuses the exact handle. Once its generation is terminal, the same key
    advances to a fresh random attempt ID and physical URI; old rows remain as fenced tombstones.
    """
    logical_uri = _validated_object_uri(logical_uri, attempt=False)
    allocation_key, run_id = str(allocation_key), str(run_id)
    if not logical_uri or not allocation_key or not run_id or kind not in _OBJECT_ATTEMPT_KINDS:
        raise ValueError("object attempt allocation requires logical URI, kind, run ID, and allocation key")
    if kind == "sink" and not str(catalog_key_base or "").strip():
        raise ValueError("managed sink allocation requires a stable catalog key base")
    with session() as s:
        ident = _installation_identity(s)
        if expected_namespace is not None and ident.storage_namespace != expected_namespace:
            raise RuntimeError("storage namespace changed during provider ownership claim")
        pointer = s.get(ObjectAttemptAllocation, allocation_key, with_for_update=True)
        generation = 1
        locked_logical = None
        if pointer is not None:
            # Catalog publication takes logical -> attempt. Read only immutable identity first, then
            # take the same logical -> attempt order so a committed allocation retry cannot deadlock
            # with publication. Installation and allocation-pointer locks are never taken by publish.
            current_identity = s.get(ObjectAttempt, pointer.attempt_uri)
            if current_identity is None:
                raise RuntimeError("object attempt allocation points to a missing ownership row")
            _validate_object_attempt_identity(
                current_identity, logical_uri=logical_uri, kind=kind)
            if kind == "sink":
                if not current_identity.logical_id:
                    raise RuntimeError("object sink attempt logical publication identity is missing")
                locked_logical = s.get(
                    CatalogLogicalDataset, current_identity.logical_id, with_for_update=True)
                if locked_logical is None or locked_logical.logical_uri != logical_uri:
                    raise RuntimeError("object sink attempt logical publication identity is missing")
            current = s.get(ObjectAttempt, pointer.attempt_uri, with_for_update=True)
            if current is None:
                raise RuntimeError("object attempt allocation points to a missing ownership row")
            if current.state in _WRITABLE_ATTEMPT_STATES:
                _validate_object_attempt_identity(
                    current, logical_uri=logical_uri, kind=kind, run_id=run_id)
                current.state = "writing"
                write_lease_id = _put_lease(
                    s, current, "write", run_id, write_lease_seconds,
                    lease_id=f"write:{current.attempt_id}",
                )
                publish_lease_id = _put_lease(
                    s, current, "publish", run_id,
                    publish_lease_seconds or write_lease_seconds,
                    lease_id=f"publish:{current.attempt_id}",
                )
                return _attempt_handle(current, write_lease_id, publish_lease_id)
            _validate_object_attempt_identity(current, logical_uri=logical_uri, kind=kind)
            generation = pointer.generation + 1
        logical_id = catalog_epoch = publish_seq = None
        if kind == "sink":
            if locked_logical is None:
                logical_id, catalog_epoch, publish_seq = _reserve_catalog_publication(
                    s, logical_uri, str(catalog_key_base))
            else:
                locked_logical.next_publish_seq += 1
                logical_id = locked_logical.logical_id
                catalog_epoch = locked_logical.catalog_epoch
                publish_seq = locked_logical.next_publish_seq
        attempt_id = uuid.uuid4().hex
        uri = _validated_object_uri(
            uri_factory(ident.storage_namespace, generation, attempt_id), attempt=True)
        if s.get(ObjectAttempt, uri) is not None:
            raise RuntimeError("physical object-attempt URI is already owned")
        row = ObjectAttempt(
            uri=uri, attempt_id=attempt_id, allocation_key=allocation_key,
            storage_namespace=ident.storage_namespace, generation=generation,
            logical_uri=logical_uri, kind=kind, run_id=run_id,
            logical_id=logical_id, catalog_epoch=catalog_epoch, publish_seq=publish_seq,
            state="writing",
        )
        s.add(row)
        s.flush()
        if pointer is None:
            s.add(ObjectAttemptAllocation(
                allocation_key=allocation_key, attempt_uri=uri, generation=generation,
            ))
        else:
            pointer.attempt_uri, pointer.generation = uri, generation
        write_lease_id = _put_lease(
            s, row, "write", run_id, write_lease_seconds, lease_id=f"write:{attempt_id}")
        publish_lease_id = _put_lease(
            s, row, "publish", run_id, publish_lease_seconds or write_lease_seconds,
            lease_id=f"publish:{attempt_id}")
        return _attempt_handle(row, write_lease_id, publish_lease_id)


def lookup_object_attempt(*, allocation_key: str, logical_uri: str, kind: str,
                          run_id: str | None = None) -> dict | None:
    """Read the current generation without acquiring writer authority or changing its state."""
    logical_uri = _validated_object_uri(logical_uri, attempt=False)
    with session() as s:
        pointer = s.get(ObjectAttemptAllocation, str(allocation_key))
        if pointer is None:
            return None
        row = s.get(ObjectAttempt, pointer.attempt_uri)
        if row is None:
            raise RuntimeError("object attempt allocation points to a missing ownership row")
        _validate_object_attempt_identity(
            row, logical_uri=logical_uri, kind=kind, run_id=str(run_id) if run_id else None)
        return _attempt_handle(row)


def _has_attempt_refs(s, uri: str) -> bool:
    return bool(s.scalar(select(func.count()).select_from(ObjectAttemptRef).where(
        ObjectAttemptRef.attempt_uri == uri)))


def _maybe_supersede(s, row: ObjectAttempt, now: datetime.datetime) -> bool:
    if row.state == "published" and not _has_attempt_refs(s, row.uri):
        row.state, row.retired_at = "superseded", now
        row.delete_owner = row.delete_lease_expires_at = None
        return True
    return False


def _replace_attempt_ref(s, ref_type: str, ref_key: str, uri: str | None,
                         *, publish: bool = False) -> list[str]:
    key = {"ref_type": str(ref_type), "ref_key": str(ref_key)}
    current = s.get(ObjectAttemptRef, key, with_for_update=True)
    old_uri = current.attempt_uri if current is not None else None
    normalized = str(uri).rstrip("/") if uri else None
    if old_uri == normalized:
        return []
    new_attempt = None
    if normalized:
        new_attempt = s.get(ObjectAttempt, normalized, with_for_update=True)
        if new_attempt is None and object_attempt_uri_shape(normalized):
            _validated_object_uri(normalized, attempt=True)
            raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
        if new_attempt is not None:
            if new_attempt.state in _TERMINAL_ATTEMPT_STATES:
                raise RuntimeError(f"cannot reference object attempt in state {new_attempt.state!r}")
            if publish:
                if new_attempt.state not in ("committed", "published"):
                    raise RuntimeError("object attempt must be committed before pointer publication")
                new_attempt.state = "published"
                new_attempt.published_at = new_attempt.published_at or _db_now(s)
            elif new_attempt.state != "published":
                raise RuntimeError("run history and state may reference only a published object attempt")
    if current is not None:
        s.delete(current)
        s.flush()
    if new_attempt is not None:
        s.add(ObjectAttemptRef(
            ref_type=str(ref_type), ref_key=str(ref_key), attempt_uri=normalized,
            generation=new_attempt.generation,
        ))
        s.flush()
        if publish:
            for lease in s.scalars(select(ObjectAttemptLease).where(
                    ObjectAttemptLease.attempt_uri == normalized,
                    ObjectAttemptLease.lease_type == "publish")):
                s.delete(lease)
    superseded: list[str] = []
    if old_uri and old_uri != normalized:
        old = s.get(ObjectAttempt, old_uri, with_for_update=True)
        if old is not None and _maybe_supersede(s, old, _db_now(s)):
            superseded.append(old_uri)
    return superseded


def acquire_object_attempt_lease(uri: str, lease_type: str, owner: str,
                                 ttl_seconds: float = 300, *,
                                 allow_committed: bool = False) -> str | None:
    """Resolve one managed URI and create its lease in the same transaction.

    ``None`` means the URI is not managed. A managed row already won by GC raises an explicit miss.
    """
    uri = str(uri).rstrip("/")
    if lease_type not in ("read", "write"):
        raise ValueError("callers may acquire only read or write leases")
    if lease_type == "read":
        lease_id, _attestation = acquire_attested_object_read(
            uri, owner, ttl_seconds=ttl_seconds, allow_committed=allow_committed)
        return lease_id
    with session() as s:
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is None:
            if object_attempt_uri_shape(uri):
                _validated_object_uri(uri, attempt=True)
                raise FileNotFoundError("attempt-shaped object URI has no lifecycle ownership row")
            return None
        allowed = ("allocated", "writing")
        if row.state not in allowed:
            raise FileNotFoundError(
                f"managed artifact generation is unavailable (state={row.state})")
        if lease_type == "write":
            row.state = "writing"
        return _put_lease(s, row, lease_type, owner, ttl_seconds)


def acquire_attested_object_read(uri: str, owner: str, ttl_seconds: float = 300, *,
                                 allow_committed: bool = False
                                 ) -> tuple[str | None, dict | None]:
    """Atomically attest one exact managed generation and acquire its renewable read lease.

    Ordinary unmanaged URIs return ``(None, None)``. Attempt-shaped URIs always fail closed unless the
    current installation owns the exact published (or explicitly allowed committed) generation.
    """
    normalized = str(uri).rstrip("/")
    reserved_path = object_attempt_namespace_path(normalized)
    if reserved_path and not object_attempt_uri_shape(normalized):
        raise FileNotFoundError("managed source must reference the exact attempt root")
    if reserved_path:
        normalized = _validated_object_uri(normalized, attempt=True)
    with session() as s:
        candidate = s.get(ObjectAttempt, normalized)
        if candidate is None:
            if reserved_path:
                raise FileNotFoundError(
                    "attempt-shaped object URI has no lifecycle ownership row")
            return None, None
        # Installation identity is immutable during normal operation. Clone isolation fences every
        # inherited attempt before changing it, so a snapshot avoids a global registry write lock on each
        # read while the locked attempt reload below remains the sole attestation/publication authority.
        identity = s.get(InstallationIdentity, _INSTALLATION_ID)
        if identity is None:
            raise RuntimeError("object-attempt installation identity is missing")
        configured = os.environ.get("DP_STORAGE_NAMESPACE", "").strip()
        if configured and configured != identity.storage_namespace:
            raise RuntimeError(
                "DP_STORAGE_NAMESPACE does not match this metadata database")
        row = s.get(ObjectAttempt, normalized, with_for_update=True, populate_existing=True)
        if row is None:
            raise FileNotFoundError("managed artifact generation is unavailable")
        if row.storage_namespace != identity.storage_namespace:
            raise FileNotFoundError("managed artifact belongs to another storage namespace")
        allowed = ("published", "committed") if allow_committed else ("published",)
        if row.state not in allowed:
            raise FileNotFoundError(
                f"managed artifact generation is unavailable (state={row.state})")
        lease_id = _put_lease(s, row, "read", owner, ttl_seconds)
        return lease_id, _attempt_handle(row)


def renew_object_attempt_lease(lease_id: str, ttl_seconds: float = 300) -> bool:
    with session() as s:
        key = str(lease_id)
        identity = s.execute(select(
            ObjectAttemptLease.attempt_uri,
            ObjectAttemptLease.generation,
        ).where(ObjectAttemptLease.lease_id == key)).one_or_none()
        if identity is None:
            return False
        row = s.get(ObjectAttempt, identity.attempt_uri, with_for_update=True)
        if row is None or row.generation != identity.generation:
            return False
        lease = s.get(ObjectAttemptLease, key, with_for_update=True)
        if (lease is None or lease.attempt_uri != row.uri
                or lease.generation != row.generation):
            return False
        _put_lease(s, row, lease.lease_type, lease.owner, ttl_seconds, lease_id=lease.lease_id)
        return True


def release_object_attempt_lease(lease_id: str) -> None:
    with session() as s:
        lease = s.get(ObjectAttemptLease, str(lease_id), with_for_update=True)
        if lease is not None:
            s.delete(lease)


def release_result_cache_pin(pin_id: str) -> None:
    """Release one cache-reader ref/lease pair after terminal ownership publication."""
    with session() as s:
        key = str(pin_id)
        lease_uri = s.scalar(select(ObjectAttemptLease.attempt_uri).where(
            ObjectAttemptLease.lease_id == key))
        ref_uri = s.scalar(select(ObjectAttemptRef.attempt_uri).where(
            ObjectAttemptRef.ref_type == "result_reader",
            ObjectAttemptRef.ref_key == key,
        ))
        uris = sorted({uri for uri in (lease_uri, ref_uri) if uri})
        attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri).with_for_update())} \
            if uris else {}
        ref = s.get(ObjectAttemptRef, {
            "ref_type": "result_reader", "ref_key": key}, with_for_update=True)
        lease = s.get(ObjectAttemptLease, key, with_for_update=True)
        current_uris = {uri for uri in (
            ref.attempt_uri if ref is not None else None,
            lease.attempt_uri if lease is not None else None,
        ) if uri}
        if not current_uris.issubset(attempts):
            raise RuntimeError("result cache pin ownership changed concurrently")
        if ref is not None:
            s.delete(ref)
        if lease is not None:
            s.delete(lease)
        s.flush()
        now = _db_now(s)
        for uri in sorted(current_uris):
            _maybe_supersede(s, attempts[uri], now)


def _inventory_hash(inventory: list[dict]) -> str:
    normalized = [{
        "member_id": str(item["member_id"]),
        "key": str(item["key"]),
        "member_type": str(item["member_type"]),
        "etag": item.get("etag"),
        "version_id": item.get("version_id"),
        "upload_id": item.get("upload_id"),
        "size": int(item.get("size") or 0),
        "is_latest": bool(item.get("is_latest")),
        "is_commit": bool(item.get("is_commit")),
    } for item in inventory]
    normalized.sort(key=lambda item: item["member_id"])
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _store_inventory(s, row: ObjectAttempt, inventory: list[dict]) -> None:
    member_ids = [str(item.get("member_id") or "") for item in inventory]
    keys = [str(item.get("key") or "") for item in inventory]
    if (any(not value for value in (*member_ids, *keys))
            or len(member_ids) != len(set(member_ids))):
        raise RuntimeError("exact object inventory must contain unique non-empty member identities")
    for old in s.scalars(select(ObjectAttemptInventory).where(
            ObjectAttemptInventory.attempt_uri == row.uri)):
        s.delete(old)
    s.flush()
    for item in inventory:
        s.add(ObjectAttemptInventory(
            attempt_uri=row.uri, member_id=str(item["member_id"]),
            object_key=str(item["key"]), member_type=str(item["member_type"]),
            etag=item.get("etag"), version_id=item.get("version_id"),
            upload_id=item.get("upload_id"), size=int(item.get("size") or 0),
            is_latest=bool(item.get("is_latest")),
            is_commit=bool(item.get("is_commit")),
        ))


def record_object_attempt_commit(uri: str, inventory: list[dict], quiet_seconds: float = 0) -> None:
    """Persist writer terminal proof and exact published members before any pointer can reference them."""
    with session() as s:
        row = s.get(ObjectAttempt, str(uri).rstrip("/"), with_for_update=True)
        if row is None:
            raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
        if row.state in _TERMINAL_ATTEMPT_STATES:
            raise RuntimeError(f"cannot commit object attempt in state {row.state!r}")
        digest = _inventory_hash(inventory)
        if row.state in ("committed", "published"):
            if not row.inventory_complete or row.inventory_hash != digest:
                raise RuntimeError("committed object inventory changed")
            return
        now = _db_now(s)
        _store_inventory(s, row, inventory)
        row.inventory_hash = digest
        row.inventory_observations = 2
        row.inventory_complete = True
        row.terminal_proof_at = now
        row.quiet_until = now + datetime.timedelta(seconds=_gc_seconds(quiet_seconds, "quiet_seconds"))
        row.state = "committed"
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type == "write")):
            s.delete(lease)


def abandon_committed_object_attempt(uri: str) -> bool:
    """Make an unreferenced, fully inventoried write reclaimable after publication fails."""
    with session() as s:
        row = s.get(ObjectAttempt, str(uri).rstrip("/"), with_for_update=True)
        if row is None:
            return False
        if row.state == "abandoned":
            return True
        if row.state != "committed":
            return False
        owned = s.scalar(select(exists().where(ObjectAttemptRef.attempt_uri == row.uri)))
        if owned:
            return False
        row.state = "abandoned"
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type == "publish")):
            s.delete(lease)
        return True


def mark_object_attempt_terminal(uri: str, *, quiet_seconds: float = 60) -> bool:
    """Backend/supervisor proof that a failed writer can no longer mutate this generation."""
    with session() as s:
        row = s.get(ObjectAttempt, str(uri).rstrip("/"), with_for_update=True)
        if row is None:
            return False
        if row.state in ("abandoned", "delete_pending", "deleting", "deleted", "quarantined"):
            return True
        if row.state not in ("allocated", "writing"):
            return False
        now = _db_now(s)
        row.state, row.terminal_proof_at = "abandoned", now
        row.quiet_until = now + datetime.timedelta(seconds=_gc_seconds(quiet_seconds, "quiet_seconds"))
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type.in_(("write", "publish")))):
            s.delete(lease)
        return True


def observe_object_attempt_inventory(uri: str, inventory: list[dict],
                                     quiet_seconds: float = 60) -> str:
    """Require two DB-clock-separated identical observations for a failed partial attempt."""
    with session() as s:
        row = s.get(ObjectAttempt, str(uri).rstrip("/"), with_for_update=True)
        if row is None:
            return "missing"
        if row.state != "abandoned" or row.terminal_proof_at is None:
            return row.state
        now = _db_now(s)
        digest = _inventory_hash(inventory) if inventory else hashlib.sha256(b"[]").hexdigest()
        if row.inventory_hash is None:
            row.inventory_hash, row.inventory_observations = digest, 1
            margin = 1.0 if s.get_bind().dialect.name == "sqlite" else 0.0
            row.quiet_until = now + datetime.timedelta(
                seconds=max(margin, _gc_seconds(quiet_seconds, "quiet_seconds")))
            return "observed"
        if digest != row.inventory_hash:
            row.state = "quarantined"
            row.quarantine_reason = "object inventory changed after writer terminal proof"
            return "quarantined"
        if row.quiet_until is not None and now < row.quiet_until:
            return "waiting"
        _store_inventory(s, row, inventory)
        row.inventory_observations = 2
        row.inventory_complete = True
        return "complete"


def quarantine_object_attempt(uri: str, reason: str) -> None:
    with session() as s:
        row = s.get(ObjectAttempt, str(uri).rstrip("/"), with_for_update=True)
        if row is not None and row.state != "deleted":
            row.state, row.quarantine_reason = "quarantined", str(reason)[:4000]
            row.delete_owner = row.delete_lease_expires_at = None
            for lease in s.scalars(select(ObjectAttemptLease).where(
                    ObjectAttemptLease.attempt_uri == row.uri)):
                s.delete(lease)


def object_attempt_catalog_prior(uri: str) -> dict | None:
    """Catalog identity/organization of the current published version of this sink target."""
    uri = str(uri).rstrip("/")
    with session() as s:
        row = s.get(ObjectAttempt, uri)
        if row is None or row.kind != "sink":
            return None
        prior = s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.kind == "sink",
                ObjectAttempt.logical_uri == row.logical_uri,
                ObjectAttempt.state == "published",
                ObjectAttempt.uri != uri,
            ).order_by(ObjectAttempt.published_at.desc(), ObjectAttempt.uri.desc()).limit(1)
        ).first()
        if prior is None:
            return None
        entry = s.get(CatalogEntry, prior.uri)
        return _row_to_doc(entry, _tags_for(s, [prior.uri]).get(prior.uri, [])) if entry else None


def _gc_seconds(value: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _object_attempt_action(row: ObjectAttempt, action: str) -> dict:
    return {
        "action": action,
        "uri": row.uri,
        "logical_uri": row.logical_uri,
        "kind": row.kind,
        "run_id": row.run_id,
        "storage_namespace": row.storage_namespace,
        "attempt_id": row.attempt_id,
        "generation": row.generation,
        "delete_epoch": row.delete_epoch,
        "delete_owner": row.delete_owner,
    }


def _expire_object_attempt_leases(limit: int) -> None:
    """Remove a bounded set of expired leases in attempt -> ref -> lease order."""
    with session() as s:
        cutoff = _db_now(s)
        attempt_uris = list(s.scalars(select(
            ObjectAttemptLease.attempt_uri,
        ).where(
            ObjectAttemptLease.expires_at <= cutoff,
        ).distinct().order_by(ObjectAttemptLease.attempt_uri).limit(min(limit, 100))))
    # Do not carry one attempt lock into acquisition of another. GC candidates and pointer replacement
    # can touch multiple attempts in other deterministic orders; a short transaction per attempt cannot
    # participate in a cross-attempt ABBA cycle.
    for uri in attempt_uris:
        with session() as s:
            attempt = s.get(ObjectAttempt, uri, with_for_update=True)
            if attempt is None:
                continue
            now = _db_now(s)
            has_expired_pin = exists().where(
                ObjectAttemptLease.lease_id == ObjectAttemptRef.ref_key,
                ObjectAttemptLease.attempt_uri == uri,
                ObjectAttemptLease.expires_at <= now,
            )
            expired_refs = list(s.scalars(select(ObjectAttemptRef).where(
                ObjectAttemptRef.ref_type == "result_reader",
                ObjectAttemptRef.attempt_uri == uri,
                has_expired_pin,
            ).order_by(ObjectAttemptRef.ref_key).with_for_update()))
            expired = list(s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == uri,
                ObjectAttemptLease.expires_at <= now,
            ).order_by(ObjectAttemptLease.lease_id).with_for_update()))
            for ref in expired_refs:
                s.delete(ref)
            for lease in expired:
                s.delete(lease)
            s.flush()
            if expired_refs:
                _maybe_supersede(s, attempt, now)


def object_attempt_gc_batch(retention_seconds: float, grace_seconds: float,
                            limit: int = 100) -> list[dict]:
    """Claim bounded observe/delete work using refs, DB-time leases, generation, and delete epoch."""
    retention = _gc_seconds(retention_seconds, "retention_seconds")
    grace = _gc_seconds(grace_seconds, "grace_seconds")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("object attempt GC limit must be a positive integer")
    actions: list[dict] = []
    # Each expiry transaction commits before the candidate scan, so it never carries attempt/lease
    # locks into a differently ordered set of GC candidates. Publication and renewal use the same order.
    _expire_object_attempt_leases(limit)
    with session() as s:
        now = _db_now(s)

        def remaining() -> int:
            return limit - len(actions)

        no_refs = ~exists().where(ObjectAttemptRef.attempt_uri == ObjectAttempt.uri)
        no_leases = ~exists().where(
            (ObjectAttemptLease.attempt_uri == ObjectAttempt.uri)
            & (ObjectAttemptLease.expires_at > now)
        )

        retention_cutoff = now - datetime.timedelta(seconds=retention)
        committed_orphans = list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state == "committed",
                ObjectAttempt.terminal_proof_at.is_not(None),
                ObjectAttempt.terminal_proof_at <= retention_cutoff,
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        ))
        for row in committed_orphans:
            row.state = "abandoned"
        s.flush()

        observations = list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state == "abandoned",
                ObjectAttempt.terminal_proof_at.is_not(None),
                ObjectAttempt.inventory_complete.is_(False),
                or_(ObjectAttempt.quiet_until.is_(None), ObjectAttempt.quiet_until <= now),
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        ))
        actions.extend(_object_attempt_action(row, "observe") for row in observations)
        if remaining() <= 0:
            return actions

        grace_cutoff = now - datetime.timedelta(seconds=grace)
        candidates = list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state.in_(("superseded", "abandoned")),
                ObjectAttempt.terminal_proof_at.is_not(None),
                ObjectAttempt.inventory_complete.is_(True),
                or_(ObjectAttempt.quiet_until.is_(None), ObjectAttempt.quiet_until <= now),
                ObjectAttempt.terminal_proof_at <= grace_cutoff,
                or_(
                    (ObjectAttempt.state == "superseded")
                    & (ObjectAttempt.retired_at.is_not(None))
                    & (ObjectAttempt.retired_at <= retention_cutoff),
                    (ObjectAttempt.state == "abandoned")
                    & (ObjectAttempt.terminal_proof_at <= retention_cutoff),
                ),
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        ))
        for row in candidates:
            row.state, row.next_delete_at = "delete_pending", now
        s.flush()

        claimable = list(s.scalars(
            select(ObjectAttempt).where(
                or_(
                    (ObjectAttempt.state == "delete_pending")
                    & or_(ObjectAttempt.next_delete_at.is_(None), ObjectAttempt.next_delete_at <= now),
                    (ObjectAttempt.state == "deleting")
                    & (ObjectAttempt.delete_lease_expires_at <= now),
                    (ObjectAttempt.state == "delete_verifying")
                    & or_(ObjectAttempt.next_delete_at.is_(None), ObjectAttempt.next_delete_at <= now),
                ),
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        ))
        for row in claimable:
            owner = uuid.uuid4().hex
            action = "verify_empty" if row.state == "delete_verifying" else "delete"
            if action == "delete":
                row.state = "deleting"
            row.delete_epoch += 1
            row.delete_owner = owner
            row.delete_lease_expires_at = now + datetime.timedelta(seconds=60)
            _put_lease(
                s, row, "delete", owner, 60,
                lease_id=f"delete:{row.attempt_id}:{row.delete_epoch}",
            )
            actions.append(_object_attempt_action(row, action))
    return actions


def object_attempt_inventory(uri: str, *, pending_only: bool = False) -> list[dict]:
    with session() as s:
        stmt = select(ObjectAttemptInventory).where(
            ObjectAttemptInventory.attempt_uri == str(uri).rstrip("/"))
        if pending_only:
            stmt = stmt.where(ObjectAttemptInventory.deleted_at.is_(None))
        return [{
            "member_id": row.member_id, "key": row.object_key,
            "member_type": row.member_type, "etag": row.etag,
            "version_id": row.version_id, "upload_id": row.upload_id,
            "size": row.size, "is_latest": row.is_latest, "is_commit": row.is_commit,
        } for row in s.scalars(stmt.order_by(ObjectAttemptInventory.member_id))]


def validate_object_attempt_delete(action: dict) -> bool:
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        now = _db_now(s)
        return bool(row and row.state in ("deleting", "delete_verifying")
                    and row.attempt_id == action.get("attempt_id")
                    and row.generation == action.get("generation")
                    and row.delete_epoch == action.get("delete_epoch")
                    and row.delete_owner == action.get("delete_owner")
                    and row.delete_lease_expires_at and row.delete_lease_expires_at > now)


def renew_object_attempt_delete(action: dict, ttl_seconds: float = 300) -> bool:
    """Renew the same epoch/owner before each provider I/O; stale workers cannot advance it."""
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        now = _db_now(s)
        if not (row and row.state in ("deleting", "delete_verifying")
                and row.attempt_id == action.get("attempt_id")
                and row.generation == action.get("generation")
                and row.delete_epoch == action.get("delete_epoch")
                and row.delete_owner == action.get("delete_owner")
                and row.delete_lease_expires_at and row.delete_lease_expires_at > now):
            return False
        ttl = max(1.0, _gc_seconds(ttl_seconds, "delete lease ttl"))
        row.delete_lease_expires_at = now + datetime.timedelta(seconds=ttl)
        lease = s.get(ObjectAttemptLease, f"delete:{row.attempt_id}:{row.delete_epoch}",
                      with_for_update=True)
        if lease is None:
            return False
        lease.expires_at = row.delete_lease_expires_at
        return True


def acknowledge_object_attempt_member(action: dict, member_id: str) -> None:
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        if not row or not (row.state == "deleting" and row.attempt_id == action.get("attempt_id")
                           and row.generation == action.get("generation")
                           and row.delete_epoch == action.get("delete_epoch")
                           and row.delete_owner == action.get("delete_owner")):
            raise RuntimeError("stale object-attempt delete acknowledgement")
        member = s.get(ObjectAttemptInventory, {
            "attempt_uri": row.uri, "member_id": str(member_id),
        }, with_for_update=True)
        if member is None:
            raise RuntimeError("delete acknowledgement is outside the exact inventory")
        member.deleted_at = member.deleted_at or _db_now(s)


def begin_object_attempt_delete_verification(action: dict, quiet_seconds: float) -> None:
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        if not row or not (row.state == "deleting" and row.attempt_id == action.get("attempt_id")
                           and row.generation == action.get("generation")
                           and row.delete_epoch == action.get("delete_epoch")
                           and row.delete_owner == action.get("delete_owner")):
            raise RuntimeError("stale object-attempt delete verification")
        remaining = s.scalar(select(func.count()).select_from(ObjectAttemptInventory).where(
            ObjectAttemptInventory.attempt_uri == row.uri,
            ObjectAttemptInventory.deleted_at.is_(None))) or 0
        if remaining:
            raise RuntimeError("cannot verify object-attempt deletion with inventory members remaining")
        now = _db_now(s)
        margin = 1.0 if s.get_bind().dialect.name == "sqlite" else 0.0
        row.state = "delete_verifying"
        row.delete_empty_observations = 1
        row.delete_empty_observed_at = now
        row.next_delete_at = now + datetime.timedelta(
            seconds=max(margin, _gc_seconds(quiet_seconds, "delete verification quiet seconds")))
        row.delete_owner = row.delete_lease_expires_at = None
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type == "delete")):
            s.delete(lease)


def complete_object_attempt_delete_verification(action: dict) -> bool:
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        if not row or not (row.state == "delete_verifying"
                           and row.attempt_id == action.get("attempt_id")
                           and row.generation == action.get("generation")
                           and row.delete_epoch == action.get("delete_epoch")
                           and row.delete_owner == action.get("delete_owner")):
            raise RuntimeError("stale object-attempt empty verification")
        now = _db_now(s)
        if (row.delete_empty_observed_at is None or now <= row.delete_empty_observed_at
                or (row.next_delete_at is not None and now < row.next_delete_at)):
            return False
        row.delete_empty_observations += 1
        if row.delete_empty_observations < 2:
            return False
        row.state, row.deleted_at = "deleted", now
        row.delete_owner = row.delete_lease_expires_at = None
        row.next_delete_at = None
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type == "delete")):
            s.delete(lease)
        return True


def fail_object_attempt_delete(action: dict, error: str) -> None:
    with session() as s:
        row = s.get(ObjectAttempt, str(action["uri"]).rstrip("/"), with_for_update=True)
        if not row or not (row.state in ("deleting", "delete_verifying")
                           and row.attempt_id == action.get("attempt_id")
                           and row.generation == action.get("generation")
                           and row.delete_epoch == action.get("delete_epoch")
                           and row.delete_owner == action.get("delete_owner")):
            return
        now = _db_now(s)
        row.delete_attempts += 1
        row.state = "quarantined" if row.delete_attempts >= 20 else "delete_pending"
        row.next_delete_at = (None if row.state == "quarantined" else
                              now + datetime.timedelta(seconds=min(
                                  3600, 2 ** min(row.delete_attempts, 10))))
        row.delete_owner = row.delete_lease_expires_at = None
        row.quarantine_reason = str(error)[:4000] if row.state == "quarantined" else None
        for lease in s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri == row.uri,
                ObjectAttemptLease.lease_type == "delete")):
            s.delete(lease)


def _result_doc_uri(raw: str | dict | None) -> str | None:
    try:
        doc = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    uri = (doc.get("uri") or doc.get("outputUri") or doc.get("output_uri")) \
        if isinstance(doc, dict) else None
    return str(uri).rstrip("/") if uri else None


def put_result(key: str, doc: dict) -> list[str]:
    """Atomically replace a cache row and its durable object/local artifact reference."""
    payload = json.dumps(doc, default=str)
    new_uri = _result_doc_uri(doc)
    retired: list[str] = []
    with session() as s:
        now = _db_now(s)
        stale_candidate_keys: list[str] = []
        locked_cache: dict[str, ResultCache] = {}
        if s.get_bind().dialect.name == "sqlite":
            # One atomic same-key upsert is the SQLite lock/CAS point, including concurrent first
            # publication. It obtains SQLite's writer transaction before any attempt/ref mutation; a
            # reader uses the matching key-scoped no-op update above. PostgreSQL keeps row locks below.
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            statement = sqlite_insert(ResultCache).values(
                key=key, doc=payload, created_at=now)
            s.execute(statement.on_conflict_do_update(
                index_elements=[ResultCache.key],
                set_={"doc": payload, "created_at": now},
            ))
        else:
            existing = s.get(ResultCache, key)
            stale_candidate_keys = list(s.scalars(select(ResultCache.key).where(
                ResultCache.key != str(key)
            ).order_by(ResultCache.created_at.desc(), ResultCache.key.desc())
              .offset(max(0, _RESULT_CACHE_MAX - 1))))
            lock_keys = set(stale_candidate_keys)
            if existing is not None:
                lock_keys.add(str(key))
            locked_cache = {row.key: row for row in s.scalars(select(ResultCache).where(
                ResultCache.key.in_(sorted(lock_keys))
            ).order_by(ResultCache.key).with_for_update())} if lock_keys else {}
            row = locked_cache.get(str(key))
            if row is None:
                s.add(ResultCache(key=key, doc=payload, created_at=now))
            else:
                row.doc, row.created_at = payload, now
        s.flush()
        if s.get_bind().dialect.name == "sqlite":
            stale = [row for row in s.scalars(
                select(ResultCache).where(ResultCache.key != str(key))
                .order_by(ResultCache.created_at.desc(), ResultCache.key.desc())
                .offset(max(0, _RESULT_CACHE_MAX - 1)).with_for_update())]
        else:
            stale_now = set(s.scalars(select(ResultCache.key).order_by(
                ResultCache.created_at.desc(), ResultCache.key.desc()
            ).offset(_RESULT_CACHE_MAX)))
            stale = [locked_cache[stale_key]
                     for stale_key in sorted(stale_now & set(stale_candidate_keys))
                     if stale_key != str(key) and stale_key in locked_cache]
        attempt = s.get(ObjectAttempt, new_uri, with_for_update=True) if new_uri else None
        if attempt is not None:
            if attempt.kind != "region":
                raise RuntimeError("result cache cannot own a sink attempt")
        retired.extend(_replace_attempt_ref(
            s, "result_cache", key, new_uri, publish=attempt is not None))
        for stale_row in stale:
            retired.extend(_replace_attempt_ref(s, "result_cache", stale_row.key, None))
            s.delete(stale_row)
        if stale:
            _lock_local_result_registry(s)
        # Every object-attempt row/ref is settled before the local registry lock is acquired.
        sync_local_result_owner(s, "result_cache", key, doc)
        for stale_row in stale:
            _drop_local_result_owner_locked(s, "result_cache", stale_row.key)
    return list(dict.fromkeys(retired))


def _doc_org(doc: dict) -> tuple[str, str, str | None, str | None, int | None, list[str], list[str]]:
    """Pull the projectable/indexable fields out of a CatalogTable doc: (tbl_id, folder, owner,
    description, row_count, tags, column-names). The doc stays authoritative; these are its mirror."""
    tbl_id = doc.get("id")
    folder = (doc.get("folder") or "").strip("/")
    owner = doc.get("owner") or None
    description = doc.get("description") or None
    rows = doc.get("rowCount")
    if rows is None:
        rows = doc.get("row_count")
    tags = [str(t) for t in (doc.get("tags") or []) if str(t).strip()]
    cols = [c.get("name") for c in (doc.get("columns") or []) if isinstance(c, dict) and c.get("name")]
    return tbl_id, folder, owner, description, rows, tags, cols


def _sync_children(s, uri: str, tags: list[str], cols: list[str]) -> None:
    """Replace a uri's tag + column rows (the indexable projections) in one transaction."""
    for r in s.scalars(select(CatalogTag).where(CatalogTag.uri == uri)):
        s.delete(r)
    for r in s.scalars(select(CatalogColumn).where(CatalogColumn.uri == uri)):
        s.delete(r)
    s.flush()
    for t in dict.fromkeys(tags):  # de-dupe, preserve order
        s.add(CatalogTag(uri=uri, tag=t))
    for c in dict.fromkeys(cols):
        s.add(CatalogColumn(uri=uri, column=c))


def _catalog_logical_id(logical_uri: str) -> str:
    return "logical_" + hashlib.sha256(str(logical_uri).rstrip("/").encode()).hexdigest()[:24]


def _relationship_key(doc: dict) -> str:
    left_uri = doc.get("leftUri") or doc.get("left_uri")
    right_uri = doc.get("rightUri") or doc.get("right_uri")
    left_cols = doc.get("leftColumns") or doc.get("left_columns") or []
    right_cols = doc.get("rightColumns") or doc.get("right_columns") or []
    return json.dumps(sorted([[left_uri, list(left_cols)], [right_uri, list(right_cols)]]))


def _catalog_token_to_key(s, token: str) -> str:
    token = str(token).rstrip("/")
    logical = s.get(CatalogLogicalDataset, token)
    if logical is None:
        logical = s.scalars(select(CatalogLogicalDataset).where(or_(
            CatalogLogicalDataset.catalog_key == token,
            CatalogLogicalDataset.logical_uri == token,
            CatalogLogicalDataset.current_uri == token,
        )).limit(1)).first()
    if logical is None:
        attempt = s.get(ObjectAttempt, token)
        if attempt is not None and attempt.logical_id:
            logical = s.get(CatalogLogicalDataset, attempt.logical_id)
    if logical is None:
        entry = s.get(CatalogEntry, token)
        if entry is not None and entry.logical_id:
            logical = s.get(CatalogLogicalDataset, entry.logical_id)
    return logical.catalog_key if logical is not None else token


def _catalog_token_logical_id(s, token: str) -> str | None:
    """Resolve a catalog token to its managed logical identity without taking a row lock."""
    token = str(token).rstrip("/")
    logical = s.get(CatalogLogicalDataset, token)
    if logical is None:
        logical = s.scalars(select(CatalogLogicalDataset).where(or_(
            CatalogLogicalDataset.catalog_key == token,
            CatalogLogicalDataset.logical_uri == token,
            CatalogLogicalDataset.current_uri == token,
        )).limit(1)).first()
    if logical is not None:
        return logical.logical_id
    attempt = s.get(ObjectAttempt, token)
    if attempt is not None and attempt.logical_id:
        return attempt.logical_id
    entry = s.get(CatalogEntry, token)
    return entry.logical_id if entry is not None and entry.logical_id else None


def _catalog_key_to_uri(s, token: str) -> str:
    logical = s.scalars(select(CatalogLogicalDataset).where(
        CatalogLogicalDataset.catalog_key == str(token)).limit(1)).first()
    return str(logical.current_uri) if logical is not None and logical.current_uri else str(token)


def _lock_catalog_mutation_targets(s, tokens: list[str], *,
                                   exact_current_attempts: set[str] | None = None) -> list[dict]:
    """Resolve curation targets, lock managed logical rows in deterministic order, and fence stale
    physical generations. Attempt identity is read before logical locks because its publication epoch
    and sequence are immutable; governance paths do not need to lock the attempt row itself."""
    normalized = [str(token).rstrip("/") for token in tokens]
    exact = {str(token).rstrip("/") for token in (exact_current_attempts or set())}
    resolved: list[dict] = []
    logical_ids: set[str] = set()
    unmanaged_uris: set[str] = set()
    for token in normalized:
        attempt = s.get(ObjectAttempt, token)
        logical = None
        entry = None
        if attempt is not None and attempt.logical_id:
            logical_ids.add(attempt.logical_id)
        else:
            logical = s.get(CatalogLogicalDataset, token)
            if logical is None:
                logical = s.scalars(select(CatalogLogicalDataset).where(or_(
                    CatalogLogicalDataset.catalog_key == token,
                    CatalogLogicalDataset.logical_uri == token,
                    CatalogLogicalDataset.current_uri == token,
                )).limit(1)).first()
            if logical is not None:
                logical_ids.add(logical.logical_id)
            else:
                entry = s.get(CatalogEntry, token)
                if entry is None:
                    entry = s.scalars(select(CatalogEntry).where(or_(
                        CatalogEntry.tbl_id == token, CatalogEntry.name == token,
                    )).order_by(CatalogEntry.uri).limit(1)).first()
                if entry is not None and entry.logical_id:
                    logical_ids.add(entry.logical_id)
                elif entry is not None:
                    unmanaged_uris.add(entry.uri)
        resolved.append({
            "token": token,
            "attempt_logical_id": attempt.logical_id if attempt is not None else None,
            "attempt_epoch": attempt.catalog_epoch if attempt is not None else None,
            "attempt_publish_seq": attempt.publish_seq if attempt is not None else None,
            "logical_id": (attempt.logical_id if attempt is not None and attempt.logical_id
                           else logical.logical_id if logical is not None
                           else entry.logical_id if entry is not None else None),
            "entry_uri": entry.uri if entry is not None and not entry.logical_id else None,
        })

    logical_rows = {row.logical_id: row for row in s.scalars(
        select(CatalogLogicalDataset).where(
            CatalogLogicalDataset.logical_id.in_(sorted(logical_ids)))
        .order_by(CatalogLogicalDataset.logical_id).with_for_update())} if logical_ids else {}
    managed_entry_uris = sorted({
        str(row.current_uri) for row in logical_rows.values() if row.current_uri
    })
    entry_uris = sorted(set(managed_entry_uris) | unmanaged_uris)
    entries = {row.uri: row for row in s.scalars(
        select(CatalogEntry).where(CatalogEntry.uri.in_(entry_uris))
        .order_by(CatalogEntry.uri).with_for_update())} if entry_uris else {}

    for target in resolved:
        logical_id = target["logical_id"]
        if logical_id:
            logical = logical_rows.get(logical_id)
            if (logical is None or logical.state != "active" or not logical.current_uri
                    or logical.current_uri not in entries):
                raise RuntimeError("catalog governance target is inactive")
            if target["attempt_logical_id"]:
                if target["attempt_epoch"] != logical.catalog_epoch:
                    raise RuntimeError("catalog governance request was fenced by unregister")
                if (target["token"] in exact
                        and target["attempt_publish_seq"] != logical.current_publish_seq):
                    raise RuntimeError("derived catalog state is stale for the current publication")
            target.update({
                "known": True, "catalog_key": logical.catalog_key,
                "current_uri": logical.current_uri, "logical": logical,
                "entry": entries[logical.current_uri],
            })
        elif target["entry_uri"]:
            entry = entries.get(target["entry_uri"])
            if entry is None or entry.logical_id:
                raise RuntimeError("catalog governance target changed concurrently")
            target.update({
                "known": True, "catalog_key": entry.uri, "current_uri": entry.uri,
                "logical": None, "entry": entry,
            })
        else:
            target.update({
                "known": False, "catalog_key": target["token"],
                "current_uri": None, "logical": None, "entry": None,
            })
    return resolved


def _catalog_governance(doc: dict) -> dict:
    return {key: doc.get(key) for key in (
        "id", "name", "folder", "owner", "description", "tags") if key in doc}


def _catalog_upsert_in_session(s, uri: str, name: str, doc: dict,
                               parents: list[str] | None = None,
                               pipeline: str | None = None) -> None:
    attempt_identity = s.get(ObjectAttempt, uri)
    if attempt_identity is None and object_attempt_uri_shape(uri):
        _validated_object_uri(uri, attempt=True)
        raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
    logical = None
    old_uri = None
    logical_id = None
    locked_entries: dict[str, CatalogEntry] = {}
    attempt = attempt_identity
    parent_snapshots: list[tuple[str, str | None, int | None]] = []
    for token in dict.fromkeys(str(parent).rstrip("/") for parent in (parents or [])):
        parent_attempt = s.get(ObjectAttempt, token)
        parent_snapshots.append((
            token,
            (parent_attempt.logical_id if parent_attempt is not None and parent_attempt.logical_id
             else _catalog_token_logical_id(s, token)),
            parent_attempt.catalog_epoch if parent_attempt is not None else None,
        ))
    if attempt_identity is not None:
        if attempt_identity.kind != "sink":
            raise RuntimeError("catalog cannot publish a region attempt")
        if (not attempt_identity.logical_id or attempt_identity.catalog_epoch is None
                or attempt_identity.publish_seq is None):
            raise RuntimeError("object sink attempt has no reserved logical publication identity")
        logical_id = attempt_identity.logical_id
    logical_ids = sorted(({logical_id} if logical_id is not None else set()) | {
        parent_logical_id for _token, parent_logical_id, _epoch in parent_snapshots
        if parent_logical_id is not None
    })
    locked_logicals = {row.logical_id: row for row in s.scalars(
        select(CatalogLogicalDataset).where(
            CatalogLogicalDataset.logical_id.in_(logical_ids))
        .order_by(CatalogLogicalDataset.logical_id).with_for_update())} if logical_ids else {}
    if attempt_identity is not None:
        logical = locked_logicals.get(logical_id)
        if logical is None:
            raise RuntimeError("object sink attempt logical publication identity is missing")
        old_uri = logical.current_uri
        attempt_uris = sorted({candidate for candidate in (uri, old_uri) if candidate})
        locked_attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(attempt_uris)).order_by(ObjectAttempt.uri).with_for_update())}
        attempt = locked_attempts.get(uri)
        if attempt is None or (old_uri and old_uri not in locked_attempts):
            raise RuntimeError("catalog publication ownership changed concurrently")
        s.get(ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical_id}, with_for_update=True)
        locked_entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(attempt_uris)).order_by(CatalogEntry.uri).with_for_update())}
        if attempt.state not in ("committed", "published"):
            raise RuntimeError("object sink attempt lacks terminal proof or exact inventory")
        if (attempt.logical_id, attempt.catalog_epoch, attempt.publish_seq) != (
                attempt_identity.logical_id, attempt_identity.catalog_epoch,
                attempt_identity.publish_seq):
            raise RuntimeError("catalog publication identity changed concurrently")
        if attempt.catalog_epoch != logical.catalog_epoch:
            raise RuntimeError("object sink attempt was fenced by catalog unregister")
        if attempt.publish_seq <= logical.current_publish_seq:
            raise RuntimeError("object sink attempt publication is older than the current version")
        try:
            governance = json.loads(logical.governance_doc or "{}")
        except (TypeError, ValueError):
            governance = {}
        doc["id"] = logical.catalog_key
        for key in ("name", "folder", "owner", "description", "tags"):
            if key in governance:
                doc[key] = governance[key]

        if old_uri and old_uri != uri:
            for model in (CatalogTag, CatalogColumn):
                for child in s.scalars(select(model).where(model.uri == old_uri)):
                    s.delete(child)
            prior = locked_entries.get(old_uri)
            if prior is not None:
                s.delete(prior)
            s.flush()

    tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
    payload = json.dumps(doc, default=str)
    entry = locked_entries.get(uri) if logical is not None else \
        s.get(CatalogEntry, uri, with_for_update=True)
    if entry is None:
        entry = CatalogEntry(
            uri=uri, name=name, doc=payload, tbl_id=tbl_id, folder=folder,
            owner=owner, description=description, row_count=rows, logical_id=logical_id,
            usage=(logical.usage if logical is not None else 0),
        )
        s.add(entry)
    else:
        entry.name, entry.doc, entry.tbl_id = name, payload, tbl_id
        entry.folder, entry.owner, entry.description, entry.row_count = folder, owner, description, rows
        entry.logical_id = logical_id
        if logical is not None:
            entry.usage = logical.usage
    _sync_children(s, uri, tags, cols)
    s.flush()

    if logical is not None and logical_id is not None:
        logical.current_uri = uri
        logical.current_publish_seq = int(attempt.publish_seq)
        logical.state = "active"
        logical.governance_doc = json.dumps(_catalog_governance(doc), default=str, sort_keys=True)
        _replace_attempt_ref(s, "catalog", logical_id, uri, publish=True)
    child_key = logical.catalog_key if logical is not None else uri
    for parent, parent_logical_id, parent_attempt_epoch in parent_snapshots:
        parent_logical = locked_logicals.get(parent_logical_id) \
            if parent_logical_id is not None else None
        current_parent_attempt = s.execute(select(
            ObjectAttempt.logical_id, ObjectAttempt.catalog_epoch,
        ).where(ObjectAttempt.uri == parent)).one_or_none() \
            if parent_attempt_epoch is not None else None
        parent_attempt_is_current_epoch = (
            parent_attempt_epoch is None or (
                current_parent_attempt is not None
                and current_parent_attempt.logical_id == parent_logical_id
                and current_parent_attempt.catalog_epoch == parent_attempt_epoch
                and parent_logical is not None
                and parent_attempt_epoch == parent_logical.catalog_epoch
            )
        )
        if (parent_logical is not None and parent_logical.state == "active"
                and parent_logical.current_uri and parent_attempt_is_current_epoch):
            parent_key = parent_logical.catalog_key
        elif parent_logical is not None and parent == parent_logical.catalog_key:
            # A stable key cannot remain on an edge after unregister: it would silently attach the
            # historical edge to a future registration. Preserve only the raw logical URI instead.
            parent_key = parent_logical.logical_uri
        else:
            parent_key = parent
        if parent_key == child_key:
            continue
        edge = s.scalars(select(CatalogEdge).where(
            CatalogEdge.parent == parent_key, CatalogEdge.child == child_key).limit(1)).first()
        if edge is None:
            s.add(CatalogEdge(parent=parent_key, child=child_key, pipeline=pipeline))


def catalog_upsert_entry(uri: str, name: str, doc: dict, *,
                         parents: list[str] | None = None,
                         pipeline: str | None = None) -> None:
    """Write-through a catalog entry (registered dataset / written output) to the shared DB, keyed by
    uri, so other instances + a restart see it. `doc` is the full CatalogTable model_dump; its folder /
    owner / description / row_count / tags / column-names are mirrored to indexed columns + join tables
    so browse/search/facet push down to the DB. `usage` (popularity) is owned by the column and NOT
    overwritten from the doc — it's bumped independently on reads."""
    with session() as s:
        normalized = str(uri).rstrip("/")
        payload = dict(doc)
        _catalog_upsert_in_session(
            s, normalized, name, payload, parents=parents, pipeline=pipeline)
        sync_local_result_owner(s, "catalog_entry", normalized, normalized, payload)


def catalog_publish_entries(entries: list[tuple[str, str, dict, list[str] | None, str | None]]) -> None:
    """Reusable atomic publication primitive for a future successful multi-sink batch."""
    with session() as s:
        local_owners: list[tuple[str, dict]] = []
        for uri, name, doc, parents, pipeline in entries:
            normalized = str(uri).rstrip("/")
            payload = dict(doc)
            _catalog_upsert_in_session(
                s, normalized, name, payload,
                parents=parents, pipeline=pipeline)
            local_owners.append((normalized, payload))
        for normalized, payload in local_owners:
            sync_local_result_owner(
                s, "catalog_entry", normalized, normalized, payload)


def catalog_managed_publication_receipt(uri: str) -> dict | None:
    """Core-only durable receipt; execution backends never inspect lifecycle tables themselves."""
    with session() as s:
        attempt = s.get(ObjectAttempt, str(uri).rstrip("/"))
        if attempt is None or attempt.kind != "sink" or attempt.state != "published" \
                or not attempt.logical_id:
            return None
        logical = s.get(CatalogLogicalDataset, attempt.logical_id)
        ref = s.get(ObjectAttemptRef, {"ref_type": "catalog", "ref_key": attempt.logical_id})
        entry = s.get(CatalogEntry, attempt.uri)
        if (logical is None or logical.current_uri != attempt.uri or ref is None
                or ref.attempt_uri != attempt.uri or entry is None):
            return None
        return {
            "uri": attempt.uri, "logical_id": attempt.logical_id,
            "catalog_key": logical.catalog_key, "catalog_epoch": attempt.catalog_epoch,
            "publish_seq": attempt.publish_seq, "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        }


def catalog_set_metadata(uri: str, folder: str, owner: str | None, description: str | None,
                         tags: list[str]) -> None:
    """Update ONLY the organization fields of an entry (folder/owner/description/tags) — both the
    indexed columns AND the mirrored fields inside the stored doc, so a re-read is consistent without
    re-probing the dataset. Unknown or inactive managed targets fail closed."""
    with session() as s:
        target = _lock_catalog_mutation_targets(s, [uri])[0]
        if not target["known"]:
            raise RuntimeError("catalog governance target is not registered")
        logical, r = target["logical"], target["entry"]
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            doc = {}
        doc["folder"], doc["owner"], doc["description"], doc["tags"] = folder, owner, description, list(tags)
        r.folder, r.owner, r.description, r.doc = folder, owner, description, json.dumps(doc, default=str)
        if logical is not None:
            logical.governance_doc = json.dumps(
                _catalog_governance(doc), default=str, sort_keys=True)
            logical.metadata_version += 1
        cols = [c.get("name") for c in doc.get("columns", []) if isinstance(c, dict) and c.get("name")]
        _sync_children(s, r.uri, tags, cols)


def catalog_bump_usage(uri: str, n: int = 1) -> None:
    """Increment a dataset's read-count popularity (called best-effort when it's sampled / read in a
    run). An atomic `usage = usage + n` (concurrent bumps can't lose increments) that explicitly
    carries updated_at, so a READ never masquerades as an update in the 'Recently updated' sort."""
    with session() as s:
        target = _lock_catalog_mutation_targets(s, [uri])[0]
        if not target["known"]:
            return
        logical, current_uri = target["logical"], target["current_uri"]
        s.execute(update(CatalogEntry).where(CatalogEntry.uri == current_uri)
                  .values(usage=CatalogEntry.usage + n, updated_at=CatalogEntry.updated_at))
        if logical is not None:
            logical.usage += n


def catalog_record_output_publication(event_key: str, uri: str, version: str | None) -> None:
    """Persist a receipt only after the referenced catalog entry is durably readable.

    The entry upsert and this receipt are separate retry-safe commits: a crash between them leaves an
    unacknowledged entry that the next call re-upserts, while a crash after the receipt replays the same
    event. Durable backends must not expose terminal success until this function returns.
    """
    if not event_key:
        raise ValueError("catalog publication event_key is required")
    if not uri:
        raise ValueError("catalog publication uri is required")
    try:
        with session() as s:
            if s.get(CatalogEntry, uri) is None:
                raise RuntimeError(f"catalog output is not durably readable: {uri}")
            event = s.get(CatalogPublicationEvent, event_key)
            if event is not None:
                if event.effect_type != "output" or event.uri != uri or event.version != version:
                    raise RuntimeError(f"catalog publication key collision: {event_key}")
                return
            s.add(CatalogPublicationEvent(
                event_key=event_key, effect_type="output", uri=uri, version=version
            ))
            s.flush()
    except IntegrityError:
        # A concurrent publisher may win the primary-key insert. Validate its committed receipt rather
        # than treating an unrelated event-key collision as idempotent success.
        with session() as s:
            event = s.get(CatalogPublicationEvent, event_key)
            if (event is None or event.effect_type != "output" or event.uri != uri
                    or event.version != version or s.get(CatalogEntry, uri) is None):
                raise RuntimeError(f"catalog publication key collision: {event_key}") from None


def catalog_bump_usage_once(event_key: str, uris: list[str]) -> bool:
    """Apply one durable publication event to every distinct parent URI exactly once."""
    if not event_key:
        raise ValueError("catalog publication event_key is required")
    try:
        with session() as s:
            event = s.get(CatalogPublicationEvent, event_key)
            if event is not None:
                if event.effect_type != "usage":
                    raise RuntimeError(f"catalog publication key collision: {event_key}")
                return False
            s.add(CatalogPublicationEvent(event_key=event_key, effect_type="usage"))
            s.flush()  # unique PK is the cross-process idempotency fence
            for uri in dict.fromkeys(str(value) for value in uris if value):
                s.execute(
                    update(CatalogEntry).where(CatalogEntry.uri == uri)
                    .values(usage=CatalogEntry.usage + 1, updated_at=CatalogEntry.updated_at)
                )
            return True
    except IntegrityError:
        with session() as s:
            event = s.get(CatalogPublicationEvent, event_key)
            if event is None or event.effect_type != "usage":
                raise RuntimeError(f"catalog publication key collision: {event_key}") from None
            return False


def catalog_add_edge(parent: str, child: str, pipeline: str | None = None,
                     column: str | None = None) -> bool:
    """Write-through a lineage edge; one row per (parent, child). `column` records column-level
    provenance when known. Returns whether this call created the edge."""
    if parent == child:
        return False
    with session() as s:
        parent_target, child_target = _lock_catalog_mutation_targets(s, [parent, child])
        if not child_target["known"]:
            raise RuntimeError("catalog lineage child is not registered")
        parent_key, child_key = parent_target["catalog_key"], child_target["catalog_key"]
        if parent_key == child_key:
            return False
        edge = s.scalars(select(CatalogEdge).where(
            CatalogEdge.parent == parent_key, CatalogEdge.child == child_key)).first()
        if edge is None:
            s.add(CatalogEdge(
                parent=parent_key, child=child_key, pipeline=pipeline, column=column))
            s.flush()
            return True
        if column and not edge.column:
            edge.column = column
        return False


def _row_to_doc(r: "CatalogEntry", tags: list[str]) -> dict:
    """Materialize a CatalogTable-shaped dict from a row, overlaying the authoritative indexed
    columns (id/folder/owner/description/usage) + the tag rows onto the stored doc."""
    try:
        d = json.loads(r.doc)
    except (ValueError, TypeError):
        d = {"id": r.tbl_id or f"tbl_{r.name}", "name": r.name, "uri": r.uri}
    d["id"] = r.tbl_id or d.get("id") or f"tbl_{r.name}"
    d["folder"] = r.folder or ""
    d["owner"] = r.owner
    d["description"] = r.description
    d["usage"] = r.usage or 0
    d["tags"] = tags
    return d


def _tags_for(s, uris: list[str]) -> dict[str, list[str]]:
    """{uri: [tag, ...]} for a batch of uris — one query, so a page of N rows costs one round-trip."""
    out: dict[str, list[str]] = {u: [] for u in uris}
    if not uris:
        return out
    for row in s.scalars(select(CatalogTag).where(CatalogTag.uri.in_(uris))):
        out.setdefault(row.uri, []).append(row.tag)
    return out


def _like_escape(s: str) -> str:
    """Escape LIKE metacharacters in user input so a literal % / _ matches itself (used with ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _catalog_filters(q: str | None, folder: str | None, tags: list[str] | None,
                     owner: str | None, has_columns: list[str] | None,
                     uris: list[str] | None = None) -> list:
    """The WHERE terms shared by catalog_query + catalog_facets, so a page and its facet counts always
    describe the SAME filtered set."""
    from sqlalchemy import exists
    terms: list = []
    # every whitespace token must match SOMEWHERE (name/uri/folder/description/column/tag), so
    # "curated images" finds demo/images/curated even though the words never appear adjacent
    for token in (q or "").lower().split():
        like = f"%{_like_escape(token)}%"
        terms.append(or_(
            func.lower(CatalogEntry.name).like(like, escape="\\"),
            func.lower(CatalogEntry.uri).like(like, escape="\\"),
            func.lower(CatalogEntry.folder).like(like, escape="\\"),
            func.lower(func.coalesce(CatalogEntry.description, "")).like(like, escape="\\"),
            exists().where(CatalogColumn.uri == CatalogEntry.uri,
                           func.lower(CatalogColumn.column).like(like, escape="\\")),
            exists().where(CatalogTag.uri == CatalogEntry.uri,
                           func.lower(CatalogTag.tag).like(like, escape="\\")),
        ))
    if uris:
        terms.append(CatalogEntry.uri.in_(list(uris)))
    if folder:
        f = folder.strip("/")
        terms.append(or_(CatalogEntry.folder == f,
                         CatalogEntry.folder.like(_like_escape(f) + "/%", escape="\\")))
    for t in (tags or []):
        terms.append(exists().where(CatalogTag.uri == CatalogEntry.uri, CatalogTag.tag == t))
    if owner:
        terms.append(CatalogEntry.owner == owner)
    for c in (has_columns or []):
        terms.append(exists().where(CatalogColumn.uri == CatalogEntry.uri, CatalogColumn.column == c))
    return terms


_SORT_COLS = {"name": CatalogEntry.name, "rows": CatalogEntry.row_count,
              "updated": CatalogEntry.updated_at, "usage": CatalogEntry.usage,
              "folder": CatalogEntry.folder}


def catalog_query(q: str | None = None, folder: str | None = None, tags: list[str] | None = None,
                  owner: str | None = None, has_columns: list[str] | None = None,
                  uris: list[str] | None = None,
                  sort: str = "name", order: str = "asc", limit: int = 50, offset: int = 0,
                  ) -> tuple[list[dict], int]:
    """A filtered, sorted, paginated window over the catalog PUSHED DOWN to the DB (indexed columns +
    EXISTS subqueries), plus the total match count. Returns (docs, total). This is the scalable core:
    memory + wire cost are bounded by `limit`, not by the catalog size."""
    terms = _catalog_filters(q, folder, tags, owner, has_columns, uris)
    col = _SORT_COLS.get(sort, CatalogEntry.name)
    with session() as s:
        total = s.scalar(select(func.count()).select_from(CatalogEntry).where(*terms)) or 0
        stmt = select(CatalogEntry).where(*terms)
        primary = col.desc() if order == "desc" else col.asc()
        # a stable tiebreak (name, then uri) keeps paging deterministic when many rows share a sort key
        stmt = stmt.order_by(primary, CatalogEntry.name.asc(), CatalogEntry.uri.asc())
        rows = list(s.scalars(stmt.limit(max(0, limit)).offset(max(0, offset))))
        tag_map = _tags_for(s, [r.uri for r in rows])
        docs = [_row_to_doc(r, tag_map.get(r.uri, [])) for r in rows]
    return docs, int(total)


def catalog_filter_uris(folder: str | None = None, tags: list[str] | None = None,
                        owner: str | None = None, has_columns: list[str] | None = None,
                        uris: list[str] | None = None) -> set[str] | None:
    """URIs allowed by structured catalog filters, or ``None`` when there are no such filters.

    Semantic search already scores the complete embedding matrix.  Restricting that matrix with one
    URI-only DB query keeps ranking exact without loading catalog documents or applying filters after
    the top-k cutoff (which can incorrectly return too few matches).
    """
    if not (folder or tags or owner or has_columns or uris):
        return None
    terms = _catalog_filters(None, folder, tags, owner, has_columns, uris)
    with session() as s:
        return set(s.scalars(select(CatalogEntry.uri).where(*terms)))


def catalog_facets(q: str | None = None, folder: str | None = None, tags: list[str] | None = None,
                   owner: str | None = None, has_columns: list[str] | None = None, top: int = 100,
                   ) -> dict[str, list[tuple[str, int]]]:
    """Distinct values + counts for the folder / tag / owner dimensions over the ACTIVE filter set
    (drill-down semantics). Each list is capped to the `top` most common. Powers the facet rail."""
    terms = _catalog_filters(q, folder, tags, owner, has_columns)
    with session() as s:
        folders = [(v or "", int(c)) for v, c in s.execute(
            select(CatalogEntry.folder, func.count()).where(*terms, CatalogEntry.folder != "")
            .group_by(CatalogEntry.folder).order_by(func.count().desc()).limit(top)).all()]
        owners = [(v, int(c)) for v, c in s.execute(
            select(CatalogEntry.owner, func.count()).where(*terms, CatalogEntry.owner.is_not(None))
            .group_by(CatalogEntry.owner).order_by(func.count().desc()).limit(top)).all()]
        # tags live in a join table → count distinct entries per tag within the filtered set
        uri_sq = select(CatalogEntry.uri).where(*terms).subquery()
        tag_rows = s.execute(
            select(CatalogTag.tag, func.count()).where(CatalogTag.uri.in_(select(uri_sq.c.uri)))
            .group_by(CatalogTag.tag).order_by(func.count().desc()).limit(top)).all()
        tag_counts = [(v, int(c)) for v, c in tag_rows]
    return {"folders": folders, "tags": tag_counts, "owners": owners}


def catalog_tree(prefix: str = "", table_limit: int = 100
                 ) -> tuple[list[tuple[str, str, int]], list[dict], int]:
    """One level of the browse tree at `prefix`: (child_folders, direct_tables, direct_total).
    child_folders is a list of (name, path, subtree_table_count) for the immediate sub-folders;
    direct_tables are the first `table_limit` tables filed exactly at `prefix` (direct_total says how
    many exist, so a caller can signal truncation). Folder aggregation is over the (small) set of
    distinct folders, so this scales with the number of FOLDERS, not tables."""
    p = (prefix or "").strip("/")
    depth = 0 if not p else p.count("/") + 1
    with session() as s:
        folder_counts = s.execute(
            select(CatalogEntry.folder, func.count()).where(CatalogEntry.folder != "")
            .group_by(CatalogEntry.folder)).all()
        children: dict[str, int] = {}
        for folder, cnt in folder_counts:
            f = (folder or "").strip("/")
            if p:
                if f != p and not f.startswith(p + "/"):
                    continue
            segs = f.split("/")
            if len(segs) <= depth:
                continue  # this folder is AT the prefix (no deeper child segment)
            child_path = "/".join(segs[: depth + 1])
            children[child_path] = children.get(child_path, 0) + int(cnt)
        child_list = sorted(
            ((cp.split("/")[-1], cp, n) for cp, n in children.items()), key=lambda x: x[0].lower())
        direct_total = int(s.scalar(select(func.count()).select_from(CatalogEntry)
                                    .where(CatalogEntry.folder == p)) or 0)
        direct_rows = list(s.scalars(
            select(CatalogEntry).where(CatalogEntry.folder == p).order_by(CatalogEntry.name).limit(table_limit)))
        tag_map = _tags_for(s, [r.uri for r in direct_rows])
        tables = [_row_to_doc(r, tag_map.get(r.uri, [])) for r in direct_rows]
    return child_list, tables, direct_total


def catalog_get(token: str) -> dict | None:
    """A single entry by uri (PK), then by tbl_id, then by name — all indexed. None if unknown.
    Replaces the old 'load the whole catalog then look it up' path, so get_table is O(1), not O(n)."""
    with session() as s:
        catalog_key = _catalog_token_to_key(s, token)
        logical = s.scalars(select(CatalogLogicalDataset).where(
            CatalogLogicalDataset.catalog_key == catalog_key).limit(1)).first()
        r = s.get(CatalogEntry, logical.current_uri) \
            if logical is not None and logical.current_uri else None
        if r is None and logical is None:
            r = s.get(CatalogEntry, token)
        if r is None:
            r = s.scalars(select(CatalogEntry).where(CatalogEntry.tbl_id == token).limit(1)).first()
        if r is None:
            r = s.scalars(select(CatalogEntry).where(CatalogEntry.name == token).limit(1)).first()
        if r is None:
            return None
        return _row_to_doc(r, [t.tag for t in s.scalars(select(CatalogTag).where(CatalogTag.uri == r.uri))])


def catalog_get_many(uris: list[str]) -> dict[str, dict]:
    """{uri: doc} for a batch of uris — used to name lineage-graph nodes in one round-trip."""
    if not uris:
        return {}
    with session() as s:
        rows = list(s.scalars(select(CatalogEntry).where(CatalogEntry.uri.in_(uris))))
        tag_map = _tags_for(s, [r.uri for r in rows])
        return {r.uri: _row_to_doc(r, tag_map.get(r.uri, [])) for r in rows}


def catalog_entries() -> list[dict]:
    """Every persisted catalog entry, as CatalogTable-shaped dicts. Retained for tooling/back-compat;
    the browse path uses catalog_query (bounded), not this (unbounded) scan."""
    with session() as s:
        rows = list(s.scalars(select(CatalogEntry)))
        tag_map = _tags_for(s, [r.uri for r in rows])
        return [_row_to_doc(r, tag_map.get(r.uri, [])) for r in rows]


def _delete_catalog_children(s, uris: list[str]) -> None:
    """Remove EVERY row keyed to `uris` alongside the entries themselves — tags/columns/embeddings,
    lineage edges (either endpoint), declared keys, and relationships. Otherwise a deleted table
    haunts lineage/ER as a ghost node, and a NEW dataset re-registered at the same uri silently
    inherits the old declared key + parents."""
    for model in (CatalogTag, CatalogColumn):
        for r in s.scalars(select(model).where(model.uri.in_(uris))):
            s.delete(r)
    for r in s.scalars(select(CatalogEdge).where(
            or_(CatalogEdge.parent.in_(uris), CatalogEdge.child.in_(uris)))):
        s.delete(r)
    gone = set(uris)
    for r in s.scalars(select(CatalogRelationship)):
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            continue
        if doc.get("leftUri") in gone or doc.get("rightUri") in gone \
                or doc.get("left_uri") in gone or doc.get("right_uri") in gone:
            s.delete(r)


def _delete_catalog_governance(s, catalog_key: str) -> None:
    for model in (CatalogEmbedding, CatalogDeclaredKey):
        row = s.get(model, catalog_key)
        if row is not None:
            s.delete(row)
    for edge in s.scalars(select(CatalogEdge).where(or_(
            CatalogEdge.parent == catalog_key, CatalogEdge.child == catalog_key))):
        s.delete(edge)
    for relationship in s.scalars(select(CatalogRelationship)):
        try:
            doc = json.loads(relationship.doc)
        except (TypeError, ValueError):
            continue
        if catalog_key in (
                doc.get("leftUri"), doc.get("left_uri"),
                doc.get("rightUri"), doc.get("right_uri")):
            s.delete(relationship)


def catalog_delete_entry(uri: str) -> None:
    """Remove a catalog entry (unregister) + everything keyed to it (tags/columns/embedding/edges/
    declared key/relationships)."""
    with session() as s:
        token = str(uri).rstrip("/")
        attempt_identity = s.get(ObjectAttempt, token)
        logical_id = attempt_identity.logical_id if attempt_identity is not None else None
        logical_snapshot = None
        if logical_id is None:
            logical_snapshot = s.get(CatalogLogicalDataset, token)
            if logical_snapshot is None:
                logical_snapshot = s.scalars(select(CatalogLogicalDataset).where(or_(
                    CatalogLogicalDataset.catalog_key == token,
                    CatalogLogicalDataset.logical_uri == token,
                    CatalogLogicalDataset.current_uri == token,
                )).limit(1)).first()
            logical_id = logical_snapshot.logical_id if logical_snapshot is not None else None
        logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=True) \
            if logical_id else None
        if logical is not None:
            if logical.state != "active" or not logical.current_uri:
                raise RuntimeError("catalog governance target is inactive")
            if (attempt_identity is not None
                    and attempt_identity.catalog_epoch != logical.catalog_epoch):
                raise RuntimeError("catalog governance request was fenced by unregister")
            current_uri = logical.current_uri
            current_attempt = s.get(ObjectAttempt, current_uri, with_for_update=True)
            if current_attempt is None or current_attempt.logical_id != logical.logical_id:
                raise RuntimeError("catalog unregister ownership changed concurrently")
            s.get(ObjectAttemptRef, {
                "ref_type": "catalog", "ref_key": logical.logical_id}, with_for_update=True)
            entry = s.get(CatalogEntry, current_uri, with_for_update=True)
            if entry is None or entry.logical_id != logical.logical_id:
                raise RuntimeError("catalog unregister entry changed concurrently")
            catalog_key = logical.catalog_key
        else:
            entry_snapshot = s.get(CatalogEntry, token)
            if entry_snapshot is None:
                entry_snapshot = s.scalars(select(CatalogEntry).where(or_(
                    CatalogEntry.tbl_id == token, CatalogEntry.name == token,
                )).order_by(CatalogEntry.uri).limit(1)).first()
            if entry_snapshot is None:
                return
            entry = s.get(CatalogEntry, entry_snapshot.uri, with_for_update=True)
            if entry is None or entry.logical_id:
                raise RuntimeError("catalog unregister entry changed concurrently")
            current_uri, catalog_key = entry.uri, entry.uri
        if logical is not None:
            _replace_attempt_ref(s, "catalog", logical.logical_id, None)
            logical.current_uri = None
            logical.catalog_epoch += 1
            logical.state = "unregistered"
            logical.metadata_version += 1
            logical.governance_doc = "{}"
        _delete_catalog_governance(s, catalog_key)
        if current_uri:
            _delete_catalog_children(s, [current_uri])
        if entry is not None:
            s.delete(entry)
        # Object governance/ref mutations above always precede the local registry lock.
        _drop_local_result_owner(s, "catalog_entry", current_uri)


def catalog_delete_prefix(uri_prefix: str) -> int:
    """Delete every entry (+ everything keyed to it) whose uri starts with `uri_prefix`. Returns the
    count removed. For bulk teardown of demo/scale entries; a no-op for a prefix that matches none."""
    like = _like_escape(uri_prefix) + "%"
    with session() as s:
        snapshot = list(s.execute(select(CatalogEntry.uri, CatalogEntry.logical_id).where(
            CatalogEntry.uri.like(like, escape="\\")).order_by(CatalogEntry.uri)).all())
        if not snapshot:
            return 0
        uris = [uri for uri, _logical_id in snapshot]
        logical_ids = sorted({logical_id for _uri, logical_id in snapshot if logical_id})
        logical_rows = {row.logical_id: row for row in s.scalars(
            select(CatalogLogicalDataset).where(
                CatalogLogicalDataset.logical_id.in_(logical_ids))
            .order_by(CatalogLogicalDataset.logical_id).with_for_update())} if logical_ids else {}
        managed_uris = sorted(uri for uri, logical_id in snapshot if logical_id)
        attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(managed_uris)).order_by(ObjectAttempt.uri).with_for_update())} \
            if managed_uris else {}
        refs = {row.ref_key: row for row in s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "catalog",
            ObjectAttemptRef.ref_key.in_(logical_ids),
        ).order_by(ObjectAttemptRef.ref_key).with_for_update())} if logical_ids else {}
        entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(uris)).order_by(CatalogEntry.uri).with_for_update())}
        if len(entries) != len(snapshot):
            raise RuntimeError("catalog prefix changed concurrently")
        for uri, logical_id in snapshot:
            if logical_id:
                logical = logical_rows.get(logical_id)
                if (logical is None or logical.state != "active"
                        or logical.current_uri != uri or uri not in attempts
                        or logical_id not in refs or refs[logical_id].attempt_uri != uri):
                    raise RuntimeError("catalog prefix changed concurrently")
            elif entries[uri].logical_id:
                raise RuntimeError("catalog prefix changed concurrently")
        current_uris = list(uris)
        _delete_catalog_children(s, current_uris)
        for uri, logical_id in snapshot:
            logical = logical_rows.get(logical_id) if logical_id else None
            if logical is not None:
                _replace_attempt_ref(s, "catalog", logical.logical_id, None)
                logical.current_uri = None
                logical.catalog_epoch += 1
                logical.state = "unregistered"
                logical.metadata_version += 1
                logical.governance_doc = "{}"
                _delete_catalog_governance(s, logical.catalog_key)
            s.delete(entries[uri])
        _lock_local_result_registry(s)
        for uri in current_uris:
            _drop_local_result_owner_locked(s, "catalog_entry", uri)
    return len(current_uris)


def catalog_edges() -> list[dict]:
    with session() as s:
        return [{"parent": _catalog_key_to_uri(s, r.parent),
                 "child": _catalog_key_to_uri(s, r.child),
                 "column": r.column, "pipeline": r.pipeline}
                for r in s.scalars(select(CatalogEdge))]


def catalog_edges_touching(uris: list[str], limit: int | None = None) -> list[dict]:
    """Every edge with an endpoint in `uris` — the frontier expansion step of a bounded lineage BFS
    (so lineage never loads the whole edge table). `limit` caps a pathologically-connected frontier
    (a hub node with 100k children) so one expansion can't load unbounded rows; the caller treats a
    full batch as truncation."""
    if not uris:
        return []
    with session() as s:
        keys = list(dict.fromkeys(_catalog_token_to_key(s, uri) for uri in uris))
        stmt = select(CatalogEdge).where(
            or_(CatalogEdge.parent.in_(keys), CatalogEdge.child.in_(keys)))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = s.scalars(stmt)
        return [{"parent": _catalog_key_to_uri(s, r.parent),
                 "child": _catalog_key_to_uri(s, r.child),
                 "column": r.column, "pipeline": r.pipeline} for r in rows]


def catalog_edges_page(limit: int = 500, offset: int = 0) -> tuple[list[dict], int]:
    """One page of the whole lineage edge set + the total count — the bulk-export surface an external
    lineage store (e.g. an OpenLineage bridge plugin) syncs from."""
    with session() as s:
        total = s.scalar(select(func.count()).select_from(CatalogEdge)) or 0
        rows = s.scalars(select(CatalogEdge).order_by(CatalogEdge.id.asc())
                         .limit(max(0, limit)).offset(max(0, offset)))
        return ([{"parent": _catalog_key_to_uri(s, r.parent),
                  "child": _catalog_key_to_uri(s, r.child),
                  "column": r.column, "pipeline": r.pipeline}
                 for r in rows], int(total))


# -- semantic search (opt-in: only populated when an embedder is registered) ----------------------- #
def catalog_set_embedding(uri: str, model: str, dim: int, vec: bytes) -> None:
    with session() as s:
        target = _lock_catalog_mutation_targets(
            s, [uri], exact_current_attempts={str(uri).rstrip("/")})[0]
        if not target["known"]:
            raise RuntimeError("catalog embedding target is not registered")
        catalog_key = target["catalog_key"]
        r = s.get(CatalogEmbedding, catalog_key)
        if r is None:
            s.add(CatalogEmbedding(catalog_key=catalog_key, model=model, dim=dim, vec=vec))
        else:
            r.model, r.dim, r.vec = model, dim, vec


def catalog_embeddings_for(model: str) -> list[tuple[str, bytes]]:
    """(uri, vec-bytes) for every embedding under `model` — the candidate set semantic search scores."""
    with session() as s:
        return [(_catalog_key_to_uri(s, r.catalog_key), r.vec) for r in s.scalars(
            select(CatalogEmbedding).where(CatalogEmbedding.model == model))]


def catalog_bulk_seed(entries: list[dict]) -> int:
    """Register many synthetic/pre-built entries in one transaction (skipping uris already present) —
    for demos + the scale acceptance test. Each entry: {uri, name, doc, folder?, tags?, owner?,
    description?, rowCount?}. Returns how many were inserted."""
    n = 0
    with session() as s:
        existing = {u for (u,) in s.execute(select(CatalogEntry.uri).where(
            CatalogEntry.uri.in_([e["uri"] for e in entries]))).all()}
        local_owners: list[tuple[str, dict]] = []
        for e in entries:
            uri = e["uri"]
            if uri in existing:
                continue
            doc = e.get("doc") or {}
            tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
            s.add(CatalogEntry(uri=uri, name=e["name"], doc=json.dumps(doc, default=str), tbl_id=tbl_id,
                               folder=folder, owner=owner, description=description, row_count=rows))
            for t in dict.fromkeys(tags):
                s.add(CatalogTag(uri=uri, tag=t))
            for c in dict.fromkeys(cols):
                s.add(CatalogColumn(uri=uri, column=c))
            local_owners.append((uri, doc))
            n += 1
        s.flush()
        for uri, doc in local_owners:
            sync_local_result_owner(s, "catalog_entry", uri, uri, doc)
    return n


def catalog_relationships() -> list[dict]:
    """Every declared relationship as a Relationship-shaped dict."""
    with session() as s:
        out = []
        for row in s.scalars(select(CatalogRelationship)):
            doc = json.loads(row.doc)
            for key in ("leftUri", "left_uri", "rightUri", "right_uri"):
                if doc.get(key):
                    doc[key] = _catalog_key_to_uri(s, doc[key])
            out.append(doc)
        return out


def catalog_upsert_relationship(rel_key: str, doc: dict) -> None:
    """Insert or replace ONE relationship row (keyed by rel_key) — no read-modify-write of a shared
    blob, so a concurrent declare of a DIFFERENT relationship on another instance can't be lost."""
    with session() as s:
        doc = dict(doc)
        endpoint_keys = [key for key in ("leftUri", "left_uri", "rightUri", "right_uri")
                         if doc.get(key)]
        targets = _lock_catalog_mutation_targets(s, [doc[key] for key in endpoint_keys])
        if endpoint_keys and not any(target["known"] for target in targets):
            raise RuntimeError("catalog relationship has no registered endpoint")
        for key, target in zip(endpoint_keys, targets):
            doc[key] = target["catalog_key"]
        rel_key = _relationship_key(doc) if endpoint_keys else str(rel_key)
        r = s.get(CatalogRelationship, rel_key)
        payload = json.dumps(doc, default=str)
        if r is None:
            s.add(CatalogRelationship(rel_key=rel_key, doc=payload))
        else:
            r.doc = payload


def catalog_delete_relationship(rel_key: str) -> None:
    with session() as s:
        try:
            ends = json.loads(rel_key)
            flat = [uri for uri, _columns in ends]
            targets = _lock_catalog_mutation_targets(s, flat)
            rel_key = json.dumps(sorted([
                [target["catalog_key"], list(columns)]
                for target, (_uri, columns) in zip(targets, ends)
            ]))
        except (TypeError, ValueError):
            pass
        r = s.get(CatalogRelationship, rel_key)
        if r is not None:
            s.delete(r)


def catalog_declared_keys(uris: list[str] | None = None) -> dict[str, list]:
    """{uri: [column, ...]} for the declared primary keys of `uris` (an indexed PK batch lookup — the
    read path passes the page's uris so this stays O(page), never O(catalog)). None → all keys."""
    with session() as s:
        stmt = select(CatalogDeclaredKey)
        requested: dict[str, str] | None = None
        if uris is not None:
            if not uris:
                return {}
            requested = {str(uri): _catalog_token_to_key(s, uri) for uri in uris}
            stmt = stmt.where(CatalogDeclaredKey.catalog_key.in_(requested.values()))
        rows = {r.catalog_key: json.loads(r.columns) for r in s.scalars(stmt)}
        if requested is not None:
            return {token: rows[key] for token, key in requested.items() if key in rows}
        return {_catalog_key_to_uri(s, key): value for key, value in rows.items()}


def catalog_set_declared_key(uri: str, columns: list) -> None:
    """Set (columns non-empty) or clear (empty) ONE dataset's declared key — a single row, so it
    can't clobber another dataset's key set concurrently on another instance."""
    with session() as s:
        target = _lock_catalog_mutation_targets(s, [uri])[0]
        if not target["known"]:
            raise RuntimeError("catalog declared-key target is not registered")
        catalog_key = target["catalog_key"]
        r = s.get(CatalogDeclaredKey, catalog_key)
        if columns:
            payload = json.dumps(list(columns))
            if r is None:
                s.add(CatalogDeclaredKey(catalog_key=catalog_key, columns=payload))
            else:
                r.columns = payload
        elif r is not None:
            s.delete(r)


# --------------------------------------------------------------------------- #
# Kernels — the per-canvas execution-kernel lease (Phase 1 of the session kernel)
# --------------------------------------------------------------------------- #
KERNEL_STALE_S = 30  # a kernel whose heartbeat is older than this is presumed dead


def _stale_secs(dt: "datetime.datetime | None") -> float:
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:  # SQLite reads DateTime back naive — treat stored times as UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (_now() - dt).total_seconds()


def _kernel_stale(r: "Kernel") -> bool:
    return _stale_secs(r.heartbeat_at) >= KERNEL_STALE_S


def claim_kernel(canvas_id: str, kernel_id: str, token: str) -> dict:
    """Atomically claim the right to spawn THE kernel for a canvas — the single-spawner guard the
    no-split-brain invariant rests on. Returns {won, endpoint, state, kernel_id}: won=True → this
    caller holds the lease under `kernel_id` and should spawn; won=False → a live kernel already
    exists, use its `endpoint`. A stale/dead lease is taken over (rebinding fences the old kernel_id)."""
    now = _now()
    with session() as s:
        # row-lock the read so a concurrent stale-lease takeover serializes: the second claimer blocks,
        # then re-reads the freshly-rebound (no-longer-stale) row and loses (won=False). Without it two
        # hubs could both read the same stale row, both pass the staleness check, and both spawn.
        # (SELECT … FOR UPDATE on Postgres — the prod DB; a no-op on single-process SQLite dev.)
        r = s.get(Kernel, canvas_id, with_for_update=True)
        if r is not None:
            if not _kernel_stale(r):
                return {"won": False, "endpoint": r.endpoint, "state": r.state, "kernel_id": r.kernel_id}
            r.kernel_id, r.token, r.state = kernel_id, token, "starting"  # take over (fences the old id)
            r.endpoint, r.heartbeat_at, r.started_at = None, now, now
            return {"won": True, "endpoint": None, "state": "starting", "kernel_id": kernel_id}
        s.add(Kernel(canvas_id=canvas_id, kernel_id=kernel_id, token=token, state="starting",
                     heartbeat_at=now, started_at=now))
        try:
            s.flush()  # a concurrent creator makes the PK insert fail → we lost the race
        except IntegrityError:
            s.rollback()
            r = s.get(Kernel, canvas_id)
            return {"won": False, "endpoint": r.endpoint if r else None,
                    "state": r.state if r else "starting", "kernel_id": r.kernel_id if r else ""}
        return {"won": True, "endpoint": None, "state": "starting", "kernel_id": kernel_id}


def _fenced(s, canvas_id: str, kernel_id: str) -> "Kernel | None":
    """The lease row IFF it's still ours — a kernel fenced out (replaced by a newer one) sees None."""
    r = s.get(Kernel, canvas_id)
    return r if (r is not None and r.kernel_id == kernel_id) else None


def mark_kernel_ready(canvas_id: str, kernel_id: str, endpoint: str) -> bool:
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is None:
            return False
        r.state, r.endpoint, r.heartbeat_at = "ready", endpoint, _now()
        return True


def heartbeat_kernel(canvas_id: str, kernel_id: str) -> bool:
    """Touch the lease. False if we've been fenced out (a newer kernel took over) → the kernel exits."""
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is None:
            return False
        r.heartbeat_at = _now()
        return True


def drop_kernel(canvas_id: str, kernel_id: str) -> None:
    """Release our lease (idle-exit / explicit shutdown). Fenced: a zombie can't delete the new owner."""
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is not None:
            s.delete(r)


def get_kernel(canvas_id: str) -> dict | None:
    with session() as s:
        r = s.get(Kernel, canvas_id)
        if r is None:
            return None
        return {"canvas_id": r.canvas_id, "kernel_id": r.kernel_id, "endpoint": r.endpoint,
                "token": r.token, "state": r.state, "stale": _kernel_stale(r)}


def active_runs(canvas_id: str) -> list[dict]:
    """In-flight runs (queued/running) for a canvas — so a reopened canvas re-subscribes to a run that
    outlived a hub restart (its kernel kept it alive). Returns each run's last-known RunStatus dict."""
    out = []
    with session() as s:
        for r in s.scalars(select(RunState).where(RunState.canvas_id == canvas_id,
                                                   RunState.status.in_(("queued", "running")))):
            try:
                out.append(json.loads(r.doc))
            except Exception:  # noqa: BLE001
                out.append({"run_id": r.run_id, "status": r.status})
    return out


def kernel_for_run(run_id: str) -> dict | None:
    """The kernel that OWNS a run (endpoint + token) — for routing cancel to it. None if the run's owning
    kernel no longer holds the canvas lease (fenced / replaced / gone): routing /cancel to whatever kernel
    now holds the canvas would hit one that never knew this run (an HTTP error), so the caller instead
    falls back to the last-known persisted status."""
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None or not r.canvas_id:
            return None
        k = s.get(Kernel, r.canvas_id)
        if k is None or (r.kernel_id and k.kernel_id != r.kernel_id):
            return None
        return {"endpoint": k.endpoint, "token": k.token, "kernel_id": k.kernel_id}


def reap_kernels() -> list[tuple[str, str]]:
    """Delete leases whose kernel is presumed dead (stale heartbeat). Any hub, on boot + on a timer.
    Returns the reaped (canvas_id, kernel_id) pairs so the caller can also tear down the substrate
    (delete the pod + service) — otherwise a crashed/fenced pod's k8s objects accumulate as orphans."""
    reaped: list[tuple[str, str]] = []
    with session() as s:
        for r in s.scalars(select(Kernel)):
            if _kernel_stale(r):
                reaped.append((r.canvas_id, r.kernel_id))
                s.delete(r)
    return reaped


def reap_orphaned_runs(only_kernel_runs: bool = False) -> int:
    """Fail a non-terminal run whose owning kernel is gone/stale. A run owned by a still-live kernel is
    LEFT running so the client reattaches — replacing the old blanket "fail every run on restart".

    `only_kernel_runs` distinguishes the two callers: on hub BOOT (default False) a kernel-less run —
    an in-process / subprocess run — belonged to the now-dead previous hub, so it is reaped too. On the
    PERIODIC path (True) a kernel-less run belongs to THIS live hub process (or, across instances, to
    another live one) and must NOT be reaped mid-flight — only its dead-kernel runs are."""
    n = 0
    with session() as s:
        live = {k.kernel_id for k in s.scalars(select(Kernel)) if not _kernel_stale(k)}
        reaped_run_ids: list[str] = []
        candidate = select(RunState).where(
            RunState.status.in_(("queued", "running")),
            ~exists().where(RunBackendJob.run_id == RunState.run_id),
        )
        if only_kernel_runs:
            candidate = candidate.where(RunState.kernel_id.is_not(None))
            if live:
                candidate = candidate.where(RunState.kernel_id.not_in(live))
        elif live:
            candidate = candidate.where(or_(
                RunState.kernel_id.is_(None), RunState.kernel_id.not_in(live)))
        # Filter before FOR UPDATE: a periodic pass must never block progress writes for a run whose
        # kernel is observably live.  Empty ``live`` intentionally means every kernel-owned row is a
        # candidate (and, on boot, every kernel-less row from the previous hub is too).
        rows = s.scalars(candidate.order_by(
            RunState.run_id).with_for_update()).all()
        for r in rows:
            try:
                d = json.loads(r.doc)
            except Exception:  # noqa: BLE001
                d = {"run_id": r.run_id}
            # A child may have reported a provisional binding before its durable parent reaped and
            # committed it. Interrupted runs never publish that binding as a failed RunState owner.
            for key in ("uri", "outputUri", "output_uri", "outputTable", "output_table"):
                d.pop(key, None)
            d["status"] = "failed"
            d["error"] = "interrupted — the run's kernel is gone (hub restarted with no live kernel)"
            r.status = "failed"
            r.doc = json.dumps(d, default=str)
            _replace_attempt_ref(s, "run_state", r.run_id, None)
            _record_terminal_fence(s, r.run_id, "failed")
            reaped_run_ids.append(r.run_id)
            n += 1
        if reaped_run_ids:
            # Global order: every object-attempt ref above, then the local registry exactly once.
            _lock_local_result_registry(s)
            for run_id in reaped_run_ids:
                _drop_local_result_owner_locked(s, "run_state", run_id)
    return n


def _snapshot_canvas_in_session(
        s, canvas: Canvas, doc_json: str, version: int,
        author_id: str | None = None, label: str | None = None,
        throttle_seconds: int = 90, keep: int = 30) -> bool:
    """Snapshot in the transaction that already holds the canvas row lock."""
    canvas_id = canvas.id
    if label is None:
        last = s.scalars(select(CanvasVersion).where(
            CanvasVersion.canvas_id == canvas_id, CanvasVersion.label.is_(None)
        ).order_by(CanvasVersion.created_at.desc()).limit(1)).first()
        if last:
            if last.doc == doc_json:
                return False
            lc = last.created_at
            if lc is not None and lc.tzinfo is None:
                lc = lc.replace(tzinfo=datetime.timezone.utc)
            if lc is not None and (_now() - lc).total_seconds() < throttle_seconds:
                return False
    snapshot_id = _uid()
    s.add(CanvasVersion(
        id=snapshot_id, canvas_id=canvas_id, version=version, doc=doc_json,
        label=label, author_id=author_id))
    s.flush()  # owner row/FK first; local registry remains the final lifecycle lock
    autos = list(s.scalars(select(CanvasVersion).where(
        CanvasVersion.canvas_id == canvas_id, CanvasVersion.label.is_(None)
    ).order_by(CanvasVersion.created_at.desc(), CanvasVersion.id).with_for_update()))
    try:
        snapshot_doc = json.loads(doc_json)
    except (TypeError, ValueError):
        snapshot_doc = {}
    old_autos = autos[keep:]
    if old_autos:
        _lock_local_result_registry(s)
    sync_local_result_owner(s, "canvas_version", snapshot_id, snapshot_doc)
    for old in old_autos:
        _drop_local_result_owner_locked(s, "canvas_version", old.id)
        s.delete(old)
    return True


def snapshot_canvas(canvas_id: str, doc_json: str, version: int, author_id: str | None = None,
                    label: str | None = None, throttle_seconds: int = 90, keep: int = 30) -> bool:
    """Save a bounded snapshot while serializing with canvas update/restore/delete."""
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            return False
        return _snapshot_canvas_in_session(
            s, canvas, doc_json, version, author_id=author_id, label=label,
            throttle_seconds=throttle_seconds, keep=keep)


def list_versions(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(CanvasVersion).where(CanvasVersion.canvas_id == canvas_id)
                         .order_by(CanvasVersion.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "version": r.version, "label": r.label, "authorId": r.author_id,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


def get_version_doc(canvas_id: str, version_id: str) -> str | None:
    with session() as s:
        v = s.get(CanvasVersion, version_id)
        return v.doc if v and v.canvas_id == canvas_id else None


def set_setting(key: str, value, scope: str = "global", scope_id: str = "") -> None:
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        if row:
            row.value = json.dumps(value)
        else:
            s.add(Setting(scope=scope, scope_id=scope_id, key=key, value=json.dumps(value)))
