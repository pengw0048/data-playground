"""Allow reserved checkpoints to bind materialized candidate_dev/ino before commit.

Revision ID: 0014_linear_checkpoint_materialization_identity
Revises: 0013_linear_checkpoint_commit
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_linear_checkpoint_materialization_identity"
down_revision = "0013_linear_checkpoint_commit"
branch_labels = None
depends_on = None


# pending: no evidence, no inode. reserved: no committed evidence fields, but candidate_dev/ino
# may already name the exact materialized inode. committed: full immutable evidence including inode.
_COMMITTED_EVIDENCE = (
    "(phase = 'pending' AND committed_rows IS NULL AND committed_bytes IS NULL "
    "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND committed_at IS NULL "
    "AND candidate_dev IS NULL AND candidate_ino IS NULL) OR "
    "(phase = 'reserved' AND committed_rows IS NULL AND committed_bytes IS NULL "
    "AND content_sha256 IS NULL AND schema_sha256 IS NULL AND committed_at IS NULL) OR "
    "(phase = 'committed' AND committed_rows IS NOT NULL AND committed_bytes IS NOT NULL "
    "AND content_sha256 IS NOT NULL AND schema_sha256 IS NOT NULL AND committed_at IS NOT NULL "
    "AND candidate_dev IS NOT NULL AND candidate_ino IS NOT NULL "
    "AND committed_rows >= 0 AND committed_bytes >= 0)"
)

_PREVIOUS_COMMITTED_EVIDENCE = (
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
        batch.drop_constraint("ck_durable_checkpoint_committed_evidence", type_="check")
        batch.create_check_constraint(
            "ck_durable_checkpoint_committed_evidence", _COMMITTED_EVIDENCE)


def downgrade() -> None:
    bind = op.get_bind()
    reserved_with_inode = bind.execute(sa.text(
        "SELECT COUNT(*) FROM durable_checkpoints "
        "WHERE phase = 'reserved' AND candidate_dev IS NOT NULL"
    )).scalar_one()
    if reserved_with_inode:
        raise RuntimeError(
            "cannot downgrade while reserved linear-checkpoint materialization identity is retained")
    with op.batch_alter_table("durable_checkpoints") as batch:
        batch.drop_constraint("ck_durable_checkpoint_committed_evidence", type_="check")
        batch.create_check_constraint(
            "ck_durable_checkpoint_committed_evidence", _PREVIOUS_COMMITTED_EVIDENCE)
