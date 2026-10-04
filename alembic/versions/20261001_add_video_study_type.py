"""Add video to study and element type enums

Revision ID: 20261001_video_study
Revises: 20260924_design_categories
Create Date: 2026-10-01

"""
from alembic import op

revision = "20261001_video_study"
down_revision = "20260924_design_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL ADD VALUE cannot run inside a transaction on older versions.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE study_type_enum ADD VALUE IF NOT EXISTS 'video'")
        op.execute("ALTER TYPE element_type_enum ADD VALUE IF NOT EXISTS 'video'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value without recreating the type.
    pass
