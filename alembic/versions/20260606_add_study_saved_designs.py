"""add study saved designs

Revision ID: 20260606_saved_designs
Revises: 20260517_done_by_id
Create Date: 2026-06-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260606_saved_designs"
down_revision: Union[str, Sequence[str], None] = "20260517_done_by_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "study_saved_designs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("design_type", sa.String(length=30), nullable=False, server_default="configurator"),
        sa.Column(
            "study_type",
            postgresql.ENUM("grid", "layer", "text", "hybrid", name="study_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("segment_label", sa.String(length=255), nullable=True),
        sa.Column("selection_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_coefficient", sa.Float(), nullable=True),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("study_id", "design_type", "normalized_name", name="uq_study_saved_designs_study_type_name"),
    )
    op.create_index("ix_study_saved_designs_study_id", "study_saved_designs", ["study_id"], unique=False)
    op.create_index("ix_study_saved_designs_created_by_id", "study_saved_designs", ["created_by_id"], unique=False)
    op.create_index("idx_study_saved_designs_study_created", "study_saved_designs", ["study_id", "created_at"], unique=False)
    op.create_index("idx_study_saved_designs_study_type_created", "study_saved_designs", ["study_id", "design_type", "created_at"], unique=False)
    op.create_index("idx_study_saved_designs_study_updated", "study_saved_designs", ["study_id", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_study_saved_designs_study_updated", table_name="study_saved_designs")
    op.drop_index("idx_study_saved_designs_study_type_created", table_name="study_saved_designs")
    op.drop_index("idx_study_saved_designs_study_created", table_name="study_saved_designs")
    op.drop_index("ix_study_saved_designs_created_by_id", table_name="study_saved_designs")
    op.drop_index("ix_study_saved_designs_study_id", table_name="study_saved_designs")
    op.drop_table("study_saved_designs")
