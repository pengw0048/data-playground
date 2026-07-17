"""Extend the hidden linear checkpoint with a committed phase and immutable evidence.

Revision ID: 0013_linear_checkpoint_commit
Revises: 0012_linear_checkpoint_admission
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_linear_checkpoint_commit"
down_revision = "0012_linear_checkpoint_admission"
branch_labels = None
depends_on = None


_COMMITTED_EVIDENCE = (
    "(phase <> 'committed' AND committed_rows IS NULL AND committed_bytes IS NULL "
    "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND committed_at IS NULL "
    "AND candidate_dev IS NULL AND candidate_ino IS NULL) OR "
    "(phase = 'committed' AND committed_rows IS NOT NULL AND committed_bytes IS NOT NULL "
    "AND content_sha256 IS NOT NULL AND schema_sha256 IS NOT NULL AND committed_at IS NOT NULL "
    "AND candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL "
    "AND committed_rows >= 0 AND committed_bytes >= 0)"
)


def upgrade() -> None:
    with op.batch_alter_table("durable_checkpoints") as batch:
        batch.drop_constraint("ck_durable_checkpoint_phase", type_="check")
        batch.drop_constraint("ck_durable_checkpoint_phase_binding", type_="check")
        batch.add_column(sa.Column("committed_rows", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("committed_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("content_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("schema_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_durable_checkpoint_phase", "phase IN ('pending','reserved','committed')")
        batch.create_check_constraint(
            "ck_durable_checkpoint_phase_binding",
            "(phase = 'pending' AND candidate_uri IS NULL) OR "
            "(phase IN ('reserved','committed') AND candidate_uri IS NOT NULL)")
        batch.create_check_constraint(
            "ck_durable_checkpoint_committed_evidence", _COMMITTED_EVIDENCE)


def downgrade() -> None:
    bind = op.get_bind()
    committed = bind.execute(sa.text(
        "SELECT COUNT(*) FROM durable_checkpoints WHERE phase = 'committed'"
    )).scalar_one()
    if committed:
        raise RuntimeError(
            "cannot downgrade while committed linear-checkpoint evidence is retained")
    with op.batch_alter_table("durable_checkpoints") as batch:
        batch.drop_constraint("ck_durable_checkpoint_committed_evidence", type_="check")
        batch.drop_constraint("ck_durable_checkpoint_phase_binding", type_="check")
        batch.drop_constraint("ck_durable_checkpoint_phase", type_="check")
        batch.drop_column("committed_at")
        batch.drop_column("schema_sha256")
        batch.drop_column("content_sha256")
        batch.drop_column("committed_bytes")
        batch.drop_column("committed_rows")
        batch.create_check_constraint(
            "ck_durable_checkpoint_phase", "phase IN ('pending','reserved')")
        batch.create_check_constraint(
            "ck_durable_checkpoint_phase_binding",
            "(phase = 'pending' AND candidate_uri IS NULL) OR "
            "(phase = 'reserved' AND candidate_uri IS NOT NULL)")
