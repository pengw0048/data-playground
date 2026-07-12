"""durable external backend jobs and publication fencing

Revision ID: 0020_backend_jobs
Revises: 0019_object_attempts
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_backend_jobs"
down_revision = "0019_object_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0018 introduced logical run ids without uniqueness. Keep the newest legacy duplicate before
    # making completion replay idempotent at the database boundary (NULL legacy ids remain unrestricted).
    op.execute(sa.text("""
        DELETE FROM run_records
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY canvas_id, run_id ORDER BY created_at DESC, id DESC
                ) AS duplicate_rank
                FROM run_records
                WHERE run_id IS NOT NULL
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
        sa.Column("publication_state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("publication_owner", sa.String(), nullable=True),
        sa.Column("publication_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_doc", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("backend", "submission_id", name="uq_run_backend_submission"),
    )
    op.create_index("ix_run_backend_jobs_backend", "run_backend_jobs", ["backend"])


def downgrade() -> None:
    op.drop_index("ix_run_backend_jobs_backend", table_name="run_backend_jobs")
    op.drop_table("run_backend_jobs")
    with op.batch_alter_table("run_records") as batch:
        batch.drop_constraint("uq_run_record_canvas_run", type_="unique")
