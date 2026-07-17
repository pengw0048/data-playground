"""Persist explicit output-port identity for durable full-profile jobs.

Revision ID: 0005_profile_output_ports
Revises: 0004_local_run_input_admissions
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_profile_output_ports"
down_revision = "0004_local_run_input_admissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_records", sa.Column("target_port_id", sa.String(), nullable=True))
    op.add_column("run_states", sa.Column("target_port_id", sa.String(), nullable=True))
    op.add_column("profile_job_latest", sa.Column("target_port_id", sa.String(), nullable=True))
    op.add_column("run_terminal_fences", sa.Column("target_port_id", sa.String(), nullable=True))

    # The old unpublished profile identity had no recoverable port. Keeping those rows would let a
    # restart present statistics for whichever port happens to be selected now, so fail closed by
    # dropping only old profile job state. Every new live, projection, fence, and history identity
    # is explicit; retaining an old result would make the history route guess its sibling port.
    op.execute(sa.text("DELETE FROM run_records WHERE job_type = 'profile'"))
    op.execute(sa.text("DELETE FROM profile_job_latest"))
    op.execute(sa.text("DELETE FROM run_states WHERE job_type = 'profile'"))
    op.execute(sa.text("DELETE FROM run_terminal_fences WHERE job_type = 'profile'"))
    with op.batch_alter_table("profile_job_latest") as batch:
        batch.alter_column(
            "target_port_id", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    op.drop_column("run_terminal_fences", "target_port_id")
    op.drop_column("profile_job_latest", "target_port_id")
    op.drop_column("run_states", "target_port_id")
    op.drop_column("run_records", "target_port_id")
