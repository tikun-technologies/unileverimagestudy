"""Landing-page contact / study inquiry submissions."""

from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.base import Base


class ContactInquiry(Base):
    """Public Contact Us form submissions from the landing page."""

    __tablename__ = "contact_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(120), nullable=False)
    company = Column(String(120), nullable=True)
    email = Column(String(255), nullable=False, index=True)
    message = Column(Text, nullable=False)
    source = Column(String(50), nullable=False, server_default="landing")
    status = Column(String(30), nullable=False, server_default="new")
    # Reserved for future notification to a team inbox
    email_notified_at = Column(DateTime(timezone=True), nullable=True)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_contact_inquiries_created_at", "created_at"),
        Index("idx_contact_inquiries_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<ContactInquiry(id={self.id}, email={self.email!r})>"
