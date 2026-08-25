"""add contact_inquiries table for landing Contact Us form

Revision ID: 20260825_contact_inquiries
Revises: 20260729_assistant_chat
Create Date: 2026-08-25

Safe for shared MindSurve DB: creates a Unilever-owned table only.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260825_contact_inquiries"
down_revision: Union[str, Sequence[str], None] = "20260729_assistant_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contact_inquiries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=50),
            nullable=False,
            server_default="landing",
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="new",
        ),
        sa.Column("email_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_contact_inquiries_id", "contact_inquiries", ["id"], unique=False)
    op.create_index("ix_contact_inquiries_email", "contact_inquiries", ["email"], unique=False)
    op.create_index(
        "idx_contact_inquiries_created_at",
        "contact_inquiries",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "idx_contact_inquiries_status_created",
        "contact_inquiries",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_contact_inquiries_status_created", table_name="contact_inquiries")
    op.drop_index("idx_contact_inquiries_created_at", table_name="contact_inquiries")
    op.drop_index("ix_contact_inquiries_email", table_name="contact_inquiries")
    op.drop_index("ix_contact_inquiries_id", table_name="contact_inquiries")
    op.drop_table("contact_inquiries")
