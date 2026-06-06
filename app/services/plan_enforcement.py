from __future__ import annotations

from typing import Any, List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.billing_exceptions import LiveStudyAccessBlocked, PaymentRequired
from app.models.study_model import Study
from app.models.user_model import User
from app.schemas.billing_schema import PlanType, UserBillingSummary
from app.services.billing import BillingService
from app.services.plan_limits import (
    count_ai_respondents_used,
    get_plan_limits,
    validate_structure_limits,
)


def enforce_structure_limits(
    billing: UserBillingSummary,
    *,
    study_type: str,
    categories: Optional[List[Any]] = None,
    elements: Optional[List[Any]] = None,
    study_layers: Optional[List[Any]] = None,
) -> None:
    try:
        validate_structure_limits(
            plan=billing.plan,
            study_type=study_type,
            categories=categories,
            elements=elements,
            study_layers=study_layers,
        )
    except ValueError as exc:
        required_plan: PlanType = "pro" if billing.plan == "free" else "enterprise"
        raise PaymentRequired(
            code="plan_limit_exceeded",
            message=str(exc),
            required_plan=required_plan,
            checkout_type="subscription",
        )


def enforce_ai_respondent_limit(
    db: Session,
    user_id: UUID,
    billing: UserBillingSummary,
    requested: int,
) -> None:
    limits = get_plan_limits(billing.plan)
    if requested <= 0:
        raise HTTPException(status_code=400, detail="Invalid respondent count.")
    used = count_ai_respondents_used(db, user_id)
    if used + requested > limits.ai_respondent_limit:
        required_plan: PlanType = "pro" if billing.plan == "free" else "enterprise"
        raise PaymentRequired(
            code="ai_respondent_limit_exceeded",
            message=(
                f"Plan '{billing.plan}' allows {limits.ai_respondent_limit} AI respondents total; "
                f"used {used}, requested {requested}."
            ),
            required_plan=required_plan,
            checkout_type="subscription",
        )


def enforce_share_study(billing: UserBillingSummary) -> None:
    if not billing.limits.can_share_study:
        raise PaymentRequired(
            code="plan_upgrade_required",
            message="Sharing studies with team members requires the Pro plan or higher.",
            required_plan="pro",
            checkout_type="subscription",
        )


def enforce_analysis_export(billing: UserBillingSummary) -> None:
    if not billing.limits.can_analysis_export:
        raise PaymentRequired(
            code="plan_upgrade_required",
            message="Analysis export requires the Pro plan or higher.",
            required_plan="pro",
            checkout_type="subscription",
        )


def enforce_live_participant_access(db: Session, study: Study) -> None:
    """
    Block live participants when a Free-plan creator has not paid the flat unlock fee.
    Launch is always allowed; this gate runs when someone opens the shared study link.
    """
    billing_service = BillingService(db)
    if billing_service.live_participants_allowed(study):
        return
    raise LiveStudyAccessBlocked(study_id=str(study.id))


def enforce_live_participant_access_by_study_id(db: Session, study_id: UUID) -> Study:
    study = db.scalar(select(Study).where(Study.id == study_id))
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")
    enforce_live_participant_access(db, study)
    return study
