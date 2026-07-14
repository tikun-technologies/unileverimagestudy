# app/models/template_model.py
from __future__ import annotations

import uuid

from sqlalchemy import Column, String, DateTime, ForeignKey, Index, UniqueConstraint, Enum, Integer, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


template_status_enum = Enum("draft", "published", name="template_status_enum")


class LayerTemplate(Base):
    """
    Reusable layer-study template.
    `layer_json` mirrors the Step 5 / study_layers payload so templates can be
    applied into Create Study without conversion.
    """

    __tablename__ = "layer_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    title = Column(String(255), nullable=False)
    # Lowercased trimmed title for uniqueness checks
    normalized_title = Column(String(255), nullable=False)

    status = Column(template_status_enum, nullable=False, server_default="draft", index=True)
    aspect_ratio = Column(String(16), nullable=False)  # "9:16" | "16:9" | "1:1"

    # Full Step-5 compatible payload:
    # {
    #   "aspect_ratio": "9:16",
    #   "background_image_url": "...",
    #   "study_layers": [...],
    #   "design_constraints": [...]
    # }
    layer_json = Column(JSONB, nullable=False, server_default="{}")

    # Optional denormalized preview helpers (counts, default selection ids, etc.)
    preview_metadata = Column(JSONB, nullable=True)

    layer_count = Column(Integer, nullable=False, server_default="0")
    element_count = Column(Integer, nullable=False, server_default="0")

    created_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)

    creator = relationship("User", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("normalized_title", name="uq_layer_templates_normalized_title"),
        Index("idx_layer_templates_status_updated", "status", "updated_at"),
        Index("idx_layer_templates_created_by_updated", "created_by_id", "updated_at"),
        Index("idx_layer_templates_title", "title"),
    )
