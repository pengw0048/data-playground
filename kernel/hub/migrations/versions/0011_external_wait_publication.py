"""Finalize durable external waits through typed publication.

Revision ID: 0011_external_wait_publication
Revises: 0010_durable_external_waits
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_external_wait_publication"
down_revision = "0010_durable_external_waits"
branch_labels = None
depends_on = None


_PHASES = (
    "'unsubmitted','submitting','accepted','running','provider_succeeded',"
    "'downloading','downloaded','publishing','published',"
    "'provider_failed','provider_cancelled','finalization_failed',"
    "'cancelled_before_submit','cancelled_after_success'"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_external_waits") as batch:
        batch.drop_constraint("ck_external_wait_phase", type_="check")
        batch.add_column(sa.Column("download_evidence", sa.Text(), nullable=True))
        batch.add_column(sa.Column("stage_dev", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("stage_ino", sa.BigInteger(), nullable=True))
        batch.create_check_constraint("ck_external_wait_phase", f"phase IN ({_PHASES})")
        batch.create_check_constraint(
            "ck_external_wait_stage_identity",
            "(stage_dev IS NULL AND stage_ino IS NULL) OR "
            "(stage_dev IS NOT NULL AND stage_ino IS NOT NULL AND stage_dev >= 0 AND stage_ino >= 0)",
        )


def downgrade() -> None:
    with op.batch_alter_table("durable_external_waits") as batch:
        batch.drop_constraint("ck_external_wait_stage_identity", type_="check")
        batch.drop_constraint("ck_external_wait_phase", type_="check")
        batch.drop_column("stage_ino")
        batch.drop_column("stage_dev")
        batch.drop_column("download_evidence")
        batch.create_check_constraint(
            "ck_external_wait_phase",
            "phase IN ('unsubmitted','submitting','accepted','running','provider_succeeded',"
            "'provider_failed','provider_cancelled','cancelled_before_submit')",
        )
