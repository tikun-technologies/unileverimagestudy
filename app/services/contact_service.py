"""Persist landing-page Contact Us inquiries (single INSERT, no read-back)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.contact_inquiry_model import ContactInquiry
from app.schemas.contact_schema import ContactInquiryCreate


def create_contact_inquiry(
    db: Session,
    data: ContactInquiryCreate,
    *,
    client_ip: str | None = None,
) -> uuid.UUID | None:
    """
    Save a contact inquiry with one DB round-trip (INSERT + commit).

    Returns the new row id, or None when the honeypot field is filled.
    """
    if data.website:
        return None

    row_id = uuid.uuid4()
    row = ContactInquiry(
        id=row_id,
        name=data.name,
        company=data.company,
        email=str(data.email).lower(),
        message=data.message,
        source=(data.source or "landing")[:50],
        status="new",
        client_ip=client_ip,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    # No refresh/SELECT — id and created_at are set in-process.
    return row_id
