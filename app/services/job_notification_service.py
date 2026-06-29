"""Helpers for global user-scoped job notifications."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.models.job_model import (
    DismissedJobNotification,
    Job,
    JobStatus,
    UserInvitationNotification,
    UserJobNotification,
)
from app.models.study_model import Study, StudyMember
from app.models.project_model import Project, ProjectMember

logger = logging.getLogger(__name__)

NOTIFICATION_CACHE_TTL = 8

ACTIVE_JOB_STATUSES = (
    JobStatus.PENDING,
    JobStatus.STARTED,
    JobStatus.PROCESSING,
)

TERMINAL_JOB_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
)

NOTIFICATION_KINDS = {"task_generation", "simulate_ai"}


def _notification_cache_key(user_id: UUID) -> str:
    return f"user_notifications:{user_id}"


def invalidate_user_notification_cache(user_id: UUID) -> None:
    RedisCache.delete(_notification_cache_key(user_id))


def infer_job_kind(job: Job) -> str:
    """Return task_generation, simulate_ai, or export."""
    try:
        result = job.result if isinstance(job.result, dict) else {}
        payload = result.get("payload") or {}
        if payload.get("job_type") == "simulate_ai_respondents":
            return "simulate_ai"
        if payload.get("job_type") == "export_project_zip":
            return "export"
    except Exception:
        pass
    return "task_generation"


def get_dismissed_job_ids(db: Session, user_id: UUID) -> set[str]:
    legacy = db.scalars(
        select(DismissedJobNotification.job_id).where(
            DismissedJobNotification.user_id == str(user_id)
        )
    ).all()
    persisted = db.scalars(
        select(UserJobNotification.job_id).where(
            UserJobNotification.user_id == str(user_id),
            UserJobNotification.dismissed_at.isnot(None),
        )
    ).all()
    return set(legacy) | set(persisted)


def is_job_notification_dismissed(db: Session, user_id: str, job_id: str) -> bool:
    """Return True when a user has permanently dismissed a job notification."""
    legacy = db.scalar(
        select(DismissedJobNotification.id).where(
            DismissedJobNotification.user_id == user_id,
            DismissedJobNotification.job_id == job_id,
        )
    )
    if legacy:
        return True

    persisted = db.scalar(
        select(UserJobNotification.id).where(
            UserJobNotification.user_id == user_id,
            UserJobNotification.job_id == job_id,
            UserJobNotification.dismissed_at.isnot(None),
        )
    )
    return persisted is not None


def dismiss_job_notification(db: Session, user_id: UUID, job_id: str) -> None:
    """Permanently dismiss a job notification for the user."""
    user_id_str = str(user_id)
    now = datetime.utcnow()

    existing = db.scalar(
        select(DismissedJobNotification).where(
            DismissedJobNotification.user_id == user_id_str,
            DismissedJobNotification.job_id == job_id,
        )
    )
    if not existing:
        db.add(DismissedJobNotification(user_id=user_id_str, job_id=job_id))

    row = db.scalar(
        select(UserJobNotification).where(
            UserJobNotification.user_id == user_id_str,
            UserJobNotification.job_id == job_id,
        )
    )
    if row:
        row.dismissed_at = now
        row.is_read = True
    else:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if job:
            upsert_user_job_notification(db, job, mark_read=True, dismissed=True)

    db.commit()
    invalidate_user_notification_cache(user_id)


def _accessible_study_ids(db: Session, user_id: UUID) -> set[str]:
    owned = db.scalars(select(Study.id).where(Study.creator_id == user_id)).all()
    member = db.scalars(
        select(StudyMember.study_id).where(StudyMember.user_id == user_id)
    ).all()
    return {str(sid) for sid in owned} | {str(sid) for sid in member}


def _study_title_map(db: Session, study_ids: set[str]) -> dict[str, str]:
    if not study_ids:
        return {}
    titles: dict[str, str] = {}
    for study in db.query(Study).filter(Study.id.in_(list(study_ids))).all():
        titles[str(study.id)] = study.title or "Untitled Study"
    return titles


def _serialize_job_notification_row(row: UserJobNotification) -> dict[str, Any]:
    return {
        "notification_type": "job",
        "notification_id": row.job_id,
        "job_id": row.job_id,
        "study_id": row.study_id,
        "study_title": row.study_title,
        "job_kind": row.job_kind,
        "status": row.status,
        "progress": float(row.progress or 0.0),
        "message": row.message,
        "error": row.error,
        "respondents_requested": row.respondents_requested,
        "respondents_completed": row.respondents_completed,
        "unread": not row.is_read,
        "is_read": row.is_read,
        "created_at": row.job_created_at.isoformat() if row.job_created_at else None,
        "started_at": None,
        "completed_at": row.job_completed_at.isoformat() if row.job_completed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _invitation_message(kind: str, inviter_name: str, title: str, role: str) -> str:
    inviter = inviter_name or "Someone"
    resource = title or ("Untitled Study" if kind == "study_invite" else "Untitled Project")
    noun = "study" if kind == "study_invite" else "project"
    return f"{inviter} has invited you to {noun} \"{resource}\" as {role}"


def _serialize_invitation_notification_row(row: UserInvitationNotification) -> dict[str, Any]:
    title = row.resource_title or (
        "Untitled Study" if row.notification_kind == "study_invite" else "Untitled Project"
    )
    return {
        "notification_type": row.notification_kind,
        "notification_id": str(row.id),
        "study_id": row.study_id,
        "project_id": row.project_id,
        "study_title": title if row.notification_kind == "study_invite" else None,
        "project_title": title if row.notification_kind == "project_invite" else None,
        "resource_title": title,
        "inviter_name": row.inviter_name,
        "role": row.role,
        "message": _invitation_message(
            row.notification_kind,
            row.inviter_name or "",
            title,
            row.role,
        ),
        "status": "completed",
        "progress": 100.0,
        "unread": not row.is_read,
        "is_read": row.is_read,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _publish_user_notification_event(user_id: str, envelope: dict[str, Any]) -> None:
    from app.core.redis import publish_user_job_update

    publish_user_job_update(user_id, envelope)


def create_study_invitation_notification(
    db: Session,
    *,
    user_id: UUID,
    study_id: UUID,
    study_title: str,
    inviter_name: str,
    role: str,
    member_id: UUID,
) -> Optional[UserInvitationNotification]:
    """Persist and push a study invitation notification for an existing user."""
    user_id_str = str(user_id)
    member_id_str = str(member_id)

    existing = db.scalar(
        select(UserInvitationNotification).where(
            UserInvitationNotification.user_id == user_id_str,
            UserInvitationNotification.member_id == member_id_str,
            UserInvitationNotification.dismissed_at.is_(None),
        )
    )
    if existing:
        row = existing
        row.resource_title = study_title
        row.inviter_name = inviter_name
        row.role = role
        row.is_read = False
    else:
        row = UserInvitationNotification(
            user_id=user_id_str,
            notification_kind="study_invite",
            study_id=str(study_id),
            project_id=None,
            resource_title=study_title,
            inviter_name=inviter_name,
            role=role,
            member_id=member_id_str,
            is_read=False,
        )
        db.add(row)

    db.commit()
    db.refresh(row)
    invalidate_user_notification_cache(user_id)

    envelope = _serialize_invitation_notification_row(row)
    envelope["event"] = "notification_update"
    _publish_user_notification_event(user_id_str, envelope)
    return row


def create_project_invitation_notification(
    db: Session,
    *,
    user_id: UUID,
    project_id: UUID,
    project_name: str,
    inviter_name: str,
    role: str,
    member_id: UUID,
) -> Optional[UserInvitationNotification]:
    """Persist and push a project invitation notification for an existing user."""
    user_id_str = str(user_id)
    member_id_str = str(member_id)

    existing = db.scalar(
        select(UserInvitationNotification).where(
            UserInvitationNotification.user_id == user_id_str,
            UserInvitationNotification.member_id == member_id_str,
            UserInvitationNotification.dismissed_at.is_(None),
        )
    )
    if existing:
        row = existing
        row.resource_title = project_name
        row.inviter_name = inviter_name
        row.role = role
        row.is_read = False
    else:
        row = UserInvitationNotification(
            user_id=user_id_str,
            notification_kind="project_invite",
            study_id=None,
            project_id=str(project_id),
            resource_title=project_name,
            inviter_name=inviter_name,
            role=role,
            member_id=member_id_str,
            is_read=False,
        )
        db.add(row)

    db.commit()
    db.refresh(row)
    invalidate_user_notification_cache(user_id)

    envelope = _serialize_invitation_notification_row(row)
    envelope["event"] = "notification_update"
    _publish_user_notification_event(user_id_str, envelope)
    return row


def sync_invitation_notifications_from_members(db: Session, user_id: UUID) -> None:
    """Backfill invitation notifications from linked study/project memberships."""
    user_id_str = str(user_id)

    study_members = db.scalars(
        select(StudyMember).where(
            StudyMember.user_id == user_id,
        )
    ).all()
    for member in study_members:
        existing = db.scalar(
            select(UserInvitationNotification.id).where(
                UserInvitationNotification.member_id == str(member.id)
            )
        )
        if existing:
            continue
        study = db.get(Study, member.study_id)
        if not study or study.creator_id == user_id:
            continue
        row = UserInvitationNotification(
            user_id=user_id_str,
            notification_kind="study_invite",
            study_id=str(member.study_id),
            resource_title=study.title or "Untitled Study",
            inviter_name=None,
            role=member.role,
            member_id=str(member.id),
            is_read=True,
        )
        db.add(row)

    project_members = db.scalars(
        select(ProjectMember).where(
            ProjectMember.user_id == user_id,
        )
    ).all()
    for member in project_members:
        existing = db.scalar(
            select(UserInvitationNotification.id).where(
                UserInvitationNotification.member_id == str(member.id)
            )
        )
        if existing:
            continue
        project = db.get(Project, member.project_id)
        if not project or project.creator_id == user_id:
            continue
        row = UserInvitationNotification(
            user_id=user_id_str,
            notification_kind="project_invite",
            project_id=str(member.project_id),
            resource_title=project.name or "Untitled Project",
            inviter_name=None,
            role=member.role,
            member_id=str(member.id),
            is_read=True,
        )
        db.add(row)

    try:
        db.commit()
    except Exception:
        db.rollback()


def _serialize_notification_row(row: UserJobNotification) -> dict[str, Any]:
    return _serialize_job_notification_row(row)


def _extract_respondent_counts(job: Job, kind: str) -> tuple[Optional[int], Optional[int]]:
    respondents_requested = 0
    try:
        result = job.result if isinstance(job.result, dict) else {}
        payload = result.get("payload") or {}
        respondents_requested = (
            payload.get("max_respondents")
            or payload.get("respondents_requested")
            or 0
        )
    except Exception:
        pass

    progress = float(job.progress or 0.0)
    if kind != "simulate_ai":
        return None, None
    if not respondents_requested:
        return None, None
    respondents_completed = int((progress / 100.0) * respondents_requested)
    return int(respondents_requested), respondents_completed


def upsert_user_job_notification(
    db: Session,
    job: Job,
    *,
    study_title: Optional[str] = None,
    mark_read: Optional[bool] = None,
    dismissed: bool = False,
    commit: bool = True,
) -> Optional[UserJobNotification]:
    """Create or update a persisted notification row for the job owner."""
    kind = infer_job_kind(job)
    if kind not in NOTIFICATION_KINDS:
        return None

    user_id_str = job.user_id
    if study_title is None and job.study_id:
        study = db.query(Study).filter(Study.id == job.study_id).first()
        study_title = (study.title if study else None) or "Untitled Study"

    requested, completed = _extract_respondent_counts(job, kind)
    status_val = job.status.value if isinstance(job.status, JobStatus) else str(job.status)

    row = db.scalar(
        select(UserJobNotification).where(
            UserJobNotification.user_id == user_id_str,
            UserJobNotification.job_id == job.job_id,
        )
    )

    prev_status = row.status if row else None
    became_terminal = status_val in {s.value for s in TERMINAL_JOB_STATUSES} and (
        prev_status not in {s.value for s in TERMINAL_JOB_STATUSES}
    )

    if row:
        row.study_id = job.study_id
        row.study_title = study_title
        if row.job_kind == "task_generation" or kind != "task_generation":
            row.job_kind = kind
        row.status = status_val
        row.progress = float(job.progress or 0.0)
        row.message = job.message
        row.error = job.error
        row.respondents_requested = requested
        row.respondents_completed = completed
        row.job_completed_at = job.completed_at
        if mark_read is not None:
            row.is_read = mark_read
        elif became_terminal:
            row.is_read = False
        if dismissed:
            row.dismissed_at = datetime.utcnow()
            row.is_read = True
    else:
        row = UserJobNotification(
            user_id=user_id_str,
            job_id=job.job_id,
            study_id=job.study_id,
            study_title=study_title,
            job_kind=kind,
            status=status_val,
            progress=float(job.progress or 0.0),
            message=job.message,
            error=job.error,
            respondents_requested=requested,
            respondents_completed=completed,
            is_read=mark_read if mark_read is not None else False,
            dismissed_at=datetime.utcnow() if dismissed else None,
            job_created_at=job.created_at,
            job_completed_at=job.completed_at,
        )
        db.add(row)

    if commit:
        try:
            db.commit()
            db.refresh(row)
            invalidate_user_notification_cache(UUID(user_id_str))
        except Exception:
            db.rollback()
            raise

    return row


def sync_user_notifications_from_jobs(db: Session, user_id: UUID) -> None:
    """Backfill notification rows from jobs table (cross-device bootstrap)."""
    user_id_str = str(user_id)
    dismissed = get_dismissed_job_ids(db, user_id)
    accessible = _accessible_study_ids(db, user_id)

    filters = [Job.user_id == user_id_str]
    if accessible:
        filters.append(Job.study_id.in_(accessible))

    jobs = (
        db.query(Job)
        .filter(or_(*filters))
        .filter(Job.status.in_((*ACTIVE_JOB_STATUSES, *TERMINAL_JOB_STATUSES)))
        .order_by(Job.created_at.desc())
        .limit(50)
        .all()
    )

    if not jobs:
        return

    titles = _study_title_map(db, {j.study_id for j in jobs if j.study_id})
    for job in jobs:
        if job.job_id in dismissed:
            continue
        kind = infer_job_kind(job)
        if kind not in NOTIFICATION_KINDS:
            continue
        existing = db.scalar(
            select(UserJobNotification).where(
                UserJobNotification.user_id == user_id_str,
                UserJobNotification.job_id == job.job_id,
            )
        )
        if existing and existing.dismissed_at is not None:
            continue
        upsert_user_job_notification(
            db,
            job,
            study_title=titles.get(job.study_id),
            commit=False,
        )

    try:
        db.commit()
        invalidate_user_notification_cache(user_id)
    except Exception:
        db.rollback()


def get_user_notifications(
    db: Session,
    user_id: UUID,
    *,
    active_only: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return persisted notifications with unread count (Redis-cached)."""
    cache_key = _notification_cache_key(user_id)
    if use_cache:
        cached = RedisCache.get(cache_key)
        if isinstance(cached, dict) and "notifications" in cached:
            notifications = cached["notifications"]
            if active_only:
                notifications = [
                    n for n in notifications
                    if n.get("status") in {s.value for s in ACTIVE_JOB_STATUSES}
                ]
            return {
                "notifications": notifications,
                "jobs": notifications,
                "unread_count": cached.get("unread_count", 0),
            }

    sync_user_notifications_from_jobs(db, user_id)
    sync_invitation_notifications_from_members(db, user_id)

    job_rows = db.scalars(
        select(UserJobNotification)
        .where(
            UserJobNotification.user_id == str(user_id),
            UserJobNotification.dismissed_at.is_(None),
        )
        .order_by(UserJobNotification.updated_at.desc())
        .limit(50)
    ).all()

    invite_rows = db.scalars(
        select(UserInvitationNotification)
        .where(
            UserInvitationNotification.user_id == str(user_id),
            UserInvitationNotification.dismissed_at.is_(None),
        )
        .order_by(UserInvitationNotification.updated_at.desc())
        .limit(50)
    ).all()

    notifications = [_serialize_job_notification_row(row) for row in job_rows]
    notifications.extend(_serialize_invitation_notification_row(row) for row in invite_rows)

    if active_only:
        notifications = [
            n for n in notifications
            if n.get("notification_type") == "job"
            and n.get("status") in {s.value for s in ACTIVE_JOB_STATUSES}
        ]

    notifications.sort(
        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        reverse=True,
    )
    notifications = notifications[:50]
    unread_count = sum(1 for n in notifications if n.get("unread"))

    payload = {
        "notifications": notifications,
        "jobs": notifications,
        "unread_count": unread_count,
    }
    RedisCache.set(cache_key, payload, ttl_seconds=NOTIFICATION_CACHE_TTL)

    if active_only:
        active = [
            n for n in notifications if n.get("status") in {s.value for s in ACTIVE_JOB_STATUSES}
        ]
        return {
            "notifications": active,
            "jobs": active,
            "unread_count": unread_count,
        }

    return payload


