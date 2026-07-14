"""durable external backend jobs and publication fencing

Revision ID: 0022_backend_jobs
Revises: 0021_local_result_artifacts
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_backend_jobs"
down_revision = "0021_local_result_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A prebound external run has an authoritative principal before it is allowed to allocate sink
    # attempts. The DB-clock lease keeps boot recovery from mistaking that short handoff window for an
    # orphan, while its random token fences an old caller from cleaning up a later binding.
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(sa.Column("preallocation_token", sa.String(), nullable=True))
        batch.add_column(sa.Column(
            "preallocation_expires_at", sa.DateTime(timezone=True), nullable=True))

    # 0018 introduced logical run ids without uniqueness. Keep every history row, but detach older
    # duplicate links before making completion replay idempotent at the database boundary. The newest
    # row retains the logical run id; NULL legacy ids remain unrestricted on SQLite and PostgreSQL.
    op.execute(sa.text("""
        UPDATE run_records
        SET run_id = NULL
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY canvas_id, run_id
                    ORDER BY CASE WHEN created_at IS NULL THEN 1 ELSE 0 END,
                             created_at DESC, id DESC
                ) AS duplicate_rank
                FROM run_records
                WHERE canvas_id IS NOT NULL AND run_id IS NOT NULL
            ) ranked
            WHERE duplicate_rank > 1
        )
    """))
    with op.batch_alter_table("run_records") as batch:
        batch.create_unique_constraint("uq_run_record_canvas_run", ["canvas_id", "run_id"])
    op.create_table(
        "run_backend_jobs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("cluster_ref", sa.String(), nullable=True),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("submission_id", sa.String(), nullable=False),
        sa.Column("job_uri", sa.Text(), nullable=False),
        sa.Column("result_uri", sa.Text(), nullable=False),
        sa.Column("code_ref", sa.String(), nullable=True),
        # The endpoint is a non-secret durable routing handle. Recovery and cancellation must not
        # depend on the replacement process still carrying the original environment configuration.
        sa.Column("control_address", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("submission_state", sa.String(), nullable=False, server_default="queued"),
        sa.Column("submission_owner", sa.String(), nullable=True),
        sa.Column("submission_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publication_state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("publication_owner", sa.String(), nullable=True),
        sa.Column("publication_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_control_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_blocked_reason", sa.Text(), nullable=True),
        sa.Column("job_doc", sa.Text(), nullable=True),
        sa.Column("publication_doc", sa.Text(), nullable=True),
        sa.Column("result_doc", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("backend", "submission_id", name="uq_run_backend_submission"),
    )
    op.create_index("ix_run_backend_jobs_backend", "run_backend_jobs", ["backend"])
    op.create_table(
        "run_terminal_fences",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("auth_canvas_id", sa.String(), nullable=True),
        sa.Column("canvas_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing terminal detail is already authoritative. Backfill its compact status and authorization
    # identity before retention can prune run_states; this preserves access and prevents resurrection.
    op.execute(sa.text("""
        INSERT INTO run_terminal_fences
            (run_id, status, created_by, auth_canvas_id, canvas_id, created_at)
        SELECT run_id, status, created_by, auth_canvas_id, canvas_id, CURRENT_TIMESTAMP
        FROM run_states
        WHERE status IN ('done', 'failed', 'cancelled')
    """))
    op.create_table(
        "catalog_publication_events",
        sa.Column("event_key", sa.String(), primary_key=True),
        sa.Column("effect_type", sa.String(), nullable=False, server_default="usage"),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("catalog_publication_events")
    op.drop_table("run_terminal_fences")
    op.drop_index("ix_run_backend_jobs_backend", table_name="run_backend_jobs")
    op.drop_table("run_backend_jobs")
    with op.batch_alter_table("run_records") as batch:
        batch.drop_constraint("uq_run_record_canvas_run", type_="unique")
    with op.batch_alter_table("run_states") as batch:
        batch.drop_column("preallocation_expires_at")
        batch.drop_column("preallocation_token")
