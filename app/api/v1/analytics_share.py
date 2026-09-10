"""Owner create/status/revoke and public resolve for live analytics shares."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, defer

from app.core.analytics_access import SHARE_INVALID_DETAIL, SHARE_REVOKED_DETAIL
from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.study_model import Study, StudyMember
from app.models.user_model import User
from app.services import analytics_share as share_service
from sqlalchemy import select

router = APIRouter()


def _require_study_member(db: Session, study_id: UUID, current_user: User) -> Study:
    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")
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


def _share_status_payload(share, study: Study | None = None) -> dict:
    if not share:
        return {
            "is_shared": False,
            "token": None,
            "created_at": None,
            "study_id": str(study.id) if study else None,
            "title": study.title if study else None,
            "study_type": study.study_type if study else None,
        }
    return {
        "is_shared": share.revoked_at is None,
        "token": share.token if share.revoked_at is None else None,
        "created_at": share.created_at.isoformat() if share.created_at else None,
        "study_id": str(share.study_id),
        "title": study.title if study else None,
        "study_type": study.study_type if study else None,
    }


@router.get("/studies/{study_id}/analytics-share")
def get_study_analytics_share(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    study = _require_study_member(db, study_id, current_user)
    share = share_service.get_active_share_for_study(db, study_id)
    return _share_status_payload(share, study)


@router.post("/studies/{study_id}/analytics-share", status_code=status.HTTP_200_OK)
def create_study_analytics_share(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    study = _require_study_member(db, study_id, current_user)
    share = share_service.create_or_get_active_share(db, study_id, current_user.id)
    return _share_status_payload(share, study)


@router.delete("/studies/{study_id}/analytics-share")
def revoke_study_analytics_share(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    _require_study_member(db, study_id, current_user)
    revoked = share_service.revoke_active_share(db, study_id)
    return {
        "ok": True,
        "revoked": revoked,
        "message": "This shared dashboard has been revoked. All shared access was deleted."
        if revoked
        else "No active shared dashboard to revoke.",
    }


@router.get("/share/analytics/{token}")
def resolve_analytics_share(
    token: str,
    db: Session = Depends(get_db),
):
    share = share_service.get_share_by_token(db, token)
    if not share:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=SHARE_INVALID_DETAIL)
    if share.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARE_REVOKED_DETAIL)

    study = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == share.study_id)
        .first()
    )
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")

    return {
        "is_shared": True,
        "token": share.token,
        "study_id": str(study.id),
        "title": study.title,
        "study_type": study.study_type,
        "status": study.status,
        "created_at": share.created_at.isoformat() if share.created_at else None,
    }
