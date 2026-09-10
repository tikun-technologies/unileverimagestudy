"""Create, resolve, and revoke live analytics dashboard share links."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.study_model import StudyAnalyticsShare


def generate_analytics_share_token() -> str:
    return secrets.token_urlsafe(32)


def get_share_by_token(db: Session, token: str) -> Optional[StudyAnalyticsShare]:
    cleaned = (token or "").strip()
    if not cleaned:
        return None
    return (
        db.query(StudyAnalyticsShare)
        .filter(StudyAnalyticsShare.token == cleaned)
        .first()
    )


def get_active_share_for_study(db: Session, study_id: UUID) -> Optional[StudyAnalyticsShare]:
    return (
        db.query(StudyAnalyticsShare)
        .filter(
            StudyAnalyticsShare.study_id == study_id,
            StudyAnalyticsShare.revoked_at.is_(None),
        )
        .first()
    )


def create_or_get_active_share(
    db: Session,
    study_id: UUID,
    created_by_id: UUID,
) -> StudyAnalyticsShare:
    existing = get_active_share_for_study(db, study_id)
    if existing:
        return existing

    share = StudyAnalyticsShare(
        study_id=study_id,
        created_by_id=created_by_id,
        token=generate_analytics_share_token(),
        current_filters=None,
        revoked_at=None,
    )
    db.add(share)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_active_share_for_study(db, study_id)
        if existing:
            return existing
        raise
    db.refresh(share)
    return share


def revoke_active_share(db: Session, study_id: UUID) -> bool:
    """Revoke the active share for a study. Returns True if a share was revoked."""
    share = get_active_share_for_study(db, study_id)
    if not share:
        return False
    share.revoked_at = datetime.now(timezone.utc)
    share.current_filters = None
    db.add(share)
    db.commit()
    return True


def save_share_filters(
    db: Session,
    share: StudyAnalyticsShare,
    filters: Optional[dict],
) -> Optional[dict]:
    share.current_filters = filters or None
    db.add(share)
    db.commit()
    db.refresh(share)
    return share.current_filters


def clear_share_filters(db: Session, share: StudyAnalyticsShare) -> None:
    share.current_filters = None
    db.add(share)
    db.commit()
    db.refresh(share)
