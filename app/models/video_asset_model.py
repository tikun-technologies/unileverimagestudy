from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class VideoAsset(Base):
    """Encode status for one uploaded video. Keyed by the raw blob name."""

    __tablename__ = "video_assets"

    public_id = Column(String(1024), primary_key=True)
    source_url = Column(Text, nullable=False)
    hls_url = Column(Text, nullable=True)
    poster_url = Column(Text, nullable=True)
    output_prefix = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, server_default="processing")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
