"""add study design constraints

Revision ID: 20260617_design_constraints
Revises: 20260609_user_onboarding
Create Date: 2026-06-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260617_design_constraints"
down_revision: Union[str, Sequence[str], None] = "20260609_user_onboarding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "studies", "design_constraints"):
        op.add_column(
            "studies",
            sa.Column("design_constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "studies", "design_constraints"):
        op.drop_column("studies", "design_constraints")
