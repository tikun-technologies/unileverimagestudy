"""Remember which user uploaded a video so encode progress can ride the jobs websocket.

Revision ID: 20261005_video_owner
Revises: 20261001_video_assets
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa


revision = "20261005_video_owner"
down_revision = "20261001_video_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("video_assets", sa.Column("owner_id", sa.String(length=36), nullable=True))
    op.create_index("ix_video_assets_owner_id", "video_assets", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_video_assets_owner_id", table_name="video_assets")
    op.drop_column("video_assets", "owner_id")
