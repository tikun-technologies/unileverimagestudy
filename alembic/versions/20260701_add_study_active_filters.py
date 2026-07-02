"""add study_active_filters table

Revision ID: 20260701_active_filters
Revises: 20260629_analysis_settings
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260701_active_filters"
down_revision: Union[str, Sequence[str], None] = "20260629_analysis_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_active_filters",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("study_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", "user_id", name="uq_study_active_filter_study_user"),
    )
    op.create_index("ix_study_active_filters_study_id", "study_active_filters", ["study_id"], unique=False)
    op.create_index("ix_study_active_filters_user_id", "study_active_filters", ["user_id"], unique=False)
    op.create_index("idx_study_active_filter_study_user", "study_active_filters", ["study_id", "user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_study_active_filter_study_user", table_name="study_active_filters")
    op.drop_index("ix_study_active_filters_user_id", table_name="study_active_filters")
    op.drop_index("ix_study_active_filters_study_id", table_name="study_active_filters")
    op.drop_table("study_active_filters")
