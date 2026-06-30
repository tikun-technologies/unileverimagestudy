"""add study_analysis_settings table

Revision ID: 20260629_analysis_settings
Revises: 20260626_invite_notifications
Create Date: 2026-06-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260629_analysis_settings"
down_revision: Union[str, Sequence[str], None] = "20260626_invite_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_analysis_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("study_id", sa.UUID(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", name="uq_study_analysis_settings_study_id"),
    )
    op.create_index("ix_study_analysis_settings_study_id", "study_analysis_settings", ["study_id"], unique=True)
    op.create_index("ix_study_analysis_settings_updated_by_id", "study_analysis_settings", ["updated_by_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_study_analysis_settings_updated_by_id", table_name="study_analysis_settings")
    op.drop_index("ix_study_analysis_settings_study_id", table_name="study_analysis_settings")
    op.drop_table("study_analysis_settings")
