"""ownership-aware object attempt lifecycle and stable catalog identity

Revision ID: 0020_object_attempt_lifecycle
Revises: 0019_object_attempts
Create Date: 2026-07-12
"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import sqlalchemy as sa
from alembic import op


revision = "0020_object_attempt_lifecycle"
down_revision = "0019_object_attempts"
branch_labels = None
depends_on = None


_STATES = (
    "allocated", "writing", "committed", "published", "superseded", "abandoned",
    "delete_pending", "deleting", "delete_verifying", "deleted", "quarantined",
)


def _json_uri(raw: object) -> str | None:
    try:
        doc = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    value = doc.get("uri") or doc.get("outputUri") or doc.get("output_uri")
    return str(value).rstrip("/") if value else None


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("installation_identity", sa.Column("storage_namespace", sa.String(), nullable=True))
    op.add_column("installation_identity", sa.Column("storage_fingerprint", sa.String(), nullable=True))
    namespace = os.environ.get("DP_STORAGE_NAMESPACE", "").strip() or (
        "dp-" + uuid.uuid4().hex[:20])
    if len(namespace.encode()) > 80:
        raise RuntimeError("DP_STORAGE_NAMESPACE must be at most 80 UTF-8 bytes")
    bind.execute(
        sa.text("UPDATE installation_identity SET storage_namespace=:namespace WHERE id=1"),
        {"namespace": namespace},
    )
    with op.batch_alter_table("installation_identity", recreate="auto") as batch:
        batch.alter_column("storage_namespace", existing_type=sa.String(), nullable=False)
        batch.create_unique_constraint(
            "uq_installation_identity_storage_namespace", ["storage_namespace"]
        )

    additions = (
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("allocation_key", sa.String(), nullable=True),
        sa.Column("storage_namespace", sa.String(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("logical_id", sa.String(), nullable=True),
        sa.Column("catalog_epoch", sa.Integer(), nullable=True),
        sa.Column("publish_seq", sa.BigInteger(), nullable=True),
        sa.Column("terminal_proof_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quiet_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inventory_hash", sa.String(), nullable=True),
        sa.Column("inventory_observations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inventory_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delete_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delete_owner", sa.String(), nullable=True),
        sa.Column("delete_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_delete_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_empty_observations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delete_empty_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
    )
    for column in additions:
        op.add_column("object_attempts", column)

    # Expand the state constraint before quarantining legacy rows. SQLite enforces the old CHECK on
    # UPDATE, so changing the data first would fail precisely on the rows this migration must fence.
    with op.batch_alter_table("object_attempts", recreate="always") as batch:
        batch.drop_constraint("ck_object_attempt_state", type_="check")
        batch.create_check_constraint(
            "ck_object_attempt_state", "state IN (%s)" % ", ".join(
                repr(s) for s in (*_STATES, "retiring", "retired", "discarding"))
        )

    rows = bind.execute(sa.text(
        "SELECT uri, state FROM object_attempts ORDER BY created_at, uri"
    )).mappings().all()
    for raw in rows:
        attempt_id = uuid.uuid4().hex
        bind.execute(sa.text("""
            UPDATE object_attempts
               SET attempt_id=:attempt_id,
                   allocation_key=:allocation_key,
                   storage_namespace=:namespace,
                   generation=1,
                   state=:state,
                   quarantine_reason=:reason
             WHERE uri=:uri
        """), {
            "attempt_id": attempt_id,
            "allocation_key": f"legacy:{attempt_id}",
            "namespace": namespace,
            "state": "quarantined",
            "reason": "pre-lifecycle row has no provider-complete terminal inventory",
            "uri": raw["uri"],
        })

    with op.batch_alter_table("object_attempts", recreate="always") as batch:
        batch.drop_index("ix_object_attempts_reference_key")
        batch.drop_constraint("ck_object_attempt_state", type_="check")
        batch.create_check_constraint(
            "ck_object_attempt_state", "state IN (%s)" % ", ".join(repr(s) for s in _STATES)
        )
        batch.alter_column("attempt_id", existing_type=sa.String(), nullable=False)
        batch.alter_column("allocation_key", existing_type=sa.String(), nullable=False)
        batch.alter_column("storage_namespace", existing_type=sa.String(), nullable=False)
        batch.alter_column("generation", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint("uq_object_attempt_attempt_id", ["attempt_id"])
        batch.create_unique_constraint(
            "uq_object_attempt_allocation_generation", ["allocation_key", "generation"]
        )
        batch.create_unique_constraint(
            "uq_object_attempt_logical_publication",
            ["logical_id", "catalog_epoch", "publish_seq"],
        )
        batch.drop_column("reference_key")
    op.create_index(
        "ix_object_attempts_eligibility", "object_attempts",
        ["state", "quiet_until", "next_delete_at", "created_at"],
    )

    op.create_table(
        "object_attempt_allocations",
        sa.Column("allocation_key", sa.String(), primary_key=True),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["attempt_uri"], ["object_attempts.uri"]),
    )
    op.create_table(
        "object_attempt_refs",
        sa.Column("ref_type", sa.String(), nullable=False),
        sa.Column("ref_key", sa.String(), nullable=False),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ref_type", "ref_key"),
        sa.ForeignKeyConstraint(["attempt_uri"], ["object_attempts.uri"]),
    )
    op.create_index("ix_object_attempt_refs_attempt", "object_attempt_refs", ["attempt_uri"])
    op.create_table(
        "object_attempt_leases",
        sa.Column("lease_id", sa.String(), primary_key=True),
        sa.Column("attempt_uri", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_type", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "lease_type IN ('read', 'write', 'publish', 'delete')",
            name="ck_object_attempt_lease_type"),
        sa.ForeignKeyConstraint(["attempt_uri"], ["object_attempts.uri"]),
    )
    op.create_index(
        "ix_object_attempt_leases_active", "object_attempt_leases",
        ["attempt_uri", "lease_type", "expires_at"],
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
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_commit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "member_type IN ('object_version', 'delete_marker', 'multipart_upload', "
            "'unversioned_object')", name="ck_object_attempt_inventory_member_type"),
        sa.PrimaryKeyConstraint("attempt_uri", "member_id"),
        sa.ForeignKeyConstraint(["attempt_uri"], ["object_attempts.uri"]),
    )
    op.create_index(
        "ix_object_attempt_inventory_object_key", "object_attempt_inventory", ["object_key"])
    op.create_table(
        "object_storage_claims",
        sa.Column("storage_namespace", sa.String(), primary_key=True),
        sa.Column("storage_scope", sa.String(), primary_key=True),
        sa.Column("claim_token", sa.String(), nullable=True),
        sa.Column("marker_etag", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "catalog_logical_datasets",
        sa.Column("logical_id", sa.String(), primary_key=True),
        sa.Column("catalog_key", sa.String(), nullable=False, unique=True),
        sa.Column("logical_uri", sa.String(), nullable=False, unique=True),
        sa.Column("current_uri", sa.String(), nullable=True),
        sa.Column("current_publish_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_publish_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("catalog_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(), nullable=False, server_default="active"),
        sa.Column("governance_doc", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("metadata_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('active', 'unregistered')", name="ck_catalog_logical_state"),
    )
    op.add_column("catalog_entries", sa.Column("logical_id", sa.String(), nullable=True))
    op.create_index("ix_catalog_entries_logical_id", "catalog_entries", ["logical_id"], unique=True)
    with op.batch_alter_table("catalog_embeddings", recreate="always") as batch:
        batch.alter_column("uri", new_column_name="catalog_key", existing_type=sa.String())
    with op.batch_alter_table("catalog_declared_keys", recreate="always") as batch:
        batch.alter_column("uri", new_column_name="catalog_key", existing_type=sa.String())

    attempts = {
        row["uri"].rstrip("/"): row
        for row in bind.execute(sa.text(
            "SELECT uri, logical_uri, generation, allocation_key FROM object_attempts"
        )).mappings()
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    for uri, row in attempts.items():
        bind.execute(sa.text("""
            INSERT INTO object_attempt_allocations(allocation_key, attempt_uri, generation, updated_at)
            VALUES (:key, :uri, :generation, :now)
        """), {"key": row["allocation_key"], "uri": uri, "generation": row["generation"], "now": now})

    # A pre-0020 ownership row has no provider-complete version/delete-marker/multipart inventory.
    # It is therefore quarantined above and MUST NOT remain reachable through a cache or catalog
    # pointer. Run history may retain the unavailable URI for diagnosis, but no lifecycle ref is
    # created for it. A later run will allocate a new, fully fenced attempt.
    for row in bind.execute(sa.text("SELECT key, doc FROM result_cache")).mappings():
        if _json_uri(row["doc"]) in attempts:
            bind.execute(
                sa.text("DELETE FROM result_cache WHERE key=:key"), {"key": row["key"]}
            )

    catalog_rows = list(bind.execute(sa.text(
        "SELECT uri, tbl_id, doc FROM catalog_entries"
    )).mappings())
    for row in catalog_rows:
        uri = str(row["uri"]).rstrip("/")
        if uri not in attempts:
            continue
        bind.execute(sa.text("DELETE FROM catalog_tags WHERE uri=:uri"), {"uri": uri})
        bind.execute(sa.text("DELETE FROM catalog_columns WHERE uri=:uri"), {"uri": uri})
        bind.execute(
            sa.text("DELETE FROM catalog_embeddings WHERE catalog_key=:uri"), {"uri": uri}
        )
        bind.execute(
            sa.text("DELETE FROM catalog_declared_keys WHERE catalog_key=:uri"), {"uri": uri}
        )
        bind.execute(sa.text(
            "DELETE FROM catalog_edges WHERE parent=:uri OR child=:uri"
        ), {"uri": uri})
        for relationship in bind.execute(sa.text(
                "SELECT rel_key, doc FROM catalog_relationships"
        )).mappings():
            try:
                relationship_doc = json.loads(relationship["doc"] or "{}")
            except (TypeError, ValueError):
                continue
            endpoints = {
                str(relationship_doc.get(key)).rstrip("/")
                for key in ("leftUri", "left_uri", "rightUri", "right_uri")
                if relationship_doc.get(key)
            }
            if uri in endpoints:
                bind.execute(
                    sa.text("DELETE FROM catalog_relationships WHERE rel_key=:rel_key"),
                    {"rel_key": relationship["rel_key"]},
                )
        bind.execute(sa.text("DELETE FROM catalog_entries WHERE uri=:uri"), {"uri": uri})


def downgrade() -> None:
    bind = op.get_bind()
    managed = bind.execute(sa.text("SELECT count(*) FROM object_attempts")).scalar() or 0
    if managed:
        raise RuntimeError(
            "cannot downgrade 0020_object_attempt_lifecycle while managed object attempts exist; "
            "export or explicitly retire their ownership state first"
        )

    with op.batch_alter_table("catalog_declared_keys", recreate="always") as batch:
        batch.alter_column("catalog_key", new_column_name="uri", existing_type=sa.String())
    with op.batch_alter_table("catalog_embeddings", recreate="always") as batch:
        batch.alter_column("catalog_key", new_column_name="uri", existing_type=sa.String())
    op.drop_index("ix_catalog_entries_logical_id", table_name="catalog_entries")
    op.drop_column("catalog_entries", "logical_id")
    op.drop_table("catalog_logical_datasets")
    op.drop_table("object_storage_claims")
    op.drop_index("ix_object_attempt_inventory_object_key", table_name="object_attempt_inventory")
    op.drop_table("object_attempt_inventory")
    op.drop_index("ix_object_attempt_leases_active", table_name="object_attempt_leases")
    op.drop_table("object_attempt_leases")
    op.drop_index("ix_object_attempt_refs_attempt", table_name="object_attempt_refs")
    op.drop_table("object_attempt_refs")
    op.drop_table("object_attempt_allocations")
    op.drop_index("ix_object_attempts_eligibility", table_name="object_attempts")
    with op.batch_alter_table("object_attempts", recreate="always") as batch:
        batch.add_column(sa.Column("reference_key", sa.String(), nullable=True))
        batch.drop_constraint("uq_object_attempt_logical_publication", type_="unique")
        batch.drop_constraint("uq_object_attempt_allocation_generation", type_="unique")
        batch.drop_constraint("uq_object_attempt_attempt_id", type_="unique")
        batch.drop_constraint("ck_object_attempt_state", type_="check")
        batch.create_check_constraint(
            "ck_object_attempt_state",
            "state IN ('writing', 'published', 'retiring', 'retired', 'discarding')",
        )
        for name in (
            "quarantine_reason", "deleted_at", "next_delete_at", "delete_attempts",
            "delete_empty_observed_at", "delete_empty_observations",
            "delete_lease_expires_at", "delete_owner", "delete_epoch", "inventory_complete",
            "inventory_observations", "inventory_hash", "quiet_until", "terminal_proof_at",
            "publish_seq", "catalog_epoch", "logical_id", "generation", "storage_namespace",
            "allocation_key", "attempt_id",
        ):
            batch.drop_column(name)
    op.create_index("ix_object_attempts_reference_key", "object_attempts", ["reference_key"])
    with op.batch_alter_table("installation_identity", recreate="auto") as batch:
        batch.drop_constraint("uq_installation_identity_storage_namespace", type_="unique")
        batch.drop_column("storage_fingerprint")
        batch.drop_column("storage_namespace")
