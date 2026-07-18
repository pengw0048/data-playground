"""Persist one fenced bounded fan-out plan and four leased execution slots.

Revision ID: 0015_bounded_fanout_plan
Revises: 0014_checkpoint_mat_identity
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_bounded_fanout_plan"
down_revision = "0014_checkpoint_mat_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bounded_fanout_plans",
        sa.Column("parent_task_id", sa.String(), sa.ForeignKey("durable_tasks.id"), primary_key=True),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("checkpoint_id", sa.String(128), nullable=False),
        sa.Column("checkpoint_evidence_digest", sa.String(64), nullable=False),
        sa.Column("checkpoint_rows", sa.BigInteger(), nullable=False),
        sa.Column("checkpoint_schema_sha256", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("requested_partitions", sa.Integer(), nullable=False),
        sa.Column("partition_count", sa.Integer(), nullable=False),
        sa.Column("ranges_json", sa.Text(), nullable=False),
        sa.Column("creating_attempt_id", sa.String(), sa.ForeignKey("durable_task_attempts.id"), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # plan_digest is content-only (parent Task is outside it) so it is not globally unique.
        sa.CheckConstraint("operation_id = 'identity_projection_v1'", name="ck_bounded_fanout_plan_operation"),
        sa.CheckConstraint("requested_partitions = 4", name="ck_bounded_fanout_plan_requested_partitions"),
        sa.CheckConstraint("partition_count >= 1 AND partition_count <= 4", name="ck_bounded_fanout_plan_partition_count"),
        sa.CheckConstraint("checkpoint_rows >= 0", name="ck_bounded_fanout_plan_rows"),
    )
    op.create_table(
        "bounded_fanout_units",
        sa.Column("unit_id", sa.String(64), primary_key=True),
        sa.Column("parent_task_id", sa.String(), sa.ForeignKey("bounded_fanout_plans.parent_task_id"), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("partition_index", sa.Integer(), nullable=True),
        sa.Column("range_start", sa.BigInteger(), nullable=False),
        sa.Column("range_end", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result_uri", sa.Text(), sa.ForeignKey("local_result_artifacts.uri"), nullable=True),
        sa.Column("result_rows", sa.BigInteger(), nullable=True),
        sa.Column("result_bytes", sa.BigInteger(), nullable=True),
        sa.Column("result_content_sha256", sa.String(64), nullable=True),
        sa.Column("result_schema_sha256", sa.String(64), nullable=True),
        sa.Column("result_dev", sa.BigInteger(), nullable=True),
        sa.Column("result_ino", sa.BigInteger(), nullable=True),
        sa.Column("active_attempt_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parent_task_id", "partition_index", name="uq_bounded_fanout_unit_partition"),
        sa.CheckConstraint("kind IN ('child','gather')", name="ck_bounded_fanout_unit_kind"),
    )
    op.create_table(
        "bounded_fanout_unit_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("unit_id", sa.String(64), sa.ForeignKey("bounded_fanout_units.unit_id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("parent_attempt_id", sa.String(), sa.ForeignKey("durable_task_attempts.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("owner_token", sa.String(), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("slot_number", sa.Integer(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("unit_id", "attempt_number", name="uq_bounded_fanout_unit_attempt_number"),
    )
    op.create_table(
        "bounded_fanout_slots",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("slot_number", sa.Integer(), primary_key=True),
        sa.Column("holder_attempt_id", sa.String(64), sa.ForeignKey("bounded_fanout_unit_attempts.id"), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope = 'bounded_fanout_v1'", name="ck_bounded_fanout_slot_scope"),
        sa.CheckConstraint("slot_number >= 0 AND slot_number <= 3", name="ck_bounded_fanout_slot_number"),
        sa.UniqueConstraint("holder_attempt_id", name="uq_bounded_fanout_slot_holder"),
    )
    slots = sa.table(
        "bounded_fanout_slots",
        sa.column("scope", sa.String), sa.column("slot_number", sa.Integer),
        sa.column("holder_attempt_id", sa.String), sa.column("claim_token", sa.String),
        sa.column("lease_until", sa.DateTime),
    )
    op.bulk_insert(slots, [
        {"scope": "bounded_fanout_v1", "slot_number": n,
         "holder_attempt_id": None, "claim_token": None, "lease_until": None}
        for n in range(4)])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM bounded_fanout_plans")).scalar_one():
        raise RuntimeError("cannot downgrade while bounded fan-out plans are retained")
    if bind.execute(sa.text(
            "SELECT COUNT(*) FROM bounded_fanout_slots WHERE holder_attempt_id IS NOT NULL"
    )).scalar_one():
        raise RuntimeError("cannot downgrade while bounded fan-out slots are held")
    op.drop_table("bounded_fanout_slots")
    op.drop_table("bounded_fanout_unit_attempts")
    op.drop_table("bounded_fanout_units")
    op.drop_table("bounded_fanout_plans")
