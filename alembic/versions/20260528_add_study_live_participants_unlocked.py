"""add live_participants_unlocked to studies

Revision ID: 20260528_live_unlocked
Revises: 20260527_billing
Create Date: 2026-05-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260528_live_unlocked"
down_revision: Union[str, Sequence[str], None] = "20260527_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "studies",
        sa.Column(
            "live_participants_unlocked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Backfill from existing successful study unlock payments
    op.execute(
        """
        UPDATE studies s
        SET live_participants_unlocked = true
        WHERE EXISTS (
            SELECT 1 FROM study_payments sp
            WHERE sp.study_id = s.id
              AND sp.status = 'succeeded'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("studies", "live_participants_unlocked")
