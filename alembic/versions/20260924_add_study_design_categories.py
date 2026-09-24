"""add study design categories for configurator combinations

Revision ID: 20260924_design_categories
Revises: 20260908_analytics_shares
Create Date: 2026-09-24

Categories are per study. Items point at saved designs so the list query
never loads configuration JSONB.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260924_design_categories"
down_revision: Union[str, Sequence[str], None] = "20260908_analytics_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_design_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("study_id", "normalized_name", name="uq_study_design_categories_study_name"),
    )
    op.create_index("ix_study_design_categories_id", "study_design_categories", ["id"])
    op.create_index("ix_study_design_categories_study_id", "study_design_categories", ["study_id"])
    op.create_index("ix_study_design_categories_created_by_id", "study_design_categories", ["created_by_id"])
    op.create_index(
        "idx_study_design_categories_study_position",
        "study_design_categories",
        ["study_id", "position", "created_at"],
    )

    op.create_table(
        "study_design_category_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("saved_design_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["study_design_categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["saved_design_id"], ["study_saved_designs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category_id", "saved_design_id", name="uq_study_design_category_items_design"),
    )
    op.create_index("ix_study_design_category_items_id", "study_design_category_items", ["id"])
    op.create_index("ix_study_design_category_items_category_id", "study_design_category_items", ["category_id"])
    op.create_index("ix_study_design_category_items_saved_design_id", "study_design_category_items", ["saved_design_id"])
    op.create_index(
        "idx_study_design_category_items_category_position",
        "study_design_category_items",
        ["category_id", "position", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_study_design_category_items_category_position", table_name="study_design_category_items")
    op.drop_index("ix_study_design_category_items_saved_design_id", table_name="study_design_category_items")
    op.drop_index("ix_study_design_category_items_category_id", table_name="study_design_category_items")
    op.drop_index("ix_study_design_category_items_id", table_name="study_design_category_items")
    op.drop_table("study_design_category_items")

    op.drop_index("idx_study_design_categories_study_position", table_name="study_design_categories")
    op.drop_index("ix_study_design_categories_created_by_id", table_name="study_design_categories")
    op.drop_index("ix_study_design_categories_study_id", table_name="study_design_categories")
    op.drop_index("ix_study_design_categories_id", table_name="study_design_categories")
    op.drop_table("study_design_categories")
