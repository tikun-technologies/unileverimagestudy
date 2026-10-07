"""Screening-option quotas and reusable respondent numbers.

Revision ID: 20261006_option_quotas
Revises: 20261005_video_owner
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261006_option_quotas"
down_revision = "20261005_video_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "study_responses",
        sa.Column(
            "quota_reserved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_table(
        "screening_option_quotas",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", sa.String(length=10), nullable=False),
        sa.Column("option_id", sa.String(length=10), nullable=False),
        sa.Column("max_respondents", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "study_id",
            "question_id",
            "option_id",
            name="uq_screening_option_quota",
        ),
    )
    op.create_index(
        "idx_screening_option_quota_lookup",
        "screening_option_quotas",
        ["study_id", "question_id", "option_id"],
    )
    op.create_table(
        "freed_respondent_ids",
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("respondent_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("study_id", "respondent_id"),
    )


def downgrade() -> None:
    op.drop_table("freed_respondent_ids")
    op.drop_index("idx_screening_option_quota_lookup", table_name="screening_option_quotas")
    op.drop_table("screening_option_quotas")
    op.drop_column("study_responses", "quota_reserved")
