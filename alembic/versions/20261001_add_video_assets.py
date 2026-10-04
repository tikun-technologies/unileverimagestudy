"""Track video encode status

Revision ID: 20261001_video_assets
Revises: 20261001_video_study
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa


revision = "20261001_video_assets"
down_revision = "20261001_video_study"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_assets",
        sa.Column("public_id", sa.String(length=1024), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("hls_url", sa.Text(), nullable=True),
        sa.Column("poster_url", sa.Text(), nullable=True),
        sa.Column("output_prefix", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="processing", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("public_id"),
    )


def downgrade() -> None:
    op.drop_table("video_assets")
