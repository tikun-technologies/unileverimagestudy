from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.user_model import User
from app.schemas.user_schema import OnboardingStatusResponse


def build_onboarding_status(user: Optional[User]) -> OnboardingStatusResponse:
    if not user:
        return OnboardingStatusResponse()

    dashboard_done = bool(user.dashboard_onboarding_completed or user.dashboard_onboarding_skipped)
    create_study_done = bool(
        user.create_study_onboarding_completed or user.create_study_onboarding_skipped
    )

    return OnboardingStatusResponse(
        onboarding_completed=dashboard_done,
        onboarding_skipped=bool(user.dashboard_onboarding_skipped),
        create_study_onboarding_completed=create_study_done,
        create_study_onboarding_skipped=bool(user.create_study_onboarding_skipped),
        show_dashboard_onboarding=not dashboard_done,
        show_create_study_onboarding=not create_study_done,
    )


def get_onboarding_status_for_user_id(db: Session, user_id: str) -> OnboardingStatusResponse:
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return OnboardingStatusResponse()

    user = (
        db.query(User)
        .filter(User.id == uid, User.is_active.is_(True))
        .first()
    )
    return build_onboarding_status(user)


def mark_dashboard_onboarding_complete(db: Session, user: User) -> User:
    user.dashboard_onboarding_completed = True
    user.dashboard_onboarding_skipped = False
    db.commit()
    db.refresh(user)
    return user


def mark_dashboard_onboarding_skipped(db: Session, user: User) -> User:
    user.dashboard_onboarding_skipped = True
    db.commit()
    db.refresh(user)
    return user


def mark_create_study_onboarding_complete(db: Session, user: User) -> User:
    user.create_study_onboarding_completed = True
    user.create_study_onboarding_skipped = False
    db.commit()
    db.refresh(user)
    return user


def mark_create_study_onboarding_skipped(db: Session, user: User) -> User:
    user.create_study_onboarding_skipped = True
    db.commit()
    db.refresh(user)
    return user


def reset_user_onboarding(db: Session, user: User) -> User:
    user.dashboard_onboarding_completed = False
    user.dashboard_onboarding_skipped = False
    user.create_study_onboarding_completed = False
    user.create_study_onboarding_skipped = False
    db.commit()
    db.refresh(user)
    return user