def mark_notification_read(db: Session, user_id: UUID, notification_id: str) -> None:
    user_id_str = str(user_id)

    job_row = db.scalar(
        select(UserJobNotification).where(
            UserJobNotification.user_id == user_id_str,
            UserJobNotification.job_id == notification_id,
            UserJobNotification.dismissed_at.is_(None),
        )
    )
    if job_row:
        job_row.is_read = True
        db.commit()
        invalidate_user_notification_cache(user_id)
        return

    invite_row = None
    try:
        invite_uuid = UUID(notification_id)
    except ValueError:
        invite_uuid = None
    if invite_uuid:
        invite_row = db.scalar(
            select(UserInvitationNotification).where(
                UserInvitationNotification.user_id == user_id_str,
                UserInvitationNotification.id == invite_uuid,
                UserInvitationNotification.dismissed_at.is_(None),
            )
        )
    if invite_row:
        invite_row.is_read = True
        db.commit()
        invalidate_user_notification_cache(user_id)


def mark_all_notifications_read(db: Session, user_id: UUID) -> None:
    user_id_str = str(user_id)
    changed = False

    job_rows = db.scalars(
        select(UserJobNotification).where(
            UserJobNotification.user_id == user_id_str,
            UserJobNotification.dismissed_at.is_(None),
            UserJobNotification.is_read.is_(False),
        )
    ).all()
    for row in job_rows:
        row.is_read = True
        changed = True

    invite_rows = db.scalars(
        select(UserInvitationNotification).where(
            UserInvitationNotification.user_id == user_id_str,
            UserInvitationNotification.dismissed_at.is_(None),
            UserInvitationNotification.is_read.is_(False),
        )
    ).all()
    for row in invite_rows:
        row.is_read = True
        changed = True

    if not changed:
        return
    db.commit()
    invalidate_user_notification_cache(user_id)


