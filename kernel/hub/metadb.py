"""Metadata store — users, canvases (per-user files), and settings.

A small SQLAlchemy layer, separate from `db.py` (which is the DuckDB data engine). Dev uses a
bundled SQLite file; deployment points DP_DATABASE_URL at Postgres. Only the connection string is
config; all metadata lives in this instance's DB. Per-user authentication is implemented in
`hub.auth` + `current_user` (signed session cookies gated by DP_AUTH_SECRET, verifying each
user's own scrypt password hash); with no secret set, an open X-DP-User dev mode defaults to a
seeded local user.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index,
    Integer, LargeBinary, String, Text, UniqueConstraint, and_, cast, create_engine, delete, exists,
    func, literal, or_, select, text, tuple_, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from hub.models import ColumnSchema, SchemaCompatibility, SchemaFieldCompatibility
from hub.settings import settings

DEFAULT_USER_ID = "local"
LOCAL_WORKSPACE_ROOT_ID = "workspace-local-root"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def transform_library_text(value: object) -> str:
    """Canonical Unicode text used by every Transform library search boundary."""
    return unicodedata.normalize("NFKC", str(value)).casefold()


def transform_library_sort_key(title: object) -> str:
    """Return an ASCII key whose byte ordering is identical on SQLite and PostgreSQL."""
    return transform_library_text(title).encode("utf-8").hex()


def transform_library_search_text(*values: object) -> str:
    return "\n".join(transform_library_text(value) for value in values)


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
    # Ownership is by Canvas.owner_id alone; a share can never grant the owner role.
    __table_args__ = (
        UniqueConstraint("canvas_id", "user_id", name="uq_share"),
        CheckConstraint("role IN ('editor', 'viewer')", name="ck_share_role"),
    )


class WorkspaceContainer(Base):
    """A local overlay node.  Its ID, rather than its display path, is its identity."""
    __tablename__ = "workspace_containers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    parent_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("workspace_containers.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    is_root: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # A non-null catalog folder binding makes this a projection, not a mutable Workspace hierarchy
    # node.  The binding is deliberately not a foreign key: deleting a Catalog folder must leave a
    # truthful local overlay tombstone rather than cascade or silently retarget a Canvas.
    catalog_folder_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    catalog_folder_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    catalog_folder_path: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        Index("uq_workspace_local_container_parent_name", "parent_id", "name", unique=True,
              sqlite_where=text("catalog_folder_id IS NULL"),
              postgresql_where=text("catalog_folder_id IS NULL")),
        Index("ix_workspace_containers_catalog_folder_id", "catalog_folder_id", unique=True),
        CheckConstraint("ordinal >= 0", name="ck_workspace_container_ordinal"),
        CheckConstraint("version >= 1", name="ck_workspace_container_version"),
        CheckConstraint("is_root = false OR parent_id IS NULL", name="ck_workspace_container_root"),
    )


class WorkspacePlacement(Base):
    """Canonical local placement of one canvas, dataset, or immutable DatasetView."""
    __tablename__ = "workspace_placements"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    container_id: Mapped[str] = mapped_column(
        String, ForeignKey("workspace_containers.id"), nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    __table_args__ = (
        UniqueConstraint("target_kind", "target_id", name="uq_workspace_placement_target"),
        CheckConstraint(
            "target_kind IN ('canvas', 'dataset', 'dataset_view')",
            name="ck_workspace_placement_kind"),
        CheckConstraint("ordinal >= 0", name="ck_workspace_placement_ordinal"),
        CheckConstraint("version >= 1", name="ck_workspace_placement_version"),
    )


class DatasetView(Base):
    """One owner-scoped immutable DatasetView plus a retained submission tombstone."""

    __tablename__ = "dataset_views"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    submission_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_doc: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "submission_id", name="uq_dataset_view_owner_submission"),
        Index("ix_dataset_views_owner_deleted", "owner_id", "deleted_at", "created_at"),
        CheckConstraint("length(request_sha256) = 64", name="ck_dataset_view_request_sha256"),
        CheckConstraint(
            "length(definition_sha256) = 64", name="ck_dataset_view_definition_sha256"),
    )


class WorkspaceProviderBinding(Base):
    """Durable, non-sensitive display snapshot for one explicit external resource binding.

    The binding ID is part of the Workspace reference.  A provider resource that was observed as
    deleted therefore stays detached even if the provider later reuses the same resource ID; only an
    explicit relink can mint a new binding.  We deliberately retain no URI, columns, provider config,
    credentials, or other provider-owned metadata here.
    """
    __tablename__ = "workspace_provider_bindings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mount_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(256), nullable=False)
    container_id: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    parent_binding_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workspace_provider_bindings.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    relinked_from_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workspace_provider_bindings.id"), nullable=True)
    last_resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, index=True)
    __table_args__ = (
        CheckConstraint("kind IN ('container', 'dataset')", name="ck_workspace_provider_binding_kind"),
        CheckConstraint(
            "state IN ('current', 'offline', 'permission_lost', 'detached', 'provider_error')",
            name="ck_workspace_provider_binding_state",
        ),
        Index(
            "ix_workspace_provider_binding_resource",
            "mount_id", "provider", "resource_id", "active",
        ),
    )


class RunRecord(Base):
    """A finished run, kept with its canvas (run history survives restarts). One row per run."""
    __tablename__ = "run_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    # The runner's real id links durable history back to the logical run. Nullable because callers may
    # retain diagnostic history without a live logical run; `id` is the history row's own primary key.
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # HTTP/WebSocket request id that started the run (OPS-01). Nullable for non-HTTP starts and
    # backends that do not propagate a request id.
    request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_port_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_type: Mapped[str] = mapped_column(String, nullable=False, default="run", server_default="run")
    status: Mapped[str] = mapped_column(String)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ordered, secret-free source evidence captured before local dispatch.  It is intentionally
    # separate from status so bounded live-state eviction cannot erase reproducibility evidence.
    input_manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Declaration-ordered RunOutput snapshots.  The pre-1.0 baseline stores the collection directly;
    # there are no singular compatibility columns or migration shims.
    outputs: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    # Full-profile jobs have no outputs; retain their bounded ProfileResult separately for history.
    profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    per_node: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: durable per-node breakdown
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("canvas_id", "run_id", name="uq_run_record_canvas_run"),
        CheckConstraint("job_type IN ('run', 'profile')", name="ck_run_record_job_type"),
    )


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
    # HTTP/WebSocket request id that started the run (OPS-01). Mirrored from RunStatus.request_id.
    request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Fixed-size profile identity fields. Reopen reads the independent ProfileJobLatest projection below;
    # RunState remains globally bounded detail and must never be used to reconstruct latest-wins state.
    job_type: Mapped[str] = mapped_column(String, default="run", server_default="run")
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_port_id: Mapped[str | None] = mapped_column(String, nullable=True)
    plan_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_attempt_order: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=True)
    preallocation_token: Mapped[str | None] = mapped_column(String, nullable=True)
    preallocation_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (
        UniqueConstraint(
            "canvas_id", "profile_attempt_order", name="uq_run_state_canvas_profile_attempt"),
        CheckConstraint(
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1",
            name="ck_run_state_profile_attempt_positive"),
    )


class RunInputAdmission(Base):
    """One idempotent local full-run admission and its immutable source manifest."""
    __tablename__ = "run_input_admissions"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    creator_id: Mapped[str] = mapped_column(String, nullable=False)
    canvas_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    submission_id: Mapped[str] = mapped_column(String, nullable=False)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[str] = mapped_column(Text, nullable=False)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # This is a write-ahead claim, committed with the initial queued RunState before calling the
    # in-process runner. It intentionally means "may have been dispatched", not "the call returned".
    dispatched_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("creator_id", "canvas_id", "submission_id", name="uq_run_input_admission_submission"),
    )


class ExecutionManifest(Base):
    """One immutable content-addressed definition for graph-backed execution."""
    __tablename__ = "execution_manifests"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_doc: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)


class PromotedTransform(Base):
    """Owner-scoped logical identity for one promoted Transform.

    ``key`` is the bounded client identity used to make a retried promotion converge. ``id`` is the
    opaque stable reference stored by Canvases; neither identity nor version numbers are reusable.
    """
    __tablename__ = "promoted_transforms"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    library_sort_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        UniqueConstraint("owner_id", "key", name="uq_promoted_transform_owner_key"),
    )


class PromotedTransformVersion(Base):
    """One immutable, server-digested promoted Transform definition."""
    __tablename__ = "promoted_transform_versions"
    transform_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("promoted_transforms.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    semantic_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    library_search_text: Mapped[str] = mapped_column(Text, nullable=False)
    library_category_key: Mapped[str] = mapped_column(Text, nullable=False)
    library_mode_key: Mapped[str] = mapped_column(Text, nullable=False)
    blurb: Mapped[str] = mapped_column(String(2000), nullable=False, default="", server_default="")
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[str] = mapped_column(Text, nullable=False)
    creator_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("transform_id", "semantic_digest", name="uq_promoted_transform_digest"),
        CheckConstraint("version >= 1", name="ck_promoted_transform_version_positive"),
    )


class PromotedTransformVersionRef(Base):
    """Exact retention hold owned only by a Canvas, Canvas snapshot, or execution manifest."""
    __tablename__ = "promoted_transform_version_refs"
    owner_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    transform_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["transform_id", "version"],
            ["promoted_transform_versions.transform_id", "promoted_transform_versions.version"],
            name="fk_promoted_transform_version_ref",
        ),
        CheckConstraint(
            "owner_kind IN ('canvas', 'canvas_version', 'execution_manifest')",
            name="ck_promoted_transform_ref_owner_kind",
        ),
        Index("ix_promoted_transform_version_refs_version", "transform_id", "version"),
    )


class LocalFileInputRevision(Base):
    """Content-identified immutable Parquet binding for one ordinary local-file input."""
    __tablename__ = "local_file_input_revisions"
    dataset_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_uri: Mapped[str] = mapped_column(
        Text, ForeignKey("local_result_artifacts.uri"), nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)


class DurableTask(Base):
    """One bounded, restart-safe background operation."""
    __tablename__ = "durable_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    canvas_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("canvases.id"), nullable=True, index=True)
    dataset_view_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("dataset_views.id"), nullable=True, index=True)
    submission_id: Mapped[str] = mapped_column(String, nullable=False)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="managed_local_write", server_default="managed_local_write")
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    backend_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="local", server_default="local")
    # Pre-0022 rows recover from this frozen triple. New rows leave it null and reopen the one
    # canonical ExecutionManifest instead; the forward migration never rewrites historical meaning.
    graph_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    write_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default="queued", index=True)
    status_doc: Mapped[str] = mapped_column(Text, nullable=False)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    output_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("owner_id", "canvas_id", "submission_id", name="uq_durable_task_submission"),
        CheckConstraint(
            "task_kind IN ('managed_local_write','external_wait',"
            "'linear_checkpoint_write','bounded_fanout_write','distribution_report')",
            name="ck_durable_task_kind"),
        CheckConstraint(
            "(task_kind = 'distribution_report' AND canvas_id IS NULL "
            "AND target_node_id IS NULL AND dataset_view_id IS NOT NULL "
            "AND execution_manifest_sha256 IS NULL AND graph_doc IS NULL "
            "AND input_manifest IS NULL AND write_intent IS NULL) OR "
            "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
            "AND target_node_id IS NOT NULL AND dataset_view_id IS NULL)",
            name="ck_durable_task_subject"),
        UniqueConstraint(
            "owner_id", "dataset_view_id", "submission_id",
            name="uq_distribution_report_submission"),
        CheckConstraint("backend_kind = 'local'", name="ck_durable_task_backend"),
        CheckConstraint("status IN ('queued','running','done','failed','cancelled')", name="ck_durable_task_status"),
        CheckConstraint("retry_count >= 0 AND max_attempts >= 1 AND retry_count < max_attempts", name="ck_durable_task_retry_bounds"),
    )


class DurableTaskAttempt(Base):
    """One leased execution owner for a durable task."""
    __tablename__ = "durable_task_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("durable_tasks.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued", server_default="queued")
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    output_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_durable_task_attempt_number"),
        UniqueConstraint("task_id", "retry_request_id", name="uq_durable_task_retry_request"),
        CheckConstraint("attempt_number >= 1", name="ck_durable_task_attempt_number"),
        CheckConstraint("status IN ('queued','running','done','failed','cancelled','fenced')", name="ck_durable_task_attempt_status"),
    )


class DurableExternalWait(Base):
    __tablename__ = "durable_external_waits"
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("durable_tasks.id"), primary_key=True)
    provider_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    submit_request: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    handle_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_dev: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage_ino: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unsubmitted")
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_poll_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    diagnostic_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        CheckConstraint("phase IN ('unsubmitted','submitting','accepted','running','provider_succeeded',"
                        "'downloading','downloaded','publishing','published','provider_failed',"
                        "'provider_cancelled','finalization_failed','cancelled_before_submit',"
                        "'cancelled_after_success')",
                        name="ck_external_wait_phase"),
        CheckConstraint("poll_count >= 0", name="ck_external_wait_poll_count"),
        CheckConstraint("(stage_dev IS NULL AND stage_ino IS NULL) OR "
                        "(stage_dev IS NOT NULL AND stage_ino IS NOT NULL "
                        "AND stage_dev >= 0 AND stage_ino >= 0)",
                        name="ck_external_wait_stage_identity"),
    )


class DurableTaskInboxItem(Base):
    """One owner-scoped terminal outcome for a certified durable TaskAttempt."""
    __tablename__ = "durable_task_inbox_items"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("durable_tasks.id"), nullable=False)
    task_attempt_id: Mapped[str] = mapped_column(
        String, ForeignKey("durable_task_attempts.id"), nullable=False)
    canvas_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("canvases.id"), nullable=True)
    dataset_view_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("dataset_views.id"), nullable=True, index=True)
    task_kind: Mapped[str] = mapped_column(String, nullable=False)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    diagnostic_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("task_id", "task_attempt_id", name="uq_durable_task_inbox_attempt"),
        CheckConstraint(
            "task_kind IN ('managed_local_write','external_wait',"
            "'linear_checkpoint_write','bounded_fanout_write','distribution_report')",
            name="ck_durable_task_inbox_kind"),
        CheckConstraint(
            "(task_kind = 'distribution_report' AND canvas_id IS NULL "
            "AND dataset_view_id IS NOT NULL) OR "
            "(task_kind <> 'distribution_report' AND canvas_id IS NOT NULL "
            "AND dataset_view_id IS NULL)",
            name="ck_durable_task_inbox_subject"),
        CheckConstraint(
            "outcome IN ('completed','failed','cancelled')",
            name="ck_durable_task_inbox_outcome"),
        Index("ix_durable_task_inbox_owner_created", "owner_id", "created_at", "id"),
        Index("ix_durable_task_inbox_owner_unread", "owner_id", "read_at"),
        Index("ix_durable_task_inbox_task_id", "task_id"),
    )


class DistributionReportEnvelope(Base):
    """Immutable DatasetView report admission plus its nullable terminal document."""

    __tablename__ = "distribution_report_envelopes"
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("durable_tasks.id"), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    dataset_view_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("dataset_views.id"), nullable=False)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_doc: Mapped[str] = mapped_column(Text, nullable=False)
    view_definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    view_snapshot_doc: Mapped[str] = mapped_column(Text, nullable=False)
    computation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_retention_owner: Mapped[str] = mapped_column(String(16), nullable=False)
    report_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "length(intent_sha256) = 64", name="ck_distribution_report_intent_sha256"),
        CheckConstraint(
            "length(view_definition_sha256) = 64",
            name="ck_distribution_report_view_sha256"),
        CheckConstraint(
            "revision_retention_owner = 'core'",
            name="ck_distribution_report_retention_owner"),
        Index("ix_distribution_reports_dataset_view", "dataset_view_id", "created_at"),
    )


class DurableCheckpoint(Base):
    """Minimal DB-only admission and candidate binding for one hidden checkpoint."""
    __tablename__ = "durable_checkpoints"
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("durable_tasks.id"), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_node_id: Mapped[str] = mapped_column(String(256), nullable=False)
    output_port_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_prefix_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending")
    candidate_uri: Mapped[str | None] = mapped_column(
        Text, ForeignKey("local_result_artifacts.uri"), nullable=True)
    candidate_generation: Mapped[str | None] = mapped_column(
        String(64), nullable=True)
    candidate_attempt_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("durable_task_attempts.id"), nullable=True)
    candidate_dev: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    candidate_ino: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    committed_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    committed_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    committed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("checkpoint_id", name="uq_durable_checkpoint_id"),
        UniqueConstraint("candidate_uri", name="uq_durable_checkpoint_candidate_uri"),
        UniqueConstraint(
            "candidate_generation", name="uq_durable_checkpoint_candidate_generation"),
        CheckConstraint(
            "phase IN ('pending','reserved','committed')", name="ck_durable_checkpoint_phase"),
        CheckConstraint(
            "(candidate_uri IS NULL AND candidate_generation IS NULL "
            "AND candidate_attempt_id IS NULL) OR (candidate_uri IS NOT NULL "
            "AND candidate_generation IS NOT NULL AND candidate_attempt_id IS NOT NULL)",
            name="ck_durable_checkpoint_candidate_binding"),
        CheckConstraint(
            "(candidate_dev IS NULL AND candidate_ino IS NULL) OR "
            "(candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL "
            "AND candidate_dev >= 0 AND candidate_ino >= 0)",
            name="ck_durable_checkpoint_candidate_inode"),
        CheckConstraint(
            "(phase = 'pending' AND candidate_uri IS NULL) OR "
            "(phase IN ('reserved','committed') AND candidate_uri IS NOT NULL)",
            name="ck_durable_checkpoint_phase_binding"),
        CheckConstraint(
            # pending: no evidence and no inode. reserved: no committed evidence, but materialized
            # candidate_dev/ino may already be bound before the fenced commit (#450).
            "(phase = 'pending' AND committed_rows IS NULL AND committed_bytes IS NULL "
            "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND committed_at IS NULL "
            "AND candidate_dev IS NULL AND candidate_ino IS NULL) OR "
            "(phase = 'reserved' AND committed_rows IS NULL AND committed_bytes IS NULL "
            "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND committed_at IS NULL) OR "
            "(phase = 'committed' AND committed_rows IS NOT NULL AND committed_bytes IS NOT NULL "
            "AND content_sha256 IS NOT NULL AND schema_sha256 IS NOT NULL "
            "AND committed_at IS NOT NULL "
            "AND candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL "
            "AND committed_rows >= 0 AND committed_bytes >= 0)",
            name="ck_durable_checkpoint_committed_evidence"),
    )


class ProfileJobLatest(Base):
    """Canvas-scoped latest retry for one node/plan, independent of global RunState retention.

    The projection owns a copy of the latest status document so pruning detailed RunState rows can never
    resurrect an older retry or erase reopen recovery. One bounded row exists per retained plan identity.
    """
    __tablename__ = "profile_job_latest"
    canvas_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_node_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_port_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String)
    doc: Mapped[str] = mapped_column(Text)
    attempt_order: Mapped[int] = mapped_column(BigInteger)
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (
        Index("ix_profile_job_latest_canvas_attempt", "canvas_id", "attempt_order"),
        UniqueConstraint(
            "canvas_id", "attempt_order", name="uq_profile_latest_canvas_attempt"),
        CheckConstraint("attempt_order >= 1", name="ck_profile_latest_attempt_positive"),
    )


class ProfileJobRetention(Base):
    """Per-canvas cutoff for profile identities evicted from the bounded latest projection."""
    __tablename__ = "profile_job_retention"
    canvas_id: Mapped[str] = mapped_column(String, primary_key=True)
    next_attempt_order: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    cutoff_attempt_order: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (
        CheckConstraint("next_attempt_order >= 1", name="ck_profile_next_attempt_positive"),
        CheckConstraint(
            "cutoff_attempt_order IS NULL OR cutoff_attempt_order >= 1",
            name="ck_profile_cutoff_attempt_positive"),
    )


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
    # Private control-plane copy of the immutable workload envelope. It lets recovery materialize the
    # object artifact after a crash between the SQL binding commit and object-store creation.
    job_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Immutable write-ahead terminal/effect plan while publication_state == effects_started.
    publication_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("backend", "submission_id", name="uq_run_backend_submission"),)


class RunTerminalFence(Base):
    """Compact permanent run fence plus the minimum identity needed to authorize retained status."""
    __tablename__ = "run_terminal_fences"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)
    canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_type: Mapped[str] = mapped_column(String, default="run", server_default="run")
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_port_id: Mapped[str | None] = mapped_column(String, nullable=True)
    plan_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_attempt_order: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        CheckConstraint(
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1",
            name="ck_terminal_fence_profile_attempt_positive"),
    )


class ActiveBackendJobsError(RuntimeError):
    """A canvas cannot be deleted while external work can still produce side effects."""


class TerminalRunIdError(RuntimeError):
    """A completed logical run id cannot be rebound after its retained detail is pruned."""


class ProfileSubmissionConflict(RuntimeError):
    """A submission id is already permanently bound to a different profile identity."""


class DurableTaskSubmissionConflict(RuntimeError):
    """A durable task submission id is bound to a different semantic admission."""


@dataclass(frozen=True)
class ProfileRunReservation:
    run_id: str
    admission_token: str | None
    attempt_order: int
    status: dict
    should_dispatch: bool


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
    registration_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, default=lambda: uuid.uuid4().hex)
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


class CatalogFolder(Base):
    """A first-class browse folder. Additive to the `folder` path string on CatalogEntry (which the
    external CatalogProvider namespace mapping still owns): a folder entity lets an EMPTY folder exist
    and be renamed/deleted, while rename/delete operate over the UNION of these paths and the distinct
    entry `folder` strings — so a folder created by simply registering a dataset is editable too."""
    __tablename__ = "catalog_folders"
    path: Mapped[str] = mapped_column(String, primary_key=True)
    # Folder paths are editable display hierarchy.  This opaque ID is used by the Workspace
    # projection so rename/move never changes the deep-link identity and delete/recreate cannot ABA.
    id: Mapped[str] = mapped_column(String(32), nullable=False, default=_uid, unique=True, index=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), default=_now)


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


class CatalogLineageFact(Base):
    """One immutable publication fact; repeated dataset pairs remain distinct observations."""
    __tablename__ = "catalog_lineage_facts"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    fact_key: Mapped[str] = mapped_column(String(512), nullable=False)
    publication_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    destination_key: Mapped[str] = mapped_column(String, nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    destination_uri: Mapped[str] = mapped_column(Text, nullable=False)
    # PostgreSQL B-tree entries cannot hold the public 8192-character identity ceiling. Keep the
    # original values authoritative/exportable and index fixed-width hashes; every lookup also checks
    # the original value, so a theoretical digest collision cannot join or delete unrelated facts.
    source_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_uri_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    destination_uri_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_version: Mapped[str | None] = mapped_column(String, nullable=True)
    destination_version: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    producer: Mapped[str | None] = mapped_column(String, nullable=True)
    producer_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, nullable=False)
    field_mappings_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        UniqueConstraint("fact_key", name="uq_catalog_lineage_fact_key"),
        CheckConstraint(
            "provenance IN ('run', 'manual', 'imported')",
            name="ck_catalog_lineage_fact_provenance",
        ),
        Index(
            "ix_catalog_lineage_facts_pair_hash",
            "source_key_hash", "destination_key_hash"),
        {"sqlite_autoincrement": True},
    )


class CatalogPublicationEvent(Base):
    """One durable catalog effect from an idempotent external-run publication."""
    __tablename__ = "catalog_publication_events"
    event_key: Mapped[str] = mapped_column(String, primary_key=True)
    effect_type: Mapped[str] = mapped_column(String, default="usage")
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ResultCache(Base):
    """Content-addressed result index: a run plan's content hash → its canonical sole committed
    ``RunOutput`` collection. Persisted + shared so a completed run's output is REUSED across
    kernel restarts AND across stateless web instances — the old in-process dict was per-process and
    lost on restart. Not authoritative data: a miss just recomputes, so it's safe to prune (newest N)."""
    __tablename__ = "result_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)  # {"outputs": [one committed RunOutput]}
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
    # Collection owners (RunState, history, result cache) keep one stable semantic slot per named
    # output. Singleton owners use the empty slot. The slot belongs to the owner identity rather than
    # the physical URI, so replacing one complete output set never aliases two ports through a path.
    ref_slot: Mapped[str] = mapped_column(String, primary_key=True, default="")
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


class ManagedLocalFileRevision(Base):
    """One retained, core-owned local-file revision behind a logical catalog target."""
    __tablename__ = "managed_local_file_revisions"
    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    logical_id: Mapped[str] = mapped_column(
        String, ForeignKey("catalog_logical_datasets.logical_id"), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(
        Text, ForeignKey("local_result_artifacts.uri"), nullable=False, unique=True)
    publish_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    table_doc: Mapped[str] = mapped_column(Text, nullable=False)
    write_idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    write_intent_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    write_receipt_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    committed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        UniqueConstraint(
            "write_idempotency_key",
            name="uq_managed_local_file_revision_write_key"),
        UniqueConstraint("logical_id", "publish_seq", name="uq_managed_local_file_revision_sequence"),
        Index("ix_managed_local_file_revisions_history", "logical_id", "publish_seq"),
    )


class ManagedLocalLanceWriteReceipt(Base):
    """One durable typed receipt for an exact provider-owned local Lance version."""
    __tablename__ = "managed_local_lance_write_receipts"
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String, nullable=False)
    logical_uri: Mapped[str] = mapped_column(Text, nullable=False)
    revision_id: Mapped[str] = mapped_column(String(256), nullable=False)
    write_intent_doc: Mapped[str] = mapped_column(Text, nullable=False)
    write_receipt_doc: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    execution_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    committed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "revision_id", name="uq_managed_local_lance_write_revision"),
    )


class SettingRevision(Base):
    """One optimistic-concurrency counter per independently editable settings scope."""
    __tablename__ = "setting_revisions"
    scope: Mapped[str] = mapped_column(String, primary_key=True)
    scope_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0")
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'user')", name="ck_setting_revision_scope"),
        CheckConstraint(
            "(scope = 'global' AND scope_id = '') OR "
            "(scope = 'user' AND scope_id <> '')",
            name="ck_setting_revision_identity",
        ),
    )


class Setting(Base):
    __tablename__ = "settings"
    # scope 'global' (scope_id='') for system settings; scope 'user' (scope_id=user id) for prefs
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String, default="")
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", name="uq_setting"),
        CheckConstraint("scope IN ('global', 'user')", name="ck_setting_scope"),
        CheckConstraint(
            "(scope = 'global' AND scope_id = '') OR "
            "(scope = 'user' AND scope_id <> '')",
            name="ck_setting_identity",
        ),
    )


class CredEntity(Base):
    """A named credential — a first-class entity a destination or the agent references by id.

    ``fields`` stores only secret REFERENCES (``env:VAR`` / ``file:/path``), never raw secret bytes.
    ``kind`` is 'object_store' (connection fields plus SecretRefs) or 'agent'
    (fields = {apiKey: ref}).
    """
    __tablename__ = "creds"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # 'object_store' | 'agent'
    fields_json: Mapped[str] = mapped_column(Text, default="{}")  # JSON dict of secret references
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentEgressEvent(Base):
    """Value-free audit of a catalog-reading agent tool call under a hosted model (SEC-01).

    ``event_json`` never carries raw sample values. Catalog enumeration stores a bounded summary;
    typed columns retain the small dataset/column evidence used by single-dataset tools such as preview.
    """
    __tablename__ = "agent_egress_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    tool: Mapped[str] = mapped_column(String, default="", index=True)
    dataset: Mapped[str | None] = mapped_column(Text, nullable=True)
    columns_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of column names
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_json: Mapped[str] = mapped_column(Text, default="{}")  # full value-free event payload


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
        for scope, scope_id in (("global", ""), ("user", DEFAULT_USER_ID)):
            if s.get(SettingRevision, (scope, scope_id)) is None:
                s.add(SettingRevision(scope=scope, scope_id=scope_id, revision=0))
        _workspace_backfill_root_placements_in_session(s)
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


RUN_PREALLOCATION_TTL_SECONDS = 30.0
_PREALLOCATION_STATES = ("allocating", "queued")
_RESULT_RECONCILIATION_STATES = (
    "result_fencing", "result_submitted", "result_stop_fenced",
)
_UNSETTLED_BACKEND_SUBMISSION_STATES = (
    "submitting", "fencing", "stopping", "fence_stopping",
    *_RESULT_RECONCILIATION_STATES,
)


def _lock_authorized_run_canvas(s, canvas_id: str | None) -> Canvas | None:
    """Lock a real authorization canvas before its run identity row."""
    if canvas_id is None:
        return None
    canvas = s.get(Canvas, str(canvas_id), with_for_update=True)
    if canvas is None:
        raise RuntimeError("authorized run canvas does not exist")
    return canvas


def _lock_existing_run_identity(
        s, run_id: str, requested_canvas_id: str | None = None) -> RunState | None:
    """Lock an existing run in the global Canvas -> RunState order.

    The first read is an immutable identity hint, not an ownership decision. Re-read the RunState under
    lock after taking the real canvas lock and reject a concurrent identity replacement.
    """
    identity = s.execute(select(
        RunState.auth_canvas_id, RunState.canvas_id, RunState.preallocation_token,
    ).where(RunState.run_id == str(run_id))).one_or_none()
    auth_canvas_id, preallocated_canvas_id, preallocation_token = (
        identity if identity is not None else (None, None, None)
    )
    canvas_ids = {str(auth_canvas_id)} if auth_canvas_id is not None else set()
    if preallocation_token is not None and preallocated_canvas_id is not None:
        canvas_ids.add(str(preallocated_canvas_id))
    requested_canvas_id = str(requested_canvas_id) if requested_canvas_id is not None else None
    requested_exists = False
    if requested_canvas_id is not None:
        requested_exists = s.scalar(select(Canvas.id).where(
            Canvas.id == requested_canvas_id)) is not None
        if requested_exists:
            canvas_ids.add(requested_canvas_id)
    locked_canvases = {
        canvas_id: s.get(
            Canvas, canvas_id, with_for_update=True, populate_existing=True)
        for canvas_id in sorted(canvas_ids)
    }
    if auth_canvas_id is not None and locked_canvases.get(str(auth_canvas_id)) is None:
        raise RuntimeError("authorized run canvas does not exist")
    if (preallocation_token is not None and preallocated_canvas_id is not None
            and locked_canvases.get(str(preallocated_canvas_id)) is None):
        raise RuntimeError("preallocated run canvas no longer exists")
    if requested_exists and locked_canvases.get(str(requested_canvas_id)) is None:
        raise RuntimeError("requested run canvas was deleted during backend binding")
    state = s.get(RunState, str(run_id), with_for_update=True, populate_existing=True)
    consumed_preallocation = bool(
        state is not None
        and preallocation_token is not None
        and preallocated_canvas_id is None
        and requested_canvas_id is not None
        and state.auth_canvas_id == auth_canvas_id
        and state.canvas_id == requested_canvas_id
        and state.preallocation_token is None
        and state.preallocation_expires_at is None
    )
    if state is not None and (
            state.auth_canvas_id != auth_canvas_id
            or (state.canvas_id != preallocated_canvas_id
                and not consumed_preallocation)):
        raise RuntimeError("run authorization identity changed concurrently")
    return state


def _run_preallocation_deadline(s, ttl_seconds: float) -> datetime.datetime:
    ttl = max(1.0, _gc_seconds(ttl_seconds, "run preallocation ttl"))
    margin = 1.0 if s.get_bind().dialect.name == "sqlite" else 0.0
    return _db_now(s) + datetime.timedelta(seconds=ttl + margin)


def _run_preallocation_active(
        expires_at: datetime.datetime | None, now: datetime.datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None and now.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    elif expires_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=expires_at.tzinfo)
    return expires_at > now


def _assert_bound_run_identity(
        state: RunState, uid: str, auth_canvas_id: str | None) -> None:
    """Validate a previously bound principal without reclassifying its authorization scope."""
    if state.created_by != str(uid) or state.auth_canvas_id != auth_canvas_id:
        raise RuntimeError("run id is already bound to a different authorization identity")
    if auth_canvas_id is not None and state.canvas_id != auth_canvas_id:
        raise RuntimeError("run id is already bound to a different canvas")


def _backfill_terminal_fence_identity(s, state: RunState) -> None:
    """Fill a fast terminal fence only from this exact, authoritatively bound RunState.

    A local worker can finish between its initial status write and ``bind_run_owner``. In that narrow
    window the terminal fence already has the operational canvas but not the creator/auth canvas. Never
    overwrite an existing identity, and never refill a fence whose canvas handle was severed by canvas
    deletion.
    """
    # Flush the identity first. If terminal publication already won on SQLite (where FOR UPDATE is a
    # no-op), this updates only the identity columns after its commit; if binding won, the write lock
    # keeps publication behind this transaction. The ORM status may still be the pre-publication value,
    # so refresh it after observing a fence before validating the now-linearized pair.
    s.flush()
    fence = s.get(RunTerminalFence, state.run_id, with_for_update=True)
    if fence is None:
        return
    s.refresh(state)
    if state.status not in ("done", "failed", "cancelled") or fence.status != state.status:
        raise RuntimeError("terminal run fence does not match its bound run state")
    if fence.canvas_id != state.canvas_id:
        raise RuntimeError("terminal run fence canvas identity does not match its bound run state")
    for field in (
            "created_by", "auth_canvas_id", "job_type", "target_node_id",
            "target_port_id", "plan_digest", "profile_attempt_order"):
        expected = getattr(state, field)
        current = getattr(fence, field)
        if current is not None and current != expected:
            raise RuntimeError("terminal run fence has a different authorization identity")
        if current is None:
            setattr(fence, field, expected)


def bind_run_owner(run_id: str, uid: str, auth_canvas_id: str | None,
                   request_id: str | None = None) -> None:
    """Persist a run's creator (authoritative, unspoofable owner) and the real canvas it was authorized
    against (None for an ad-hoc graph). Missing identity fields may be filled once; an established
    principal, authorization canvas, or operational canvas is never overwritten.
    `request_id` (OPS-01) correlates the durable run with the HTTP/WebSocket entry that started it."""
    run_id, uid = str(run_id), str(uid)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    with session() as s:
        _lock_authorized_run_canvas(s, auth_canvas_id)
        r = s.get(RunState, run_id, with_for_update=True)
        if r is None:
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            s.add(RunState(run_id=run_id, canvas_id=auth_canvas_id, status="queued", doc="{}",
                           created_by=uid, auth_canvas_id=auth_canvas_id, request_id=request_id))
            return
        if r.created_by is not None:
            _assert_bound_run_identity(r, uid, auth_canvas_id)
            _backfill_terminal_fence_identity(s, r)
            return
        if r.auth_canvas_id is not None:
            raise RuntimeError("unowned run has an existing authorization canvas")
        if auth_canvas_id is not None and r.canvas_id not in (None, auth_canvas_id):
            raise RuntimeError("run id is already bound to a different canvas")
        r.created_by = uid
        r.auth_canvas_id = auth_canvas_id
        if auth_canvas_id is not None and r.canvas_id is None:
            r.canvas_id = auth_canvas_id
        if request_id and not r.request_id:
            r.request_id = request_id
        _backfill_terminal_fence_identity(s, r)


def bind_run_request_id(run_id: str, request_id: str, canvas_id: str | None = None) -> None:
    """Stamp OPS-01 request_id on a run_state without altering ownership fields (open-mode starts)."""
    if not run_id or not request_id:
        return
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None:
            s.add(RunState(run_id=run_id, canvas_id=canvas_id, status="queued", doc="{}",
                           request_id=request_id))
        elif not r.request_id:
            r.request_id = request_id


def run_request_id(run_id: str) -> str | None:
    with session() as s:
        r = s.get(RunState, run_id)
        return r.request_id if r else None


def preallocate_run_owner(
        run_id: str, uid: str, auth_canvas_id: str | None,
        ttl_seconds: float = RUN_PREALLOCATION_TTL_SECONDS, *,
        operational_canvas_id: str | None = None,
        execution_manifest_sha256: str | None = None,
        execution_manifest_doc: str | None = None) -> str:
    """Create one leased run identity before an external backend may allocate durable effects."""
    run_id, uid = str(run_id), str(uid)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    operational_canvas_id = (
        str(operational_canvas_id) if operational_canvas_id is not None else auth_canvas_id
    )
    if auth_canvas_id is not None and operational_canvas_id != auth_canvas_id:
        raise RuntimeError("authorized run canvas and operational canvas must match")
    token = secrets.token_urlsafe(32)
    with session() as s:
        if (execution_manifest_sha256 is None) != (execution_manifest_doc is None):
            raise ValueError("execution manifest identity and document must be supplied together")
        for canvas_id in sorted({
                value for value in (auth_canvas_id, operational_canvas_id) if value is not None}):
            if s.get(Canvas, canvas_id, with_for_update=True) is None:
                raise RuntimeError("preallocated run canvas does not exist")
        state = s.get(RunState, run_id, with_for_update=True)
        if state is not None:
            raise RuntimeError(f"run '{run_id}' is already allocated")
        fenced = _terminal_fence_status(s, run_id)
        if fenced is not None:
            raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
        if execution_manifest_sha256 is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, str(execution_manifest_doc))
        s.add(RunState(
            run_id=run_id, canvas_id=operational_canvas_id, status="queued",
            doc=json.dumps({"run_id": run_id, "status": "queued"}),
            created_by=uid, auth_canvas_id=auth_canvas_id,
            execution_manifest_sha256=execution_manifest_sha256,
            preallocation_token=token,
            preallocation_expires_at=_run_preallocation_deadline(s, ttl_seconds),
        ))
    return token


def _lock_profile_retention(s, canvas_id: str) -> ProfileJobRetention:
    """Create then lock the per-canvas profile sequence/watermark row."""
    canvas_id = str(canvas_id)
    now = _now()
    values = {"canvas_id": canvas_id, "next_attempt_order": 1, "updated_at": now}
    dialect = s.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        s.execute(dialect_insert(ProfileJobRetention).values(
            **values,
        ).on_conflict_do_nothing(index_elements=[ProfileJobRetention.canvas_id]))
    elif s.get(ProfileJobRetention, canvas_id) is None:  # pragma: no cover - fallback dialect
        s.add(ProfileJobRetention(**values))
    s.flush()
    retention = s.get(
        ProfileJobRetention, canvas_id, with_for_update=True, populate_existing=True,
    )
    if retention is None:  # pragma: no cover - defensive database contract check
        raise RuntimeError("profile retention state reservation failed")
    return retention


def profile_submission_run_id(uid: str, canvas_id: str, submission_id: str) -> str:
    """Derive the permanent logical run id for one user submission intent."""
    canonical = "\x00".join(("profile-submission-v1", str(uid), str(canvas_id),
                              str(submission_id).lower()))
    return f"profile_{hashlib.sha256(canonical.encode()).hexdigest()[:48]}"


def _profile_binding_matches(
        identity, *, uid: str, auth_canvas_id: str | None, canvas_id: str,
        target_node_id: str, target_port_id: str, plan_digest: str) -> bool:
    return bool(
        identity.created_by == uid
        and identity.auth_canvas_id == auth_canvas_id
        and identity.canvas_id == canvas_id
        and identity.job_type == "profile"
        and identity.target_node_id == target_node_id
        and identity.target_port_id == target_port_id
        and identity.plan_digest == plan_digest
        and isinstance(identity.profile_attempt_order, int)
        and identity.profile_attempt_order >= 1
    )


def _profile_status_doc(state: RunState) -> dict:
    try:
        parsed = json.loads(state.doc)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("profile submission has an invalid durable status") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("profile submission has an invalid durable status")
    return parsed


def _terminalize_unadmitted_profile(
        s, state: RunState, *, reason: str) -> dict:
    """Permanently fail a reservation that never entered the latest-job projection."""
    if (state.job_type != "profile" or state.kernel_id is not None
            or state.preallocation_token is None
            or state.target_node_id is None or state.target_port_id is None
            or state.plan_digest is None
            or state.profile_attempt_order is None):
        raise RuntimeError("only an exact unadmitted profile reservation can be terminalized")
    failed = {
        "run_id": state.run_id,
        "status": "failed",
        "job_type": "profile",
        "target_node_id": state.target_node_id,
        "target_port_id": state.target_port_id,
        "plan_digest": state.plan_digest,
        "profile_attempt_order": int(state.profile_attempt_order),
        "request_id": state.request_id,
        "placement": "local",
        "per_node": [{
            "node_id": state.target_node_id,
            "status": "failed",
            "label": "Full profile",
            "error": reason,
        }],
        "error": reason,
    }
    _abandon_run_preallocation_attempts(s, state.run_id)
    _replace_attempt_ref(s, "run_state", state.run_id, None)
    _drop_local_result_owner(s, "profile_job", state.run_id)
    state.status = "failed"
    state.doc = json.dumps(failed, default=str)
    state.preallocation_token = None
    state.preallocation_expires_at = None
    _record_terminal_fence(s, state.run_id, "failed")
    return failed


def _profile_reservation_from_bound_identity(
        s, *, run_id: str, uid: str, auth_canvas_id: str | None,
        canvas_id: str, target_node_id: str, target_port_id: str, plan_digest: str,
        terminalize_expired: bool) -> ProfileRunReservation | None:
    state = s.get(RunState, run_id, with_for_update=True)
    if state is not None:
        if not _profile_binding_matches(
                state, uid=uid, auth_canvas_id=auth_canvas_id, canvas_id=canvas_id,
                target_node_id=target_node_id, target_port_id=target_port_id,
                plan_digest=plan_digest):
            raise ProfileSubmissionConflict(
                "profile submission id is already bound to a different identity")
        attempt_order = int(state.profile_attempt_order)
        if state.preallocation_token is not None:
            if (terminalize_expired and not _run_preallocation_active(
                    state.preallocation_expires_at, _db_now(s))):
                failed = _terminalize_unadmitted_profile(
                    s, state, reason="profile submission expired before kernel admission")
                return ProfileRunReservation(
                    run_id, None, attempt_order, failed, False)
            return ProfileRunReservation(
                run_id, state.preallocation_token, attempt_order,
                _profile_status_doc(state), True)
        if state.kernel_id is None and state.status not in _TERMINAL_RUN:
            raise RuntimeError("profile submission lost both admission and terminal ownership")
        return ProfileRunReservation(
            run_id, None, attempt_order, _profile_status_doc(state), False)

    fence = s.get(RunTerminalFence, run_id, with_for_update=True)
    if fence is None:
        return None
    if not _profile_binding_matches(
            fence, uid=uid, auth_canvas_id=auth_canvas_id, canvas_id=canvas_id,
            target_node_id=target_node_id, target_port_id=target_port_id,
            plan_digest=plan_digest):
        raise ProfileSubmissionConflict(
            "profile submission id is already bound to a different identity")
    attempt_order = int(fence.profile_attempt_order)
    latest = s.get(
        ProfileJobLatest, (canvas_id, target_node_id, plan_digest),
        with_for_update=True,
    )
    if latest is None or latest.run_id != run_id or latest.attempt_order != attempt_order:
        error = (
            "Full profile failed (terminal details were pruned)"
            if fence.status == "failed" else None
        )
        pruned = {
            "run_id": run_id,
            "status": fence.status,
            "job_type": "profile",
            "target_node_id": target_node_id,
            "target_port_id": target_port_id,
            "plan_digest": plan_digest,
            "profile_attempt_order": attempt_order,
            "placement": "local",
            "per_node": [{
                "node_id": target_node_id,
                "status": fence.status,
                "label": "Full profile",
                "error": error,
            }],
            "progress": 1.0 if fence.status == "done" else None,
            "error": error,
        }
        return ProfileRunReservation(run_id, None, attempt_order, pruned, False)
    try:
        parsed = json.loads(latest.doc)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("profile submission has an invalid retained status") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("profile submission has an invalid retained status")
    return ProfileRunReservation(run_id, None, attempt_order, parsed, False)


def lookup_profile_submission(
        submission_id: str, uid: str, auth_canvas_id: str | None,
        operational_canvas_id: str, target_node_id: str,
        target_port_id: str, plan_digest: str) -> ProfileRunReservation | None:
    """Adopt an existing submission before consulting mutable source state."""
    uid, operational_canvas_id = str(uid), str(operational_canvas_id)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    run_id = profile_submission_run_id(uid, operational_canvas_id, submission_id)
    with session() as s:
        _lock_authorized_run_canvas(s, operational_canvas_id)
        return _profile_reservation_from_bound_identity(
            s, run_id=run_id, uid=uid, auth_canvas_id=auth_canvas_id,
            canvas_id=operational_canvas_id, target_node_id=str(target_node_id),
            target_port_id=str(target_port_id), plan_digest=str(plan_digest),
            terminalize_expired=True,
        )


def preallocate_or_adopt_profile_run_owner(
        submission_id: str, uid: str, auth_canvas_id: str | None,
        operational_canvas_id: str, target_node_id: str, target_port_id: str,
        plan_digest: str,
        input_manifest: list[dict[str, str]] | None = None,
        execution_manifest_sha256: str | None = None,
        execution_manifest_doc: str | None = None,
        request_id: str | None = None,
        ttl_seconds: float = RUN_PREALLOCATION_TTL_SECONDS) -> ProfileRunReservation:
    """Reserve once or return the exact durable state bound to this submission id."""
    uid, operational_canvas_id = str(uid), str(operational_canvas_id or "")
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    target_node_id = str(target_node_id or "")
    target_port_id = str(target_port_id or "")
    plan_digest = str(plan_digest or "")
    if not operational_canvas_id:
        raise ValueError("profile run requires an operational canvas id")
    if auth_canvas_id is not None and operational_canvas_id != auth_canvas_id:
        raise RuntimeError("authorized run canvas and operational canvas must match")
    if not target_node_id or not target_port_id or not _valid_plan_digest(plan_digest):
        raise ValueError("profile preallocation requires target node/port and plan digest")
    run_id = profile_submission_run_id(uid, operational_canvas_id, submission_id)
    token = secrets.token_urlsafe(32)
    with session() as s:
        if (execution_manifest_sha256 is None) != (execution_manifest_doc is None):
            raise ValueError("execution manifest identity and document must be supplied together")
        _lock_authorized_run_canvas(s, operational_canvas_id)
        if s.get_bind().dialect.name == "sqlite":
            # SQLite ignores SELECT ... FOR UPDATE. Take its single writer lock on the already-locked
            # canvas before observing RunState, preserving Canvas -> RunState ordering while making two
            # fresh same-submission requests converge instead of racing into duplicate INSERTs.
            s.execute(update(Canvas).where(
                Canvas.id == operational_canvas_id,
            ).values(updated_at=Canvas.updated_at))
        existing = _profile_reservation_from_bound_identity(
            s, run_id=run_id, uid=uid, auth_canvas_id=auth_canvas_id,
            canvas_id=operational_canvas_id, target_node_id=target_node_id,
            target_port_id=target_port_id, plan_digest=plan_digest,
            terminalize_expired=True,
        )
        if existing is not None:
            if execution_manifest_sha256 is not None:
                state = s.get(RunState, run_id)
                # A legacy identity has no snapshot to compare and remains explicitly
                # non-reconstructable. Never backfill it from a retry's current Canvas.
                if state is not None and state.execution_manifest_sha256 is not None:
                    _persist_execution_manifest(
                        s, execution_manifest_sha256, str(execution_manifest_doc))
                    if state.execution_manifest_sha256 != execution_manifest_sha256:
                        raise ProfileSubmissionConflict(
                            "profile submission id is already bound to a different execution manifest")
            return existing

        # Existing status publication locks RunState before ProfileJobRetention. Never acquire the
        # retention row on adoption: reversing that order could deadlock a replay against terminal
        # publication on Postgres. The canvas row serializes all same-canvas fresh allocations, so once
        # absence is observed under that lock it is safe to allocate the next sequence value.
        retention = _lock_profile_retention(s, operational_canvas_id)
        attempt_order = int(retention.next_attempt_order)
        retention.next_attempt_order = attempt_order + 1
        retention.updated_at = _now()
        if execution_manifest_sha256 is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, str(execution_manifest_doc))
        status_doc = {
            "run_id": run_id,
            "status": "queued",
            "job_type": "profile",
            "target_node_id": target_node_id,
            "target_port_id": target_port_id,
            "plan_digest": plan_digest,
            "profile_attempt_order": attempt_order,
            "request_id": request_id,
        }
        s.add(RunState(
            run_id=run_id, canvas_id=operational_canvas_id, status="queued",
            doc=json.dumps(status_doc), created_by=uid, auth_canvas_id=auth_canvas_id,
            request_id=request_id, job_type="profile", target_node_id=target_node_id,
            target_port_id=target_port_id,
            plan_digest=plan_digest, profile_attempt_order=attempt_order,
            execution_manifest_sha256=execution_manifest_sha256,
            preallocation_token=token,
            preallocation_expires_at=_run_preallocation_deadline(s, ttl_seconds),
        ))
        s.flush()
        if input_manifest is not None:
            sync_local_result_owner(s, "profile_job", run_id, input_manifest)
        return ProfileRunReservation(
            run_id, token, attempt_order, status_doc, True)


def preallocate_profile_run_owner(
        run_id: str, uid: str, auth_canvas_id: str | None, operational_canvas_id: str,
        target_node_id: str, target_port_id: str, plan_digest: str,
        input_manifest: list[dict[str, str]] | None = None,
        execution_manifest_sha256: str | None = None,
        execution_manifest_doc: str | None = None,
        request_id: str | None = None,
        ttl_seconds: float = RUN_PREALLOCATION_TTL_SECONDS) -> tuple[str, int]:
    """Mint a durable profile identity and DB-monotonic attempt order before kernel allocation.

    The returned opaque token is the one capability a kernel may consume to bind itself and start the
    worker. No kernel, subprocess, plugin, or source-side effect is allowed before this transaction.
    """
    run_id, uid = str(run_id), str(uid)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    operational_canvas_id = str(operational_canvas_id or "")
    target_node_id = str(target_node_id or "")
    target_port_id = str(target_port_id or "")
    plan_digest = str(plan_digest or "")
    if not operational_canvas_id:
        raise ValueError("profile run requires an operational canvas id")
    if auth_canvas_id is not None and operational_canvas_id != auth_canvas_id:
        raise RuntimeError("authorized run canvas and operational canvas must match")
    if not target_node_id or not target_port_id or not _valid_plan_digest(plan_digest):
        raise ValueError("profile preallocation requires target node/port and plan digest")

    token = secrets.token_urlsafe(32)
    with session() as s:
        if (execution_manifest_sha256 is None) != (execution_manifest_doc is None):
            raise ValueError("execution manifest identity and document must be supplied together")
        # Full profiles are canvas-scoped durable jobs even in open mode. Holding the real canvas row
        # closes delete/recreate races before the RunState and profile sequence are allocated.
        _lock_authorized_run_canvas(s, operational_canvas_id)
        state = s.get(RunState, run_id, with_for_update=True)
        if state is not None:
            raise RuntimeError(f"run '{run_id}' is already allocated")
        fenced = _terminal_fence_status(s, run_id)
        if fenced is not None:
            raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")

        retention = _lock_profile_retention(s, operational_canvas_id)
        attempt_order = int(retention.next_attempt_order)
        retention.next_attempt_order = attempt_order + 1
        retention.updated_at = _now()
        if execution_manifest_sha256 is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, str(execution_manifest_doc))
        status_doc = {
            "run_id": run_id,
            "status": "queued",
            "job_type": "profile",
            "target_node_id": target_node_id,
            "target_port_id": target_port_id,
            "plan_digest": plan_digest,
            "profile_attempt_order": attempt_order,
            "request_id": request_id,
        }
        s.add(RunState(
            run_id=run_id, canvas_id=operational_canvas_id, status="queued",
            doc=json.dumps(status_doc), created_by=uid, auth_canvas_id=auth_canvas_id,
            request_id=request_id, job_type="profile", target_node_id=target_node_id,
            target_port_id=target_port_id,
            plan_digest=plan_digest, profile_attempt_order=attempt_order,
            execution_manifest_sha256=execution_manifest_sha256,
            preallocation_token=token,
            preallocation_expires_at=_run_preallocation_deadline(s, ttl_seconds),
        ))
        s.flush()
        if input_manifest is not None:
            sync_local_result_owner(s, "profile_job", run_id, input_manifest)
    return token, attempt_order


def consume_profile_run_preallocation(
        run_id: str, token: str, *, canvas_id: str, kernel_id: str,
        target_node_id: str, target_port_id: str,
        plan_digest: str) -> tuple[bool, dict]:
    """Atomically bind one profile preallocation to its kernel before child dispatch.

    Returns ``(True, queued_status)`` for the one token-consuming admission. A response-lost replay on
    the same authenticated kernel and exact durable identity returns ``(False, current_status)`` and
    therefore cannot launch a second worker; the one-time token is no longer consulted after consumption.
    Any identity mismatch, expired initial token, or cross-kernel replay fails closed.
    """
    run_id, canvas_id, kernel_id = str(run_id), str(canvas_id), str(kernel_id)
    target_node_id = str(target_node_id)
    target_port_id = str(target_port_id)
    plan_digest = str(plan_digest)
    with session() as s:
        state = _lock_existing_run_identity(s, run_id)
        if (state is None or state.job_type != "profile"
                or state.canvas_id != canvas_id
                or state.target_node_id != target_node_id
                or state.target_port_id != target_port_id
                or state.plan_digest != plan_digest
                or state.profile_attempt_order is None):
            raise RuntimeError("profile admission identity does not match its preallocation")

        now = _db_now(s)
        if state.preallocation_token is None:
            if state.kernel_id != kernel_id:
                raise RuntimeError("profile run is already bound to a different kernel")
            try:
                current = json.loads(state.doc)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("profile run has an invalid durable status") from exc
            if not isinstance(current, dict):
                raise RuntimeError("profile run has an invalid durable status")
            return False, current

        if (not secrets.compare_digest(state.preallocation_token, str(token))
                or not _run_preallocation_active(state.preallocation_expires_at, now)):
            raise RuntimeError("profile run preallocation is invalid or expired")

        state.kernel_id = kernel_id
        state.preallocation_token = None
        state.preallocation_expires_at = None
        try:
            queued = json.loads(state.doc)
        except (TypeError, ValueError) as exc:  # pragma: no cover - written by this module
            raise RuntimeError("profile run has an invalid queued status") from exc
        payload = json.dumps(queued, default=str)
        _upsert_profile_latest(
            s, canvas_id=canvas_id, target_node_id=target_node_id,
            target_port_id=target_port_id,
            plan_digest=plan_digest, run_id=run_id, payload=payload,
            attempt_order=int(state.profile_attempt_order),
            submitted_at=state.created_at or _now(),
        )
        return True, queued


def admitted_profile_run_status(
        run_id: str, uid: str, auth_canvas_id: str | None, *, canvas_id: str,
        target_node_id: str, target_port_id: str,
        plan_digest: str, attempt_order: int) -> dict | None:
    """Return the durable status only after the exact profile preallocation was consumed."""
    with session() as s:
        state = s.get(RunState, str(run_id))
        if (state is None or state.created_by != str(uid)
                or state.auth_canvas_id != (
                    str(auth_canvas_id) if auth_canvas_id is not None else None)
                or state.canvas_id != str(canvas_id)
                or state.job_type != "profile"
                or state.target_node_id != str(target_node_id)
                or state.target_port_id != str(target_port_id)
                or state.plan_digest != str(plan_digest)
                or state.profile_attempt_order != int(attempt_order)
                or state.preallocation_token is not None or state.kernel_id is None):
            return None
        try:
            parsed = json.loads(state.doc)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def renew_run_preallocation(
        run_id: str, token: str,
        ttl_seconds: float = RUN_PREALLOCATION_TTL_SECONDS) -> bool:
    """Extend one still-live, unbound preallocation using the metadata database clock."""
    with session() as s:
        state = _lock_existing_run_identity(s, str(run_id))
        if state is None or state.status not in _PREALLOCATION_STATES:
            return False
        backend = s.get(RunBackendJob, str(run_id), with_for_update=True)
        now = _db_now(s)
        if (backend is not None or state.preallocation_token is None
                or not secrets.compare_digest(state.preallocation_token, str(token))
                or not _run_preallocation_active(state.preallocation_expires_at, now)):
            return False
        state.preallocation_expires_at = _run_preallocation_deadline(s, ttl_seconds)
        return True


def _abandon_run_preallocation_attempts(
        s, run_id: str, *, quiet_seconds: float = 60) -> None:
    """Install writer-terminal proof for attempts that never reached a durable backend binding."""
    attempts = list(s.scalars(select(ObjectAttempt).where(
        ObjectAttempt.run_id == str(run_id),
        ObjectAttempt.state.in_(("allocated", "writing")),
    ).order_by(ObjectAttempt.uri).with_for_update()))
    if not attempts:
        return
    now = _db_now(s)
    quiet_until = now + datetime.timedelta(
        seconds=_gc_seconds(quiet_seconds, "quiet_seconds"))
    uris = [attempt.uri for attempt in attempts]
    for attempt in attempts:
        attempt.state = "abandoned"
        attempt.terminal_proof_at = now
        attempt.quiet_until = quiet_until
    for lease in s.scalars(select(ObjectAttemptLease).where(
            ObjectAttemptLease.attempt_uri.in_(uris),
            ObjectAttemptLease.lease_type.in_(("write", "publish")))):
        s.delete(lease)


def discard_run_preallocation(
        run_id: str, token: str, uid: str, auth_canvas_id: str | None) -> bool:
    """Permanently discard one exact unbound run identity after synchronous startup fails."""
    run_id, uid = str(run_id), str(uid)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    with session() as s:
        state = _lock_existing_run_identity(s, run_id)
        if state is None or state.status not in _PREALLOCATION_STATES:
            return False
        backend = s.get(RunBackendJob, run_id, with_for_update=True)
        if (backend is not None or state.preallocation_token is None
                or not secrets.compare_digest(state.preallocation_token, str(token))
                or state.created_by != uid or state.auth_canvas_id != auth_canvas_id):
            return False
        if auth_canvas_id is not None and state.canvas_id != auth_canvas_id:
            return False
        _abandon_run_preallocation_attempts(s, run_id)
        _replace_attempt_ref(s, "run_state", run_id, None)
        _record_terminal_fence(s, run_id, "failed")
        execution_manifest_sha256 = state.execution_manifest_sha256
        s.delete(state)
        s.flush()
        _delete_unreferenced_execution_manifests(
            s, {execution_manifest_sha256})
        return True


def settle_profile_submission_failure(
        run_id: str, token: str, uid: str, auth_canvas_id: str | None, *,
        canvas_id: str, target_node_id: str, target_port_id: str,
        plan_digest: str,
        attempt_order: int,
        reason: str = "execution kernel rejected profile submission before admission",
        ) -> tuple[str, dict | None]:
    """Atomically classify a failed kernel command as discarded or already admitted.

    The canvas lock serializes this transaction with deletion and the RunState lock waits for any
    concurrent kernel token-consume commit. Therefore callers never make a racy read-then-settle
    decision: the exact token still present becomes a durable unadmitted failure, while a
    consumed/kernel-bound identity returns its durable status for response-loss adoption.
    """
    run_id, uid, canvas_id = str(run_id), str(uid), str(canvas_id)
    auth_canvas_id = str(auth_canvas_id) if auth_canvas_id is not None else None
    target_node_id = str(target_node_id)
    target_port_id = str(target_port_id)
    plan_digest = str(plan_digest)
    attempt_order = int(attempt_order)
    with session() as s:
        _lock_authorized_run_canvas(s, canvas_id)
        state = _lock_existing_run_identity(s, run_id)
        if state is None:
            fence = s.get(RunTerminalFence, run_id, with_for_update=True)
            if (fence is None or not _profile_binding_matches(
                    fence, uid=uid, auth_canvas_id=auth_canvas_id,
                    canvas_id=canvas_id, target_node_id=target_node_id,
                    target_port_id=target_port_id,
                    plan_digest=plan_digest)
                    or fence.profile_attempt_order != attempt_order):
                return "identity_mismatch", None
            latest = s.get(
                ProfileJobLatest,
                (canvas_id, target_node_id, plan_digest),
                with_for_update=True,
            )
            if (latest is not None and latest.run_id == run_id
                    and latest.attempt_order == attempt_order):
                try:
                    parsed = json.loads(latest.doc)
                except (TypeError, ValueError):
                    return "identity_mismatch", None
                return ("admitted", parsed) if isinstance(parsed, dict) else (
                    "identity_mismatch", None)
            return ("discarded", None) if fence.status == "failed" else (
                "identity_mismatch", None)

        if (state.created_by != uid or state.auth_canvas_id != auth_canvas_id
                or state.canvas_id != canvas_id or state.job_type != "profile"
                or state.target_node_id != target_node_id
                or state.target_port_id != target_port_id
                or state.plan_digest != plan_digest
                or state.profile_attempt_order != attempt_order):
            return "identity_mismatch", None
        if state.preallocation_token is not None:
            if not secrets.compare_digest(state.preallocation_token, str(token)):
                return "identity_mismatch", None
            failed = _terminalize_unadmitted_profile(s, state, reason=str(reason))
            return "discarded", failed
        if state.kernel_id is None:
            return "identity_mismatch", None
        try:
            parsed = json.loads(state.doc)
        except (TypeError, ValueError):
            return "identity_mismatch", None
        return ("admitted", parsed) if isinstance(parsed, dict) else (
            "identity_mismatch", None)


def finish_run_preallocation(run_id: str, token: str, status_doc: dict) -> bool:
    """Settle a synchronous prebound return, or confirm its durable backend consumed the lease."""
    run_id = str(run_id)
    status_doc = dict(status_doc)
    if str(status_doc.get("run_id") or "") != run_id:
        raise ValueError("preallocated run status does not match its run id")
    status = str(status_doc.get("status") or "")
    with session() as s:
        state = _lock_existing_run_identity(s, run_id)
        if state is None:
            fenced = _terminal_fence_status(s, run_id)
            return status in _TERMINAL_RUN and fenced == status
        if state.created_by is None:
            return False
        backend = s.get(RunBackendJob, run_id, with_for_update=True)
        if backend is not None:
            if (state.preallocation_token is not None
                    or state.preallocation_expires_at is not None):
                raise RuntimeError("backend binding did not consume its run preallocation")
            backend_ref = status_doc.get("backend_ref")
            if (not isinstance(backend_ref, dict) or (
                    str(backend_ref.get("backend") or ""),
                    str(backend_ref.get("attempt_id") or ""),
                    str(backend_ref.get("submission_id") or ""),
            ) != (backend.backend, backend.attempt_id, backend.submission_id)):
                raise RuntimeError("returned status does not match the durable backend binding")
            return True
        if (state.preallocation_token is None
                or not secrets.compare_digest(state.preallocation_token, str(token))):
            return False
        if status not in _TERMINAL_RUN:
            # A prebound runner that owns the run locally (no external backend — e.g. the Popen path or
            # an unsupported-shape fallback) consumes the lease and keeps supervising in-process. Only
            # release the preallocation fence; never abandon the live writer's attempts or clobber a
            # status the concurrent supervisor has already advanced past preallocation.
            if state.status in _PREALLOCATION_STATES:
                state.status = status
                state.doc = json.dumps(status_doc, default=str)
            state.preallocation_token = None
            state.preallocation_expires_at = None
            return True
        _abandon_run_preallocation_attempts(s, run_id)
        state.status = status
        state.doc = json.dumps(status_doc, default=str)
        state.preallocation_token = None
        state.preallocation_expires_at = None
        _record_terminal_fence(s, run_id, status)
        return True


def run_auth(run_id: str) -> tuple[str | None, str | None]:
    """Return a run's creator and authorized real-canvas id, or nulls if identity is unavailable."""
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


class WorkspaceVersionConflict(RuntimeError):
    """A Workspace edit was based on a stale container or placement version."""


class WorkspaceTransformCompatibilityConflict(RuntimeError):
    """An exact Transform upgrade lacks compatible schema evidence."""


class WorkspaceTransformUnavailable(RuntimeError):
    """An exact Transform disappeared between admission and the atomic write."""


class NativeCanvasImportConflict(RuntimeError):
    """One owner/import UUID is already bound to different normalized import intent."""


class CanvasCopyConflict(RuntimeError):
    """One owner/copy UUID is already bound to different copy intent."""


class WorkspaceNameConflict(ValueError):
    """A sibling container already owns the requested display name."""


class DatasetViewSubmissionConflict(RuntimeError):
    """A DatasetView submission id was reused for a different immutable intent."""


class DatasetViewGone(RuntimeError):
    """A retained DatasetView tombstone forbids replay or exact access."""


def _workspace_name(name: str) -> str:
    normalized = str(name).strip()
    if not normalized:
        raise ValueError("workspace name must not be blank")
    if "\x00" in normalized:
        raise ValueError("workspace name must not contain NUL")
    return normalized


def _workspace_ordinal(ordinal: int) -> int:
    if isinstance(ordinal, bool) or int(ordinal) != ordinal or ordinal < 0:
        raise ValueError("workspace ordinal must be a non-negative integer")
    return int(ordinal)


def local_workspace_root() -> dict:
    """Return the persisted, installation-local root identity from the fresh schema baseline."""
    with session() as s:
        root = s.get(WorkspaceContainer, LOCAL_WORKSPACE_ROOT_ID)
        if root is None or not root.is_root:
            raise RuntimeError("local Workspace root is missing from the metadata baseline")
        return _workspace_container_doc(root)


def _workspace_container_doc(row: WorkspaceContainer) -> dict:
    return {"id": row.id, "parentId": row.parent_id, "name": row.name,
            "ordinal": row.ordinal, "version": row.version, "isRoot": row.is_root}


def _workspace_placement_doc(row: WorkspacePlacement) -> dict:
    return {"id": row.id, "containerId": row.container_id, "targetKind": row.target_kind,
            "targetId": row.target_id, "name": row.name, "ordinal": row.ordinal,
            "version": row.version}


def _workspace_container_locked(s, container_id: str) -> WorkspaceContainer:
    row = s.get(WorkspaceContainer, container_id, with_for_update=True)
    if row is None:
        raise KeyError(f"workspace container '{container_id}' not found")
    return row


def _workspace_writable_destination_locked(s, container_id: str) -> WorkspaceContainer:
    """Lock a destination and reject writes anywhere below a detached Catalog tombstone."""
    destination = _workspace_container_locked(s, container_id)
    current: WorkspaceContainer | None = destination
    visited: set[str] = set()
    while current is not None:
        if current.id in visited:
            raise RuntimeError("Workspace container hierarchy contains a cycle")
        visited.add(current.id)
        if current.catalog_folder_state == "detached":
            raise ValueError("a deleted Catalog folder subtree is a read-only Workspace tombstone")
        current = (s.get(WorkspaceContainer, current.parent_id, with_for_update=True)
                   if current.parent_id is not None else None)
    return destination


def _workspace_placement_locked(s, placement_id: str) -> WorkspacePlacement:
    row = s.get(WorkspacePlacement, placement_id, with_for_update=True)
    if row is None:
        raise KeyError(f"workspace placement '{placement_id}' not found")
    return row


def _workspace_version_conflict(kind: str, identity: str, expected_version: int) -> None:
    raise WorkspaceVersionConflict(
        f"workspace {kind} '{identity}' changed from expected version {expected_version}")


@contextlib.contextmanager
def _workspace_write_session():
    """Serialize Workspace writes before reading cross-row hierarchy invariants."""
    with session() as s:
        if _is_sqlite_database():
            # SQLite ignores SELECT FOR UPDATE.  Acquire its one writer slot before validating a move
            # so opposite concurrent moves cannot both validate and create a container cycle.
            s.connection().exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # The persisted root is the local authority's narrow write mutex.  Lock it first so two
            # cross-parent edits cannot deadlock or validate opposite moves against stale hierarchies.
            root = s.get(WorkspaceContainer, LOCAL_WORKSPACE_ROOT_ID, with_for_update=True)
            if root is None or not root.is_root:
                raise RuntimeError("local Workspace root is missing from the metadata baseline")
        yield s


def workspace_create_container(parent_id: str, name: str, *, ordinal: int = 0) -> dict:
    """Create one local overlay container beneath a stable parent identity."""
    name, ordinal = _workspace_name(name), _workspace_ordinal(ordinal)
    with _workspace_write_session() as s:
        _workspace_writable_destination_locked(s, parent_id)
        if s.scalar(select(WorkspaceContainer.id).where(
                WorkspaceContainer.parent_id == parent_id, WorkspaceContainer.name == name,
                WorkspaceContainer.catalog_folder_id.is_(None))) is not None:
            raise WorkspaceNameConflict(f"workspace container '{name}' already exists under its parent")
        row = WorkspaceContainer(parent_id=parent_id, name=name, ordinal=ordinal)
        s.add(row)
        s.flush()
        return _workspace_container_doc(row)


def _workspace_parent_is_descendant(s, candidate_parent_id: str, container_id: str) -> bool:
    current_id: str | None = candidate_parent_id
    while current_id is not None:
        if current_id == container_id:
            return True
        current = s.get(WorkspaceContainer, current_id, with_for_update=True)
        if current is None:
            raise KeyError(f"workspace container '{candidate_parent_id}' not found")
        current_id = current.parent_id
    return False


def workspace_update_container(container_id: str, *, expected_version: int, name: str | None = None,
                               parent_id: str | None = None, ordinal: int | None = None) -> dict:
    """CAS-update a container's display/navigation fields without changing its opaque identity."""
    with _workspace_write_session() as s:
        row = _workspace_container_locked(s, container_id)
        if row.is_root:
            raise ValueError("the local Workspace root cannot be edited")
        if row.catalog_folder_id is not None:
            raise ValueError("a projected Catalog folder is managed by the Catalog")
        if row.version != expected_version:
            _workspace_version_conflict("container", container_id, expected_version)
        target_parent = parent_id if parent_id is not None else row.parent_id
        if target_parent is None:
            raise ValueError("an overlay container requires a parent")
        if _workspace_parent_is_descendant(s, target_parent, container_id):
            raise ValueError("a workspace container cannot become its own descendant")
        _workspace_writable_destination_locked(s, target_parent)
        target_name = _workspace_name(name) if name is not None else row.name
        target_ordinal = _workspace_ordinal(ordinal) if ordinal is not None else row.ordinal
        sibling = s.scalar(select(WorkspaceContainer.id).where(
            WorkspaceContainer.parent_id == target_parent,
            WorkspaceContainer.name == target_name,
            WorkspaceContainer.id != container_id,
            WorkspaceContainer.catalog_folder_id.is_(None),
        ))
        if sibling is not None:
            raise WorkspaceNameConflict(f"workspace container '{target_name}' already exists under its parent")
        changed = s.execute(update(WorkspaceContainer).where(
            WorkspaceContainer.id == container_id,
            WorkspaceContainer.version == expected_version,
        ).values(
            parent_id=target_parent,
            name=target_name,
            ordinal=target_ordinal,
            version=WorkspaceContainer.version + 1,
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            _workspace_version_conflict("container", container_id, expected_version)
        s.refresh(row)
        return _workspace_container_doc(row)


def workspace_delete_container(container_id: str, *, expected_version: int) -> None:
    """Delete an empty overlay container; callers must explicitly move its children first."""
    with _workspace_write_session() as s:
        row = _workspace_container_locked(s, container_id)
        if row.is_root:
            raise ValueError("the local Workspace root cannot be deleted")
        if row.catalog_folder_id is not None:
            raise ValueError("a projected Catalog folder is managed by the Catalog")
        if row.version != expected_version:
            _workspace_version_conflict("container", container_id, expected_version)
        if s.scalar(select(WorkspaceContainer.id).where(
                WorkspaceContainer.parent_id == container_id).limit(1)) is not None:
            raise ValueError("cannot delete a workspace container with child containers")
        if s.scalar(select(WorkspacePlacement.id).where(
                WorkspacePlacement.container_id == container_id).limit(1)) is not None:
            raise ValueError("cannot delete a workspace container with placements")
        removed = s.execute(delete(WorkspaceContainer).where(
            WorkspaceContainer.id == container_id,
            WorkspaceContainer.version == expected_version,
        ).execution_options(synchronize_session=False))
        if removed.rowcount != 1:
            _workspace_version_conflict("container", container_id, expected_version)


def workspace_builtin_dataset_identity(uri: str) -> str:
    """Resolve a registered built-in dataset to its stable registration identity, never its path."""
    with session() as s:
        entry = s.get(CatalogEntry, uri)
        if entry is None:
            raise KeyError(f"registered dataset '{uri}' not found")
        return entry.registration_id


def _workspace_catalog_projection_parent(s, path: str) -> str:
    parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent_path:
        return LOCAL_WORKSPACE_ROOT_ID
    parent = s.scalar(select(CatalogFolder).where(CatalogFolder.path == parent_path).limit(1))
    if parent is None:
        raise RuntimeError("Catalog folder projection parent is missing")
    projection = s.scalar(select(WorkspaceContainer).where(
        WorkspaceContainer.catalog_folder_id == parent.id).limit(1))
    if projection is None:
        raise RuntimeError("Catalog folder projection parent was not materialized")
    return projection.id


def _workspace_sync_catalog_folder_projections_in_session(s, paths: list[str] | None = None) -> None:
    """Mirror current built-in Catalog folders into local Workspace navigation.

    The Catalog remains the only authority for folder hierarchy.  Workspace only persists a stable
    overlay container keyed by the folder's opaque ID; that gives Canvas placements durable targets
    across folder path edits without copying provider hierarchy or using paths as identities.
    """
    query = select(CatalogFolder)
    if paths is not None:
        wanted = list(dict.fromkeys(path for path in paths if path))
        if not wanted:
            return
        query = query.where(CatalogFolder.path.in_(wanted))
    folders = list(s.scalars(query.order_by(CatalogFolder.path)))
    for folder in folders:
        parent_id = _workspace_catalog_projection_parent(s, folder.path)
        name = folder.path.rsplit("/", 1)[-1]
        projection = s.scalar(select(WorkspaceContainer).where(
            WorkspaceContainer.catalog_folder_id == folder.id).limit(1))
        if projection is None:
            projection = WorkspaceContainer(
                parent_id=parent_id, name=name, ordinal=0, version=1,
                catalog_folder_id=folder.id, catalog_folder_state="current",
                catalog_folder_path=folder.path,
            )
            s.add(projection)
        else:
            if (projection.parent_id, projection.name, projection.catalog_folder_state,
                    projection.catalog_folder_path) != (parent_id, name, "current", folder.path):
                projection.parent_id = parent_id
                projection.name = name
                projection.catalog_folder_state = "current"
                projection.catalog_folder_path = folder.path
                projection.version += 1
    s.flush()


def _workspace_tombstone_catalog_folder_projection_in_session(s, folder_id: str) -> None:
    projection = s.scalar(select(WorkspaceContainer).where(
        WorkspaceContainer.catalog_folder_id == folder_id).limit(1))
    if projection is None:
        return
    # Keep an orphan independently navigable and explain its last-known path even if a former
    # ancestor later moves.  Its local Canvas overlay remains recoverable but cannot receive writes.
    projection.parent_id = LOCAL_WORKSPACE_ROOT_ID
    projection.name = f"Deleted Catalog folder: {projection.catalog_folder_path or projection.name}"
    projection.catalog_folder_state = "detached"
    projection.version += 1


def _workspace_catalog_container_for_folder_in_session(s, folder: str) -> str:
    if not folder:
        return LOCAL_WORKSPACE_ROOT_ID
    row = s.scalar(select(CatalogFolder).where(CatalogFolder.path == folder).limit(1))
    if row is None:
        raise RuntimeError("Catalog folder projection is missing")
    projection = s.scalar(select(WorkspaceContainer).where(
        WorkspaceContainer.catalog_folder_id == row.id,
        WorkspaceContainer.catalog_folder_state == "current").limit(1))
    if projection is None:
        raise RuntimeError("Catalog folder projection is unavailable")
    return projection.id


def _workspace_sync_dataset_folder_in_session(
        s, *, dataset_id: str, name: str, folder: str) -> None:
    """Place a built-in dataset beside its Catalog folder using its registration identity."""
    container_id = _workspace_catalog_container_for_folder_in_session(s, folder)
    _workspace_ensure_root_placement_in_session(
        s, target_kind="dataset", target_id=dataset_id, name=name or "dataset")
    placement = s.scalar(select(WorkspacePlacement).where(
        WorkspacePlacement.target_kind == "dataset",
        WorkspacePlacement.target_id == dataset_id).limit(1))
    if placement is None:
        raise RuntimeError("Workspace dataset placement was not created")
    placement.container_id = container_id


def _workspace_ensure_root_placement_in_session(
        s, *, target_kind: str, target_id: str, name: str) -> None:
    """Give a local resource its canonical root placement without moving an existing one."""
    if target_kind not in {"canvas", "dataset"}:
        raise ValueError("workspace placement target kind must be 'canvas' or 'dataset'")
    root = s.get(WorkspaceContainer, LOCAL_WORKSPACE_ROOT_ID)
    if root is None or not root.is_root:
        raise RuntimeError("local Workspace root is missing from the metadata baseline")
    values = {
        "id": _uid(), "container_id": LOCAL_WORKSPACE_ROOT_ID,
        "target_kind": target_kind, "target_id": target_id,
        "name": _workspace_name(name), "ordinal": 0, "version": 1,
    }
    dialect = s.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
        raise RuntimeError(f"unsupported metadata database dialect: {dialect}")
    s.execute(dialect_insert(WorkspacePlacement).values(**values).on_conflict_do_nothing(
        index_elements=[WorkspacePlacement.target_kind, WorkspacePlacement.target_id]))


def import_native_canvas(
        *, uid: str, canvas_id: str, doc: dict, intent_digest: str) -> bool:
    """Insert one exact native import or verify an equivalent concurrent/retry winner."""
    if re.fullmatch(r"[0-9a-f]{64}", str(intent_digest)) is None:
        raise ValueError("native Canvas intent digest is invalid")
    stored_doc = {
        **doc,
        "_nativeImport": {"intentDigest": str(intent_digest)},
    }
    canonical_doc = json.dumps(
        stored_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    values = {
        "id": str(canvas_id), "owner_id": str(uid),
        "name": str(doc.get("name") or "untitled"), "version": 1,
        "doc": canonical_doc,
    }
    with session() as s:
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
            raise RuntimeError(f"unsupported metadata database dialect: {dialect}")
        inserted = s.scalar(dialect_insert(Canvas).values(**values).on_conflict_do_nothing(
            index_elements=[Canvas.id]).returning(Canvas.id))
        if inserted is None:
            existing = s.get(Canvas, str(canvas_id), with_for_update=True)
            if existing is None:  # pragma: no cover - winner is visible after conflict handling
                raise RuntimeError("native Canvas import conflict winner is unavailable")
            try:
                existing_doc = json.loads(existing.doc)
            except (TypeError, ValueError) as exc:
                raise NativeCanvasImportConflict(
                    "native Canvas import id is already bound to invalid content") from exc
            if (existing.owner_id != str(uid) or existing.version != 1
                    or existing.name != values["name"] or existing_doc != stored_doc):
                raise NativeCanvasImportConflict(
                    "native Canvas import id is already bound to different import intent")
            return False
        sync_local_result_owner(s, "canvas", str(canvas_id), stored_doc)
        _replace_promoted_transform_refs(s, "canvas", str(canvas_id), stored_doc)
        _workspace_ensure_root_placement_in_session(
            s, target_kind="canvas", target_id=str(canvas_id), name=values["name"])
        return True


def create_canvas_copy(
        *, uid: str, canvas_id: str, doc: dict, intent_digest: str, request_digest: str,
        container_id: str, expected_container_version: int,
        source_canvas_id: str, source_canvas_version: int | None = None,
        source_subject_id: str | None = None,
        source_manifest_sha256: str | None = None) -> bool:
    """Atomically create one private owner Canvas and exact Workspace placement."""
    stored_doc = {**doc, "_copyIntent": {
        "digest": str(intent_digest), "requestDigest": str(request_digest),
    }}
    canonical_doc = json.dumps(
        stored_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def replay(row: Canvas) -> bool:
        try:
            existing = json.loads(row.doc)
        except (TypeError, ValueError) as exc:
            raise CanvasCopyConflict("copyId is already bound to invalid content") from exc
        if (row.owner_id != str(uid)
                or existing.get("_copyIntent", {}).get("digest") != str(intent_digest)
                or existing.get("_copyIntent", {}).get("requestDigest") != str(request_digest)):
            raise CanvasCopyConflict("copyId is already bound to different copy intent")
        return False

    with session() as s:
        existing = s.get(Canvas, str(canvas_id), with_for_update=True)
        if existing is not None:
            return replay(existing)
        _workspace_container_at_version(s, container_id, expected_container_version)
        source = s.get(Canvas, str(source_canvas_id), with_for_update=True)
        if source is None or _workspace_canvas_role_in_session(s, source, str(uid)) is None:
            raise KeyError(f"canvas '{source_canvas_id}' not found")
        if source_subject_id is None:
            if source_canvas_version is None or source.version != source_canvas_version:
                raise WorkspaceVersionConflict(
                    f"canvas '{source_canvas_id}' changed from expected version {source_canvas_version}")
            retained_owner_kind, retained_owner_key = "canvas", str(source_canvas_id)
        else:
            found, identity = _execution_manifest_identity_for_subject_in_session(
                s, str(source_canvas_id), str(source_subject_id))
            if (not found or identity != source_manifest_sha256
                    or s.get(ExecutionManifest, identity) is None):
                raise WorkspaceVersionConflict(
                    "retained execution manifest changed or became unavailable")
            retained_owner_kind, retained_owner_key = "execution_manifest", str(identity)
        _require_promoted_transform_use_in_session(
            s, uid, stored_doc,
            retained_owner_kind=retained_owner_kind,
            retained_owner_key=retained_owner_key)
        values = {
            "id": str(canvas_id), "owner_id": str(uid),
            "name": str(doc.get("name") or "untitled"), "version": 1,
            "doc": canonical_doc,
        }
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:  # pragma: no cover
            raise RuntimeError(f"unsupported metadata database dialect: {dialect}")
        inserted = s.scalar(dialect_insert(Canvas).values(**values).on_conflict_do_nothing(
            index_elements=[Canvas.id]).returning(Canvas.id))
        if inserted is None:
            winner = s.get(Canvas, str(canvas_id), with_for_update=True)
            if winner is None:  # pragma: no cover
                raise RuntimeError("Canvas copy conflict winner is unavailable")
            return replay(winner)
        sync_local_result_owner(s, "canvas", str(canvas_id), stored_doc)
        _replace_promoted_transform_refs(s, "canvas", str(canvas_id), stored_doc)
        s.add(WorkspacePlacement(
            container_id=container_id, target_kind="canvas", target_id=str(canvas_id),
            name=values["name"], ordinal=0, version=1))
        return True


def canvas_copy_replay(
        uid: str, canvas_id: str, intent_digest: str, request_digest: str) -> bool | None:
    """Return replay state before touching a source that may have since been deleted."""
    with session() as s:
        row = s.get(Canvas, str(canvas_id))
        if row is None:
            return None
        try:
            document = json.loads(row.doc)
        except (TypeError, ValueError) as exc:
            raise CanvasCopyConflict("copyId is already bound to invalid content") from exc
        if (row.owner_id != str(uid)
                or document.get("_copyIntent", {}).get("digest") != str(intent_digest)
                or document.get("_copyIntent", {}).get("requestDigest") != str(request_digest)):
            raise CanvasCopyConflict("copyId is already bound to different copy intent")
        return True


def _workspace_follow_target_name_in_session(
        s, *, target_kind: str, target_id: str, previous_name: str, name: str) -> None:
    """Follow a target rename only while the placement still uses the prior derived name."""
    previous, current = _workspace_name(previous_name), _workspace_name(name)
    if previous == current:
        return
    s.execute(update(WorkspacePlacement).where(
        WorkspacePlacement.target_kind == target_kind,
        WorkspacePlacement.target_id == target_id,
        WorkspacePlacement.name == previous,
    ).values(name=current, version=WorkspacePlacement.version + 1))


def _workspace_backfill_root_placements_in_session(s) -> None:
    """One-way pre-1.0 migration of existing local resources into the Workspace root."""
    for canvas_id, name in s.execute(select(Canvas.id, Canvas.name)).all():
        _workspace_ensure_root_placement_in_session(
            s, target_kind="canvas", target_id=canvas_id, name=name or "untitled")
    for dataset_id, name in s.execute(select(CatalogEntry.registration_id, CatalogEntry.name)).all():
        _workspace_ensure_root_placement_in_session(
            s, target_kind="dataset", target_id=dataset_id, name=name or "dataset")


def workspace_create_placement(container_id: str, *, target_kind: str, target_id: str,
                               name: str, ordinal: int = 0) -> dict:
    """Create the sole canonical local placement for a canvas or registered dataset identity."""
    if target_kind not in {"canvas", "dataset"}:
        raise ValueError("workspace placement target kind must be 'canvas' or 'dataset'")
    name, ordinal = _workspace_name(name), _workspace_ordinal(ordinal)
    with _workspace_write_session() as s:
        _workspace_writable_destination_locked(s, container_id)
        if target_kind == "canvas":
            if s.get(Canvas, target_id, with_for_update=True) is None:
                raise KeyError(f"canvas '{target_id}' not found")
        elif s.scalar(select(CatalogEntry.uri).where(
                CatalogEntry.registration_id == target_id).limit(1).with_for_update()) is None:
            raise KeyError(f"registered dataset identity '{target_id}' not found")
        if target_kind == "dataset":
            raise ValueError("built-in datasets are placed by the Catalog")
        if s.scalar(select(WorkspacePlacement.id).where(
                WorkspacePlacement.target_kind == target_kind,
                WorkspacePlacement.target_id == target_id)) is not None:
            raise ValueError("workspace target already has a canonical placement")
        row = WorkspacePlacement(container_id=container_id, target_kind=target_kind,
                                 target_id=target_id, name=name, ordinal=ordinal)
        s.add(row)
        s.flush()
        return _workspace_placement_doc(row)


def workspace_update_placement(placement_id: str, *, expected_version: int,
                               container_id: str | None = None, name: str | None = None,
                               ordinal: int | None = None) -> dict:
    """CAS-update a canvas placement; other local resource kinds own their placement lifecycle."""
    with _workspace_write_session() as s:
        row = _workspace_placement_locked(s, placement_id)
        if row.version != expected_version:
            _workspace_version_conflict("placement", placement_id, expected_version)
        if row.target_kind == "dataset":
            raise ValueError("built-in datasets are placed by the Catalog")
        if row.target_kind != "canvas":
            raise ValueError(
                "immutable DatasetView placements are managed by their DatasetView")
        target_container = container_id if container_id is not None else row.container_id
        _workspace_writable_destination_locked(s, target_container)
        target_name = _workspace_name(name) if name is not None else row.name
        target_ordinal = _workspace_ordinal(ordinal) if ordinal is not None else row.ordinal
        changed = s.execute(update(WorkspacePlacement).where(
            WorkspacePlacement.id == placement_id,
            WorkspacePlacement.version == expected_version,
        ).values(
            container_id=target_container,
            name=target_name,
            ordinal=target_ordinal,
            version=WorkspacePlacement.version + 1,
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            _workspace_version_conflict("placement", placement_id, expected_version)
        s.refresh(row)
        return _workspace_placement_doc(row)


def _workspace_container_at_version(s, container_id: str,
                                    expected_version: int) -> WorkspaceContainer:
    row = _workspace_writable_destination_locked(s, container_id)
    if row.version != expected_version:
        _workspace_version_conflict("container", container_id, expected_version)
    return row


def _workspace_canvas_role_in_session(s, canvas: Canvas, uid: str) -> str | None:
    explicit_role = s.scalar(select(CanvasShare.role).where(
        CanvasShare.canvas_id == canvas.id, CanvasShare.user_id == uid))
    return _effective_canvas_role(canvas, uid, explicit_role)


def _workspace_dataset_source_in_session(s, dataset_id: str) -> tuple[CatalogEntry, dict]:
    entry = s.scalar(select(CatalogEntry).where(
        CatalogEntry.registration_id == dataset_id).limit(1).with_for_update())
    if entry is None:
        raise KeyError(f"registered dataset identity '{dataset_id}' not found")
    try:
        catalog_doc = json.loads(entry.doc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"registered dataset identity '{dataset_id}' has invalid metadata") from exc
    return entry, {
        "id": f"source-{_uid()}",
        "type": "source",
        "position": {"x": 160, "y": 160},
        "data": {
            "title": entry.name or "dataset",
            "status": "draft",
            "config": {"uri": entry.uri, "tableId": catalog_doc.get("id") or entry.tbl_id},
        },
    }


def _workspace_dataset_sources_in_session(s, dataset_ids: list[str]) -> list[dict]:
    if len(dataset_ids) != len(set(dataset_ids)):
        raise ValueError("dataset selection contains a duplicate identity")
    # A canonical lock order keeps concurrent PostgreSQL batches from deadlocking while preserving the
    # researcher's selection order in the resulting canvas.
    sources = {
        dataset_id: _workspace_dataset_source_in_session(s, dataset_id)[1]
        for dataset_id in sorted(dataset_ids)
    }
    return [sources[dataset_id] for dataset_id in dataset_ids]


def _workspace_place_sources(nodes: list[dict], sources: list[dict]) -> None:
    occupied = {(node.get("position", {}).get("x"), node.get("position", {}).get("y"))
                for node in nodes if isinstance(node, dict)}
    x, y = 160, 160
    for source in sources:
        while (x, y) in occupied:
            x += 60
            y += 40
        source["position"] = {"x": x, "y": y}
        occupied.add((x, y))
        nodes.append(source)


def _workspace_transform_node_in_session(
        s, *, uid: str, transform: dict, canvas_id: str | None = None) -> dict:
    processor = str(transform.get("id", ""))
    version = str(transform.get("version", ""))
    title = str(transform.get("title", ""))
    mode = str(transform.get("mode", ""))
    if not processor or not version or not title or not mode:
        raise ValueError("an exact Transform descriptor is required")
    if processor.startswith("tr_"):
        number = _promoted_transform_version_number(version)
        row = s.get(
            PromotedTransformVersion, (processor, number), with_for_update=True) if number else None
        if row is None or row.deleted_at is not None:
            raise WorkspaceTransformUnavailable(
                f"Transform {processor}@{version} is unavailable")
        identity = s.get(PromotedTransform, processor)
        if identity is None:
            raise RuntimeError("promoted Transform version has no logical identity")
        permitted = identity.owner_id == str(uid)
        if not permitted and canvas_id is not None:
            permitted = s.get(PromotedTransformVersionRef, (
                "canvas", str(canvas_id), processor, int(number))) is not None
        if not permitted:
            raise PermissionError(f"Transform {processor}@{version} is not available to this user")
        title, mode = row.title, row.mode
    return {
        "id": f"transform_{_uid()}",
        "type": "transform",
        "position": {"x": 160, "y": 160},
        "data": {
            "title": title,
            "status": "draft",
            "config": {
                "source": "library", "processor": processor,
                "version": version, "mode": mode,
            },
        },
    }


def _workspace_invalidate_downstream(doc: dict, node_id: str) -> None:
    nodes = doc.get("nodes", [])
    by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict)}
    downstream = {str(node_id)}
    # A transform contained by a Section contributes to the Section's execution identity. Mirror the
    # existing Canvas semantics narrowly here: invalidate its ancestor Sections and their edge
    # descendants, without changing unrelated containment behavior.
    parent = by_id.get(str(node_id), {}).get("parentId")
    while isinstance(parent, str) and parent and parent not in downstream:
        downstream.add(parent)
        parent = by_id.get(parent, {}).get("parentId")
    changed = True
    edges = doc.get("edges", [])
    while changed:
        changed = False
        for edge in edges if isinstance(edges, list) else []:
            if not isinstance(edge, dict):
                continue
            source, target = str(edge.get("source", "")), str(edge.get("target", ""))
            if source in downstream and target and target not in downstream:
                downstream.add(target)
                changed = True
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("id", "")) not in downstream:
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        if str(node.get("id", "")) == str(node_id):
            data["status"] = "draft" if data.get("status") == "draft" else "stale"
        elif data.get("status") == "latest":
            data["status"] = "stale"


def workspace_create_canvas_action(*, uid: str, container_id: str,
                                   expected_container_version: int, name: str,
                                   dataset_ids: list[str] | None = None,
                                   provider_sources: list[dict] | None = None,
                                   transform: dict | None = None) -> dict:
    """Atomically create one canvas at an exact local container with a bounded dataset selection."""
    canvas_name = _workspace_name(name)
    with _workspace_write_session() as s:
        container = _workspace_container_at_version(s, container_id, expected_container_version)
        sources = [*_workspace_dataset_sources_in_session(s, dataset_ids or []),
                   *(provider_sources or [])]
        if transform is not None:
            if sources:
                raise ValueError("a Transform target cannot include dataset sources")
            sources.append(_workspace_transform_node_in_session(
                s, uid=uid, transform=transform))
        nodes: list[dict] = []
        _workspace_place_sources(nodes, sources)
        canvas_id = _uid()
        doc = {
            "id": canvas_id, "name": canvas_name, "version": 1,
            "nodes": nodes, "edges": [],
        }
        canvas = Canvas(
            id=canvas_id, owner_id=uid, name=canvas_name, version=1, doc=json.dumps(doc))
        placement = WorkspacePlacement(
            container_id=container.id, target_kind="canvas", target_id=canvas_id,
            name=canvas_name, ordinal=0, version=1)
        s.add_all([canvas, placement])
        s.flush()
        sync_local_result_owner(s, "canvas", canvas_id, doc)
        _replace_promoted_transform_refs(s, "canvas", canvas_id, doc)
        return {
            "ok": True, "id": canvas_id, "created": True,
            "nodeId": nodes[0]["id"] if transform is not None else None,
            "resource": _workspace_placement_resource(placement, detached=False),
        }


def workspace_add_datasets_action(*, uid: str, canvas_id: str,
                                  expected_canvas_version: int, dataset_ids: list[str],
                                  provider_sources: list[dict] | None = None) -> dict:
    """Atomically append bounded sources resolved from exact registration identities."""
    with _workspace_write_session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            raise KeyError(f"canvas '{canvas_id}' not found")
        if _workspace_canvas_role_in_session(s, canvas, uid) not in ("owner", "editor"):
            raise PermissionError("you don't have edit access to this canvas")
        if canvas.version != expected_canvas_version:
            raise WorkspaceVersionConflict(
                f"canvas '{canvas_id}' changed from expected version {expected_canvas_version}")
        sources = [*_workspace_dataset_sources_in_session(s, dataset_ids),
                   *(provider_sources or [])]
        try:
            doc = json.loads(canvas.doc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"canvas '{canvas_id}' has invalid content") from exc
        nodes = doc.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError(f"canvas '{canvas_id}' has invalid content")
        _snapshot_canvas_in_session(
            s, canvas, canvas.doc, canvas.version, author_id=uid,
            label="before Workspace dataset add")
        _workspace_place_sources(nodes, sources)
        canvas.version += 1
        doc["version"] = canvas.version
        canvas.doc = json.dumps(doc)
        sync_local_result_owner(s, "canvas", canvas_id, doc)
        _replace_promoted_transform_refs(s, "canvas", canvas_id, doc)
        return {"ok": True, "id": canvas_id, "version": canvas.version, "doc": doc}


def workspace_add_transform_action(
        *, uid: str, canvas_id: str, expected_canvas_version: int,
        transform: dict, replace_node_id: str | None = None) -> dict:
    """Atomically add or explicitly upgrade one exact library Transform reference."""
    with _workspace_write_session() as s:
        canvas = s.get(Canvas, str(canvas_id), with_for_update=True)
        if canvas is None:
            raise KeyError(f"canvas '{canvas_id}' not found")
        if _workspace_canvas_role_in_session(s, canvas, str(uid)) not in ("owner", "editor"):
            raise PermissionError("you don't have edit access to this canvas")
        if canvas.version != expected_canvas_version:
            raise WorkspaceVersionConflict(
                f"canvas '{canvas_id}' changed from expected version {expected_canvas_version}")
        try:
            doc = json.loads(canvas.doc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"canvas '{canvas_id}' has invalid content") from exc
        nodes = doc.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError(f"canvas '{canvas_id}' has invalid content")
        candidate = _workspace_transform_node_in_session(
            s, uid=uid, transform=transform, canvas_id=str(canvas_id))
        label = "before Workspace Transform add"
        node_id = candidate["id"]
        if replace_node_id is None:
            _workspace_place_sources(nodes, [candidate])
        else:
            existing = next((node for node in nodes
                             if isinstance(node, dict)
                             and str(node.get("id")) == str(replace_node_id)), None)
            data = existing.get("data") if isinstance(existing, dict) else None
            cfg = data.get("config") if isinstance(data, dict) else None
            if (not isinstance(existing, dict) or existing.get("type") != "transform"
                    or not isinstance(cfg, dict) or cfg.get("source") != "library"):
                raise ValueError("the selected node is not a library Transform")
            if cfg.get("processor") != candidate["data"]["config"]["processor"]:
                raise ValueError("a Transform upgrade must keep the same identity")
            if cfg.get("version") == candidate["data"]["config"]["version"]:
                raise ValueError("the selected node already uses this exact Transform version")
            processor = str(cfg.get("processor", ""))
            if not processor.startswith("tr_"):
                raise WorkspaceTransformCompatibilityConflict(
                    "plugin Transform upgrades require compatible metadata for both exact versions")
            old_number = _promoted_transform_version_number(cfg.get("version"))
            new_number = _promoted_transform_version_number(
                candidate["data"]["config"]["version"])
            old_row = s.get(PromotedTransformVersion, (processor, old_number)) if old_number else None
            new_row = s.get(PromotedTransformVersion, (processor, new_number)) if new_number else None
            if (old_row is None or old_row.deleted_at is not None
                    or new_row is None or new_row.deleted_at is not None):
                raise WorkspaceTransformCompatibilityConflict(
                    "both exact Transform versions must be active before upgrade")
            input_compatibility = diff_columns(
                json.loads(old_row.input_schema), json.loads(new_row.input_schema))
            output_compatibility = diff_columns(
                json.loads(old_row.output_schema), json.loads(new_row.output_schema))
            if (input_compatibility.status != "compatible"
                    or output_compatibility.status != "compatible"):
                raise WorkspaceTransformCompatibilityConflict(
                    "Transform upgrade requires compatible input and output schemas; "
                    f"input is {input_compatibility.status}, output is {output_compatibility.status}")
            cfg.update(candidate["data"]["config"])
            cfg.pop("code", None)
            data["title"] = candidate["data"]["title"]
            node_id = str(replace_node_id)
            _workspace_invalidate_downstream(doc, node_id)
            label = "before exact Transform version upgrade"
        _snapshot_canvas_in_session(
            s, canvas, canvas.doc, canvas.version, author_id=uid, label=label)
        canvas.version += 1
        doc["version"] = canvas.version
        canvas.doc = json.dumps(doc)
        sync_local_result_owner(s, "canvas", str(canvas_id), doc)
        _replace_promoted_transform_refs(s, "canvas", str(canvas_id), doc)
        return {
            "ok": True, "id": str(canvas_id), "version": canvas.version,
            "nodeId": node_id, "doc": doc,
        }


def workspace_move_canvas_action(*, uid: str, placement_id: str, expected_version: int,
                                 container_id: str, expected_container_version: int) -> dict:
    """CAS-move only a canvas's local placement and return enough facts for a truthful undo."""
    with _workspace_write_session() as s:
        placement = _workspace_placement_locked(s, placement_id)
        if placement.target_kind != "canvas":
            raise ValueError("only canvas placements can be moved by this action")
        canvas = s.get(Canvas, placement.target_id, with_for_update=True)
        if canvas is None:
            raise KeyError(f"canvas '{placement.target_id}' not found")
        if _workspace_canvas_role_in_session(s, canvas, uid) not in ("owner", "editor"):
            raise PermissionError("you don't have edit access to this canvas")
        if placement.version != expected_version:
            _workspace_version_conflict("placement", placement_id, expected_version)
        destination = _workspace_container_at_version(
            s, container_id, expected_container_version)
        previous = _workspace_container_locked(s, placement.container_id)
        if previous.id == destination.id:
            raise ValueError("canvas is already in that Workspace container")
        changed = s.execute(update(WorkspacePlacement).where(
            WorkspacePlacement.id == placement_id,
            WorkspacePlacement.version == expected_version,
        ).values(
            container_id=destination.id,
            version=WorkspacePlacement.version + 1,
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            _workspace_version_conflict("placement", placement_id, expected_version)
        s.refresh(placement)
        return {
            "ok": True,
            "resource": _workspace_placement_resource(placement, detached=False),
            "previousContainer": _workspace_container_resource(previous),
            "container": _workspace_container_resource(destination),
        }


def workspace_delete_placement(placement_id: str, *, expected_version: int) -> None:
    """CAS-delete a canvas placement without deleting or rewriting its target."""
    with _workspace_write_session() as s:
        row = _workspace_placement_locked(s, placement_id)
        if row.version != expected_version:
            _workspace_version_conflict("placement", placement_id, expected_version)
        if row.target_kind == "dataset":
            raise ValueError("built-in datasets are placed by the Catalog")
        if row.target_kind != "canvas":
            raise ValueError(
                "immutable DatasetView placements are managed by their DatasetView")
        removed = s.execute(delete(WorkspacePlacement).where(
            WorkspacePlacement.id == placement_id,
            WorkspacePlacement.version == expected_version,
        ).execution_options(synchronize_session=False))
        if removed.rowcount != 1:
            _workspace_version_conflict("placement", placement_id, expected_version)


def _dataset_view_row_doc(row: DatasetView) -> dict:
    try:
        definition = json.loads(row.definition_doc)
    except (TypeError, ValueError) as exc:  # pragma: no cover - committed rows are server-authored
        raise RuntimeError("DatasetView definition is corrupt") from exc
    if not isinstance(definition, dict):  # pragma: no cover - committed rows are server-authored
        raise RuntimeError("DatasetView definition is corrupt")
    return {
        "definition": definition,
        "deleted": row.deleted_at is not None,
        "deletedAt": row.deleted_at,
    }


def dataset_view_submission(uid: str, submission_id: str) -> dict | None:
    """Read one owner-scoped idempotency record, including its terminal tombstone."""
    with session() as s:
        row = s.scalar(select(DatasetView).where(
            DatasetView.owner_id == uid,
            DatasetView.submission_id == str(submission_id),
        ).limit(1))
        if row is None:
            return None
        result = _dataset_view_row_doc(row)
        result["requestSha256"] = row.request_sha256
        return result


def dataset_view_get(uid: str, view_id: str) -> dict | None:
    """Read one DatasetView without revealing another owner's identity or tombstone."""
    with session() as s:
        row = s.get(DatasetView, str(view_id))
        if row is None or row.owner_id != uid:
            return None
        return _dataset_view_row_doc(row)


def _dataset_view_source_placement_in_session(s, dataset_id: str):
    managed = s.get(CatalogLogicalDataset, str(dataset_id))
    entry = (s.get(CatalogEntry, managed.current_uri)
             if managed is not None and managed.current_uri else None)
    if entry is None:
        entry = s.scalar(select(CatalogEntry).where(
            CatalogEntry.registration_id == str(dataset_id)).limit(1))
    if entry is None:
        raise KeyError("DatasetView source registration is unavailable")
    placement = s.scalar(select(WorkspacePlacement).where(
        WorkspacePlacement.target_kind == "dataset",
        WorkspacePlacement.target_id == entry.registration_id,
    ).limit(1).with_for_update())
    if placement is None:
        raise KeyError("DatasetView source Workspace placement is unavailable")
    return entry, placement


def dataset_view_source_workspace(dataset_id: str) -> dict:
    """Resolve the current Catalog-overlay container used for server-owned placement."""
    with session() as s:
        entry, placement = _dataset_view_source_placement_in_session(s, dataset_id)
        return {
            "sourceRegistrationId": entry.registration_id,
            "sourcePlacementId": placement.id,
            "containerId": placement.container_id,
            "ordinal": placement.ordinal,
        }


def dataset_view_create(
    *, uid: str, view_id: str, placement_id: str, submission_id: str,
    request_sha256: str, definition_sha256: str, definition_doc: str,
    source_dataset_id: str, source_registration_id: str, expected_container_id: str,
) -> tuple[dict, bool]:
    """Atomically persist definition, placement, replay fence, and core revision hold."""
    if len(definition_doc.encode("utf-8")) > 1_048_576:
        raise ValueError("DatasetView definition exceeds the persisted size limit")
    with _workspace_write_session() as s:
        entry, source = _dataset_view_source_placement_in_session(s, source_dataset_id)
        if (entry.registration_id != source_registration_id
                or source.container_id != expected_container_id):
            raise WorkspaceVersionConflict("DatasetView source placement changed; retry the request")
        _workspace_writable_destination_locked(s, source.container_id)

        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
            raise RuntimeError("unsupported metadata database dialect")
        s.execute(dialect_insert(DatasetView).values(
            id=view_id,
            owner_id=uid,
            submission_id=submission_id,
            request_sha256=request_sha256,
            definition_sha256=definition_sha256,
            definition_doc=definition_doc,
            created_at=_now(),
        ).on_conflict_do_nothing())
        row = s.scalar(select(DatasetView).where(
            DatasetView.owner_id == uid,
            DatasetView.submission_id == submission_id,
        ).limit(1).with_for_update())
        if row is None:  # pragma: no cover - only a random primary-key collision can reach this
            raise RuntimeError("DatasetView identity allocation collided")
        if row.id != view_id:
            if row.request_sha256 != request_sha256:
                raise DatasetViewSubmissionConflict(
                    "DatasetView submission id belongs to a different request")
            if row.deleted_at is not None:
                raise DatasetViewGone("DatasetView submission was deleted")
            return _dataset_view_row_doc(row)["definition"], False

        document = json.loads(definition_doc)
        placement = WorkspacePlacement(
            id=placement_id,
            container_id=source.container_id,
            target_kind="dataset_view",
            target_id=view_id,
            name=_workspace_name(document["name"]),
            ordinal=source.ordinal,
            version=1,
        )
        s.add(placement)
        s.flush()
        sync_local_result_owner(s, "dataset_view", view_id, definition_doc)
        return document, True


def dataset_view_delete(uid: str, view_id: str) -> bool | None:
    """Atomically tombstone a DatasetView and release its Workspace and revision holds."""
    with _workspace_write_session() as s:
        row = s.get(DatasetView, str(view_id), with_for_update=True)
        if row is None or row.owner_id != uid:
            return None
        if row.deleted_at is not None:
            return False
        s.execute(delete(WorkspacePlacement).where(
            WorkspacePlacement.target_kind == "dataset_view",
            WorkspacePlacement.target_id == row.id,
        ).execution_options(synchronize_session=False))
        _drop_local_result_owner(s, "dataset_view", row.id)
        row.deleted_at = _now()
        return True


_WORKSPACE_BROWSE_MAX_LIMIT = 100
_WORKSPACE_ANCESTOR_LIMIT = 32
_WORKSPACE_SEARCH_CURSOR_VERSION = 1


def _workspace_ref(kind: str, identity: str) -> str:
    return f"{kind}:{identity}"


def _workspace_cursor_encode(ordinal: int, rank: int, name: str, identity: str) -> str:
    raw = json.dumps([ordinal, rank, name, identity], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _workspace_cursor_decode(cursor: str | None) -> tuple[int, int, str, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ordinal, rank, name, identity = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Workspace cursor") from exc
    if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
            or ordinal < 0 or ordinal >= 2**63
            or isinstance(rank, bool) or not isinstance(rank, int) or rank not in (0, 1, 2, 3)
            or not isinstance(name, str) or not name
            or not isinstance(identity, str) or not identity):
        raise ValueError("invalid Workspace cursor")
    return ordinal, rank, name, identity


def _workspace_name_order(column):
    """Use the same Unicode code-point ordering in SQLite, PostgreSQL, and Python."""
    return column.collate("BINARY" if _is_sqlite_database() else "C")


def _workspace_search_cursor_encode(query: str, name: str, rank: int, identity: str) -> str:
    raw = json.dumps(
        [_WORKSPACE_SEARCH_CURSOR_VERSION, query, name, rank, identity],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _workspace_search_cursor_decode(
    cursor: str | None, *, query: str,
) -> tuple[str, int, str] | None:
    if cursor is None:
        return None
    if len(cursor) > 4096:
        raise ValueError("invalid Workspace search cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        version, bound_query, name, rank, identity = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Workspace search cursor") from exc
    if (version != _WORKSPACE_SEARCH_CURSOR_VERSION or bound_query != query
            or not isinstance(name, str)
            or isinstance(rank, bool) or not isinstance(rank, int) or rank not in (0, 1, 2, 3)
            or not isinstance(identity, str) or not identity):
        raise ValueError("invalid Workspace search cursor")
    return name, rank, identity


def _workspace_search_after(name_column, id_column, rank: int, cursor):
    if cursor is None:
        return None
    name, cursor_rank, identity = cursor
    ordered_name = _workspace_name_order(func.lower(name_column))
    return or_(
        ordered_name > name,
        and_(ordered_name == name, or_(
            rank > cursor_rank,
            and_(rank == cursor_rank, id_column > identity),
        )),
    )


def _workspace_search_matches(name_column, tokens: list[str]):
    return and_(*(func.lower(name_column).contains(token, autoescape=True) for token in tokens))


def _workspace_after(ordinal_column, name_column, id_column, rank: int, cursor):
    if cursor is None:
        return None
    ordinal, cursor_rank, name, identity = cursor
    ordered_name = _workspace_name_order(name_column)
    return or_(
        ordinal_column > ordinal,
        and_(ordinal_column == ordinal, rank > cursor_rank),
        and_(ordinal_column == ordinal, rank == cursor_rank, or_(
            ordered_name > name,
            and_(ordered_name == name, id_column > identity),
        )),
    )


def _workspace_container_resource(row: WorkspaceContainer) -> dict:
    return {
        "id": _workspace_ref("container", row.id), "kind": "container", "name": row.name,
        "parentId": _workspace_ref("container", row.parent_id) if row.parent_id else None,
        "version": row.version, "detached": row.catalog_folder_state == "detached",
        "catalogFolderId": row.catalog_folder_id,
        "catalogFolderState": row.catalog_folder_state,
        "catalogFolderPath": row.catalog_folder_path,
    }


def _workspace_placement_resource(row: WorkspacePlacement, *, detached: bool) -> dict:
    return {
        "id": _workspace_ref(row.target_kind, row.target_id), "kind": row.target_kind,
        "name": row.name, "parentId": _workspace_ref("container", row.container_id),
        "placementId": row.id, "version": row.version, "detached": detached,
    }


_WORKSPACE_PROVIDER_STATES = {
    "current", "offline", "permission_lost", "detached", "provider_error",
}


def _workspace_provider_binding_doc(row: WorkspaceProviderBinding) -> dict:
    return {
        "bindingId": row.id,
        "mountId": row.mount_id,
        "provider": row.provider,
        "containerId": row.container_id,
        "resourceId": row.resource_id,
        "kind": row.kind,
        "name": row.name,
        "parentBindingId": row.parent_binding_id,
        "referenceState": row.state,
        "active": row.active,
        "lastError": row.last_error,
        "relinkedFromId": row.relinked_from_id,
        "lastResolvedAt": row.last_resolved_at,
    }


def _workspace_provider_initial_binding_id(
    mount_id: str, provider: str, resource_id: str,
) -> str:
    payload = json.dumps(
        [mount_id, provider, resource_id], separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def workspace_provider_cache_resource(
    *, mount_id: str, provider: str, container_id: str, resource_id: str, kind: str, name: str,
    parent_binding_id: str | None = None, parent_is_known: bool = True,
) -> dict:
    """Persist only the bounded display facts needed to explain a broken external reference.

    A terminal detached binding is never revived by a provider read.  This is the ABA fence that
    prevents delete/recreate with a reused provider ID from silently rebinding an old Workspace URL.
    """
    if kind not in {"container", "dataset"}:
        raise ValueError("invalid provider resource kind")
    with session() as s:
        row = s.scalar(select(WorkspaceProviderBinding).where(
            WorkspaceProviderBinding.mount_id == mount_id,
            WorkspaceProviderBinding.provider == provider,
            WorkspaceProviderBinding.resource_id == resource_id,
            WorkspaceProviderBinding.active.is_(True),
        ).order_by(WorkspaceProviderBinding.updated_at.desc()))
        if row is None:
            row = s.scalar(select(WorkspaceProviderBinding).where(
                WorkspaceProviderBinding.mount_id == mount_id,
                WorkspaceProviderBinding.provider == provider,
                WorkspaceProviderBinding.resource_id == resource_id,
            ).order_by(WorkspaceProviderBinding.updated_at.desc()))
        if row is None:
            row = WorkspaceProviderBinding(
                id=_workspace_provider_initial_binding_id(mount_id, provider, resource_id),
                mount_id=mount_id,
                provider=provider,
                container_id=container_id,
                resource_id=resource_id,
                kind=kind,
                name=name,
                parent_binding_id=parent_binding_id,
                state="current",
                active=True,
                last_resolved_at=_now(),
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                # Concurrent request-time reads mint the same deterministic first binding.  Adopt
                # the winner rather than failing a healthy provider read.
                s.rollback()
                row = s.get(WorkspaceProviderBinding, _workspace_provider_initial_binding_id(
                    mount_id, provider, resource_id))
                if row is None:
                    raise
        elif row.state != "detached":
            row.kind = kind
            row.name = name
            row.container_id = container_id
            if parent_is_known:
                row.parent_binding_id = parent_binding_id
            row.state = "current"
            row.last_error = None
            row.last_resolved_at = _now()
        return _workspace_provider_binding_doc(row)


def workspace_provider_binding(
    binding_id: str, *, mount_id: str | None = None, provider: str | None = None,
    resource_id: str | None = None,
) -> dict | None:
    with session() as s:
        row = s.get(WorkspaceProviderBinding, binding_id)
        if row is None:
            return None
        if (mount_id is not None and row.mount_id != mount_id
                or provider is not None and row.provider != provider
                or resource_id is not None and row.resource_id != resource_id):
            return None
        return _workspace_provider_binding_doc(row)


def workspace_provider_binding_for_resource(
    *, mount_id: str, provider: str, resource_id: str,
) -> dict | None:
    with session() as s:
        row = s.scalar(select(WorkspaceProviderBinding).where(
            WorkspaceProviderBinding.mount_id == mount_id,
            WorkspaceProviderBinding.provider == provider,
            WorkspaceProviderBinding.resource_id == resource_id,
        ).order_by(
            WorkspaceProviderBinding.active.desc(),
            WorkspaceProviderBinding.updated_at.desc(),
        ))
        return _workspace_provider_binding_doc(row) if row is not None else None


def workspace_provider_binding_ancestors(binding_id: str) -> list[dict]:
    with session() as s:
        row = s.get(WorkspaceProviderBinding, binding_id)
        if row is None:
            return []
        ancestors: list[dict] = []
        seen = {row.id}
        current_id = row.parent_binding_id
        while current_id is not None:
            if current_id in seen or len(ancestors) >= _WORKSPACE_ANCESTOR_LIMIT:
                break
            seen.add(current_id)
            current = s.get(WorkspaceProviderBinding, current_id)
            if current is None:
                break
            ancestors.append(_workspace_provider_binding_doc(current))
            current_id = current.parent_binding_id
        return list(reversed(ancestors))


def workspace_provider_mark_binding(binding_id: str, *, state: str, error: str | None) -> dict:
    if state not in _WORKSPACE_PROVIDER_STATES or state == "current":
        raise ValueError("invalid degraded provider reference state")
    del error
    # Provider reasons are useful on the live response but are not trusted persistence input: they
    # may contain a URI, object name, or credential-shaped diagnostic.  Persist only a fixed safe
    # explanation of the classified state.
    safe_error = {
        "offline": "provider is offline",
        "permission_lost": "provider permission was lost",
        "detached": "resource is detached",
        "provider_error": "provider read failed",
    }[state]
    with session() as s:
        row = s.get(WorkspaceProviderBinding, binding_id, with_for_update=True)
        if row is None:
            raise KeyError("Workspace provider binding not found")
        if row.state != "detached":
            row.state = state
            row.last_error = safe_error
        return _workspace_provider_binding_doc(row)


def workspace_provider_relink_binding(
    old_binding_id: str, *, mount_id: str, provider: str, container_id: str, resource_id: str,
    kind: str, name: str, parent_binding_id: str | None,
) -> tuple[dict, dict]:
    """Mint a new binding and retain the old binding as an auditable detached reference."""
    if kind not in {"container", "dataset"}:
        raise ValueError("invalid provider resource kind")
    with session() as s:
        old = s.get(WorkspaceProviderBinding, old_binding_id, with_for_update=True)
        if old is None:
            raise KeyError("Workspace provider binding not found")
        if not old.active:
            raise ValueError("Workspace provider binding was already relinked")
        if old.kind != kind:
            raise ValueError("replacement resource kind does not match the detached reference")
        old.active = False
        old.state = "detached"
        old.last_error = "relinked explicitly"
        fresh = WorkspaceProviderBinding(
            id=uuid.uuid4().hex,
            mount_id=mount_id,
            provider=provider,
            container_id=container_id,
            resource_id=resource_id,
            kind=kind,
            name=name,
            parent_binding_id=parent_binding_id,
            state="current",
            active=True,
            relinked_from_id=old.id,
            last_resolved_at=_now(),
        )
        s.add(fresh)
        s.flush()
        return _workspace_provider_binding_doc(old), _workspace_provider_binding_doc(fresh)


def _workspace_canvas_visible_clause(uid: str):
    return exists(select(Canvas.id).where(
        Canvas.id == WorkspacePlacement.target_id,
        or_(
            Canvas.owner_id == uid,
            Canvas.visibility.in_(("workspace", "workspace_view")),
            exists(select(CanvasShare.id).where(
                CanvasShare.canvas_id == Canvas.id, CanvasShare.user_id == uid)),
        ),
    ))


def _workspace_canvas_exists_clause():
    return exists(select(Canvas.id).where(Canvas.id == WorkspacePlacement.target_id))


def _workspace_dataset_view_visible_clause(uid: str):
    return exists(select(DatasetView.id).where(
        DatasetView.id == WorkspacePlacement.target_id,
        DatasetView.owner_id == uid,
        DatasetView.deleted_at.is_(None),
    ))


def workspace_browse(container_id: str, *, uid: str, limit: int = 50,
                     cursor: str | None = None) -> dict:
    """Read one bounded mixed local Workspace page without calling a catalog provider."""
    limit = max(1, min(int(limit), _WORKSPACE_BROWSE_MAX_LIMIT))
    decoded = _workspace_cursor_decode(cursor)
    with session() as s:
        container = s.get(WorkspaceContainer, container_id)
        if container is None:
            raise KeyError(f"workspace container '{container_id}' not found")
        container_after = _workspace_after(
            WorkspaceContainer.ordinal, WorkspaceContainer.name, WorkspaceContainer.id, 0, decoded)
        containers = list(s.scalars(select(WorkspaceContainer).where(
            WorkspaceContainer.parent_id == container_id,
            *([container_after] if container_after is not None else []),
        ).order_by(
            WorkspaceContainer.ordinal,
            _workspace_name_order(WorkspaceContainer.name),
            WorkspaceContainer.id,
        )
          .limit(limit + 1)))
        visible_or_missing_canvas = or_(
            ~_workspace_canvas_exists_clause(), _workspace_canvas_visible_clause(uid))
        canvas_after = _workspace_after(
            WorkspacePlacement.ordinal, WorkspacePlacement.name, WorkspacePlacement.id, 1, decoded)
        dataset_after = _workspace_after(
            WorkspacePlacement.ordinal, WorkspacePlacement.name, WorkspacePlacement.id, 2, decoded)
        view_after = _workspace_after(
            WorkspacePlacement.ordinal, WorkspacePlacement.name, WorkspacePlacement.id, 3, decoded)
        canvas_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.container_id == container_id,
            WorkspacePlacement.target_kind == "canvas", visible_or_missing_canvas,
            *([canvas_after] if canvas_after is not None else []),
        ).order_by(
            WorkspacePlacement.ordinal,
            _workspace_name_order(WorkspacePlacement.name),
            WorkspacePlacement.id,
        ).limit(limit + 1)))
        dataset_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.container_id == container_id,
            WorkspacePlacement.target_kind == "dataset",
            *([dataset_after] if dataset_after is not None else []),
        ).order_by(
            WorkspacePlacement.ordinal,
            _workspace_name_order(WorkspacePlacement.name),
            WorkspacePlacement.id,
        ).limit(limit + 1)))
        view_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.container_id == container_id,
            WorkspacePlacement.target_kind == "dataset_view",
            _workspace_dataset_view_visible_clause(uid),
            *([view_after] if view_after is not None else []),
        ).order_by(
            WorkspacePlacement.ordinal,
            _workspace_name_order(WorkspacePlacement.name),
            WorkspacePlacement.id,
        ).limit(limit + 1)))
        placements = [*canvas_placements, *dataset_placements, *view_placements]
        dataset_ids = [row.target_id for row in placements if row.target_kind == "dataset"]
        canvas_ids = [row.target_id for row in placements if row.target_kind == "canvas"]
        live_datasets = set(s.scalars(select(CatalogEntry.registration_id).where(
            CatalogEntry.registration_id.in_(dataset_ids)))) if dataset_ids else set()
        live_canvases = set(s.scalars(select(Canvas.id).where(Canvas.id.in_(canvas_ids)))) if canvas_ids else set()

        rows: list[tuple[tuple, dict]] = []
        for row in containers:
            rows.append(((row.ordinal, 0, row.name, row.id), _workspace_container_resource(row)))
        for row in placements:
            rank = {"canvas": 1, "dataset": 2, "dataset_view": 3}[row.target_kind]
            live = (row.target_id in live_canvases if row.target_kind == "canvas" else
                    row.target_id in live_datasets if row.target_kind == "dataset" else True)
            rows.append(((row.ordinal, rank, row.name, row.id),
                         _workspace_placement_resource(row, detached=not live)))
        rows.sort(key=lambda row: row[0])
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = (_workspace_cursor_encode(*page[-1][0]) if has_more and page else None)
        return {
            "container": _workspace_container_resource(container),
            "items": [item for _key, item in page], "nextCursor": next_cursor,
            "hasMore": has_more, "completeness": "page" if has_more else "complete",
        }


def workspace_search(query: str, *, uid: str, limit: int = 25,
                     cursor: str | None = None) -> dict:
    """Search current local Workspace metadata without materializing the complete catalog."""
    normalized = " ".join(query.split()).lower()
    if not normalized:
        raise ValueError("Workspace search query must not be blank")
    if len(normalized.encode("utf-8")) > 512:
        raise ValueError("Workspace search query must be at most 512 UTF-8 bytes")
    limit = max(1, min(int(limit), _WORKSPACE_BROWSE_MAX_LIMIT))
    tokens = normalized.split()
    decoded = _workspace_search_cursor_decode(cursor, query=normalized)
    with session() as s:
        container_after = _workspace_search_after(
            WorkspaceContainer.name, WorkspaceContainer.id, 0, decoded)
        containers = list(s.scalars(select(WorkspaceContainer).where(
            _workspace_search_matches(WorkspaceContainer.name, tokens),
            *([container_after] if container_after is not None else []),
        ).order_by(
            _workspace_name_order(func.lower(WorkspaceContainer.name)),
            WorkspaceContainer.id,
        ).limit(limit + 1)))

        canvas_after = _workspace_search_after(
            WorkspacePlacement.name, WorkspacePlacement.target_id, 1, decoded)
        dataset_after = _workspace_search_after(
            WorkspacePlacement.name, WorkspacePlacement.target_id, 2, decoded)
        view_after = _workspace_search_after(
            WorkspacePlacement.name, WorkspacePlacement.target_id, 3, decoded)
        canvas_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.target_kind == "canvas",
            _workspace_canvas_visible_clause(uid),
            _workspace_search_matches(WorkspacePlacement.name, tokens),
            *([canvas_after] if canvas_after is not None else []),
        ).order_by(
            _workspace_name_order(func.lower(WorkspacePlacement.name)),
            WorkspacePlacement.target_id,
        ).limit(limit + 1)))
        dataset_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.target_kind == "dataset",
            _workspace_search_matches(WorkspacePlacement.name, tokens),
            *([dataset_after] if dataset_after is not None else []),
        ).order_by(
            _workspace_name_order(func.lower(WorkspacePlacement.name)),
            WorkspacePlacement.target_id,
        ).limit(limit + 1)))
        view_placements = list(s.scalars(select(WorkspacePlacement).where(
            WorkspacePlacement.target_kind == "dataset_view",
            _workspace_dataset_view_visible_clause(uid),
            _workspace_search_matches(WorkspacePlacement.name, tokens),
            *([view_after] if view_after is not None else []),
        ).order_by(
            _workspace_name_order(func.lower(WorkspacePlacement.name)),
            WorkspacePlacement.target_id,
        ).limit(limit + 1)))

        dataset_ids = [row.target_id for row in dataset_placements]
        live_datasets = set(s.scalars(select(CatalogEntry.registration_id).where(
            CatalogEntry.registration_id.in_(dataset_ids)))) if dataset_ids else set()
        rows: list[tuple[tuple[str, int, str], dict]] = []
        rows.extend(
            ((row.name.lower(), 0, row.id), _workspace_container_resource(row))
            for row in containers
        )
        rows.extend(
            ((row.name.lower(), 1, row.target_id),
             _workspace_placement_resource(row, detached=False))
            for row in canvas_placements
        )
        rows.extend(
            ((row.name.lower(), 2, row.target_id),
             _workspace_placement_resource(row, detached=row.target_id not in live_datasets))
            for row in dataset_placements
        )
        rows.extend(
            ((row.name.lower(), 3, row.target_id),
             _workspace_placement_resource(row, detached=False))
            for row in view_placements
        )
        rows.sort(key=lambda row: row[0])
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = (
            _workspace_search_cursor_encode(normalized, *page[-1][0])
            if has_more and page else None
        )
        return {
            "items": [item for _key, item in page],
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }


def _workspace_ancestors(s, container_id: str) -> list[dict]:
    ancestors: list[dict] = []
    current_id: str | None = container_id
    while current_id is not None:
        if len(ancestors) >= _WORKSPACE_ANCESTOR_LIMIT:
            raise RuntimeError("Workspace ancestor chain exceeds the supported limit")
        row = s.get(WorkspaceContainer, current_id)
        if row is None:
            raise RuntimeError("Workspace placement parent is missing")
        ancestors.append(_workspace_container_resource(row))
        current_id = row.parent_id
    return list(reversed(ancestors))


def workspace_resolve(resource_id: str, *, uid: str) -> dict:
    """Resolve a stable local Workspace reference and its bounded display ancestors."""
    try:
        kind, identity = resource_id.split(":", 1)
    except ValueError as exc:
        raise KeyError("invalid Workspace resource reference") from exc
    if kind not in {"container", "canvas", "dataset", "dataset_view"} or not identity:
        raise KeyError("invalid Workspace resource reference")
    with session() as s:
        if kind == "container":
            row = s.get(WorkspaceContainer, identity)
            if row is None:
                raise KeyError(f"Workspace resource '{resource_id}' not found")
            return {"resource": _workspace_container_resource(row),
                    "ancestors": _workspace_ancestors(s, row.parent_id) if row.parent_id else []}
        placement = s.scalar(select(WorkspacePlacement).where(
            WorkspacePlacement.target_kind == kind, WorkspacePlacement.target_id == identity))
        if placement is None:
            raise KeyError(f"Workspace resource '{resource_id}' not found")
        if kind == "canvas" and s.get(Canvas, identity) is not None and canvas_role(identity, uid) is None:
            raise KeyError(f"Workspace resource '{resource_id}' not found")
        if kind == "dataset_view":
            view = s.get(DatasetView, identity)
            if view is None or view.owner_id != uid or view.deleted_at is not None:
                raise KeyError(f"Workspace resource '{resource_id}' not found")
            live = True
        else:
            live = (s.get(Canvas, identity) is not None if kind == "canvas" else
                    s.scalar(select(CatalogEntry.uri).where(
                        CatalogEntry.registration_id == identity)) is not None)
        return {"resource": _workspace_placement_resource(placement, detached=not live),
                "ancestors": _workspace_ancestors(s, placement.container_id)}


_RUN_HISTORY_MAX = 500  # per-canvas run_records cap — bound the local DB (older history is pruned)
_RUN_RECORD_OUTPUTS_MAX_BYTES = 1_048_576
_RUN_INPUT_MANIFEST_MAX_BYTES = 262_144
_DURABLE_TASK_DOC_MAX_BYTES = 4 * 1_048_576
_DURABLE_TASK_LEASE_SECONDS = 15
_CHECKPOINT_PARENT_KINDS = frozenset({"linear_checkpoint_write", "bounded_fanout_write"})
# #423: bounded_fanout_write is Jobs-visible via sanitized parent-only projection. The hidden
# distribution-report lifecycle has no Jobs projection until its own product surface exists.
_JOBS_HIDDEN_TASK_KINDS = frozenset({"distribution_report"})
_INBOX_PRODUCER_KINDS = frozenset({
    "managed_local_write", "external_wait", "linear_checkpoint_write",
    "bounded_fanout_write", "distribution_report",
})
_INBOX_HIDDEN_TASK_KINDS = frozenset({"distribution_report"})
_INBOX_TASK_STATUS_TO_OUTCOME = {
    "done": "completed", "failed": "failed", "cancelled": "cancelled",
}
_INBOX_DIAGNOSTIC_ALLOWLIST = {
    "managed_local_write": frozenset({
        "durable_task_attempts_exhausted",
    }),
    "external_wait": frozenset({
        "external_wait_deadline",
        "external_wait_poll_budget",
        "external_wait_evidence_invalid",
        "adapter_return_invalid",
        "phase_regressed",
        "checkpoint_regressed",
        "provider_failed",
        "adapter_unavailable",
        "adapter_transition_timeout",
        "adapter_transient_failure",
        "external_wait_stage_busy",
        "external_wait_download_invalid",
        "external_wait_download_failed",
        "external_wait_destination_stale",
        "external_wait_publication_invalid",
        "external_wait_publication_failed",
    }),
    "linear_checkpoint_write": frozenset({
        "durable_task_attempts_exhausted",
        "checkpoint_invalid",
    }),
    "bounded_fanout_write": frozenset({
        "durable_task_attempts_exhausted",
        "checkpoint_invalid",
    }),
    "distribution_report": frozenset({
        "durable_task_attempts_exhausted",
        "distribution_report_snapshot_invalid",
        "distribution_report_computation_failed",
        "distribution_report_revision_unavailable",
        "distribution_report_deadline",
    }),
}
_INBOX_DIAGNOSTIC_FALLBACK = {
    "managed_local_write": "managed_local_write_failed",
    "external_wait": "external_wait_failed",
    "linear_checkpoint_write": "linear_checkpoint_write_failed",
    "bounded_fanout_write": "bounded_fanout_write_failed",
    "distribution_report": "distribution_report_failed",
}


def _canonical_inbox_diagnostic(
        task_kind: str, diagnostic_code: str | None, outcome: str) -> str | None:
    """Map a caller diagnostic onto the bounded per-kind allowlist; never invent codes from text."""
    if outcome == "completed" or diagnostic_code is None:
        return None
    code = str(diagnostic_code)
    allowed = _INBOX_DIAGNOSTIC_ALLOWLIST.get(task_kind, frozenset())
    if code in allowed:
        return code
    return _INBOX_DIAGNOSTIC_FALLBACK.get(task_kind)


_INBOX_CURSOR_VERSION = 1


def _inbox_stamp(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    return (value.replace(tzinfo=datetime.timezone.utc)
            if value.tzinfo is None else value.astimezone(datetime.timezone.utc))


def _durable_task_inbox_doc(item: DurableTaskInboxItem) -> dict:
    return {
        "id": item.id,
        "owner_id": item.owner_id,
        "task_id": item.task_id,
        "task_attempt_id": item.task_attempt_id,
        "canvas_id": item.canvas_id,
        "dataset_view_id": item.dataset_view_id,
        "task_kind": item.task_kind,
        "execution_manifest_sha256": item.execution_manifest_sha256,
        "execution_manifest_reconstructable": item.execution_manifest_sha256 is not None,
        "outcome": item.outcome,
        "diagnostic_code": item.diagnostic_code,
        "terminal_at": _inbox_stamp(item.terminal_at),
        "created_at": _inbox_stamp(item.created_at),
        "read_at": _inbox_stamp(item.read_at),
    }


def _inbox_cursor_encode(filter_name: str, terminal_at: datetime.datetime, item_id: str) -> str:
    stamp = _inbox_stamp(terminal_at)
    assert stamp is not None
    raw = json.dumps(
        {"v": _INBOX_CURSOR_VERSION, "f": filter_name, "t": stamp.isoformat(), "i": item_id},
        separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _inbox_cursor_decode(
        cursor: str | None, expected_filter: str) -> tuple[datetime.datetime, str] | None:
    if cursor is None:
        return None
    if len(cursor) > 4096:
        raise ValueError("invalid Inbox cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid Inbox cursor")
        if value.get("v") != _INBOX_CURSOR_VERSION:
            raise ValueError("invalid Inbox cursor")
        if value.get("f") != expected_filter:
            raise ValueError("invalid Inbox cursor")
        terminal_at = datetime.datetime.fromisoformat(value["t"])
        if terminal_at.tzinfo is None:
            terminal_at = terminal_at.replace(tzinfo=datetime.timezone.utc)
        item_id = value["i"]
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("invalid Inbox cursor") from exc
    if (not isinstance(item_id, str) or not item_id
            or terminal_at.tzinfo is None or terminal_at.utcoffset() is None):
        raise ValueError("invalid Inbox cursor")
    return terminal_at, item_id


def _inbox_authorized_canvas_names(s, uid: str, canvas_ids: set[str]) -> dict[str, str]:
    """Map currently authorized canvas ids to display names for the Inbox viewer."""
    if not canvas_ids:
        return {}
    rows = list(s.execute(select(Canvas, CanvasShare.role).outerjoin(
        CanvasShare,
        and_(CanvasShare.canvas_id == Canvas.id, CanvasShare.user_id == uid),
    ).where(Canvas.id.in_(canvas_ids))).all())
    authorized: dict[str, str] = {}
    for canvas, share_role in rows:
        if _effective_canvas_role(canvas, uid, share_role) is not None:
            authorized[canvas.id] = canvas.name
    return authorized


def _inbox_public_doc(
        item: DurableTaskInboxItem, *, authorized_names: dict[str, str]) -> dict:
    """Owner-scoped Inbox wire shape for #417 — no raw docs, tokens, or route URLs."""
    canvas_name = authorized_names.get(item.canvas_id) if item.canvas_id is not None else None
    return {
        "id": item.id,
        "task_id": item.task_id,
        "canvas_id": item.canvas_id,
        "canvas_name": canvas_name,
        "task_kind": item.task_kind,
        "execution_manifest_sha256": item.execution_manifest_sha256,
        "execution_manifest_reconstructable": item.execution_manifest_sha256 is not None,
        "outcome": item.outcome,
        "diagnostic_code": item.diagnostic_code,
        "terminal_at": _inbox_stamp(item.terminal_at),
        "read_at": _inbox_stamp(item.read_at),
        "job_available": (
            canvas_name is not None and item.task_kind not in _JOBS_HIDDEN_TASK_KINDS),
    }


def _emit_durable_task_inbox_item(
        s, *, task: DurableTask, attempt: DurableTaskAttempt, task_status: str,
        diagnostic_code: str | None = None,
        now: datetime.datetime | None = None) -> dict | None:
    """Insert exactly one owner-scoped Inbox item for a terminal certified attempt.

    Must run inside the same SQL transaction that terminalizes Task/Attempt. Idempotent on
    ``(task_id, task_attempt_id)``; a later different outcome for the same attempt is rejected.
    """
    if task.task_kind not in _INBOX_PRODUCER_KINDS:
        return None
    outcome = _INBOX_TASK_STATUS_TO_OUTCOME.get(task_status)
    if outcome is None:
        raise ValueError("inbox emission requires a terminal durable task status")
    if attempt.task_id != task.id:
        raise ValueError("inbox attempt does not belong to the durable task")
    existing = s.scalar(select(DurableTaskInboxItem).where(
        DurableTaskInboxItem.task_id == task.id,
        DurableTaskInboxItem.task_attempt_id == attempt.id,
    ).limit(1))
    if existing is not None:
        if (existing.owner_id != task.owner_id or existing.task_kind != task.task_kind
                or existing.outcome != outcome or existing.canvas_id != task.canvas_id
                or existing.dataset_view_id != task.dataset_view_id
                or existing.execution_manifest_sha256 != task.execution_manifest_sha256):
            raise ValueError("later inbox outcome for the same attempt is rejected")
        return _durable_task_inbox_doc(existing)
    stamp = now or _durable_task_db_now(s)
    code = _canonical_inbox_diagnostic(task.task_kind, diagnostic_code, outcome)
    item_id = uuid.uuid4().hex
    values = {
        "id": item_id, "owner_id": task.owner_id, "task_id": task.id,
        "task_attempt_id": attempt.id, "canvas_id": task.canvas_id,
        "dataset_view_id": task.dataset_view_id,
        "task_kind": task.task_kind,
        "execution_manifest_sha256": task.execution_manifest_sha256,
        "outcome": outcome, "diagnostic_code": code,
        "terminal_at": stamp, "created_at": stamp, "read_at": None,
    }
    if s.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    s.execute(dialect_insert(DurableTaskInboxItem).values(**values).on_conflict_do_nothing(
        index_elements=["task_id", "task_attempt_id"]))
    row = s.scalar(select(DurableTaskInboxItem).where(
        DurableTaskInboxItem.task_id == task.id,
        DurableTaskInboxItem.task_attempt_id == attempt.id,
    ).limit(1))
    if row is None:
        raise RuntimeError("durable task inbox emission failed to persist")
    if (row.owner_id != task.owner_id or row.task_kind != task.task_kind
            or row.outcome != outcome or row.canvas_id != task.canvas_id
            or row.dataset_view_id != task.dataset_view_id
            or row.execution_manifest_sha256 != task.execution_manifest_sha256):
        raise ValueError("later inbox outcome for the same attempt is rejected")
    return _durable_task_inbox_doc(row)


def list_durable_task_inbox_items(
        owner_id: str, *, limit: int = 50, cursor: str | None = None,
        unread_only: bool = False) -> dict:
    """Bounded owner-scoped Inbox page. Owner predicate and keyset live in one SQL query."""
    owner_id = str(owner_id)
    limit = max(1, min(int(limit), 200))
    filter_name = "unread" if unread_only else "all"
    decoded = _inbox_cursor_decode(cursor, filter_name)
    with session() as s:
        predicates = [
            DurableTaskInboxItem.owner_id == owner_id,
            DurableTaskInboxItem.task_kind.notin_(_INBOX_HIDDEN_TASK_KINDS),
        ]
        if unread_only:
            predicates.append(DurableTaskInboxItem.read_at.is_(None))
        if decoded is not None:
            stamp, before_id = decoded
            predicates.append(or_(
                DurableTaskInboxItem.terminal_at < stamp,
                and_(DurableTaskInboxItem.terminal_at == stamp,
                     DurableTaskInboxItem.id < str(before_id)),
            ))
        rows = list(s.scalars(select(DurableTaskInboxItem).where(*predicates).order_by(
            DurableTaskInboxItem.terminal_at.desc(), DurableTaskInboxItem.id.desc(),
        ).limit(limit + 1)))
        page = rows[:limit]
        authorized = _inbox_authorized_canvas_names(
            s, owner_id, {row.canvas_id for row in page if row.canvas_id is not None})
        items = [_inbox_public_doc(row, authorized_names=authorized) for row in page]
        has_more = len(rows) > limit
        next_cursor = (
            _inbox_cursor_encode(filter_name, page[-1].terminal_at, page[-1].id)
            if has_more and page else None)
        return {"items": items, "has_more": has_more, "next_cursor": next_cursor}


def count_durable_task_inbox_unread(owner_id: str) -> int:
    with session() as s:
        return int(s.scalar(select(func.count()).select_from(DurableTaskInboxItem).where(
            DurableTaskInboxItem.owner_id == str(owner_id),
            DurableTaskInboxItem.task_kind.notin_(_INBOX_HIDDEN_TASK_KINDS),
            DurableTaskInboxItem.read_at.is_(None),
        )) or 0)


def mark_durable_task_inbox_item_read(owner_id: str, item_id: str) -> dict | None:
    """Idempotent owner-scoped mark-read using DB time; replay returns the original timestamp."""
    owner_id, item_id = str(owner_id), str(item_id)
    with session() as s:
        item = s.get(DurableTaskInboxItem, item_id, with_for_update=True)
        if (item is None or item.owner_id != owner_id
                or item.task_kind in _INBOX_HIDDEN_TASK_KINDS):
            return None
        if item.read_at is None:
            item.read_at = _durable_task_db_now(s)
        authorized = _inbox_authorized_canvas_names(
            s, owner_id, {item.canvas_id} if item.canvas_id is not None else set())
        return _inbox_public_doc(item, authorized_names=authorized)


def durable_task_inbox_item(owner_id: str, item_id: str) -> dict | None:
    with session() as s:
        item = s.get(DurableTaskInboxItem, str(item_id))
        if (item is None or item.owner_id != str(owner_id)
                or item.task_kind in _INBOX_HIDDEN_TASK_KINDS):
            return None
        authorized = _inbox_authorized_canvas_names(
            s, owner_id, {item.canvas_id} if item.canvas_id is not None else set())
        return _inbox_public_doc(item, authorized_names=authorized)


def durable_task_submission_id(uid: str, canvas_id: str, submission_id: str) -> str:
    # Preserve the existing write-admission lineage identity. The durable task replaces the old local
    # dispatch owner for this exact submission; it must not mint a second publication idempotency key.
    return local_run_submission_id(uid, canvas_id, submission_id)


def _task_status_doc(task_id: str, target_node_id: str | None, status: str = "queued") -> dict:
    from hub.models import RunStatus
    return RunStatus(
        run_id=str(task_id), status=status,
        target_node_id=str(target_node_id) if target_node_id is not None else None,
    ).model_dump()


def _durable_task_db_now(s) -> datetime.datetime:
    value = _db_now(s)
    return (value.replace(tzinfo=datetime.timezone.utc)
            if value.tzinfo is None else value.astimezone(datetime.timezone.utc))


def _durable_task_admission(s, task: DurableTask) -> dict:
    """Load one Task's immutable definition from its canonical manifest or legacy frozen columns."""
    if task.task_kind == "distribution_report":
        return {}
    if task.execution_manifest_sha256 is not None:
        from hub.execution_manifest import execution_manifest_admission

        manifest = s.get(ExecutionManifest, task.execution_manifest_sha256)
        if manifest is None:
            raise RuntimeError("durable task execution manifest is unavailable")
        try:
            admission = execution_manifest_admission(manifest.sha256, manifest.semantic_doc)
        except ValueError as exc:
            raise RuntimeError("durable task execution manifest is invalid") from exc
        if (admission["target_node_id"] != task.target_node_id
                or admission["target_port_id"] is not None
                or admission["write_intent"] is None):
            raise RuntimeError("durable task execution manifest changed its admission")
        return admission
    if task.graph_doc is None or task.input_manifest is None or task.write_intent is None:
        raise RuntimeError("legacy durable task frozen admission is incomplete")
    try:
        return {
            "graph_doc": json.loads(task.graph_doc),
            "input_manifest": json.loads(task.input_manifest),
            "write_intent": json.loads(task.write_intent),
            "target_node_id": task.target_node_id,
            "target_port_id": None,
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError("legacy durable task frozen admission is invalid") from exc


def _persist_durable_task_manifest(
        s, sha256: str, semantic_doc: str, *, target_node_id: str,
        graph_doc: dict, input_manifest: list[dict[str, str]], write_intent: dict) -> dict:
    """Persist and reopen the one canonical definition before creating Task ownership rows."""
    from hub.execution_manifest import (
        execution_manifest_accepts_graph_replay,
        execution_manifest_admission,
    )
    from hub.local_run_inputs import validate_manifest
    from hub.models import Graph, WriteIntent

    supplied_graph = Graph.model_validate(graph_doc)
    supplied_inputs = validate_manifest(input_manifest)
    supplied_write = WriteIntent.model_validate(write_intent)
    if not execution_manifest_accepts_graph_replay(
            str(sha256), str(semantic_doc), supplied_graph,
            target_node_id=str(target_node_id), target_port_id=None):
        raise ValueError("durable task execution manifest does not match its graph")
    _persist_execution_manifest(s, str(sha256), str(semantic_doc))
    manifest = s.get(ExecutionManifest, str(sha256))
    if manifest is None:  # pragma: no cover - persistence helper guarantees the row
        raise RuntimeError("durable task execution manifest was not persisted")
    admission = execution_manifest_admission(manifest.sha256, manifest.semantic_doc)
    if (admission["target_node_id"] != str(target_node_id)
            or admission["target_port_id"] is not None
            or admission["write_intent"] is None):
        raise ValueError("durable task execution manifest does not match its target")
    retained_inputs = [{key: item[key] for key in (
        "node_id", "dataset_id", "revision_id", "provider")}
        for item in admission["input_manifest"]]
    requested_inputs = [{key: item[key] for key in (
        "node_id", "dataset_id", "revision_id", "provider")}
        for item in supplied_inputs]
    if retained_inputs != requested_inputs:
        raise ValueError("durable task execution manifest does not match its inputs")
    if WriteIntent.model_validate(admission["write_intent"]) != supplied_write:
        raise ValueError("durable task execution manifest does not match its write intent")
    return admission


def submit_durable_local_write_task(
        *, uid: str, canvas_id: str, submission_id: str, target_node_id: str,
        intent_sha256: str, graph_doc: dict, input_manifest: list[dict[str, str]],
        write_intent: dict, execution_manifest_sha256: str,
        execution_manifest_doc: str,
        local_file_candidates: list[dict[str, str]] | None = None,
        ) -> tuple[dict, bool]:
    """Persist a task and its first attempt atomically, adopting an identical replay."""
    from hub.local_run_inputs import validate_manifest
    from hub.models import Graph, WriteIntent

    uid, canvas_id, target_node_id = str(uid), str(canvas_id), str(target_node_id)
    intent_sha256 = str(intent_sha256).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", intent_sha256):
        raise ValueError("durable task requires a SHA-256 semantic admission identity")
    execution_manifest_sha256 = str(execution_manifest_sha256).lower()
    if intent_sha256 != execution_manifest_sha256:
        raise ValueError("durable task semantic identity must be its execution manifest")
    graph = Graph.model_validate(graph_doc).model_dump(by_alias=True, mode="json")
    intent = WriteIntent.model_validate(write_intent)
    if intent.mode not in ("create", "replace"):
        raise ValueError("durable local tasks support only create or replace writes")
    input_manifest = validate_manifest(input_manifest)
    graph_payload = json.dumps(graph, sort_keys=True, separators=(",", ":"))
    manifest_payload = json.dumps(input_manifest, sort_keys=True, separators=(",", ":"))
    intent_payload = json.dumps(
        intent.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))
    if any(len(payload.encode()) > _DURABLE_TASK_DOC_MAX_BYTES
           for payload in (graph_payload, manifest_payload, intent_payload)):
        raise ValueError("durable task immutable admission exceeds the bounded limit")
    task_id = durable_task_submission_id(uid, canvas_id, submission_id)
    status_doc = json.dumps(_task_status_doc(task_id, target_node_id), default=str)
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            raise RuntimeError("durable task canvas does not exist")
        if s.get_bind().dialect.name == "sqlite":
            s.execute(update(Canvas).where(Canvas.id == canvas_id).values(
                updated_at=Canvas.updated_at))
        now = _durable_task_db_now(s)
        existing = s.get(DurableTask, task_id, with_for_update=True)
        if existing is not None:
            if (existing.owner_id != uid or existing.canvas_id != canvas_id
                    or existing.submission_id != str(submission_id).lower()
                    or existing.target_node_id != target_node_id
                    or (existing.execution_manifest_sha256 is not None
                        and (existing.execution_manifest_sha256 != execution_manifest_sha256
                             or existing.intent_sha256 != execution_manifest_sha256))
                    or (existing.execution_manifest_sha256 is None
                        and existing.intent_sha256 != intent_sha256)):
                raise DurableTaskSubmissionConflict(
                    "durable task submission does not match its frozen admission")
            if existing.execution_manifest_sha256 is not None:
                _persist_execution_manifest(
                    s, execution_manifest_sha256, str(execution_manifest_doc))
            return _durable_task_doc(s, existing), False
        admission = _persist_durable_task_manifest(
            s, execution_manifest_sha256, execution_manifest_doc,
            target_node_id=target_node_id, graph_doc=graph_doc,
            input_manifest=input_manifest, write_intent=write_intent)
        input_manifest = admission["input_manifest"]
        task = DurableTask(
            id=task_id, owner_id=uid, canvas_id=canvas_id,
            submission_id=str(submission_id).lower(), intent_sha256=execution_manifest_sha256,
            target_node_id=target_node_id,
            task_kind="managed_local_write",
            execution_manifest_sha256=execution_manifest_sha256,
            backend_kind="local", graph_doc=None,
            input_manifest=None, write_intent=None,
            status="queued", status_doc=status_doc, created_at=now, updated_at=now,
        )
        s.add(task)
        s.add(DurableTaskAttempt(
            id=uuid.uuid4().hex, task_id=task_id, attempt_number=1,
            execution_manifest_sha256=execution_manifest_sha256,
            status="queued", created_at=now,
        ))
        s.flush()
        _admit_local_file_input_revisions(
            s, input_manifest, list(local_file_candidates or []))
        sync_local_result_owner(s, "durable_task", task_id, input_manifest)
        return _durable_task_doc(s, task), True


def _linear_checkpoint_identity(value: object, field: str, limit: int) -> str:
    value = str(value)
    if (not value or len(value) > limit or value != value.strip() or "\x00" in value
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
        raise ValueError(f"checkpoint {field} is not a canonical identity")
    return value


def _linear_checkpoint_payloads(
        graph_doc: dict, input_manifest: list[dict[str, str]], write_intent: dict,
        final_target_node_id: object, checkpoint_node_id: object,
        output_port_id: object) -> tuple[str, str, str, str, str, str]:
    from hub.local_run_inputs import validate_manifest
    from hub.models import Graph, WriteIntent

    graph = Graph.model_validate(graph_doc, extra="forbid")
    manifest = validate_manifest(input_manifest)
    intent = WriteIntent.model_validate(write_intent, extra="forbid")
    final_id = _linear_checkpoint_identity(final_target_node_id, "final target", 256)
    checkpoint_id = _linear_checkpoint_identity(checkpoint_node_id, "node", 256)
    port_id = _linear_checkpoint_identity(output_port_id, "output port", 128)
    node_ids = {str(node.id) for node in graph.nodes}
    if final_id not in node_ids or checkpoint_id not in node_ids:
        raise ValueError("checkpoint admission nodes must exist in the saved graph")
    payloads = (
        json.dumps(graph.model_dump(by_alias=True, mode="json"),
                   sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        json.dumps(intent.model_dump(by_alias=True, mode="json"),
                   sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )
    if any(len(payload.encode("utf-8")) > _DURABLE_TASK_DOC_MAX_BYTES for payload in payloads):
        raise ValueError("checkpoint frozen admission exceeds the bounded limit")
    return (*payloads, final_id, checkpoint_id, port_id)


def _linear_checkpoint_admission_doc(
        s, task: DurableTask, checkpoint: DurableCheckpoint) -> dict:
    first = s.scalar(select(DurableTaskAttempt).where(
        DurableTaskAttempt.task_id == task.id,
        DurableTaskAttempt.attempt_number == 1))
    try:
        admission = _durable_task_admission(s, task)
        graph_doc = admission["graph_doc"]
        input_manifest = admission["input_manifest"]
        write_intent = admission["write_intent"]
        canonical = _linear_checkpoint_payloads(
            graph_doc, input_manifest, write_intent, task.target_node_id,
            checkpoint.checkpoint_node_id, checkpoint.output_port_id)
    except Exception as exc:
        raise DurableTaskSubmissionConflict("checkpoint admission is invalid") from exc
    legacy_payloads = (task.graph_doc, task.input_manifest, task.write_intent)
    if (first is None or checkpoint.task_id != task.id
            or first.execution_manifest_sha256 != task.execution_manifest_sha256
            or (task.execution_manifest_sha256 is None and canonical[:3] != legacy_payloads)
            or checkpoint.task_intent_sha256 != task.intent_sha256
            or hashlib.sha256(canonical[1].encode()).hexdigest()
            != checkpoint.input_manifest_sha256):
        raise DurableTaskSubmissionConflict("checkpoint admission is incomplete")
    return {
        "task_id": task.id, "attempt_id": first.id,
        "owner_id": task.owner_id, "canvas_id": task.canvas_id,
        "submission_id": task.submission_id,
        "final_target_node_id": task.target_node_id,
        "graph_doc": graph_doc, "input_manifest": input_manifest,
        "write_intent": write_intent,
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_node_id": checkpoint.checkpoint_node_id,
        "output_port_id": checkpoint.output_port_id,
        "task_intent_sha256": checkpoint.task_intent_sha256,
        "graph_prefix_sha256": checkpoint.graph_prefix_sha256,
        "input_manifest_sha256": checkpoint.input_manifest_sha256,
        "phase": checkpoint.phase,
    }


def submit_linear_checkpoint_task(
        *, uid: str, canvas_id: str, submission_id: str,
        final_target_node_id: str, checkpoint_id: str, checkpoint_node_id: str,
        output_port_id: str, task_intent_sha256: str, graph_prefix_sha256: str,
        input_manifest_sha256: str, graph_doc: dict,
        input_manifest: list[dict[str, str]], write_intent: dict,
        execution_manifest_sha256: str, execution_manifest_doc: str,
        task_kind: str = "linear_checkpoint_write") -> tuple[dict, bool]:
    """Atomically persist one complete hidden Task, first Attempt, and checkpoint."""
    from hub.models import Graph

    if task_kind not in _CHECKPOINT_PARENT_KINDS:
        raise ValueError("checkpoint parent task_kind is not supported")
    graph, manifest, intent, final_id, node_id, port_id = _linear_checkpoint_payloads(
        graph_doc, input_manifest, write_intent, final_target_node_id,
        checkpoint_node_id, output_port_id)
    checkpoint_id = _linear_checkpoint_identity(checkpoint_id, "ID", 128)
    digests = tuple(map(str, (
        task_intent_sha256, graph_prefix_sha256, input_manifest_sha256)))
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests):
        raise ValueError("checkpoint admission requires canonical SHA-256 digests")
    execution_manifest_sha256 = str(execution_manifest_sha256).lower()
    if digests[0] != execution_manifest_sha256:
        raise ValueError("checkpoint semantic identity must be its execution manifest")
    if hashlib.sha256(manifest.encode()).hexdigest() != digests[2]:
        raise ValueError("checkpoint input-manifest digest does not match its document")
    uid, canvas_id = str(uid), str(canvas_id)
    normalized_submission = str(submission_id).lower()
    _linear_checkpoint_identity(normalized_submission, "submission", 256)
    task_id = durable_task_submission_id(uid, canvas_id, normalized_submission)
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            raise RuntimeError("checkpoint canvas does not exist")
        if s.get_bind().dialect.name == "sqlite":
            s.execute(update(Canvas).where(Canvas.id == canvas_id).values(
                updated_at=Canvas.updated_at))
        task = s.get(DurableTask, task_id, with_for_update=True)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        if task is not None or checkpoint is not None:
            if task is None or checkpoint is None:
                raise DurableTaskSubmissionConflict("checkpoint admission is incomplete")
            common = (
                task.owner_id, task.canvas_id, task.submission_id, task.target_node_id,
                checkpoint.checkpoint_id, checkpoint.checkpoint_node_id,
                checkpoint.output_port_id, checkpoint.task_intent_sha256,
                checkpoint.graph_prefix_sha256, checkpoint.input_manifest_sha256)
            expected_common = (
                uid, canvas_id, normalized_submission, final_id,
                checkpoint_id, node_id, port_id, *digests)
            legacy_matches = (
                task.intent_sha256, task.graph_doc, task.input_manifest, task.write_intent,
            ) == (digests[0], graph, manifest, intent)
            manifest_matches = (
                task.execution_manifest_sha256 == execution_manifest_sha256
                and task.intent_sha256 == execution_manifest_sha256)
            if (task.task_kind != task_kind or common != expected_common
                    or not (manifest_matches if task.execution_manifest_sha256 is not None
                            else legacy_matches)):
                raise DurableTaskSubmissionConflict(
                    "checkpoint submission does not match its frozen admission")
            if task.execution_manifest_sha256 is not None:
                _persist_execution_manifest(
                    s, execution_manifest_sha256, str(execution_manifest_doc))
            _lock_local_result_registry(s)
            _linear_checkpoint_candidate_doc(s, task, checkpoint)
            return _linear_checkpoint_admission_doc(s, task, checkpoint), False
        if (s.scalar(select(DurableCheckpoint.task_id).where(
                DurableCheckpoint.checkpoint_id == checkpoint_id).limit(1)) is not None
                or s.scalar(select(LocalResultArtifact.uri).where(
                    LocalResultArtifact.writer_run_id == task_id).limit(1)) is not None):
            raise DurableTaskSubmissionConflict("checkpoint identity has conflicting state")
        now = _durable_task_db_now(s)
        admission = _persist_durable_task_manifest(
            s, execution_manifest_sha256, execution_manifest_doc,
            target_node_id=final_id, graph_doc=graph_doc,
            input_manifest=input_manifest, write_intent=write_intent)
        graph, manifest, intent, final_id, node_id, port_id = _linear_checkpoint_payloads(
            admission["graph_doc"], admission["input_manifest"], admission["write_intent"],
            final_id, node_id, port_id)
        from hub.linear_checkpoint_tasks import graph_prefix_sha256 as canonical_prefix_sha256
        canonical_prefix = canonical_prefix_sha256(
            Graph.model_validate(admission["graph_doc"]), node_id)
        if canonical_prefix != digests[1]:
            raise ValueError("checkpoint graph-prefix digest does not match its manifest")
        if hashlib.sha256(manifest.encode()).hexdigest() != digests[2]:
            raise ValueError("checkpoint input-manifest digest does not match its manifest")
        task = DurableTask(
            id=task_id, owner_id=uid, canvas_id=canvas_id,
            submission_id=normalized_submission, intent_sha256=execution_manifest_sha256,
            target_node_id=final_id, task_kind=task_kind,
            execution_manifest_sha256=execution_manifest_sha256,
            backend_kind="local", graph_doc=None, input_manifest=None,
            write_intent=None, status="queued",
            status_doc=json.dumps(_task_status_doc(task_id, final_id), default=str),
            created_at=now, updated_at=now)
        attempt = DurableTaskAttempt(
            id=uuid.uuid4().hex, task_id=task_id, attempt_number=1,
            execution_manifest_sha256=execution_manifest_sha256,
            status="queued", created_at=now)
        checkpoint = DurableCheckpoint(
            task_id=task_id, checkpoint_id=checkpoint_id,
            checkpoint_node_id=node_id, output_port_id=port_id,
            task_intent_sha256=digests[0], graph_prefix_sha256=digests[1],
            input_manifest_sha256=digests[2], phase="pending",
            created_at=now, updated_at=now)
        s.add_all((task, attempt))
        s.flush()
        s.add(checkpoint)
        s.flush()
        return _linear_checkpoint_admission_doc(s, task, checkpoint), True


def linear_checkpoint_admission(task_id: str) -> dict | None:
    """Internal exact readback; product task readers intentionally exclude this kind."""
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id), with_for_update=True)
        if task is None and checkpoint is None:
            return None
        if (task is None or checkpoint is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise DurableTaskSubmissionConflict("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        _linear_checkpoint_candidate_doc(s, task, checkpoint)
        return _linear_checkpoint_admission_doc(s, task, checkpoint)


def _external_wait_key(task_id: str, attempt_number: int) -> str:
    digest = hashlib.sha256(
        f"external-wait-v1\x00{task_id}\x00{attempt_number}".encode()).hexdigest()
    return f"ew:{digest}"


def submit_durable_external_wait_task(
        *, uid: str, canvas_id: str, submission_id: str, target_node_id: str,
        intent_sha256: str, graph_doc: dict, provider_kind: str,
        operation: str, document_json: str, write_intent: dict,
        execution_manifest_sha256: str,
        execution_manifest_doc: str) -> tuple[dict, bool]:
    from hub.external_wait import ExternalWaitSubmitRequest
    from hub.models import Graph

    task_id = durable_task_submission_id(uid, canvas_id, submission_id)
    request = ExternalWaitSubmitRequest(
        provider_kind=provider_kind, idempotency_key=_external_wait_key(task_id, 1),
        operation=operation, document_json=document_json)
    graph = Graph.model_validate(graph_doc).model_dump(by_alias=True, mode="json")
    graph_payload = json.dumps(graph, sort_keys=True, separators=(",", ":"))
    request_payload = request.model_dump_json(by_alias=False)
    from hub.models import WriteIntent
    WriteIntent.model_validate(write_intent)
    if not re.fullmatch(r"[0-9a-f]{64}", str(intent_sha256)):
        raise ValueError("durable task requires a SHA-256 semantic admission identity")
    execution_manifest_sha256 = str(execution_manifest_sha256).lower()
    if str(intent_sha256).lower() != execution_manifest_sha256:
        raise ValueError("durable task semantic identity must be its execution manifest")
    if len(graph_payload.encode()) > _DURABLE_TASK_DOC_MAX_BYTES:
        raise ValueError("durable task immutable admission exceeds the bounded limit")
    status_doc = json.dumps(_task_status_doc(task_id, target_node_id), default=str)
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            raise RuntimeError("durable task canvas does not exist")
        if s.get_bind().dialect.name == "sqlite":
            s.execute(update(Canvas).where(Canvas.id == canvas_id).values(
                updated_at=Canvas.updated_at))
        now = _durable_task_db_now(s)
        existing = s.get(DurableTask, task_id, with_for_update=True)
        if existing is not None:
            if (existing.task_kind != "external_wait" or existing.owner_id != uid
                    or existing.canvas_id != canvas_id
                    or existing.submission_id != str(submission_id).lower()
                    or existing.target_node_id != target_node_id
                    or (existing.execution_manifest_sha256 is not None
                        and (existing.execution_manifest_sha256 != execution_manifest_sha256
                             or existing.intent_sha256 != execution_manifest_sha256))
                    or (existing.execution_manifest_sha256 is None
                        and existing.intent_sha256 != intent_sha256)):
                raise DurableTaskSubmissionConflict(
                    "durable task submission does not match its frozen admission")
            if existing.execution_manifest_sha256 is not None:
                _persist_execution_manifest(
                    s, execution_manifest_sha256, str(execution_manifest_doc))
            return _durable_task_doc(s, existing), False
        _persist_durable_task_manifest(
            s, execution_manifest_sha256, execution_manifest_doc,
            target_node_id=target_node_id, graph_doc=graph_doc,
            input_manifest=[], write_intent=write_intent)
        task = DurableTask(
            id=task_id, owner_id=uid, canvas_id=canvas_id,
            submission_id=str(submission_id).lower(), intent_sha256=execution_manifest_sha256,
            target_node_id=target_node_id, task_kind="external_wait", backend_kind="local",
            execution_manifest_sha256=execution_manifest_sha256,
            graph_doc=None, input_manifest=None, write_intent=None,
            status="queued", status_doc=status_doc, created_at=now, updated_at=now)
        attempt = DurableTaskAttempt(
            id=uuid.uuid4().hex, task_id=task_id, attempt_number=1,
            execution_manifest_sha256=execution_manifest_sha256,
            status="queued", created_at=now)
        s.add(task)
        s.flush()
        s.add_all((attempt, DurableExternalWait(
            task_id=task_id, provider_kind=request.provider_kind,
            submit_request=request_payload, idempotency_key=request.idempotency_key,
            phase="unsubmitted", next_poll_at=now,
            deadline_at=now + datetime.timedelta(seconds=60), updated_at=now)))
        s.flush()
        return _durable_task_doc(s, task), True


def _durable_task_doc(
        s, task: DurableTask, *, include_admission: bool = True,
        include_attempt_updates: bool = False,
        attempts: list[DurableTaskAttempt] | None = None) -> dict:
    if attempts is None:
        attempts = list(s.scalars(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task.id,
        ).order_by(DurableTaskAttempt.attempt_number)))
    if any(item.execution_manifest_sha256 != task.execution_manifest_sha256 for item in attempts):
        raise RuntimeError("durable Task and Attempt manifest owners disagree")

    def attempt_updated_at(attempt: DurableTaskAttempt) -> datetime.datetime:
        timestamps = [item for item in (
            attempt.created_at, attempt.started_at, attempt.heartbeat_at,
            attempt.cancel_requested_at, attempt.completed_at,
        ) if item is not None]
        return max(timestamps, key=lambda item: (
            item if item.tzinfo is not None
            else item.replace(tzinfo=datetime.timezone.utc)
        ).timestamp())
    doc = {
        "id": task.id, "owner_id": task.owner_id, "canvas_id": task.canvas_id,
        "dataset_view_id": task.dataset_view_id,
        "submission_id": task.submission_id, "target_node_id": task.target_node_id,
        "task_kind": task.task_kind, "intent_sha256": task.intent_sha256,
        "execution_manifest_sha256": task.execution_manifest_sha256,
        "execution_manifest_reconstructable": task.execution_manifest_sha256 is not None,
        "backend_kind": task.backend_kind, "status": task.status,
        "status_doc": json.loads(task.status_doc), "progress": task.progress,
        "cancel_requested": task.cancel_requested, "retry_count": task.retry_count,
        "max_attempts": task.max_attempts,
        "output_receipt": json.loads(task.output_receipt) if task.output_receipt else None,
        "error": task.error,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "attempts": [{
            "id": item.id, "attempt_number": item.attempt_number,
            "execution_manifest_sha256": item.execution_manifest_sha256,
            "execution_manifest_reconstructable": item.execution_manifest_sha256 is not None,
            "status": item.status, "owner_token": item.owner_token,
            "lease_until": item.lease_until, "heartbeat_at": item.heartbeat_at,
            "progress": item.progress, "error": item.error,
            "started_at": item.started_at, "completed_at": item.completed_at,
            **({"updated_at": attempt_updated_at(item)} if include_attempt_updates else {}),
            "output_receipt": json.loads(item.output_receipt) if item.output_receipt else None,
        } for item in attempts],
    }
    if include_admission:
        doc.update(_durable_task_admission(s, task))
    wait = s.get(DurableExternalWait, task.id)
    if wait is not None:
        doc["external_wait"] = {
            "provider_kind": wait.provider_kind, "phase": wait.phase,
            "next_poll_at": wait.next_poll_at, "deadline_at": wait.deadline_at,
            "poll_count": wait.poll_count,
            "diagnostic_code": wait.diagnostic_code,
        }
    return doc


def durable_task(task_id: str, *, include_admission: bool = True) -> dict | None:
    with session() as s:
        task = s.get(DurableTask, str(task_id))
        return (_durable_task_doc(s, task, include_admission=include_admission)
                if task is not None else None)


def durable_task_auth(task_id: str) -> tuple[str, str | None] | None:
    with session() as s:
        row = s.execute(select(DurableTask.owner_id, DurableTask.canvas_id).where(
            DurableTask.id == str(task_id))).one_or_none()
        return tuple(row) if row is not None else None


def _lock_durable_task_for_write(s, task_id: str) -> DurableTask | None:
    """Lock one Task before making an ownership/action decision on SQLite or Postgres."""
    task_id = str(task_id)
    if s.get_bind().dialect.name == "sqlite":
        # SQLite ignores SELECT ... FOR UPDATE. Acquire its stable explicit subject row before
        # reading Task/action replay state; distribution reports never fabricate a Canvas subject.
        subject = s.execute(select(
            DurableTask.canvas_id, DurableTask.dataset_view_id,
        ).where(DurableTask.id == task_id)).one_or_none()
        if subject is None:
            return None
        canvas_id, dataset_view_id = subject
        if canvas_id is not None:
            locked = s.execute(update(Canvas).where(Canvas.id == canvas_id).values(
                updated_at=Canvas.updated_at))
        elif dataset_view_id is not None:
            locked = s.execute(update(DatasetView).where(
                DatasetView.id == dataset_view_id).values(created_at=DatasetView.created_at))
        else:
            return None
        if locked.rowcount != 1:
            return None
    return s.get(DurableTask, task_id, with_for_update=True, populate_existing=True)


def _durable_task_write_receipt(task: DurableTask, receipt):
    """Bind receipt evidence to the Task's canonical definition without upgrading legacy rows."""
    retained = receipt.execution_manifest_sha256
    if retained is not None and retained != task.execution_manifest_sha256:
        raise ValueError("durable task WriteReceipt changed its execution manifest")
    if task.execution_manifest_sha256 is not None and retained is None:
        receipt = receipt.model_copy(update={
            "execution_manifest_sha256": task.execution_manifest_sha256})
    return receipt


def _finish_task_with_landed_write_receipt(s, task, attempt, now) -> bool:
    """Reconcile a write-bearing task to done when its receipt already landed, so dead-owner
    recovery, cancel, or attempt exhaustion never misreports a committed write as failed/cancelled."""
    from hub.models import RunOutput, RunStatus, WriteReceipt
    try:
        admission = _durable_task_admission(s, task)
        intent, _payload, canonical = _canonical_managed_local_write_intent(
            admission["write_intent"])
        prior = _managed_local_write_receipt_in_session(s, intent.idempotency_key, canonical)
        if prior is None:
            prior = _managed_local_lance_write_receipt_in_session(
                s, intent.idempotency_key, canonical)
    except Exception:
        return False
    if prior is None:
        return False
    receipt = _durable_task_write_receipt(task, WriteReceipt.model_validate(prior))
    output = RunOutput(
        node_id=task.target_node_id, port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed", uri=receipt.publication.artifact_uri,
        table=intent.destination.name, version=receipt.publication.catalog_version,
        rows=receipt.rows, write_receipt=receipt)
    status = RunStatus(
        run_id=task.id, status="done", target_node_id=task.target_node_id,
        outputs=[output], total_rows=receipt.rows).model_dump()
    receipt_json = json.dumps(receipt.model_dump(by_alias=True, mode="json"), sort_keys=True)
    task.status = attempt.status = "done"
    task.error = attempt.error = None
    task.status_doc = json.dumps(status, default=str)
    task.output_receipt = attempt.output_receipt = receipt_json
    task.completed_at = attempt.completed_at = task.updated_at = now
    _emit_durable_task_inbox_item(s, task=task, attempt=attempt, task_status="done", now=now)
    return True


def _terminalize_hidden_task_envelope(s, task: DurableTask, now: datetime.datetime) -> None:
    if task.task_kind != "distribution_report":
        return
    report = s.get(DistributionReportEnvelope, task.id, with_for_update=True)
    if report is None:
        raise RuntimeError("distribution report envelope is unavailable")
    report.updated_at = report.completed_at = now


def _claim_durable_task_kind(
        task_id: str, owner_token: str, task_kind: str) -> dict | None:
    """Claim queued work or fence one expired owner and create the next bounded attempt."""
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        if (task is None or task.task_kind != task_kind
                or task.status in _TERMINAL_RUN):
            return None
        now = _durable_task_db_now(s)
        attempt = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task.id,
        ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
        if attempt is None:
            raise RuntimeError("durable task has no attempt")
        lease = attempt.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if attempt.status == "running" and lease is not None and lease > now:
            return None
        if attempt.status == "running":
            attempt.status = "fenced"
            attempt.error = "attempt owner lease expired"
            attempt.completed_at = now
            if attempt.attempt_number >= task.max_attempts:
                if _finish_task_with_landed_write_receipt(s, task, attempt, now):
                    return None
                task.status = "failed"
                task.error = "durable task exhausted recovery attempts"
                task.completed_at = now
                failed = _task_status_doc(task.id, task.target_node_id, "failed")
                failed["error"] = task.error
                task.status_doc = json.dumps(failed, default=str)
                _terminalize_hidden_task_envelope(s, task, now)
                _emit_durable_task_inbox_item(
                    s, task=task, attempt=attempt, task_status="failed",
                    diagnostic_code="durable_task_attempts_exhausted", now=now)
                return None
            task.retry_count = attempt.attempt_number
            attempt = DurableTaskAttempt(
                id=uuid.uuid4().hex, task_id=task.id,
                attempt_number=attempt.attempt_number + 1,
                execution_manifest_sha256=task.execution_manifest_sha256,
                status="queued", created_at=now)
            s.add(attempt)
            s.flush()
        if task.cancel_requested:
            if _finish_task_with_landed_write_receipt(s, task, attempt, now):
                return None
            attempt.status = "cancelled"
            attempt.cancel_requested_at = now
            attempt.completed_at = now
            task.status = "cancelled"
            task.completed_at = now
            task.status_doc = json.dumps(
                _task_status_doc(task.id, task.target_node_id, "cancelled"), default=str)
            _terminalize_hidden_task_envelope(s, task, now)
            _emit_durable_task_inbox_item(
                s, task=task, attempt=attempt, task_status="cancelled", now=now)
            return None
        attempt.status = "running"
        attempt.owner_token = str(owner_token)
        attempt.started_at = attempt.started_at or now
        attempt.heartbeat_at = now
        attempt.lease_until = now + datetime.timedelta(seconds=_DURABLE_TASK_LEASE_SECONDS)
        task.status = "running"
        task.error = None
        task.updated_at = now
        return _durable_task_doc(s, task)


def claim_durable_task(task_id: str, owner_token: str) -> dict | None:
    return _claim_durable_task_kind(task_id, owner_token, "managed_local_write")


def claim_linear_checkpoint_task(task_id: str, owner_token: str) -> dict | None:
    """Claim one linear-checkpoint Task under its dedicated recovery/worker path."""
    return _claim_durable_task_kind(task_id, owner_token, "linear_checkpoint_write")


def claim_bounded_fanout_write_task(task_id: str, owner_token: str) -> dict | None:
    """Claim one bounded fan-out parent Task under its dedicated recovery/worker path."""
    return _claim_durable_task_kind(task_id, owner_token, "bounded_fanout_write")


def durable_task_attempt_should_stop(task_id: str, attempt_id: str, owner_token: str) -> bool:
    with session() as s:
        now = _durable_task_db_now(s)
        task = s.get(DurableTask, str(task_id))
        attempt = s.get(DurableTaskAttempt, str(attempt_id))
        if task is None or attempt is None:
            return True
        lease = attempt.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        return bool(
            task.status in _TERMINAL_RUN or task.cancel_requested
            or attempt.task_id != task.id or attempt.status != "running"
            or attempt.owner_token != str(owner_token)
            or lease is None or lease <= now)


def heartbeat_durable_task(task_id: str, attempt_id: str, owner_token: str) -> bool:
    with session() as s:
        now = _durable_task_db_now(s)
        result = s.execute(update(DurableTaskAttempt).where(
            DurableTaskAttempt.id == str(attempt_id),
            DurableTaskAttempt.task_id == str(task_id),
            DurableTaskAttempt.owner_token == str(owner_token),
            DurableTaskAttempt.status == "running",
            DurableTaskAttempt.lease_until > now,
        ).values(
            heartbeat_at=now,
            lease_until=now + datetime.timedelta(seconds=_DURABLE_TASK_LEASE_SECONDS),
        ))
        return result.rowcount == 1


def update_durable_task_status(
        task_id: str, attempt_id: str, owner_token: str, status_doc: dict) -> bool:
    from hub.models import RunStatus
    status = RunStatus.model_validate(status_doc)
    if status.run_id != str(task_id):
        raise ValueError("durable task status changed its task identity")
    if status.status in _TERMINAL_RUN:
        return finish_durable_task_attempt(
            task_id, attempt_id, owner_token, status.model_dump())
    payload = json.dumps(status.model_dump(), default=str)
    with session() as s:
        task = s.get(DurableTask, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = attempt.lease_until if attempt is not None else None
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.owner_token != str(owner_token) or attempt.status != "running"
                or lease is None or lease <= now):
            return False
        task.status = "running"
        task.status_doc = payload
        task.progress = status.progress
        task.updated_at = now
        attempt.progress = status.progress
        return True


def finish_durable_task_attempt(
        task_id: str, attempt_id: str, owner_token: str, status_doc: dict) -> bool:
    from hub.models import RunStatus, WriteReceipt
    status = RunStatus.model_validate(status_doc)
    if status.status not in _TERMINAL_RUN or status.run_id != str(task_id):
        raise ValueError("durable task completion requires its terminal task status")
    receipt: WriteReceipt | None = None
    if status.status == "done":
        receipts = [output.write_receipt for output in status.outputs
                    if output.outcome == "committed" and output.write_receipt is not None]
        if len(status.outputs) != 1 or len(receipts) != 1:
            raise ValueError("successful durable write task requires one exact WriteReceipt")
        receipt = receipts[0]
    with session() as s:
        task = s.get(DurableTask, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        if task is None or attempt is None:
            return False
        if receipt is not None:
            receipt = _durable_task_write_receipt(task, receipt)
            status.outputs[0].write_receipt = receipt
        payload = json.dumps(status.model_dump(), default=str)
        receipt_payload = (
            json.dumps(receipt.model_dump(by_alias=True, mode="json"), sort_keys=True)
            if receipt is not None else None)
        if task.status in _TERMINAL_RUN:
            ok = task.status == status.status and task.status_doc == payload
            # Same-attempt response-loss replay reconciles the existing item. A superseded /
            # fenced caller that happens to carry an identical task-level status_doc must not
            # mint a second Inbox row under a different attempt id.
            if ok and attempt.task_id == task.id and attempt.status == status.status:
                _emit_durable_task_inbox_item(
                    s, task=task, attempt=attempt, task_status=status.status)
            return ok
        now = _durable_task_db_now(s)
        lease = attempt.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (attempt.owner_token != str(owner_token) or attempt.status != "running"
                or lease is None or lease <= now):
            return False
        attempt.status = status.status
        attempt.progress = status.progress
        attempt.error = status.error
        attempt.output_receipt = receipt_payload
        attempt.completed_at = now
        attempt.lease_until = now
        task.status = status.status
        task.status_doc = payload
        task.progress = status.progress
        task.error = status.error
        task.output_receipt = receipt_payload
        task.completed_at = now
        task.updated_at = now
        _emit_durable_task_inbox_item(
            s, task=task, attempt=attempt, task_status=status.status, now=now)
        return True


def request_durable_task_cancel(task_id: str) -> dict | None:
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        if task is None:
            return None
        if task.status not in _TERMINAL_RUN:
            now = _durable_task_db_now(s)
            task.cancel_requested = True
            task.updated_at = now
            attempt = s.scalar(select(DurableTaskAttempt).where(
                DurableTaskAttempt.task_id == task.id,
            ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
            if attempt is not None and attempt.cancel_requested_at is None:
                attempt.cancel_requested_at = now
        return _durable_task_doc(s, task)


def _external_wait_terminal(
        s, task: DurableTask, attempt: DurableTaskAttempt, wait: DurableExternalWait,
        *, task_status: str, phase: str, code: str | None = None) -> None:
    now = _durable_task_db_now(s)
    error = code.replace("_", " ") if code else None
    wait.phase, wait.diagnostic_code = phase, code
    wait.owner_token = wait.lease_until = None
    wait.next_poll_at = now
    wait.updated_at = now
    attempt.status = task_status
    attempt.error = error
    attempt.completed_at = now
    task.status = task_status
    task.error = error
    task.output_receipt = None
    task.completed_at = now
    task.updated_at = now
    status = _task_status_doc(
        task.id, None if task_status == "done" else task.target_node_id, task_status)
    if error:
        status["error"] = error
    task.status_doc = json.dumps(status, default=str)
    _emit_durable_task_inbox_item(
        s, task=task, attempt=attempt, task_status=task_status,
        diagnostic_code=code, now=now)


def due_external_wait_task_ids(limit: int = 100) -> list[str]:
    with session() as s:
        now = _durable_task_db_now(s)
        rows = s.scalars(select(DurableTask.id).join(
            DurableExternalWait, DurableExternalWait.task_id == DurableTask.id).where(
                DurableTask.task_kind == "external_wait",
                DurableTask.status.in_(("queued", "running")),
                or_(DurableTask.cancel_requested,
                    DurableExternalWait.next_poll_at <= now,
                    DurableExternalWait.deadline_at <= now),
                or_(DurableExternalWait.lease_until.is_(None),
                    DurableExternalWait.lease_until <= now),
            ).order_by(DurableExternalWait.next_poll_at, DurableTask.created_at).limit(limit)).all()
        return [str(row) for row in rows]


def fail_corrupt_external_wait_tasks(limit: int = 100) -> None:
    with session() as s:
        has_attempt = exists(select(DurableTaskAttempt.id).where(
            DurableTaskAttempt.task_id == DurableTask.id))
        ids = list(s.scalars(select(DurableTask.id).outerjoin(
            DurableExternalWait, DurableExternalWait.task_id == DurableTask.id).where(
            DurableTask.task_kind == "external_wait",
            DurableTask.status.in_(("queued", "running")),
            or_(DurableExternalWait.task_id.is_(None), ~has_attempt),
        ).order_by(DurableTask.created_at).limit(limit)))
    for task_id in ids:
        with session() as s:
            task = _lock_durable_task_for_write(s, str(task_id))
            attempt = s.scalar(select(DurableTaskAttempt).where(
                DurableTaskAttempt.task_id == str(task_id),
            ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
            wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
            if (task is None or task.status in _TERMINAL_RUN
                    or (attempt is not None and wait is not None)):
                continue
            now = _durable_task_db_now(s)
            if attempt is None:
                attempt = DurableTaskAttempt(
                    id=uuid.uuid4().hex, task_id=task.id, attempt_number=1,
                    execution_manifest_sha256=task.execution_manifest_sha256,
                    status="failed", error="external wait evidence invalid",
                    completed_at=now, created_at=now)
                s.add(attempt)
                s.flush()
            else:
                attempt.status = "failed"
                attempt.error = "external wait evidence invalid"
                attempt.completed_at = now
            task.status = "failed"
            task.error = "external wait evidence invalid"
            task.completed_at = task.updated_at = now
            if wait is not None:
                wait.phase = "provider_failed"
                wait.diagnostic_code = "external_wait_evidence_invalid"
                wait.owner_token = wait.lease_until = None
                wait.updated_at = now
            status = _task_status_doc(task.id, task.target_node_id, "failed")
            status["error"] = task.error
            task.status_doc = json.dumps(status, default=str)
            _emit_durable_task_inbox_item(
                s, task=task, attempt=attempt, task_status="failed",
                diagnostic_code="external_wait_evidence_invalid", now=now)


def expire_external_wait_deadlines(limit: int = 100) -> None:
    with session() as s:
        now = _durable_task_db_now(s)
        ids = list(s.scalars(select(DurableTask.id).join(
            DurableExternalWait, DurableExternalWait.task_id == DurableTask.id).where(
                DurableTask.task_kind == "external_wait",
                DurableTask.status.in_(("queued", "running")),
                DurableExternalWait.phase.in_((
                    "unsubmitted", "submitting", "accepted", "running")),
                DurableExternalWait.deadline_at <= now,
            ).order_by(DurableExternalWait.deadline_at).limit(limit)))
    for task_id in ids:
        with session() as s:
            task = _lock_durable_task_for_write(s, str(task_id))
            wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
            attempt = s.scalar(select(DurableTaskAttempt).where(
                DurableTaskAttempt.task_id == str(task_id),
            ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
            if (task is not None and wait is not None and attempt is not None
                    and task.status not in _TERMINAL_RUN):
                _external_wait_terminal(
                    s, task, attempt, wait, task_status="failed",
                    phase="provider_failed", code="external_wait_deadline")


def claim_external_wait_transition(task_id: str, owner_token: str) -> dict | None:
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        if (task is None or task.task_kind != "external_wait"
                or task.status in _TERMINAL_RUN):
            return None
        wait = s.get(DurableExternalWait, task.id, with_for_update=True)
        attempt = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task.id,
        ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
        if wait is None or attempt is None:
            raise RuntimeError("external-wait durable evidence is missing")
        now = _durable_task_db_now(s)
        lease = wait.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if lease is not None and lease > now:
            return None
        finalizing = wait.phase in (
            "provider_succeeded", "downloading", "downloaded", "publishing")
        if task.cancel_requested and wait.handle_doc is None and wait.phase == "unsubmitted":
            _external_wait_terminal(
                s, task, attempt, wait, task_status="cancelled",
                phase="cancelled_before_submit")
            return None
        deadline = wait.deadline_at.replace(
            tzinfo=wait.deadline_at.tzinfo or datetime.timezone.utc)
        next_poll = wait.next_poll_at.replace(
            tzinfo=wait.next_poll_at.tzinfo or datetime.timezone.utc)
        if not finalizing and deadline <= now:
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="provider_failed", code="external_wait_deadline")
            return None
        if not finalizing and wait.poll_count >= 64:
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="provider_failed", code="external_wait_poll_budget")
            return None
        if not finalizing and not task.cancel_requested and next_poll > now:
            return None
        wait.owner_token = str(owner_token)
        wait.lease_until = now + datetime.timedelta(seconds=_DURABLE_TASK_LEASE_SECONDS)
        wait.updated_at = now
        if wait.phase == "unsubmitted":
            wait.phase = "submitting"
        elif wait.phase == "provider_succeeded" and not task.cancel_requested:
            wait.phase = "downloading"
        action = ("cancel_after_success" if task.cancel_requested
                  and wait.phase in ("provider_succeeded", "downloading", "downloaded") else
                  "download" if wait.phase == "downloading" else
                  "publish" if wait.phase in ("downloaded", "publishing") else "provider")
        if action == "publish":
            wait.phase = "publishing"
        attempt.status = "running"
        attempt.started_at = attempt.started_at or now
        task.status = "running"
        task.status_doc = json.dumps(
            _task_status_doc(task.id, task.target_node_id, "running"), default=str)
        task.updated_at = now
        try:
            return {
                "task_id": task.id, "attempt_id": attempt.id,
                "provider_kind": wait.provider_kind,
                "submit_request": json.loads(wait.submit_request),
                "handle": json.loads(wait.handle_doc) if wait.handle_doc else None,
                "checkpoint": json.loads(wait.checkpoint_doc) if wait.checkpoint_doc else None,
                "phase": wait.phase, "action": action,
                "attempt_number": attempt.attempt_number,
                "stage_dev": wait.stage_dev, "stage_ino": wait.stage_ino,
                "download_evidence": (json.loads(wait.download_evidence)
                                      if wait.download_evidence else None),
                "write_intent": _durable_task_admission(s, task)["write_intent"],
                "cancel_requested": task.cancel_requested,
            }
        except (TypeError, ValueError):
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="provider_failed", code="external_wait_evidence_invalid")
            return None


def commit_external_wait_transition(
        task_id: str, attempt_id: str, owner_token: str, *,
        handle: dict | None = None, outcome: dict | None = None,
        failure_code: str | None = None, retry_after: float | None = None) -> bool:
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        if (task is None or wait is None or attempt is None
                or task.status in _TERMINAL_RUN or attempt.task_id != task.id
                or wait.owner_token != str(owner_token)):
            return False
        now = _durable_task_db_now(s)
        lease = wait.lease_until
        if lease is None or lease.replace(
                tzinfo=lease.tzinfo or datetime.timezone.utc) <= now:
            return False
        latest = s.scalar(select(func.max(DurableTaskAttempt.attempt_number)).where(
            DurableTaskAttempt.task_id == task.id))
        if latest != attempt.attempt_number:
            return False
        wait.owner_token = wait.lease_until = None
        wait.updated_at = now
        if failure_code is not None:
            if wait.handle_doc is not None:
                wait.poll_count += 1
                if wait.poll_count >= 64:
                    _external_wait_terminal(
                        s, task, attempt, wait, task_status="failed",
                        phase="provider_failed", code="external_wait_poll_budget")
                    return True
            if failure_code in ("adapter_return_invalid", "phase_regressed", "checkpoint_regressed"):
                _external_wait_terminal(
                    s, task, attempt, wait, task_status="failed",
                    phase="provider_failed", code=failure_code)
            else:
                wait.diagnostic_code = failure_code
                wait.next_poll_at = now + datetime.timedelta(
                    seconds=max(.05, min(float(retry_after or .25), 5.0)))
            return True
        if handle is not None:
            if wait.handle_doc is not None:
                return False
            wait.handle_doc = json.dumps(handle, sort_keys=True, separators=(",", ":"))
            wait.phase = "accepted"
            wait.diagnostic_code = None
            wait.next_poll_at = now + datetime.timedelta(seconds=.05)
            return True
        if outcome is None or wait.handle_doc is None:
            return False
        wait.poll_count += 1
        phase = str(outcome["phase"])
        ranks = {"unsubmitted": 0, "submitting": 0, "accepted": 1, "running": 2,
                 "succeeded": 3, "failed": 3, "cancelled": 3}
        current = ranks.get(wait.phase, 0)
        if ranks[phase] < current:
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="provider_failed", code="phase_regressed")
            return True
        checkpoint = outcome.get("checkpoint")
        prior = json.loads(wait.checkpoint_doc) if wait.checkpoint_doc else None
        if checkpoint is not None and prior is not None and checkpoint["sequence"] < prior["sequence"]:
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="provider_failed", code="checkpoint_regressed")
            return True
        if checkpoint is not None:
            wait.checkpoint_doc = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
        wait.diagnostic_code = (outcome.get("diagnostic") or {}).get("code")
        if phase == "succeeded":
            wait.phase = "provider_succeeded"
            wait.next_poll_at = now
        elif phase == "failed":
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed", phase="provider_failed",
                code=wait.diagnostic_code or "provider_failed")
        elif phase == "cancelled":
            _external_wait_terminal(
                s, task, attempt, wait, task_status="cancelled", phase="provider_cancelled")
        else:
            wait.phase = phase
            hint = float((outcome.get("retry") or {}).get("after_seconds") or .25)
            backoff = min(5.0, .05 * (2 ** min(wait.poll_count, 6)))
            wait.next_poll_at = now + datetime.timedelta(seconds=max(.05, min(5.0, max(hint, backoff))))
        return True


def pin_external_wait_stage(
        task_id: str, attempt_id: str, owner_token: str, dev: int, ino: int) -> bool:
    """Persist the exact attempt directory identity before untrusted download I/O."""
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.task_id != task.id or wait.phase != "downloading"
                or wait.owner_token != str(owner_token) or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return False
        identity = (int(dev), int(ino))
        if wait.stage_dev is None:
            wait.stage_dev, wait.stage_ino = identity
        elif (wait.stage_dev, wait.stage_ino) != identity:
            return False
        return True


def heartbeat_external_wait_transition(
        task_id: str, attempt_id: str, owner_token: str) -> bool:
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.task_id != task.id or wait.owner_token != str(owner_token)
                or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return False
        wait.lease_until = now + datetime.timedelta(seconds=_DURABLE_TASK_LEASE_SECONDS)
        wait.updated_at = now
        return True


def commit_external_wait_download(
        task_id: str, attempt_id: str, owner_token: str, evidence: dict) -> str | None:
    from hub.external_wait import ExternalWaitDownloadEvidence
    payload = ExternalWaitDownloadEvidence.model_validate(evidence).model_dump(mode="json")
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.task_id != task.id or wait.phase != "downloading"
                or wait.owner_token != str(owner_token) or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return None
        wait.updated_at = wait.next_poll_at = now
        if task.cancel_requested:
            return "cancel_requested"
        wait.owner_token = wait.lease_until = None
        wait.download_evidence = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        wait.phase = "downloaded"
        wait.diagnostic_code = None
        return "downloaded"


def cancel_external_wait_after_success(
        task_id: str, attempt_id: str, owner_token: str) -> bool:
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or not task.cancel_requested
                or attempt.task_id != task.id
                or wait.phase not in ("provider_succeeded", "downloading", "downloaded")
                or wait.owner_token != str(owner_token) or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return False
        wait.stage_dev = wait.stage_ino = None
        wait.download_evidence = None
        _external_wait_terminal(
            s, task, attempt, wait, task_status="cancelled", phase="cancelled_after_success")
        return True


def finish_external_wait_publication(
        task_id: str, attempt_id: str, owner_token: str, receipt_doc: dict) -> bool:
    from hub.models import RunOutput, RunStatus, WriteIntent, WriteReceipt
    receipt = WriteReceipt.model_validate(receipt_doc)
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.task_id != task.id or wait.phase != "publishing"
                or wait.owner_token != str(owner_token) or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return False
        intent = WriteIntent.model_validate(_durable_task_admission(s, task)["write_intent"])
        receipt = _durable_task_write_receipt(task, receipt)
        if (receipt.publication.idempotency_key != intent.idempotency_key
                or receipt.publication.logical_uri != intent.destination.logical_uri):
            raise RuntimeError("external-wait publication receipt changed its frozen intent")
        output = RunOutput(
            node_id=task.target_node_id, port_id="out", wire="dataset",
            publication_kind="catalog", outcome="committed",
            uri=receipt.publication.artifact_uri, table=intent.destination.name,
            version=receipt.publication.catalog_version, rows=receipt.rows,
            write_receipt=receipt)
        status = RunStatus(
            run_id=task.id, status="done", target_node_id=task.target_node_id,
            outputs=[output], total_rows=receipt.rows).model_dump()
        receipt_json = json.dumps(receipt.model_dump(by_alias=True, mode="json"), sort_keys=True)
        task.status = attempt.status = "done"
        task.status_doc = json.dumps(status, default=str)
        task.output_receipt = attempt.output_receipt = receipt_json
        task.completed_at = attempt.completed_at = task.updated_at = now
        wait.phase, wait.owner_token, wait.lease_until = "published", None, None
        wait.stage_dev = wait.stage_ino = None
        wait.download_evidence = None
        wait.updated_at = now
        _emit_durable_task_inbox_item(
            s, task=task, attempt=attempt, task_status="done", now=now)
        return True


def fail_external_wait_finalization(
        task_id: str, attempt_id: str, owner_token: str, code: str, *, permanent: bool) -> bool:
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        wait = s.get(DurableExternalWait, str(task_id), with_for_update=True)
        attempt = s.get(DurableTaskAttempt, str(attempt_id), with_for_update=True)
        now = _durable_task_db_now(s)
        lease = wait.lease_until if wait is not None else None
        if (task is None or wait is None or attempt is None or task.status in _TERMINAL_RUN
                or attempt.task_id != task.id or wait.phase not in ("downloading", "publishing")
                or wait.owner_token != str(owner_token) or lease is None
                or lease.replace(tzinfo=lease.tzinfo or datetime.timezone.utc) <= now):
            return False
        wait.owner_token = wait.lease_until = None
        wait.diagnostic_code = str(code)[:64]
        wait.next_poll_at = wait.updated_at = now
        if permanent:
            wait.stage_dev = wait.stage_ino = None
            wait.download_evidence = None
            _external_wait_terminal(
                s, task, attempt, wait, task_status="failed",
                phase="finalization_failed", code=wait.diagnostic_code)
        return True


def retry_durable_task(task_id: str, retry_request_id: str) -> dict:
    retry_request_id = str(retry_request_id).lower()
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        if task is None:
            raise KeyError(task_id)
        replay = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task.id,
            DurableTaskAttempt.retry_request_id == retry_request_id,
        ).limit(1))
        if replay is not None:
            return _durable_task_doc(s, task)
        if task.status not in ("failed", "cancelled"):
            raise ValueError("only a failed or cancelled durable task can be retried")
        last = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task.id,
        ).order_by(DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
        if last is None or last.attempt_number >= task.max_attempts:
            raise ValueError("durable task retry limit is exhausted")
        now = _durable_task_db_now(s)
        number = last.attempt_number + 1
        s.add(DurableTaskAttempt(
            id=uuid.uuid4().hex, task_id=task.id, attempt_number=number,
            execution_manifest_sha256=task.execution_manifest_sha256,
            retry_request_id=retry_request_id, status="queued", created_at=now))
        task.status = "queued"
        task.cancel_requested = False
        task.retry_count = number - 1
        task.progress = None
        task.error = None
        task.output_receipt = None
        task.completed_at = None
        task.updated_at = now
        task.status_doc = json.dumps(
            _task_status_doc(task.id, task.target_node_id), default=str)
        if task.task_kind == "external_wait":
            from hub.external_wait import ExternalWaitSubmitRequest
            wait = s.get(DurableExternalWait, task.id, with_for_update=True)
            if wait is None:
                raise RuntimeError("external-wait durable evidence is missing")
            prior = ExternalWaitSubmitRequest.model_validate_json(wait.submit_request)
            request = prior.model_copy(update={
                "idempotency_key": _external_wait_key(task.id, number)})
            wait.submit_request = request.model_dump_json()
            wait.idempotency_key = request.idempotency_key
            wait.handle_doc = wait.checkpoint_doc = None
            wait.download_evidence = None
            wait.stage_dev = wait.stage_ino = None
            wait.phase = "unsubmitted"
            wait.poll_count = 0
            wait.next_poll_at = now
            wait.deadline_at = now + datetime.timedelta(seconds=60)
            wait.diagnostic_code = None
            wait.owner_token = wait.lease_until = None
            wait.updated_at = now
        elif task.task_kind == "distribution_report":
            report = s.get(DistributionReportEnvelope, task.id, with_for_update=True)
            if report is None:
                raise RuntimeError("distribution report envelope is unavailable")
            report.report_doc = None
            report.completed_at = None
            report.updated_at = now
        s.flush()
        return _durable_task_doc(s, task)


def recoverable_durable_task_ids(limit: int = 100) -> list[str]:
    """Return queued or expired running managed-local tasks; live leased owners are left untouched."""
    with session() as s:
        now = _durable_task_db_now(s)
        latest_attempt = select(
            DurableTaskAttempt.task_id,
            func.max(DurableTaskAttempt.attempt_number).label("number"),
        ).group_by(DurableTaskAttempt.task_id).subquery()
        rows = s.scalars(select(DurableTask.id).join(
            latest_attempt, latest_attempt.c.task_id == DurableTask.id,
        ).join(DurableTaskAttempt, and_(
            DurableTaskAttempt.task_id == latest_attempt.c.task_id,
            DurableTaskAttempt.attempt_number == latest_attempt.c.number,
        )).where(
            DurableTask.task_kind == "managed_local_write",
            DurableTask.status.in_(("queued", "running")),
            or_(DurableTaskAttempt.status == "queued",
                DurableTaskAttempt.lease_until.is_(None),
                DurableTaskAttempt.lease_until <= now),
        ).order_by(DurableTask.created_at).limit(limit)).all()
        return [str(row) for row in rows]


def recoverable_linear_checkpoint_task_ids(limit: int = 100) -> list[str]:
    """Return queued or expired running linear-checkpoint tasks for the dedicated two-phase worker."""
    with session() as s:
        now = _durable_task_db_now(s)
        latest_attempt = select(
            DurableTaskAttempt.task_id,
            func.max(DurableTaskAttempt.attempt_number).label("number"),
        ).group_by(DurableTaskAttempt.task_id).subquery()
        rows = s.scalars(select(DurableTask.id).join(
            latest_attempt, latest_attempt.c.task_id == DurableTask.id,
        ).join(DurableTaskAttempt, and_(
            DurableTaskAttempt.task_id == latest_attempt.c.task_id,
            DurableTaskAttempt.attempt_number == latest_attempt.c.number,
        )).where(
            DurableTask.task_kind == "linear_checkpoint_write",
            DurableTask.status.in_(("queued", "running")),
            or_(DurableTaskAttempt.status == "queued",
                DurableTaskAttempt.lease_until.is_(None),
                DurableTaskAttempt.lease_until <= now),
        ).order_by(DurableTask.created_at).limit(limit)).all()
        return [str(row) for row in rows]


def recoverable_bounded_fanout_write_task_ids(limit: int = 100) -> list[str]:
    """Return queued or expired running bounded fan-out parents for the dedicated worker."""
    with session() as s:
        now = _durable_task_db_now(s)
        latest_attempt = select(
            DurableTaskAttempt.task_id,
            func.max(DurableTaskAttempt.attempt_number).label("number"),
        ).group_by(DurableTaskAttempt.task_id).subquery()
        rows = s.scalars(select(DurableTask.id).join(
            latest_attempt, latest_attempt.c.task_id == DurableTask.id,
        ).join(DurableTaskAttempt, and_(
            DurableTaskAttempt.task_id == latest_attempt.c.task_id,
            DurableTaskAttempt.attempt_number == latest_attempt.c.number,
        )).where(
            DurableTask.task_kind == "bounded_fanout_write",
            DurableTask.status.in_(("queued", "running")),
            or_(DurableTaskAttempt.status == "queued",
                DurableTaskAttempt.lease_until.is_(None),
                DurableTaskAttempt.lease_until <= now),
        ).order_by(DurableTask.created_at).limit(limit)).all()
        return [str(row) for row in rows]


def local_run_submission_id(uid: str, canvas_id: str | None, submission_id: str) -> str:
    canonical = "\x00".join(("local-run-submission-v1", str(uid), str(canvas_id or ""), str(submission_id).lower()))
    return f"run_{hashlib.sha256(canonical.encode()).hexdigest()[:48]}"


_LOCAL_FILE_INPUT_PROVIDER = "local-file-snapshot"


class LocalFileInputAdmissionRetry(RuntimeError):
    """A reusable local-file snapshot lost its ready lifecycle state before admission."""


def _admit_local_file_input_revisions(
        s, manifest: list[dict[str, str]], candidates: list[dict[str, str]]) -> None:
    """Publish candidate snapshot mappings and prove every local-file manifest binding."""
    if any(
            not isinstance(item, dict)
            or set(item) != {"dataset_id", "revision_id", "artifact_uri"}
            or any(not isinstance(value, str) or not value for value in item.values())
            for item in candidates):
        raise ValueError("local file input candidates are invalid")
    candidate_by_identity = {
        (item["dataset_id"], item["revision_id"]): item for item in candidates}
    required = sorted({
        (item["dataset_id"], item["revision_id"])
        for item in manifest if item["provider"] == _LOCAL_FILE_INPUT_PROVIDER})
    if any(identity not in required for identity in candidate_by_identity):
        raise ValueError("local file input candidate is absent from the manifest")
    if not required:
        return
    # Reclaim takes the lifecycle registry before changing an artifact or removing its mapping.
    # Serialize mapping adoption under the same lock so readmission cannot deadlock with delete by
    # taking the mapping first, and can replace a stale mapping whose old artifact is already deleting.
    _lock_local_result_registry(s)
    for dataset_id, revision_id in required:
        candidate = candidate_by_identity.get((dataset_id, revision_id))
        candidate_artifact = None
        if candidate is not None:
            candidate_artifact = s.get(
                LocalResultArtifact, candidate["artifact_uri"], with_for_update=True)
            if candidate_artifact is None or candidate_artifact.state != "ready":
                raise RuntimeError("local file input candidate is not an immutable ready artifact")
        binding = s.get(
            LocalFileInputRevision,
            {"dataset_id": dataset_id, "revision_id": revision_id},
            with_for_update=True,
        )
        current_artifact = (s.get(
            LocalResultArtifact, binding.artifact_uri, with_for_update=True)
            if binding is not None else None)
        if binding is None and candidate_artifact is not None:
            binding = LocalFileInputRevision(
                dataset_id=dataset_id,
                revision_id=revision_id,
                artifact_uri=candidate_artifact.uri,
                created_at=_db_now(s),
            )
            s.add(binding)
            s.flush()
        elif binding is not None and (current_artifact is None
                                      or current_artifact.state != "ready"):
            if candidate_artifact is not None:
                binding.artifact_uri = candidate_artifact.uri
                binding.created_at = _db_now(s)
                s.flush()
            else:
                # Snapshot lookup observed this mapping while it was ready, but reclaim won the
                # registry before admission could retain it. Roll back and let the caller materialize
                # one replacement; never publish an owner for a deleting or missing artifact.
                raise LocalFileInputAdmissionRetry(
                    "local file input snapshot changed lifecycle state during admission")
        if binding is None:
            raise LocalFileInputAdmissionRetry(
                "local file input snapshot mapping disappeared during admission")
        registration = s.scalars(select(CatalogEntry).where(
            CatalogEntry.registration_id == dataset_id).limit(1)).first()
        if registration is None:
            raise RuntimeError("local file input registration is unavailable")


def _promoted_transform_id(owner_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{owner_id}\0{key}".encode("utf-8")).hexdigest()
    return f"tr_{digest[:29]}"


def _canonical_promoted_transform_definition(
        *, title: str, blurb: str, category: str, mode: str, code: str,
        input_schema: list[ColumnSchema], output_schema: list[ColumnSchema],
        requirements: list[str]) -> tuple[str, str]:
    """Return one immutable definition's server-derived digest and canonical document."""
    from hub.promoted_transforms import promoted_transform_definition

    digest, doc = promoted_transform_definition(
        title=title, blurb=blurb, category=category, mode=mode, code=code,
        input_schema=input_schema, output_schema=output_schema, requirements=requirements)
    return digest, json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _promoted_transform_version_doc(row: PromotedTransformVersion) -> dict:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    return {
        "id": row.transform_id,
        "version": f"v{row.version}",
        "title": row.title,
        "blurb": row.blurb,
        "category": row.category,
        "mode": row.mode,
        "code": row.code,
        "input_schema": json.loads(row.input_schema),
        "output_schema": json.loads(row.output_schema),
        "requirements": json.loads(row.requirements),
        "creator_id": row.creator_id,
        "created_at": created_at,
        "semantic_digest": row.semantic_digest,
        "deleted_at": row.deleted_at,
    }


def promote_transform(
        *, owner_id: str, key: str, title: str, blurb: str, category: str,
        mode: str, code: str, input_schema: list[ColumnSchema],
        output_schema: list[ColumnSchema], requirements: list[str]) -> dict:
    """Append or idempotently reopen one immutable owner-scoped Transform version."""
    semantic_digest, canonical = _canonical_promoted_transform_definition(
        title=title, blurb=blurb, category=category, mode=mode, code=code,
        input_schema=input_schema, output_schema=output_schema, requirements=requirements)
    definition = json.loads(canonical)
    transform_id = _promoted_transform_id(owner_id, key)
    with session() as s:
        dialect = s.get_bind().dialect.name
        values = {
            "id": transform_id, "owner_id": owner_id, "key": key,
            "library_sort_key": transform_library_sort_key(definition["title"]),
            "created_at": _now(),
        }
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
            raise RuntimeError(f"unsupported metadata database dialect: {dialect}")
        s.execute(dialect_insert(PromotedTransform).values(
            **values,
        ).on_conflict_do_nothing(index_elements=[PromotedTransform.id]))
        s.flush()
        identity = s.get(
            PromotedTransform, transform_id, with_for_update=True, populate_existing=True)
        if identity is None or identity.owner_id != owner_id or identity.key != key:
            raise RuntimeError("promoted Transform identity collision")
        existing = s.scalar(select(PromotedTransformVersion).where(
            PromotedTransformVersion.transform_id == transform_id,
            PromotedTransformVersion.semantic_digest == semantic_digest,
        ).limit(1).with_for_update())
        if existing is not None:
            # A deleted immutable definition stays a tombstone and cannot be republished by replay.
            if existing.deleted_at is not None:
                raise ValueError("this promoted Transform definition was deleted and cannot be reused")
            return _promoted_transform_version_doc(existing)
        next_version = int(s.scalar(select(
            func.max(PromotedTransformVersion.version),
        ).where(PromotedTransformVersion.transform_id == transform_id)) or 0) + 1
        row = PromotedTransformVersion(
            transform_id=transform_id,
            version=next_version,
            semantic_digest=semantic_digest,
            title=definition["title"],
            library_search_text=transform_library_search_text(
                definition["title"], definition["blurb"], definition["category"],
                definition["mode"], transform_id),
            library_category_key=transform_library_text(definition["category"]),
            library_mode_key=transform_library_text(definition["mode"]),
            blurb=definition["blurb"],
            category=definition["category"],
            mode=definition["mode"],
            code=definition["code"],
            input_schema=json.dumps(definition["inputSchema"], separators=(",", ":")),
            output_schema=json.dumps(definition["outputSchema"], separators=(",", ":")),
            requirements=json.dumps(definition["requirements"], separators=(",", ":")),
            creator_id=owner_id,
            created_at=_now(),
        )
        s.add(row)
        s.flush()
        return _promoted_transform_version_doc(row)


def list_promoted_transforms(owner_id: str) -> list[dict]:
    """Return the newest active version of each owner-visible promoted identity."""
    with session() as s:
        rows = list(s.scalars(select(PromotedTransformVersion).join(
            PromotedTransform,
            PromotedTransform.id == PromotedTransformVersion.transform_id,
        ).where(
            PromotedTransform.owner_id == owner_id,
            PromotedTransformVersion.deleted_at.is_(None),
        ).order_by(
            PromotedTransformVersion.transform_id,
            PromotedTransformVersion.version.desc(),
        )))
        latest: dict[str, PromotedTransformVersion] = {}
        for row in rows:
            latest.setdefault(row.transform_id, row)
        return [_promoted_transform_version_doc(row) for row in latest.values()]


def promoted_transform_library_page(
        owner_id: str, *, q: str = "", mode: str = "", category: str = "",
        after_sort_key: str | None = None, after_id: str | None = None,
        limit: int = 26) -> list[dict]:
    """Return a stable bounded keyset page, selecting newest active or newest tombstone per identity."""
    bounded_limit = max(1, min(int(limit), 101))
    ranked = select(
        PromotedTransformVersion.transform_id.label("id"),
        PromotedTransformVersion.version,
        PromotedTransformVersion.semantic_digest,
        PromotedTransformVersion.title,
        PromotedTransform.library_sort_key,
        PromotedTransformVersion.library_search_text,
        PromotedTransformVersion.library_category_key,
        PromotedTransformVersion.library_mode_key,
        PromotedTransformVersion.blurb,
        PromotedTransformVersion.category,
        PromotedTransformVersion.mode,
        PromotedTransformVersion.input_schema,
        PromotedTransformVersion.output_schema,
        PromotedTransformVersion.requirements,
        PromotedTransformVersion.creator_id,
        PromotedTransformVersion.created_at,
        PromotedTransformVersion.deleted_at,
        func.row_number().over(
            partition_by=PromotedTransformVersion.transform_id,
            order_by=(
                PromotedTransformVersion.deleted_at.is_not(None).asc(),
                PromotedTransformVersion.version.desc(),
            ),
        ).label("selected_rank"),
        func.count().over(
            partition_by=PromotedTransformVersion.transform_id,
        ).label("version_count"),
    ).join(
        PromotedTransform,
        PromotedTransform.id == PromotedTransformVersion.transform_id,
    ).where(PromotedTransform.owner_id == str(owner_id)).subquery()
    sort_key = ranked.c.library_sort_key
    statement = select(ranked).where(ranked.c.selected_rank == 1)
    query = transform_library_text(q.strip())
    if query:
        statement = statement.where(
            ranked.c.library_search_text.contains(query, autoescape=True))
    if mode:
        statement = statement.where(
            ranked.c.library_mode_key == transform_library_text(mode.strip()))
    if category:
        statement = statement.where(
            ranked.c.library_category_key == transform_library_text(category.strip()))
    if after_sort_key is not None and after_id is not None:
        statement = statement.where(or_(
            sort_key > after_sort_key,
            and_(sort_key == after_sort_key, ranked.c.id > after_id),
        ))
    statement = statement.order_by(sort_key, ranked.c.id).limit(bounded_limit)
    with session() as s:
        result: list[dict] = []
        for row in s.execute(statement).mappings():
            created_at = row["created_at"]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)
            result.append({
                "id": row["id"], "version": f"v{row['version']}",
                "title": row["title"], "blurb": row["blurb"],
                "library_sort_key": row["library_sort_key"],
                "category": row["category"], "mode": row["mode"],
                "input_schema": json.loads(row["input_schema"]),
                "output_schema": json.loads(row["output_schema"]),
                "requirements": json.loads(row["requirements"]),
                "creator_id": row["creator_id"], "created_at": created_at,
                "semantic_digest": row["semantic_digest"],
                "deleted_at": row["deleted_at"],
                "version_count": int(row["version_count"]),
            })
        return result


def promoted_transform_library_detail(owner_id: str, transform_id: str) -> list[dict]:
    """Return every immutable version plus bounded retention counts for one owned identity."""
    with session() as s:
        identity = s.get(PromotedTransform, str(transform_id))
        if identity is None or identity.owner_id != str(owner_id):
            raise KeyError("promoted Transform not found")
        rows = list(s.scalars(select(PromotedTransformVersion).where(
            PromotedTransformVersion.transform_id == str(transform_id),
        ).order_by(PromotedTransformVersion.version.desc())))
        counts: dict[int, dict[str, int]] = {}
        for version, kind, count in s.execute(select(
            PromotedTransformVersionRef.version,
            PromotedTransformVersionRef.owner_kind,
            func.count(),
        ).where(
            PromotedTransformVersionRef.transform_id == str(transform_id),
        ).group_by(
            PromotedTransformVersionRef.version,
            PromotedTransformVersionRef.owner_kind,
        )):
            counts.setdefault(int(version), {})[str(kind)] = int(count)
        total = len(rows)
        result: list[dict] = []
        for row in rows:
            doc = _promoted_transform_version_doc(row)
            doc["version_count"] = total
            doc["retention"] = counts.get(row.version, {})
            result.append(doc)
        return result


def promoted_transform_library_cursor_key(
        owner_id: str, transform_id: str, version: str) -> str | None:
    """Resolve the immutable ordered key behind one exact promoted-version cursor."""
    number = _promoted_transform_version_number(version)
    if number is None:
        return None
    with session() as s:
        identity = s.get(PromotedTransform, str(transform_id))
        row = s.get(PromotedTransformVersion, (str(transform_id), number))
        if identity is None or identity.owner_id != str(owner_id) or row is None:
            return None
        return identity.library_sort_key


def canvas_transform_references(uid: str, canvas_id: str) -> list[dict]:
    """Resolve only the exact library refs already present in one readable Canvas."""
    with session() as s:
        canvas = s.get(Canvas, str(canvas_id))
        if canvas is None:
            raise KeyError(f"canvas '{canvas_id}' not found")
        if _workspace_canvas_role_in_session(s, canvas, str(uid)) is None:
            raise PermissionError("you don't have read access to this canvas")
        try:
            doc = json.loads(canvas.doc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"canvas '{canvas_id}' has invalid content") from exc
        refs: dict[tuple[str, str], list[str]] = {}
        for node in doc.get("nodes", []):
            if not isinstance(node, dict):
                continue
            data = node.get("data")
            cfg = data.get("config") if isinstance(data, dict) else None
            if not isinstance(cfg, dict) or cfg.get("source") != "library":
                continue
            processor, version = cfg.get("processor"), cfg.get("version")
            if isinstance(processor, str) and isinstance(version, str):
                refs.setdefault((processor, version), []).append(str(node.get("id", "")))
        result: list[dict] = []
        retained = {(str(pid), f"v{int(version)}") for pid, version in s.execute(select(
            PromotedTransformVersionRef.transform_id,
            PromotedTransformVersionRef.version,
        ).where(
            PromotedTransformVersionRef.owner_kind == "canvas",
            PromotedTransformVersionRef.owner_key == str(canvas_id),
        ))}
        for (processor, version), node_ids in sorted(refs.items()):
            item = {"id": processor, "version": version, "node_ids": node_ids}
            if processor.startswith("tr_"):
                number = _promoted_transform_version_number(version)
                row = s.get(PromotedTransformVersion, (processor, number)) if number else None
                identity = s.get(PromotedTransform, processor) if row is not None else None
                visible = identity is not None and (
                    identity.owner_id == str(uid) or (processor, version) in retained)
                if visible and row is not None:
                    item["descriptor"] = _promoted_transform_version_doc(row)
                    item["availability"] = "deleted" if row.deleted_at is not None else "active"
                else:
                    item["availability"] = "missing"
            result.append(item)
        return result


def _promoted_transform_version_number(version) -> int | None:
    from hub.promoted_transforms import promoted_transform_version_number
    return promoted_transform_version_number(version)


def promoted_transform_version(transform_id: str, version: str) -> dict | None:
    """Resolve one exact active promoted version; never substitute a different version."""
    number = _promoted_transform_version_number(version)
    if number is None:
        return None
    with session() as s:
        row = s.get(PromotedTransformVersion, (str(transform_id), number))
        if row is None or row.deleted_at is not None:
            return None
        return _promoted_transform_version_doc(row)


def _promoted_transform_refs(*values) -> set[tuple[str, int]]:
    refs: set[tuple[str, int]] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get("source") == "library":
                transform_id, raw_version = value.get("processor"), value.get("version")
                if isinstance(transform_id, str) and transform_id.startswith("tr_"):
                    version = _promoted_transform_version_number(raw_version)
                    if version is not None:
                        refs.add((transform_id, version))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for value in values:
        if hasattr(value, "model_dump"):
            value = value.model_dump(by_alias=True, mode="json")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        walk(value)
    return refs


def _replace_promoted_transform_refs(
        s, owner_kind: str, owner_key: str, *values) -> None:
    """Replace one durable owner's exact holds in the caller's transaction."""
    if owner_kind not in {"canvas", "canvas_version", "execution_manifest"}:
        raise ValueError("invalid promoted Transform reference owner")
    owner_key = str(owner_key)
    desired = _promoted_transform_refs(*values)
    existing = {(str(transform_id), int(version)) for transform_id, version in s.execute(select(
        PromotedTransformVersionRef.transform_id,
        PromotedTransformVersionRef.version,
    ).where(
        PromotedTransformVersionRef.owner_kind == owner_kind,
        PromotedTransformVersionRef.owner_key == owner_key,
    ))}
    # Global lock order shared with version deletion: exact version rows, sorted, then ref rows. Taking
    # the old+new union before changing owner refs prevents Canvas replacement from holding a ref while
    # waiting on a version that a concurrent tombstone owns (the inverse path deadlocked PostgreSQL).
    active: set[tuple[str, int]] = set()
    for transform_id, version in sorted(existing | desired):
        row = s.get(
            PromotedTransformVersion, (transform_id, version), with_for_update=True)
        if (transform_id, version) in desired and row is not None and row.deleted_at is None:
            active.add((transform_id, version))
    list(s.scalars(select(PromotedTransformVersionRef).where(
        PromotedTransformVersionRef.owner_kind == owner_kind,
        PromotedTransformVersionRef.owner_key == owner_key,
    ).order_by(
        PromotedTransformVersionRef.transform_id,
        PromotedTransformVersionRef.version,
    ).with_for_update()))
    s.execute(delete(PromotedTransformVersionRef).where(
        PromotedTransformVersionRef.owner_kind == owner_kind,
        PromotedTransformVersionRef.owner_key == owner_key,
    ))
    for transform_id, version in sorted(active):
        s.add(PromotedTransformVersionRef(
            owner_kind=owner_kind,
            owner_key=owner_key,
            transform_id=transform_id,
            version=version,
            created_at=_now(),
        ))
    s.flush()


def _drop_promoted_transform_refs(s, owner_kind: str, owner_key: str) -> None:
    s.execute(delete(PromotedTransformVersionRef).where(
        PromotedTransformVersionRef.owner_kind == owner_kind,
        PromotedTransformVersionRef.owner_key == str(owner_key),
    ))


def _require_promoted_transform_use_in_session(
        s, uid: str, graph_doc, *,
        retained_owner_kind: str | None = None,
        retained_owner_key: str | None = None) -> None:
    """Authorize exact refs by ownership or one already-authorized durable owner."""
    requested = _promoted_transform_refs(graph_doc)
    if not requested:
        return
    if (retained_owner_kind is None) != (retained_owner_key is None):
        raise ValueError("retained promoted Transform owner is incomplete")
    retained: set[tuple[str, int]] = set()
    if retained_owner_kind is not None:
        if retained_owner_kind not in {"canvas", "execution_manifest"}:
            raise ValueError("invalid promoted Transform authorization owner")
        # Authorization comes only from a hold established while the exact version existed. A raw
        # missing-version ref in a document is never a future capability after an owner publishes it.
        retained = {(str(transform_id), int(version))
                    for transform_id, version in s.execute(select(
                        PromotedTransformVersionRef.transform_id,
                        PromotedTransformVersionRef.version,
                    ).join(PromotedTransformVersion, and_(
                        PromotedTransformVersion.transform_id
                        == PromotedTransformVersionRef.transform_id,
                        PromotedTransformVersion.version
                        == PromotedTransformVersionRef.version,
                    )).where(
                        PromotedTransformVersionRef.owner_kind == retained_owner_kind,
                        PromotedTransformVersionRef.owner_key == str(retained_owner_key),
                        PromotedTransformVersion.deleted_at.is_(None),
                    ))}
    for transform_id, version in sorted(requested):
        row = s.get(PromotedTransformVersion, (transform_id, version))
        if row is None or row.deleted_at is not None:
            continue
        identity = s.get(PromotedTransform, transform_id)
        if identity is None:
            raise RuntimeError("promoted Transform version has no logical identity")
        if identity.owner_id != str(uid) and (transform_id, version) not in retained:
            raise PermissionError(
                f"promoted Transform {transform_id}@v{version} is not available to this user")


def require_promoted_transform_use(
        uid: str, graph_doc, *, canvas_id: str | None = None) -> None:
    """Authorize promoted exact refs by ownership or an already-visible retained Canvas ref."""
    with session() as s:
        retained_canvas_id: str | None = None
        if canvas_id:
            canvas = s.get(Canvas, str(canvas_id))
            if (canvas is not None
                    and _workspace_canvas_role_in_session(s, canvas, str(uid)) is not None):
                retained_canvas_id = str(canvas_id)
        _require_promoted_transform_use_in_session(
            s, uid, graph_doc,
            retained_owner_kind="canvas" if retained_canvas_id is not None else None,
            retained_owner_key=retained_canvas_id)


def require_retained_execution_manifest_transform_use(
        uid: str, canvas_id: str, subject_id: str,
        manifest_sha256: str, graph_doc) -> None:
    """Authorize only the exact retained manifest resolved through one visible run subject."""
    with session() as s:
        canvas = s.get(Canvas, str(canvas_id))
        if (canvas is None
                or _workspace_canvas_role_in_session(s, canvas, str(uid)) is None):
            raise KeyError(f"canvas '{canvas_id}' not found")
        found, identity = _execution_manifest_identity_for_subject_in_session(
            s, str(canvas_id), str(subject_id))
        if (not found or identity != str(manifest_sha256)
                or s.get(ExecutionManifest, str(manifest_sha256)) is None):
            raise WorkspaceVersionConflict(
                "retained execution manifest changed or became unavailable")
        _require_promoted_transform_use_in_session(
            s, uid, graph_doc,
            retained_owner_kind="execution_manifest",
            retained_owner_key=str(manifest_sha256))


def delete_promoted_transform_version(
        owner_id: str, transform_id: str, version: str) -> dict:
    """Tombstone one unreferenced version while retaining its non-reusable identity."""
    number = _promoted_transform_version_number(version)
    if number is None:
        raise KeyError("promoted Transform version not found")
    with session() as s:
        identity = s.get(PromotedTransform, str(transform_id), with_for_update=True)
        row = s.get(
            PromotedTransformVersion, (str(transform_id), number), with_for_update=True)
        if identity is None or row is None or identity.owner_id != owner_id:
            raise KeyError("promoted Transform version not found")
        if row.deleted_at is not None:
            return {"ok": True, "deleted": True}
        owners = list(s.execute(select(
            PromotedTransformVersionRef.owner_kind,
            PromotedTransformVersionRef.owner_key,
        ).where(
            PromotedTransformVersionRef.transform_id == transform_id,
            PromotedTransformVersionRef.version == number,
        ).order_by(
            PromotedTransformVersionRef.owner_kind,
            PromotedTransformVersionRef.owner_key,
        ).limit(20).with_for_update()))
        if owners:
            kinds = ", ".join(sorted({str(kind) for kind, _key in owners}))
            raise ValueError(
                f"promoted Transform {transform_id}@v{number} is retained by {kinds}")
        row.deleted_at = _now()
        return {"ok": True, "deleted": True}


def _persist_execution_manifest(s, sha256: str, semantic_doc: str) -> str:
    """Create or verify one immutable content-addressed execution definition."""
    from hub.execution_manifest import SCHEMA_VERSION, validate_execution_manifest

    manifest_doc = validate_execution_manifest(sha256, semantic_doc)
    values = {
        "sha256": str(sha256), "schema_version": SCHEMA_VERSION,
        "semantic_doc": semantic_doc, "created_at": _now(),
    }
    dialect = s.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        s.execute(dialect_insert(ExecutionManifest).values(
            **values,
        ).on_conflict_do_nothing(index_elements=[ExecutionManifest.sha256]))
        s.flush()
        existing = s.get(
            ExecutionManifest, str(sha256), with_for_update=True,
            populate_existing=True)
    else:  # pragma: no cover - supported production databases use native convergence
        existing = s.get(ExecutionManifest, str(sha256), with_for_update=True)
        if existing is None:
            existing = ExecutionManifest(**values)
            s.add(existing)
            s.flush()
    if (existing is None or existing.schema_version != SCHEMA_VERSION
            or existing.semantic_doc != semantic_doc):
        raise RuntimeError("execution manifest digest collision")
    _replace_promoted_transform_refs(
        s, "execution_manifest", str(sha256), manifest_doc)
    return str(sha256)


def execution_manifest(sha256: str) -> dict | None:
    """Return one validated manifest, or null for legacy/non-reconstructable history."""
    from hub.execution_manifest import validate_execution_manifest

    with session() as s:
        row = s.get(ExecutionManifest, str(sha256))
        if row is None:
            return None
        doc = validate_execution_manifest(row.sha256, row.semantic_doc)
        return {"sha256": row.sha256, "schema_version": row.schema_version, "document": doc}


def _execution_manifest_summary_from_row(
        sha256: str | None, schema_version: int | None, *, present: bool) -> dict:
    from hub.execution_manifest import SCHEMA_VERSION

    if sha256 is None:
        return {
            "executionManifestSha256": None,
            "executionManifestSchemaVersion": None,
            "executionManifestAvailability": "not_recorded",
            "executionManifestReconstructable": False,
        }
    if not present:
        return {
            "executionManifestSha256": sha256,
            "executionManifestSchemaVersion": None,
            "executionManifestAvailability": "pruned",
            "executionManifestReconstructable": False,
        }
    assert schema_version is not None
    available = schema_version == SCHEMA_VERSION
    return {
        "executionManifestSha256": sha256,
        "executionManifestSchemaVersion": schema_version,
        "executionManifestAvailability": "available" if available else "unavailable",
        "executionManifestReconstructable": available,
    }


def _execution_manifest_summaries(identities: set[str | None]) -> dict[str | None, dict]:
    """Read only digest/schema metadata for one bounded History or Jobs page."""
    normalized = {str(identity) for identity in identities if identity is not None}
    with session() as s:
        rows = {
            sha256: schema_version
            for sha256, schema_version in s.execute(select(
                ExecutionManifest.sha256, ExecutionManifest.schema_version,
            ).where(ExecutionManifest.sha256.in_(normalized))).all()
        } if normalized else {}
    return {
        identity: _execution_manifest_summary_from_row(
            identity, rows.get(identity), present=identity in rows)
        for identity in {*normalized, None}
    }


def _execution_manifest_identity_for_subject_in_session(
        s, canvas_id: str, subject_id: str) -> tuple[bool, str | None]:
    if subject_id.startswith("t:"):
        task = s.get(DurableTask, subject_id.removeprefix("t:"))
        return (task is not None and task.canvas_id == canvas_id,
                task.execution_manifest_sha256 if task is not None and task.canvas_id == canvas_id else None)
    if subject_id.startswith("s:"):
        state = s.get(RunState, subject_id.removeprefix("s:"))
        return (state is not None and state.canvas_id == canvas_id,
                state.execution_manifest_sha256 if state is not None and state.canvas_id == canvas_id else None)
    history = s.get(RunRecord, subject_id)
    return (history is not None and history.canvas_id == canvas_id,
            history.execution_manifest_sha256 if history is not None and history.canvas_id == canvas_id else None)


def execution_manifest_detail_for_subject(
        uid: str, canvas_id: str, subject_id: str) -> dict | None:
    """Resolve one History/Jobs subject under its current Canvas visibility.

    ``None`` means the Canvas itself is not visible. A missing subject on an otherwise visible Canvas
    is an explicit unavailable result, so callers never fall back to a live Canvas or another owner
    that happens to reference the same digest.
    """
    from hub.execution_manifest import (
        SCHEMA_VERSION,
        ExecutionManifestError,
        execution_manifest_admission,
        validate_execution_manifest,
    )

    with session() as s:
        canvas = s.get(Canvas, str(canvas_id))
        if canvas is None or _workspace_canvas_role_in_session(s, canvas, str(uid)) is None:
            return None

        found, identity = _execution_manifest_identity_for_subject_in_session(
            s, str(canvas_id), str(subject_id))

        if not found:
            return {
                "sha256": None, "schemaVersion": None,
                "availability": "unavailable", "document": None,
            }
        row = s.get(ExecutionManifest, identity) if identity is not None else None
        summary = _execution_manifest_summary_from_row(
            identity, row.schema_version if row is not None else None,
            present=row is not None)
        availability = summary["executionManifestAvailability"]
        document = None
        if availability == "available":
            assert row is not None and row.schema_version == SCHEMA_VERSION
            try:
                document = validate_execution_manifest(row.sha256, row.semantic_doc)
                execution_manifest_admission(row.sha256, row.semantic_doc)
                if not isinstance(document.get("descriptors"), dict):
                    raise ExecutionManifestError(
                        "execution manifest descriptor snapshot is invalid")
            except ExecutionManifestError:
                availability = "corrupt"
        return {
            "sha256": identity,
            "schemaVersion": row.schema_version if row is not None else None,
            "availability": availability,
            "document": document,
        }


def _execution_manifest_sha256_for_run_in_session(
        s, run_id: str, *, lock: bool = False) -> str | None:
    queries = [
        select(RunInputAdmission.execution_manifest_sha256).where(
            RunInputAdmission.run_id == str(run_id)),
        select(RunState.execution_manifest_sha256).where(
            RunState.run_id == str(run_id)),
        select(RunRecord.execution_manifest_sha256).where(
            RunRecord.run_id == str(run_id)),
        select(CatalogLineageFact.execution_manifest_sha256).where(
            CatalogLineageFact.run_id == str(run_id)),
        select(ManagedLocalFileRevision.execution_manifest_sha256).where(
            ManagedLocalFileRevision.run_id == str(run_id)),
        select(ManagedLocalLanceWriteReceipt.execution_manifest_sha256).where(
            ManagedLocalLanceWriteReceipt.run_id == str(run_id)),
        select(DurableTask.execution_manifest_sha256).where(
            DurableTask.id == str(run_id)),
        select(DurableTaskAttempt.execution_manifest_sha256).where(
            DurableTaskAttempt.task_id == str(run_id)),
        select(DurableTaskInboxItem.execution_manifest_sha256).where(
            DurableTaskInboxItem.task_id == str(run_id)),
    ]
    if lock:
        queries = [query.with_for_update() for query in queries]
    identities = {
        str(identity)
        for query in queries
        for identity in s.scalars(query)
        if identity is not None
    }
    if len(identities) > 1:
        raise RuntimeError("run manifest owners disagree on their execution manifest")
    return next(iter(identities), None)


def _retain_execution_manifest_for_run_in_session(s, run_id: str) -> str | None:
    """Lock one run's existing owners before extending its manifest retention to a new owner."""
    sha256 = _execution_manifest_sha256_for_run_in_session(s, str(run_id), lock=True)
    if sha256 is None:
        return None
    if s.get(ExecutionManifest, sha256, with_for_update=True) is None:
        raise RuntimeError("run manifest owner points to a missing execution manifest")
    return sha256


def execution_manifest_sha256_for_run(run_id: str) -> str | None:
    """Resolve the retained manifest identity across live, history, receipt, and lineage owners."""
    with session() as s:
        return _execution_manifest_sha256_for_run_in_session(s, str(run_id))


def _delete_unreferenced_execution_manifests(s, identities: set[str | None]) -> None:
    candidates = sorted({str(item) for item in identities if item})
    if not candidates:
        return
    for sha256 in candidates:
        row = s.get(ExecutionManifest, sha256, with_for_update=True)
        if row is None:
            continue
        referenced = any(s.scalar(select(exists().where(column == sha256))) for column in (
            RunInputAdmission.execution_manifest_sha256,
            RunState.execution_manifest_sha256,
            RunRecord.execution_manifest_sha256,
            CatalogLineageFact.execution_manifest_sha256,
            ManagedLocalFileRevision.execution_manifest_sha256,
            ManagedLocalLanceWriteReceipt.execution_manifest_sha256,
            DurableTask.execution_manifest_sha256,
            DurableTaskAttempt.execution_manifest_sha256,
            DurableTaskInboxItem.execution_manifest_sha256,
        ))
        if not referenced:
            _drop_promoted_transform_refs(s, "execution_manifest", sha256)
            s.delete(row)


def admit_local_run_inputs(*, uid: str, canvas_id: str | None, submission_id: str,
                           target_node_id: str | None, intent_sha256: str,
                           manifest: list[dict[str, str]],
                           execution_manifest_sha256: str | None = None,
                           execution_manifest_doc: str | None = None,
                           local_file_candidates: list[dict[str, str]] | None = None,
                           ) -> tuple[str, bool]:
    """Atomically retain one exact local admission, or adopt the identical prior submission.

    The manifest is validated as secret-free, ordered primitive evidence before it crosses the durable
    boundary.  A different intent under the same client submission id is a conflict, never a new run.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", str(intent_sha256)):
        raise ValueError("local run admission requires a SHA-256 intent")
    if not isinstance(manifest, list) or any(
            not isinstance(item, dict) or set(item) != {"node_id", "dataset_id", "revision_id", "provider", "resolved_at"}
            or any(not isinstance(value, str) or not value for value in item.values())
            for item in manifest):
        raise ValueError("local run input manifest is invalid")
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(payload.encode("utf-8")) > _RUN_INPUT_MANIFEST_MAX_BYTES:
        raise ValueError("local run input manifest exceeds the durable limit")
    uid, canvas = str(uid), str(canvas_id) if canvas_id is not None else None
    run_id = local_run_submission_id(uid, canvas, submission_id)
    with session() as s:
        if (execution_manifest_sha256 is None) != (execution_manifest_doc is None):
            raise ValueError("execution manifest identity and document must be supplied together")
        if canvas is not None and s.get(Canvas, canvas, with_for_update=True) is None:
            raise RuntimeError("local run canvas does not exist")
        if canvas is not None and s.get_bind().dialect.name == "sqlite":
            # SQLite ignores SELECT ... FOR UPDATE. Take its single writer lock before observing the
            # admission so concurrent first submissions converge instead of racing duplicate INSERTs.
            s.execute(update(Canvas).where(
                Canvas.id == canvas,
            ).values(updated_at=Canvas.updated_at))
        existing = s.get(RunInputAdmission, run_id, with_for_update=True)
        if existing is not None:
            if (existing.creator_id != uid or existing.canvas_id != canvas
                    or existing.target_node_id != target_node_id
                    or existing.intent_sha256 != intent_sha256
                    or (execution_manifest_sha256 is not None
                        and existing.execution_manifest_sha256 != execution_manifest_sha256)):
                raise RuntimeError("local run submission does not match its persisted admission")
            return run_id, False
        if execution_manifest_sha256 is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, str(execution_manifest_doc))
        _admit_local_file_input_revisions(
            s, manifest, list(local_file_candidates or []))
        s.add(RunInputAdmission(
            run_id=run_id, creator_id=uid, canvas_id=canvas,
            submission_id=str(submission_id).lower(), target_node_id=target_node_id,
            intent_sha256=intent_sha256, manifest=payload,
            execution_manifest_sha256=execution_manifest_sha256,
        ))
        s.flush()
        sync_local_result_owner(s, "run_input_admission", run_id, manifest)
        return run_id, True


def local_file_input_revision_artifact(dataset_id: str, revision_id: str) -> str | None:
    """Return one reusable ready artifact for a content-identified ordinary local input."""
    with session() as s:
        return s.scalar(select(LocalFileInputRevision.artifact_uri).join(
            LocalResultArtifact,
            LocalResultArtifact.uri == LocalFileInputRevision.artifact_uri,
        ).where(
            LocalFileInputRevision.dataset_id == str(dataset_id),
            LocalFileInputRevision.revision_id == str(revision_id),
            LocalResultArtifact.state == "ready",
        ).limit(1))


def local_file_input_revision_for_artifact(uri: str) -> dict | None:
    """Resolve a private snapshot artifact to its exact dataset/revision identity."""
    with session() as s:
        row = s.scalars(select(LocalFileInputRevision).where(
            LocalFileInputRevision.artifact_uri == str(uri)).limit(1)).first()
        if row is None:
            return None
        artifact = s.get(LocalResultArtifact, row.artifact_uri)
        if artifact is None or artifact.state != "ready":
            return None
        return {
            "dataset_id": row.dataset_id,
            "revision_id": row.revision_id,
            "artifact_uri": row.artifact_uri,
            "created_at": row.created_at,
        }


def local_run_input_manifest(run_id: str) -> list[dict[str, str]] | None:
    with session() as s:
        row = s.get(RunInputAdmission, str(run_id))
        if row is None:
            return None
        try:
            parsed = json.loads(row.manifest)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("persisted local run input manifest is invalid") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("persisted local run input manifest is invalid")
    return [dict(item) for item in parsed]


def local_run_input_admission(run_id: str) -> dict | None:
    """Return the immutable identity and manifest required by local execution transport."""
    with session() as s:
        row = s.get(RunInputAdmission, str(run_id))
        if row is None:
            return None
        admission = {
            "run_id": row.run_id,
            "canvas_id": row.canvas_id,
            "target_node_id": row.target_node_id,
            "execution_manifest_sha256": row.execution_manifest_sha256,
        }
        try:
            manifest = json.loads(row.manifest)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("persisted local run input manifest is invalid") from exc
    if not isinstance(manifest, list):
        raise RuntimeError("persisted local run input manifest is invalid")
    return {**admission, "manifest": [dict(item) for item in manifest]}


def claim_local_run_dispatch(*, run_id: str, uid: str, auth_canvas_id: str | None,
                             request_id: str | None) -> tuple[dict, bool]:
    """Claim one local admission before the runner can create a worker.

    The admission claim and its pollable queued status share one transaction. Once that transaction
    commits, a caller cannot distinguish a failure before ``runner.run`` from a lost response after it,
    so retries must adopt rather than dispatch a second worker.
    """
    from hub.models import RunStatus

    with session() as s:
        admission_hint = s.execute(select(RunInputAdmission.canvas_id).where(
            RunInputAdmission.run_id == str(run_id)
        )).one_or_none()
        if admission_hint is None:
            raise RuntimeError("local run admission was not persisted")
        _lock_authorized_run_canvas(s, admission_hint[0])
        row = s.get(RunInputAdmission, str(run_id), with_for_update=True)
        if row is None:
            raise RuntimeError("local run admission was deleted before dispatch")
        state = s.get(RunState, str(run_id), with_for_update=True)
        if row.dispatched_at is not None:
            if state is None:
                raise RuntimeError("claimed local run has no durable status")
            return json.loads(state.doc), False
        if state is not None:
            # A prior process may have created the runner state just before this write-ahead boundary.
            # Treat it as possibly dispatched: repeating a local worker is never safe.
            row.dispatched_at = _db_now(s)
            return json.loads(state.doc), False
        queued = RunStatus(
            run_id=str(run_id), status="queued", target_node_id=row.target_node_id,
            request_id=request_id,
        ).model_dump()
        s.add(RunState(
            run_id=str(run_id), canvas_id=row.canvas_id, status="queued",
            doc=json.dumps(queued, default=str), created_by=str(uid),
            auth_canvas_id=str(auth_canvas_id) if auth_canvas_id is not None else None,
            request_id=request_id,
            execution_manifest_sha256=row.execution_manifest_sha256,
        ))
        row.dispatched_at = _db_now(s)
        return queued, True


def fail_claimed_local_run_dispatch(run_id: str, error: str) -> dict:
    """Terminalize a claimed local run only after its in-process receipt is conclusively absent."""
    from hub.models import RunStatus

    failed_status: RunStatus | None = None
    canvas_id: str | None = None
    with session() as s:
        admission_hint = s.execute(select(RunInputAdmission.canvas_id).where(
            RunInputAdmission.run_id == str(run_id)
        )).one_or_none()
        if admission_hint is None:
            raise RuntimeError("local run admission was not persisted")
        _lock_authorized_run_canvas(s, admission_hint[0])
        admission = s.get(RunInputAdmission, str(run_id), with_for_update=True)
        state = s.get(RunState, str(run_id), with_for_update=True)
        if admission is None or admission.dispatched_at is None or state is None:
            raise RuntimeError("claimed local run has no durable status")
        status = RunStatus.model_validate(json.loads(state.doc))
        if status.status != "queued":
            return status.model_dump()
        failed_status = status.model_copy(update={
            "status": "failed", "error": str(error), "stalled": False,
        })
        failed = failed_status.model_dump()
        canvas_id = admission.canvas_id
        state.status = "failed"
        state.doc = json.dumps(failed, default=str)
        _record_terminal_fence(s, str(run_id), "failed")

    assert failed_status is not None
    # Reuse the normal terminal boundary after fencing the claim: it enforces bounded RunState
    # retention, while history retains the admission manifest and owns its normal pruning lifecycle.
    save_run_state(str(run_id), failed_status.model_dump(), canvas_id=canvas_id)
    record_run(
        canvas_id=canvas_id,
        target_node_id=failed_status.target_node_id,
        job_type=failed_status.job_type,
        status=failed_status.status,
        rows=failed_status.total_rows,
        ms=failed_status.ms,
        error=failed_status.error,
        outputs=[output.model_dump() for output in failed_status.outputs],
        per_node=[item.model_dump() for item in failed_status.per_node] or None,
        profile=failed_status.profile.model_dump() if failed_status.profile else None,
        run_id=str(run_id),
        request_id=failed_status.request_id,
    )
    return failed_status.model_dump()


def _upsert_run_record(s, *, canvas_id: str | None, target_node_id: str | None,
                       target_port_id: str | None,
                       job_type: str, status: str,
                       rows: int | None = None, ms: int | None = None, error: str | None = None,
                       outputs: list[dict] | None = None, per_node: list[dict] | None = None,
                       profile: dict | None = None,
                       run_id: str | None = None,
                       request_id: str | None = None,
                       execution_manifest_sha256: str | None = None,
                       execution_manifest_doc: str | None = None) -> bool:
    """Session-scoped history upsert shared by normal completion and backend publication."""
    if not canvas_id:
        return False
    from hub.models import RunHistoryRecord
    # One model owns the complete public history invariant before any row/ref/local-owner mutation.
    history = RunHistoryRecord.model_validate({
        "id": "validation",
        "run_id": run_id,
        "request_id": request_id,
        "job_type": job_type,
        "status": status,
        "target_node_id": target_node_id,
        "target_port_id": target_port_id,
        "rows": rows,
        "ms": ms,
        "error": error,
        "outputs": outputs if outputs is not None else [],
        "profile": profile,
        "per_node": per_node,
    })
    output_docs = [output.model_dump() for output in history.outputs]
    output_payload = json.dumps(output_docs, separators=(",", ":"), default=str)
    if len(output_payload.encode("utf-8")) > _RUN_RECORD_OUTPUTS_MAX_BYTES:
        raise ValueError(
            f"run history outputs exceed {_RUN_RECORD_OUTPUTS_MAX_BYTES} encoded bytes")
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
    if request_id and not rec.request_id:
        rec.request_id = request_id
    rec.target_node_id, rec.target_port_id, rec.job_type, rec.status = (
        history.target_node_id, history.target_port_id,
        history.job_type, history.status)
    rec.rows, rec.ms, rec.error = history.rows, history.ms, history.error
    admission = s.get(RunInputAdmission, str(run_id)) if run_id else None
    rec.input_manifest = admission.manifest if admission is not None else None
    state = s.get(RunState, str(run_id)) if run_id else None
    owner_manifest_sha256s = {
        identity for identity in (
            admission.execution_manifest_sha256 if admission is not None else None,
            state.execution_manifest_sha256 if state is not None else None,
        ) if identity is not None
    }
    if len(owner_manifest_sha256s) > 1:
        raise RuntimeError("run owners disagree on their execution manifest")
    retained_manifest_sha256 = next(iter(owner_manifest_sha256s), None)
    if execution_manifest_sha256 is not None:
        if (retained_manifest_sha256 is not None
                and retained_manifest_sha256 != execution_manifest_sha256):
            raise RuntimeError("run history does not match its execution manifest")
        if execution_manifest_doc is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, execution_manifest_doc)
        else:
            manifest_row = s.get(
                ExecutionManifest, execution_manifest_sha256, with_for_update=True)
            if manifest_row is None:
                raise RuntimeError("run history execution manifest does not exist")
        retained_manifest_sha256 = execution_manifest_sha256
    elif execution_manifest_doc is not None:
        raise ValueError("execution manifest document requires its identity")
    rec.execution_manifest_sha256 = retained_manifest_sha256
    rec.outputs = output_payload
    rec.profile = json.dumps(history.profile.model_dump(), default=str) if history.profile else None
    rec.per_node = (json.dumps(
        [item.model_dump() for item in history.per_node], default=str)
        if history.per_node else None)
    s.flush()
    stale = list(s.scalars(select(RunRecord).where(
        RunRecord.canvas_id == canvas_id, RunRecord.id != rid
    ).order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
      .offset(max(0, _RUN_HISTORY_MAX - 1)).with_for_update()))
    _replace_attempt_refs(
        s, "run_record", rid, _result_doc_refs({"outputs": output_docs}))
    stale_execution_manifests = {
        obj.execution_manifest_sha256 for obj in stale
    }
    for obj in stale:
        _replace_attempt_ref(s, "run_record", obj.id, None)
        if obj.run_id:
            admission = s.get(RunInputAdmission, obj.run_id, with_for_update=True)
            if admission is not None:
                s.delete(admission)
        s.delete(obj)
    if stale:
        s.flush()
        _delete_unreferenced_execution_manifests(s, stale_execution_manifests)
    if stale:
        _lock_local_result_registry(s)
    sync_local_result_owner(
        s, "run_record", rid, {"outputs": output_docs}, rec.input_manifest, rec.profile)
    for obj in stale:
        _drop_local_result_owner_locked(s, "run_record", obj.id)
        if obj.run_id:
            _drop_local_result_owner_locked(s, "run_input_admission", obj.run_id)
    return True


def record_run(canvas_id: str | None, target_node_id: str | None, job_type: str, status: str,
               target_port_id: str | None = None,
               rows: int | None = None, ms: int | None = None, error: str | None = None,
               outputs: list[dict] | None = None, per_node: list[dict] | None = None,
               profile: dict | None = None,
               run_id: str | None = None,
               request_id: str | None = None,
               execution_manifest_sha256: str | None = None,
               execution_manifest_doc: str | None = None) -> bool:
    """Persist a finished run under its canvas. No-op (returns False) without a real canvas — an ad-hoc
    API run or an internal region sub-run (graph id '_region'). Returns True when a row was written.
    Prunes this canvas's history to the newest _RUN_HISTORY_MAX rows so the local DB can't grow without
    bound (one row per run, forever) — mirrors the ResultCache / CanvasVersion caps.
    `request_id` (OPS-01) is the HTTP/WebSocket id that started the run."""
    if not canvas_id:
        return False
    with session() as s:
        return _upsert_run_record(
            s, canvas_id=canvas_id, target_node_id=target_node_id,
            target_port_id=target_port_id,
            job_type=job_type, status=status,
            rows=rows, ms=ms, error=error, outputs=outputs, per_node=per_node, profile=profile,
            run_id=run_id, request_id=request_id,
            execution_manifest_sha256=execution_manifest_sha256,
            execution_manifest_doc=execution_manifest_doc,
        )


def delete_canvas_cascade(canvas_id: str) -> None:
    """Delete a canvas and its children (shares, run history) — FKs don't cascade (SQLite FK off,
    Postgres would error), so clean them explicitly."""
    with session() as s:
        canvas = s.get(Canvas, canvas_id, with_for_update=True)
        if canvas is None:
            return
        active = s.scalar(
            select(RunState.run_id)
            .outerjoin(RunBackendJob, RunBackendJob.run_id == RunState.run_id)
            .where(
                or_(RunState.canvas_id == canvas_id, RunState.auth_canvas_id == canvas_id),
                or_(
                    RunState.preallocation_token.is_not(None),
                    and_(
                        RunBackendJob.run_id.is_not(None),
                        RunState.status.in_(("queued", "running")),
                    ),
                    and_(
                        RunState.job_type == "profile",
                        RunState.status.in_(("queued", "running")),
                    ),
                ),
            )
            .order_by(RunState.run_id).limit(1)
        )
        if active:
            raise ActiveBackendJobsError(
                f"canvas '{canvas_id}' has active run '{active}'; "
                "cancel it and wait for terminal status"
            )
        active_task = s.scalar(select(DurableTask.id).where(
            DurableTask.canvas_id == canvas_id,
            DurableTask.status.in_(("queued", "running")),
        ).order_by(DurableTask.id).limit(1))
        if active_task:
            raise ActiveBackendJobsError(
                f"canvas '{canvas_id}' has active durable task '{active_task}'; "
                "cancel it and wait for terminal status")
        shares = list(s.scalars(select(CanvasShare).where(
            CanvasShare.canvas_id == canvas_id
        ).order_by(CanvasShare.user_id).with_for_update()))
        runs = list(s.scalars(select(RunRecord).where(
            RunRecord.canvas_id == canvas_id
        ).order_by(RunRecord.id).with_for_update()))
        admissions = list(s.scalars(select(RunInputAdmission).where(
            RunInputAdmission.canvas_id == canvas_id
        ).order_by(RunInputAdmission.run_id).with_for_update()))
        durable_tasks = list(s.scalars(select(DurableTask).where(
            DurableTask.canvas_id == canvas_id,
        ).order_by(DurableTask.id).with_for_update()))
        durable_checkpoints = list(s.scalars(select(DurableCheckpoint).where(
            DurableCheckpoint.task_id.in_([task.id for task in durable_tasks]),
        ).order_by(DurableCheckpoint.task_id).with_for_update())) if durable_tasks else []
        durable_attempts = list(s.scalars(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id.in_([task.id for task in durable_tasks]),
        ).order_by(DurableTaskAttempt.task_id, DurableTaskAttempt.attempt_number).with_for_update()
        )) if durable_tasks else []
        durable_waits = list(s.scalars(select(DurableExternalWait).where(
            DurableExternalWait.task_id.in_([task.id for task in durable_tasks]),
        ).order_by(DurableExternalWait.task_id).with_for_update())) if durable_tasks else []
        durable_inbox = list(s.scalars(select(DurableTaskInboxItem).where(
            DurableTaskInboxItem.task_id.in_([task.id for task in durable_tasks]),
        ).order_by(DurableTaskInboxItem.task_id, DurableTaskInboxItem.id).with_for_update())
        ) if durable_tasks else []
        versions = list(s.scalars(select(CanvasVersion).where(
            CanvasVersion.canvas_id == canvas_id
        ).order_by(CanvasVersion.id).with_for_update()))
        run_states = list(s.scalars(select(RunState).where(
            (RunState.canvas_id == canvas_id) | (RunState.auth_canvas_id == canvas_id)
        ).order_by(RunState.run_id).with_for_update()))
        terminal_fences = list(s.scalars(select(RunTerminalFence).where(
            (RunTerminalFence.canvas_id == canvas_id)
            | (RunTerminalFence.auth_canvas_id == canvas_id)
        ).order_by(RunTerminalFence.run_id).with_for_update()))
        execution_manifest_identities = {
            row.execution_manifest_sha256
            for row in [
                *runs, *admissions, *run_states,
                *durable_tasks, *durable_attempts, *durable_inbox,
            ]
        }
        profile_retention = s.get(ProfileJobRetention, canvas_id, with_for_update=True)
        profile_latest = list(s.scalars(select(ProfileJobLatest).where(
            ProfileJobLatest.canvas_id == canvas_id
        ).order_by(
            ProfileJobLatest.target_node_id, ProfileJobLatest.plan_digest,
        ).with_for_update()))
        local_owners: list[tuple[str, str]] = [("canvas", canvas_id)]
        local_owners.extend(("durable_task", task.id) for task in durable_tasks)
        for sh in shares:
            s.delete(sh)
        for admission in admissions:
            local_owners.append(("run_input_admission", admission.run_id))
            s.delete(admission)
        for wait in durable_waits:
            s.delete(wait)
        for item in durable_inbox:
            s.delete(item)
        if durable_inbox:
            # Inbox rows FK to attempts; drop them before attempt deletion on Postgres.
            s.flush()
        parent_ids = [task.id for task in durable_tasks]
        if parent_ids or durable_checkpoints:
            # Drop fan-out / checkpoint LocalResult owners under the registry lock first.
            _lock_local_result_registry(s)
            if parent_ids:
                from hub import bounded_fanout as _bounded_fanout
                _bounded_fanout.purge_for_delete(s, parent_ids)
            task_by_id = {task.id: task for task in durable_tasks}
            for checkpoint in durable_checkpoints:
                _purge_linear_checkpoint_for_delete(s, task_by_id[checkpoint.task_id], checkpoint)
        for checkpoint in durable_checkpoints:
            s.delete(checkpoint)
        if durable_checkpoints:
            s.flush()
        for attempt in durable_attempts:
            s.delete(attempt)
        if durable_attempts or durable_waits:
            # No ORM relationship orders these explicit bulk objects; honor the FK on Postgres before
            # deleting their Task parents (SQLite's default FK mode would otherwise hide the bug).
            s.flush()
        for task in durable_tasks:
            s.delete(task)
        for r in runs:
            _replace_attempt_ref(s, "run_record", r.id, None)
            local_owners.append(("run_record", r.id))
            s.delete(r)
        for v in versions:
            local_owners.append(("canvas_version", v.id))
            _drop_promoted_transform_refs(s, "canvas_version", v.id)
            s.delete(v)
        for latest in profile_latest:
            local_owners.append(("profile_job", latest.run_id))
            s.delete(latest)
        if profile_retention is not None:
            s.delete(profile_retention)
        # also drop this canvas's run_states — else auth_canvas_id/canvas_id dangle into a reusable id
        # namespace and a later canvas re-created under the same id could re-grant its old runs (P0-AUTH-02)
        for rs in run_states:
            job = s.get(RunBackendJob, rs.run_id)
            if job is not None:
                s.delete(job)
            _replace_attempt_ref(s, "run_state", rs.run_id, None)
            local_owners.append(("run_state", rs.run_id))
            if rs.job_type == "profile":
                local_owners.append(("profile_job", rs.run_id))
            s.delete(rs)
        # Keep the opaque anti-resurrection fence, but sever every authorization handle into a deleted,
        # reusable canvas namespace. A replacement canvas must never inherit retained runs.
        for fence in terminal_fences:
            fence.created_by = None
            fence.auth_canvas_id = None
            fence.canvas_id = None
        _lock_local_result_registry(s)
        for owner_kind, owner_key in sorted(local_owners):
            _drop_local_result_owner_locked(s, owner_kind, owner_key)
        _drop_promoted_transform_refs(s, "canvas", canvas_id)
        s.delete(canvas)
        s.flush()
        _delete_unreferenced_execution_manifests(
            s, execution_manifest_identities)


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
        result = [{"id": r.id, "runId": r.run_id, "requestId": r.request_id,
                   "status": r.status, "targetNodeId": r.target_node_id,
                   "targetPortId": r.target_port_id, "jobType": r.job_type,
                   "rows": r.rows, "ms": r.ms, "error": r.error,
                   "outputs": json.loads(r.outputs),
                   "inputManifest": json.loads(r.input_manifest) if r.input_manifest else None,
                   "executionManifestSha256": r.execution_manifest_sha256,
                   "profile": json.loads(r.profile) if r.profile else None,
                   "perNode": json.loads(r.per_node) if r.per_node else None,
                   "createdAt": r.created_at.isoformat() if r.created_at else None}
                  for r in rows]
    summaries = _execution_manifest_summaries({
        item["executionManifestSha256"] for item in result})
    for item in result:
        item.update(summaries[item["executionManifestSha256"]])
    return result


def _workspace_run_cursor_encode(created_at: datetime.datetime, identity: str) -> str:
    stamp = created_at.isoformat()
    raw = json.dumps([stamp, identity], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _workspace_run_cursor_decode(cursor: str | None) -> tuple[datetime.datetime, str] | None:
    if cursor is None:
        return None
    if len(cursor) > 4096:
        raise ValueError("invalid Jobs cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        created_at = datetime.datetime.fromisoformat(value[0])
        if created_at.tzinfo is None:
            # SQLite returns timezone-aware columns as naive values; the metadata contract stores UTC.
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        identity = value[1]
    except (ValueError, TypeError, IndexError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("invalid Jobs cursor") from exc
    if (not isinstance(identity, str) or not identity
            or created_at.tzinfo is None or created_at.utcoffset() is None):
        raise ValueError("invalid Jobs cursor")
    return created_at, identity


def _workspace_utc_iso(value: datetime.datetime | None) -> str | None:
    """Serialize one metadata timestamp without letting SQLite-naive UTC become browser-local."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()


def _workspace_progress(value) -> float | None:
    """Keep malformed legacy status documents from turning one Jobs page into a 500."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= 1 else None


def _workspace_run_doc(row, *, canvas_name: str, state_doc: dict | None,
                       backend: str | None, backend_attempt: str | None,
                       state_updated_at: datetime.datetime | None,
                       source: str) -> tuple[tuple[datetime.datetime, str], dict]:
    """Normalize terminal history and live state without inventing another lifecycle."""
    if source == "history":
        outputs = json.loads(row.outputs)
        input_manifest = json.loads(row.input_manifest) if row.input_manifest else None
        profile = json.loads(row.profile) if row.profile else None
        per_node = json.loads(row.per_node) if row.per_node else None
        placement = str((state_doc or {}).get("placement") or ("distributed" if backend else "local"))
        profile_attempt = (state_doc or {}).get("profile_attempt_order")
        created_at = row.created_at or _now()
        identity = f"h:{row.id}"
        doc = {
            "id": row.id, "runId": row.run_id, "requestId": row.request_id,
            "jobType": row.job_type, "status": row.status,
            "targetNodeId": row.target_node_id, "targetPortId": row.target_port_id,
            "rows": row.rows, "ms": row.ms, "error": row.error,
            "progress": _workspace_progress((state_doc or {}).get("progress")),
            "inputManifest": input_manifest, "outputs": outputs, "profile": profile,
            "executionManifestSha256": row.execution_manifest_sha256,
            "executionManifestReconstructable": row.execution_manifest_sha256 is not None,
            "perNode": per_node, "createdAt": created_at.isoformat(),
            "updatedAt": _workspace_utc_iso(state_updated_at),
        }
    else:
        assert state_doc is not None
        outputs = state_doc.get("outputs") or []
        per_node = state_doc.get("per_node") or []
        placement = str(state_doc.get("placement") or ("distributed" if backend else "local"))
        profile_attempt = state_doc.get("profile_attempt_order")
        created_at = row.created_at or row.updated_at or _now()
        identity = f"s:{row.run_id}"
        doc = {
            "id": identity, "runId": row.run_id, "requestId": row.request_id,
            "jobType": state_doc.get("job_type") or row.job_type or "run",
            "status": row.status, "targetNodeId": state_doc.get("target_node_id") or row.target_node_id,
            "targetPortId": state_doc.get("target_port_id") or row.target_port_id,
            "rows": state_doc.get("total_rows"), "ms": state_doc.get("ms"),
            "progress": _workspace_progress(state_doc.get("progress")),
            "error": state_doc.get("error"), "inputManifest": None,
            "executionManifestSha256": row.execution_manifest_sha256,
            "executionManifestReconstructable": row.execution_manifest_sha256 is not None,
            "outputs": outputs, "profile": state_doc.get("profile"),
            "perNode": per_node or None, "createdAt": created_at.isoformat(),
            "updatedAt": _workspace_utc_iso(state_updated_at),
        }
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        doc["createdAt"] = created_at.isoformat()
    target = doc.get("targetNodeId")
    node_label = next((str(item.get("label")) for item in (doc.get("perNode") or [])
                       if isinstance(item, dict) and item.get("node_id") == target
                       and item.get("label")), None)
    backend_ref = (state_doc or {}).get("backend_ref") or {}
    effective_backend = str(
        backend or backend_ref.get("backend")
        or (placement if state_doc is not None else "unknown"))
    attempt = str(
        backend_ref.get("attempt_id") or backend_attempt
        or profile_attempt or doc.get("runId") or doc["id"])
    doc.update({
        "canvasId": row.canvas_id, "canvasName": canvas_name,
        "nodeLabel": node_label, "backend": effective_backend,
        "placement": placement, "attempt": attempt,
    })
    return (created_at, identity), doc


def _sanitized_checkpoint_jobs_view(
        task: DurableTask, checkpoint: DurableCheckpoint, status_doc: dict,
        *, can_retry: bool) -> dict:
    """Project one path-free checkpoint Jobs surface from SQL-authoritative state."""
    progress = status_doc.get("progress")
    try:
        progress_value = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress_value = None
    if task.status in _TERMINAL_RUN:
        phase = "terminal"
    elif checkpoint.phase == "committed":
        phase = "publishing" if progress_value is not None and progress_value >= 0.6 else "committed"
    elif checkpoint.phase == "reserved" or task.status == "running":
        phase = "materializing"
    else:
        phase = "pending"
    resume_eligible = bool(
        checkpoint.phase == "committed"
        and checkpoint.content_sha256
        and checkpoint.committed_bytes
        and task.status in ("queued", "running", "failed", "cancelled"))
    digest = checkpoint.content_sha256
    abbreviated = (f"{digest[:12]}…{digest[-8:]}" if isinstance(digest, str) and len(digest) == 64
                   else None)
    diagnosis = None
    error = task.error or status_doc.get("error")
    if isinstance(error, str) and "checkpoint_invalid" in error:
        diagnosis = "checkpoint_invalid"
    return {
        "phase": phase,
        "checkpointNodeId": checkpoint.checkpoint_node_id,
        "outputPortId": checkpoint.output_port_id,
        "committedAt": (checkpoint.committed_at.isoformat()
                        if checkpoint.committed_at is not None else None),
        "rows": checkpoint.committed_rows,
        "bytes": checkpoint.committed_bytes,
        "contentDigest": abbreviated,
        "resumeEligible": resume_eligible,
        "retryLabel": ("Retry from checkpoint" if can_retry and resume_eligible else None),
        "clientKey": f"checkpoint:{task.id}",
        "diagnosticCode": diagnosis,
    }


def _sanitized_bounded_fanout_jobs_view(
        s, task: DurableTask, checkpoint: DurableCheckpoint | None,
        status_doc: dict) -> dict:
    """Parent-only Jobs surface for bounded_fanout_write — no child/plan internals.

    Stage and checkpoint/gather aggregates are derived from SQL-authoritative rows.
    ``status_doc`` extras such as ``fanout_phase`` are not durable: ``update_durable_task_status``
    round-trips through ``RunStatus`` and drops unknown fields.
    """
    from hub import bounded_fanout as fanout

    plan = s.get(fanout.BoundedFanoutPlan, task.id)
    units = list(s.scalars(select(fanout.BoundedFanoutUnit).where(
        fanout.BoundedFanoutUnit.parent_task_id == task.id))) if plan is not None else []
    children = [unit for unit in units if unit.kind == "child"]
    gather = next((unit for unit in units if unit.kind == "gather"), None)

    partition_count = int(plan.partition_count) if plan is not None else None
    completed = sum(1 for unit in children if unit.status == "done")
    failed = sum(1 for unit in children if unit.status in ("failed", "cancelled"))
    checkpoint_committed = checkpoint is not None and checkpoint.phase == "committed"
    gather_done = gather is not None and gather.status == "done"
    gather_running = gather is not None and gather.status in ("claimed", "running")
    children_started = any(
        unit.status in ("claimed", "done", "failed", "cancelled") for unit in children)
    all_children_done = bool(children) and all(unit.status == "done" for unit in children)

    if task.status in _TERMINAL_RUN:
        stage = "terminal"
    elif gather_done:
        # Committed gather means publication (or receipt reconcile) remains.
        stage = "publishing"
    elif gather_running or all_children_done:
        stage = "gathering"
    elif children_started:
        stage = "running_partitions"
    elif plan is not None or checkpoint_committed:
        stage = "planning"
    else:
        stage = "checkpointing"

    if not checkpoint_committed:
        checkpoint_state = "pending"
    elif plan is not None:
        # Fan-out plan exists: checkpoint evidence is being consumed.
        checkpoint_state = "reused"
    else:
        checkpoint_state = "committed"

    if gather is None:
        gather_state = "pending"
    elif gather_done:
        gather_state = "committed"
    elif gather_running:
        gather_state = "running"
    else:
        gather_state = "pending"

    diagnosis = None
    error = task.error or status_doc.get("error")
    if isinstance(error, str):
        lowered = error.lower()
        if "checkpoint_invalid" in lowered:
            diagnosis = "checkpoint_invalid"
        elif "durable_task_attempts_exhausted" in lowered:
            diagnosis = "durable_task_attempts_exhausted"

    return {
        "stage": stage,
        "partitionCount": partition_count,
        "completedPartitions": completed,
        "failedPartitions": failed,
        "checkpoint": checkpoint_state,
        "gather": gather_state,
        "diagnosticCode": diagnosis,
    }


def resolve_checkpoint_full_result(task_id: str) -> dict | None:
    """Resolve an opaque checkpoint client key to the committed artifact under authorization."""
    with session() as s:
        task = s.get(DurableTask, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id))
        if (task is None or checkpoint is None
                or task.task_kind != "linear_checkpoint_write"
                or checkpoint.phase != "committed"
                or checkpoint.candidate_uri is None):
            return None
        return {
            "task_id": task.id,
            "owner_id": task.owner_id,
            "canvas_id": task.canvas_id,
            "node_id": checkpoint.checkpoint_node_id,
            "port_id": checkpoint.output_port_id,
            "uri": checkpoint.candidate_uri,
            "rows": checkpoint.committed_rows,
        }


def list_workspace_runs(
        uid: str, *, limit: int = 50, cursor: str | None = None,
        status: str | None = None, canvas_id: str | None = None,
        node_id: str | None = None, run_id: str | None = None,
        backend: str | None = None,
        recorded_after: datetime.datetime | None = None,
        recorded_before: datetime.datetime | None = None,
        text: str | None = None) -> dict:
    """Return one bounded keyset page across every canvas the caller can currently read."""
    if limit < 1 or limit > 100:
        raise ValueError("Jobs limit must be between 1 and 100")
    decoded = _workspace_run_cursor_decode(cursor)
    normalized_text = str(text or "").strip().lower()
    fetch_limit = limit * 2 + 1  # terminal-state/history overlap is excluded, but retain merge headroom
    candidates: list[tuple[tuple[datetime.datetime, str], dict]] = []
    with session() as s:
        # Keep visibility in the data query itself. A separate preflight list would open a revoke race
        # where a formerly visible canvas could still contribute one Jobs page.
        visible_canvas = or_(
            Canvas.owner_id == str(uid),
            Canvas.visibility.in_(("workspace", "workspace_view")),
            exists().where(and_(
                CanvasShare.canvas_id == Canvas.id,
                CanvasShare.user_id == str(uid),
            )),
        )
        if s.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB
            state_json = cast(RunState.doc, JSONB)
            state_placement = state_json.op("->>")("placement")
            state_backend_ref = state_json.op("->")("backend_ref").op("->>")("backend")
        else:
            state_placement = func.json_extract(RunState.doc, "$.placement")
            state_backend_ref = func.json_extract(RunState.doc, "$.backend_ref.backend")
        effective_backend = func.coalesce(
            RunBackendJob.backend, state_backend_ref, state_placement, literal("unknown"))
        history_identity = literal("h:") + RunRecord.id
        history_predicates = [visible_canvas]
        if canvas_id:
            history_predicates.append(RunRecord.canvas_id == canvas_id)
        if run_id:
            history_predicates.append(RunRecord.run_id == run_id)
        if status:
            history_predicates.append(RunRecord.status == status)
        if node_id:
            history_predicates.append(RunRecord.target_node_id == node_id)
        if recorded_after:
            history_predicates.append(RunRecord.created_at >= recorded_after)
        if recorded_before:
            history_predicates.append(RunRecord.created_at <= recorded_before)
        if backend:
            history_predicates.append(effective_backend == backend)
        if normalized_text:
            literal_text = normalized_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal_text}%"
            history_predicates.append(or_(
                func.lower(Canvas.name).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunRecord.target_node_id, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunRecord.error, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunRecord.run_id, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunRecord.per_node, "")).like(pattern, escape="\\"),
            ))
        if decoded:
            stamp, identity = decoded
            history_predicates.append(or_(
                RunRecord.created_at < stamp,
                and_(RunRecord.created_at == stamp, history_identity < identity),
            ))
        history_rows = s.execute(
            select(
                RunRecord, Canvas.name, RunState.doc, RunState.updated_at,
                RunBackendJob.backend, RunBackendJob.attempt_id,
            )
            .join(Canvas, Canvas.id == RunRecord.canvas_id)
            .outerjoin(RunState, and_(
                RunState.run_id == RunRecord.run_id,
                RunState.canvas_id == RunRecord.canvas_id,
            ))
            .outerjoin(RunBackendJob, RunBackendJob.run_id == RunRecord.run_id)
            .where(*history_predicates)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc()).limit(fetch_limit)
        ).all()
        for row, name, raw_state, state_updated_at, backend_name, backend_attempt in history_rows:
            state_doc = json.loads(raw_state) if raw_state else None
            candidates.append(_workspace_run_doc(
                row, canvas_name=name, state_doc=state_doc,
                backend=backend_name, backend_attempt=backend_attempt,
                state_updated_at=state_updated_at, source="history"))

        state_identity = literal("s:") + RunState.run_id
        state_predicates = [
            visible_canvas,
            ~exists().where(and_(
                RunRecord.canvas_id == RunState.canvas_id,
                RunRecord.run_id == RunState.run_id,
            )),
        ]
        if canvas_id:
            state_predicates.append(RunState.canvas_id == canvas_id)
        if run_id:
            state_predicates.append(RunState.run_id == run_id)
        if status:
            state_predicates.append(RunState.status == status)
        if node_id:
            state_predicates.append(RunState.target_node_id == node_id)
        if recorded_after:
            state_predicates.append(RunState.created_at >= recorded_after)
        if recorded_before:
            state_predicates.append(RunState.created_at <= recorded_before)
        if backend:
            state_predicates.append(effective_backend == backend)
        if normalized_text:
            literal_text = normalized_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal_text}%"
            state_predicates.append(or_(
                func.lower(Canvas.name).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunState.target_node_id, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(RunState.run_id, "")).like(pattern, escape="\\"),
                func.lower(RunState.doc).like(pattern, escape="\\"),
            ))
        if decoded:
            stamp, identity = decoded
            state_predicates.append(or_(
                RunState.created_at < stamp,
                and_(RunState.created_at == stamp, state_identity < identity),
            ))
        state_rows = s.execute(
            select(
                RunState, Canvas.name,
                RunBackendJob.backend, RunBackendJob.attempt_id,
            )
            .join(Canvas, Canvas.id == RunState.canvas_id)
            .outerjoin(RunBackendJob, RunBackendJob.run_id == RunState.run_id)
            .where(*state_predicates)
            .order_by(RunState.created_at.desc(), RunState.run_id.desc()).limit(fetch_limit)
        ).all()
        for row, name, backend_name, backend_attempt in state_rows:
            try:
                state_doc = json.loads(row.doc)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("persisted run state is invalid") from exc
            candidates.append(_workspace_run_doc(
                row, canvas_name=name, state_doc=state_doc,
                backend=backend_name, backend_attempt=backend_attempt,
                state_updated_at=row.updated_at, source="state"))

        task_identity = literal("t:") + DurableTask.id
        task_predicates = [
            visible_canvas,
            DurableTask.task_kind.notin_(_JOBS_HIDDEN_TASK_KINDS),
        ]
        if canvas_id:
            task_predicates.append(DurableTask.canvas_id == canvas_id)
        if run_id:
            task_predicates.append(DurableTask.id == run_id)
        if status:
            task_predicates.append(DurableTask.status == status)
        if node_id:
            task_predicates.append(DurableTask.target_node_id == node_id)
        if backend:
            task_predicates.append(DurableTask.backend_kind == backend)
        if recorded_after:
            task_predicates.append(DurableTask.created_at >= recorded_after)
        if recorded_before:
            task_predicates.append(DurableTask.created_at <= recorded_before)
        if normalized_text:
            literal_text = normalized_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal_text}%"
            task_predicates.append(or_(
                func.lower(Canvas.name).like(pattern, escape="\\"),
                func.lower(DurableTask.target_node_id).like(pattern, escape="\\"),
                func.lower(DurableTask.id).like(pattern, escape="\\"),
                func.lower(func.coalesce(DurableTask.error, "")).like(pattern, escape="\\"),
                func.lower(DurableTask.graph_doc).like(pattern, escape="\\"),
            ))
        if decoded:
            stamp, identity = decoded
            task_predicates.append(or_(
                DurableTask.created_at < stamp,
                and_(DurableTask.created_at == stamp, task_identity < identity),
            ))
        task_rows = s.execute(select(DurableTask, Canvas.name, Canvas.owner_id, Canvas.visibility).join(
            Canvas, Canvas.id == DurableTask.canvas_id,
        ).where(*task_predicates).order_by(
            DurableTask.created_at.desc(), DurableTask.id.desc()).limit(fetch_limit)).all()
        share_roles = {
            row.canvas_id: row.role
            for row in s.execute(select(CanvasShare.canvas_id, CanvasShare.role).where(
                CanvasShare.user_id == str(uid),
                CanvasShare.canvas_id.in_([task.canvas_id for task, *_rest in task_rows] or ["__none__"]),
            )).all()
        } if task_rows else {}
        for task, name, canvas_owner_id, canvas_visibility in task_rows:
            task_doc = _durable_task_doc(s, task, include_attempt_updates=True)
            status_doc = task_doc["status_doc"]
            created_at = task.created_at or _now()
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)
            attempts = task_doc["attempts"]
            latest = (attempts[-1] if attempts else {
                "id": "missing", "attempt_number": task.retry_count + 1,
                "status": "failed", "progress": None, "error": task.error,
                "started_at": None, "completed_at": task.completed_at,
            })
            per_node = status_doc.get("per_node") or []
            node_label = next((str(item.get("label")) for item in per_node
                               if item.get("node_id") == task.target_node_id
                               and item.get("label")), None)
            from types import SimpleNamespace
            role = _effective_canvas_role(
                SimpleNamespace(owner_id=canvas_owner_id, visibility=canvas_visibility),
                str(uid), share_roles.get(task.canvas_id))
            can_mutate = role in ("owner", "editor")
            can_retry = (
                can_mutate and task.status in ("failed", "cancelled")
                and latest["attempt_number"] < task.max_attempts)
            can_cancel = can_mutate and task.status in ("queued", "running")

            checkpoint_view = None
            fanout_view = None
            if task.task_kind == "linear_checkpoint_write":
                checkpoint = s.get(DurableCheckpoint, task.id)
                if checkpoint is not None:
                    checkpoint_view = _sanitized_checkpoint_jobs_view(
                        task, checkpoint, status_doc, can_retry=can_retry)
            elif task.task_kind == "bounded_fanout_write":
                checkpoint = s.get(DurableCheckpoint, task.id)
                fanout_view = _sanitized_bounded_fanout_jobs_view(
                    s, task, checkpoint, status_doc)
            # Final outputs stay reserved for the Write receipt; never surface checkpoint URIs.
            outputs = status_doc.get("outputs") or []
            if (task.task_kind in ("linear_checkpoint_write", "bounded_fanout_write")
                    and task.status != "done"):
                outputs = []
            candidates.append(((created_at, f"t:{task.id}"), {
                "id": f"t:{task.id}", "runId": task.id, "requestId": None,
                "jobType": "run", "status": task.status,
                "targetNodeId": task.target_node_id, "targetPortId": None,
                "rows": status_doc.get("total_rows"), "ms": status_doc.get("ms"),
                "progress": _workspace_progress(task.progress),
                "error": task.error, "inputManifest": task_doc["input_manifest"],
                "executionManifestSha256": task.execution_manifest_sha256,
                "executionManifestReconstructable": task.execution_manifest_sha256 is not None,
                "outputs": outputs, "profile": None,
                "perNode": per_node or None, "createdAt": created_at.isoformat(),
                "updatedAt": _workspace_utc_iso(task.updated_at),
                "canvasId": task.canvas_id, "canvasName": name, "nodeLabel": node_label,
                "backend": "local", "placement": "local", "attempt": latest["id"],
                "taskId": task.id,
                "taskAttempts": [{
                    "id": item["id"], "attemptNumber": item["attempt_number"],
                    "executionManifestSha256": item["execution_manifest_sha256"],
                    "executionManifestReconstructable": (
                        item["execution_manifest_sha256"] is not None),
                    "status": item["status"], "progress": _workspace_progress(item["progress"]),
                    "error": item["error"],
                    "startedAt": item["started_at"].isoformat() if item["started_at"] else None,
                    "completedAt": item["completed_at"].isoformat() if item["completed_at"] else None,
                    "updatedAt": _workspace_utc_iso(item["updated_at"]),
                } for item in attempts],
                "cancelRequested": task.cancel_requested,
                "canRetry": can_retry,
                "canCancel": can_cancel,
                "writeIntent": task_doc["write_intent"],
                "outputReceipt": task_doc["output_receipt"],
                "externalWait": ({
                    "providerKind": task_doc["external_wait"]["provider_kind"],
                    "phase": task_doc["external_wait"]["phase"],
                    "nextPollAt": task_doc["external_wait"]["next_poll_at"].isoformat(),
                    "deadlineAt": task_doc["external_wait"]["deadline_at"].isoformat(),
                    "pollCount": task_doc["external_wait"]["poll_count"],
                    "attemptNumber": latest["attempt_number"],
                    "cancelRequested": task.cancel_requested,
                    "canRetry": can_retry,
                    "diagnosticCode": task_doc["external_wait"]["diagnostic_code"],
                } if task.task_kind == "external_wait" and "external_wait" in task_doc else None),
                **({"checkpoint": checkpoint_view} if checkpoint_view is not None else {}),
                **({"boundedFanout": fanout_view} if fanout_view is not None else {}),
            }))

        # DatasetView reports are owner-scoped and deliberately have no Canvas. Project them through
        # their own bounded query so the existing Canvas authorization join cannot fabricate one.
        if not canvas_id and not node_id and (backend is None or backend == "local"):
            report_identity = literal("d:") + DurableTask.id
            report_predicates = [
                DurableTask.owner_id == str(uid),
                DurableTask.task_kind == "distribution_report",
            ]
            if run_id:
                report_predicates.append(DurableTask.id == run_id)
            if status:
                report_predicates.append(DurableTask.status == status)
            if recorded_after:
                report_predicates.append(DurableTask.created_at >= recorded_after)
            if recorded_before:
                report_predicates.append(DurableTask.created_at <= recorded_before)
            if normalized_text:
                literal_text = normalized_text.replace(
                    "\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{literal_text}%"
                report_predicates.append(or_(
                    func.lower(DurableTask.id).like(pattern, escape="\\"),
                    func.lower(DistributionReportEnvelope.report_id).like(
                        pattern, escape="\\"),
                    func.lower(DistributionReportEnvelope.dataset_view_id).like(
                        pattern, escape="\\"),
                    func.lower(DistributionReportEnvelope.view_snapshot_doc).like(
                        pattern, escape="\\"),
                ))
            if decoded:
                stamp, identity = decoded
                report_predicates.append(or_(
                    DurableTask.created_at < stamp,
                    and_(DurableTask.created_at == stamp, report_identity < identity),
                ))
            report_rows = s.execute(select(
                DurableTask, DistributionReportEnvelope,
            ).join(
                DistributionReportEnvelope,
                DistributionReportEnvelope.task_id == DurableTask.id,
            ).where(*report_predicates).order_by(
                DurableTask.created_at.desc(), DurableTask.id.desc(),
            ).limit(fetch_limit)).all()
            from hub.distribution_reports import DistributionReportDocumentV1
            from hub.models import DatasetViewDefinitionV1
            for task, envelope in report_rows:
                task_doc = _durable_task_doc(s, task, include_attempt_updates=True)
                attempts = task_doc["attempts"]
                latest = attempts[-1] if attempts else {
                    "id": "missing", "attempt_number": task.retry_count + 1,
                    "status": "failed", "progress": None, "error": task.error,
                    "started_at": None, "completed_at": task.completed_at,
                    "updated_at": task.updated_at,
                }
                view = DatasetViewDefinitionV1.model_validate_json(envelope.view_snapshot_doc)
                report = (DistributionReportDocumentV1.model_validate_json(envelope.report_doc)
                          if envelope.report_doc is not None else None)
                coverage = (next(
                    section for section in report.sections
                    if section.kind == "coverage_schema") if report is not None else None)
                created_at = task.created_at or _now()
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=datetime.timezone.utc)
                can_retry = (
                    task.status in ("failed", "cancelled")
                    and latest["attempt_number"] < task.max_attempts)
                candidates.append(((created_at, f"d:{task.id}"), {
                    "id": f"d:{task.id}", "runId": task.id, "requestId": None,
                    "jobType": "distribution_report", "status": task.status,
                    "targetNodeId": None, "targetPortId": None,
                    "rows": report.measured_rows if report is not None else None,
                    "ms": None, "progress": _workspace_progress(task.progress),
                    "error": task.error, "inputManifest": None,
                    "executionManifestSha256": None,
                    "executionManifestReconstructable": False,
                    "outputs": [], "profile": None, "perNode": None,
                    "createdAt": created_at.isoformat(),
                    "updatedAt": _workspace_utc_iso(task.updated_at),
                    "canvasId": None, "canvasName": None,
                    "nodeLabel": view.name, "backend": "local", "placement": "local",
                    "attempt": latest["id"], "taskId": task.id,
                    "taskAttempts": [{
                        "id": item["id"], "attemptNumber": item["attempt_number"],
                        "executionManifestSha256": None,
                        "executionManifestReconstructable": False,
                        "status": item["status"],
                        "progress": _workspace_progress(item["progress"]),
                        "error": item["error"],
                        "startedAt": item["started_at"].isoformat()
                        if item["started_at"] else None,
                        "completedAt": item["completed_at"].isoformat()
                        if item["completed_at"] else None,
                        "updatedAt": _workspace_utc_iso(item["updated_at"]),
                    } for item in attempts],
                    "cancelRequested": task.cancel_requested,
                    "canRetry": can_retry,
                    "canCancel": task.status in ("queued", "running"),
                    "writeIntent": None, "outputReceipt": None,
                    "externalWait": None,
                    "distributionReport": {
                        "reportId": envelope.report_id,
                        "datasetViewId": envelope.dataset_view_id,
                        "computationVersion": envelope.computation_version,
                        "measuredRows": report.measured_rows if report is not None else None,
                        "complete": report.complete if report is not None else None,
                        "reportedColumnCount": (
                            coverage.reported_column_count if coverage is not None else None),
                        "deepLink": f"/distribution-reports/{envelope.report_id}",
                    },
                }))
    candidates.sort(key=lambda item: item[0], reverse=True)
    page = candidates[:limit]
    has_more = len(candidates) > limit
    next_cursor = _workspace_run_cursor_encode(*page[-1][0]) if has_more and page else None
    summaries = _execution_manifest_summaries({
        doc.get("executionManifestSha256") for _key, doc in page})
    for _key, doc in page:
        doc.update(summaries[doc.get("executionManifestSha256")])
    return {"items": [doc for _key, doc in page],
            "nextCursor": next_cursor, "hasMore": has_more}


def get_run_record_outputs(run_id: str) -> list[dict] | None:
    """Return durable output snapshots by logical runner id, or ``None`` when no record exists.

    ``RunRecord.id`` is the history row identity exposed by the history list; it is deliberately not
    accepted here. Artifact APIs must remain keyed by the globally meaningful ``run_id`` so a UI row id
    can never accidentally become an authorization or storage lookup handle.
    """
    with session() as s:
        payload = s.scalar(
            select(RunRecord.outputs)
            .where(RunRecord.run_id == str(run_id))
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
            .limit(1)
        )
    if payload is None:
        return None
    try:
        outputs = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("durable run history output metadata is invalid") from exc
    if not isinstance(outputs, list) or any(not isinstance(output, dict) for output in outputs):
        raise RuntimeError("durable run history output metadata is invalid")
    return [dict(output) for output in outputs]


def get_run_record_output(run_id: str, node_id: str, port_id: str) -> dict | None:
    """Return one history output by logical run id and declared port identity."""
    outputs = get_run_record_outputs(run_id)
    if outputs is None:
        return None
    return next((output for output in outputs
                 if output.get("node_id") == node_id and output.get("port_id") == port_id), None)


_RUN_STATE_MAX = 2000  # cap on TERMINAL run_states — live (queued/running) rows are never pruned
_TERMINAL_RUN = ("done", "failed", "cancelled")
_PROFILE_LATEST_MAX = 100  # per canvas; each row retains one latest status document for reopen


class RunStatePublicationRejected(RuntimeError):
    """A definitive owner-row race loss, never an unknown database commit outcome."""


def _terminal_fence_status(s, run_id: str) -> str | None:
    return s.scalar(select(RunTerminalFence.status).where(RunTerminalFence.run_id == run_id))


def terminal_run_status(run_id: str) -> str | None:
    """Return the permanent terminal fence for ``run_id``, if one has been recorded."""
    with session() as s:
        return _terminal_fence_status(s, run_id)


def terminal_run_identity(
        run_id: str) -> tuple[str | None, str | None, str | None] | None:
    """Return retained ``(creator, auth canvas, operational canvas)`` identity for a terminal run."""
    with session() as s:
        row = s.execute(select(
            RunTerminalFence.created_by,
            RunTerminalFence.auth_canvas_id,
            RunTerminalFence.canvas_id,
        ).where(RunTerminalFence.run_id == str(run_id))).one_or_none()
        return tuple(row) if row is not None else None


def _record_terminal_fence(s, run_id: str, status: str) -> None:
    current = s.get(RunTerminalFence, str(run_id), with_for_update=True)
    if current is not None and current.status != status:
        raise RuntimeError(
            f"run '{run_id}' is permanently fenced as {current.status}, not {status}"
        )
    if current is None:
        identity = s.execute(select(
            RunState.created_by, RunState.auth_canvas_id, RunState.canvas_id,
            RunState.job_type, RunState.target_node_id, RunState.target_port_id,
            RunState.plan_digest,
            RunState.profile_attempt_order,
        ).where(RunState.run_id == str(run_id))).one_or_none()
        (created_by, auth_canvas_id, canvas_id, job_type, target_node_id, target_port_id,
         plan_digest, profile_attempt_order) = (
            identity if identity is not None
            else (None, None, None, "run", None, None, None, None)
        )
        s.add(RunTerminalFence(
            run_id=str(run_id), status=status, created_by=created_by,
            auth_canvas_id=auth_canvas_id, canvas_id=canvas_id,
            job_type=job_type, target_node_id=target_node_id,
            target_port_id=target_port_id,
            plan_digest=plan_digest, profile_attempt_order=profile_attempt_order,
        ))
        s.flush()


def _valid_plan_digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _upsert_profile_latest(
        s, *, canvas_id: str, target_node_id: str, target_port_id: str,
        plan_digest: str,
        run_id: str, payload: str, attempt_order: int,
        submitted_at: datetime.datetime) -> None:
    """Atomically advance one canvas/node/plan pointer and retain its latest status document.

    A status update for the current run refreshes the document. A newer submission replaces the pointer;
    a late update from an older run is ignored. The projection is pruned independently per canvas and is
    deliberately unaffected by global RunState detail retention.
    """
    canvas_id, target_node_id = str(canvas_id), str(target_node_id)
    target_port_id = str(target_port_id)
    plan_digest, run_id = str(plan_digest), str(run_id)
    attempt_order = int(attempt_order)
    if attempt_order < 1:
        raise ValueError("profile attempt order must be positive")
    now = _now()
    # This one row is the canvas-scoped mutex and DB sequence. It also retains the eviction watermark
    # that prevents an absent identity from being resurrected by delayed status from an older attempt.
    retention = _lock_profile_retention(s, canvas_id)
    current = s.get(
        ProfileJobLatest, (canvas_id, target_node_id, plan_digest),
        with_for_update=True, populate_existing=True,
    )
    retired_run_ids: set[str] = set()
    if current is None:
        if (retention.cutoff_attempt_order is not None
                and attempt_order <= retention.cutoff_attempt_order):
            return
        s.add(ProfileJobLatest(
            canvas_id=canvas_id, target_node_id=target_node_id,
            target_port_id=target_port_id,
            plan_digest=plan_digest, run_id=run_id, doc=payload,
            attempt_order=attempt_order,
            submitted_at=submitted_at, updated_at=now,
        ))
    elif current.run_id == run_id:
        if current.attempt_order != attempt_order:
            raise RuntimeError("profile run changed its durable attempt order")
        current.doc = payload
        current.updated_at = now
    elif attempt_order > current.attempt_order:
        retired_run_ids.add(current.run_id)
        current.run_id = run_id
        current.doc = payload
        current.attempt_order = attempt_order
        current.submitted_at = submitted_at
        current.updated_at = now
    else:
        return
    s.flush()
    stale = list(s.scalars(select(ProfileJobLatest).where(
        ProfileJobLatest.canvas_id == canvas_id,
    ).order_by(
        ProfileJobLatest.attempt_order.desc(),
    ).offset(_PROFILE_LATEST_MAX).with_for_update()))
    if stale:
        evicted_order = max(row.attempt_order for row in stale)
        if (retention.cutoff_attempt_order is None
                or evicted_order > retention.cutoff_attempt_order):
            retention.cutoff_attempt_order = evicted_order
            retention.updated_at = now
    for row in stale:
        retired_run_ids.add(row.run_id)
        s.delete(row)
    if retired_run_ids:
        _lock_local_result_registry(s)
        for retired_run_id in sorted(retired_run_ids - {run_id}):
            _drop_local_result_owner_locked(s, "profile_job", retired_run_id)


def save_run_state(run_id: str, status: dict, canvas_id: str | None = None,
                   kernel_id: str | None = None, *, publish_region: bool = False,
                   execution_manifest_sha256: str | None = None,
                   execution_manifest_doc: str | None = None) -> None:
    """Upsert a run's live status (the runner calls this on each transition). `status` is a RunStatus
    model_dump; stored whole as JSON so GET /run/{id} can rebuild it on any instance. `kernel_id`
    stamps the owning kernel so the boot-time reaper fails a run only when its kernel is gone. When a run
    reaches a terminal status, prunes finished run_states to the newest _RUN_STATE_MAX (each row holds a
    full RunStatus JSON, so unbounded growth is a real local-DB leak) — live rows are never touched, so
    the reaper and in-flight status lookups are unaffected. An evicted old run retains an authorized,
    synthetic terminal status through its compact identity fence, while detailed status fields are gone;
    durable per-canvas history remains separate."""
    from hub.models import RunStatus
    # Revalidate mutable in-memory models at the durable boundary.  This is where terminal pending
    # ports, stale singular fields, and inconsistent scalar row projections would otherwise become
    # restart-visible state even though response-model validation catches them later.
    status = RunStatus.model_validate(status).model_dump()
    st = str(status.get("status", "running"))
    output_refs = _result_doc_refs(status)
    output_uris = list(output_refs.values())
    has_managed_output = any(
        _local_result_candidate(uri) is not None or object_attempt_uri_shape(uri)
        for uri in output_uris)
    job_type = str(status.get("job_type", status.get("jobType", "run")))
    target_node_id = status.get("target_node_id", status.get("targetNodeId"))
    target_port_id = status.get("target_port_id", status.get("targetPortId"))
    plan_digest = status.get("plan_digest", status.get("planDigest"))
    profile_attempt_order = status.get(
        "profile_attempt_order", status.get("profileAttemptOrder"))
    if job_type == "profile" and (
            not target_node_id or not target_port_id or not _valid_plan_digest(plan_digest)
            or not isinstance(profile_attempt_order, int)
            or isinstance(profile_attempt_order, bool) or profile_attempt_order < 1):
        raise ValueError(
            "profile status requires target node/port, lowercase SHA-256 plan digest, "
            "and positive attempt order")
    with session() as s:
        if (execution_manifest_sha256 is None) != (execution_manifest_doc is None):
            raise ValueError("execution manifest identity and document must be supplied together")
        stale_candidate_ids: list[str] = []
        locked: dict[str, RunState] = {}
        if st not in _TERMINAL_RUN:
            # No retention rows participate in a progress update, so the current row can keep the
            # original direct lock. This also serializes a canvas cascade with every live update.
            r = s.get(RunState, run_id, with_for_update=True)
        else:
            existing_was_present = s.get(RunState, run_id) is not None
            if has_managed_output and not existing_was_present:
                # Managed publication must attach to the run identity minted before execution. Upserting
                # a missing row here could resurrect a canvas-deleted run and fabricate its first owner.
                raise RunStatePublicationRejected(
                    "managed result has no pre-existing run state")
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
        request_id = status.get("request_id") or status.get("requestId")
        fenced = _terminal_fence_status(s, run_id)
        if fenced is not None and (r is None or st != fenced):
            if job_type == "profile" or (r is not None and r.job_type == "profile"):
                raise RunStatePublicationRejected(
                    "profile terminal publication lost its permanent terminal fence race")
            if has_managed_output:
                # A strict managed-result publisher must distinguish a durable write from this
                # monotonic no-op. Otherwise it would release its writer and expose an in-memory URI
                # even though the permanent terminal winner owns no reference to that artifact set.
                raise RunStatePublicationRejected(
                    "managed terminal publication lost its permanent terminal fence race")
            return
        if r is not None and (job_type == "profile" or r.job_type == "profile"):
            if (job_type != "profile" or r.job_type != "profile"
                    or r.target_node_id != str(target_node_id)
                    or r.target_port_id != str(target_port_id)
                    or r.plan_digest != str(plan_digest)
                    or r.profile_attempt_order != profile_attempt_order):
                raise RunStatePublicationRejected(
                    "profile status does not match its preallocated identity")
            if canvas_id is None or str(canvas_id) != r.canvas_id:
                raise RunStatePublicationRejected(
                    "profile status was published for a different canvas")
            if r.preallocation_token is not None or r.kernel_id is None:
                raise RunStatePublicationRejected(
                    "profile status was published before kernel admission")
            if kernel_id != r.kernel_id:
                raise RunStatePublicationRejected(
                    "profile status was published by a different kernel")
        if (r is not None and execution_manifest_sha256 is not None
                and r.execution_manifest_sha256 not in (None, execution_manifest_sha256)):
            raise RunStatePublicationRejected(
                "run status does not match its execution manifest")
        # Existing lifecycle rows are locked before their content-addressed definition. Canvas cascade
        # and retention cleanup use the same owner-row -> manifest order, avoiding an inversion when a
        # progress callback races deletion or eviction.
        if execution_manifest_sha256 is not None:
            _persist_execution_manifest(
                s, execution_manifest_sha256, str(execution_manifest_doc))
        if r is None:
            if job_type == "profile":
                raise RunStatePublicationRejected(
                    "profile status has no preallocated run identity")
            r = RunState(run_id=run_id, canvas_id=canvas_id, status=st, doc=payload,
                         kernel_id=kernel_id, request_id=request_id,
                         job_type=job_type, target_node_id=target_node_id,
                         target_port_id=target_port_id,
                         plan_digest=plan_digest,
                         profile_attempt_order=profile_attempt_order,
                         execution_manifest_sha256=execution_manifest_sha256)
            s.add(r)
        else:
            # Make terminal monotonicity an UPDATE predicate, not a prior ORM read. A transaction can load
            # queued, pause while another supervisor atomically publishes done, then otherwise flush its
            # stale queued object over the terminal result.
            values = {"status": st, "doc": payload}
            if canvas_id:
                values["canvas_id"] = func.coalesce(RunState.canvas_id, canvas_id)
            if kernel_id:
                values["kernel_id"] = func.coalesce(RunState.kernel_id, kernel_id)
            if request_id:
                values["request_id"] = func.coalesce(RunState.request_id, request_id)
            if execution_manifest_sha256 is not None:
                values["execution_manifest_sha256"] = func.coalesce(
                    RunState.execution_manifest_sha256, execution_manifest_sha256)
            if job_type == "profile":
                # ``bind_run_owner`` may have pre-created a generic queued identity. The first profile
                # status promotes that placeholder once; later writes preserve the same node/plan key.
                values["job_type"] = "profile"
                if target_node_id is not None:
                    values["target_node_id"] = func.coalesce(
                        RunState.target_node_id, str(target_node_id))
                if target_port_id is not None:
                    values["target_port_id"] = func.coalesce(
                        RunState.target_port_id, str(target_port_id))
                if plan_digest is not None:
                    values["plan_digest"] = func.coalesce(
                        RunState.plan_digest, plan_digest)
                if profile_attempt_order is not None:
                    values["profile_attempt_order"] = func.coalesce(
                        RunState.profile_attempt_order, profile_attempt_order)
            updated = s.execute(update(RunState).where(
                RunState.run_id == run_id,
                or_(RunState.status.not_in(_TERMINAL_RUN), RunState.status == st),
                or_(plan_digest is None,
                    RunState.plan_digest.is_(None),
                    RunState.plan_digest == plan_digest),
                or_(target_node_id is None,
                    RunState.target_node_id.is_(None),
                    RunState.target_node_id == str(target_node_id)),
                or_(target_port_id is None,
                    RunState.target_port_id.is_(None),
                    RunState.target_port_id == str(target_port_id)),
                or_(profile_attempt_order is None,
                    RunState.profile_attempt_order.is_(None),
                    RunState.profile_attempt_order == profile_attempt_order),
            ).values(**values))
            if not updated.rowcount:
                if has_managed_output and st in _TERMINAL_RUN:
                    raise RunStatePublicationRejected(
                        "managed terminal publication did not match its durable run identity")
                return
        s.flush()
        if st in _TERMINAL_RUN:
            _record_terminal_fence(s, run_id, st)
        profile_canvas_id = r.canvas_id if job_type == "profile" else (canvas_id or r.canvas_id)
        if job_type == "profile" and profile_canvas_id is not None:
            _upsert_profile_latest(
                s,
                canvas_id=str(profile_canvas_id),
                target_node_id=str(target_node_id),
                target_port_id=str(target_port_id),
                plan_digest=str(plan_digest),
                run_id=str(run_id),
                payload=payload,
                attempt_order=int(profile_attempt_order),
                submitted_at=r.created_at or _now(),
            )
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
        _replace_attempt_refs(
            s, "run_state", run_id, output_refs,
            publish=bool(publish_region and st in ("done", "failed")),
            publish_kind="region")
        if st in _TERMINAL_RUN:
            stale_execution_manifests = {
                obj.execution_manifest_sha256 for obj in stale
            }
            for obj in stale:
                job = s.get(RunBackendJob, obj.run_id)
                if job is not None:
                    s.delete(job)
                _replace_attempt_ref(s, "run_state", obj.run_id, None)
                s.delete(obj)
            s.flush()
            _delete_unreferenced_execution_manifests(s, stale_execution_manifests)
            _lock_local_result_registry(s)
        # The RunState transaction is the primary local-result publication boundary. Object attempt
        # locks above always precede the local registry lock.
        sync_local_result_owner(s, "run_state", run_id, status)
        if st in ("done", "failed"):
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=False)
        if st in _TERMINAL_RUN:
            for obj in stale:
                _drop_local_result_owner_locked(s, "run_state", obj.run_id)


def local_result_run_state_receipt(
        uris: list[str], run_id: str, namespace_id: str, expected_doc: dict) -> bool:
    """Prove an exact managed-result terminal transaction committed.

    A database driver may raise after PostgreSQL committed.  Callers must not turn that unknown result
    into ``failed`` and delete a now-published artifact.  This read-back receipt validates every part of
    the same publication boundary: byte-for-byte RunState JSON, the exact durable reference, the ready
    artifact in this filesystem namespace, and release of the writer identity.  Connectivity errors are
    intentionally allowed to raise so they can never be mistaken for an authoritative negative answer.
    """
    if not namespace_id or not isinstance(expected_doc, dict):
        raise ValueError("local result receipt requires a namespace and status document")
    expected = dict(expected_doc)
    if str(expected.get("status")) not in ("done", "failed"):
        return False
    expected_payload = json.dumps(expected, default=str)
    supplied = [_local_result_candidate(uri) for uri in uris]
    if not supplied or any(uri is None for uri in supplied):
        return False
    supplied_set = {str(uri) for uri in supplied if uri is not None}
    if len(supplied_set) != len(supplied):
        return False
    expected_refs = _result_doc_refs(expected)
    expected_local = {
        candidate for uri in expected_refs.values()
        if (candidate := _local_result_candidate(uri)) is not None
    }
    if supplied_set != expected_local:
        return False
    with session() as s:
        state = s.get(RunState, str(run_id), with_for_update=True)
        if (state is None or state.status != str(expected.get("status"))
                or state.doc != expected_payload):
            return False
        expected_object_refs = {
            slot: uri for slot, uri in expected_refs.items()
            if object_attempt_uri_shape(uri)
        }
        object_refs = list(s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "run_state",
            ObjectAttemptRef.ref_key == str(run_id),
        ).order_by(ObjectAttemptRef.ref_slot)))
        if {ref.ref_slot: ref.attempt_uri for ref in object_refs} != expected_object_refs:
            return False
        object_uris = sorted(expected_object_refs.values())
        attempts = {attempt.uri: attempt for attempt in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(object_uris)).order_by(ObjectAttempt.uri))} \
            if object_uris else {}
        if (set(attempts) != set(object_uris)
                or any(attempts[ref.attempt_uri].generation != ref.generation
                       or attempts[ref.attempt_uri].state != "published"
                       for ref in object_refs)):
            return False
        # Owner rows precede the registry in the global lifecycle lock order.
        _lock_local_result_registry(s)
        local_refs = list(s.scalars(select(LocalResultReference).where(
            LocalResultReference.owner_kind == "run_state",
            LocalResultReference.owner_key == str(run_id),
        ).order_by(LocalResultReference.uri)))
        if {ref.uri for ref in local_refs} != expected_local:
            return False
        artifacts = {artifact.uri: artifact for artifact in s.scalars(
            select(LocalResultArtifact).where(
                LocalResultArtifact.uri.in_(sorted(expected_local)))
            .order_by(LocalResultArtifact.uri).with_for_update())}
        return bool(
            set(artifacts) == expected_local
            and all(
                artifact.namespace_id == namespace_id
                and artifact.state == "ready"
                and artifact.writer_run_id is None
                and artifact.writer_token is None
                for artifact in artifacts.values()))


def object_result_run_state_receipt(
        uris: list[str], run_id: str, expected_doc: dict) -> bool:
    """Prove an exact multi-port object-result RunState publication committed.

    A failed database response is commit-unknown. This receipt binds the byte-for-byte terminal
    document to its complete semantic port reference set and exact published attempt generations.
    """
    if not isinstance(expected_doc, dict):
        raise ValueError("object result receipt requires a status document")
    expected = dict(expected_doc)
    if str(expected.get("status")) not in ("done", "failed"):
        return False
    expected_payload = json.dumps(expected, default=str)
    supplied = [str(uri).strip().rstrip("/") for uri in uris]
    if (not supplied or any(not object_attempt_uri_shape(uri) for uri in supplied)
            or len(set(supplied)) != len(supplied)):
        return False
    expected_refs = {
        slot: uri for slot, uri in _result_doc_refs(expected).items()
        if object_attempt_uri_shape(uri)
    }
    if set(expected_refs.values()) != set(supplied):
        return False
    with session() as s:
        state = s.get(RunState, str(run_id), with_for_update=True)
        if (state is None or state.status != str(expected.get("status"))
                or state.doc != expected_payload):
            return False
        refs = list(s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "run_state",
            ObjectAttemptRef.ref_key == str(run_id),
        ).order_by(ObjectAttemptRef.ref_slot)))
        if {ref.ref_slot: ref.attempt_uri for ref in refs} != expected_refs:
            return False
        attempts = {attempt.uri: attempt for attempt in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(sorted(supplied))).order_by(ObjectAttempt.uri))}
        return bool(
            set(attempts) == set(supplied)
            and all(
                attempt.state == "published" and attempt.run_id == str(run_id)
                for attempt in attempts.values())
            and all(
                attempts[ref.attempt_uri].generation == ref.generation
                for ref in refs))


def get_run_state(run_id: str) -> dict | None:
    """The last-persisted RunStatus dict for a run, or None if unknown to this instance's DB."""
    with session() as s:
        r = s.get(RunState, run_id)
        return json.loads(r.doc) if r else None


def _backend_source_ref_prefix(run_id: str) -> str:
    return f"{str(run_id)}:"


def _backend_source_pins_in_session(
        s, run_id: str, *, lock: bool) -> list[tuple[ObjectAttemptRef, ObjectAttempt]]:
    """Read exact ordered source pins after the caller has locked the RunBackendJob owner row."""
    prefix = _backend_source_ref_prefix(run_id)
    refs_stmt = select(ObjectAttemptRef).where(
        ObjectAttemptRef.ref_type == "backend_source",
        ObjectAttemptRef.ref_key.startswith(prefix, autoescape=True),
    ).order_by(ObjectAttemptRef.ref_key)
    if lock:
        refs_stmt = refs_stmt.with_for_update()
    refs = list(s.scalars(refs_stmt))
    indexed: dict[int, ObjectAttemptRef] = {}
    for ref in refs:
        suffix = ref.ref_key[len(prefix):]
        if not suffix.isdigit() or int(suffix) in indexed:
            raise RuntimeError("backend source pin index is malformed")
        indexed[int(suffix)] = ref
    if sorted(indexed) != list(range(len(indexed))):
        raise RuntimeError("backend source pin order is incomplete")
    uris = sorted({ref.attempt_uri for ref in refs})
    attempts_stmt = select(ObjectAttempt).where(
        ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri)
    if lock:
        attempts_stmt = attempts_stmt.with_for_update()
    attempts = {attempt.uri: attempt for attempt in s.scalars(attempts_stmt)} if uris else {}
    pins: list[tuple[ObjectAttemptRef, ObjectAttempt]] = []
    for index in range(len(indexed)):
        ref = indexed[index]
        attempt = attempts.get(ref.attempt_uri)
        if (attempt is None or attempt.generation != ref.generation
                or attempt.state != "published"):
            raise RuntimeError("backend source pin no longer attests a published generation")
        pins.append((ref, attempt))
    return pins


def _bind_backend_source_pins(
        s, run_id: str, source_uris: list[str]) -> list[tuple[ObjectAttemptRef, ObjectAttempt]]:
    """Lock published source generations and create their run-scoped durable owner refs."""
    uris = sorted(set(source_uris))
    attempts = {attempt.uri: attempt for attempt in s.scalars(
        select(ObjectAttempt).where(ObjectAttempt.uri.in_(uris))
        .order_by(ObjectAttempt.uri).with_for_update())} if uris else {}
    pins: list[tuple[ObjectAttemptRef, ObjectAttempt]] = []
    for index, uri in enumerate(source_uris):
        attempt = attempts.get(uri)
        if attempt is None or attempt.state != "published":
            raise RuntimeError(
                "backend source must be an exact published managed object attempt")
        ref = ObjectAttemptRef(
            ref_type="backend_source", ref_key=f"{run_id}:{index}",
            ref_slot="",
            attempt_uri=attempt.uri, generation=attempt.generation,
        )
        s.add(ref)
        pins.append((ref, attempt))
    s.flush()
    return pins


def _release_backend_source_pins(
        s, pins: list[tuple[ObjectAttemptRef, ObjectAttempt]]) -> None:
    """Release source owners and retire generations whose last durable pointer disappeared."""
    if not pins:
        return
    attempts = {attempt.uri: attempt for _ref, attempt in pins}
    for ref, _attempt in pins:
        s.delete(ref)
    s.flush()
    now = _db_now(s)
    for uri in sorted(attempts):
        _maybe_supersede(s, attempts[uri], now)


def backend_source_pins(run_id: str) -> list[dict] | None:
    """Attest the exact ordered managed source generations for one recoverable backend job."""
    with session() as s:
        job = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if job is None:
            return None
        pins = _backend_source_pins_in_session(s, str(run_id), lock=True)
        return [
            {"uri": attempt.uri, "generation": ref.generation}
            for ref, attempt in pins
        ]


def bind_backend_job(run_id: str, ref: dict, status: dict,
                     canvas_id: str | None = None,
                     job_payload: bytes | None = None,
                     source_uris: list[str] | None = None) -> tuple[dict, bool]:
    """Atomically bind a logical run to one external attempt and its recoverable queued state.

    Returns ``(stored_ref, created)``. A caller whose deterministic attempt differs from ``stored_ref``
    must not submit: another request already owns this logical run id. The backend row and ``run_states``
    handoff commit together, so a process cannot die in between and leave a binding recovery cannot join.
    """
    if job_payload is not None:
        from hub.job_artifacts import JOB_SQL_ENVELOPE_MAX_BYTES

        if len(job_payload) > JOB_SQL_ENVELOPE_MAX_BYTES:
            raise ValueError(
                f"Ray durable SQL job envelope exceeds the {JOB_SQL_ENVELOPE_MAX_BYTES}-byte limit"
            )
    if source_uris is not None and not isinstance(source_uris, list):
        raise ValueError("backend source_uris must be an ordered list")
    normalized_sources = [
        _validated_object_uri(str(uri), attempt=True) for uri in (source_uris or [])
    ]
    if normalized_sources != sorted(set(normalized_sources)):
        raise ValueError("backend source_uris must be canonical, sorted, and unique")
    run_id = str(run_id)
    status = dict(status)
    if str(status.get("run_id") or "") != run_id:
        raise ValueError("backend run status does not match its run id")
    requested_canvas_id = str(canvas_id) if canvas_id is not None else None
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
        last_control_observed_at=None,
        job_doc=job_payload.decode("utf-8") if job_payload is not None else None,
    )
    def bind_operational_canvas(state: RunState) -> None:
        if (state.auth_canvas_id is not None
                and requested_canvas_id != state.auth_canvas_id):
            raise RuntimeError("backend run canvas does not match its authorization canvas")
        if requested_canvas_id is None:
            return
        if state.canvas_id is None:
            state.canvas_id = requested_canvas_id
        elif state.canvas_id != requested_canvas_id:
            raise RuntimeError("backend run canvas changed after identity allocation")

    try:
        with session() as s:
            state = _lock_existing_run_identity(
                s, run_id, requested_canvas_id=requested_canvas_id)
            if state is None or state.created_by is None:
                raise RuntimeError("backend run requires a preallocated owner identity")
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            bind_operational_canvas(state)
            existing = s.get(RunBackendJob, run_id, with_for_update=True)
            if existing is not None:
                pins = _backend_source_pins_in_session(s, run_id, lock=True)
                if [attempt.uri for _ref, attempt in pins] != normalized_sources:
                    raise RuntimeError("backend run source generations changed after binding")
                return _backend_job_doc(existing), False
            now = _db_now(s)
            if (state.status not in _PREALLOCATION_STATES
                    or state.preallocation_token is None
                    or not _run_preallocation_active(state.preallocation_expires_at, now)):
                raise RuntimeError("backend run requires a live preallocation lease")
            # Hub clocks may disagree across replicas. Start the external-control liveness grace period
            # from the metadata server's clock, which also owns every later observation and comparison.
            row.last_control_observed_at = now
            s.add(row)
            s.flush()
            _bind_backend_source_pins(s, run_id, normalized_sources)
            # The backend handoff consumes the preallocation in the same transaction. From this point a
            # boot reaper sees either the complete binding or the still-leased preallocation, never a gap.
            state.status = str(status.get("status") or "queued")
            state.doc = json.dumps(status, default=str)
            state.preallocation_token = None
            state.preallocation_expires_at = None
            return _backend_job_doc(row), True
    except IntegrityError:
        with session() as s:
            state = _lock_existing_run_identity(
                s, run_id, requested_canvas_id=requested_canvas_id)
            if state is None or state.created_by is None:
                raise
            fenced = _terminal_fence_status(s, run_id)
            if fenced is not None:
                raise TerminalRunIdError(f"run '{run_id}' is already terminal ({fenced})")
            bind_operational_canvas(state)
            existing = s.get(RunBackendJob, run_id, with_for_update=True)
            if existing is None:
                raise
            pins = _backend_source_pins_in_session(s, run_id, lock=True)
            if [attempt.uri for _ref, attempt in pins] != normalized_sources:
                raise RuntimeError("backend run source generations changed after binding")
            return _backend_job_doc(existing), False


def _backend_job_doc(row: RunBackendJob) -> dict:
    result = None
    publication_effects = None
    recovery_error = None
    if row.result_doc is not None:
        try:
            result = json.loads(row.result_doc)
            if not isinstance(result, dict):
                raise TypeError("stored backend result is not a JSON object")
        except (TypeError, ValueError):
            result = None
            recovery_error = "stored RunBackendJob.result_doc is not a valid JSON object"
    if row.publication_doc is not None:
        try:
            publication_effects = _decode_backend_publication_effects(
                row.publication_doc, row.run_id, row.attempt_id
            )
        except (TypeError, ValueError):
            publication_effects = None
            recovery_error = "stored RunBackendJob.publication_doc is not a valid effects plan"
    try:
        from hub.models import RunStatus

        if row.publication_state == "pending":
            if row.publication_doc is not None or row.result_doc is not None:
                raise ValueError("pending publication contains terminal documents")
        elif row.publication_state == _BACKEND_PUBLICATION_EFFECTS_STATE:
            if publication_effects is None or row.result_doc is not None:
                raise ValueError("effects_started publication has an invalid document pairing")
            _validate_backend_publication_effects_binding(row, publication_effects)
        elif row.publication_state == "published":
            if row.publication_doc is not None or result is None:
                raise ValueError("published backend has an invalid document pairing")
            final = RunStatus.model_validate(result)
            if (final.model_dump() != result or final.run_id != row.run_id
                    or final.status not in _TERMINAL_RUN):
                raise ValueError("published backend result is not a canonical terminal RunStatus")
            expected_payload = json.dumps(
                result, sort_keys=True, separators=(",", ":"), default=str)
            if row.result_doc != expected_payload:
                raise ValueError("published backend result is not canonically encoded")
        else:
            raise ValueError("backend publication has an unsupported state")
    except (TypeError, ValueError, RuntimeError) as exc:
        publication_effects = None if row.publication_state == \
            _BACKEND_PUBLICATION_EFFECTS_STATE else publication_effects
        result = None if row.publication_state == "published" else result
        pairing_error = f"stored RunBackendJob state/doc pairing is invalid: {exc}"
        recovery_error = (
            f"{recovery_error}; {pairing_error}" if recovery_error else pairing_error
        )
    doc = {
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
        "result": result,
        # A staged effects plan is private control state, never a canonical terminal result.
        "publication_effects": publication_effects,
    }
    if recovery_error:
        doc["_recovery_error"] = recovery_error
    return doc


def backend_job(run_id: str) -> dict | None:
    with session() as s:
        row = s.get(RunBackendJob, run_id)
        return _backend_job_doc(row) if row else None


def backend_job_artifact_payload(run_id: str) -> bytes | None:
    """Private canonical envelope bytes used only to recover the external job artifact."""
    with session() as s:
        row = s.get(RunBackendJob, run_id)
        return row.job_doc.encode("utf-8") if row and row.job_doc else None


def note_backend_control_observed(
        run_id: str, attempt_id: str, min_interval_s: float = 10.0) -> bool:
    """Throttle the durable liveness clock advanced only by successful backend observations."""
    with session() as s:
        now = _db_now(s)
        cutoff = now - datetime.timedelta(seconds=max(0.0, float(min_interval_s)))
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
    run_id, backend = str(run_id), str(backend)
    status = dict(status)
    bounded = str(reason)[:2000]
    with session() as s:
        canvas_id = s.scalar(select(RunState.canvas_id).where(
            RunState.run_id == run_id))
        state = _lock_existing_run_identity(
            s, run_id, requested_canvas_id=canvas_id)
        if state is None or state.status not in ("queued", "running"):
            return False
        job = s.get(RunBackendJob, run_id, with_for_update=True)
        if (job is None or job.backend != backend
                or job.publication_state == "published"):
            return False
        requested_status = str(status.get("status") or state.status)
        if requested_status not in ("queued", "running"):
            return False
        if status.get("run_id") not in (None, run_id):
            return False
        status["run_id"], status["status"] = run_id, requested_status
        job.recovery_blocked_reason = bounded
        state.status = requested_status
        state.doc = json.dumps(status, default=str)
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
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.publication_state == "pending",
            )
            .values(cancel_requested=True)
        )
        return bool(updated.rowcount)


def request_backend_quarantine(run_id: str, reason: str) -> bool:
    """Win the pre-effects corruption race, or reject a stale observer after effects started."""
    with session() as s:
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.publication_state == "pending",
            )
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
        now = _db_now(s)
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_state == "pending",
                or_(RunBackendJob.publication_owner.is_(None),
                    RunBackendJob.publication_lease_until.is_(None),
                    RunBackendJob.publication_lease_until < now),
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
                     submission_lease_until=lease,
                     publication_owner=None, publication_lease_until=None)
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
        now = _db_now(s)
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_state == "pending",
                RunBackendJob.submission_state == "submitting",
                RunBackendJob.submission_owner == owner,
            ).values(submission_lease_until=lease)
        )
        return bool(updated.rowcount)


def note_backend_submission_observed(run_id: str, attempt_id: str) -> bool:
    """Persist a visible remote winner and invalidate any pre-effects terminal candidate."""
    with session() as s:
        row = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if (row is None or row.attempt_id != str(attempt_id)
                or row.publication_state != "pending"
                or row.submission_state in (
                    "stop_fenced", "result_stop_fenced", "result_fenced")):
            return False
        if row.submission_state in _RESULT_RECONCILIATION_STATES:
            row.submission_state = "result_submitted"
        elif row.cancel_requested or row.quarantine_reason:
            # A live workload observed under durable stop intent must keep publication blocked until
            # the same control loop records terminal/absent proof after stop_job.
            row.submission_state = "stopping"
        else:
            row.submission_state = "submitted"
        row.submission_owner = row.submission_lease_until = None
        row.publication_owner = row.publication_lease_until = None
        return True


def claim_backend_stop_fence(run_id: str, attempt_id: str, owner: str,
                             lease_seconds: float = 30.0, *,
                             result_reconcile: bool = False) -> str:
    """Claim an expired uncertain submit so stop intent can reserve its deterministic Ray ID.

    The fixed remote fence job and a delayed original submit race on the same Ray submission ID; only
    one can be accepted. This turns an otherwise unknowable crashed-owner state into stoppable evidence.
    """
    with session() as s:
        now = _db_now(s)
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        intent = (
            RunBackendJob.submission_state.in_(("submitting", "result_fencing"))
            if result_reconcile else
            and_(
                or_(RunBackendJob.cancel_requested.is_(True),
                    RunBackendJob.quarantine_reason.is_not(None)),
                RunBackendJob.submission_state.in_(("submitting", "fencing")),
            )
        )
        updated = s.execute(update(RunBackendJob).where(
            RunBackendJob.run_id == run_id,
            RunBackendJob.attempt_id == attempt_id,
            RunBackendJob.publication_state == "pending",
            or_(RunBackendJob.publication_owner.is_(None),
                RunBackendJob.publication_lease_until.is_(None),
                RunBackendJob.publication_lease_until < now),
            intent,
            or_(RunBackendJob.submission_lease_until.is_(None),
                RunBackendJob.submission_lease_until < now),
        ).values(
            submission_state=("result_fencing" if result_reconcile else "fencing"),
            submission_owner=owner, submission_lease_until=lease,
            publication_owner=None, publication_lease_until=None,
        ))
        if updated.rowcount:
            return "claimed"
        row = s.get(RunBackendJob, run_id)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        if row.publication_state != "pending":
            return "busy"
        if result_reconcile:
            if row.submission_state in ("queued", "submitted"):
                return "not_needed"
            return "busy"
        if not (row.cancel_requested or row.quarantine_reason):
            return "lost"
        if row.submission_state == "queued":
            return "not_needed"
        if row.submission_state in ("submitted", "stop_fenced"):
            return "settled_missing"
        return "busy"


def note_backend_stop_fence_accepted(
        run_id: str, attempt_id: str, submission_owner: str | None = None) -> bool:
    """Record that the fixed stop fence, rather than the original workload, reserved the Ray ID."""
    with session() as s:
        row = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if (row is None or row.attempt_id != str(attempt_id)
                or row.publication_state != "pending"
                or row.submission_state not in (
                    "fencing", "fence_stopping", "stop_fenced",
                    "result_fencing", "result_stop_fenced",
                )
                or (submission_owner is not None
                    and row.submission_owner != submission_owner)):
            return False
        result_reconcile = row.submission_state in (
            "result_fencing", "result_stop_fenced")
        if not result_reconcile and not (row.cancel_requested or row.quarantine_reason):
            return False
        row.submission_state = (
            "result_stop_fenced" if result_reconcile else "fence_stopping")
        row.submission_owner = row.submission_lease_until = None
        row.publication_owner = row.publication_lease_until = None
        return True


def settle_backend_stop_control(run_id: str, attempt_id: str) -> bool:
    """Release the publication block only after a durable stop intent observes terminal/absence."""
    with session() as s:
        row = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if (row is None or row.attempt_id != str(attempt_id)
                or row.publication_state != "pending"
                or row.submission_state not in ("stopping", "fence_stopping")
                or not (row.cancel_requested or row.quarantine_reason)):
            return False
        row.submission_state = (
            "submitted" if row.submission_state == "stopping" else "stop_fenced"
        )
        row.submission_owner = row.submission_lease_until = None
        row.publication_owner = row.publication_lease_until = None
        return True


def settle_backend_result_reconciliation(run_id: str, attempt_id: str) -> bool:
    """Clear the durable result fence only after the caller observed its remote winner terminal."""
    with session() as s:
        row = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if (row is None or row.attempt_id != str(attempt_id)
                or row.publication_state != "pending"
                or row.submission_state not in ("result_submitted", "result_stop_fenced")):
            return False
        row.submission_state = (
            "submitted" if row.submission_state == "result_submitted" else "result_fenced"
        )
        row.submission_owner = row.submission_lease_until = None
        row.publication_owner = row.publication_lease_until = None
        return True


def note_unhandled_backend_jobs(available_backends: set[str]) -> int:
    """Keep external runs live but make a missing plugin/configuration visible after process restart."""
    available_backends = {str(backend) for backend in available_backends}
    with session() as s:
        candidate = (
            select(
                RunState.run_id, RunState.canvas_id, RunState.auth_canvas_id,
            )
            .join(RunBackendJob, RunBackendJob.run_id == RunState.run_id)
            .where(
                RunState.status.in_(("queued", "running")),
                RunBackendJob.publication_state != "published",
            )
            .order_by(RunState.run_id)
        )
        if available_backends:
            candidate = candidate.where(
                RunBackendJob.backend.not_in(available_backends))
        hints = {
            str(run_id): (
                str(canvas_id) if canvas_id is not None else None,
                str(auth_canvas_id) if auth_canvas_id is not None else None,
            )
            for run_id, canvas_id, auth_canvas_id in s.execute(candidate)
        }
        if not hints:
            return 0

        canvas_ids = sorted({
            canvas_id
            for canvas_id, auth_canvas_id in hints.values()
            for canvas_id in (canvas_id, auth_canvas_id)
            if canvas_id is not None
        })
        locked_canvases = {
            canvas.id: canvas for canvas in s.scalars(select(Canvas).where(
                Canvas.id.in_(canvas_ids)
            ).order_by(Canvas.id).with_for_update())
        } if canvas_ids else {}
        run_ids = sorted(hints)
        states = {
            state.run_id: state for state in s.scalars(select(RunState).where(
                RunState.run_id.in_(run_ids)
            ).order_by(RunState.run_id).with_for_update())
        }
        jobs = {
            job.run_id: job for job in s.scalars(select(RunBackendJob).where(
                RunBackendJob.run_id.in_(run_ids)
            ).order_by(RunBackendJob.run_id).with_for_update())
        }

        changed = 0
        for run_id in run_ids:
            state, job = states.get(run_id), jobs.get(run_id)
            canvas_id, auth_canvas_id = hints[run_id]
            if (state is None or job is None
                    or (state.canvas_id, state.auth_canvas_id) != (
                        canvas_id, auth_canvas_id)
                    or state.status not in ("queued", "running")
                    or job.publication_state == "published"
                    or job.backend in available_backends):
                continue
            if auth_canvas_id is not None and auth_canvas_id not in locked_canvases:
                continue
            try:
                doc = json.loads(state.doc)
                if not isinstance(doc, dict):
                    raise TypeError("stored RunStatus is not an object")
            except (TypeError, ValueError):
                doc = {"per_node": []}
            doc["run_id"], doc["status"] = state.run_id, state.status
            if not isinstance(doc.get("per_node"), list):
                doc["per_node"] = []
            unavailable_error = (
                f"durable backend '{job.backend}' is unavailable in this process; "
                "restore its plugin and control-plane configuration to reattach or cancel"
            )
            doc["error"] = unavailable_error
            try:
                from hub.models import RunStatus
                doc = RunStatus.model_validate(doc).model_dump()
            except (TypeError, ValueError):
                doc = {
                    "run_id": state.run_id, "status": state.status,
                    "per_node": [], "error": unavailable_error,
                }
            state.doc = json.dumps(doc, default=str)
            changed += 1
        return changed


_BACKEND_PUBLICATION_EFFECTS_STATE = "effects_started"
_BACKEND_PUBLICATION_EFFECTS_VERSION = 1
_BACKEND_PUBLICATION_EFFECTS_FIELDS = {
    "contract_version", "run_id", "attempt_id", "terminal_status", "validated_result",
    "sink_attempts", "catalog_effects", "usage_effect",
}
_ACTIVE_BACKEND_SINK_STATES = ("allocated", "writing", "committed")


class BackendPublicationConflict(RuntimeError):
    """A newer catalog generation or unregister won before terminal effects linearized."""


class BackendPublicationBusy(RuntimeError):
    """Another staged backend publication temporarily owns the same logical dataset."""


def _validated_backend_publication_effects(
        run_id: str, attempt_id: str, terminal_status: dict,
        validated_result: dict | None, sink_attempts: dict,
        catalog_effects: list[dict], usage_effect: dict | None) -> tuple[dict, str]:
    """Validate and canonically encode one immutable terminal-effect recovery plan."""
    from hub.job_artifacts import (
        JOB_SQL_ENVELOPE_MAX_BYTES, RAY_JOB_CONTRACT_VERSION, RAY_JOB_RESULT_FIELDS,
    )
    from hub.models import RunStatus
    from hub.run_outputs import sole_output

    run_id, attempt_id = str(run_id), str(attempt_id)
    if not isinstance(terminal_status, dict) or set(terminal_status) != set(RunStatus.model_fields):
        raise ValueError("backend publication terminal_status has an invalid field set")
    candidate = RunStatus.model_validate(terminal_status)
    if candidate.model_dump() != terminal_status:
        raise ValueError("backend publication terminal_status is not canonical")
    if candidate.run_id != run_id or candidate.status not in _TERMINAL_RUN:
        raise ValueError("backend publication terminal_status is not this run's terminal candidate")
    if (candidate.backend_ref is None
            or candidate.backend_ref.attempt_id != attempt_id
            or not candidate.backend_ref.durable):
        raise ValueError("backend publication terminal_status has no exact durable attempt")
    public_output = sole_output(candidate, committed=True)
    if candidate.status == "done" and public_output is None:
        raise ValueError("successful backend publication requires one committed RunOutput")
    if candidate.status != "done" and public_output is not None:
        raise ValueError("negative backend publication cannot expose a committed output")

    if (not isinstance(sink_attempts, dict)
            or any(not isinstance(step_id, str) or not step_id
                   or not isinstance(uri, str) or not uri
                   for step_id, uri in sink_attempts.items())):
        raise ValueError("backend publication sink_attempts must be an exact step-to-uri object")
    canonical_sinks = {
        step_id: _validated_object_uri(uri, attempt=True)
        for step_id, uri in sorted(sink_attempts.items())
    }
    output_by_step: dict[str, dict] = {}
    if candidate.status == "done":
        if (not isinstance(validated_result, dict)
                or set(validated_result) != set(RAY_JOB_RESULT_FIELDS)):
            raise ValueError("successful backend publication requires an exact validated result")
        if (validated_result.get("contract_version") != RAY_JOB_CONTRACT_VERSION
                or validated_result.get("status") != candidate.status
                or validated_result.get("attempt_id") != attempt_id
                or validated_result.get("submission_id") != candidate.backend_ref.submission_id):
            raise ValueError("backend publication validated_result has an invalid attempt contract")
        assert public_output is not None
        if (validated_result.get("rows") != candidate.total_rows
                or validated_result.get("rows") != public_output.rows
                or validated_result.get("output_uri") != public_output.uri
                or validated_result.get("output_table") != public_output.table):
            raise ValueError("backend publication result and terminal_status disagree")
        outputs = validated_result.get("outputs")
        if not isinstance(outputs, list):
            raise ValueError("backend publication validated_result outputs must be a list")
        for output in outputs:
            if (not isinstance(output, dict)
                    or set(output) != {"step_id", "name", "uri", "logical_uri"}
                    or any(not isinstance(output.get(key), str) or not output[key]
                           for key in ("step_id", "name", "uri", "logical_uri"))):
                raise ValueError("backend publication contains an invalid output contract")
            step_id = output["step_id"]
            if step_id in output_by_step:
                raise ValueError(f"backend publication repeats output step '{step_id}'")
            if (_validated_object_uri(output["uri"], attempt=True)
                    != canonical_sinks.get(step_id)):
                raise ValueError(
                    f"backend publication output '{step_id}' does not match its pinned sink")
            output_by_step[step_id] = dict(output)
        if set(output_by_step) != set(canonical_sinks):
            raise ValueError("successful backend outputs do not exactly match staged sink attempts")
    elif validated_result is not None:
        # Failed/cancelled result artifacts can contain remote error text and partial user data. The
        # canonical, sanitized RunStatus is sufficient terminal evidence; never copy raw results into
        # metadata backups for a negative publication.
        raise ValueError("negative backend publication must not persist a raw result artifact")

    if not isinstance(catalog_effects, list):
        raise ValueError("backend publication catalog_effects must be a list")
    canonical_catalog: list[dict] = []
    catalog_steps: set[str] = set()
    for raw_plan in catalog_effects:
        plan = validate_managed_catalog_publication_plan(raw_plan)
        step_id = plan["step_id"]
        if step_id in catalog_steps:
            raise ValueError(f"backend publication repeats catalog effect '{step_id}'")
        output = output_by_step.get(step_id)
        if (output is None or plan["run_id"] != run_id
                or plan["ref_key"] != f"{run_id}:{step_id}"
                or plan["event_key"] != f"ray-jobs:{attempt_id}:{step_id}"
                or plan["name"] != output["name"]
                or plan["uri"] != output["uri"]):
            raise ValueError(
                f"backend publication catalog effect '{step_id}' changed its output identity")
        catalog_steps.add(step_id)
        canonical_catalog.append(plan)
    canonical_catalog.sort(key=lambda plan: plan["step_id"])
    if candidate.status == "done" and catalog_steps != set(output_by_step):
        raise ValueError("successful backend publication has an incomplete catalog effect set")
    if candidate.status != "done" and canonical_catalog:
        raise ValueError("negative backend publication cannot contain catalog effects")

    canonical_usage = None
    if usage_effect is not None:
        canonical_usage = validate_catalog_usage_publication_plan(usage_effect)
        if (candidate.status != "done" or not canonical_catalog
                or canonical_usage["run_id"] != run_id
                or canonical_usage["event_key"] != f"ray-jobs:{attempt_id}"):
            raise ValueError("backend publication contains an unexpected usage effect")
    elif candidate.status == "done" and canonical_catalog:
        raise ValueError("successful sink publication has no frozen usage effect")
    event_keys = [plan["event_key"] for plan in canonical_catalog]
    if canonical_usage is not None:
        event_keys.append(canonical_usage["event_key"])
    if len(event_keys) != len(set(event_keys)):
        raise ValueError("backend publication effect event keys are not unique")

    envelope = {
        "contract_version": _BACKEND_PUBLICATION_EFFECTS_VERSION,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "terminal_status": terminal_status,
        "validated_result": validated_result,
        "sink_attempts": canonical_sinks,
        "catalog_effects": canonical_catalog,
        "usage_effect": canonical_usage,
    }
    payload = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), default=str
    )
    if len(payload.encode("utf-8")) > JOB_SQL_ENVELOPE_MAX_BYTES:
        raise ValueError(
            "backend publication effects plan exceeds the durable SQL envelope limit"
        )
    return envelope, payload


def _decode_backend_publication_effects(raw: str, run_id: str, attempt_id: str) -> dict:
    try:
        envelope = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("backend publication effects plan is not valid JSON") from exc
    if (not isinstance(envelope, dict)
            or set(envelope) != _BACKEND_PUBLICATION_EFFECTS_FIELDS
            or envelope.get("contract_version") != _BACKEND_PUBLICATION_EFFECTS_VERSION
            or envelope.get("run_id") != str(run_id)
            or envelope.get("attempt_id") != str(attempt_id)):
        raise ValueError("backend publication effects plan has an invalid envelope")
    validated, canonical = _validated_backend_publication_effects(
        str(run_id), str(attempt_id), envelope.get("terminal_status"),
        envelope.get("validated_result"), envelope.get("sink_attempts"),
        envelope.get("catalog_effects"), envelope.get("usage_effect"),
    )
    if raw != canonical:
        raise ValueError("backend publication effects plan is not canonical")
    return validated


def _validate_backend_publication_effects_binding(
        job: RunBackendJob, effects: dict) -> None:
    ref = effects.get("terminal_status", {}).get("backend_ref")
    expected = {
        "backend": job.backend, "cluster_ref": job.cluster_ref,
        "submission_id": job.submission_id, "attempt_id": job.attempt_id,
        "job_uri": job.job_uri, "result_uri": job.result_uri,
        "code_ref": job.code_ref, "durable": True,
    }
    if ref != expected:
        raise RuntimeError("backend publication effects changed their durable binding")


def begin_backend_publication_effects(
        run_id: str, attempt_id: str, owner: str, terminal_status: dict,
        validated_result: dict | None, sink_attempts: dict | None,
        catalog_effects: list[dict] | None = None,
        usage_effect: dict | None = None) -> str:
    """Linearize terminal effects against quarantine and durably freeze their replay plan.

    Returns ``started | effects | submission | quarantined | published | busy | lost``. ``effects``
    means this or a previous lease holder already committed the immutable plan; callers must replay that
    SQL copy, never an object artifact that may have changed after the decision. ``submission`` means an
    already-linearized submit/stop-fence request must be re-observed before terminal evidence is fresh.
    """
    run_id, attempt_id, owner = str(run_id), str(attempt_id), str(owner)
    terminal_status = dict(terminal_status)
    validated_result = dict(validated_result) if validated_result is not None else None
    catalog_effects = [dict(plan) for plan in (catalog_effects or [])]
    derive_quarantined_sinks = sink_attempts is None
    trusted_sinks = dict(sink_attempts) if sink_attempts is not None else None
    with session() as s:
        now = _db_now(s)
        row = s.get(RunBackendJob, run_id, with_for_update=True)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        if row.publication_state == "published":
            return "published"
        if row.publication_state == _BACKEND_PUBLICATION_EFFECTS_STATE:
            return "effects"
        if (row.publication_state != "pending" or row.publication_owner != owner
                or not _run_preallocation_active(row.publication_lease_until, now)):
            return "busy"
        if row.submission_state in _UNSETTLED_BACKEND_SUBMISSION_STATES:
            return "submission"

        terminal = str(terminal_status.get("status") or "")
        if row.quarantine_reason is not None and terminal != "failed":
            return "quarantined"
        derive_authorized = (
            (row.quarantine_reason is not None and terminal == "failed")
            or (bool(row.cancel_requested) and terminal == "cancelled")
        )
        if derive_quarantined_sinks and not derive_authorized:
            raise RuntimeError(
                "only durable quarantine or acknowledged cancellation may derive bound sinks")
        candidate_ref = terminal_status.get("backend_ref") or {}
        expected_ref = {
            "backend": row.backend, "cluster_ref": row.cluster_ref,
            "submission_id": row.submission_id, "attempt_id": row.attempt_id,
            "job_uri": row.job_uri, "result_uri": row.result_uri,
            "code_ref": row.code_ref, "durable": True,
        }
        if (not isinstance(candidate_ref, dict)
                or candidate_ref != expected_ref):
            raise RuntimeError("backend publication candidate changed its durable binding")
        if terminal == "done":
            try:
                durable_job = json.loads(row.job_doc or "")
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "successful backend publication has no valid durable job envelope") from exc
            if (not isinstance(durable_job, dict)
                    or durable_job.get("run_id") != run_id
                    or durable_job.get("backend") != row.backend
                    or durable_job.get("submission_id") != row.submission_id
                    or durable_job.get("attempt_id") != row.attempt_id
                    or validated_result is None
                    or validated_result.get("envelope_sha256")
                    != durable_job.get("envelope_sha256")):
                raise RuntimeError(
                    "successful backend publication result changed its durable job envelope")

        attempt_identities = list(s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.run_id == run_id,
            ObjectAttempt.kind == "sink",
        ).order_by(ObjectAttempt.uri)))
        logical_ids = sorted({attempt.logical_id for attempt in attempt_identities
                              if attempt.logical_id})
        locked_logicals = {logical.logical_id: logical for logical in s.scalars(
            select(CatalogLogicalDataset).where(
                CatalogLogicalDataset.logical_id.in_(logical_ids))
            .order_by(CatalogLogicalDataset.logical_id).with_for_update()
        )} if logical_ids else {}
        attempts = list(s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.run_id == run_id,
            ObjectAttempt.kind == "sink",
        ).order_by(ObjectAttempt.uri).with_for_update()))
        active = [row for row in attempts if row.state in _ACTIVE_BACKEND_SINK_STATES]
        if derive_quarantined_sinks:
            staged_sinks = {
                "derived:" + hashlib.sha256(attempt.uri.encode()).hexdigest(): attempt.uri
                for attempt in active
            }
        else:
            staged_sinks = trusted_sinks or {}
            canonical_expected = {
                _validated_object_uri(uri, attempt=True) for uri in staged_sinks.values()
            }
            active_uris = {attempt.uri for attempt in active}
            if (len(canonical_expected) != len(staged_sinks)
                    or canonical_expected != active_uris):
                raise RuntimeError(
                    "backend publication sink plan does not exactly cover all active bound sinks")

        envelope, payload = _validated_backend_publication_effects(
            run_id, attempt_id, terminal_status, validated_result, staged_sinks,
            catalog_effects, usage_effect,
        )
        by_uri = {attempt.uri: attempt for attempt in active}
        selected = [(step_id, by_uri[uri]) for step_id, uri in envelope["sink_attempts"].items()]
        if terminal == "done":
            selected_logical_ids = [attempt.logical_id for _step_id, attempt in selected]
            output_by_step = {
                output["step_id"]: output for output in envelope["validated_result"]["outputs"]
            }
            if (None in selected_logical_ids
                    or len(set(selected_logical_ids)) != len(selected_logical_ids)):
                raise BackendPublicationConflict(
                    "one backend publication cannot stage two generations of one logical dataset")
            for step_id, attempt in selected:
                if attempt.state != "committed":
                    raise RuntimeError(
                        f"backend publication sink '{step_id}' is not committed")
                if output_by_step[step_id]["logical_uri"].rstrip("/") != attempt.logical_uri:
                    raise BackendPublicationConflict(
                        f"backend publication sink '{step_id}' changed its logical target")
                logical = locked_logicals.get(attempt.logical_id)
                if (logical is None or logical.state != "active"
                        or attempt.catalog_epoch != logical.catalog_epoch):
                    raise BackendPublicationConflict(
                        f"backend publication sink '{step_id}' was fenced by unregister")
                if attempt.publish_seq is None or attempt.publish_seq <= logical.current_publish_seq:
                    raise BackendPublicationConflict(
                        f"backend publication sink '{step_id}' is not newer than the catalog pointer")
            existing_backend_refs = list(s.scalars(
                select(ObjectAttemptRef).join(
                    ObjectAttempt, ObjectAttempt.uri == ObjectAttemptRef.attempt_uri
                ).where(
                    ObjectAttemptRef.ref_type == "backend_publication",
                    ObjectAttempt.logical_id.in_(selected_logical_ids),
                ).order_by(ObjectAttemptRef.ref_key).with_for_update()
            )) if selected_logical_ids else []
            if existing_backend_refs:
                raise BackendPublicationBusy(
                    "another staged backend publication owns a target logical dataset")
            for step_id, attempt in selected:
                key = {
                    "ref_type": "backend_publication",
                    "ref_key": f"{run_id}:{step_id}",
                    "ref_slot": "",
                }
                existing = s.get(ObjectAttemptRef, key, with_for_update=True)
                if existing is not None and (
                        existing.attempt_uri != attempt.uri
                        or existing.generation != attempt.generation):
                    raise RuntimeError(
                        f"backend publication sink '{step_id}' changed its pinned attempt")
                if existing is None:
                    s.add(ObjectAttemptRef(
                        **key, attempt_uri=attempt.uri, generation=attempt.generation,
                    ))
            plan_by_step = {plan["step_id"]: plan for plan in envelope["catalog_effects"]}
            for step_id, attempt in selected:
                plan = plan_by_step[step_id]
                if plan["generation"] != attempt.generation:
                    raise RuntimeError(
                        f"backend publication catalog plan '{step_id}' changed generation")
        else:
            committed_uris = [attempt.uri for _step_id, attempt in selected
                              if attempt.state == "committed"]
            if committed_uris and s.scalar(select(exists().where(
                    ObjectAttemptRef.attempt_uri.in_(committed_uris)))):
                raise RuntimeError(
                    "cannot terminalize a committed backend sink with a durable owner")
            uris = [attempt.uri for _step_id, attempt in selected]
            leases = list(s.scalars(select(ObjectAttemptLease).where(
                ObjectAttemptLease.attempt_uri.in_(uris),
                ObjectAttemptLease.lease_type.in_(("write", "publish")),
            ).order_by(ObjectAttemptLease.lease_id).with_for_update())) if uris else []
            for _step_id, attempt in selected:
                _terminalize_bound_backend_sink_attempt(attempt, now)
            for lease in leases:
                s.delete(lease)

        row.publication_state = _BACKEND_PUBLICATION_EFFECTS_STATE
        row.publication_doc = payload
        s.flush()
        return "started"


def _backend_publication_effects_in_session(
        s, job: RunBackendJob, *, lock: bool = False) -> dict:
    if job.publication_state != _BACKEND_PUBLICATION_EFFECTS_STATE or job.publication_doc is None:
        raise RuntimeError("backend publication has no staged terminal effects")
    effects = _decode_backend_publication_effects(
        job.publication_doc, job.run_id, job.attempt_id
    )
    _validate_backend_publication_effects_binding(job, effects)
    if effects["terminal_status"]["status"] != "done":
        return effects
    uris = sorted(effects["sink_attempts"].values())
    attempt_query = select(ObjectAttempt).where(
        ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri)
    if lock:
        attempt_query = attempt_query.with_for_update()
    attempts = {row.uri: row for row in s.scalars(attempt_query)} if uris else {}
    ref_keys = sorted(f"{job.run_id}:{step_id}" for step_id in effects["sink_attempts"])
    ref_query = select(ObjectAttemptRef).where(
        ObjectAttemptRef.ref_type == "backend_publication",
        ObjectAttemptRef.ref_key.in_(ref_keys),
    ).order_by(ObjectAttemptRef.ref_type, ObjectAttemptRef.ref_key)
    if lock:
        ref_query = ref_query.with_for_update()
    refs = {row.ref_key: row for row in s.scalars(ref_query)} if ref_keys else {}
    for step_id, uri in effects["sink_attempts"].items():
        ref = refs.get(f"{job.run_id}:{step_id}")
        attempt = attempts.get(uri)
        if (ref is None or attempt is None or ref.attempt_uri != uri
                or ref.generation != attempt.generation
                or attempt.run_id != job.run_id or attempt.kind != "sink"):
            raise RuntimeError(
                f"backend publication sink '{step_id}' lost its exact temporary reference"
            )
    return effects


def backend_publication_effects(run_id: str, attempt_id: str) -> dict | None:
    """Return a staged terminal plan only after attesting every temporary attempt reference."""
    with session() as s:
        job = s.get(RunBackendJob, str(run_id))
        if (job is None or job.attempt_id != str(attempt_id)
                or job.publication_state != _BACKEND_PUBLICATION_EFFECTS_STATE):
            return None
        return _backend_publication_effects_in_session(s, job)


def _release_backend_publication_effect_refs(s, job: RunBackendJob, effects: dict) -> None:
    if effects["terminal_status"]["status"] != "done":
        return
    uris = sorted(effects["sink_attempts"].values())
    attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
        ObjectAttempt.uri.in_(uris)
    ).order_by(ObjectAttempt.uri).with_for_update())} if uris else {}
    ref_keys = sorted(f"{job.run_id}:{step_id}" for step_id in effects["sink_attempts"])
    refs = {row.ref_key: row for row in s.scalars(select(ObjectAttemptRef).where(
        ObjectAttemptRef.ref_type == "backend_publication",
        ObjectAttemptRef.ref_key.in_(ref_keys),
    ).order_by(ObjectAttemptRef.ref_type, ObjectAttemptRef.ref_key).with_for_update())} \
        if ref_keys else {}
    pinned: list[ObjectAttempt] = []
    for step_id, uri in effects["sink_attempts"].items():
        ref = refs.get(f"{job.run_id}:{step_id}")
        attempt = attempts.get(uri)
        if (ref is None or attempt is None or ref.attempt_uri != uri
                or ref.generation != attempt.generation):
            raise RuntimeError(
                f"backend publication sink '{step_id}' lost its temporary reference"
            )
        s.delete(ref)
        pinned.append(attempt)
    if pinned:
        s.flush()
        now = _db_now(s)
        for attempt in pinned:
            _maybe_supersede(s, attempt, now)


def claim_backend_publication(run_id: str, attempt_id: str, owner: str,
                              lease_seconds: float = 30.0) -> str:
    """Claim a terminal publisher lease: claimed | effects | submission | busy | published | lost."""
    with session() as s:
        # Derive every deadline from the metadata DB server's clock. Hub/pod wall-clock skew must not
        # let one supervisor steal a live lease early or write a deadline far into the future.
        now = _db_now(s)
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        result = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_state != "published",
                or_(
                    RunBackendJob.publication_state == _BACKEND_PUBLICATION_EFFECTS_STATE,
                    RunBackendJob.submission_state.not_in(
                        _UNSETTLED_BACKEND_SUBMISSION_STATES),
                ),
                or_(RunBackendJob.publication_owner == owner,
                    RunBackendJob.publication_owner.is_(None),
                    RunBackendJob.publication_lease_until.is_(None),
                    RunBackendJob.publication_lease_until < now),
            ).values(publication_owner=owner, publication_lease_until=lease)
        )
        if result.rowcount:
            state = s.scalar(select(RunBackendJob.publication_state).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
            ))
            return "effects" if state == _BACKEND_PUBLICATION_EFFECTS_STATE else "claimed"
        row = s.get(RunBackendJob, run_id)
        if row is None or row.attempt_id != attempt_id:
            return "lost"
        if row.publication_state == "published":
            return "published"
        if (row.publication_state == "pending"
                and row.submission_state in _UNSETTLED_BACKEND_SUBMISSION_STATES):
            return "submission"
        return "busy"


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
    with session() as s:
        identity = s.execute(select(
            RunState.canvas_id, RunState.auth_canvas_id,
        ).where(RunState.run_id == run_id)).one_or_none()
        if identity is None:
            return False
        canvas_id, auth_canvas_id = identity
        canvas_ids = {str(auth_canvas_id)} if auth_canvas_id is not None else set()
        if canvas_id is not None and s.scalar(select(Canvas.id).where(
                Canvas.id == canvas_id)) is not None:
            canvas_ids.add(str(canvas_id))
        locked_canvases = {
            key: s.get(Canvas, key, with_for_update=True, populate_existing=True)
            for key in sorted(canvas_ids)
        }
        if auth_canvas_id is not None and locked_canvases.get(str(auth_canvas_id)) is None:
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
        job = s.get(RunBackendJob, str(run_id), with_for_update=True)
        if (job is None or job.attempt_id != str(attempt_id)
                or job.publication_owner != str(owner)
                or job.publication_state != _BACKEND_PUBLICATION_EFFECTS_STATE):
            return False
        effects = _backend_publication_effects_in_session(s, job, lock=True)
        if published != effects["terminal_status"]:
            return False
        published = dict(effects["terminal_status"])
        terminal = str(published["status"])
        if terminal == "done":
            sink_uris = list(effects["sink_attempts"].values())
            states = dict(s.execute(select(
                ObjectAttempt.uri, ObjectAttempt.state,
            ).where(ObjectAttempt.uri.in_(sink_uris))).all()) if sink_uris else {}
            if any(states.get(uri) != "published" for uri in sink_uris):
                return False
            expected_events = {
                plan["event_key"]: (
                    "output", plan["uri"], plan["version"], plan["fingerprint"])
                for plan in effects["catalog_effects"]
            }
            usage = effects["usage_effect"]
            if usage is not None:
                expected_events[usage["event_key"]] = (
                    "usage", None, None, usage["fingerprint"])
            event_keys = sorted(expected_events)
            events = {row.event_key: row for row in s.scalars(
                select(CatalogPublicationEvent).where(
                    CatalogPublicationEvent.event_key.in_(event_keys))
                .order_by(CatalogPublicationEvent.event_key).with_for_update()
            )} if event_keys else {}
            if any(
                    key not in events or (
                        events[key].effect_type, events[key].uri,
                        events[key].version, events[key].fingerprint,
                    ) != expected
                    for key, expected in expected_events.items()):
                return False
        source_pins = _backend_source_pins_in_session(s, str(run_id), lock=True)
        job.publication_state = "published"
        job.publication_owner = None
        job.publication_lease_until = None
        job.recovery_blocked_reason = None
        job.publication_doc = None
        job.result_doc = json.dumps(
            published, sort_keys=True, separators=(",", ":"), default=str)
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
        _replace_attempt_refs(
            s, "run_state", run_id, _result_doc_refs(published))
        _release_backend_publication_effect_refs(s, job, effects)
        for obj in pruned:
            stale_job = s.get(RunBackendJob, obj.run_id)
            if stale_job is not None:
                s.delete(stale_job)
            _replace_attempt_ref(s, "run_state", obj.run_id, None)
            s.delete(obj)
        per_node = published.get("per_node") or None
        # Source refs are object-lifecycle owners and must be released before any helper can take the
        # local-result registry lock. They still share this authoritative transaction, so any later
        # publication failure restores the backend marker, public state/history, and pins together.
        _release_backend_source_pins(s, source_pins)
        _upsert_run_record(
            s, canvas_id=state.canvas_id, target_node_id=published.get("target_node_id"),
            target_port_id=None,
            job_type="run", status=terminal,
            rows=published.get("total_rows"), ms=published.get("ms"),
            error=published.get("error"), outputs=published.get("outputs") or [],
            per_node=per_node, run_id=run_id,
            execution_manifest_sha256=state.execution_manifest_sha256,
        )
        if pruned:
            _lock_local_result_registry(s)
        if not prune_current:
            sync_local_result_owner(s, "run_state", run_id, published)
        if terminal in ("done", "failed") and not prune_current:
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=False)
        elif terminal in ("done", "failed"):
            _release_terminal_local_result_writers(
                s, run_id, allow_unreferenced=True)
        for obj in pruned:
            _drop_local_result_owner_locked(s, "run_state", obj.run_id)
        return True


def renew_backend_publication(run_id: str, attempt_id: str, owner: str,
                              lease_seconds: float = 30.0) -> bool:
    """Extend an active publication lease while catalog/history side effects are in flight."""
    with session() as s:
        now = _db_now(s)
        lease = now + datetime.timedelta(seconds=max(1.0, lease_seconds))
        updated = s.execute(
            update(RunBackendJob).where(
                RunBackendJob.run_id == run_id,
                RunBackendJob.attempt_id == attempt_id,
                RunBackendJob.publication_owner == owner,
                RunBackendJob.publication_state != "published",
                or_(
                    RunBackendJob.publication_state == _BACKEND_PUBLICATION_EFFECTS_STATE,
                    and_(
                        RunBackendJob.publication_state == "pending",
                        RunBackendJob.quarantine_reason.is_(None),
                        RunBackendJob.submission_state.not_in(
                            _UNSETTLED_BACKEND_SUBMISSION_STATES),
                    ),
                ),
            ).values(publication_lease_until=lease)
        )
        return bool(updated.rowcount)


def backend_publication_owned(run_id: str, attempt_id: str, owner: str) -> bool:
    """Fence an external side effect immediately before it is issued by a lease holder."""
    with session() as s:
        return s.scalar(select(RunBackendJob.run_id).where(
            RunBackendJob.run_id == run_id,
            RunBackendJob.attempt_id == attempt_id,
            RunBackendJob.publication_state == _BACKEND_PUBLICATION_EFFECTS_STATE,
            RunBackendJob.publication_owner == owner,
            RunBackendJob.publication_lease_until >= func.current_timestamp(),
        ).limit(1)) is not None


def _terminalize_bound_backend_sink_attempt(
        row: ObjectAttempt, now: datetime.datetime) -> None:
    """Apply writer-terminal proof without regressing a published or terminal generation."""
    if row.state in ("allocated", "writing"):
        row.state = "abandoned"
        row.terminal_proof_at = now
        row.quiet_until = now + datetime.timedelta(seconds=60)
    elif row.state == "committed":
        row.state = "abandoned"
        row.terminal_proof_at = row.terminal_proof_at or now
    elif row.state == "published" or row.state in _TERMINAL_ATTEMPT_STATES:
        return
    else:
        raise RuntimeError(f"unsupported backend sink attempt state {row.state!r}")


def terminalize_bound_backend_sink_attempts(
        run_id: str, attempt_id: str, publication_owner: str, *,
        expected_sink_uris: list[str] | None = None) -> bool:
    """Retire only the exact bound run's sink writers under an active publication lease.

    The caller must first establish from the external control plane that the writer is terminal. This
    transaction then fences that proof to the exact durable binding and, for a trusted envelope, its
    hash-bound sink URI set. A corrupt envelope may omit that set only when durable stop/quarantine
    intent exists. ``False`` means the binding, lease, intent, or sink attestation no longer authorizes
    cleanup; no attempt is changed in that case.
    """
    run_id = str(run_id)
    attempt_id = str(attempt_id)
    publication_owner = str(publication_owner)
    if expected_sink_uris is not None:
        if not isinstance(expected_sink_uris, list):
            raise ValueError("expected backend sink URIs must be a list")
        expected_sink_uris = [
            _validated_object_uri(str(uri), attempt=True)
            for uri in expected_sink_uris
        ]
        if expected_sink_uris != sorted(set(expected_sink_uris)):
            raise ValueError(
                "expected backend sink URIs must be canonical, sorted, and unique")
    with session() as s:
        state = _lock_existing_run_identity(s, run_id)
        if state is None:
            return False
        job = s.get(RunBackendJob, run_id, with_for_update=True)
        now = _db_now(s)
        if (job is None
                or job.attempt_id != attempt_id
                or job.publication_state == "published"
                or job.publication_owner != publication_owner
                or not _run_preallocation_active(job.publication_lease_until, now)):
            return False
        if expected_sink_uris is None and not (job.cancel_requested
                or job.quarantine_reason is not None
                or job.recovery_blocked_reason is not None
                or state.status in _TERMINAL_RUN):
            return False

        attempts = list(s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.run_id == run_id,
            ObjectAttempt.kind == "sink",
        ).order_by(ObjectAttempt.uri).with_for_update()))
        if expected_sink_uris is None:
            selected = attempts
        else:
            by_uri = {row.uri: row for row in attempts}
            if any(uri not in by_uri for uri in expected_sink_uris):
                return False
            active_uris = {
                row.uri for row in attempts
                if row.state in ("allocated", "writing", "committed")
            }
            if not active_uris.issubset(set(expected_sink_uris)):
                return False
            selected = [by_uri[uri] for uri in expected_sink_uris]

        committed_uris = [row.uri for row in selected if row.state == "committed"]
        if committed_uris and s.scalar(select(exists().where(
                ObjectAttemptRef.attempt_uri.in_(committed_uris)))):
            raise RuntimeError("cannot abandon a committed backend sink with a durable owner")
        uris = [row.uri for row in selected]
        leases = list(s.scalars(select(ObjectAttemptLease).where(
            ObjectAttemptLease.attempt_uri.in_(uris),
            ObjectAttemptLease.lease_type.in_(("write", "publish")),
        ).order_by(ObjectAttemptLease.lease_id).with_for_update())) if uris else []

        for row in selected:
            _terminalize_bound_backend_sink_attempt(row, now)
        for lease in leases:
            s.delete(lease)
        return True


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
        if job is None:
            return _stale_secs(r.updated_at) > threshold_s
        now = _db_now(s)
        observed_at = (
            job.last_control_observed_at or job.updated_at or r.updated_at
        )
        if observed_at is None:
            return False
        # SQLite returns naive timestamps while Postgres returns timezone-aware values. Normalize both
        # as UTC before comparing, but keep the source of "now" the metadata DB on every backend.
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=datetime.timezone.utc)
        return (now - observed_at).total_seconds() > threshold_s


def _stored_schema_columns(columns: list[dict]) -> list[dict]:
    """Validate and retain the complete current schema model in contract storage."""
    stored: list[dict] = []
    for raw in columns:
        field = ColumnSchema.model_validate(raw)
        # Direct callers create a named contract, so omitted provenance means a declaration.
        if "provenance" not in field.model_fields_set:
            field = field.model_copy(update={"provenance": "declared"})
        stored.append(field.model_dump(by_alias=True))
    return stored


def save_schema_contract(name: str, columns: list[dict]) -> int:
    """Save a named schema contract as a new version without dropping field evidence."""
    from sqlalchemy import func
    from sqlalchemy.exc import IntegrityError
    doc = json.dumps(_stored_schema_columns(columns))
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


_NUMERIC_TYPE_RANK = {
    "tinyint": 0, "int8": 0,
    "smallint": 1, "int16": 1,
    "int": 2, "integer": 2, "int32": 2,
    "bigint": 3, "int64": 3,
    "float": 4, "real": 4, "float32": 4,
    "double": 5, "float64": 5,
}


def _type_change_status(before: str, after: str) -> tuple[str, str]:
    before, after = before.strip().lower(), after.strip().lower()
    if not before or not after:
        return "unknown", "logical type is unknown"
    if before == after:
        return "compatible", "logical type is unchanged"
    old_rank, new_rank = _NUMERIC_TYPE_RANK.get(before), _NUMERIC_TYPE_RANK.get(after)
    if old_rank is not None and new_rank is not None:
        if new_rank == old_rank:
            return "compatible", f"logical types {before} and {after} are equivalent"
        if new_rank > old_rank:
            return "compatible", f"logical type widens from {before} to {after}"
        return "breaking", f"logical type narrows from {before} to {after}"
    return "breaking", f"logical type changes from {before} to {after}"


def _matched_field_status(before: ColumnSchema, after: ColumnSchema) -> tuple[str, str]:
    type_status, reason = _type_change_status(before.type, after.type)
    if type_status == "breaking":
        return type_status, reason
    if before.nullable is None or after.nullable is None:
        return "unknown", reason + "; nullability is not proven on both versions"
    if before.nullable and not after.nullable:
        return "breaking", reason + "; field became non-nullable"
    if not before.nullable and after.nullable:
        return "compatible", reason + "; field became nullable"
    return type_status, reason


def _addition_status(field: ColumnSchema) -> tuple[str, str]:
    if field.nullable is True:
        return "compatible", "nullable field was added"
    if field.nullable is False and field.has_default is True:
        return "compatible", "non-nullable field was added with a default"
    if field.nullable is False and field.has_default is False:
        return "breaking", "non-nullable field was added without a default"
    return "unknown", "added field has unknown nullability or default evidence"


def _overall_status(fields: list[SchemaFieldCompatibility]) -> str:
    statuses = {field.status for field in fields}
    if "breaking" in statuses:
        return "breaking"
    if "unknown" in statuses:
        return "unknown"
    return "compatible"


def diff_columns(a: list[dict], b: list[dict]) -> SchemaCompatibility:
    """Evaluate a schema transition without inferring identity the evidence does not provide."""
    before = [ColumnSchema.model_validate(field) for field in a]
    after = [ColumnSchema.model_validate(field) for field in b]
    before_ids = {field.field_id for field in before if field.field_id}
    after_ids = {field.field_id for field in after if field.field_id}
    duplicate_ids = {
        field_id for field_id in before_ids | after_ids
        if sum(field.field_id == field_id for field in before) > 1
        or sum(field.field_id == field_id for field in after) > 1
    }
    if duplicate_ids:
        uncertain = [SchemaFieldCompatibility(
            kind="changed", status="unknown", field_id=field_id,
            reason="stable field identity is duplicated and cannot prove a match")
            for field_id in sorted(duplicate_ids)]
        remaining = diff_columns(
            [field.model_dump(by_alias=True) for field in before if field.field_id not in duplicate_ids],
            [field.model_dump(by_alias=True) for field in after if field.field_id not in duplicate_ids],
        )
        fields = uncertain + remaining.fields
        return SchemaCompatibility(status=_overall_status(fields), fields=fields)

    by_id = {field.field_id: index for index, field in enumerate(after) if field.field_id}
    matched_after: set[int] = set()
    stable_matches: dict[int, int] = {}
    fields: list[SchemaFieldCompatibility] = []

    # Reserve every proven identity match before falling back to names. Otherwise
    # an earlier evidence-poor field can consume a newer field that belongs to a
    # later stable-ID match, producing two contradictory results for one field.
    for old_index, old in enumerate(before):
        if old.field_id and old.field_id in by_id:
            new_index = by_id[old.field_id]
            stable_matches[old_index] = new_index
            matched_after.add(new_index)

    for old_index, old in enumerate(before):
        new_index = stable_matches.get(old_index)
        matched_by_id = new_index is not None
        if new_index is None:
            new_index = next((
                index for index, field in enumerate(after)
                if index not in matched_after and field.name == old.name
            ), None)
        new = after[new_index] if new_index is not None else None
        if new is not None and not matched_by_id:
            if old.field_id or new.field_id:
                fields.append(SchemaFieldCompatibility(
                    kind="changed", status="unknown", old_name=old.name, new_name=new.name,
                    field_id=old.field_id or new.field_id,
                    reason="field identity is missing or changed, so the name match is not proven stable"))
                matched_after.add(new_index)
                continue
        if new is None:
            if old.field_id and len(after_ids) == len(after):
                status, reason = "breaking", "stable field identity is absent from the newer complete schema"
            else:
                status, reason = "unknown", "field is absent by name; no stable identity proves removal versus rename"
            fields.append(SchemaFieldCompatibility(
                kind="removed", status=status, old_name=old.name, field_id=old.field_id, reason=reason))
            continue
        matched_after.add(new_index)
        status, reason = _matched_field_status(old, new)
        fields.append(SchemaFieldCompatibility(
            kind="renamed" if matched_by_id and old.name != new.name else "unchanged" if old.name == new.name else "changed",
            status=status, reason=(f"renamed from {old.name}; " if matched_by_id and old.name != new.name else "") + reason,
            field_id=old.field_id if matched_by_id else None, old_name=old.name, new_name=new.name))

    for new_index, new in enumerate(after):
        if new_index in matched_after:
            continue
        status, reason = _addition_status(new)
        fields.append(SchemaFieldCompatibility(
            kind="added", status=status, new_name=new.name, field_id=new.field_id, reason=reason))
    return SchemaCompatibility(status=_overall_status(fields), fields=fields)


_RESULT_CACHE_MAX = 1000  # persistent equivalent of the old in-process _MAX_RUNS cache cap
_INSTALLATION_ID = 1
_OBJECT_ATTEMPT_KINDS = ("region", "sink")
_LOCAL_RESULT_REGISTRY_ID = 1
_LOCAL_RESULT_OWNER_KINDS = {
    "canvas", "canvas_version", "catalog_entry", "managed_file_revision", "result_cache",
    "dataset_view", "distribution_report", "durable_task", "profile_job",
    "run_input_admission", "run_record", "run_state",
}
_LOCAL_RESULT_EPHEMERAL_OWNER_KIND = "read_lease"
_LINEAR_CHECKPOINT_OWNER_KIND = "durable_checkpoint"
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
    """Extract local-result URIs from the canvas's canonical node list."""
    if not isinstance(value, dict):
        raise ValueError("canvas document must be an object")
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 5000:
        raise ValueError("canvas document has an invalid or oversized node list")
    candidates: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "source":
            continue
        data = node.get("data")
        config = data.get("config") if isinstance(data, dict) else None
        if not isinstance(config, dict):
            continue
        candidate = _local_result_candidate(config.get("uri"))
        if candidate is not None:
            candidates.add(candidate)
    return candidates


def _local_result_owner_candidates(owner_kind: str, values: tuple) -> list[str]:
    """Owner-aware exact extraction avoids scanning arbitrary caller-controlled JSON strings."""
    candidates: set[str] = set()
    if owner_kind in ("run_state", "run_record", "result_cache"):
        for value in values:
            for uri in _result_doc_uris(value):
                candidate = _local_result_candidate(uri)
                if candidate is not None:
                    candidates.add(candidate)
    elif owner_kind in ("catalog_entry", "managed_file_revision"):
        for value in values:
            candidate = _local_result_candidate(value)
            if candidate is not None:
                candidates.add(candidate)
    elif owner_kind in ("canvas", "canvas_version"):
        for value in values:
            candidates.update(_canvas_local_result_candidates(value))
    elif owner_kind in (
            "dataset_view", "distribution_report", "durable_task", "profile_job",
            "run_input_admission"):
        pass
    else:
        raise ValueError(f"unknown local-result owner kind {owner_kind!r}")
    return sorted(candidates)


def _manifest_revision_identities(value: object) -> set[tuple[str, str]]:
    """Extract the fixed, secret-free dataset/revision pairs from one persisted manifest surface."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return set()
    if isinstance(value, dict):
        identities: set[tuple[str, str]] = set()
        for key in ("input_manifest", "inputManifest"):
            if key in value:
                identities.update(_manifest_revision_identities(value[key]))
        profile = value.get("profile")
        if isinstance(profile, dict):
            identities.update(_manifest_revision_identities(profile))
        return identities
    if not isinstance(value, list):
        return set()
    identities = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        # Provider revision ids are opaque within their provider. Only the built-in managed-local-file
        # provider may establish core artifact ownership; external history remains provider-retained.
        if item.get("provider") != "managed-local-file":
            continue
        dataset_id = item.get("dataset_id")
        revision_id = item.get("revision_id")
        if isinstance(dataset_id, str) and dataset_id and isinstance(revision_id, str) and revision_id:
            identities.add((dataset_id, revision_id))
    return identities


def _manifest_local_file_input_identities(value: object) -> set[tuple[str, str]]:
    """Extract content-identified ordinary local-file bindings from a manifest surface."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return set()
    if isinstance(value, dict):
        identities: set[tuple[str, str]] = set()
        for key in ("input_manifest", "inputManifest"):
            if key in value:
                identities.update(_manifest_local_file_input_identities(value[key]))
        profile = value.get("profile")
        if isinstance(profile, dict):
            identities.update(_manifest_local_file_input_identities(profile))
        return identities
    if not isinstance(value, list):
        return set()
    identities = set()
    for item in value:
        if not isinstance(item, dict) or item.get("provider") != _LOCAL_FILE_INPUT_PROVIDER:
            continue
        dataset_id = item.get("dataset_id")
        revision_id = item.get("revision_id")
        if isinstance(dataset_id, str) and dataset_id and isinstance(revision_id, str) and revision_id:
            identities.add((dataset_id, revision_id))
    return identities


def _canvas_revision_identities(value: object) -> set[tuple[str, str]]:
    """Extract exact core-revision selections from one canonical canvas document."""
    if not isinstance(value, dict):
        return set()
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 5000:
        return set()
    identities: set[tuple[str, str]] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "source":
            continue
        data = node.get("data")
        config = data.get("config") if isinstance(data, dict) else None
        ref = config.get("datasetRef") if isinstance(config, dict) else None
        if not isinstance(ref, dict):
            continue
        selected = ref.get("resolved") if ref.get("kind") == "as_of" else ref
        if not isinstance(selected, dict):
            continue
        dataset_id = selected.get("datasetId", selected.get("dataset_id"))
        revision_id = selected.get("revisionId", selected.get("revision_id"))
        if isinstance(dataset_id, str) and dataset_id and isinstance(revision_id, str) and revision_id:
            identities.add((dataset_id, revision_id))
    return identities


def _dataset_view_revision_identities(value: object) -> set[tuple[str, str]]:
    """Extract only core-retained exact revisions from one immutable DatasetView definition."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return set()
    if not isinstance(value, dict) or value.get("retentionOwner") != "core":
        return set()
    ref = value.get("datasetRef")
    if not isinstance(ref, dict) or ref.get("kind") != "exact":
        return set()
    dataset_id = ref.get("datasetId")
    revision_id = ref.get("revisionId")
    if isinstance(dataset_id, str) and dataset_id and isinstance(revision_id, str) and revision_id:
        return {(dataset_id, revision_id)}
    return set()


def _local_result_revision_identities(owner_kind: str, values: tuple) -> list[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    if owner_kind in ("canvas", "canvas_version"):
        for value in values:
            identities.update(_canvas_revision_identities(value))
    elif owner_kind in ("dataset_view", "distribution_report"):
        for value in values:
            identities.update(_dataset_view_revision_identities(value))
    elif owner_kind in ("profile_job", "run_input_admission", "run_record", "run_state"):
        for value in values:
            identities.update(_manifest_revision_identities(value))
    return sorted(identities)


def _local_result_input_identities(owner_kind: str, values: tuple) -> list[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    if owner_kind in (
            "durable_task", "profile_job", "run_input_admission", "run_record", "run_state"):
        for value in values:
            identities.update(_manifest_local_file_input_identities(value))
    return sorted(identities)


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
    revision_identities = _local_result_revision_identities(owner_kind, values)
    input_identities = _local_result_input_identities(owner_kind, values)
    input_artifact_uris: set[str] = set()
    if not candidates and not revision_identities and not input_identities and s.scalar(select(LocalResultReference.uri).where(
            LocalResultReference.owner_kind == owner_kind,
            LocalResultReference.owner_key == str(owner_key)).limit(1)) is None:
        # The durable owner row/key is already serialized by its caller. Avoid turning every ordinary
        # progress/autosave/catalog write into a global local-registry UPDATE on object-only workloads.
        return
    _lock_local_result_registry(s)
    _drop_local_result_owner_locked(s, owner_kind, str(owner_key))
    if revision_identities:
        revision_ids = [revision_id for _dataset_id, revision_id in revision_identities]
        revisions = {row.revision_id: row for row in s.scalars(
            select(ManagedLocalFileRevision).where(
                ManagedLocalFileRevision.revision_id.in_(revision_ids))
            .order_by(ManagedLocalFileRevision.revision_id).with_for_update())}
        managed_dataset_ids = set(s.scalars(select(CatalogLogicalDataset.logical_id).where(
            CatalogLogicalDataset.logical_id.in_(
                [dataset_id for dataset_id, _revision_id in revision_identities]))))
        for dataset_id, revision_id in revision_identities:
            revision = revisions.get(revision_id)
            if revision is None:
                if dataset_id in managed_dataset_ids:
                    raise RuntimeError("core-managed revision reference is unavailable")
                continue
            if revision.logical_id != dataset_id:
                raise RuntimeError("core-managed revision reference does not match its dataset")
            candidates.append(revision.artifact_uri)
        candidates = sorted(set(candidates))
    if input_identities:
        bindings = {
            (row.dataset_id, row.revision_id): row for row in s.scalars(
                select(LocalFileInputRevision).where(
                    tuple_(
                        LocalFileInputRevision.dataset_id,
                        LocalFileInputRevision.revision_id,
                    ).in_(input_identities))
                .order_by(
                    LocalFileInputRevision.dataset_id,
                    LocalFileInputRevision.revision_id,
                ).with_for_update())
        }
        if set(bindings) != set(input_identities):
            raise RuntimeError("local file input revision reference is unavailable")
        input_artifact_uris = {binding.artifact_uri for binding in bindings.values()}
        candidates.extend(input_artifact_uris)
        candidates = sorted(set(candidates))
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
            if (owner_kind not in ("run_state", "managed_file_revision")
                    or (owner_kind == "run_state" and str(owner_key) != artifact.writer_run_id)) \
                    and not (
                        owner_kind in ("durable_task", "run_input_admission")
                        and artifact.uri in input_artifact_uris
                    ):
                # Only the exact writer's RunState transaction may establish the primary ref and clear
                # its writer pair. Secondary owners cannot pin a guessed/provisional URI before that.
                raise RuntimeError(
                    "managed local result must be published by its exact writer run first")
        s.add(LocalResultReference(
            uri=artifact.uri, owner_kind=owner_kind, owner_key=str(owner_key)))
        if owner_kind == "durable_task" and artifact.uri in input_artifact_uris:
            # Task visibility is also recovery eligibility. Publish its exact ref and release the DB
            # writer authority in this transaction so a recovery scan can never claim a Task whose
            # snapshot is still provisional. The process-local guard closes idempotently after commit.
            artifact.writer_run_id = artifact.writer_token = None


def _linear_checkpoint_generation(
        task: DurableTask, checkpoint: DurableCheckpoint, attempt_id: str) -> str:
    payload = "\x00".join((
        "linear-checkpoint-candidate-v1", task.id, checkpoint.checkpoint_id,
        checkpoint.task_intent_sha256, checkpoint.graph_prefix_sha256,
        checkpoint.input_manifest_sha256, str(attempt_id)))
    return hashlib.sha256(payload.encode()).hexdigest()


def _linear_checkpoint_candidate_doc(
        s, task: DurableTask, checkpoint: DurableCheckpoint) -> dict | None:
    owned = list(s.scalars(select(LocalResultArtifact).where(
        LocalResultArtifact.writer_run_id == task.id).order_by(
            LocalResultArtifact.uri).with_for_update()))
    if checkpoint.phase == "pending":
        if (checkpoint.candidate_uri is not None
                or checkpoint.candidate_generation is not None
                or checkpoint.candidate_attempt_id is not None
                or checkpoint.candidate_dev is not None or checkpoint.candidate_ino is not None
                or owned):
            raise RuntimeError("pending checkpoint has partial candidate state")
        return None
    if (checkpoint.phase != "reserved" or checkpoint.candidate_uri is None
            or checkpoint.candidate_generation is None
            or checkpoint.candidate_attempt_id is None or len(owned) != 1
            or owned[0].uri != checkpoint.candidate_uri):
        raise RuntimeError("checkpoint candidate binding is incomplete")
    candidate_attempt = s.get(
        DurableTaskAttempt, checkpoint.candidate_attempt_id, with_for_update=True)
    if candidate_attempt is None or candidate_attempt.task_id != task.id:
        raise RuntimeError("checkpoint candidate attempt belongs to a different task")
    artifact = owned[0]
    generation = _linear_checkpoint_generation(
        task, checkpoint, checkpoint.candidate_attempt_id)
    basename = f"{_LOCAL_RESULT_PREFIX}checkpoint_{generation}.parquet"
    expected_uri = os.path.join(artifact.storage_root, basename)
    expected_lock = f"{basename[:-len('.parquet')]}.lock"
    if (checkpoint.candidate_generation != generation
            or checkpoint.candidate_uri != expected_uri
            or _local_result_candidate(expected_uri) != expected_uri
            or artifact.lock_name != expected_lock or artifact.state != "writing"
            or artifact.writer_token is None or artifact.committed_at is not None
            or re.fullmatch(r"[0-9a-f]{32}", artifact.namespace_id) is None
            or re.fullmatch(r"[0-9a-f]{32}", artifact.writer_token) is None
            or bool(artifact.lock_protected) != bool(artifact.lock_token)
            or (artifact.lock_token is not None
                and re.fullmatch(r"[0-9a-f]{32}", artifact.lock_token) is None)
            or artifact.delete_token is not None or artifact.delete_attempted_at is not None):
        raise RuntimeError("checkpoint candidate disagrees with its artifact authority")
    return {
        "task_id": task.id, "checkpoint_id": checkpoint.checkpoint_id,
        "uri": artifact.uri, "generation": generation,
        "attempt_id": checkpoint.candidate_attempt_id,
        "namespace_id": artifact.namespace_id, "storage_root": artifact.storage_root,
        "lock_name": artifact.lock_name, "lock_token": artifact.lock_token,
        "lock_protected": bool(artifact.lock_protected),
        "writer_token": artifact.writer_token, "state": artifact.state,
        "dev": checkpoint.candidate_dev, "ino": checkpoint.candidate_ino,
    }


def linear_checkpoint_candidate(task_id: str) -> dict | None:
    """Read back only the exact DB binding after an unknown reservation response."""
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id), with_for_update=True)
        if task is None and checkpoint is None:
            return None
        if (task is None or checkpoint is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        return _linear_checkpoint_candidate_doc(s, task, checkpoint)


def reserve_linear_checkpoint_candidate(
        *, task_id: str, attempt_id: str, owner_token: str,
        namespace_id: str, storage_root: str, writer_token: str,
        lock_token: str | None) -> dict:
    """Atomically create or exactly replay one DB-only managed-local candidate."""
    namespace_id, writer_token = str(namespace_id), str(writer_token)
    storage_root, owner_token = str(storage_root), str(owner_token)
    if (re.fullmatch(r"[0-9a-f]{32}", namespace_id) is None
            or re.fullmatch(r"[0-9a-f]{32}", writer_token) is None
            or (lock_token is not None
                and re.fullmatch(r"[0-9a-f]{32}", str(lock_token)) is None)
            or not owner_token or len(owner_token) > 512 or "\x00" in owner_token
            or not os.path.isabs(storage_root) or os.path.normpath(storage_root) != storage_root
            or os.path.basename(storage_root) != _LOCAL_RESULT_DIR
            or len(storage_root) > _LOCAL_RESULT_MAX_URI or "\x00" in storage_root):
        raise ValueError("checkpoint candidate authority is not canonical")
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id), with_for_update=True)
        latest = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == str(task_id)).order_by(
                DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
        if task is None or checkpoint is None or latest is None:
            raise RuntimeError("checkpoint admission is incomplete")
        now = _durable_task_db_now(s)
        lease = latest.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task.task_kind not in _CHECKPOINT_PARENT_KINDS or task.status != "running"
                or task.cancel_requested or latest.id != str(attempt_id)
                or latest.status != "running" or latest.owner_token != owner_token
                or lease is None or lease <= now):
            raise RuntimeError("checkpoint candidate owner is stale or fenced")
        _lock_local_result_registry(s)
        existing = _linear_checkpoint_candidate_doc(s, task, checkpoint)
        generation = _linear_checkpoint_generation(task, checkpoint, latest.id)
        basename = f"{_LOCAL_RESULT_PREFIX}checkpoint_{generation}.parquet"
        uri = os.path.join(storage_root, basename)
        lock_name = f"{basename[:-len('.parquet')]}.lock"
        if existing is not None:
            expected = (latest.id, namespace_id, storage_root, writer_token, lock_token)
            actual = (existing["attempt_id"], existing["namespace_id"],
                      existing["storage_root"], existing["writer_token"],
                      existing["lock_token"])
            if existing["generation"] != generation or actual != expected:
                raise RuntimeError("checkpoint reservation replay changed exact authority")
            return existing
        if _local_result_candidate(uri) != uri:
            raise ValueError("derived checkpoint candidate is outside the managed namespace")
        artifact = LocalResultArtifact(
            uri=uri, namespace_id=namespace_id, storage_root=storage_root,
            lock_name=lock_name, lock_token=str(lock_token) if lock_token else None,
            lock_protected=lock_token is not None, state="writing",
            writer_run_id=task.id, writer_token=writer_token, created_at=now)
        s.add(artifact)
        # DurableCheckpoint has scalar foreign-key fields rather than an ORM relationship, so
        # SQLAlchemy cannot infer that this insert must precede the candidate binding update.
        # PostgreSQL enforces the FK immediately; establish the artifact authority first while
        # retaining both writes in this transaction.
        s.flush()
        checkpoint.phase = "reserved"
        checkpoint.candidate_uri = uri
        checkpoint.candidate_generation = generation
        checkpoint.candidate_attempt_id = latest.id
        checkpoint.updated_at = now
        s.flush()
        return _linear_checkpoint_candidate_doc(s, task, checkpoint)


def _linear_checkpoint_evidence_shape(
        *, generation, rows, size_bytes, content_sha256, schema_sha256, dev, ino):
    """Reject any non-canonical committed-evidence value before it can bind checkpoint truth."""
    generation = str(generation)
    content_sha256, schema_sha256 = str(content_sha256), str(schema_sha256)
    if (re.fullmatch(r"[0-9a-f]{64}", generation) is None
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", schema_sha256) is None
            or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0
            or not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0
            or not isinstance(dev, int) or isinstance(dev, bool) or dev < 0
            or not isinstance(ino, int) or isinstance(ino, bool) or ino < 0):
        raise ValueError("checkpoint commit evidence is not canonical")
    return (generation, int(rows), int(size_bytes),
            content_sha256, schema_sha256, int(dev), int(ino))


def _linear_checkpoint_committed_doc(s, task: DurableTask, checkpoint: DurableCheckpoint) -> dict:
    """One committed checkpoint is truth only when evidence, artifact, and one owner all agree."""
    if (checkpoint.phase != "committed" or checkpoint.candidate_uri is None
            or checkpoint.candidate_generation is None
            or checkpoint.candidate_attempt_id is None
            or checkpoint.candidate_dev is None or checkpoint.candidate_ino is None
            or checkpoint.committed_rows is None or checkpoint.committed_bytes is None
            or checkpoint.content_sha256 is None or checkpoint.schema_sha256 is None
            or checkpoint.committed_at is None):
        raise RuntimeError("committed checkpoint evidence is incomplete")
    artifact = s.get(LocalResultArtifact, checkpoint.candidate_uri, with_for_update=True)
    candidate_attempt = s.get(
        DurableTaskAttempt, checkpoint.candidate_attempt_id, with_for_update=True)
    refs = list(s.scalars(select(LocalResultReference).where(
        LocalResultReference.uri == checkpoint.candidate_uri).order_by(
            LocalResultReference.owner_kind, LocalResultReference.owner_key).with_for_update()))
    # Exactly one durable owner must exist. Ephemeral read leases are transient readers, not owners,
    # so an active reopen guard must not make committed truth look inconsistent.
    owners = [ref for ref in refs if ref.owner_kind != _LOCAL_RESULT_EPHEMERAL_OWNER_KIND]
    generation = _linear_checkpoint_generation(
        task, checkpoint, checkpoint.candidate_attempt_id)
    if (artifact is None or artifact.uri != checkpoint.candidate_uri
            or artifact.state != "ready" or artifact.committed_at is None
            or artifact.writer_run_id is not None or artifact.writer_token is not None
            or artifact.delete_token is not None or artifact.delete_attempted_at is not None
            or re.fullmatch(r"[0-9a-f]{32}", artifact.namespace_id) is None
            or bool(artifact.lock_protected) != bool(artifact.lock_token)
            or (artifact.lock_token is not None
                and re.fullmatch(r"[0-9a-f]{32}", artifact.lock_token) is None)
            or checkpoint.candidate_generation != generation
            or candidate_attempt is None or candidate_attempt.task_id != task.id
            or len(owners) != 1 or owners[0].owner_kind != _LINEAR_CHECKPOINT_OWNER_KIND
            or owners[0].owner_key != checkpoint.checkpoint_id):
        raise RuntimeError("committed checkpoint truth is inconsistent")
    committed_at = checkpoint.committed_at
    if committed_at.tzinfo is None:
        committed_at = committed_at.replace(tzinfo=datetime.timezone.utc)
    return {
        "task_id": task.id, "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_node_id": checkpoint.checkpoint_node_id,
        "output_port_id": checkpoint.output_port_id,
        "task_intent_sha256": checkpoint.task_intent_sha256,
        "graph_prefix_sha256": checkpoint.graph_prefix_sha256,
        "input_manifest_sha256": checkpoint.input_manifest_sha256,
        "uri": artifact.uri, "generation": generation,
        "attempt_id": checkpoint.candidate_attempt_id,
        "namespace_id": artifact.namespace_id, "storage_root": artifact.storage_root,
        "lock_name": artifact.lock_name, "lock_token": artifact.lock_token,
        "lock_protected": bool(artifact.lock_protected), "state": artifact.state,
        "phase": "committed",
        "rows": int(checkpoint.committed_rows), "bytes": int(checkpoint.committed_bytes),
        "content_sha256": checkpoint.content_sha256, "schema_sha256": checkpoint.schema_sha256,
        "dev": int(checkpoint.candidate_dev), "ino": int(checkpoint.candidate_ino),
        "committed_at": committed_at,
    }


def commit_linear_checkpoint(
        *, task_id: str, attempt_id: str, owner_token: str, namespace_id: str,
        writer_token: str, lock_token: str | None, generation: str,
        rows: int, size_bytes: int, content_sha256: str, schema_sha256: str,
        dev: int, ino: int) -> dict:
    """Fence one exact checkpoint commit under the current DB-time lease and install one owner."""
    task_id, attempt_id, owner_token = str(task_id), str(attempt_id), str(owner_token)
    namespace_id, writer_token = str(namespace_id), str(writer_token)
    lock_token = str(lock_token) if lock_token is not None else None
    (generation, rows, size_bytes, content_sha256, schema_sha256, dev,
     ino) = _linear_checkpoint_evidence_shape(
        generation=generation, rows=rows, size_bytes=size_bytes,
        content_sha256=content_sha256, schema_sha256=schema_sha256, dev=dev, ino=ino)
    if (re.fullmatch(r"[0-9a-f]{32}", namespace_id) is None
            or re.fullmatch(r"[0-9a-f]{32}", writer_token) is None
            or (lock_token is not None and re.fullmatch(r"[0-9a-f]{32}", lock_token) is None)):
        raise ValueError("checkpoint commit authority is not canonical")
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        latest = s.scalar(select(DurableTaskAttempt).where(
            DurableTaskAttempt.task_id == task_id).order_by(
                DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())
        if (task is None or checkpoint is None or latest is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        if checkpoint.phase == "committed":
            # Commit-before-response-loss: return the original evidence, never a fabricated one.
            committed = _linear_checkpoint_committed_doc(s, task, checkpoint)
            if (committed["generation"] != generation or committed["attempt_id"] != attempt_id
                    or committed["namespace_id"] != namespace_id
                    or committed["rows"] != rows or committed["bytes"] != size_bytes
                    or committed["content_sha256"] != content_sha256
                    or committed["schema_sha256"] != schema_sha256
                    or committed["dev"] != dev or committed["ino"] != ino):
                raise RuntimeError("checkpoint commit replay changed committed evidence")
            return committed
        existing = _linear_checkpoint_candidate_doc(s, task, checkpoint)
        if existing is None:
            raise RuntimeError("checkpoint candidate is not reserved for commit")
        now = _durable_task_db_now(s)
        lease = latest.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task.status != "running" or task.cancel_requested
                or latest.id != attempt_id or latest.status != "running"
                or latest.owner_token != owner_token or lease is None or lease <= now):
            raise RuntimeError("checkpoint commit owner is stale or fenced")
        if (existing["generation"] != generation or existing["attempt_id"] != attempt_id
                or existing["namespace_id"] != namespace_id
                or existing["writer_token"] != writer_token
                or existing["lock_token"] != lock_token):
            raise RuntimeError("checkpoint commit does not match the reserved candidate")
        # When materialization identity was bound before commit (#450), the held-FD evidence must
        # name the exact same inode — a path swap cannot establish a different committed identity.
        if existing["dev"] is not None or existing["ino"] is not None:
            if (existing["dev"], existing["ino"]) != (dev, ino):
                raise RuntimeError("checkpoint commit disagrees with materialized identity")
        artifact = s.get(LocalResultArtifact, existing["uri"], with_for_update=True)
        if (artifact is None or artifact.state != "writing"
                or artifact.committed_at is not None
                or artifact.writer_run_id != task.id or artifact.writer_token != writer_token):
            raise RuntimeError("checkpoint artifact is not the current uncommitted writer")
        if s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == artifact.uri).limit(1)) is not None:
            raise RuntimeError("checkpoint artifact already has a reference before commit")
        # Install the sole durable owner, then mark ready and clear the writer pair atomically so no
        # window exists where the committed file is both unreferenced and owner-cleared.
        s.add(LocalResultReference(
            uri=artifact.uri, owner_kind=_LINEAR_CHECKPOINT_OWNER_KIND,
            owner_key=checkpoint.checkpoint_id))
        artifact.state = "ready"
        artifact.committed_at = now
        artifact.writer_run_id = None
        artifact.writer_token = None
        checkpoint.phase = "committed"
        checkpoint.candidate_dev = dev
        checkpoint.candidate_ino = ino
        checkpoint.committed_rows = rows
        checkpoint.committed_bytes = size_bytes
        checkpoint.content_sha256 = content_sha256
        checkpoint.schema_sha256 = schema_sha256
        checkpoint.committed_at = now
        checkpoint.updated_at = now
        s.flush()
        return _linear_checkpoint_committed_doc(s, task, checkpoint)


def bind_linear_checkpoint_materialization(
        *, task_id: str, attempt_id: str, owner_token: str,
        uri: str, dev: int, ino: int) -> dict:
    """Persist the exact materialized inode for a reserved candidate before commit or crash.

    Identity is proven from a held descriptor at materialize/seal time. Reattach and commit refuse
    any later path swap that disagrees with this binding (#450).
    """
    task_id, attempt_id, owner_token = str(task_id), str(attempt_id), str(owner_token)
    uri = str(uri)
    if (not isinstance(dev, int) or isinstance(dev, bool) or dev < 0
            or not isinstance(ino, int) or isinstance(ino, bool) or ino < 0):
        raise ValueError("checkpoint materialization identity is not canonical")
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        latest = _linear_checkpoint_latest_attempt(s, task_id)
        if (task is None or checkpoint is None or latest is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        if checkpoint.phase == "committed":
            committed = _linear_checkpoint_committed_doc(s, task, checkpoint)
            if (committed["dev"], committed["ino"]) != (dev, ino) or committed["uri"] != uri:
                raise RuntimeError("materialization binding disagrees with committed identity")
            return committed
        now = _durable_task_db_now(s)
        lease = latest.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task.status != "running" or latest.id != attempt_id
                or latest.status != "running" or latest.owner_token != owner_token
                or lease is None or lease <= now):
            raise RuntimeError("checkpoint materialization owner is stale or fenced")
        candidate = _linear_checkpoint_candidate_doc(s, task, checkpoint)
        if candidate is None or candidate["uri"] != uri or candidate["attempt_id"] != attempt_id:
            raise RuntimeError("checkpoint materialization does not match the reserved candidate")
        if candidate["dev"] is not None or candidate["ino"] is not None:
            if (candidate["dev"], candidate["ino"]) != (dev, ino):
                raise RuntimeError("checkpoint materialization identity changed")
            return candidate
        checkpoint.candidate_dev = int(dev)
        checkpoint.candidate_ino = int(ino)
        checkpoint.updated_at = now
        s.flush()
        return _linear_checkpoint_candidate_doc(s, task, checkpoint)


def reconcile_linear_checkpoint(task_id: str) -> dict | None:
    """Reconcile after an unknown commit response; committed truth or the exact reserved binding."""
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id), with_for_update=True)
        if task is None and checkpoint is None:
            return None
        if (task is None or checkpoint is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        if checkpoint.phase == "committed":
            return _linear_checkpoint_committed_doc(s, task, checkpoint)
        return {"phase": checkpoint.phase,
                "candidate": _linear_checkpoint_candidate_doc(s, task, checkpoint)}


def linear_checkpoint_committed(task_id: str) -> dict | None:
    """Read exact committed evidence for reopen; None until the checkpoint is committed truth."""
    with session() as s:
        task = _lock_durable_task_for_write(s, str(task_id))
        checkpoint = s.get(DurableCheckpoint, str(task_id), with_for_update=True)
        if (task is None or checkpoint is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        if checkpoint.phase != "committed":
            return None
        _lock_local_result_registry(s)
        return _linear_checkpoint_committed_doc(s, task, checkpoint)


def _linear_checkpoint_latest_attempt(s, task_id: str) -> DurableTaskAttempt | None:
    return s.scalar(select(DurableTaskAttempt).where(
        DurableTaskAttempt.task_id == str(task_id)).order_by(
            DurableTaskAttempt.attempt_number.desc()).limit(1).with_for_update())


def _detach_linear_checkpoint_candidate(
        s, checkpoint: DurableCheckpoint, artifact: LocalResultArtifact | None,
        now) -> str | None:
    """Retire one uncommitted candidate: clear its binding and abandon only its exact writer file."""
    delete_token = None
    if artifact is not None:
        artifact.writer_run_id = None
        artifact.writer_token = None
        artifact.state = "deleting"
        artifact.delete_token = uuid.uuid4().hex
        artifact.delete_attempted_at = now
        delete_token = artifact.delete_token
    checkpoint.phase = "pending"
    checkpoint.candidate_uri = None
    checkpoint.candidate_generation = None
    checkpoint.candidate_attempt_id = None
    checkpoint.candidate_dev = None
    checkpoint.candidate_ino = None
    checkpoint.updated_at = now
    return delete_token


def abort_linear_checkpoint_candidate(
        task_id: str, attempt_id: str, owner_token: str) -> dict:
    """Abort the exact current uncommitted candidate; refuse committed truth; idempotent on replay."""
    task_id, attempt_id, owner_token = str(task_id), str(attempt_id), str(owner_token)
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        latest = _linear_checkpoint_latest_attempt(s, task_id)
        if (task is None or checkpoint is None or latest is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        if checkpoint.phase == "committed":
            raise RuntimeError("cannot abort a committed checkpoint")
        now = _durable_task_db_now(s)
        lease = latest.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task.status != "running" or latest.id != attempt_id
                or latest.status != "running" or latest.owner_token != owner_token
                or lease is None or lease <= now):
            raise RuntimeError("checkpoint abort owner is stale or fenced")
        candidate = _linear_checkpoint_candidate_doc(s, task, checkpoint)
        if candidate is None:
            # Response-loss replay: the exact candidate is already retired and reclaimable.
            return {"uri": None, "delete_token": None, "lock_token": None,
                    "namespace_id": None, "lock_name": None}
        if candidate["attempt_id"] != attempt_id:
            raise RuntimeError("checkpoint abort does not own the current generation")
        artifact = s.get(LocalResultArtifact, candidate["uri"], with_for_update=True)
        token = _detach_linear_checkpoint_candidate(s, checkpoint, artifact, now)
        s.flush()
        return {"uri": candidate["uri"], "delete_token": token,
                "lock_token": candidate["lock_token"], "namespace_id": candidate["namespace_id"],
                "lock_name": candidate["lock_name"]}


def reattach_or_retire_linear_checkpoint(
        task_id: str, attempt_id: str, owner_token: str) -> dict:
    """Recover after loss: reattach the exact same-generation candidate or retire a superseded one.

    Only the current unexpired attempt may act. A candidate produced by the same attempt is safely
    reattached with its generation preserved; a candidate stranded by a fenced older attempt is
    retired and abandoned before any replacement generation is reserved. Committed truth is never
    mutated — a late owner only reads it back.
    """
    task_id, attempt_id, owner_token = str(task_id), str(attempt_id), str(owner_token)
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        latest = _linear_checkpoint_latest_attempt(s, task_id)
        if (task is None or checkpoint is None or latest is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        _lock_local_result_registry(s)
        if checkpoint.phase == "committed":
            return {"action": "committed",
                    "committed": _linear_checkpoint_committed_doc(s, task, checkpoint)}
        now = _durable_task_db_now(s)
        lease = latest.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (task.status != "running" or latest.id != attempt_id
                or latest.status != "running" or latest.owner_token != owner_token
                or lease is None or lease <= now):
            raise RuntimeError("checkpoint recovery owner is stale or fenced")
        candidate = _linear_checkpoint_candidate_doc(s, task, checkpoint)
        if candidate is None:
            return {"action": "reserve"}
        if candidate["attempt_id"] == attempt_id:
            return {"action": "reattach", "candidate": candidate}
        artifact = s.get(LocalResultArtifact, candidate["uri"], with_for_update=True)
        token = _detach_linear_checkpoint_candidate(s, checkpoint, artifact, now)
        s.flush()
        return {"action": "retire", "uri": candidate["uri"], "delete_token": token,
                "lock_token": candidate["lock_token"], "namespace_id": candidate["namespace_id"],
                "lock_name": candidate["lock_name"]}


def release_linear_checkpoint(task_id: str) -> dict | None:
    """Explicitly release one committed checkpoint: drop its owner and full hidden lifecycle in order.

    Validation fails closed and preserves primary truth; only a fully consistent committed set is
    released. The artifact keeps its bytes but becomes unreferenced, so bounded GC may reclaim it.
    Idempotent: a lifecycle already removed reports no release.
    """
    task_id = str(task_id)
    with session() as s:
        task = _lock_durable_task_for_write(s, task_id)
        checkpoint = s.get(DurableCheckpoint, task_id, with_for_update=True)
        if task is None and checkpoint is None:
            return None
        if (task is None or checkpoint is None
                or task.task_kind not in _CHECKPOINT_PARENT_KINDS):
            raise RuntimeError("checkpoint admission is incomplete")
        latest = _linear_checkpoint_latest_attempt(s, task_id)
        _lock_local_result_registry(s)
        committed = _linear_checkpoint_committed_doc(s, task, checkpoint)
        now = _durable_task_db_now(s)
        if latest is not None and latest.status == "running":
            lease = latest.lease_until
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=datetime.timezone.utc)
            if lease is not None and lease > now:
                raise RuntimeError("cannot release a checkpoint with a live attempt lease")
        artifact = s.get(LocalResultArtifact, committed["uri"], with_for_update=True)
        for ref in s.scalars(select(LocalResultReference).where(
                LocalResultReference.uri == committed["uri"],
                LocalResultReference.owner_kind == _LINEAR_CHECKPOINT_OWNER_KIND,
        ).with_for_update()):
            s.delete(ref)
        if artifact is not None:
            artifact.updated_at = now
        s.delete(checkpoint)
        s.flush()
        for attempt in s.scalars(select(DurableTaskAttempt).where(
                DurableTaskAttempt.task_id == task.id).with_for_update()):
            s.delete(attempt)
        s.flush()
        s.delete(task)
        return {"released": True, "uri": committed["uri"],
                "namespace_id": committed["namespace_id"],
                "checkpoint_id": committed["checkpoint_id"]}


def _purge_linear_checkpoint_for_delete(
        s, task: DurableTask, checkpoint: DurableCheckpoint) -> None:
    """Fail-closed teardown of one terminal hidden checkpoint before its rows are deleted.

    Committed truth drops its sole durable owner and keeps its bytes reclaimable; an uncommitted
    candidate is retired and abandoned. Only exact owner/writer state is touched, and any disagreement
    with committed/candidate truth raises so a canvas is never deleted over inconsistent state.
    """
    if task.task_kind not in _CHECKPOINT_PARENT_KINDS:
        return
    if checkpoint.phase == "committed":
        committed = _linear_checkpoint_committed_doc(s, task, checkpoint)
        s.get(LocalResultArtifact, committed["uri"], with_for_update=True)
        for ref in s.scalars(select(LocalResultReference).where(
                LocalResultReference.uri == committed["uri"],
                LocalResultReference.owner_kind == _LINEAR_CHECKPOINT_OWNER_KIND,
        ).with_for_update()):
            s.delete(ref)
        s.flush()
        return
    candidate = _linear_checkpoint_candidate_doc(s, task, checkpoint)
    if candidate is None:
        return
    now = _db_now(s)
    artifact = s.get(LocalResultArtifact, candidate["uri"], with_for_update=True)
    _detach_linear_checkpoint_candidate(s, checkpoint, artifact, now)
    s.flush()


def linear_checkpoint_restore_audit() -> list[dict]:
    """Validate every hidden checkpoint forms one complete consistency set; raise to reject a restore.

    A committed set must have consistent evidence, a ready artifact, and exactly one durable owner; an
    uncommitted set must have a coherent candidate binding; and no durable owner may dangle without a
    committed checkpoint. Any missing/mismatched member raises rather than fabricating or dropping
    truth, so backup/restore accepts or rejects the lifecycle as a unit.
    """
    report: list[dict] = []
    with session() as s:
        tasks = {t.id: t for t in s.scalars(select(DurableTask).where(
            DurableTask.task_kind.in_(_CHECKPOINT_PARENT_KINDS)).order_by(DurableTask.id))}
        checkpoints = list(s.scalars(
            select(DurableCheckpoint).order_by(DurableCheckpoint.task_id)))
        _lock_local_result_registry(s)
        committed_keys: set[str] = set()
        for checkpoint in checkpoints:
            task = tasks.get(checkpoint.task_id)
            if task is None:
                raise RuntimeError("restored checkpoint has no owning hidden task")
            if checkpoint.phase == "committed":
                doc = _linear_checkpoint_committed_doc(s, task, checkpoint)
                committed_keys.add(checkpoint.checkpoint_id)
                report.append({"task_id": task.id, "phase": "committed", "uri": doc["uri"]})
            else:
                candidate = _linear_checkpoint_candidate_doc(s, task, checkpoint)
                report.append({"task_id": task.id, "phase": checkpoint.phase,
                               "uri": candidate["uri"] if candidate else None})
        for ref in s.scalars(select(LocalResultReference).where(
                LocalResultReference.owner_kind == _LINEAR_CHECKPOINT_OWNER_KIND).order_by(
                    LocalResultReference.owner_key)):
            if ref.owner_key not in committed_keys:
                raise RuntimeError("restored durable checkpoint owner has no committed checkpoint")
    return report


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


def _uri_bound_as_checkpoint_candidate(s, uri: str) -> bool:
    """A durable_checkpoints.candidate_uri binding is a live reclaim reference (#449)."""
    return s.scalar(select(DurableCheckpoint.task_id).where(
        DurableCheckpoint.candidate_uri == uri).limit(1)) is not None


def _checkpoint_candidate_uri_exists():
    """SQL EXISTS correlating a local-result URI to a live checkpoint candidate binding."""
    return select(DurableCheckpoint.candidate_uri).where(
        DurableCheckpoint.candidate_uri == LocalResultArtifact.uri).exists()


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
        # Never clear the writer fence of a live checkpoint candidate: reclaim would then delete the
        # exact file #426 must reattach after crash (#449).
        predicates = (
            LocalResultArtifact.namespace_id == namespace_id,
            LocalResultArtifact.lock_protected.is_(True),
            LocalResultArtifact.state.in_(("writing", "ready")),
            or_(LocalResultArtifact.writer_run_id.is_not(None), lease_exists),
            ~_checkpoint_candidate_uri_exists(),
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
        # Defense in depth for #449: a candidate binding is not proof the writer died.
        if _uri_bound_as_checkpoint_candidate(s, uri):
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
        # A durable_checkpoints.candidate_uri binding is a reclaim reference even with no
        # LocalResultReference row yet (uncommitted reserved candidates, #449).
        no_checkpoint = ~_checkpoint_candidate_uri_exists()
        rows = list(s.scalars(select(LocalResultArtifact).where(
            LocalResultArtifact.namespace_id == namespace_id,
            LocalResultArtifact.lock_protected.is_(True),
            LocalResultArtifact.state.in_(("writing", "ready")),
            LocalResultArtifact.writer_run_id.is_(None), no_reference, no_checkpoint,
        ).order_by(LocalResultArtifact.created_at, LocalResultArtifact.uri)
          .limit(remaining).with_for_update()))
        for row in rows:
            if s.scalar(select(LocalResultReference.uri).where(
                    LocalResultReference.uri == row.uri).limit(1)) is not None:
                continue
            if _uri_bound_as_checkpoint_candidate(s, row.uri):
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
        if _uri_bound_as_checkpoint_candidate(s, uri):
            raise RuntimeError("cannot delete a checkpoint-bound local result")
        delete_file()
        for binding in s.scalars(select(LocalFileInputRevision).where(
                LocalFileInputRevision.artifact_uri == uri).with_for_update()):
            s.delete(binding)
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
    """The canonical ``{"outputs": [...]}`` cache document for a plan hash, or ``None``."""
    with session() as s:
        r = s.get(ResultCache, key)
        return json.loads(r.doc) if r else None


def acquire_result_cache_pin(
        key: str, owner: str, ttl_seconds: float = 300,
        ) -> tuple[dict | None, list[str] | None]:
    """Atomically read the current cache pointer and pin every managed region generation.

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
        from hub.run_outputs import committed_document_outputs
        outputs = committed_document_outputs(doc)
        if set(doc) != {"outputs"} or not outputs:
            return None, None
        uris = [str(output.uri).strip().rstrip("/") for output in outputs]
        if any(not uri for uri in uris) or len(uris) != len(set(uris)):
            return None, None
        managed_uris = sorted({uri for uri in uris if object_attempt_uri_shape(uri)})
        attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(managed_uris)
        ).order_by(ObjectAttempt.uri).with_for_update())} if managed_uris else {}
        missing = set(managed_uris) - set(attempts)
        if missing:
            for uri in sorted(missing):
                _validated_object_uri(uri, attempt=True)
            raise FileNotFoundError("cached object attempt has no lifecycle ownership row")
        if any(attempt.kind != "region" or attempt.state != "published"
               for attempt in attempts.values()):
            raise FileNotFoundError(
                "cached managed result set is not currently published")
        expected_refs = {
            slot: uri for slot, uri in _result_doc_refs(doc).items() if uri in attempts
        }
        cache_refs = list(s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "result_cache",
            ObjectAttemptRef.ref_key == str(key),
        ).order_by(ObjectAttemptRef.ref_slot).with_for_update()))
        if ({ref.ref_slot: ref.attempt_uri for ref in cache_refs} != expected_refs
                or any(attempts[ref.attempt_uri].generation != ref.generation
                       for ref in cache_refs)):
            raise FileNotFoundError(
                "cached managed result set has incomplete lifecycle ownership")
        local_uris = _local_result_owner_candidates("result_cache", (doc,))
        if local_uris:
            _lock_local_result_registry(s)
            local_refs = list(s.scalars(select(LocalResultReference).where(
                LocalResultReference.owner_kind == "result_cache",
                LocalResultReference.owner_key == str(key),
            ).order_by(LocalResultReference.uri)))
            if {ref.uri for ref in local_refs} != set(local_uris):
                raise FileNotFoundError(
                    "cached local result set has incomplete lifecycle ownership")
            artifacts = {artifact.uri: artifact for artifact in s.scalars(
                select(LocalResultArtifact).where(
                    LocalResultArtifact.uri.in_(local_uris))
                .order_by(LocalResultArtifact.uri).with_for_update())}
            if (set(artifacts) != set(local_uris)
                    or any(artifact.state != "ready" for artifact in artifacts.values())):
                raise FileNotFoundError(
                    "cached local result set is not currently available")
        pin_ids: list[str] = []
        for uri in uris:
            attempt = attempts.get(uri)
            if attempt is None:
                continue
            pin_id = uuid.uuid4().hex
            _put_lease(s, attempt, "read", str(owner), ttl_seconds, lease_id=pin_id)
            s.add(ObjectAttemptRef(
                ref_type="result_reader", ref_key=pin_id, ref_slot="",
                attempt_uri=attempt.uri, generation=attempt.generation,
            ))
            pin_ids.append(pin_id)
        return doc, pin_ids


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
    execution_manifest_candidates: set[str | None] = set()
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
                if any(uri in inherited_uris for uri in _result_doc_uris(cache.doc)):
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
                    execution_manifest_candidates.update(
                        _delete_catalog_children(s, [current_uri]))
                    entry = s.get(CatalogEntry, current_uri, with_for_update=True)
                    if entry is not None:
                        s.delete(entry)
                execution_manifest_candidates.update(
                    _delete_catalog_governance(s, logical.catalog_key))
                logical.current_uri = None
                logical.catalog_epoch += 1
                logical.state = "unregistered"
                logical.metadata_version += 1
                logical.governance_doc = "{}"

            remaining_entries = list(s.scalars(select(CatalogEntry).where(
                CatalogEntry.uri.in_(inherited_uris)).order_by(CatalogEntry.uri).with_for_update()))
            for entry in remaining_entries:
                execution_manifest_candidates.update(
                    _delete_catalog_children(s, [entry.uri]))
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
        s.flush()
        _delete_unreferenced_execution_manifests(
            s, execution_manifest_candidates)
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
    logical_id, catalog_key = _catalog_managed_namespace_identity(
        logical_uri, catalog_key_base)
    logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=True)
    if logical is None:
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
                            catalog_key_base: str | None = None,
                            require_live_preallocation: bool = False) -> dict:
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
        if require_live_preallocation:
            state = _lock_existing_run_identity(s, run_id)
            backend = s.get(RunBackendJob, run_id, with_for_update=True)
            now = _db_now(s)
            if (state is None
                    or state.status not in _PREALLOCATION_STATES
                    or state.preallocation_token is None
                    or not _run_preallocation_active(state.preallocation_expires_at, now)
                    or backend is not None):
                raise RuntimeError(
                    "managed sink allocation requires a live unbound run preallocation")
        managed_namespace = None
        if kind == "sink":
            managed_namespace = _catalog_managed_namespace_identity(
                logical_uri, str(catalog_key_base))
            _lock_catalog_namespace_tokens(
                s, [logical_uri, *managed_namespace])
            _assert_managed_catalog_namespace_available(
                s, logical_uri=logical_uri,
                logical_id=managed_namespace[0], catalog_key=managed_namespace[1])
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


def _replace_attempt_refs(
        s, ref_type: str, ref_key: str, refs: dict[str, str] | None,
        *, publish: bool = False, publish_kind: str | None = None,
        required_kind: str | None = None) -> list[str]:
    """Atomically replace one owner's complete named-output attempt-ref set.

    The durable owner row is the caller's serialization point. We discover its old set without a ref
    lock, lock the union of old/new attempts in URI order, and only then lock and replace the refs in
    slot order. This preserves the global attempt -> ref -> local-registry order while ensuring a reader
    can observe either complete generation set, never a mixture. ``publish`` promotes committed attempts;
    ``publish_kind`` can restrict that authority when the owner only publishes one attempt kind.
    """
    owner_type, owner_key = str(ref_type), str(ref_key)
    normalized: dict[str, str] = {}
    for raw_slot, raw_uri in (refs or {}).items():
        slot = str(raw_slot)
        uri = str(raw_uri).strip().rstrip("/")
        if not uri:
            raise ValueError("object attempt reference URI cannot be empty")
        if slot in normalized:
            raise ValueError("object attempt reference slots must be unique")
        normalized[slot] = uri

    owner_predicate = (
        ObjectAttemptRef.ref_type == owner_type,
        ObjectAttemptRef.ref_key == owner_key,
    )
    observed = list(s.scalars(select(ObjectAttemptRef).where(
        *owner_predicate).order_by(ObjectAttemptRef.ref_slot)))
    observed_refs = {ref.ref_slot: ref.attempt_uri for ref in observed}
    candidate_uris = sorted(set(observed_refs.values()) | set(normalized.values()))
    attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
        ObjectAttempt.uri.in_(candidate_uris)
    ).order_by(ObjectAttempt.uri).with_for_update())} if candidate_uris else {}

    for uri in normalized.values():
        if uri not in attempts and object_attempt_uri_shape(uri):
            _validated_object_uri(uri, attempt=True)
            raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")

    current = list(s.scalars(select(ObjectAttemptRef).where(
        *owner_predicate).order_by(ObjectAttemptRef.ref_slot).with_for_update()))
    current_refs = {ref.ref_slot: ref.attempt_uri for ref in current}
    if current_refs != observed_refs:
        raise RuntimeError("object attempt reference set changed without its owner lock")

    managed_refs = {
        slot: attempts[uri] for slot, uri in normalized.items() if uri in attempts
    }
    published_uris: set[str] = set()
    published_at = None
    for slot in sorted(managed_refs):
        attempt = managed_refs[slot]
        if required_kind is not None and attempt.kind != required_kind:
            raise RuntimeError(
                f"object attempt reference requires kind {required_kind!r}")
        if attempt.state in _TERMINAL_ATTEMPT_STATES:
            raise RuntimeError(
                f"cannot reference object attempt in state {attempt.state!r}")
        if publish and (publish_kind is None or attempt.kind == publish_kind):
            if attempt.state not in ("committed", "published"):
                raise RuntimeError(
                    "object attempt must be committed before pointer publication")
            if published_at is None:
                published_at = _db_now(s)
            attempt.state = "published"
            attempt.published_at = attempt.published_at or published_at
            published_uris.add(attempt.uri)
        elif attempt.state != "published":
            raise RuntimeError(
                "run history and state may reference only a published object attempt")

    for ref in current:
        s.delete(ref)
    s.flush()
    for slot in sorted(managed_refs):
        attempt = managed_refs[slot]
        s.add(ObjectAttemptRef(
            ref_type=owner_type,
            ref_key=owner_key,
            ref_slot=slot,
            attempt_uri=attempt.uri,
            generation=attempt.generation,
        ))
    s.flush()
    if published_uris:
        leases = list(s.scalars(select(ObjectAttemptLease).where(
            ObjectAttemptLease.attempt_uri.in_(sorted(published_uris)),
            ObjectAttemptLease.lease_type == "publish",
        ).order_by(
            ObjectAttemptLease.attempt_uri, ObjectAttemptLease.lease_id,
        ).with_for_update()))
        for lease in leases:
            s.delete(lease)
        s.flush()

    superseded: list[str] = []
    retained_uris = {attempt.uri for attempt in managed_refs.values()}
    now = _db_now(s)
    for old_uri in sorted(set(observed_refs.values()) - retained_uris):
        old = attempts.get(old_uri)
        if old is not None and _maybe_supersede(s, old, now):
            superseded.append(old_uri)
    return superseded


def _replace_attempt_ref(s, ref_type: str, ref_key: str, uri: str | None,
                         *, publish: bool = False) -> list[str]:
    """Singleton-owner compatibility wrapper over the complete-set primitive."""
    return _replace_attempt_refs(
        s, ref_type, ref_key, {"": str(uri)} if uri else {}, publish=publish)


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


def _result_cache_pin_ids(pin_ids: list[str]) -> list[str]:
    normalized = [str(pin_id) for pin_id in pin_ids if str(pin_id)]
    if len(normalized) != len(set(normalized)):
        raise ValueError("result cache pin IDs must be unique")
    return sorted(normalized)


def renew_result_cache_pins(pin_ids: list[str], ttl_seconds: float = 300) -> bool:
    """Renew one cache hit's complete pin set in a single transaction."""
    keys = _result_cache_pin_ids(pin_ids)
    if not keys:
        return True
    with session() as s:
        identities = list(s.execute(select(
            ObjectAttemptLease.lease_id,
            ObjectAttemptLease.attempt_uri,
            ObjectAttemptLease.generation,
        ).where(
            ObjectAttemptLease.lease_id.in_(keys),
        ).order_by(ObjectAttemptLease.lease_id)))
        if {row.lease_id for row in identities} != set(keys):
            return False
        uris = sorted({row.attempt_uri for row in identities})
        attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri).with_for_update())}
        refs = list(s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "result_reader",
            ObjectAttemptRef.ref_key.in_(keys),
        ).order_by(ObjectAttemptRef.ref_key, ObjectAttemptRef.ref_slot).with_for_update()))
        leases = list(s.scalars(select(ObjectAttemptLease).where(
            ObjectAttemptLease.lease_id.in_(keys),
        ).order_by(ObjectAttemptLease.lease_id).with_for_update()))
        refs_by_key = {ref.ref_key: ref for ref in refs if ref.ref_slot == ""}
        leases_by_key = {lease.lease_id: lease for lease in leases}
        if (len(refs) != len(keys) or set(refs_by_key) != set(keys)
                or set(leases_by_key) != set(keys) or set(attempts) != set(uris)):
            return False
        for key in keys:
            ref, lease = refs_by_key[key], leases_by_key[key]
            attempt = attempts.get(lease.attempt_uri)
            if (attempt is None or lease.generation != attempt.generation
                    or ref.attempt_uri != attempt.uri
                    or ref.generation != attempt.generation):
                return False
        for key in keys:
            lease = leases_by_key[key]
            attempt = attempts[lease.attempt_uri]
            _put_lease(
                s, attempt, lease.lease_type, lease.owner, ttl_seconds,
                lease_id=lease.lease_id)
        return True


def release_result_cache_pins(pin_ids: list[str]) -> None:
    """Release one cache hit's complete reader ref/lease set atomically."""
    keys = _result_cache_pin_ids(pin_ids)
    if not keys:
        return
    with session() as s:
        lease_uris = list(s.scalars(select(ObjectAttemptLease.attempt_uri).where(
            ObjectAttemptLease.lease_id.in_(keys))))
        ref_uris = list(s.scalars(select(ObjectAttemptRef.attempt_uri).where(
            ObjectAttemptRef.ref_type == "result_reader",
            ObjectAttemptRef.ref_key.in_(keys),
        )))
        uris = sorted(set(lease_uris) | set(ref_uris))
        attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri).with_for_update())} \
            if uris else {}
        refs = list(s.scalars(select(ObjectAttemptRef).where(
            ObjectAttemptRef.ref_type == "result_reader",
            ObjectAttemptRef.ref_key.in_(keys),
        ).order_by(ObjectAttemptRef.ref_key, ObjectAttemptRef.ref_slot).with_for_update()))
        leases = list(s.scalars(select(ObjectAttemptLease).where(
            ObjectAttemptLease.lease_id.in_(keys),
        ).order_by(ObjectAttemptLease.lease_id).with_for_update()))
        current_uris = {
            *[ref.attempt_uri for ref in refs],
            *[lease.attempt_uri for lease in leases],
        }
        if not current_uris.issubset(attempts):
            raise RuntimeError("result cache pin ownership changed concurrently")
        for ref in refs:
            s.delete(ref)
        for lease in leases:
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
            if s.scalar(select(exists().where(
                    ObjectAttemptRef.attempt_uri == row.uri))):
                raise RuntimeError(
                    "cannot quarantine an object attempt with a durable reference")
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

        def still_unowned(rows: list[ObjectAttempt]) -> list[ObjectAttempt]:
            """Recheck ownership in a fresh statement after candidate attempt locks are held.

            PostgreSQL evaluates the correlated NOT EXISTS predicates before a conflicting row lock
            wait. A publisher can therefore commit a ref while GC is waiting and leave the original
            SELECT snapshot stale. Every ref/lease creator takes the attempt lock first; once these
            locks are ours, this fresh read both sees prior winners and prevents a later one until commit.
            """
            uris = [row.uri for row in rows]
            if not uris:
                return []
            referenced = set(s.scalars(select(ObjectAttemptRef.attempt_uri).where(
                ObjectAttemptRef.attempt_uri.in_(uris)
            )))
            leased = set(s.scalars(select(ObjectAttemptLease.attempt_uri).where(
                ObjectAttemptLease.attempt_uri.in_(uris),
                ObjectAttemptLease.expires_at > now,
            )))
            return [row for row in rows if row.uri not in referenced and row.uri not in leased]

        retention_cutoff = now - datetime.timedelta(seconds=retention)
        committed_orphans = still_unowned(list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state == "committed",
                ObjectAttempt.terminal_proof_at.is_not(None),
                ObjectAttempt.terminal_proof_at <= retention_cutoff,
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        )))
        for row in committed_orphans:
            row.state = "abandoned"
        s.flush()

        observations = still_unowned(list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state == "abandoned",
                ObjectAttempt.terminal_proof_at.is_not(None),
                ObjectAttempt.inventory_complete.is_(False),
                or_(ObjectAttempt.quiet_until.is_(None), ObjectAttempt.quiet_until <= now),
                no_refs, no_leases,
            ).order_by(ObjectAttempt.created_at, ObjectAttempt.uri)
            .limit(remaining()).with_for_update()
        )))
        actions.extend(_object_attempt_action(row, "observe") for row in observations)
        if remaining() <= 0:
            return actions

        grace_cutoff = now - datetime.timedelta(seconds=grace)
        candidates = still_unowned(list(s.scalars(
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
        )))
        for row in candidates:
            row.state, row.next_delete_at = "delete_pending", now
        s.flush()

        claimable = still_unowned(list(s.scalars(
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
        )))
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


def run_output_ref_slot(node_id: str, port_id: str) -> str:
    """Canonical semantic ownership slot for one declared output port."""
    node, port = str(node_id), str(port_id)
    if not node or not port:
        raise ValueError("run output reference slots require node and port identities")
    return json.dumps([node, port], ensure_ascii=True, separators=(",", ":"))


def _result_doc_outputs(raw: str | dict | None):
    try:
        doc = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    from hub.run_outputs import outputs_from_document
    # Run state/history documents contain the canonical collection alongside status metadata, while
    # result-cache documents are exactly {"outputs": [...]}.  Extract only that collection here; the
    # cache write/read boundaries separately enforce their exact document shape.
    return [output for output in outputs_from_document({"outputs": doc.get("outputs")})
            if output.outcome == "committed"]


def _result_doc_refs(raw: str | dict | None) -> dict[str, str]:
    refs: dict[str, str] = {}
    for output in _result_doc_outputs(raw):
        slot = run_output_ref_slot(output.node_id, output.port_id)
        uri = str(output.uri).strip().rstrip("/")
        if not uri:
            raise ValueError("committed run output URI cannot be empty")
        refs[slot] = uri
    return refs


def _result_doc_uris(raw: str | dict | None) -> list[str]:
    return list(_result_doc_refs(raw).values())


def put_result(key: str, doc: dict) -> list[str]:
    """Atomically replace a cache row and its durable object/local artifact reference."""
    from hub.run_outputs import committed_document_outputs
    outputs = committed_document_outputs(doc)
    if set(doc) != {"outputs"} or not outputs:
        raise ValueError(
            "result cache requires a complete committed output set with known row counts")
    output_uris = [str(output.uri).strip().rstrip("/") for output in outputs]
    if any(not uri for uri in output_uris) or len(output_uris) != len(set(output_uris)):
        raise ValueError("result cache outputs must reference distinct physical URIs")
    payload = json.dumps(doc, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) > 65_536:
        raise ValueError("result cache output document exceeds 65536 encoded bytes")
    new_refs = _result_doc_refs(doc)
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
        retired.extend(_replace_attempt_refs(
            s, "result_cache", key, new_refs, publish=True,
            required_kind="region"))
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


def _catalog_managed_namespace_identity(
        logical_uri: str, catalog_key_base: str) -> tuple[str, str]:
    """Return every derived stable identifier reserved by one managed logical target."""
    logical_id = _catalog_logical_id(logical_uri)
    base = str(catalog_key_base).strip() or logical_id
    catalog_key = f"{base}_{hashlib.sha256(logical_uri.encode()).hexdigest()[:16]}"
    return logical_id, catalog_key


def _lock_catalog_namespace_tokens(s, tokens: list[str]) -> None:
    """Serialize claims of managed logical aliases with durable unmanaged publication.

    PostgreSQL row locks cannot protect an absent row in either namespace table, so both claim paths
    take the same transaction advisory lock for every token they may create. SQLite already serializes
    catalog writers through the installation-identity write fence.
    """
    canonical = sorted(set(str(token).rstrip("/") for token in tokens if token))
    if s.get_bind().dialect.name == "sqlite":
        _catalog_sqlite_write_fence(s)
        return
    if s.get_bind().dialect.name != "postgresql":
        return
    for token in canonical:
        digest = hashlib.sha256(
            f"catalog-namespace:v1\0{token}".encode("utf-8")).digest()
        lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
        s.execute(select(func.pg_advisory_xact_lock(lock_id)))


def _assert_managed_catalog_namespace_available(
        s, *, logical_uri: str, logical_id: str, catalog_key: str) -> None:
    """Reject a managed allocation while an unmanaged projection owns a reserved alias."""
    tokens = sorted({logical_uri, logical_id, catalog_key})
    conflict = s.scalars(select(CatalogEntry).where(
        CatalogEntry.uri.in_(tokens), CatalogEntry.logical_id.is_(None),
    ).order_by(CatalogEntry.uri).with_for_update()
        .execution_options(populate_existing=True)).first()
    if conflict is not None:
        raise RuntimeError(
            "managed catalog namespace is occupied by an unmanaged catalog entry")


def _assert_unmanaged_catalog_namespace_available(s, uri: str) -> None:
    """Reject a first unmanaged apply for every active or tombstoned managed alias."""
    conflict = s.scalars(select(CatalogLogicalDataset).where(or_(
        CatalogLogicalDataset.logical_uri == uri,
        CatalogLogicalDataset.catalog_key == uri,
        CatalogLogicalDataset.logical_id == uri,
    )).order_by(CatalogLogicalDataset.logical_id).with_for_update()
        .execution_options(populate_existing=True)).first()
    if conflict is not None:
        raise RuntimeError(
            "catalog namespace is reserved for a managed logical dataset")


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
                                   exact_current_attempts: set[str] | None = None,
                                   allow_inactive: bool = False) -> list[dict]:
    """Resolve curation targets, lock managed logical rows in deterministic order, and fence stale
    physical generations. ``allow_inactive`` preserves only a managed tombstone's stable identity as an
    unknown target; active rows with a missing current projection still fail as corruption. Attempt
    identity is read before logical locks because its publication epoch and sequence are immutable;
    governance paths do not need to lock the attempt row itself."""
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
        .order_by(CatalogLogicalDataset.logical_id).with_for_update()
        .execution_options(populate_existing=True))} if logical_ids else {}
    managed_entry_uris = sorted({
        str(row.current_uri) for row in logical_rows.values() if row.current_uri
    })
    entry_uris = sorted(set(managed_entry_uris) | unmanaged_uris)
    entries = {row.uri: row for row in s.scalars(
        select(CatalogEntry).where(CatalogEntry.uri.in_(entry_uris))
        .order_by(CatalogEntry.uri).with_for_update()
        .execution_options(populate_existing=True))} if entry_uris else {}

    for target in resolved:
        logical_id = target["logical_id"]
        if logical_id:
            logical = logical_rows.get(logical_id)
            if (allow_inactive and logical is not None
                    and logical.state == "unregistered" and not logical.current_uri):
                target.update({
                    "known": False, "catalog_key": logical.catalog_key,
                    "current_uri": None, "logical": logical, "entry": None,
                })
                continue
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


_CATALOG_LINEAGE_FIELDS = {
    "idempotency_key", "run_id", "attempt_id", "producer", "producer_version",
    "step_id", "provenance", "field_mappings",
}
_CATALOG_LINEAGE_MAX_PARENTS = 5_000
_CATALOG_LINEAGE_MAX_URI_LENGTH = 8_192
_CATALOG_LINEAGE_MAX_VERSION_LENGTH = 512


def _catalog_lineage_uri(value: object, *, field: str) -> str:
    """Validate one URI/key before it can become durable lineage or a replay identity."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"catalog lineage {field} must be a non-empty canonical string")
    canonical = value.rstrip("/")
    if not canonical:
        raise ValueError(f"catalog lineage {field} cannot contain only path separators")
    if len(canonical) > _CATALOG_LINEAGE_MAX_URI_LENGTH:
        raise ValueError(
            f"catalog lineage {field} exceeds {_CATALOG_LINEAGE_MAX_URI_LENGTH} characters")
    return canonical


def catalog_lineage_uri(value: object) -> str:
    """Validate one public lineage graph root using the persisted fact URI contract."""
    return _catalog_lineage_uri(value, field="root URI")


def _catalog_lineage_identity_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _catalog_lineage_version(value: object, *, field: str) -> str | None:
    """Keep persisted versions inside the public LineageFact/RunOutput wire contract."""
    if value is None:
        return None
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > _CATALOG_LINEAGE_MAX_VERSION_LENGTH):
        raise ValueError(
            f"catalog lineage {field} must be a non-empty canonical string of at most "
            f"{_CATALOG_LINEAGE_MAX_VERSION_LENGTH} characters")
    return value


def catalog_lineage_parent_tokens(parents: list[str] | None) -> list[str]:
    """Return the one canonical source-token set shared by every publication path."""
    if parents is None:
        return []
    if not isinstance(parents, list):
        raise ValueError("catalog lineage parents must be a list")
    if len(parents) > _CATALOG_LINEAGE_MAX_PARENTS:
        raise ValueError(
            f"catalog lineage parents exceed {_CATALOG_LINEAGE_MAX_PARENTS} entries")
    return sorted({
        _catalog_lineage_uri(parent, field="parent") for parent in parents
    })


def _catalog_lineage_canonical(lineage: dict | None, parent_count: int) -> dict | None:
    """Validate the catalog-local wire shape and require its already-canonical representation."""
    if lineage is None:
        return None
    if not isinstance(lineage, dict) or set(lineage) != _CATALOG_LINEAGE_FIELDS:
        raise ValueError("catalog lineage has an invalid field set")
    from hub.models import LineagePublication
    canonical = LineagePublication.model_validate(lineage).model_dump()
    if json.dumps(lineage, sort_keys=True, separators=(",", ":")) != json.dumps(
            canonical, sort_keys=True, separators=(",", ":")):
        raise ValueError("catalog lineage must use canonical field mapping order")
    if (canonical["producer_version"] is not None
            and canonical["producer_version"] > 2**53 - 1):
        raise ValueError("catalog lineage producer version exceeds the JSON safe-integer range")
    mappings_json = json.dumps(
        canonical["field_mappings"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(mappings_json.encode("utf-8")) > 64 * 1024:
        raise ValueError("catalog lineage field mappings exceed 64 KiB")
    if parent_count != 1 and canonical["field_mappings"]:
        raise ValueError(
            "catalog lineage field mappings require exactly one source per publication")
    return {**canonical, "field_mappings_json": mappings_json}


def _catalog_lineage_publication_semantic(lineage: dict) -> dict:
    """Return only caller-supplied lineage fields, excluding the derived storage encoding."""
    return {
        key: lineage[key] for key in sorted(_CATALOG_LINEAGE_FIELDS)
    }


def _catalog_lineage_publication_identity(
        *, destination_uri: str, destination_version: str | None,
        parent_tokens: list[str], lineage: dict) -> tuple[str, str]:
    """Return the durable key and exact request fingerprint for one lineage publication."""
    destination_uri = _catalog_lineage_uri(
        destination_uri, field="destination URI")
    destination_version = _catalog_lineage_version(
        destination_version, field="destination version")
    parent_tokens = catalog_lineage_parent_tokens(parent_tokens)
    publication_key = "lineage-publication:v1:sha256:" + hashlib.sha256(
        lineage["idempotency_key"].encode("utf-8")
    ).hexdigest()
    semantic = {
        "schema_version": 2,
        "idempotency_key": lineage["idempotency_key"],
        "parents": parent_tokens,
        "destination_uri": destination_uri,
        "destination_version": destination_version,
        "lineage": _catalog_lineage_publication_semantic(lineage),
    }
    fingerprint = "lineage-publication:v2:sha256:" + hashlib.sha256(json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return publication_key, fingerprint


def _catalog_require_lineage_publication(
        s, *, destination_uri: str, destination_version: str | None,
        parent_tokens: list[str], lineage: dict) -> str:
    """Validate that an existing output receipt belongs to this exact lineage request."""
    publication_key, fingerprint = _catalog_lineage_publication_identity(
        destination_uri=destination_uri,
        destination_version=destination_version,
        parent_tokens=parent_tokens,
        lineage=lineage,
    )
    event = s.get(CatalogPublicationEvent, publication_key)
    if (event is None or event.effect_type != "lineage"
            or event.uri != str(destination_uri).rstrip("/")
            or event.version != destination_version
            or event.fingerprint != fingerprint):
        raise RuntimeError(f"catalog publication key collision: {publication_key}")
    return publication_key


def _catalog_reserve_lineage_publication(
        s, *, destination_uri: str, destination_version: str | None,
        parent_tokens: list[str], lineage: dict) -> tuple[str, bool]:
    """Reserve one complete caller request before resolving its mutable catalog projection.

    The reservation deliberately binds raw, canonical request tokens rather than registration IDs or
    whichever source versions happen to be current. Those resolved values belong to the immutable facts
    created by the first apply. A later exact retry therefore remains a no-op after overwrite or
    unregister, while changing the source set or any lineage metadata is still a key collision.
    """
    publication_key, fingerprint = _catalog_lineage_publication_identity(
        destination_uri=destination_uri,
        destination_version=destination_version,
        parent_tokens=parent_tokens,
        lineage=lineage,
    )
    applied = _catalog_publication_event_once(
        s, publication_key, "lineage", destination_uri,
        destination_version, fingerprint)
    return publication_key, applied


def _catalog_doc_version(doc: str | None) -> str | None:
    try:
        version = json.loads(doc or "{}").get("version")
    except (AttributeError, TypeError, ValueError):
        version = None
    return _catalog_lineage_version(version, field="version")


def _catalog_entry_version(entry: CatalogEntry | None) -> str | None:
    return _catalog_doc_version(entry.doc) if entry is not None else None


def _catalog_sqlite_write_fence(s) -> None:
    """Acquire SQLite's single writer transaction before reading catalog publication identity.

    SQLite ignores ``SELECT FOR UPDATE``. Without an early write, unregister or overwrite can commit
    after source validation and before the fact insert. PostgreSQL keeps its finer row-lock ordering.
    """
    if s.get_bind().dialect.name != "sqlite":
        return
    result = s.execute(update(InstallationIdentity).where(
        InstallationIdentity.id == _INSTALLATION_ID,
    ).values(owner_token=InstallationIdentity.owner_token))
    if result.rowcount != 1:
        raise RuntimeError("catalog publication installation identity is missing")


@dataclass(frozen=True)
class _CatalogLineageParentSnapshot:
    token: str
    logical_id: str | None
    attempt_epoch: int | None
    entry_uri: str | None
    entry_version: str | None
    entry_registration_id: str | None
    resolved_source_key: str | None = None
    resolved_source_uri: str | None = None
    logical_state: str | None = None
    logical_current_uri: str | None = None
    logical_catalog_epoch: int | None = None
    logical_catalog_key: str | None = None
    logical_uri: str | None = None


def _catalog_lineage_logical_parent_snapshot(
        s, token: str, logical: CatalogLogicalDataset) -> _CatalogLineageParentSnapshot:
    """Resolve any managed alias against the same pre-mutation logical generation."""
    if logical.state == "active" and logical.current_uri:
        current = s.get(CatalogEntry, logical.current_uri)
        source_key, source_uri = logical.catalog_key, logical.current_uri
        entry_uri = logical.current_uri
        entry_version = _catalog_entry_version(current)
        entry_registration_id = current.registration_id if current is not None else None
    else:
        source_key = logical.logical_uri if token == logical.catalog_key else token
        source_uri = source_key
        entry_uri = entry_version = entry_registration_id = None
    return _CatalogLineageParentSnapshot(
        token, logical.logical_id, None,
        entry_uri, entry_version, entry_registration_id,
        resolved_source_key=source_key, resolved_source_uri=source_uri,
        logical_state=logical.state, logical_current_uri=logical.current_uri,
        logical_catalog_epoch=logical.catalog_epoch,
        logical_catalog_key=logical.catalog_key, logical_uri=logical.logical_uri,
    )


def _catalog_lineage_parent_snapshot(s, token: str) -> _CatalogLineageParentSnapshot:
    """Remember whether a source was a concrete catalog entry before taking mutation locks."""
    attempt = s.get(ObjectAttempt, token)
    if attempt is not None and attempt.logical_id:
        return _CatalogLineageParentSnapshot(
            token, attempt.logical_id, attempt.catalog_epoch, None, None, None)
    logical = s.scalars(select(CatalogLogicalDataset).where(or_(
        CatalogLogicalDataset.logical_id == token,
        CatalogLogicalDataset.catalog_key == token,
        CatalogLogicalDataset.logical_uri == token,
        CatalogLogicalDataset.current_uri == token,
    )).order_by(CatalogLogicalDataset.logical_id).limit(1)).first()
    if logical is not None:
        return _catalog_lineage_logical_parent_snapshot(s, token, logical)
    entry = s.execute(select(
        CatalogEntry.uri, CatalogEntry.logical_id, CatalogEntry.doc,
        CatalogEntry.registration_id,
    ).where(CatalogEntry.uri == token).limit(1)).first()
    if entry is None:
        entry = s.execute(select(
            CatalogEntry.uri, CatalogEntry.logical_id, CatalogEntry.doc,
            CatalogEntry.registration_id,
        ).where(or_(
            CatalogEntry.tbl_id == token, CatalogEntry.name == token,
        )).order_by(CatalogEntry.uri).limit(1)).first()
    if entry is None:
        return _CatalogLineageParentSnapshot(token, None, None, None, None, None)
    if entry.logical_id:
        logical = s.get(CatalogLogicalDataset, entry.logical_id)
        if logical is None:
            raise RuntimeError("catalog lineage entry has no logical source identity")
        return _catalog_lineage_logical_parent_snapshot(s, token, logical)
    return _CatalogLineageParentSnapshot(
        token, None, None, entry.uri, _catalog_doc_version(entry.doc), entry.registration_id)


def _catalog_validate_lineage_parent_entries(
        snapshots: list[_CatalogLineageParentSnapshot],
        locked_entries: dict[str, CatalogEntry]) -> None:
    """Fence unregister or overwrite that won after the initial identity snapshot."""
    for snapshot in snapshots:
        if snapshot.entry_uri is None:
            continue
        entry = locked_entries.get(snapshot.entry_uri)
        if (entry is None
                or entry.registration_id != snapshot.entry_registration_id
                or _catalog_entry_version(entry) != snapshot.entry_version):
            raise RuntimeError("catalog lineage entry changed concurrently")


def _catalog_validate_lineage_parent_logicals(
        snapshots: list[_CatalogLineageParentSnapshot],
        locked_logicals: dict[str, CatalogLogicalDataset]) -> None:
    """Fence a stable alias against resolving through a different current generation."""
    for snapshot in snapshots:
        if snapshot.logical_state is None or snapshot.logical_id is None:
            continue
        logical = locked_logicals.get(snapshot.logical_id)
        if (logical is None
                or logical.state != snapshot.logical_state
                or logical.current_uri != snapshot.logical_current_uri
                or logical.catalog_epoch != snapshot.logical_catalog_epoch
                or logical.catalog_key != snapshot.logical_catalog_key
                or logical.logical_uri != snapshot.logical_uri):
            raise RuntimeError("catalog lineage logical source changed concurrently")


def _catalog_lineage_source_snapshot(
        s, snapshot: _CatalogLineageParentSnapshot,
        locked_logicals: dict[str, CatalogLogicalDataset],
        locked_entries: dict[str, CatalogEntry]) -> dict:
    """Freeze the source identity and exact physical generation used by this publication."""
    parent = snapshot.token
    if snapshot.resolved_source_uri is not None:
        return {
            "source_key": snapshot.resolved_source_key,
            "source_uri": snapshot.resolved_source_uri,
            "source_version": snapshot.entry_version,
            "source_registration_id": snapshot.entry_registration_id,
        }
    if snapshot.entry_uri is not None:
        return {
            "source_key": snapshot.entry_uri,
            "source_uri": snapshot.entry_uri,
            "source_version": snapshot.entry_version,
            "source_registration_id": snapshot.entry_registration_id,
        }
    parent_logical = locked_logicals.get(snapshot.logical_id) \
        if snapshot.logical_id is not None else None
    current_parent_attempt = s.execute(select(
        ObjectAttempt.logical_id, ObjectAttempt.catalog_epoch,
    ).where(ObjectAttempt.uri == parent)).one_or_none() \
        if snapshot.attempt_epoch is not None else None
    attempt_is_current_epoch = (
        snapshot.attempt_epoch is None or (
            current_parent_attempt is not None
            and current_parent_attempt.logical_id == snapshot.logical_id
            and current_parent_attempt.catalog_epoch == snapshot.attempt_epoch
            and parent_logical is not None
            and snapshot.attempt_epoch == parent_logical.catalog_epoch
        )
    )
    if (parent_logical is not None and parent_logical.state == "active"
            and parent_logical.current_uri and attempt_is_current_epoch):
        source_key = parent_logical.catalog_key
        source_uri = parent if snapshot.attempt_epoch is not None else parent_logical.current_uri
    elif parent_logical is not None and parent == parent_logical.catalog_key:
        # Never retain an inactive stable key: re-registering the logical URI must not inherit facts.
        source_key = parent_logical.logical_uri
        source_uri = parent_logical.logical_uri
    else:
        source_key = parent
        source_uri = parent
    entry = locked_entries.get(source_uri)
    return {
        "source_key": _catalog_lineage_uri(source_key, field="source key"),
        "source_uri": _catalog_lineage_uri(source_uri, field="source URI"),
        "source_version": _catalog_entry_version(entry),
        "source_registration_id": entry.registration_id if entry is not None else None,
    }


def _catalog_insert_lineage_fact(
        s, *, publication_key: str, source: dict, destination_key: str, destination_uri: str,
        destination_version: str | None, lineage: dict) -> bool:
    public_source = {
        key: source[key] for key in ("source_key", "source_uri", "source_version")
    }
    fact_key = "lineage-fact:v1:sha256:" + hashlib.sha256(
        (publication_key + "\0" + source["source_key"]).encode("utf-8")
    ).hexdigest()
    execution_manifest_sha256 = (
        _retain_execution_manifest_for_run_in_session(s, lineage["run_id"])
        if lineage["run_id"] is not None else None
    )
    semantic = {
        "schema_version": 1,
        "fact_key": fact_key,
        "publication_key": publication_key,
        **public_source,
        "destination_key": destination_key,
        "destination_uri": destination_uri,
        "destination_version": destination_version,
        "run_id": lineage["run_id"],
        "execution_manifest_sha256": execution_manifest_sha256,
        "attempt_id": lineage["attempt_id"],
        "producer": lineage["producer"],
        "producer_version": lineage["producer_version"],
        "step_id": lineage["step_id"],
        "provenance": lineage["provenance"],
        "field_mappings": lineage["field_mappings"],
    }
    # Last durable-boundary guard: no built-in publication path may create a fact that the public
    # exporter cannot validate and paginate past. This also catches a long resolved catalog key/URI
    # even when the caller supplied a short alias.
    from hub.models import LineageFact
    created_at = _now()
    LineageFact.model_validate({
        "id": "1",
        **{key: value for key, value in semantic.items() if key != "schema_version"},
        "created_at": created_at,
    })
    fingerprint = "lineage-fact:v1:sha256:" + hashlib.sha256(json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    values = {
        "fact_key": fact_key,
        "publication_key": publication_key,
        "fingerprint": fingerprint,
        **public_source,
        "destination_key": destination_key,
        "destination_uri": destination_uri,
        "source_key_hash": _catalog_lineage_identity_hash(source["source_key"]),
        "destination_key_hash": _catalog_lineage_identity_hash(destination_key),
        "source_uri_hash": _catalog_lineage_identity_hash(source["source_uri"]),
        "destination_uri_hash": _catalog_lineage_identity_hash(destination_uri),
        "destination_version": destination_version,
        "run_id": lineage["run_id"],
        "execution_manifest_sha256": execution_manifest_sha256,
        "attempt_id": lineage["attempt_id"],
        "producer": lineage["producer"],
        "producer_version": lineage["producer_version"],
        "step_id": lineage["step_id"],
        "provenance": lineage["provenance"],
        "field_mappings_json": lineage["field_mappings_json"],
        "created_at": created_at,
    }
    dialect = s.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - the product supports SQLite and PostgreSQL metadata stores
        raise RuntimeError(f"unsupported catalog lineage dialect: {dialect}")
    inserted_id = s.scalar(dialect_insert(CatalogLineageFact).values(
        **values,
    ).on_conflict_do_nothing(
        index_elements=[CatalogLineageFact.fact_key],
    ).returning(CatalogLineageFact.id))
    winner = s.scalars(select(CatalogLineageFact).where(
        CatalogLineageFact.fact_key == fact_key).limit(1)).first()
    if winner is None:  # pragma: no cover - defensive database contract check
        raise RuntimeError("catalog lineage fact reservation failed")
    persisted = {
        key: getattr(winner, key) for key in values if key != "created_at"
    }
    expected = {key: value for key, value in values.items() if key != "created_at"}
    if persisted != expected:
        raise RuntimeError(
            f"catalog lineage idempotency key collision: {lineage['idempotency_key']}")
    return inserted_id is not None


def _catalog_apply_lineage_in_session(
        s, destination_key: str, destination_uri: str,
        destination_version: str | None,
        parent_snapshots: list[_CatalogLineageParentSnapshot],
        locked_logicals: dict[str, CatalogLogicalDataset],
        locked_entries: dict[str, CatalogEntry], lineage: dict | None,
        publication_key: str | None) -> int:
    """Insert immutable facts for the first reserved publication without moving a pointer."""
    if publication_key is None:
        return 0
    if lineage is None:
        raise ValueError("catalog publication with sources requires lineage identity")
    sources: dict[str, dict] = {}
    for snapshot in parent_snapshots:
        source = _catalog_lineage_source_snapshot(
            s, snapshot, locked_logicals, locked_entries)
        prior = sources.get(source["source_key"])
        if prior is not None and prior != source:
            raise ValueError(
                "catalog lineage publication names multiple versions of one source identity")
        sources[source["source_key"]] = source
    inserted = 0
    for source_key in sorted(sources):
        inserted += int(_catalog_insert_lineage_fact(
            s, publication_key=publication_key, source=sources[source_key],
            destination_key=destination_key,
            destination_uri=destination_uri,
            destination_version=destination_version, lineage=lineage))
    return inserted


def _catalog_upsert_in_session(s, uri: str, name: str, doc: dict,
                               parents: list[str] | None = None,
                               pipeline: str | None = None,
                               lineage: dict | None = None, *,
                               publication_event_key: str | None = None,
                               publication_fingerprint: str | None = None,
                               backend_publication: dict | None = None,
                               lineage_replay_noop: bool = True,
                               require_unmanaged: bool = False,
                               requested_destination_version: str | None = None) -> bool:
    if require_unmanaged:
        _lock_catalog_namespace_tokens(s, [uri])
    else:
        _catalog_sqlite_write_fence(s)
    parent_tokens = catalog_lineage_parent_tokens(parents)
    canonical_lineage = _catalog_lineage_canonical(lineage, len(parent_tokens))
    destination_version = (
        _catalog_lineage_version(doc.get("version"), field="destination version")
        if canonical_lineage is not None or publication_event_key else None
    )
    lineage_reservation_version = (
        _catalog_lineage_version(
            requested_destination_version, field="requested destination version")
        if require_unmanaged else destination_version
    )
    lineage_publication_key = None
    # Composite publications use one global reservation order on every backend and execution path:
    # lineage header -> output receipt -> catalog/lifecycle projection mutation. This keeps managed and
    # unmanaged callers from taking the two unique event keys in opposite orders under PostgreSQL.
    if canonical_lineage is not None:
        lineage_publication_key, lineage_applied = _catalog_reserve_lineage_publication(
            s,
            destination_uri=uri,
            destination_version=lineage_reservation_version,
            parent_tokens=parent_tokens,
            lineage=canonical_lineage,
        )
        if not lineage_applied:
            if lineage_replay_noop:
                return False
            lineage_publication_key = None
    if publication_event_key:
        if not publication_fingerprint:
            raise RuntimeError("catalog publication requires an exact effect fingerprint")
        applied = (
            _catalog_unmanaged_output_event_once(
                s, publication_event_key, uri, destination_version,
                publication_fingerprint)
            if require_unmanaged else _catalog_publication_event_once(
                s, publication_event_key, "output", uri,
                destination_version, publication_fingerprint)
        )
        if not applied:
            return False
    if require_unmanaged:
        _assert_unmanaged_catalog_namespace_available(s, uri)
    attempt_identity = s.get(ObjectAttempt, uri)
    if require_unmanaged and (
            attempt_identity is not None or object_attempt_uri_shape(uri)):
        raise RuntimeError(
            "managed catalog output requires the core object-lifecycle publication receipt")
    if attempt_identity is None and object_attempt_uri_shape(uri):
        _validated_object_uri(uri, attempt=True)
        raise RuntimeError("attempt-shaped object URI has no lifecycle ownership row")
    logical = None
    old_uri = None
    logical_id = None
    locked_entries: dict[str, CatalogEntry] = {}
    attempt = attempt_identity
    parent_snapshots = [
        _catalog_lineage_parent_snapshot(s, token)
        for token in parent_tokens
    ]
    if attempt_identity is not None:
        if attempt_identity.kind != "sink":
            raise RuntimeError("catalog cannot publish a region attempt")
        if (not attempt_identity.logical_id or attempt_identity.catalog_epoch is None
                or attempt_identity.publish_seq is None):
            raise RuntimeError("object sink attempt has no reserved logical publication identity")
        logical_id = attempt_identity.logical_id
    logical_ids = sorted(({logical_id} if logical_id is not None else set()) | {
        snapshot.logical_id for snapshot in parent_snapshots
        if snapshot.logical_id is not None
    })
    locked_logicals = {row.logical_id: row for row in s.scalars(
        select(CatalogLogicalDataset).where(
            CatalogLogicalDataset.logical_id.in_(logical_ids))
        .order_by(CatalogLogicalDataset.logical_id).with_for_update()
        .execution_options(populate_existing=True))} if logical_ids else {}
    _catalog_validate_lineage_parent_logicals(parent_snapshots, locked_logicals)
    lineage_entry_uris: set[str] = set()
    for snapshot in parent_snapshots:
        if snapshot.entry_uri is not None:
            lineage_entry_uris.add(snapshot.entry_uri)
            continue
        parent = snapshot.token
        parent_logical = locked_logicals.get(snapshot.logical_id) \
            if snapshot.logical_id is not None else None
        if (parent_logical is not None and parent_logical.state == "active"
                and parent_logical.current_uri
                and (snapshot.attempt_epoch is None
                     or snapshot.attempt_epoch == parent_logical.catalog_epoch)):
            lineage_entry_uris.add(
                parent if snapshot.attempt_epoch is not None else parent_logical.current_uri)
        elif parent_logical is not None and parent == parent_logical.catalog_key:
            lineage_entry_uris.add(parent_logical.logical_uri)
        else:
            lineage_entry_uris.add(parent)
    if attempt_identity is not None:
        logical = locked_logicals.get(logical_id)
        if logical is None:
            raise RuntimeError("object sink attempt logical publication identity is missing")
        old_uri = logical.current_uri
        attempt_uris = sorted({candidate for candidate in (
            uri, old_uri, *(snapshot.token for snapshot in parent_snapshots
                            if snapshot.attempt_epoch is not None),
        ) if candidate})
        locked_attempts = {row.uri: row for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(attempt_uris)).order_by(ObjectAttempt.uri).with_for_update()
            .execution_options(populate_existing=True))}
        attempt = locked_attempts.get(uri)
        if attempt is None or (old_uri and old_uri not in locked_attempts):
            raise RuntimeError("catalog publication ownership changed concurrently")
        ref_predicates = [and_(
            ObjectAttemptRef.ref_type == "catalog",
            ObjectAttemptRef.ref_key == logical_id,
        ), and_(
            ObjectAttemptRef.ref_type == "backend_publication",
            ObjectAttemptRef.attempt_uri.in_(select(ObjectAttempt.uri).where(
                ObjectAttempt.logical_id == logical_id)),
        )]
        locked_refs = {(row.ref_type, row.ref_key): row for row in s.scalars(
            select(ObjectAttemptRef).where(or_(*ref_predicates))
            .order_by(ObjectAttemptRef.ref_type, ObjectAttemptRef.ref_key)
            .with_for_update().execution_options(populate_existing=True)
        )}
        entry_uris = sorted(set(attempt_uris) | lineage_entry_uris)
        locked_entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(entry_uris)).order_by(CatalogEntry.uri).with_for_update()
            .execution_options(populate_existing=True))}
        _catalog_validate_lineage_parent_entries(parent_snapshots, locked_entries)
        if attempt.state not in ("committed", "published"):
            raise RuntimeError("object sink attempt lacks terminal proof or exact inventory")
        if (attempt.logical_id, attempt.catalog_epoch, attempt.publish_seq) != (
                attempt_identity.logical_id, attempt_identity.catalog_epoch,
                attempt_identity.publish_seq):
            raise RuntimeError("catalog publication identity changed concurrently")
        if attempt.catalog_epoch != logical.catalog_epoch:
            raise RuntimeError("object sink attempt was fenced by catalog unregister")
        backend_refs = [ref for (ref_type, _ref_key), ref in locked_refs.items()
                        if ref_type == "backend_publication"]
        if backend_publication is not None:
            ref_key = str(backend_publication.get("ref_key") or "")
            ref = locked_refs.get(("backend_publication", ref_key))
            if (backend_publication.get("run_id") != attempt.run_id
                    or backend_publication.get("generation") != attempt.generation
                    or ref is None or ref.attempt_uri != attempt.uri
                    or ref.generation != attempt.generation):
                raise RuntimeError(
                    "managed catalog publication lost its exact backend temporary reference")
            if len(backend_refs) != 1:
                raise BackendPublicationBusy(
                    "another staged backend publication owns the same logical dataset")
            if not publication_event_key or not publication_fingerprint:
                raise RuntimeError(
                    "managed catalog publication requires an exact durable event identity")
        elif backend_refs:
            raise BackendPublicationBusy(
                "managed catalog publication is fenced by a staged backend publication")
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

    if logical is None:
        entry_uris = sorted({uri, *lineage_entry_uris})
        locked_entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(entry_uris)).order_by(CatalogEntry.uri).with_for_update()
            .execution_options(populate_existing=True))}
        _catalog_validate_lineage_parent_entries(parent_snapshots, locked_entries)
    tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
    payload = json.dumps(doc, default=str)
    entry = locked_entries.get(uri)
    previous_entry_name = entry.name if entry is not None else None
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
    locked_entries[uri] = entry
    _sync_children(s, uri, tags, cols)
    s.flush()

    if logical is not None and logical_id is not None:
        logical.current_uri = uri
        logical.current_publish_seq = int(attempt.publish_seq)
        logical.state = "active"
        logical.governance_doc = json.dumps(_catalog_governance(doc), default=str, sort_keys=True)
        _replace_attempt_ref(s, "catalog", logical_id, uri, publish=True)
    child_key = logical.catalog_key if logical is not None else uri
    _catalog_apply_lineage_in_session(
        s, child_key, uri,
        destination_version,
        parent_snapshots, locked_logicals, locked_entries, canonical_lineage,
        lineage_publication_key)
    _workspace_ensure_root_placement_in_session(
        s, target_kind="dataset", target_id=entry.registration_id, name=entry.name)
    if previous_entry_name is not None:
        _workspace_follow_target_name_in_session(
            s, target_kind="dataset", target_id=entry.registration_id,
            previous_name=previous_entry_name, name=entry.name)
    _materialize_folder(s, entry.folder or "")
    _workspace_sync_dataset_folder_in_session(
        s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")
    return True


_MANAGED_CATALOG_PLAN_FIELDS = {
    "contract_version", "run_id", "step_id", "ref_key", "generation", "event_key",
    "name", "uri", "version", "parents", "pipeline", "lineage", "table_doc", "fingerprint",
}


def validate_managed_catalog_publication_plan(plan: dict) -> dict:
    """Validate one immutable, pre-probed managed catalog effect without touching its artifact."""
    from hub.models import CatalogTable

    if not isinstance(plan, dict) or set(plan) != _MANAGED_CATALOG_PLAN_FIELDS:
        raise ValueError("managed catalog publication plan has an invalid field set")
    if plan.get("contract_version") != 2:
        raise ValueError("managed catalog publication plan has an unsupported version")
    for key in (
            "run_id", "step_id", "ref_key", "event_key", "name", "uri", "version",
            "fingerprint"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise ValueError(f"managed catalog publication plan has an invalid {key}")
    if plan["ref_key"] != f"{plan['run_id']}:{plan['step_id']}":
        raise ValueError("managed catalog publication plan has an invalid temporary reference key")
    if (isinstance(plan.get("generation"), bool)
            or not isinstance(plan.get("generation"), int)
            or plan["generation"] < 1):
        raise ValueError("managed catalog publication plan has an invalid generation")
    uri = _validated_object_uri(plan["uri"], attempt=True)
    parents = plan.get("parents")
    try:
        canonical_parents = catalog_lineage_parent_tokens(parents)
    except ValueError as exc:
        raise ValueError("managed catalog publication plan has invalid parents") from exc
    if parents != canonical_parents:
        raise ValueError("managed catalog publication plan has invalid parents")
    if plan.get("pipeline") is not None and not isinstance(plan["pipeline"], str):
        raise ValueError("managed catalog publication plan has an invalid pipeline")
    canonical_lineage = _catalog_lineage_canonical(plan.get("lineage"), len(parents))
    if parents and canonical_lineage is None:
        raise ValueError("managed catalog publication with sources requires lineage identity")
    if canonical_lineage is not None:
        comparable = {key: value for key, value in canonical_lineage.items()
                      if key != "field_mappings_json"}
        if plan["lineage"] != comparable:
            raise ValueError("managed catalog publication lineage is not canonical")
        if comparable["idempotency_key"] != plan["event_key"]:
            raise ValueError("managed catalog publication lineage identity changed after preflight")
        if (comparable["provenance"] == "run"
                and comparable["step_id"] != plan["step_id"]):
            raise ValueError("managed catalog publication lineage step identity does not match")
    if not isinstance(plan.get("table_doc"), dict):
        raise ValueError("managed catalog publication plan has no exact table document")
    table = CatalogTable.model_validate(plan["table_doc"])
    if (table.model_dump(by_alias=True) != plan["table_doc"]
            or table.name != plan["name"] or table.uri.rstrip("/") != uri
            or table.version != plan["version"]):
        raise ValueError("managed catalog publication table document changed after preflight")
    unsigned = {key: value for key, value in plan.items() if key != "fingerprint"}
    expected = "managed-output:v2:sha256:" + hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    if plan["fingerprint"] != expected:
        raise ValueError("managed catalog publication fingerprint does not match its plan")
    return dict(plan)


def managed_catalog_publication_identity(uri: str, run_id: str) -> dict:
    """Return the exact committed sink generation to bind into a pre-effects catalog plan."""
    normalized, run_id = _validated_object_uri(uri, attempt=True), str(run_id)
    with session() as s:
        attempt = s.get(ObjectAttempt, normalized)
        if (attempt is None or attempt.kind != "sink" or attempt.run_id != run_id
                or attempt.state != "committed"):
            raise RuntimeError("managed catalog publication sink is not an exact committed attempt")
        return {"uri": attempt.uri, "generation": attempt.generation}


def _catalog_output_event_receipt_in_session(
        s, event_key: str, uri: str, version: str | None,
        fingerprint: str) -> dict | None:
    event = s.get(CatalogPublicationEvent, str(event_key), with_for_update=True)
    if event is None:
        return None
    if (event.effect_type != "output" or event.uri != uri or event.version != version
            or event.fingerprint != fingerprint):
        raise RuntimeError(f"catalog publication key collision: {event_key}")
    return {
        "event_key": event.event_key, "uri": event.uri,
        "version": event.version, "fingerprint": event.fingerprint,
    }


def catalog_apply_managed_publication(plan: dict) -> dict:
    """Apply a pre-probed SQL-only managed effect, or attest its exact prior event receipt."""
    validated = validate_managed_catalog_publication_plan(plan)
    with session() as s:
        # A durable event is a complete exact receipt. Replays after finish has released the temporary
        # ref, or after a later generation became current, must remain successful and side-effect free.
        receipt = _catalog_output_event_receipt_in_session(
            s, validated["event_key"], validated["uri"], validated["version"],
            validated["fingerprint"],
        )
        if receipt is not None:
            return receipt
        _catalog_upsert_in_session(
            s, validated["uri"], validated["name"], dict(validated["table_doc"]),
            parents=validated["parents"], pipeline=validated["pipeline"],
            lineage=validated["lineage"],
            publication_event_key=validated["event_key"],
            publication_fingerprint=validated["fingerprint"],
            backend_publication={
                "run_id": validated["run_id"],
                "ref_key": validated["ref_key"],
                "generation": validated["generation"],
            },
            lineage_replay_noop=False,
        )
        receipt = _catalog_output_event_receipt_in_session(
            s, validated["event_key"], validated["uri"], validated["version"],
            validated["fingerprint"],
        )
        if receipt is None:
            raise RuntimeError("managed catalog publication did not record its exact event")
        return receipt


def catalog_upsert_entry(uri: str, name: str, doc: dict, *,
                         parents: list[str] | None = None,
                         pipeline: str | None = None,
                         lineage: dict | None = None) -> bool:
    """Write-through a catalog entry (registered dataset / written output) to the shared DB, keyed by
    uri, so other instances + a restart see it. `doc` is the full CatalogTable model_dump; its folder /
    owner / description / row_count / tags / column-names are mirrored to indexed columns + join tables
    so browse/search/facet push down to the DB. `usage` (popularity) is owned by the column and NOT
    overwritten from the doc — it's bumped independently on reads."""
    with session() as s:
        normalized = (
            _catalog_lineage_uri(uri, field="destination URI")
            if lineage is not None else str(uri).rstrip("/")
        )
        payload = dict(doc)
        payload["folder"] = catalog_folder_normalize(payload.get("folder") or "")
        applied = _catalog_upsert_in_session(
            s, normalized, name, payload, parents=parents, pipeline=pipeline,
            lineage=lineage)
        if not applied:
            return False
        sync_local_result_owner(s, "catalog_entry", normalized, normalized, payload)
        _materialize_folder(s, payload.get("folder") or "")  # a registered dataset's folder is first-class
        entry = s.get(CatalogEntry, normalized)
        if entry is None:
            raise RuntimeError("catalog entry disappeared during Workspace projection")
        _workspace_sync_dataset_folder_in_session(
            s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")
        return True


class ManagedLocalWriteConflict(RuntimeError):
    """A typed local-write precondition no longer matches its durable destination head."""


def _canonical_managed_local_write_intent(value: object) -> tuple[object, dict, str]:
    from hub.models import WriteIntent

    intent = WriteIntent.model_validate(value)
    payload = intent.model_dump(by_alias=True, mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return intent, payload, canonical


def _managed_local_write_receipt_in_session(
        s, idempotency_key: str, intent_doc: str, *, lock: bool = False) -> dict | None:
    query = select(ManagedLocalFileRevision).where(
        ManagedLocalFileRevision.write_idempotency_key == str(idempotency_key)).limit(1)
    if lock:
        query = query.with_for_update()
    revision = s.scalars(query).first()
    if revision is None:
        return None
    if revision.write_intent_doc != intent_doc:
        raise RuntimeError(f"managed local write idempotency key collision: {idempotency_key}")
    try:
        from hub.models import WriteReceipt

        receipt = WriteReceipt.model_validate(json.loads(revision.write_receipt_doc or ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("managed local write receipt is invalid") from exc
    ref = s.get(LocalResultReference, {
        "uri": revision.artifact_uri,
        "owner_kind": "managed_file_revision",
        "owner_key": revision.revision_id,
    })
    if (receipt.dataset_id != revision.logical_id
            or receipt.revision_id != revision.revision_id
            or receipt.publication.artifact_uri != revision.artifact_uri
            or receipt.publication.publish_sequence != revision.publish_seq
            or receipt.publication.idempotency_key != idempotency_key
            or receipt.provenance.publication.run_id != revision.run_id
            or receipt.execution_manifest_sha256 != revision.execution_manifest_sha256
            or ref is None):
        raise RuntimeError("managed local write receipt lost its exact revision evidence")
    return receipt.model_dump(by_alias=True, mode="json")


def _managed_local_lance_write_receipt_in_session(
        s, idempotency_key: str, intent_doc: str, *, lock: bool = False) -> dict | None:
    query = select(ManagedLocalLanceWriteReceipt).where(
        ManagedLocalLanceWriteReceipt.idempotency_key == str(idempotency_key)).limit(1)
    if lock:
        query = query.with_for_update()
    row = s.scalars(query).first()
    if row is None:
        return None
    if row.write_intent_doc != intent_doc:
        raise RuntimeError(f"managed local write idempotency key collision: {idempotency_key}")
    try:
        from hub.models import WriteReceipt

        receipt = WriteReceipt.model_validate(json.loads(row.write_receipt_doc))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("managed local Lance write receipt is invalid") from exc
    if (receipt.dataset_id != row.dataset_id
            or receipt.revision_id != row.revision_id
            or receipt.publication.provider != "managed-local-lance"
            or receipt.publication.logical_uri != row.logical_uri
            or receipt.publication.artifact_uri != row.logical_uri
            or receipt.publication.idempotency_key != idempotency_key
            or receipt.provenance.publication.run_id != row.run_id
            or receipt.execution_manifest_sha256 != row.execution_manifest_sha256):
        raise RuntimeError("managed local Lance write receipt lost its exact revision evidence")
    return receipt.model_dump(by_alias=True, mode="json")


def _validate_managed_local_lance_destination_in_session(
        s, intent, *, lock: bool) -> CatalogEntry:
    if intent.mode != "append" or intent.destination.provider != "managed-local-lance":
        raise ValueError("managed local Lance writes support append only")
    if intent.partitions:
        raise ValueError("managed local Lance append does not support partitions")
    query = select(CatalogEntry).where(
        CatalogEntry.uri == intent.destination.logical_uri).limit(1)
    if lock:
        query = query.with_for_update()
    entry = s.scalars(query).first()
    if (entry is None or entry.logical_id is not None
            or entry.registration_id != intent.destination.dataset_id):
        raise ManagedLocalWriteConflict("append destination does not exist")
    if entry.name != intent.destination.name:
        raise ManagedLocalWriteConflict("append destination identity changed")
    if not entry.uri.lower().endswith(".lance"):
        raise ManagedLocalWriteConflict("append destination is not Lance")
    return entry


def _managed_local_write_head_in_session(s, logical_uri: str, *, lock: bool = False) -> dict | None:
    logical_id = _catalog_logical_id(logical_uri)
    logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=lock)
    if logical is None:
        return None
    revision = None
    if logical.current_uri:
        query = select(ManagedLocalFileRevision).where(
            ManagedLocalFileRevision.logical_id == logical.logical_id,
            ManagedLocalFileRevision.artifact_uri == logical.current_uri,
        ).limit(1)
        if lock:
            query = query.with_for_update()
        revision = s.scalars(query).first()
    name = None
    if revision is not None:
        try:
            name = json.loads(revision.table_doc).get("name")
        except (TypeError, ValueError):
            name = None
    return {
        "dataset_id": logical.logical_id,
        "logical_uri": logical.logical_uri,
        "state": logical.state,
        "artifact_uri": logical.current_uri,
        "revision_id": revision.revision_id if revision is not None else None,
        "publish_seq": revision.publish_seq if revision is not None else None,
        "name": name,
    }


def _validate_managed_local_write_precondition(s, intent, *, lock: bool) -> dict | None:
    head = _managed_local_write_head_in_session(
        s, intent.destination.logical_uri, lock=lock)
    if intent.mode == "create":
        if head is not None:
            raise ManagedLocalWriteConflict("create destination identity already exists")
        return None
    if (head is None or head["state"] != "active" or head["revision_id"] is None
            or head["artifact_uri"] is None):
        raise ManagedLocalWriteConflict("replace destination does not exist")
    expected = intent.expected_head
    if (head["dataset_id"] != intent.destination.dataset_id
            or expected is None
            or head["dataset_id"] != expected.dataset_id
            or head["revision_id"] != expected.revision_id):
        raise ManagedLocalWriteConflict("replace expected head is stale")
    if head["name"] != intent.destination.name:
        raise ManagedLocalWriteConflict("replace destination identity changed")
    return head


def catalog_managed_local_write_head(logical_uri: str) -> dict | None:
    """Return the exact current managed-local head used to construct a replace intent."""
    normalized = str(logical_uri).rstrip("/")
    if not normalized:
        raise ValueError("managed local write requires a logical destination URI")
    with session() as s:
        return _managed_local_write_head_in_session(s, normalized)


def catalog_admit_managed_local_write(value: object) -> dict | None:
    """Validate a frozen intent before artifact allocation, returning a prior durable receipt."""
    intent, _payload, canonical = _canonical_managed_local_write_intent(value)
    if intent.mode not in ("create", "replace"):
        raise ValueError("managed local-file writes support create/replace only")
    if intent.partitions:
        raise ValueError("managed local-file create/replace does not support partitions")
    with session() as s:
        prior = _managed_local_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        if prior is not None:
            return prior
        prior = _managed_local_lance_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        if prior is not None:
            return prior
        _validate_managed_local_write_precondition(s, intent, lock=False)
        return None


def catalog_managed_local_write_receipt(value: object) -> dict | None:
    """Recover an exact typed write receipt by idempotency key after response loss."""
    intent, _payload, canonical = _canonical_managed_local_write_intent(value)
    with session() as s:
        prior = _managed_local_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        return prior if prior is not None else _managed_local_lance_write_receipt_in_session(
            s, intent.idempotency_key, canonical)


def catalog_admit_managed_local_lance_write(value: object) -> dict | None:
    """Validate one registered local Lance append before any provider-side allocation."""
    intent, _payload, canonical = _canonical_managed_local_write_intent(value)
    with session() as s:
        prior = _managed_local_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        if prior is not None:
            return prior
        prior = _managed_local_lance_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        if prior is not None:
            return prior
        _validate_managed_local_lance_destination_in_session(s, intent, lock=False)
        return None


def catalog_managed_local_lance_write_receipt(value: object) -> dict | None:
    """Recover one exact native Lance receipt by its frozen idempotent intent."""
    intent, _payload, canonical = _canonical_managed_local_write_intent(value)
    with session() as s:
        prior = _managed_local_write_receipt_in_session(
            s, intent.idempotency_key, canonical)
        return prior if prior is not None else _managed_local_lance_write_receipt_in_session(
            s, intent.idempotency_key, canonical)


def catalog_publish_managed_local_lance_write(value: object, publish) -> dict:
    """Serialize one admitted Lance append and persist its exact provider receipt.

    ``publish`` runs while the registered destination row is locked. The external provider commit is
    intentionally recoverable rather than transactionally coupled to metadata: its transaction evidence
    lets a retry reconstruct this row if the database response is lost after Lance commits.
    """
    intent, _payload, canonical = _canonical_managed_local_write_intent(value)
    with session() as s:
        _catalog_sqlite_write_fence(s)
        _lock_catalog_namespace_tokens(
            s, [f"managed-local-write:{intent.idempotency_key}"])
        prior = _managed_local_write_receipt_in_session(
            s, intent.idempotency_key, canonical, lock=True)
        if prior is not None:
            return prior
        prior = _managed_local_lance_write_receipt_in_session(
            s, intent.idempotency_key, canonical, lock=True)
        if prior is not None:
            return prior
        _validate_managed_local_lance_destination_in_session(s, intent, lock=True)
        from hub.models import WriteReceipt

        receipt = WriteReceipt.model_validate(publish())
        run_id = intent.provenance.publication.run_id
        execution_manifest_sha256 = (
            _retain_execution_manifest_for_run_in_session(s, run_id)
            if run_id is not None else None
        )
        if receipt.execution_manifest_sha256 not in (None, execution_manifest_sha256):
            raise RuntimeError("managed local Lance publication receipt changed its execution manifest")
        receipt = receipt.model_copy(update={
            "execution_manifest_sha256": execution_manifest_sha256,
        })
        expected = intent.expected_head
        if (expected is None
                or receipt.dataset_id != intent.destination.dataset_id
                or receipt.head.dataset_id != receipt.dataset_id
                or receipt.head.revision_id != receipt.revision_id
                or receipt.parent_head != expected
                or receipt.publication.provider != "managed-local-lance"
                or receipt.publication.logical_uri != intent.destination.logical_uri
                or receipt.publication.artifact_uri != intent.destination.logical_uri
                or receipt.publication.publish_sequence != int(receipt.revision_id)
                or receipt.publication.idempotency_key != intent.idempotency_key
                or receipt.provenance != intent.provenance):
            raise RuntimeError("managed local Lance publication receipt changed after admission")
        committed_at = receipt.head.committed_at or _now()
        if committed_at.tzinfo is None or committed_at.utcoffset() is None:
            committed_at = committed_at.replace(tzinfo=datetime.timezone.utc)
        s.add(ManagedLocalLanceWriteReceipt(
            idempotency_key=intent.idempotency_key,
            dataset_id=receipt.dataset_id,
            logical_uri=intent.destination.logical_uri,
            revision_id=receipt.revision_id,
            write_intent_doc=canonical,
            write_receipt_doc=json.dumps(
                receipt.model_dump(by_alias=True, mode="json"),
                sort_keys=True, separators=(",", ":")),
            run_id=run_id,
            execution_manifest_sha256=execution_manifest_sha256,
            committed_at=committed_at,
        ))
        return receipt.model_dump(by_alias=True, mode="json")


def catalog_publish_managed_local_file(
        logical_uri: str, artifact_uri: str, name: str, doc: dict, *,
        parents: list[str] | None = None, lineage: dict | None = None,
        write_intent: object | None = None, total_bytes: int | None = None) -> dict:
    """Atomically publish one already-validated local artifact as a new immutable revision.

    The catalog retains only the current physical projection; the revision ledger owns every physical
    artifact so old exact reads remain valid until an explicit retention policy exists.
    """
    from hub.models import CatalogTable

    logical_uri = str(logical_uri).rstrip("/")
    artifact_uri = _local_result_candidate(artifact_uri) or ""
    if not logical_uri or not artifact_uri:
        raise ValueError("managed local publication requires logical and exact artifact URIs")
    payload = CatalogTable.model_validate(doc).model_dump(by_alias=True)
    if payload.get("uri") != artifact_uri or payload.get("name") != str(name):
        raise ValueError("managed local publication table does not match its artifact")
    parent_tokens = catalog_lineage_parent_tokens(parents)
    canonical_lineage = _catalog_lineage_canonical(lineage, len(parent_tokens))
    typed_intent = intent_payload = intent_doc = None
    if write_intent is not None:
        typed_intent, intent_payload, intent_doc = _canonical_managed_local_write_intent(write_intent)
        if typed_intent.mode not in ("create", "replace"):
            raise ValueError("managed local-file writes support create/replace only")
        if (typed_intent.destination.logical_uri != logical_uri
                or typed_intent.destination.name != str(name)):
            raise ValueError("managed local write intent changed its destination")
        if typed_intent.partitions:
            raise ValueError("managed local-file create/replace does not support partitions")
        expected_schema = [
            (column.name, column.type) for column in typed_intent.expected_schema
        ]
        actual_schema = [
            (str(column.get("name") or ""), str(column.get("type") or ""))
            for column in payload.get("columns") or []
        ]
        if expected_schema != actual_schema:
            raise ValueError("managed local write output schema does not match its intent")
        if (canonical_lineage is None
                or canonical_lineage["idempotency_key"] != typed_intent.idempotency_key
                or parent_tokens != typed_intent.provenance.parents):
            raise ValueError("managed local write provenance changed after admission")
        comparable_lineage = {
            key: value for key, value in canonical_lineage.items()
            if key not in ("field_mappings_json", "schema_version")
        }
        if comparable_lineage != typed_intent.provenance.publication.model_dump():
            raise ValueError("managed local write provenance changed after admission")
        if (isinstance(total_bytes, bool) or not isinstance(total_bytes, int)
                or total_bytes < 0):
            raise ValueError("managed local write requires bounded non-negative bytes")
    execution_manifest_candidates: set[str | None] = set()
    with session() as s:
        # Take the lineage reservation before the managed output projection. This matches the
        # composite-publication ordering used by the other catalog writers and keeps both effects
        # in this transaction: a lineage failure must roll back the new head and revision receipt.
        _catalog_sqlite_write_fence(s)
        lineage_publication_key = None
        lineage_applied = False
        parent_snapshots: list[_CatalogLineageParentSnapshot] = []
        destination_version = _catalog_lineage_version(
            payload.get("version"), field="destination version")
        receipt = _managed_local_file_publication_receipt_in_session(
            s, logical_uri, artifact_uri, str(name))
        if receipt is not None:
            if canonical_lineage is not None:
                _catalog_require_lineage_publication(
                    s,
                    destination_uri=artifact_uri,
                    destination_version=destination_version,
                    parent_tokens=parent_tokens,
                    lineage=canonical_lineage,
                )
            return receipt
        if canonical_lineage is not None:
            lineage_publication_key, lineage_applied = _catalog_reserve_lineage_publication(
                s,
                destination_uri=artifact_uri,
                destination_version=destination_version,
                parent_tokens=parent_tokens,
                lineage=canonical_lineage,
            )
            parent_snapshots = [
                _catalog_lineage_parent_snapshot(s, token)
                for token in parent_tokens
            ]
        logical_id, catalog_key = _catalog_managed_namespace_identity(logical_uri, f"tbl_{name}")
        namespace_tokens = [logical_uri, logical_id, catalog_key]
        if typed_intent is not None:
            namespace_tokens.append(
                f"managed-local-write:{typed_intent.idempotency_key}")
        _lock_catalog_namespace_tokens(s, namespace_tokens)
        if typed_intent is not None:
            prior_write = _managed_local_write_receipt_in_session(
                s, typed_intent.idempotency_key, intent_doc, lock=True)
            if prior_write is not None:
                _catalog_require_lineage_publication(
                    s,
                    destination_uri=prior_write["publication"]["artifactUri"],
                    destination_version=prior_write["publication"].get("catalogVersion"),
                    parent_tokens=parent_tokens,
                    lineage=canonical_lineage,
                )
                return prior_write
            prior_lance_write = _managed_local_lance_write_receipt_in_session(
                s, typed_intent.idempotency_key, intent_doc, lock=True)
            if prior_lance_write is not None:  # pragma: no cover - mode validation forbids this replay
                return prior_lance_write
        prior_head = (_validate_managed_local_write_precondition(
            s, typed_intent, lock=True) if typed_intent is not None else None)
        receipt = _managed_local_file_publication_receipt_in_session(
            s, logical_uri, artifact_uri, str(name))
        if receipt is not None:
            if canonical_lineage is not None:
                _catalog_require_lineage_publication(
                    s,
                    destination_uri=artifact_uri,
                    destination_version=destination_version,
                    parent_tokens=parent_tokens,
                    lineage=canonical_lineage,
                )
                if lineage_applied:
                    raise RuntimeError(
                        "managed local publication receipt belongs to another lineage request")
            return receipt
        if canonical_lineage is not None and not lineage_applied:
            raise RuntimeError("managed local publication lineage reservation has no receipt")
        _assert_managed_catalog_namespace_available(
            s, logical_uri=logical_uri, logical_id=logical_id, catalog_key=catalog_key)
        reserved_id, _epoch, publish_seq = _reserve_catalog_publication(
            s, logical_uri, f"tbl_{name}")
        logical = s.get(CatalogLogicalDataset, reserved_id, with_for_update=True)
        if logical is None:  # pragma: no cover - reservation is checked above
            raise RuntimeError("managed local publication identity is missing")
        old_uri = logical.current_uri
        old = s.get(CatalogEntry, old_uri, with_for_update=True) if old_uri else None
        current = s.get(CatalogEntry, artifact_uri, with_for_update=True)
        if current is not None:
            raise RuntimeError("managed local artifact is already cataloged")
        if old is not None:
            # Keep the browse ID and curation stable while replacing only the physical head.
            payload["id"] = old.tbl_id or payload["id"]
            for field in ("folder", "owner", "description"):
                if not payload.get(field) and field in json.loads(old.doc):
                    payload[field] = json.loads(old.doc).get(field)
            if not payload.get("tags"):
                payload["tags"] = [tag.tag for tag in s.scalars(select(CatalogTag).where(
                    CatalogTag.uri == old.uri).order_by(CatalogTag.tag))]
            execution_manifest_candidates.update(
                _delete_catalog_children(s, [old.uri]))
            s.flush()
            _delete_unreferenced_execution_manifests(
                s, execution_manifest_candidates)
            s.delete(old)
            # ``logical_id`` is unique on the current catalog projection. Flush the retired head before
            # adding its replacement so SQLite and PostgreSQL observe the same one-head invariant.
            s.flush()
        tbl_id, folder, owner, description, rows, tags, cols = _doc_org(payload)
        entry = CatalogEntry(
            uri=artifact_uri, name=str(name), doc=json.dumps(payload, default=str),
            tbl_id=tbl_id, folder=folder, owner=owner, description=description,
            row_count=rows, logical_id=logical.logical_id, usage=logical.usage,
        )
        s.add(entry)
        _sync_children(s, artifact_uri, tags, cols)
        revision_id = uuid.uuid4().hex
        committed_at = _db_now(s)
        revision = ManagedLocalFileRevision(
            revision_id=revision_id, logical_id=logical.logical_id,
            artifact_uri=artifact_uri, publish_seq=publish_seq,
            table_doc=json.dumps(payload, default=str),
            committed_at=committed_at,
        )
        if typed_intent is not None:
            from hub.models import (
                CatalogTable, DatasetRevision, ExactDatasetRef, WritePublicationIdentity, WriteReceipt,
            )

            parent_head = (ExactDatasetRef(
                kind="exact",
                dataset_id=prior_head["dataset_id"],
                revision_id=prior_head["revision_id"],
            ) if prior_head is not None else None)
            run_id = typed_intent.provenance.publication.run_id
            execution_manifest_sha256 = (
                _retain_execution_manifest_for_run_in_session(s, run_id)
                if run_id is not None else None
            )
            receipt = WriteReceipt(
                dataset_id=logical.logical_id,
                revision_id=revision_id,
                parent_head=parent_head,
                head=DatasetRevision(
                    dataset_id=logical.logical_id,
                    revision_id=revision_id,
                    committed_at=committed_at,
                    retention_owner="core",
                ),
                rows=int(payload.get("rowCount") or 0),
                bytes=total_bytes,
                schema=CatalogTable.model_validate(payload).columns,
                partitions=typed_intent.partitions,
                publication=WritePublicationIdentity(
                    logical_uri=logical_uri,
                    artifact_uri=artifact_uri,
                    publish_sequence=publish_seq,
                    idempotency_key=typed_intent.idempotency_key,
                    catalog_version=payload.get("version"),
                ),
                provenance=typed_intent.provenance,
                execution_manifest_sha256=execution_manifest_sha256,
            )
            revision.write_idempotency_key = typed_intent.idempotency_key
            revision.write_intent_doc = intent_doc
            revision.write_receipt_doc = json.dumps(
                receipt.model_dump(by_alias=True, mode="json"),
                sort_keys=True, separators=(",", ":"))
            revision.run_id = run_id
            revision.execution_manifest_sha256 = execution_manifest_sha256
        s.add(revision)
        logical.current_uri = artifact_uri
        logical.current_publish_seq = publish_seq
        logical.state = "active"
        logical.governance_doc = json.dumps(_catalog_governance(payload), default=str, sort_keys=True)
        _materialize_folder(s, folder)
        entry = s.get(CatalogEntry, artifact_uri)
        if entry is not None:
            _workspace_sync_dataset_folder_in_session(
                s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")
        # The revision ledger, rather than the replaceable head projection, retains every artifact.
        sync_local_result_owner(s, "managed_file_revision", revision_id, artifact_uri)
        if canonical_lineage is not None:
            destination_snapshot = _catalog_lineage_parent_snapshot(s, artifact_uri)
            target_snapshots = [destination_snapshot, *parent_snapshots]
            targets = _lock_catalog_mutation_targets(
                s, [artifact_uri, *parent_tokens],
                exact_current_attempts={artifact_uri})
            destination = targets[0]
            if (target_snapshots[0].logical_id is None
                    and target_snapshots[0].entry_uri is None):
                raise RuntimeError("lineage destination is not the exact current catalog output")
            if (not destination["known"]
                    or destination["current_uri"] != artifact_uri):
                raise RuntimeError("lineage destination is not the exact current catalog output")
            observed_version = _catalog_entry_version(destination["entry"])
            if observed_version != destination_version:
                raise RuntimeError("lineage destination version is not exact")
            locked_logicals = {
                target["logical"].logical_id: target["logical"]
                for target in targets if target.get("logical") is not None
            }
            locked_entries = {
                target["entry"].uri: target["entry"]
                for target in targets if target.get("entry") is not None
            }
            _catalog_validate_lineage_parent_logicals(target_snapshots, locked_logicals)
            _catalog_validate_lineage_parent_entries(target_snapshots, locked_entries)
            _catalog_apply_lineage_in_session(
                s, destination["catalog_key"], artifact_uri, observed_version,
                target_snapshots[1:], locked_logicals, locked_entries,
                canonical_lineage, lineage_publication_key)
        if typed_intent is not None:
            result = receipt.model_dump(by_alias=True, mode="json")
        else:
            result = {
                "dataset_id": logical.logical_id,
                "revision_id": revision_id,
                "committed_at": committed_at,
                "table": payload,
            }
    return result


def _managed_local_file_publication_receipt_in_session(
        s, logical_uri: str, artifact_uri: str, name: str) -> dict | None:
    revision = s.scalars(select(ManagedLocalFileRevision).where(
        ManagedLocalFileRevision.artifact_uri == artifact_uri).limit(1)).first()
    if revision is None:
        return None
    logical = s.get(CatalogLogicalDataset, revision.logical_id)
    ref = s.get(LocalResultReference, {
        "uri": artifact_uri,
        "owner_kind": "managed_file_revision",
        "owner_key": revision.revision_id,
    })
    try:
        from hub.models import CatalogTable
        table = CatalogTable.model_validate(json.loads(revision.table_doc))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("managed local publication receipt is invalid") from exc
    if (logical is None or logical.logical_uri != logical_uri or ref is None
            or table.uri != artifact_uri or table.name != name):
        raise RuntimeError("managed local publication receipt does not match its request")
    return {
        "dataset_id": logical.logical_id,
        "revision_id": revision.revision_id,
        "committed_at": revision.committed_at,
        "table": table.model_dump(by_alias=True),
    }


def catalog_managed_local_file_publication_receipt(
        logical_uri: str, artifact_uri: str, name: str, *,
        parents: list[str] | None = None, lineage: dict | None = None) -> dict | None:
    """Recover one exact committed local revision after an unknown transaction response."""
    logical_uri = str(logical_uri).rstrip("/")
    artifact_uri = _local_result_candidate(artifact_uri) or ""
    if not logical_uri or not artifact_uri:
        raise ValueError("managed local publication requires logical and exact artifact URIs")
    with session() as s:
        receipt = _managed_local_file_publication_receipt_in_session(
            s, logical_uri, artifact_uri, str(name))
        parent_tokens = catalog_lineage_parent_tokens(parents)
        canonical_lineage = _catalog_lineage_canonical(lineage, len(parent_tokens))
        if receipt is not None and canonical_lineage is not None:
            _catalog_require_lineage_publication(
                s,
                destination_uri=artifact_uri,
                destination_version=_catalog_lineage_version(
                    receipt["table"].get("version"), field="destination version"),
                parent_tokens=parent_tokens,
                lineage=canonical_lineage,
            )
        return receipt


def catalog_managed_publication_receipt(uri: str) -> dict | None:
    """Core-only durable receipt; execution backends never inspect lifecycle tables themselves."""
    with session() as s:
        attempt = s.get(ObjectAttempt, str(uri).rstrip("/"))
        if attempt is None or attempt.kind != "sink" or attempt.state != "published" \
                or not attempt.logical_id:
            return None
        logical = s.get(CatalogLogicalDataset, attempt.logical_id)
        ref = s.get(ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": attempt.logical_id, "ref_slot": "",
        })
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
                         tags: list[str], name: str | None = None) -> None:
    """Update ONLY the organization fields of an entry (folder/owner/description/tags, and an optional
    friendly `name` rename) — both the indexed columns AND the mirrored fields inside the stored doc,
    so a re-read is consistent without re-probing the dataset. Unknown or inactive managed targets
    fail closed."""
    folder = catalog_folder_normalize(folder)
    with session() as s:
        target = _lock_catalog_mutation_targets(s, [uri])[0]
        if not target["known"]:
            raise RuntimeError("catalog governance target is not registered")
        logical, r = target["logical"], target["entry"]
        previous_name = r.name
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            doc = {}
        doc["folder"], doc["owner"], doc["description"], doc["tags"] = folder, owner, description, list(tags)
        if name:
            doc["name"] = name
        r.folder, r.owner, r.description, r.doc = folder, owner, description, json.dumps(doc, default=str)
        if name:
            r.name = name
            _workspace_follow_target_name_in_session(
                s, target_kind="dataset", target_id=r.registration_id,
                previous_name=previous_name, name=r.name)
        _materialize_folder(s, folder)  # curating into a folder makes it a first-class entity (#155)
        _workspace_sync_dataset_folder_in_session(
            s, dataset_id=r.registration_id, name=r.name, folder=r.folder or "")
        if logical is not None:
            logical.governance_doc = json.dumps(
                _catalog_governance(doc), default=str, sort_keys=True)
            logical.metadata_version += 1
        cols = [c.get("name") for c in doc.get("columns", []) if isinstance(c, dict) and c.get("name")]
        _sync_children(s, r.uri, tags, cols)


class CatalogMetadataConflict(RuntimeError):
    """A staged catalog edit was based on metadata that is no longer current."""


def catalog_metadata_revision(doc: dict, declared_key: list[str] | None) -> str:
    """Return a short, opaque CAS token for the editable catalog surface.

    This is intentionally derived from the small owner-editable projection rather than a database
    timestamp: it works for both ordinary and managed catalog entries without adding persistence
    schema, and folder rewrites/legacy key calls naturally invalidate an already-open drawer.
    """
    payload = {
        "name": doc.get("name") or "",
        "folder": doc.get("folder") or "",
        "tags": sorted(str(tag) for tag in (doc.get("tags") or [])),
        "owner": doc.get("owner"),
        "description": doc.get("description"),
        "declaredKey": list(declared_key or []),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "m1_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def catalog_save_metadata_edit(
        uri: str, *, expected_revision: str, folder: str, owner: str | None,
        description: str | None, tags: list[str], name: str | None,
        declared_key: list[str],
) -> None:
    """CAS-save catalog organization metadata and its declared key as one transaction.

    Both the entry projection/doc and the independent declared-key row are locked before the
    revision comparison. Any validation error or injected persistence failure rolls the complete
    transaction back, so a caller can never observe just one half of the staged edit.
    """
    folder = catalog_folder_normalize(folder)
    cleaned_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    cleaned_key = list(declared_key)
    with session() as s:
        target = _lock_catalog_mutation_targets(s, [uri])[0]
        if not target["known"]:
            raise RuntimeError("catalog governance target is not registered")
        logical, entry = target["logical"], target["entry"]
        previous_name = entry.name
        try:
            doc = json.loads(entry.doc)
        except (ValueError, TypeError):
            doc = {}
        catalog_key = target["catalog_key"]
        declared = s.get(CatalogDeclaredKey, catalog_key, with_for_update=True)
        current_key = json.loads(declared.columns) if declared is not None else []
        if catalog_metadata_revision(doc, current_key) != expected_revision:
            raise CatalogMetadataConflict("catalog metadata changed; reload or reapply your edits")
        columns = {c.get("name") for c in doc.get("columns", [])
                   if isinstance(c, dict) and c.get("name")}
        missing = [column for column in cleaned_key if column not in columns]
        if missing:
            raise ValueError(f"columns not in '{doc.get('name') or entry.name}': {', '.join(missing)}")
        own = owner.strip() if owner else None
        desc = description.strip() if description else None
        next_name = name.strip() if name and name.strip() else None
        doc.update(folder=folder, owner=own, description=desc, tags=cleaned_tags)
        if next_name:
            doc["name"] = next_name
        entry.folder, entry.owner, entry.description = folder, own, desc
        entry.doc = json.dumps(doc, default=str)
        if next_name:
            entry.name = next_name
            _workspace_follow_target_name_in_session(
                s, target_kind="dataset", target_id=entry.registration_id,
                previous_name=previous_name, name=entry.name)
        _materialize_folder(s, folder)
        _workspace_sync_dataset_folder_in_session(
            s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")
        if logical is not None:
            logical.governance_doc = json.dumps(
                _catalog_governance(doc), default=str, sort_keys=True)
            logical.metadata_version += 1
        if cleaned_key:
            payload = json.dumps(cleaned_key)
            if declared is None:
                s.add(CatalogDeclaredKey(catalog_key=catalog_key, columns=payload))
            else:
                declared.columns = payload
        elif declared is not None:
            s.delete(declared)
        cols = [c.get("name") for c in doc.get("columns", [])
                if isinstance(c, dict) and c.get("name")]
        _sync_children(s, entry.uri, cleaned_tags, cols)


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


def _catalog_publication_event_once(
        s, event_key: str, effect_type: str, uri: str | None,
        version: str | None, fingerprint: str | None = None) -> bool:
    """Insert one exact catalog effect identity in the caller's transaction.

    The savepoint contains only the unique event insert, so a concurrent winner can be validated without
    rolling back the caller's transaction or accidentally converting a later catalog mutation error into
    an idempotent replay. Callers reserve before mutating catalog projections.
    """
    fingerprint = fingerprint or (
        "catalog-effect:v1:sha256:" + hashlib.sha256(json.dumps(
            [effect_type, uri, version], ensure_ascii=False,
            separators=(",", ":"),
        ).encode()).hexdigest()
    )

    def validate(event: CatalogPublicationEvent | None) -> None:
        if (event is None or event.effect_type != effect_type
                or event.uri != uri or event.version != version
                or event.fingerprint != fingerprint):
            raise RuntimeError(f"catalog publication key collision: {event_key}")

    event = s.get(CatalogPublicationEvent, event_key, with_for_update=True)
    if event is not None:
        validate(event)
        return False
    try:
        with s.begin_nested():
            s.add(CatalogPublicationEvent(
                event_key=event_key, effect_type=effect_type,
                uri=uri, version=version, fingerprint=fingerprint,
            ))
            s.flush()
    except IntegrityError:
        # The failed INSERT was isolated to the savepoint. Under READ COMMITTED the winner is visible
        # after its unique-key wait completes; validate its full effect identity before accepting it.
        s.expire_all()
        validate(s.get(CatalogPublicationEvent, event_key, populate_existing=True))
        return False
    return True


def _catalog_unmanaged_output_event_once(
        s, event_key: str, uri: str, version: str | None, fingerprint: str) -> bool:
    """Reserve a first unmanaged apply; replay matching uses caller semantics, not a fresh probe.

    ``version`` is the exact value discovered by the winning first probe and returned in its receipt.
    A concurrent exact caller may observe different bytes at the mutable URI, but its request
    fingerprint is still identical and must attest the winner without replacing that version.
    """
    version = _catalog_lineage_version(version, field="destination version")

    def validate(event: CatalogPublicationEvent | None) -> None:
        if (event is None or event.effect_type != "output" or event.uri != uri
                or event.fingerprint != fingerprint):
            raise RuntimeError(f"catalog publication key collision: {event_key}")
        _catalog_lineage_version(
            event.version, field="persisted destination version")

    event = s.get(CatalogPublicationEvent, event_key, with_for_update=True)
    if event is not None:
        validate(event)
        return False
    try:
        with s.begin_nested():
            s.add(CatalogPublicationEvent(
                event_key=event_key, effect_type="output", uri=uri,
                version=version, fingerprint=fingerprint,
            ))
            s.flush()
    except IntegrityError:
        s.expire_all()
        validate(s.get(CatalogPublicationEvent, event_key, populate_existing=True))
        return False
    return True


def _canonical_unmanaged_output_request(
        event_key: str, uri: str, name: str, requested_version: str | None, *,
        parents: list[str] | None, pipeline: str | None,
        lineage: dict | None) -> tuple[str, str | None, list[str], dict | None, str]:
    """Fingerprint only stable caller semantics, never the mutable artifact at ``uri``."""
    if not event_key:
        raise ValueError("catalog publication event_key is required")
    normalized = _catalog_lineage_uri(uri, field="destination URI")
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ValueError("catalog publication name must be a non-empty string of at most 512 characters")
    requested_version = _catalog_lineage_version(
        requested_version, field="requested destination version")
    if pipeline is not None and not isinstance(pipeline, str):
        raise ValueError("catalog publication pipeline must be a string")
    parent_tokens = catalog_lineage_parent_tokens(parents)
    canonical_lineage = _catalog_lineage_canonical(lineage, len(parent_tokens))
    if parent_tokens and canonical_lineage is None:
        raise ValueError("catalog publication with sources requires lineage identity")
    semantic = {
        "schema_version": 2,
        "name": name,
        "uri": normalized,
        "requested_version": requested_version,
        "parents": parent_tokens,
        "pipeline": pipeline,
        "lineage": (
            _catalog_lineage_publication_semantic(canonical_lineage)
            if canonical_lineage is not None else None),
    }
    fingerprint = "unmanaged-output:v2:sha256:" + hashlib.sha256(json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return normalized, requested_version, parent_tokens, canonical_lineage, fingerprint


def catalog_unmanaged_output_publication_receipt(
        event_key: str, uri: str, name: str, requested_version: str | None, *,
        parents: list[str] | None = None, pipeline: str | None = None,
        lineage: dict | None = None) -> dict | None:
    """Return an exact prior receipt before probing a mutable or already-deleted artifact."""
    normalized, _requested, _parents, _lineage, fingerprint = (
        _canonical_unmanaged_output_request(
            event_key, uri, name, requested_version,
            parents=parents, pipeline=pipeline, lineage=lineage))
    with session() as s:
        event = s.get(CatalogPublicationEvent, event_key, with_for_update=True)
        if event is None:
            return None
        if (event.effect_type != "output" or event.uri != normalized
                or event.fingerprint != fingerprint):
            raise RuntimeError(f"catalog publication key collision: {event_key}")
        version = _catalog_lineage_version(
            event.version, field="persisted destination version")
        return {"uri": event.uri, "version": version, "fingerprint": event.fingerprint}


def _canonical_unmanaged_output_publication(
        event_key: str, uri: str, name: str, doc: dict, *,
        requested_version: str | None, parents: list[str] | None,
        pipeline: str | None, lineage: dict | None,
        ) -> tuple[str, str | None, list[str], dict | None, dict, str]:
    """Validate a first apply against its pre-probe caller request."""
    (normalized, requested_version, parent_tokens,
     canonical_lineage, fingerprint) = _canonical_unmanaged_output_request(
        event_key, uri, name, requested_version,
        parents=parents, pipeline=pipeline, lineage=lineage)

    from hub.models import CatalogTable
    table = CatalogTable.model_validate(doc)
    if table.name != name or table.uri.rstrip("/") != normalized:
        raise ValueError("catalog publication table document does not match its destination")
    canonical_doc = table.model_dump(by_alias=True)
    canonical_doc["uri"] = normalized
    version = _catalog_lineage_version(
        table.version, field="destination version")
    if requested_version is not None and version != requested_version:
        raise ValueError("catalog publication table version does not match its caller request")
    return (
        normalized, version, parent_tokens, canonical_lineage,
        canonical_doc, fingerprint,
    )


def catalog_upsert_output_idempotent(
        event_key: str, uri: str, name: str, doc: dict, *,
        requested_version: str | None,
        parents: list[str] | None = None, pipeline: str | None = None,
        lineage: dict | None = None) -> bool:
    """Atomically reserve and apply one complete unmanaged catalog output publication.

    The output receipt, entry projection, child indexes, local ownership, and optional lineage header /
    facts share one transaction. Exact retries return before consulting or mutating the current catalog
    projection; changed retries collide before they can roll an entry back.
    """
    (normalized, version, parent_tokens, canonical_lineage,
     canonical_doc, fingerprint) = _canonical_unmanaged_output_publication(
        event_key, uri, name, doc, requested_version=requested_version,
        parents=parents, pipeline=pipeline, lineage=lineage)
    lineage_doc = (
        _catalog_lineage_publication_semantic(canonical_lineage)
        if canonical_lineage is not None else None)
    with session() as s:
        applied = _catalog_upsert_in_session(
            s, normalized, str(name), dict(canonical_doc),
            parents=parent_tokens, pipeline=pipeline, lineage=lineage_doc,
            publication_event_key=event_key,
            publication_fingerprint=fingerprint,
            lineage_replay_noop=False,
            require_unmanaged=True,
            requested_destination_version=requested_version)
        if not applied:
            return False
        sync_local_result_owner(
            s, "catalog_entry", normalized, normalized, canonical_doc)
        _materialize_folder(s, canonical_doc.get("folder") or "")
        return True


def _lock_exact_unmanaged_catalog_output(
        s, uri: str, version: str | None) -> CatalogEntry:
    """Lock and attest the exact unmanaged catalog row behind an output receipt."""
    if s.get(ObjectAttempt, uri) is not None or object_attempt_uri_shape(uri):
        raise RuntimeError(
            "managed catalog output requires the core object-lifecycle publication receipt")
    entry = s.get(CatalogEntry, uri, with_for_update=True)
    if entry is None:
        raise RuntimeError(f"catalog output is not durably readable: {uri}")
    if entry.logical_id is not None:
        raise RuntimeError(
            "managed catalog output requires the core object-lifecycle publication receipt")
    try:
        doc = json.loads(entry.doc)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"catalog output has invalid durable metadata: {uri}") from exc
    if not isinstance(doc, dict) or doc.get("version") != version:
        raise RuntimeError(f"catalog output version does not match durable metadata: {uri}")
    return entry


def catalog_record_output_publication(event_key: str, uri: str, version: str | None) -> None:
    """Persist a receipt only after the referenced catalog entry is durably readable.

    This compatibility receipt API does not perform the entry upsert. It reserves the event first in its
    transaction, so an exact replay stays successful after the catalog projection advances or is removed;
    a first apply still has to attest the exact readable unmanaged row before the event can commit.
    """
    if not event_key:
        raise ValueError("catalog publication event_key is required")
    normalized = str(uri).rstrip("/") if uri else ""
    if not normalized:
        raise ValueError("catalog publication uri is required")
    with session() as s:
        _lock_catalog_namespace_tokens(s, [normalized])
        if not _catalog_publication_event_once(
                s, event_key, "output", normalized, version):
            return
        _assert_unmanaged_catalog_namespace_available(s, normalized)
        _lock_exact_unmanaged_catalog_output(s, normalized, version)


def _catalog_usage_effect(
        s, uris: list[str]) -> tuple[str, list[dict]]:
    """Resolve aliases to stable identities and return one canonical effect plus unique live targets."""
    normalized = catalog_lineage_parent_tokens(uris)
    targets = _lock_catalog_mutation_targets(
        s, normalized, allow_inactive=True)
    identities: set[str] = set()
    unique_targets: dict[str, dict] = {}
    for target in targets:
        logical = target["logical"]
        identity = (
            f"logical:{logical.logical_id}" if logical is not None
            else f"uri:{target['current_uri'] or target['token']}"
        )
        identities.add(identity)
        if target["known"]:
            unique_targets.setdefault(identity, target)
    canonical = json.dumps(
        sorted(identities), ensure_ascii=False, separators=(",", ":"))
    fingerprint = "usage:v1:sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return fingerprint, [unique_targets[key] for key in sorted(unique_targets)]


def catalog_bump_usage_once(event_key: str, uris: list[str]) -> bool:
    """Apply one canonical run-level popularity effect to each stable parent identity exactly once."""
    if not event_key:
        raise ValueError("catalog publication event_key is required")
    with session() as s:
        fingerprint, targets = _catalog_usage_effect(s, uris)
        if not _catalog_publication_event_once(
                s, event_key, "usage", fingerprint, None):
            return False
        for target in targets:
            updated = s.execute(
                update(CatalogEntry).where(
                    CatalogEntry.uri == target["current_uri"])
                .values(
                    usage=CatalogEntry.usage + 1,
                    updated_at=CatalogEntry.updated_at,
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("catalog usage target changed concurrently")
            logical = target["logical"]
            if logical is not None:
                logical.usage += 1
        return True


_CATALOG_USAGE_PLAN_FIELDS = {
    "contract_version", "run_id", "event_key", "identities", "fingerprint",
}
_CATALOG_USAGE_IDENTITY_FIELDS = {"kind", "key"}


def validate_catalog_usage_publication_plan(plan: dict) -> dict:
    """Validate one frozen stable-identity usage effect without resolving mutable aliases."""
    if not isinstance(plan, dict) or set(plan) != _CATALOG_USAGE_PLAN_FIELDS:
        raise ValueError("catalog usage publication plan has an invalid field set")
    if plan.get("contract_version") != 1:
        raise ValueError("catalog usage publication plan has an unsupported version")
    for key in ("run_id", "event_key", "fingerprint"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise ValueError(f"catalog usage publication plan has an invalid {key}")
    identities = plan.get("identities")
    if not isinstance(identities, list):
        raise ValueError("catalog usage publication plan identities must be a list")
    canonical: list[dict[str, str]] = []
    for identity in identities:
        if (not isinstance(identity, dict)
                or set(identity) != _CATALOG_USAGE_IDENTITY_FIELDS
                or identity.get("kind") not in ("logical", "uri")
                or not isinstance(identity.get("key"), str)
                or not identity["key"]):
            raise ValueError("catalog usage publication plan has an invalid identity")
        canonical.append({"kind": identity["kind"], "key": identity["key"]})
    canonical = sorted(canonical, key=lambda item: (item["kind"], item["key"]))
    if identities != canonical or len({(item["kind"], item["key"])
                                       for item in canonical}) != len(canonical):
        raise ValueError("catalog usage publication identities must be canonical and unique")
    unsigned = {key: value for key, value in plan.items() if key != "fingerprint"}
    expected = "catalog-usage:v1:sha256:" + hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    if plan["fingerprint"] != expected:
        raise ValueError("catalog usage publication fingerprint does not match its plan")
    return dict(plan)


def catalog_prepare_usage_publication(
        run_id: str, event_key: str, parents: list[str]) -> dict:
    """Resolve parent aliases once, before effects, into stable logical or exact URI identities."""
    run_id, event_key = str(run_id), str(event_key)
    if not run_id or not event_key or not isinstance(parents, list):
        raise ValueError("catalog usage publication requires run, event key, and parent list")
    parent_tokens = catalog_lineage_parent_tokens(parents)
    with session() as s:
        targets = _lock_catalog_mutation_targets(
            s, parent_tokens, allow_inactive=False)
        identities = []
        for target in targets:
            logical = target["logical"]
            if logical is not None:
                identities.append({"kind": "logical", "key": logical.logical_id})
            else:
                identities.append({
                    "kind": "uri", "key": target["current_uri"] or target["token"],
                })
    identities = sorted(
        {(item["kind"], item["key"]) for item in identities}
    )
    plan = {
        "contract_version": 1,
        "run_id": run_id,
        "event_key": event_key,
        "identities": [{"kind": kind, "key": key} for kind, key in identities],
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    plan["fingerprint"] = "catalog-usage:v1:sha256:" + hashlib.sha256(
        canonical.encode()
    ).hexdigest()
    return validate_catalog_usage_publication_plan(plan)


def catalog_apply_usage_publication(plan: dict) -> bool:
    """Apply a staged usage effect once against current generations of frozen identities.

    A logical identity follows generation replacement. An identity unregistered before first apply is
    deliberately a no-op, while the event is still recorded so terminal publication cannot be blocked
    or later resurrect the dataset.
    """
    validated = validate_catalog_usage_publication_plan(plan)
    with session() as s:
        logical_ids = [item["key"] for item in validated["identities"]
                       if item["kind"] == "logical"]
        uri_ids = [item["key"] for item in validated["identities"]
                   if item["kind"] == "uri"]
        logicals = {row.logical_id: row for row in s.scalars(
            select(CatalogLogicalDataset).where(
                CatalogLogicalDataset.logical_id.in_(logical_ids))
            .order_by(CatalogLogicalDataset.logical_id).with_for_update()
        )} if logical_ids else {}
        current_uris = sorted({
            logical.current_uri for logical in logicals.values()
            if logical.state == "active" and logical.current_uri
        })
        entry_uris = sorted(set(current_uris) | set(uri_ids))
        entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(entry_uris)
        ).order_by(CatalogEntry.uri).with_for_update())} if entry_uris else {}
        if not _catalog_publication_event_once(
                s, validated["event_key"], "usage", None, None,
                validated["fingerprint"]):
            return False
        for logical_id in logical_ids:
            logical = logicals.get(logical_id)
            if logical is None or logical.state != "active" or not logical.current_uri:
                continue
            entry = entries.get(logical.current_uri)
            if entry is None or entry.logical_id != logical_id:
                # Concurrent unregister/replacement takes the logical lock in the same order, so a
                # missing exact current entry is corruption, not a benign liveness transition.
                raise RuntimeError("catalog usage logical identity has no exact current entry")
            # A read-popularity bump must not advance updated_at (the 'recently updated' sort key).
            s.execute(update(CatalogEntry).where(CatalogEntry.uri == entry.uri)
                      .values(usage=CatalogEntry.usage + 1, updated_at=CatalogEntry.updated_at))
            logical.usage += 1
        for uri in uri_ids:
            entry = entries.get(uri)
            if entry is not None and entry.logical_id is None:
                s.execute(update(CatalogEntry).where(CatalogEntry.uri == entry.uri)
                          .values(usage=CatalogEntry.usage + 1, updated_at=CatalogEntry.updated_at))
        return True


def catalog_record_lineage(
        destination_uri: str, destination_version: str | None,
        parents: list[str], lineage: dict) -> int:
    """Record facts for an exact current destination without rewriting its catalog entry."""
    destination_uri = _catalog_lineage_uri(
        destination_uri, field="destination URI")
    destination_version = _catalog_lineage_version(
        destination_version, field="destination version")
    parent_tokens = catalog_lineage_parent_tokens(parents)
    canonical_lineage = _catalog_lineage_canonical(lineage, len(parent_tokens))
    if canonical_lineage is None:  # pragma: no cover - public contract is intentionally non-null
        raise ValueError("catalog lineage identity is required")
    with session() as s:
        _catalog_sqlite_write_fence(s)
        publication_key, applied = _catalog_reserve_lineage_publication(
            s,
            destination_uri=destination_uri,
            destination_version=destination_version,
            parent_tokens=parent_tokens,
            lineage=canonical_lineage,
        )
        if not applied:
            return 0
        target_snapshots = [
            _catalog_lineage_parent_snapshot(s, token)
            for token in [destination_uri, *parent_tokens]
        ]
        if (target_snapshots[0].logical_id is None
                and target_snapshots[0].entry_uri is None):
            raise RuntimeError("lineage destination is not the exact current catalog output")
        targets = _lock_catalog_mutation_targets(
            s, [destination_uri, *parent_tokens],
            exact_current_attempts={destination_uri})
        destination = targets[0]
        if (not destination["known"]
                or destination["current_uri"] != destination_uri):
            raise RuntimeError("lineage destination is not the exact current catalog output")
        observed_version = _catalog_entry_version(destination["entry"])
        expected_version = destination_version
        if observed_version != expected_version:
            raise RuntimeError("lineage destination version is not exact")
        parent_snapshots = target_snapshots[1:]
        locked_logicals = {
            target["logical"].logical_id: target["logical"]
            for target in targets if target.get("logical") is not None
        }
        locked_entries = {
            target["entry"].uri: target["entry"]
            for target in targets if target.get("entry") is not None
        }
        _catalog_validate_lineage_parent_logicals(target_snapshots, locked_logicals)
        _catalog_validate_lineage_parent_entries(target_snapshots, locked_entries)
        return _catalog_apply_lineage_in_session(
            s, destination["catalog_key"], destination_uri, observed_version,
            parent_snapshots, locked_logicals, locked_entries, canonical_lineage,
            publication_key)


def _row_to_doc(r: "CatalogEntry", tags: list[str]) -> dict:
    """Materialize a CatalogTable-shaped dict from a row, overlaying the authoritative indexed
    columns (id/folder/owner/description/usage) + the tag rows onto the stored doc."""
    try:
        d = json.loads(r.doc)
    except (ValueError, TypeError):
        d = {"id": r.tbl_id or f"tbl_{r.name}", "name": r.name, "uri": r.uri}
    d["id"] = r.tbl_id or d.get("id") or f"tbl_{r.name}"
    d["registrationId"] = r.registration_id
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


def catalog_folder_normalize(path: str) -> str:
    """A folder path with surrounding slashes stripped and each segment validated. Rejects empty
    segments and `.`/`..` so a path can't escape its namespace. '' means the tree root."""
    p = (path or "").strip().strip("/")
    if not p:
        return ""
    segs = [seg.strip() for seg in p.split("/")]
    if any(not seg or seg in (".", "..") for seg in segs):
        raise ValueError(f"invalid folder path: '{path}'")
    return "/".join(segs)


def _folder_ancestors(path: str) -> list[str]:
    """Every prefix of `path` including itself, e.g. 'x/y/z' -> ['x', 'x/y', 'x/y/z']."""
    segs = path.split("/")
    return ["/".join(segs[: i + 1]) for i in range(len(segs))]


def _folder_exists(s, path: str) -> bool:
    """True when `path` is a known folder — a folder entity at/under it OR any entry filed at/under it."""
    like = _like_escape(path) + "/%"
    if s.get(CatalogFolder, path) is not None:
        return True
    if s.scalar(select(CatalogFolder.path).where(
            CatalogFolder.path.like(like, escape="\\")).limit(1)) is not None:
        return True
    return s.scalar(select(CatalogEntry.uri).where(or_(
        CatalogEntry.folder == path,
        CatalogEntry.folder.like(like, escape="\\"))).limit(1)) is not None


def _ensure_folder_rows(s, paths) -> None:
    """Insert a CatalogFolder row for every path not already present. Conflict-safe: a concurrent
    creator that wins the check-then-insert race just leaves the row present (no duplicate-PK 500)."""
    from sqlalchemy.exc import IntegrityError
    want = list(dict.fromkeys(p for p in paths if p))
    if not want:
        return
    have = {p for (p,) in s.execute(select(CatalogFolder.path).where(CatalogFolder.path.in_(want)))}
    for p in want:
        if p in have:
            continue
        sp = s.begin_nested()
        try:
            s.add(CatalogFolder(path=p))
            s.flush()
        except IntegrityError:
            sp.rollback()  # another writer inserted this path between the check and our insert
    _workspace_sync_catalog_folder_projections_in_session(s, want)


def _materialize_folder(s, raw: str) -> None:
    """Back a dataset's assigned folder STRING with first-class folder entities (its full ancestry), so
    registering/curating into a path makes it a real folder — not a merely-implicit derived namespace
    (#155). A malformed legacy string is left as a derived-only folder rather than failing the write."""
    if not raw:
        return
    try:
        _ensure_folder_rows(s, _folder_ancestors(catalog_folder_normalize(raw)))
    except ValueError:
        pass


def catalog_folders_list() -> list[dict]:
    """Every folder entity, as {path, created_at}. Empty folders live only here (entry-derived folders
    are additionally discovered from CatalogEntry.folder), so this powers the folder-name autocomplete."""
    with session() as s:
        return [{"path": r.path, "created_at": r.created_at}
                for r in s.scalars(select(CatalogFolder).order_by(CatalogFolder.path))]


class FolderExistsError(ValueError):
    """The folder already exists, or a concurrent create won the race — the route maps this to 409."""


def catalog_folder_create(path: str) -> str:
    """Create an empty folder (+ its ancestor entity rows). Raises FolderExistsError if the folder
    already exists (as an entity or via any entry) OR a concurrent create wins the race, so the loser
    gets a stable conflict rather than a false success. Returns the normalized path."""
    from sqlalchemy.exc import IntegrityError
    path = catalog_folder_normalize(path)
    if not path:
        raise ValueError("folder path cannot be empty")
    with session() as s:
        if _folder_exists(s, path):
            raise FolderExistsError(f"folder '{path}' already exists")
        _ensure_folder_rows(s, _folder_ancestors(path)[:-1])  # parents tolerantly
        sp = s.begin_nested()
        try:
            s.add(CatalogFolder(path=path))  # the target itself: a race here is a real conflict
            s.flush()
        except IntegrityError:
            sp.rollback()
            raise FolderExistsError(f"folder '{path}' already exists")
        _workspace_sync_catalog_folder_projections_in_session(s, [path])
    return path


def _lock_folder_entries(s, path: str) -> list[tuple[CatalogEntry, CatalogLogicalDataset | None]]:
    """Lock every entry in a folder subtree using the catalog governance lock order.

    The initial snapshot discovers managed logical IDs without taking entry locks. The shared mutation
    helper then locks logical datasets before their current entries, matching metadata writes and
    managed publication. A changed snapshot fails closed instead of moving a newly published generation
    according to stale folder state.
    """
    like = _like_escape(path) + "/%"
    snapshots = list(s.execute(select(CatalogEntry.uri, CatalogEntry.folder).where(or_(
        CatalogEntry.folder == path, CatalogEntry.folder.like(like, escape="\\"),
    )).order_by(CatalogEntry.uri)))
    if not snapshots:
        return []
    targets = _lock_catalog_mutation_targets(s, [uri for uri, _folder in snapshots])
    locked: list[tuple[CatalogEntry, CatalogLogicalDataset | None]] = []
    for (uri, folder), target in zip(snapshots, targets, strict=True):
        entry = target.get("entry")
        logical = target.get("logical")
        if (not target.get("known") or entry is None
                or entry.uri != uri or entry.folder != folder
                or (logical is not None and entry.logical_id != logical.logical_id)):
            raise RuntimeError("catalog folder contents changed concurrently")
        locked.append((entry, logical))
    return locked


def _rewrite_entry_folders(
        rows: list[tuple[CatalogEntry, CatalogLogicalDataset | None]], rewrite) -> list[str]:
    """Rewrite locked entry folders and their durable managed-governance projection together."""
    targets: list[str] = []
    for r, logical in rows:
        target = rewrite(r.folder or "")
        r.folder = target
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            doc = {}
        doc["folder"] = target
        r.doc = json.dumps(doc, default=str)
        if logical is not None:
            try:
                governance = json.loads(logical.governance_doc or "{}")
            except (TypeError, ValueError):
                governance = _catalog_governance(doc)
            if not isinstance(governance, dict):
                governance = _catalog_governance(doc)
            governance["folder"] = target
            logical.governance_doc = json.dumps(governance, default=str, sort_keys=True)
            logical.metadata_version += 1
        targets.append(target)
    return targets


def _materialize_folder_paths(s, paths) -> None:
    for path in dict.fromkeys(paths):
        _materialize_folder(s, path)


def catalog_folder_rename(old: str, new: str) -> None:
    """Rename a folder, cascading to every dataset and subfolder under it. Operates over the UNION of
    folder entities and entry `folder` strings, so a folder that exists only because a dataset was
    registered into it is renameable too. Raises ValueError if `old` is unknown or `new` already exists."""
    old = catalog_folder_normalize(old)
    new = catalog_folder_normalize(new)
    if not old or not new:
        raise ValueError("folder path cannot be empty")
    with session() as s:
        if not _folder_exists(s, old):
            raise ValueError(f"folder '{old}' not found")
        if new == old:
            return
        if new.startswith(old + "/"):  # a folder can't become its own descendant (self-nesting)
            raise ValueError("cannot move a folder into itself")
        if _folder_exists(s, new):
            raise ValueError(f"folder '{new}' already exists")
        entries = _lock_folder_entries(s, old)
        like = _like_escape(old) + "/%"
        moved_entities = list(s.scalars(select(CatalogFolder).where(or_(
            CatalogFolder.path == old, CatalogFolder.path.like(like, escape="\\")))
            .order_by(CatalogFolder.path).with_for_update()))
        targets = [new + row.path[len(old):] for row in moved_entities]
        for row, target in zip(moved_entities, targets, strict=True):
            row.path = target
        s.flush()
        targets.extend(_rewrite_entry_folders(
            entries, lambda folder: new + folder[len(old):]))
        _materialize_folder_paths(s, targets + [new])
        _workspace_sync_catalog_folder_projections_in_session(s, targets)
        for entry, _logical in entries:
            _workspace_sync_dataset_folder_in_session(
                s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")


def catalog_folder_delete(path: str) -> None:
    """Delete a folder, moving everything under it UP one level to the folder's parent while PRESERVING
    descendant structure (deleting 'research' turns 'research/vision/raw' into 'vision/raw', not a flat
    dump), then removing the deleted node. Nothing is lost — datasets and subfolders are re-homed, not
    deleted. Operates over the UNION of folder entities and entry `folder` strings; raises ValueError
    if the folder is unknown."""
    path = catalog_folder_normalize(path)
    if not path:
        raise ValueError("folder path cannot be empty")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""

    def _reparent(folder: str) -> str:
        if folder == path:
            return parent
        rel = folder[len(path) + 1:]  # strip the deleted "path/" prefix, keep the rest
        return f"{parent}/{rel}" if parent else rel

    with session() as s:
        if not _folder_exists(s, path):
            raise ValueError(f"folder '{path}' not found")
        entries = _lock_folder_entries(s, path)
        like = _like_escape(path) + "/%"
        entry_targets = _rewrite_entry_folders(entries, _reparent)
        # descendant folder entities move up one level; only the deleted node itself is removed
        moved = list(s.scalars(select(CatalogFolder).where(
            CatalogFolder.path.like(like, escape="\\"))
            .order_by(CatalogFolder.path).with_for_update()))
        targets = [_reparent(row.path) for row in moved]
        deleted = list(s.scalars(select(CatalogFolder).where(or_(
            CatalogFolder.path == path, CatalogFolder.path.like(like, escape="\\")))
            .order_by(CatalogFolder.path).with_for_update()))
        deleted_root = next((row for row in deleted if row.path == path), None)
        for row, target in zip(moved, targets, strict=True):
            row.path = target
        if deleted_root is not None:
            s.delete(deleted_root)
        s.flush()
        _materialize_folder_paths(s, targets + entry_targets)
        _workspace_sync_catalog_folder_projections_in_session(s, targets)
        if deleted_root is not None:
            _workspace_tombstone_catalog_folder_projection_in_session(s, deleted_root.id)
        for entry, _logical in entries:
            _workspace_sync_dataset_folder_in_session(
                s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")


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
        # merge in folder ENTITY paths (count 0) so an empty folder — created directly or emptied by a
        # move/delete — still appears in the tree alongside the entry-derived folders
        entity_paths = [(pth, 0) for (pth,) in s.execute(select(CatalogFolder.path)).all()]
        children: dict[str, int] = {}
        for folder, cnt in list(folder_counts) + entity_paths:
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


def catalog_revision_binding(dataset_id: str) -> dict | None:
    """Resolve one persisted opaque registration identity to its current URI.

    A re-registration gets a new ``registration_id``. Unregister deletes the old row, so a stale
    exact-revision reference cannot be rebound merely because a path or name was reused.
    """
    with session() as s:
        managed = s.get(CatalogLogicalDataset, str(dataset_id))
        if managed is not None and managed.current_uri and s.scalar(select(
                ManagedLocalFileRevision.revision_id).where(
                    ManagedLocalFileRevision.logical_id == managed.logical_id,
                    ManagedLocalFileRevision.artifact_uri == managed.current_uri,
                ).limit(1)) is not None:
            return {"dataset_id": managed.logical_id, "uri": managed.current_uri}
        entry = s.scalars(select(CatalogEntry).where(
            CatalogEntry.registration_id == str(dataset_id)).limit(1)).first()
        if entry is None:
            return None
        return {"dataset_id": entry.registration_id, "uri": entry.uri}


def catalog_revision_binding_for_uri(uri: str) -> dict | None:
    """Return the persisted opaque identity for one currently registered exact URI."""
    with session() as s:
        entry = s.get(CatalogEntry, str(uri).rstrip("/"))
        if entry is None:
            return None
        if entry.logical_id and s.scalar(select(ManagedLocalFileRevision.revision_id).where(
                ManagedLocalFileRevision.logical_id == entry.logical_id,
                ManagedLocalFileRevision.artifact_uri == entry.uri,
        ).limit(1)) is not None:
            return {"dataset_id": entry.logical_id, "uri": entry.uri}
        return {"dataset_id": entry.registration_id, "uri": entry.uri}


def managed_local_file_revision_history(
        uri: str, *, limit: int, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Return a bounded newest-first page for the managed local dataset owning ``uri``."""
    bounded = max(1, min(int(limit), 100))
    with session() as s:
        entry = s.get(CatalogEntry, str(uri).rstrip("/"))
        if entry is None or not entry.logical_id:
            raise KeyError(uri)
        logical_id = entry.logical_id
        if cursor is not None:
            cursor_row = s.get(ManagedLocalFileRevision, str(cursor))
            if cursor_row is None or cursor_row.logical_id != logical_id:
                raise KeyError(cursor)
            rows = list(s.scalars(select(ManagedLocalFileRevision).where(
                ManagedLocalFileRevision.logical_id == logical_id,
                ManagedLocalFileRevision.publish_seq < cursor_row.publish_seq,
            ).order_by(ManagedLocalFileRevision.publish_seq.desc()).limit(bounded + 1)))
        else:
            rows = list(s.scalars(select(ManagedLocalFileRevision).where(
                ManagedLocalFileRevision.logical_id == logical_id,
            ).order_by(ManagedLocalFileRevision.publish_seq.desc()).limit(bounded + 1)))
        items = rows[:bounded]
        return ([{"revision_id": row.revision_id, "committed_at": row.committed_at}
                 for row in items], items[-1].revision_id if len(rows) > bounded else None)


def managed_local_file_revision_resolve(
        uri: str, *, as_of: datetime.datetime | None = None) -> dict:
    """Resolve the local head or latest retained revision at ``as_of`` without a path fallback."""
    with session() as s:
        entry = s.get(CatalogEntry, str(uri).rstrip("/"))
        if entry is None or not entry.logical_id:
            raise KeyError(uri)
        query = select(ManagedLocalFileRevision).where(
            ManagedLocalFileRevision.logical_id == entry.logical_id)
        if as_of is not None:
            query = query.where(ManagedLocalFileRevision.committed_at <= as_of)
        row = s.scalars(query.order_by(ManagedLocalFileRevision.publish_seq.desc()).limit(1)).first()
        if row is None:
            raise KeyError(uri)
        return {"revision_id": row.revision_id, "committed_at": row.committed_at,
                "artifact_uri": row.artifact_uri}


def managed_local_file_revision_open(uri: str, revision_id: str) -> str:
    """Return only the exact retained physical artifact for an opaque local revision ID."""
    with session() as s:
        entry = s.get(CatalogEntry, str(uri).rstrip("/"))
        if entry is None or not entry.logical_id:
            raise KeyError(uri)
        row = s.get(ManagedLocalFileRevision, str(revision_id))
        if row is None or row.logical_id != entry.logical_id:
            raise KeyError(revision_id)
        artifact = s.get(LocalResultArtifact, row.artifact_uri)
        if artifact is None or artifact.state != "ready":
            raise KeyError(revision_id)
        return row.artifact_uri


def managed_local_file_revision_artifact(dataset_id: str, revision_id: str) -> str | None:
    """Resolve one exact core-owned revision to its managed artifact for read fencing."""
    with session() as s:
        row = s.get(ManagedLocalFileRevision, str(revision_id))
        if row is None or row.logical_id != str(dataset_id):
            return None
        artifact = s.get(LocalResultArtifact, row.artifact_uri)
        if artifact is None or artifact.state != "ready":
            return None
        return row.artifact_uri


def managed_local_file_revision_gc_batch(
        retention_seconds: float, *, limit: int = 50) -> dict[str, int | bool]:
    """Retire one DB-clock-bounded batch of expired, unreferenced core revisions.

    The revision's existing ``managed_file_revision`` reference is its retention hold. Every real
    owner (canvas, admission, durable profile, cache/history, or live reader) uses the same artifact
    reference table and therefore excludes the revision here. File deletion remains the established
    local-result GC's job, including its exact lock/token fencing and retryable ``deleting`` state.
    """
    retention = _gc_seconds(retention_seconds, "managed revision retention_seconds")
    bounded = max(0, min(int(limit), 500))
    if bounded == 0:
        return {"retired": 0, "has_more": False}
    with session() as s:
        _lock_local_result_registry(s)
        cutoff = _db_now(s) - datetime.timedelta(seconds=retention)
        external_reference = select(LocalResultReference.uri).where(
            LocalResultReference.uri == ManagedLocalFileRevision.artifact_uri,
            or_(
                LocalResultReference.owner_kind != "managed_file_revision",
                LocalResultReference.owner_key != ManagedLocalFileRevision.revision_id,
            ),
        ).exists()
        statement = (select(ManagedLocalFileRevision)
            .join(CatalogLogicalDataset,
                  CatalogLogicalDataset.logical_id == ManagedLocalFileRevision.logical_id)
            .where(
                ManagedLocalFileRevision.committed_at <= cutoff,
                or_(
                    CatalogLogicalDataset.current_uri.is_(None),
                    CatalogLogicalDataset.current_uri != ManagedLocalFileRevision.artifact_uri,
                ),
                ~external_reference,
            )
            .order_by(
                ManagedLocalFileRevision.committed_at,
                ManagedLocalFileRevision.revision_id,
            ).limit(bounded + 1))
        if s.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(of=ManagedLocalFileRevision)
        rows = list(s.scalars(statement))
        retired = 0
        for revision in rows[:bounded]:
            logical = s.get(CatalogLogicalDataset, revision.logical_id)
            if logical is not None and logical.current_uri == revision.artifact_uri:
                continue
            artifact = s.get(LocalResultArtifact, revision.artifact_uri, with_for_update=True)
            retention_ref = s.get(LocalResultReference, {
                "uri": revision.artifact_uri,
                "owner_kind": "managed_file_revision",
                "owner_key": revision.revision_id,
            }, with_for_update=True)
            other_ref = s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == revision.artifact_uri,
                or_(
                    LocalResultReference.owner_kind != "managed_file_revision",
                    LocalResultReference.owner_key != revision.revision_id,
                ),
            ).limit(1))
            if other_ref is not None:
                continue
            if artifact is None or retention_ref is None or artifact.state != "ready":
                raise RuntimeError("managed local revision retention ownership is inconsistent")
            s.delete(retention_ref)
            s.delete(revision)
            retired += 1
        return {"retired": retired, "has_more": len(rows) > bounded}


def managed_local_file_revision_detail(uri: str, revision_id: str) -> dict:
    """Return persisted facts for one exact retained local revision and its immediate parent."""
    from hub.models import CatalogTable

    with session() as s:
        entry = s.get(CatalogEntry, str(uri).rstrip("/"))
        if entry is None or not entry.logical_id:
            raise KeyError(uri)
        row = s.get(ManagedLocalFileRevision, str(revision_id))
        if row is None or row.logical_id != entry.logical_id:
            raise KeyError(revision_id)
        artifact = s.get(LocalResultArtifact, row.artifact_uri)
        if artifact is None or artifact.state != "ready":
            raise KeyError(revision_id)
        parent = s.scalars(select(ManagedLocalFileRevision).where(
            ManagedLocalFileRevision.logical_id == row.logical_id,
            ManagedLocalFileRevision.publish_seq < row.publish_seq,
        ).order_by(ManagedLocalFileRevision.publish_seq.desc()).limit(1)).first()
        table = CatalogTable.model_validate(json.loads(row.table_doc))
        return {
            "revision_id": row.revision_id,
            "committed_at": row.committed_at,
            "parent_revision_id": parent.revision_id if parent is not None else None,
            "artifact_uri": row.artifact_uri,
            "table": table,
        }


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


def _delete_catalog_children(s, uris: list[str]) -> set[str | None]:
    """Remove EVERY row keyed to `uris` alongside the entries themselves — tags/columns/embeddings,
    lineage facts (either endpoint), declared keys, and relationships. Otherwise a deleted table
    haunts lineage/ER as a ghost node, and a NEW dataset re-registered at the same uri silently
    inherits the old declared key + parents."""
    # The lineage predicate repeats the URI and digest lists across four endpoint shapes. Keep each
    # statement under SQLite's conservative bind-variable ceiling while retaining the caller's one
    # transaction and deterministic URI order.
    execution_manifest_candidates: set[str | None] = set()
    for offset in range(0, len(uris), 100):
        batch = uris[offset:offset + 100]
        for model in (CatalogTag, CatalogColumn):
            for row in s.scalars(select(model).where(model.uri.in_(batch))):
                s.delete(row)
        identity_hashes = [_catalog_lineage_identity_hash(uri) for uri in batch]
        lineage_predicate = or_(
            and_(
                CatalogLineageFact.source_uri_hash.in_(identity_hashes),
                CatalogLineageFact.source_uri.in_(batch)),
            and_(
                CatalogLineageFact.destination_uri_hash.in_(identity_hashes),
                CatalogLineageFact.destination_uri.in_(batch)),
            and_(
                CatalogLineageFact.source_key_hash.in_(identity_hashes),
                CatalogLineageFact.source_key.in_(batch)),
            and_(
                CatalogLineageFact.destination_key_hash.in_(identity_hashes),
                CatalogLineageFact.destination_key.in_(batch)),
        )
        execution_manifest_candidates.update(s.scalars(select(
            CatalogLineageFact.execution_manifest_sha256,
        ).where(lineage_predicate)))
        s.execute(delete(CatalogLineageFact).where(lineage_predicate))
    gone = set(uris)
    for r in s.scalars(select(CatalogRelationship)):
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            continue
        if doc.get("leftUri") in gone or doc.get("rightUri") in gone \
                or doc.get("left_uri") in gone or doc.get("right_uri") in gone:
            s.delete(r)
    return execution_manifest_candidates


def _delete_catalog_governance(s, catalog_key: str) -> set[str | None]:
    for model in (CatalogEmbedding, CatalogDeclaredKey):
        row = s.get(model, catalog_key)
        if row is not None:
            s.delete(row)
    identity_hash = _catalog_lineage_identity_hash(catalog_key)
    lineage_predicate = or_(
        and_(CatalogLineageFact.source_key_hash == identity_hash,
             CatalogLineageFact.source_key == catalog_key),
        and_(CatalogLineageFact.destination_key_hash == identity_hash,
             CatalogLineageFact.destination_key == catalog_key),
        and_(CatalogLineageFact.source_uri_hash == identity_hash,
             CatalogLineageFact.source_uri == catalog_key),
        and_(CatalogLineageFact.destination_uri_hash == identity_hash,
             CatalogLineageFact.destination_uri == catalog_key),
    )
    execution_manifest_candidates = set(s.scalars(select(
        CatalogLineageFact.execution_manifest_sha256,
    ).where(lineage_predicate)))
    s.execute(delete(CatalogLineageFact).where(lineage_predicate))
    for relationship in s.scalars(select(CatalogRelationship)):
        try:
            doc = json.loads(relationship.doc)
        except (TypeError, ValueError):
            continue
        if catalog_key in (
                doc.get("leftUri"), doc.get("left_uri"),
                doc.get("rightUri"), doc.get("right_uri")):
            s.delete(relationship)
    return execution_manifest_candidates


def _catalog_logicals_have_backend_publication_refs(s, logical_ids: list[str]) -> bool:
    if not logical_ids:
        return False
    return bool(s.scalar(select(exists().where(
        ObjectAttemptRef.ref_type == "backend_publication",
        ObjectAttemptRef.attempt_uri == ObjectAttempt.uri,
        ObjectAttempt.logical_id.in_(logical_ids),
    ))))


def _catalog_current_logical_ownership(
        s, logical: CatalogLogicalDataset, *, validate_managed_local: bool = True) -> str:
    """Classify one exact head; optionally defer its final local-registry validation."""
    current_uri = logical.current_uri
    if not current_uri:
        raise RuntimeError("catalog unregister ownership changed concurrently")
    attempt = s.get(ObjectAttempt, current_uri, with_for_update=True)
    revision = s.scalars(select(ManagedLocalFileRevision).where(
        ManagedLocalFileRevision.logical_id == logical.logical_id,
        ManagedLocalFileRevision.artifact_uri == current_uri,
    ).with_for_update()).first()
    if attempt is not None and revision is not None:
        raise RuntimeError("catalog unregister ownership changed concurrently")
    if attempt is not None:
        ref = s.get(ObjectAttemptRef, {
            "ref_type": "catalog", "ref_key": logical.logical_id, "ref_slot": "",
        }, with_for_update=True)
        if (attempt.logical_id != logical.logical_id or ref is None
                or ref.attempt_uri != current_uri):
            raise RuntimeError("catalog unregister ownership changed concurrently")
        return "object"
    if revision is not None:
        if not validate_managed_local:
            return "managed_local"
        # Local lifecycle rows are always locked behind the registry. Object attempts (including all
        # prefix members) must already be locked before entering this branch to preserve the global
        # object -> local-registry -> local-artifact order.
        _lock_local_result_registry(s)
        artifact = s.get(LocalResultArtifact, current_uri, with_for_update=True)
        ref = s.get(LocalResultReference, {
            "uri": current_uri,
            "owner_kind": "managed_file_revision",
            "owner_key": revision.revision_id,
        }, with_for_update=True)
        if artifact is None or artifact.state != "ready" or ref is None:
            raise RuntimeError("catalog unregister ownership changed concurrently")
        return "managed_local"
    raise RuntimeError("catalog unregister ownership changed concurrently")


def catalog_delete_entry(uri: str, *, expected_registration_id: str | None = None,
                         expected_metadata_revision: str | None = None,
                         report_result: bool = False) -> bool | None:
    """Remove a catalog entry (unregister) + everything keyed to it (tags/columns/embedding/facts/
    declared key/relationships). ``report_result`` lets the versioned HTTP mutation distinguish a
    committed removal from a concurrent miss without changing the legacy fire-and-forget contract."""
    execution_manifest_candidates: set[str | None] = set()
    with session() as s:
        token = str(uri).rstrip("/")
        attempt_identity = s.get(ObjectAttempt, token)
        logical_id = attempt_identity.logical_id if attempt_identity is not None else None
        logical_snapshot = None
        entry_snapshot = None
        if logical_id is None:
            logical_snapshot = s.get(CatalogLogicalDataset, token)
            if logical_snapshot is None:
                logical_snapshot = s.scalars(select(CatalogLogicalDataset).where(or_(
                    CatalogLogicalDataset.catalog_key == token,
                    CatalogLogicalDataset.logical_uri == token,
                    CatalogLogicalDataset.current_uri == token,
                )).limit(1)).first()
            logical_id = logical_snapshot.logical_id if logical_snapshot is not None else None
        if logical_id is None:
            # Resolve the public table id/name alias before choosing the managed or unmanaged branch.
            # Keep this read in the same transaction as the logical/entry locks and CAS below: a
            # separate catalog GET would let unregister/re-register rebind the alias between calls.
            entry_snapshot = s.get(CatalogEntry, token)
            if entry_snapshot is None:
                entry_snapshot = s.scalars(select(CatalogEntry).where(or_(
                    CatalogEntry.tbl_id == token, CatalogEntry.name == token,
                )).order_by(CatalogEntry.uri).limit(1)).first()
            logical_id = entry_snapshot.logical_id if entry_snapshot is not None else None
        logical = s.get(CatalogLogicalDataset, logical_id, with_for_update=True) \
            if logical_id else None
        if logical is not None:
            if logical.state != "active" or not logical.current_uri:
                if report_result and logical.state == "unregistered" and not logical.current_uri:
                    return False
                raise RuntimeError("catalog governance target is inactive")
            if (attempt_identity is not None
                    and attempt_identity.catalog_epoch != logical.catalog_epoch):
                raise RuntimeError("catalog governance request was fenced by unregister")
            current_uri = logical.current_uri
            if _catalog_logicals_have_backend_publication_refs(s, [logical.logical_id]):
                raise RuntimeError(
                    "catalog unregister is blocked by an active backend publication")
            entry = s.get(CatalogEntry, current_uri, with_for_update=True)
            if entry is None or entry.logical_id != logical.logical_id:
                raise RuntimeError("catalog unregister entry changed concurrently")
            catalog_key = logical.catalog_key
            # Lock object/revision ownership before manifest GC, but delay the managed-local registry
            # lock until afterwards: local lifecycle locks are deliberately last in the global order.
            ownership = _catalog_current_logical_ownership(
                s, logical, validate_managed_local=False)
        else:
            if entry_snapshot is None:
                return False if report_result else None
            entry = s.get(CatalogEntry, entry_snapshot.uri, with_for_update=True)
            if entry is None or entry.logical_id:
                raise RuntimeError("catalog unregister entry changed concurrently")
            current_uri, catalog_key = entry.uri, entry.uri
            ownership = None
        if (expected_registration_id is not None
                and entry.registration_id != expected_registration_id):
            raise CatalogMetadataConflict(
                "catalog registration changed; reload before removing this dataset")
        if expected_metadata_revision is not None:
            declared = s.get(CatalogDeclaredKey, catalog_key, with_for_update=True)
            declared_key = json.loads(declared.columns) if declared is not None else []
            try:
                current_doc = json.loads(entry.doc)
            except (TypeError, ValueError):
                current_doc = {}
            if catalog_metadata_revision(current_doc, declared_key) != expected_metadata_revision:
                raise CatalogMetadataConflict(
                    "catalog metadata changed; reload before removing this dataset")
        execution_manifest_candidates.update(
            _delete_catalog_governance(s, catalog_key))
        if current_uri:
            execution_manifest_candidates.update(
                _delete_catalog_children(s, [current_uri]))
        s.flush()
        _delete_unreferenced_execution_manifests(
            s, execution_manifest_candidates)
        if logical is not None:
            if ownership == "managed_local":
                _catalog_current_logical_ownership(s, logical)
            if ownership == "object":
                _replace_attempt_ref(s, "catalog", logical.logical_id, None)
            logical.current_uri = None
            logical.catalog_epoch += 1
            logical.state = "unregistered"
            logical.metadata_version += 1
            logical.governance_doc = "{}"
        if entry is not None:
            s.delete(entry)
        # Object governance/ref mutations above always precede the local registry lock.
        _drop_local_result_owner(s, "catalog_entry", current_uri)
        return True if report_result else None


def catalog_delete_prefix(uri_prefix: str) -> int:
    """Delete every entry (+ everything keyed to it) whose uri starts with `uri_prefix`. Returns the
    count removed. For bulk teardown of demo/scale entries; a no-op for a prefix that matches none."""
    like = _like_escape(uri_prefix) + "%"
    execution_manifest_candidates: set[str | None] = set()
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
        object_uris = {row.uri for row in s.scalars(select(ObjectAttempt).where(
            ObjectAttempt.uri.in_(uris)).order_by(ObjectAttempt.uri).with_for_update())} \
            if uris else set()
        if _catalog_logicals_have_backend_publication_refs(s, logical_ids):
            raise RuntimeError(
                "catalog unregister is blocked by an active backend publication")
        entries = {row.uri: row for row in s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(uris)).order_by(CatalogEntry.uri).with_for_update())}
        if len(entries) != len(snapshot):
            raise RuntimeError("catalog prefix changed concurrently")
        ownerships: dict[str, str] = {}
        # Lock every object/revision head before manifest GC. Managed-local artifact validation is
        # deliberately delayed until afterwards so the local registry remains the final lifecycle lock.
        ordered_snapshot = sorted(snapshot, key=lambda item: (item[0] not in object_uris, item[0]))
        for uri, logical_id in ordered_snapshot:
            if logical_id:
                logical = logical_rows.get(logical_id)
                if (logical is None or logical.state != "active"
                        or logical.current_uri != uri):
                    raise RuntimeError("catalog prefix changed concurrently")
                ownerships[logical_id] = _catalog_current_logical_ownership(
                    s, logical, validate_managed_local=False)
            elif entries[uri].logical_id:
                raise RuntimeError("catalog prefix changed concurrently")
        current_uris = list(uris)
        execution_manifest_candidates.update(
            _delete_catalog_children(s, current_uris))
        for uri, logical_id in snapshot:
            logical = logical_rows.get(logical_id) if logical_id else None
            if logical is not None:
                execution_manifest_candidates.update(
                    _delete_catalog_governance(s, logical.catalog_key))
        s.flush()
        _delete_unreferenced_execution_manifests(
            s, execution_manifest_candidates)
        for logical_id, ownership in ownerships.items():
            if ownership == "managed_local":
                _catalog_current_logical_ownership(s, logical_rows[logical_id])
        for uri, logical_id in snapshot:
            logical = logical_rows.get(logical_id) if logical_id else None
            if logical is not None:
                if ownerships[logical.logical_id] == "object":
                    _replace_attempt_ref(s, "catalog", logical.logical_id, None)
                logical.current_uri = None
                logical.catalog_epoch += 1
                logical.state = "unregistered"
                logical.metadata_version += 1
                logical.governance_doc = "{}"
            s.delete(entries[uri])
        _lock_local_result_registry(s)
        for uri in current_uris:
            _drop_local_result_owner_locked(s, "catalog_entry", uri)
    return len(current_uris)


def _catalog_lineage_pair_dicts(s, rows) -> list[dict]:
    materialized = list(rows)
    keys = sorted({key for source, destination, _count in materialized
                   for key in (source, destination)})
    logicals = {row.catalog_key: row.current_uri for row in s.scalars(select(
        CatalogLogicalDataset,
    ).where(
        CatalogLogicalDataset.catalog_key.in_(keys),
        CatalogLogicalDataset.state == "active",
        CatalogLogicalDataset.current_uri.is_not(None),
    ))} if keys else {}
    return [{
        "parent": logicals.get(source, source),
        "child": logicals.get(destination, destination),
        "fact_count": int(count),
    } for source, destination, count in materialized]


def _catalog_lineage_key_pair_dicts(rows) -> list[dict]:
    return [{
        "parent": source,
        "child": destination,
        "fact_count": int(count),
    } for source, destination, count in rows]


def _catalog_lineage_pair_rows(s, keys: list[str], bounded: int):
    key_hashes = [_catalog_lineage_identity_hash(key) for key in keys]
    return s.execute(select(
        CatalogLineageFact.source_key,
        CatalogLineageFact.destination_key,
        func.count(CatalogLineageFact.id),
    ).where(or_(
        and_(CatalogLineageFact.source_key_hash.in_(key_hashes),
             CatalogLineageFact.source_key.in_(keys)),
        and_(CatalogLineageFact.destination_key_hash.in_(key_hashes),
             CatalogLineageFact.destination_key.in_(keys)),
    )).group_by(
        CatalogLineageFact.source_key,
        CatalogLineageFact.destination_key,
    ).order_by(
        CatalogLineageFact.source_key,
        CatalogLineageFact.destination_key,
    ).limit(bounded + 1)).all()


def catalog_lineage_root_key(token: str) -> str:
    """Resolve a graph root to its immutable catalog key, including friendly names and table IDs."""
    normalized = catalog_lineage_uri(token)
    with session() as s:
        catalog_key = _catalog_token_to_key(s, normalized)
        if catalog_key != normalized:
            return catalog_key
        entry = s.get(CatalogEntry, normalized)
        if entry is None:
            entry = s.scalars(select(CatalogEntry).where(
                CatalogEntry.tbl_id == normalized).limit(1)).first()
        if entry is None:
            entry = s.scalars(select(CatalogEntry).where(
                CatalogEntry.name == normalized).order_by(CatalogEntry.uri).limit(1)).first()
        return _catalog_token_to_key(s, entry.uri) if entry is not None else normalized


def catalog_lineage_key_pairs_touching(
        keys: list[str], limit: int) -> tuple[list[dict], bool]:
    """Return bounded aggregate pairs in immutable catalog-key space for graph traversal."""
    canonical = list(dict.fromkeys(
        _catalog_lineage_uri(key, field="catalog key") for key in keys))
    if not canonical:
        return [], False
    bounded = max(1, int(limit))
    with session() as s:
        rows = _catalog_lineage_pair_rows(s, canonical, bounded)
        truncated = len(rows) > bounded
        return _catalog_lineage_key_pair_dicts(rows[:bounded]), truncated


def catalog_lineage_project_keys(keys: list[str]) -> tuple[dict[str, str], dict[str, dict]]:
    """Project stable graph keys and display rows through one current catalog snapshot.

    Traversal stays in immutable key space. Applying this one projection to the root, every edge,
    and every node prevents a managed overwrite from mixing physical generations in one response.
    """
    canonical = list(dict.fromkeys(
        _catalog_lineage_uri(key, field="catalog key") for key in keys))
    if not canonical:
        return {}, {}
    with session() as s:
        projection = {key: key for key in canonical}
        managed_rows = list(s.execute(select(
            CatalogLogicalDataset.catalog_key,
            CatalogLogicalDataset.current_uri,
            CatalogEntry,
        ).outerjoin(
            CatalogEntry, CatalogEntry.uri == CatalogLogicalDataset.current_uri,
        ).where(
                CatalogLogicalDataset.catalog_key.in_(canonical),
                CatalogLogicalDataset.state == "active",
                CatalogLogicalDataset.current_uri.is_not(None),
        )))
        managed_keys: set[str] = set()
        managed_entries: list[CatalogEntry] = []
        for catalog_key, current_uri, entry in managed_rows:
            projection[catalog_key] = current_uri
            managed_keys.add(catalog_key)
            if entry is not None:
                managed_entries.append(entry)
        unmanaged_uris = [key for key in canonical if key not in managed_keys]
        unmanaged_entries = list(s.scalars(select(CatalogEntry).where(
            CatalogEntry.uri.in_(unmanaged_uris)))) if unmanaged_uris else []
        rows = [*managed_entries, *unmanaged_entries]
        tag_map = _tags_for(s, [row.uri for row in rows])
        docs = {row.uri: _row_to_doc(row, tag_map.get(row.uri, [])) for row in rows}
        return projection, docs


def catalog_lineage_pairs() -> list[dict]:
    """Aggregate the complete fact set for diagnostics and bounded backup fixtures."""
    with session() as s:
        rows = s.execute(select(
            CatalogLineageFact.source_key,
            CatalogLineageFact.destination_key,
            func.count(CatalogLineageFact.id),
        ).group_by(
            CatalogLineageFact.source_key,
            CatalogLineageFact.destination_key,
        ).order_by(
            CatalogLineageFact.source_key,
            CatalogLineageFact.destination_key,
        )).all()
        return _catalog_lineage_pair_dicts(s, rows)


def catalog_lineage_pairs_touching(
        uris: list[str], limit: int) -> tuple[list[dict], bool]:
    """Return a bounded page of aggregate pairs touching one graph frontier."""
    if not uris:
        return [], False
    bounded = max(1, int(limit))
    with session() as s:
        keys = list(dict.fromkeys(_catalog_token_to_key(s, uri) for uri in uris))
        rows = _catalog_lineage_pair_rows(s, keys, bounded)
        truncated = len(rows) > bounded
        return _catalog_lineage_pair_dicts(s, rows[:bounded]), truncated


def _catalog_lineage_fact_dict(row: CatalogLineageFact) -> dict:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    else:
        created_at = created_at.astimezone(datetime.timezone.utc)
    try:
        mappings = json.loads(row.field_mappings_json)
    except (TypeError, ValueError) as e:  # pragma: no cover - persistent corruption guard
        raise RuntimeError("catalog lineage field mappings are corrupt") from e
    return {
        "id": int(row.id),
        "fact_key": row.fact_key,
        "publication_key": row.publication_key,
        "source_key": row.source_key,
        "source_uri": row.source_uri,
        "source_version": row.source_version,
        "destination_key": row.destination_key,
        "destination_uri": row.destination_uri,
        "destination_version": row.destination_version,
        "run_id": row.run_id,
        "execution_manifest_sha256": row.execution_manifest_sha256,
        "attempt_id": row.attempt_id,
        "producer": row.producer,
        "producer_version": row.producer_version,
        "step_id": row.step_id,
        "provenance": row.provenance,
        "field_mappings": mappings,
        "created_at": created_at,
    }


def catalog_lineage_facts_page(
        limit: int = 100, after_id: int = 0,
) -> tuple[list[dict], int | None, bool]:
    """Export immutable facts using a monotonic BIGINT keyset cursor."""
    if (isinstance(after_id, bool) or not isinstance(after_id, int)
            or after_id < 0 or after_id >= 2**63):
        raise ValueError("catalog lineage cursor must be a signed non-negative BIGINT")
    bounded = max(1, min(int(limit), 500))
    with session() as s:
        rows = list(s.scalars(select(CatalogLineageFact).where(
            CatalogLineageFact.id > after_id,
        ).order_by(CatalogLineageFact.id.asc()).limit(bounded + 1)))
        has_more = len(rows) > bounded
        page = rows[:bounded]
        next_after_id = int(page[-1].id) if has_more and page else None
        return [_catalog_lineage_fact_dict(row) for row in page], next_after_id, has_more


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
        inserted_entries: list[CatalogEntry] = []
        for e in entries:
            uri = e["uri"]
            if uri in existing:
                continue
            doc = e.get("doc") or {}
            tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
            entry = CatalogEntry(uri=uri, name=e["name"], doc=json.dumps(doc, default=str), tbl_id=tbl_id,
                                 folder=folder, owner=owner, description=description, row_count=rows)
            s.add(entry)
            inserted_entries.append(entry)
            for t in dict.fromkeys(tags):
                s.add(CatalogTag(uri=uri, tag=t))
            for c in dict.fromkeys(cols):
                s.add(CatalogColumn(uri=uri, column=c))
            local_owners.append((uri, doc))
            n += 1
        s.flush()
        for entry in inserted_entries:
            _materialize_folder(s, entry.folder or "")
            _workspace_sync_dataset_folder_in_session(
                s, dataset_id=entry.registration_id, name=entry.name, folder=entry.folder or "")
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
                "token": r.token, "state": r.state, "stale": _kernel_stale(r),
                "started_at": r.started_at}


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


def latest_profile_jobs(canvas_id: str, limit: int = 100) -> list[dict]:
    """Latest durable profile retry for each ``(node, plan)`` identity on a canvas.

    This reads only the canvas-scoped latest projection. It never reconstructs pointers from globally
    pruned RunState detail, so an older late-finishing retry cannot reappear after the newer row is evicted.
    """
    bounded = min(_PROFILE_LATEST_MAX, max(1, int(limit)))
    with session() as s:
        rows = s.execute(select(ProfileJobLatest.doc).where(
            ProfileJobLatest.canvas_id == str(canvas_id),
        ).order_by(
            ProfileJobLatest.attempt_order.desc(),
        ).limit(bounded)).all()
    out: list[dict] = []
    for (doc,) in rows:
        try:
            parsed = json.loads(doc)
        except Exception:  # noqa: BLE001 - retain a bounded best-effort recovery surface
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
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


def run_kernel_id(run_id: str) -> str | None:
    """Return the durable kernel owner stamp for a run, independent of the live kernel lease."""
    with session() as s:
        return s.scalar(select(RunState.kernel_id).where(RunState.run_id == str(run_id)))


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
    another live one) and must NOT be reaped mid-flight — only its dead-kernel runs are. A leased
    external preallocation is excluded on both paths; once its DB-clock lease expires, either path may
    safely terminalize it because no backend writer was durably admitted."""
    n = 0
    with session() as s:
        live = {k.kernel_id for k in s.scalars(select(Kernel)) if not _kernel_stale(k)}
        reaped_run_ids: list[str] = []
        expired_preallocation = and_(
            RunState.preallocation_token.is_not(None),
            or_(RunState.preallocation_expires_at.is_(None),
                RunState.preallocation_expires_at <= func.now()),
        )
        candidate = select(RunState).where(
            RunState.status.in_(("allocating", "queued", "running")),
            ~exists().where(RunBackendJob.run_id == RunState.run_id),
            or_(RunState.preallocation_token.is_(None), expired_preallocation),
        )
        if only_kernel_runs:
            dead_kernel = RunState.kernel_id.is_not(None)
            if live:
                dead_kernel = and_(dead_kernel, RunState.kernel_id.not_in(live))
            candidate = candidate.where(or_(expired_preallocation, dead_kernel))
        elif live:
            candidate = candidate.where(or_(
                RunState.kernel_id.is_(None), RunState.kernel_id.not_in(live)))
        # Filter before FOR UPDATE: a periodic pass must never block progress writes for a run whose
        # kernel is observably live.  Empty ``live`` intentionally means every kernel-owned row is a
        # candidate (and, on boot, every kernel-less row from the previous hub is too).
        rows = s.scalars(candidate.order_by(
            RunState.run_id).with_for_update()).all()
        for r in rows:
            # Bind follows RunState -> backend. Taking the same order means an absent backend is proof:
            # either binding committed first and this row is skipped, or this terminal fence wins and
            # a delayed binder observes that the preallocation is no longer live.
            if s.get(RunBackendJob, r.run_id, with_for_update=True) is not None:
                continue
            preallocation_expired = r.preallocation_token is not None
            admitted_profile = (
                not preallocation_expired and r.kernel_id is not None
                and r.job_type == "profile" and r.canvas_id is not None
                and r.target_node_id is not None and r.target_port_id is not None
                and r.plan_digest is not None
                and r.profile_attempt_order is not None
            )
            if preallocation_expired:
                _abandon_run_preallocation_attempts(s, r.run_id)
                r.preallocation_token = None
                r.preallocation_expires_at = None
            try:
                d = json.loads(r.doc)
            except Exception:  # noqa: BLE001
                d = {"run_id": r.run_id}
            interrupted_error = (
                "interrupted before durable backend binding"
                if preallocation_expired else
                "interrupted — the run's kernel is gone (hub restarted with no live kernel)"
            )
            if admitted_profile:
                # Reopen reads ProfileJobLatest rather than globally bounded RunState detail. Advance
                # both views in this transaction so a dead kernel cannot leave the durable projection
                # queued forever after its owner row has become terminal.
                d.update({
                    "job_type": "profile",
                    "target_node_id": str(r.target_node_id),
                    "target_port_id": str(r.target_port_id),
                    "plan_digest": str(r.plan_digest),
                    "profile_attempt_order": int(r.profile_attempt_order or 0),
                })
            # A child may have reported a provisional binding before its durable parent reaped and
            # committed it. Interrupted runs settle every expected port without publishing an identity.
            try:
                from hub.models import RunStatus
                from hub.run_outputs import discard_unpublished_outputs
                # Validate the live document before making it terminal: a valid queued/running status is
                # expected to contain pending ports, which terminal model validation correctly rejects.
                interrupted = RunStatus.model_validate(d)
                interrupted.status = "failed"
                interrupted.error = interrupted_error
                interrupted.profile = None
                interrupted.total_rows = None
                if interrupted.job_type == "profile":
                    interrupted.outputs = []
                else:
                    discard_unpublished_outputs(interrupted, "failed", interrupted_error)
                d = RunStatus.model_validate(interrupted.model_dump()).model_dump()
            except (TypeError, ValueError):
                d = RunStatus(
                    run_id=r.run_id,
                    status="failed",
                    job_type="profile" if admitted_profile else "run",
                    target_node_id=str(r.target_node_id) if r.target_node_id is not None else None,
                    target_port_id=str(r.target_port_id) if admitted_profile else None,
                    error=interrupted_error,
                    plan_digest=str(r.plan_digest) if admitted_profile else None,
                    profile_attempt_order=(
                        int(r.profile_attempt_order or 0) if admitted_profile else None),
                    outputs=[],
                ).model_dump()
            r.status = "failed"
            r.doc = json.dumps(d, default=str)
            if admitted_profile:
                _upsert_profile_latest(
                    s, canvas_id=str(r.canvas_id), target_node_id=str(r.target_node_id),
                    target_port_id=str(r.target_port_id),
                    plan_digest=str(r.plan_digest), run_id=r.run_id, payload=r.doc,
                    attempt_order=int(r.profile_attempt_order or 0),
                    submitted_at=r.created_at or _now(),
                )
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
    _replace_promoted_transform_refs(
        s, "canvas_version", snapshot_id, snapshot_doc)
    for old in old_autos:
        _drop_local_result_owner_locked(s, "canvas_version", old.id)
        _drop_promoted_transform_refs(s, "canvas_version", old.id)
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


class SettingsRevisionConflict(RuntimeError):
    """A settings batch was based on an older scope snapshot."""

    def __init__(self, current_revision: dict[str, int]):
        super().__init__("settings revision is stale")
        self.current_revision = dict(current_revision)


class _SettingsRevisionStale(RuntimeError):
    """Internal marker raised inside a transaction so the context manager rolls it back."""


def _setting_scope_identity(scope: str, scope_id: str) -> tuple[str, str]:
    if scope == "global" and scope_id == "":
        return scope, scope_id
    if scope == "user" and scope_id:
        return scope, scope_id
    raise ValueError("setting scope must be global or a non-empty user identity")


def _ensure_setting_revision_in_session(
        s, scope: str, scope_id: str) -> SettingRevision:
    """Return the scope counter, tolerating a first-use race for directly seeded test users."""
    identity = _setting_scope_identity(scope, scope_id)
    row = s.get(SettingRevision, identity)
    if row is not None:
        return row
    try:
        with s.begin_nested():
            s.add(SettingRevision(scope=scope, scope_id=scope_id, revision=0))
            s.flush()
    except IntegrityError:
        # A concurrent first writer created the row. The unique-key wait has completed, so its
        # committed counter is now visible under PostgreSQL READ COMMITTED; SQLite serializes writers.
        s.expire_all()
    row = s.get(SettingRevision, identity, populate_existing=True)
    if row is None:
        raise RuntimeError("setting revision row could not be established")
    return row


def _ensure_setting_revisions_in_session(s, user_id: str) -> None:
    for scope, scope_id in (("global", ""), ("user", user_id)):
        _ensure_setting_revision_in_session(s, scope, scope_id)


def _read_setting_revisions_in_session(s, user_id: str) -> dict[str, int]:
    identities = (("global", ""), ("user", user_id))
    rows = list(s.execute(select(
        SettingRevision.scope, SettingRevision.revision,
    ).where(or_(*(
        and_(SettingRevision.scope == scope, SettingRevision.scope_id == scope_id)
        for scope, scope_id in identities
    )))))
    revisions = {str(scope): int(revision) for scope, revision in rows}
    if set(revisions) != {"global", "user"}:
        raise RuntimeError("setting revision rows are incomplete")
    return revisions


def settings_snapshot(user_id: str) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """Read a value snapshot whose revisions cannot belong to an older or newer commit."""
    for _attempt in range(5):
        with session() as s:
            _ensure_setting_revisions_in_session(s, user_id)
            before = _read_setting_revisions_in_session(s, user_id)
            rows = list(s.execute(select(Setting.scope, Setting.key, Setting.value).where(or_(
                and_(Setting.scope == "global", Setting.scope_id == ""),
                and_(Setting.scope == "user", Setting.scope_id == user_id),
            )).order_by(Setting.scope, Setting.key)))
            after = _read_setting_revisions_in_session(s, user_id)
            if before == after:
                return (
                    [(str(scope), str(key), str(value)) for scope, key, value in rows],
                    after,
                )
    raise RuntimeError("settings changed too frequently to read a consistent snapshot")


def setting_revisions(user_id: str) -> dict[str, int]:
    with session() as s:
        _ensure_setting_revisions_in_session(s, user_id)
        return _read_setting_revisions_in_session(s, user_id)


def _set_setting_in_session(s, key: str, value, *, scope: str, scope_id: str) -> None:
    encoded = json.dumps(value, allow_nan=False)
    row = s.scalar(select(Setting).where(
        Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
    if row:
        row.value = encoded
    else:
        s.add(Setting(scope=scope, scope_id=scope_id, key=key, value=encoded))


def set_settings_batch(
        changes: list[tuple[str, str, object]], expected_revision: dict[str, int],
        user_id: str) -> dict[str, int]:
    """Atomically compare scope revisions, write every change, and advance each touched scope once."""
    if not changes:
        return setting_revisions(user_id)
    identities = [(scope, "" if scope == "global" else user_id) for scope, _key, _value in changes]
    for scope, scope_id in identities:
        _setting_scope_identity(scope, scope_id)
    keys = [(scope, key) for scope, key, _value in changes]
    if len(set(keys)) != len(keys):
        raise ValueError("settings batch contains a duplicate scope and key")
    touched = {scope for scope, _scope_id in identities}
    if set(expected_revision) != {"global", "user"}:
        raise ValueError("settings batch requires global and user revisions")
    result_revision: dict[str, int]
    try:
        with session() as s:
            _ensure_setting_revisions_in_session(s, user_id)
            for scope in ("global", "user"):
                if scope not in touched:
                    continue
                scope_id = "" if scope == "global" else user_id
                advanced = s.execute(update(SettingRevision).where(
                    SettingRevision.scope == scope,
                    SettingRevision.scope_id == scope_id,
                    SettingRevision.revision == expected_revision[scope],
                ).values(revision=SettingRevision.revision + 1))
                if advanced.rowcount != 1:
                    raise _SettingsRevisionStale
            for scope, key, value in changes:
                _set_setting_in_session(
                    s, key, value, scope=scope,
                    scope_id="" if scope == "global" else user_id,
                )
            s.flush()
            # One statement observes the untouched scope alongside our uncommitted touched counters.
            # The token may become stale immediately after this read, as every optimistic token can,
            # but it always described one real database snapshot and adds no post-commit failure point.
            result_revision = _read_setting_revisions_in_session(s, user_id)
    except _SettingsRevisionStale:
        # Read only after session() has rolled back every earlier scope CAS in a mixed batch.
        raise SettingsRevisionConflict(setting_revisions(user_id)) from None
    return result_revision


def set_setting(key: str, value, scope: str = "global", scope_id: str = "") -> None:
    scope, scope_id = _setting_scope_identity(scope, scope_id)
    with session() as s:
        _ensure_setting_revision_in_session(s, scope, scope_id)
        advanced = s.execute(update(SettingRevision).where(
            SettingRevision.scope == scope,
            SettingRevision.scope_id == scope_id,
        ).values(revision=SettingRevision.revision + 1))
        if advanced.rowcount != 1:
            raise RuntimeError("setting revision row disappeared during write")
        _set_setting_in_session(s, key, value, scope=scope, scope_id=scope_id)


CRED_KINDS = ("object_store", "agent")


def _cred_row(c: CredEntity) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind,
            "fields": json.loads(c.fields_json or "{}"),
            "createdAt": c.created_at.isoformat() if c.created_at else None}


def creds_list() -> list[dict]:
    with session() as s:
        return [_cred_row(c) for c in s.scalars(select(CredEntity).order_by(CredEntity.created_at))]


def cred_get(cred_id: str | None) -> dict | None:
    if not cred_id:
        return None
    with session() as s:
        c = s.get(CredEntity, cred_id)
        return _cred_row(c) if c else None


CRED_FIELD_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "object_store": ("accessKeyId", "secretAccessKey", "sessionToken", "region", "endpoint"),
    "agent": ("apiKey",),
}


def _validate_cred_fields(kind: str, fields: dict | None) -> dict:
    """Normalize Cred fields to a per-kind allowlist and validate every secret field as a SecretRef.

    Unknown fields are rejected so a raw secret cannot be smuggled into an unvalidated or unredacted
    field and round-tripped.
    """
    from hub.secrets import OBJECT_STORE_SECRET_SUBKEYS, validate_secret_reference
    if kind not in CRED_KINDS:
        raise ValueError(f"unknown credential kind {kind!r}; must be one of {list(CRED_KINDS)}")
    allowed = CRED_FIELD_ALLOWLIST[kind]
    fields = dict(fields or {})
    extra = [k for k in fields if k not in allowed]
    if extra:  # reject, not silently drop — a stray key is a client bug, and dropping hides it
        raise ValueError(f"unknown credential field(s) {extra}; allowed for {kind}: {list(allowed)}")
    secret_fields = OBJECT_STORE_SECRET_SUBKEYS if kind == "object_store" else ("apiKey",)
    for field in secret_fields:
        if field not in fields:
            continue
        ref = validate_secret_reference(fields[field], field=f"{kind}.{field}")
        if ref:
            fields[field] = ref
        else:
            fields.pop(field)
    return fields


def cred_upsert(cred_id: str | None, name: str, kind: str, fields: dict | None) -> dict:
    """Create or update a credential. Rejects raw secret bytes — fields must be env:/file: references."""
    fields = _validate_cred_fields(kind, fields)
    with session() as s:
        c = s.get(CredEntity, cred_id) if cred_id else None
        if c is None:
            c = CredEntity(id=cred_id or _uid(), name=name, kind=kind, fields_json=json.dumps(fields))
            s.add(c)
        else:
            if c.kind != kind:  # a kind change would silently repoint every binding at a wrong identity
                raise ValueError("cannot change a credential's kind")
            c.name, c.fields_json = name, json.dumps(fields)
        s.flush()
        return _cred_row(c)


def cred_references(cred_id: str) -> list[str]:
    """Human-readable places a cred id is bound (default/agent/destinations), so deletion can refuse to
    strand a live reference (which would otherwise fail open to another identity)."""
    refs: list[str] = []
    if get_setting("defaultObjectStoreCredId", "global") == cred_id:
        refs.append("the default object store")
    if get_setting("agentCredId", "global") == cred_id:
        refs.append("the agent")
    for d in get_setting("destinations", "global", default=[]) or []:
        if isinstance(d, dict) and (d.get("credId") or d.get("cred_id")) == cred_id:
            refs.append(f"destination '{d.get('name') or d.get('id')}'")
    return refs


def cred_delete(cred_id: str) -> None:
    """Delete a credential. Refuses (ValueError) if it is still bound anywhere — detach it first, so a
    dangling explicit reference can't silently resolve to a different account."""
    refs = cred_references(cred_id)
    if refs:
        raise ValueError(f"credential is in use by {', '.join(refs)} — detach it first")
    with session() as s:
        c = s.get(CredEntity, cred_id)
        if c is not None:
            s.delete(c)


class CredResolutionError(RuntimeError):
    """An explicit (or configured-default) credential reference did not resolve to a cred of the right
    kind. Resolution RAISES rather than silently falling back to ambient/legacy — using a different
    identity for a configured reference is worse than a loud failure."""


def cred_object_store_config(cred_id: str | None = None) -> dict:
    """Unresolved object-store fields. An EXPLICIT (non-empty) ``cred_id`` — or a configured
    ``defaultObjectStoreCredId`` — that is missing/wrong-kind RAISES CredResolutionError (never silently
    uses ambient identity). When no default is configured, return an empty config so SDK consumers use
    their deliberate ambient credential chain."""
    if cred_id:
        c = cred_get(cred_id)
        if not c or c.get("kind") != "object_store":
            raise CredResolutionError(f"object-store credential '{cred_id}' not found")
        return dict(c["fields"])
    default_id = get_setting("defaultObjectStoreCredId", "global")
    if default_id:
        c = cred_get(default_id)
        if not c or c.get("kind") != "object_store":
            raise CredResolutionError(f"default object-store credential '{default_id}' is missing or not an object store")
        return dict(c["fields"])
    return {}


def cred_agent_api_key_ref(cred_id: str | None = None) -> str:
    """The agent apiKey reference. An EXPLICIT ``cred_id`` — or a configured ``agentCredId``
    — that is missing, wrong-kind, or has no key RAISES CredResolutionError. When neither is set, return
    an empty string so the configured provider may use its process environment. Returns a reference
    string, never a resolved value.

    An empty selected Cred is not the same as no selection: returning ``""`` here would let the Agent
    silently continue under an ambient provider identity.
    """
    if cred_id is not None:
        explicit_id = str(cred_id).strip()
        if not explicit_id:
            raise CredResolutionError("selected agent credential id is empty")
        c = cred_get(explicit_id)
        if not c or c.get("kind") != "agent":
            raise CredResolutionError("selected agent credential is missing or has the wrong kind")
        ref = c["fields"].get("apiKey")
        if not ref:
            raise CredResolutionError("selected agent credential has no API key reference")
        return str(ref)
    default_id = get_setting("agentCredId", "global")
    if default_id:
        c = cred_get(default_id)
        if not c or c.get("kind") != "agent":
            raise CredResolutionError("selected agent credential is missing or has the wrong kind")
        ref = c["fields"].get("apiKey")
        if not ref:
            raise CredResolutionError("selected agent credential has no API key reference")
        return str(ref)
    return ""


def record_agent_egress_event(event: dict) -> None:
    """Persist one value-free Agent catalog-tool audit event in one metadata transaction."""
    columns = event.get("columns") or []
    if not isinstance(columns, list):
        columns = list(columns)
    with session() as s:
        s.add(AgentEgressEvent(
            provider=str(event.get("provider") or ""),
            model=str(event.get("model") or ""),
            tool=str(event.get("tool") or ""),
            dataset=(None if event.get("dataset") is None else str(event.get("dataset"))),
            columns_json=json.dumps(columns),
            row_count=event.get("rowCount") if event.get("rowCount") is not None else event.get("row_count"),
            event_json=json.dumps(event, default=str),
        ))


def list_agent_egress_events(*, limit: int = 200) -> list[dict]:
    """Return recent agent egress audit events as plain dicts (newest last within the window)."""
    with session() as s:
        rows = s.scalars(
            select(AgentEgressEvent).order_by(AgentEgressEvent.id.desc()).limit(max(1, int(limit)))
        ).all()
        out = []
        for row in reversed(rows):
            try:
                payload = json.loads(row.event_json)
            except Exception:  # noqa: BLE001
                payload = {
                    "provider": row.provider,
                    "model": row.model,
                    "tool": row.tool,
                    "dataset": row.dataset,
                    "columns": json.loads(row.columns_json or "[]"),
                    "rowCount": row.row_count,
                }
            payload["id"] = row.id
            payload["createdAt"] = row.created_at.isoformat() if row.created_at else None
            out.append(payload)
        return out


# Import sibling modules that declare ORM models on Base so Base.metadata is complete for
# migrations and create_all regardless of which module is imported first. Must stay at the end:
# these modules import metadb's helpers defined above.
from hub import bounded_fanout as _bounded_fanout_models  # noqa: E402,F401
