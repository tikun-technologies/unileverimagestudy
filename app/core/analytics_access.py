"""Dual auth for analytics: JWT owner/member or a live share token."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.core.dependencies import get_optional_current_active_user
from app.db.session import get_db
from app.models.study_model import Study, StudyAnalyticsShare, StudyMember
from app.models.user_model import User
from app.services import analytics_share as share_service
from app.services.user import get_user_by_id

ANALYTICS_SHARE_HEADER = "X-Analytics-Share-Token"
SHARE_REVOKED_DETAIL = "This shared dashboard has been revoked. All shared access was deleted."
SHARE_INVALID_DETAIL = "This share link is invalid or has expired."
SHARE_VIEW_ONLY_DETAIL = "Shared dashboards are view-only for this action."
SHARE_STUDY_MISMATCH_DETAIL = "This share link does not match this study."


@dataclass
class AnalyticsAccess:
    study: Study
    user: Optional[User]
    share: Optional[StudyAnalyticsShare]

    @property
    def is_share(self) -> bool:
        return self.share is not None


def load_study_for_analysis(db: Session, study_id: UUID) -> Study:
    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")
    return study_obj


def authorize_study_membership(db: Session, study_id: UUID, current_user: User) -> Study:
    study_obj = load_study_for_analysis(db, study_id)
    if study_obj.creator_id == current_user.id:
        return study_obj
    member = db.scalar(
        select(StudyMember).where(
            StudyMember.study_id == study_id,
            StudyMember.user_id == current_user.id,
        )
    )
    if not member:
        raise HTTPException(status_code=403, detail="Access denied")
    return study_obj


def analysis_viewer_email(db: Session, access: AnalyticsAccess) -> str:
    if access.user and access.user.email:
        return access.user.email
    creator = get_user_by_id(db, access.study.creator_id)
    return (creator.email if creator else "") or ""


def get_analytics_access(
    study_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_active_user),
) -> AnalyticsAccess:
    token = (request.headers.get(ANALYTICS_SHARE_HEADER) or "").strip()
    if token:
        share = share_service.get_share_by_token(db, token)
        if not share:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=SHARE_INVALID_DETAIL)
        if share.revoked_at is not None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARE_REVOKED_DETAIL)
        if share.study_id != study_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARE_STUDY_MISMATCH_DETAIL)
        study_obj = load_study_for_analysis(db, study_id)
        return AnalyticsAccess(study=study_obj, user=None, share=share)

    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    study_obj = authorize_study_membership(db, study_id, current_user)
    return AnalyticsAccess(study=study_obj, user=current_user, share=None)


def require_analytics_owner(
    access: AnalyticsAccess = Depends(get_analytics_access),
) -> AnalyticsAccess:
    if access.is_share:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARE_VIEW_ONLY_DETAIL)
    return access
