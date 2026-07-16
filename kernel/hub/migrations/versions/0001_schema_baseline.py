"""Single pre-1.0 baseline for the complete metadata schema.

Revision ID: 0001_schema_baseline
Revises:
"""

import os
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0001_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _initial_storage_namespace() -> str:
    namespace = os.environ.get("DP_STORAGE_NAMESPACE", "").strip()
    if not namespace:
        namespace = f"dp-{uuid.uuid4().hex[:20]}"
    if len(namespace.encode()) > 80:
        raise RuntimeError("DP_STORAGE_NAMESPACE must be at most 80 UTF-8 bytes")
    return namespace


def upgrade() -> None:
    op.create_table(
        "agent_egress_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=True),
        sa.Column("columns_json", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("event_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_egress_events_created_at"),
        "agent_egress_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_egress_events_tool"),
        "agent_egress_events",
        ["tool"],
        unique=False,
    )
    op.create_table(
        "catalog_columns",
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("column", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("uri", "column"),
    )
    op.create_index(
        "ix_catalog_columns_column", "catalog_columns", ["column"], unique=False
    )
    op.create_table(
        "catalog_declared_keys",
        sa.Column("catalog_key", sa.String(), nullable=False),
        sa.Column("columns", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("catalog_key"),
    )
    op.create_table(
        "catalog_lineage_facts",
        sa.Column(
            "id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True, nullable=False,
        ),
        sa.Column("fact_key", sa.String(length=512), nullable=False),
        sa.Column("publication_key", sa.String(length=96), nullable=False),
        sa.Column("fingerprint", sa.String(length=96), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("destination_key", sa.String(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("destination_uri", sa.Text(), nullable=False),
        sa.Column("source_key_hash", sa.String(length=64), nullable=False),
        sa.Column("destination_key_hash", sa.String(length=64), nullable=False),
        sa.Column("source_uri_hash", sa.String(length=64), nullable=False),
        sa.Column("destination_uri_hash", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.String(), nullable=True),
        sa.Column("destination_version", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("producer", sa.String(), nullable=True),
        sa.Column("producer_version", sa.BigInteger(), nullable=True),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("field_mappings_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provenance IN ('run', 'manual', 'imported')",
            name="ck_catalog_lineage_fact_provenance",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_key", name="uq_catalog_lineage_fact_key"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_catalog_lineage_facts_destination_key_hash"),
        "catalog_lineage_facts", ["destination_key_hash"], unique=False,
    )
    op.create_index(
        op.f("ix_catalog_lineage_facts_destination_uri_hash"),
        "catalog_lineage_facts", ["destination_uri_hash"], unique=False,
    )
    op.create_index(
        "ix_catalog_lineage_facts_pair_hash", "catalog_lineage_facts",
        ["source_key_hash", "destination_key_hash"], unique=False,
    )
    op.create_index(
        op.f("ix_catalog_lineage_facts_publication_key"),
        "catalog_lineage_facts", ["publication_key"], unique=False,
    )
    op.create_index(
        op.f("ix_catalog_lineage_facts_source_uri_hash"),
        "catalog_lineage_facts", ["source_uri_hash"], unique=False,
    )
    op.create_table(
        "catalog_embeddings",
        sa.Column("catalog_key", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vec", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("catalog_key"),
    )
    op.create_table(
        "catalog_entries",
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("registration_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("tbl_id", sa.String(), nullable=True),
        sa.Column("folder", sa.String(), server_default="", nullable=False),
        sa.Column("owner", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("usage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("logical_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uri"),
        sa.UniqueConstraint("registration_id"),
        sa.UniqueConstraint("logical_id"),
    )
    op.create_index(
        op.f("ix_catalog_entries_folder"), "catalog_entries", ["folder"], unique=False
    )
    op.create_index(
        op.f("ix_catalog_entries_name"), "catalog_entries", ["name"], unique=False
    )
    op.create_index(
        op.f("ix_catalog_entries_owner"), "catalog_entries", ["owner"], unique=False
    )
    op.create_index(
        op.f("ix_catalog_entries_row_count"),
        "catalog_entries",
        ["row_count"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_entries_tbl_id"), "catalog_entries", ["tbl_id"], unique=False
    )
    op.create_index(
        op.f("ix_catalog_entries_updated_at"),
        "catalog_entries",
        ["updated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_entries_usage"), "catalog_entries", ["usage"], unique=False
    )
    op.create_table(
        "catalog_folders",
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("path"),
    )
    op.create_table(
        "catalog_logical_datasets",
        sa.Column("logical_id", sa.String(), nullable=False),
        sa.Column("catalog_key", sa.String(), nullable=False),
        sa.Column("logical_uri", sa.String(), nullable=False),
        sa.Column("current_uri", sa.String(), nullable=True),
        sa.Column(
            "current_publish_seq", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "next_publish_seq", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("catalog_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("state", sa.String(), server_default="active", nullable=False),
        sa.Column("governance_doc", sa.Text(), server_default="{}", nullable=False),
        sa.Column("metadata_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("usage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'unregistered')", name="ck_catalog_logical_state"
        ),
        sa.PrimaryKeyConstraint("logical_id"),
        sa.UniqueConstraint("catalog_key"),
        sa.UniqueConstraint("logical_uri"),
    )
    op.create_table(
        "catalog_publication_events",
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("effect_type", sa.String(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_key"),
    )
    op.create_table(
        "catalog_relationships",
        sa.Column("rel_key", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("rel_key"),
    )
    op.create_table(
        "catalog_tags",
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("tag", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("uri", "tag"),
    )
    op.create_index("ix_catalog_tags_tag", "catalog_tags", ["tag"], unique=False)
    op.create_table(
        "creds",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "installation_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.Column("storage_namespace", sa.String(), nullable=False),
        sa.Column("storage_fingerprint", sa.String(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_installation_identity_singleton"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_token", name="uq_installation_identity_owner_token"),
        sa.UniqueConstraint(
            "storage_namespace", name="uq_installation_identity_storage_namespace"
        ),
    )
    op.bulk_insert(
        sa.table(
            "installation_identity",
            sa.column("id", sa.Integer()),
            sa.column("owner_token", sa.String()),
            sa.column("storage_namespace", sa.String()),
            sa.column("storage_fingerprint", sa.String()),
        ),
        [
            {
                "id": 1,
                "owner_token": uuid.uuid4().hex,
                "storage_namespace": _initial_storage_namespace(),
                "storage_fingerprint": None,
            }
        ],
    )
    op.create_table(
        "kernels",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("kernel_id", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=True),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("canvas_id"),
    )
    op.create_table(
        "local_result_artifacts",
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("namespace_id", sa.String(), nullable=False),
        sa.Column("storage_root", sa.Text(), nullable=False),
        sa.Column("lock_name", sa.String(), nullable=False),
        sa.Column("lock_token", sa.String(), nullable=True),
        sa.Column(
            "lock_protected", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("state", sa.String(), server_default="writing", nullable=False),
        sa.Column("writer_run_id", sa.String(), nullable=True),
        sa.Column("writer_token", sa.String(), nullable=True),
        sa.Column("delete_token", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "((state = 'deleting' AND delete_token IS NOT NULL AND delete_attempted_at IS NOT NULL) OR (state <> 'deleting' AND delete_token IS NULL AND delete_attempted_at IS NULL))",
            name="ck_local_result_artifact_delete_state",
        ),
        sa.CheckConstraint(
            "state <> 'ready' OR committed_at IS NOT NULL",
            name="ck_local_result_artifact_ready_commit",
        ),
        sa.CheckConstraint(
            "state IN ('writing', 'ready', 'deleting')",
            name="ck_local_result_artifact_state",
        ),
        sa.CheckConstraint(
            "((lock_protected AND lock_token IS NOT NULL) OR (NOT lock_protected AND lock_token IS NULL))",
            name="ck_local_result_artifact_lock_pair",
        ),
        sa.CheckConstraint(
            "((writer_run_id IS NULL AND writer_token IS NULL) OR (writer_run_id IS NOT NULL AND writer_token IS NOT NULL))",
            name="ck_local_result_artifact_writer_pair",
        ),
        sa.PrimaryKeyConstraint("uri"),
        sa.UniqueConstraint(
            "namespace_id", "lock_name", name="uq_local_result_artifact_namespace_lock"
        ),
    )
    op.create_index(
        "ix_local_result_artifacts_reclaim",
        "local_result_artifacts",
        ["namespace_id", "state", "delete_attempted_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_local_result_artifacts_writer",
        "local_result_artifacts",
        ["writer_run_id", "writer_token"],
        unique=False,
    )
    op.create_table(
        "local_result_registry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.Column("lock_cursor_uri", sa.Text(), nullable=True),
        sa.Column("reclaim_cursor_uri", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_local_result_registry_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "local_result_registry",
            sa.column("id", sa.Integer()),
            sa.column("owner_token", sa.String()),
            sa.column("lock_cursor_uri", sa.Text()),
            sa.column("reclaim_cursor_uri", sa.Text()),
        ),
        [
            {
                "id": 1,
                "owner_token": uuid.uuid4().hex,
                "lock_cursor_uri": None,
                "reclaim_cursor_uri": None,
            }
        ],
    )
    op.create_table(
        "object_attempts",
        sa.Column("uri", sa.String(), nullable=False),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("allocation_key", sa.String(), nullable=False),
        sa.Column("storage_namespace", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("logical_uri", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("logical_id", sa.String(), nullable=True),
        sa.Column("catalog_epoch", sa.Integer(), nullable=True),
        sa.Column("publish_seq", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(), server_default="writing", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gc_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_proof_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quiet_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inventory_hash", sa.String(), nullable=True),
        sa.Column(
            "inventory_observations", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "inventory_complete", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("delete_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delete_owner", sa.String(), nullable=True),
        sa.Column("delete_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_delete_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delete_empty_observations",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "delete_empty_observed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('region', 'sink')", name="ck_object_attempt_kind"),
        sa.CheckConstraint(
            "state IN ('allocated', 'writing', 'committed', 'published', 'superseded', 'abandoned', 'delete_pending', 'deleting', 'delete_verifying', 'deleted', 'quarantined')",
            name="ck_object_attempt_state",
        ),
        sa.PrimaryKeyConstraint("uri"),
        sa.UniqueConstraint(
            "allocation_key",
            "generation",
            name="uq_object_attempt_allocation_generation",
        ),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint(
            "logical_id",
            "catalog_epoch",
            "publish_seq",
            name="uq_object_attempt_logical_publication",
        ),
    )
    op.create_index(
        "ix_object_attempts_eligibility",
        "object_attempts",
        ["state", "quiet_until", "next_delete_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_object_attempts_gc",
        "object_attempts",
        ["state", "gc_attempted_at", "retired_at", "created_at", "uri"],
        unique=False,
    )
    op.create_index(
        op.f("ix_object_attempts_kind"), "object_attempts", ["kind"], unique=False
    )
    op.create_index(
        op.f("ix_object_attempts_logical_id"),
        "object_attempts",
        ["logical_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_object_attempts_logical_uri"),
        "object_attempts",
        ["logical_uri"],
        unique=False,
    )
    op.create_index(
        op.f("ix_object_attempts_run_id"), "object_attempts", ["run_id"], unique=False
    )
    op.create_index(
        "ix_object_attempts_sink_target",
        "object_attempts",
        ["kind", "logical_uri", "state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_object_attempts_state"), "object_attempts", ["state"], unique=False
    )
    op.create_table(
        "object_storage_claims",
        sa.Column("storage_namespace", sa.String(), nullable=False),
        sa.Column("storage_scope", sa.String(), nullable=False),
        sa.Column("claim_token", sa.String(), nullable=True),
        sa.Column("marker_etag", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("storage_namespace", "storage_scope"),
    )
    op.create_table(
        "profile_job_latest",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("attempt_order", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_order >= 1", name="ck_profile_latest_attempt_positive"
        ),
        sa.PrimaryKeyConstraint("canvas_id", "target_node_id", "plan_digest"),
        sa.UniqueConstraint(
            "canvas_id", "attempt_order", name="uq_profile_latest_canvas_attempt"
        ),
    )
    op.create_index(
        "ix_profile_job_latest_canvas_attempt",
        "profile_job_latest",
        ["canvas_id", "attempt_order"],
        unique=False,
    )
    op.create_table(
        "profile_job_retention",
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column(
            "next_attempt_order", sa.BigInteger(), server_default="1", nullable=False
        ),
        sa.Column("cutoff_attempt_order", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cutoff_attempt_order IS NULL OR cutoff_attempt_order >= 1",
            name="ck_profile_cutoff_attempt_positive",
        ),
        sa.CheckConstraint(
            "next_attempt_order >= 1", name="ck_profile_next_attempt_positive"
        ),
        sa.PrimaryKeyConstraint("canvas_id"),
    )
    op.create_table(
        "result_cache",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "run_backend_jobs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("cluster_ref", sa.String(), nullable=True),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("submission_id", sa.String(), nullable=False),
        sa.Column("job_uri", sa.Text(), nullable=False),
        sa.Column("result_uri", sa.Text(), nullable=False),
        sa.Column("code_ref", sa.String(), nullable=True),
        sa.Column("control_address", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column(
            "submission_state", sa.String(), server_default="queued", nullable=False
        ),
        sa.Column("submission_owner", sa.String(), nullable=True),
        sa.Column("submission_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publication_state", sa.String(), nullable=False),
        sa.Column("publication_owner", sa.String(), nullable=True),
        sa.Column("publication_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_control_observed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("recovery_blocked_reason", sa.Text(), nullable=True),
        sa.Column("job_doc", sa.Text(), nullable=True),
        sa.Column("publication_doc", sa.Text(), nullable=True),
        sa.Column("result_doc", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "backend", "submission_id", name="uq_run_backend_submission"
        ),
    )
    op.create_index(
        op.f("ix_run_backend_jobs_backend"),
        "run_backend_jobs",
        ["backend"],
        unique=False,
    )
    op.create_table(
        "run_states",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("kernel_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("auth_canvas_id", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("job_type", sa.String(), server_default="run", nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("plan_digest", sa.String(length=64), nullable=True),
        sa.Column("profile_attempt_order", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preallocation_token", sa.String(), nullable=True),
        sa.Column(
            "preallocation_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1",
            name="ck_run_state_profile_attempt_positive",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "canvas_id",
            "profile_attempt_order",
            name="uq_run_state_canvas_profile_attempt",
        ),
    )
    op.create_index(
        op.f("ix_run_states_canvas_id"), "run_states", ["canvas_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_states_request_id"), "run_states", ["request_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_states_status"), "run_states", ["status"], unique=False
    )
    op.create_table(
        "run_terminal_fences",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("auth_canvas_id", sa.String(), nullable=True),
        sa.Column("canvas_id", sa.String(), nullable=True),
        sa.Column("job_type", sa.String(), server_default="run", nullable=False),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("plan_digest", sa.String(length=64), nullable=True),
        sa.Column("profile_attempt_order", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "profile_attempt_order IS NULL OR profile_attempt_order >= 1",
            name="ck_terminal_fence_profile_attempt_positive",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "schema_contracts",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", "version"),
    )
    op.create_table(
        "setting_revisions",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "scope IN ('global', 'user')", name="ck_setting_revision_scope"
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND scope_id = '') OR "
            "(scope = 'user' AND scope_id <> '')",
            name="ck_setting_revision_identity",
        ),
        sa.PrimaryKeyConstraint("scope", "scope_id"),
    )
    op.bulk_insert(
        sa.table(
            "setting_revisions",
            sa.column("scope", sa.String()),
            sa.column("scope_id", sa.String()),
            sa.column("revision", sa.BigInteger()),
        ),
        [{"scope": "global", "scope_id": "", "revision": 0}],
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.CheckConstraint("scope IN ('global', 'user')", name="ck_setting_scope"),
        sa.CheckConstraint(
            "(scope = 'global' AND scope_id = '') OR "
            "(scope = 'user' AND scope_id <> '')",
            name="ck_setting_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "scope_id", "key", name="uq_setting"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("token_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "canvases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_canvases_owner_id"), "canvases", ["owner_id"], unique=False
    )
    op.create_table(
        "workspace_containers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("is_root", sa.Boolean(), server_default="0", nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_workspace_container_ordinal"),
        sa.CheckConstraint("version >= 1", name="ck_workspace_container_version"),
        sa.CheckConstraint("is_root = false OR parent_id IS NULL", name="ck_workspace_container_root"),
        sa.ForeignKeyConstraint(["parent_id"], ["workspace_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_id", "name", name="uq_workspace_container_parent_name"),
    )
    op.create_index(
        op.f("ix_workspace_containers_parent_id"), "workspace_containers", ["parent_id"], unique=False
    )
    op.bulk_insert(
        sa.table(
            "workspace_containers",
            sa.column("id", sa.String()),
            sa.column("parent_id", sa.String()),
            sa.column("name", sa.String()),
            sa.column("ordinal", sa.BigInteger()),
            sa.column("version", sa.BigInteger()),
            sa.column("is_root", sa.Boolean()),
        ),
        [{"id": "workspace-local-root", "parent_id": None, "name": "Workspace",
          "ordinal": 0, "version": 1, "is_root": True}],
    )
    op.create_table(
        "workspace_placements",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("container_id", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.CheckConstraint("target_kind IN ('canvas', 'dataset')", name="ck_workspace_placement_kind"),
        sa.CheckConstraint("ordinal >= 0", name="ck_workspace_placement_ordinal"),
        sa.CheckConstraint("version >= 1", name="ck_workspace_placement_version"),
        sa.ForeignKeyConstraint(["container_id"], ["workspace_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_kind", "target_id", name="uq_workspace_placement_target"),
    )
    op.create_index(
        op.f("ix_workspace_placements_container_id"), "workspace_placements", ["container_id"], unique=False
    )
    op.create_table(
        "local_result_references",
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("owner_kind", sa.String(), nullable=False),
        sa.Column("owner_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["uri"],
            ["local_result_artifacts.uri"],
        ),
        sa.PrimaryKeyConstraint("uri", "owner_kind", "owner_key"),
    )
    op.create_index(
        "ix_local_result_references_owner",
        "local_result_references",
        ["owner_kind", "owner_key"],
        unique=False,
    )
    op.create_table(
        "object_attempt_allocations",
        sa.Column("allocation_key", sa.String(), nullable=False),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_uri"],
            ["object_attempts.uri"],
        ),
        sa.PrimaryKeyConstraint("allocation_key"),
    )
    op.create_table(
        "object_attempt_inventory",
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("member_id", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("member_type", sa.String(), nullable=False),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("version_id", sa.String(), nullable=True),
        sa.Column("upload_id", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("is_commit", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "member_type IN ('object_version', 'delete_marker', 'multipart_upload', 'unversioned_object')",
            name="ck_object_attempt_inventory_member_type",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_uri"],
            ["object_attempts.uri"],
        ),
        sa.PrimaryKeyConstraint("attempt_uri", "member_id"),
    )
    op.create_index(
        op.f("ix_object_attempt_inventory_object_key"),
        "object_attempt_inventory",
        ["object_key"],
        unique=False,
    )
    op.create_table(
        "object_attempt_leases",
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_type", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lease_type IN ('read', 'write', 'publish', 'delete')",
            name="ck_object_attempt_lease_type",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_uri"],
            ["object_attempts.uri"],
        ),
        sa.PrimaryKeyConstraint("lease_id"),
    )
    op.create_index(
        "ix_object_attempt_leases_active",
        "object_attempt_leases",
        ["attempt_uri", "lease_type", "expires_at"],
        unique=False,
    )
    op.create_table(
        "object_attempt_refs",
        sa.Column("ref_type", sa.String(), nullable=False),
        sa.Column("ref_key", sa.String(), nullable=False),
        sa.Column("ref_slot", sa.String(), nullable=False),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_uri"],
            ["object_attempts.uri"],
        ),
        sa.PrimaryKeyConstraint("ref_type", "ref_key", "ref_slot"),
    )
    op.create_index(
        op.f("ix_object_attempt_refs_attempt_uri"),
        "object_attempt_refs",
        ["attempt_uri"],
        unique=False,
    )
    op.create_table(
        "canvas_shares",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.CheckConstraint("role IN ('editor', 'viewer')", name="ck_share_role"),
        sa.ForeignKeyConstraint(
            ["canvas_id"],
            ["canvases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canvas_id", "user_id", name="uq_share"),
    )
    op.create_index(
        op.f("ix_canvas_shares_canvas_id"), "canvas_shares", ["canvas_id"], unique=False
    )
    op.create_index(
        op.f("ix_canvas_shares_user_id"), "canvas_shares", ["user_id"], unique=False
    )
    op.create_table(
        "canvas_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["canvas_id"],
            ["canvases.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_canvas_versions_canvas_id"),
        "canvas_versions",
        ["canvas_id"],
        unique=False,
    )
    op.create_table(
        "run_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canvas_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("job_type", sa.String(), server_default="run", nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("outputs", sa.Text(), server_default="[]", nullable=False),
        sa.Column("profile", sa.Text(), nullable=True),
        sa.Column("per_node", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["canvas_id"],
            ["canvases.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "job_type IN ('run', 'profile')", name="ck_run_record_job_type"
        ),
        sa.UniqueConstraint("canvas_id", "run_id", name="uq_run_record_canvas_run"),
    )
    op.create_index(
        op.f("ix_run_records_canvas_id"), "run_records", ["canvas_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_records_request_id"), "run_records", ["request_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_records_run_id"), "run_records", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_run_records_run_id"), table_name="run_records")
    op.drop_index(op.f("ix_run_records_request_id"), table_name="run_records")
    op.drop_index(op.f("ix_run_records_canvas_id"), table_name="run_records")
    op.drop_table("run_records")
    op.drop_index(op.f("ix_canvas_versions_canvas_id"), table_name="canvas_versions")
    op.drop_table("canvas_versions")
    op.drop_index(op.f("ix_workspace_placements_container_id"), table_name="workspace_placements")
    op.drop_table("workspace_placements")
    op.drop_index(op.f("ix_workspace_containers_parent_id"), table_name="workspace_containers")
    op.drop_table("workspace_containers")
    op.drop_index(op.f("ix_canvas_shares_user_id"), table_name="canvas_shares")
    op.drop_index(op.f("ix_canvas_shares_canvas_id"), table_name="canvas_shares")
    op.drop_table("canvas_shares")
    op.drop_index(
        op.f("ix_object_attempt_refs_attempt_uri"), table_name="object_attempt_refs"
    )
    op.drop_table("object_attempt_refs")
    op.drop_index("ix_object_attempt_leases_active", table_name="object_attempt_leases")
    op.drop_table("object_attempt_leases")
    op.drop_index(
        op.f("ix_object_attempt_inventory_object_key"),
        table_name="object_attempt_inventory",
    )
    op.drop_table("object_attempt_inventory")
    op.drop_table("object_attempt_allocations")
    op.drop_index(
        "ix_local_result_references_owner", table_name="local_result_references"
    )
    op.drop_table("local_result_references")
    op.drop_index(op.f("ix_canvases_owner_id"), table_name="canvases")
    op.drop_table("canvases")
    op.drop_table("users")
    op.drop_table("settings")
    op.drop_table("setting_revisions")
    op.drop_table("schema_contracts")
    op.drop_table("run_terminal_fences")
    op.drop_index(op.f("ix_run_states_status"), table_name="run_states")
    op.drop_index(op.f("ix_run_states_request_id"), table_name="run_states")
    op.drop_index(op.f("ix_run_states_canvas_id"), table_name="run_states")
    op.drop_table("run_states")
    op.drop_index(op.f("ix_run_backend_jobs_backend"), table_name="run_backend_jobs")
    op.drop_table("run_backend_jobs")
    op.drop_table("result_cache")
    op.drop_table("profile_job_retention")
    op.drop_index(
        "ix_profile_job_latest_canvas_attempt", table_name="profile_job_latest"
    )
    op.drop_table("profile_job_latest")
    op.drop_table("object_storage_claims")
    op.drop_index(op.f("ix_object_attempts_state"), table_name="object_attempts")
    op.drop_index("ix_object_attempts_sink_target", table_name="object_attempts")
    op.drop_index(op.f("ix_object_attempts_run_id"), table_name="object_attempts")
    op.drop_index(op.f("ix_object_attempts_logical_uri"), table_name="object_attempts")
    op.drop_index(op.f("ix_object_attempts_logical_id"), table_name="object_attempts")
    op.drop_index(op.f("ix_object_attempts_kind"), table_name="object_attempts")
    op.drop_index("ix_object_attempts_gc", table_name="object_attempts")
    op.drop_index("ix_object_attempts_eligibility", table_name="object_attempts")
    op.drop_table("object_attempts")
    op.drop_table("local_result_registry")
    op.drop_index(
        "ix_local_result_artifacts_writer", table_name="local_result_artifacts"
    )
    op.drop_index(
        "ix_local_result_artifacts_reclaim", table_name="local_result_artifacts"
    )
    op.drop_table("local_result_artifacts")
    op.drop_table("kernels")
    op.drop_table("installation_identity")
    op.drop_table("creds")
    op.drop_index("ix_catalog_tags_tag", table_name="catalog_tags")
    op.drop_table("catalog_tags")
    op.drop_table("catalog_relationships")
    op.drop_table("catalog_publication_events")
    op.drop_table("catalog_logical_datasets")
    op.drop_table("catalog_folders")
    op.drop_index(op.f("ix_catalog_entries_usage"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_updated_at"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_tbl_id"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_row_count"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_owner"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_name"), table_name="catalog_entries")
    op.drop_index(op.f("ix_catalog_entries_folder"), table_name="catalog_entries")
    op.drop_table("catalog_entries")
    op.drop_table("catalog_embeddings")
    op.drop_index(
        "ix_catalog_lineage_facts_pair_hash", table_name="catalog_lineage_facts")
    op.drop_index(
        op.f("ix_catalog_lineage_facts_publication_key"),
        table_name="catalog_lineage_facts",
    )
    op.drop_index(
        op.f("ix_catalog_lineage_facts_source_uri_hash"),
        table_name="catalog_lineage_facts",
    )
    op.drop_index(
        op.f("ix_catalog_lineage_facts_destination_uri_hash"),
        table_name="catalog_lineage_facts",
    )
    op.drop_index(
        op.f("ix_catalog_lineage_facts_destination_key_hash"),
        table_name="catalog_lineage_facts",
    )
    op.drop_table("catalog_lineage_facts")
    op.drop_table("catalog_declared_keys")
    op.drop_index("ix_catalog_columns_column", table_name="catalog_columns")
    op.drop_table("catalog_columns")
    op.drop_index(op.f("ix_agent_egress_events_tool"), table_name="agent_egress_events")
    op.drop_index(
        op.f("ix_agent_egress_events_created_at"), table_name="agent_egress_events"
    )
    op.drop_table("agent_egress_events")
