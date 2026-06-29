"""add dismissed job notifications

Revision ID: 20260623_dismissed_jobs
Revises: 20260617_design_constraints
Create Date: 2026-06-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260623_dismissed_jobs"
down_revision: Union[str, Sequence[str], None] = "20260617_design_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dismissed_job_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "job_id", name="uq_dismissed_job_notifications_user_job"),
    )
    op.create_index(
        "ix_dismissed_job_notifications_user_id",
        "dismissed_job_notifications",
        ["user_id"],
    )
    op.create_index(
        "ix_dismissed_job_notifications_job_id",
        "dismissed_job_notifications",
        ["job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dismissed_job_notifications_job_id", table_name="dismissed_job_notifications")
    op.drop_index("ix_dismissed_job_notifications_user_id", table_name="dismissed_job_notifications")
    op.drop_table("dismissed_job_notifications")