def dismiss_notification(db: Session, user_id: UUID, notification_id: str) -> None:
    """Dismiss a job or invitation notification permanently."""
    user_id_str = str(user_id)
    now = datetime.utcnow()

    job_row = db.scalar(
        select(UserJobNotification).where(
            UserJobNotification.user_id == user_id_str,
            UserJobNotification.job_id == notification_id,
        )
    )
    if job_row:
        dismiss_job_notification(db, user_id, notification_id)
        return

    invite_row = None
    try:
        invite_uuid = UUID(notification_id)
    except ValueError:
        invite_uuid = None
    if invite_uuid:
        invite_row = db.scalar(
            select(UserInvitationNotification).where(
                UserInvitationNotification.user_id == user_id_str,
                UserInvitationNotification.id == invite_uuid,
            )
        )
    if invite_row:
        invite_row.dismissed_at = now
        invite_row.is_read = True
        db.commit()
        invalidate_user_notification_cache(user_id)


def serialize_job(job: Job, study_title: Optional[str] = None) -> dict[str, Any]:
    kind = infer_job_kind(job)
    requested, completed = _extract_respondent_counts(job, kind)

    return {
        "job_id": job.job_id,
        "study_id": job.study_id,
        "study_title": study_title,
        "job_kind": kind,
        "status": job.status.value,
        "progress": float(job.progress or 0.0),
        "message": job.message,
        "error": job.error,
        "respondents_requested": requested,
        "respondents_completed": completed,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def get_jobs_for_user(
    db: Session,
    user_id: UUID,
    *,
    active_only: bool = True,
    include_kinds: Optional[set[str]] = None,
    dismissed_job_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Jobs visible to a user — served from persisted notifications for cross-device sync."""
    include_kinds = include_kinds or NOTIFICATION_KINDS
    payload = get_user_notifications(db, user_id, active_only=active_only, use_cache=True)
    notifications = payload.get("notifications") or payload.get("jobs") or []

    filtered: list[dict[str, Any]] = []
    for item in notifications:
        if item.get("notification_type") != "job":
            continue
        kind = item.get("job_kind")
        if kind not in include_kinds:
            continue
        filtered.append(item)
    return filtered


def build_user_job_envelope(
    job: Job,
    data: dict[str, Any],
    study_title: Optional[str] = None,
) -> dict[str, Any]:
    """Merge progress payload with job metadata for user channel."""
    kind = infer_job_kind(job)
    base = serialize_job(job, study_title)
    envelope: dict[str, Any] = {
        "event": "job_update",
        **base,
        **data,
    }
    if kind == "simulate_ai":
        progress_val = float(data.get("progress", base.get("progress", 0)) or 0)
        requested = base.get("respondents_requested") or 0
        if requested and "respondents_completed" not in data:
            envelope["respondents_completed"] = int((progress_val / 100.0) * requested)
        if requested and "respondents_requested" not in data:
            envelope["respondents_requested"] = requested
    if data.get("type") == "completed":
        envelope["status"] = "completed"
        envelope["progress"] = 100.0
    elif data.get("type") == "failed":
        envelope["status"] = "failed"
    elif data.get("type") == "progress":
        envelope["status"] = "processing"
    return envelope


def send_task_generation_complete_email_for_job(
    db: Session,
    *,
    user_id: str,
    study_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Email the job owner when task generation completes. Failures are logged, not raised."""
    from app.core.config import settings
    from app.models.user_model import User
    from app.services.email_service import email_service

    try:
        user = db.query(User).filter(User.id == UUID(user_id)).first()
        if not user or not user.email:
            logger.warning("Task generation email skipped: user %s not found or has no email", user_id)
            return

        study = db.query(Study).filter(Study.id == UUID(study_id)).first()
        study_title = (study.title if study else None) or "Your study"

        respondents_count = None
        if payload:
            audience = payload.get("audience_segmentation") or {}
            if isinstance(audience, dict):
                respondents_count = audience.get("number_of_respondents")
            if not respondents_count:
                respondents_count = payload.get("number_of_respondents")

        study_url = f"{settings.FRONTEND_URL.rstrip('/')}/home/create-study?study_id={study_id}"
        user_name = user.name or user.email.split("@")[0]

        sent = email_service.send_task_generation_complete_email(
            to_email=user.email,
            user_name=user_name,
            study_title=study_title,
            study_url=study_url,
            respondents_count=int(respondents_count) if respondents_count else None,
        )
        if sent:
            logger.info("Task generation complete email sent to %s for study %s", user.email, study_id)
        else:
            logger.warning("Failed to send task generation complete email to %s", user.email)
    except Exception as e:
        logger.warning("Task generation complete email failed for study %s: %s", study_id, e)
