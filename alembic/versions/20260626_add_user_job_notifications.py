"""add user job notifications table

Revision ID: 20260626_user_job_notifications
Revises: 20260623_dismissed_jobs
Create Date: 2026-06-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260626_user_job_notifications"
down_revision: Union[str, Sequence[str], None] = "20260623_dismissed_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_job_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("study_id", sa.String(length=36), nullable=False),
        sa.Column("study_title", sa.String(length=500), nullable=True),
        sa.Column("job_kind", sa.String(length=32), nullable=False, server_default="task_generation"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("respondents_requested", sa.Integer(), nullable=True),
        sa.Column("respondents_completed", sa.Integer(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "job_id", name="uq_user_job_notifications_user_job"),
    )
    op.create_index("ix_user_job_notifications_user_id", "user_job_notifications", ["user_id"])
    op.create_index("ix_user_job_notifications_job_id", "user_job_notifications", ["job_id"])
    op.create_index("ix_user_job_notifications_study_id", "user_job_notifications", ["study_id"])
    op.create_index(
        "ix_user_job_notifications_user_dismissed",
        "user_job_notifications",
        ["user_id", "dismissed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_job_notifications_user_dismissed", table_name="user_job_notifications")
    op.drop_index("ix_user_job_notifications_study_id", table_name="user_job_notifications")
    op.drop_index("ix_user_job_notifications_job_id", table_name="user_job_notifications")
    op.drop_index("ix_user_job_notifications_user_id", table_name="user_job_notifications")
    op.drop_table("user_job_notifications")
