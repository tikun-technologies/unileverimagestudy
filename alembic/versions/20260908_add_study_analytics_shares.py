"""add study_analytics_shares for live analytics dashboard links

Revision ID: 20260908_analytics_shares
Revises: 20260825_contact_inquiries
Create Date: 2026-09-08

Distinct from studies.share_token (survey participate). One active unrevoked
row per study.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260908_analytics_shares"
down_revision: Union[str, Sequence[str], None] = "20260825_contact_inquiries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_analytics_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("current_filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_study_analytics_shares_token"),
    )
    op.create_index(
        "ix_study_analytics_shares_id",
        "study_analytics_shares",
        ["id"],
        unique=False,
    )
    op.create_index(
        "ix_study_analytics_shares_study_id",
        "study_analytics_shares",
        ["study_id"],
        unique=False,
    )
    op.create_index(
        "ix_study_analytics_shares_created_by_id",
        "study_analytics_shares",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_study_analytics_shares_token",
        "study_analytics_shares",
        ["token"],
        unique=True,
    )
    op.create_index(
        "idx_study_analytics_shares_study_revoked",
        "study_analytics_shares",
        ["study_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "uq_study_analytics_shares_active_study",
        "study_analytics_shares",
        ["study_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_study_analytics_shares_active_study",
        table_name="study_analytics_shares",
    )
    op.drop_index(
        "idx_study_analytics_shares_study_revoked",
        table_name="study_analytics_shares",
    )
    op.drop_index("ix_study_analytics_shares_token", table_name="study_analytics_shares")
    op.drop_index(
        "ix_study_analytics_shares_created_by_id",
        table_name="study_analytics_shares",
    )
    op.drop_index("ix_study_analytics_shares_study_id", table_name="study_analytics_shares")
    op.drop_index("ix_study_analytics_shares_id", table_name="study_analytics_shares")
    op.drop_table("study_analytics_shares")
