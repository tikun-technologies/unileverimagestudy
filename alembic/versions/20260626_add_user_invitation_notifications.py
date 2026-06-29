"""add user invitation notifications

Revision ID: 20260626_invite_notifications
Revises: 20260626_user_job_notifications
Create Date: 2026-06-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260626_invite_notifications"
down_revision: Union[str, Sequence[str], None] = "20260626_user_job_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_invitation_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("notification_kind", sa.String(length=32), nullable=False),
        sa.Column("study_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("resource_title", sa.String(length=500), nullable=True),
        sa.Column("inviter_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.String(length=36), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_user_invitation_notifications_user_id",
        "user_invitation_notifications",
        ["user_id"],
    )
    op.create_index(
        "ix_user_invitation_notifications_study_id",
        "user_invitation_notifications",
        ["study_id"],
    )
    op.create_index(
        "ix_user_invitation_notifications_project_id",
        "user_invitation_notifications",
        ["project_id"],
    )
    op.create_index(
        "ix_user_invitation_notifications_member_id",
        "user_invitation_notifications",
        ["member_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_invitation_notifications_member_id", table_name="user_invitation_notifications")
    op.drop_index("ix_user_invitation_notifications_project_id", table_name="user_invitation_notifications")
    op.drop_index("ix_user_invitation_notifications_study_id", table_name="user_invitation_notifications")
    op.drop_index("ix_user_invitation_notifications_user_id", table_name="user_invitation_notifications")
    op.drop_table("user_invitation_notifications")
