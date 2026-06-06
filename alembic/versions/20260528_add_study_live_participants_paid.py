"""add explicit study live participants paid flag

Revision ID: 20260528_live_paid
Revises: 20260528_live_unlocked
Create Date: 2026-05-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260528_live_paid"
down_revision: Union[str, Sequence[str], None] = "20260528_live_unlocked"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "studies",
        sa.Column(
            "live_participants_paid",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Only a successful per-study live-access payment means "$10 paid".
    op.execute(
        """
        UPDATE studies s
        SET live_participants_paid = true,
            live_participants_unlocked = true
        WHERE EXISTS (
            SELECT 1 FROM study_payments sp
            WHERE sp.study_id = s.id
              AND sp.status = 'succeeded'
              AND sp.audience_snapshot ->> 'payment_type' = 'live_access_unlock'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("studies", "live_participants_paid")
