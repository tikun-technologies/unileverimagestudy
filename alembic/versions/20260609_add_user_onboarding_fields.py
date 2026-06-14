"""add user onboarding fields

Revision ID: 20260609_user_onboarding
Revises: 20260607_saved_designs_type
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260609_user_onboarding"
down_revision: Union[str, Sequence[str], None] = "20260607_saved_designs_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    for column in (
        "dashboard_onboarding_completed",
        "dashboard_onboarding_skipped",
        "create_study_onboarding_completed",
        "create_study_onboarding_skipped",
    ):
        if not _column_exists(bind, "users", column):
            op.add_column(
                "users",
                sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
            )


def downgrade() -> None:
    bind = op.get_bind()
    for column in (
        "create_study_onboarding_skipped",
        "create_study_onboarding_completed",
        "dashboard_onboarding_skipped",
        "dashboard_onboarding_completed",
    ):
        if _column_exists(bind, "users", column):
            op.drop_column("users", column)
