from __future__ import annotations

from typing import Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.billing_exceptions import PaymentRequired
from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.schemas.billing_schema import PlanType, UserBillingSummary
from app.services.billing import BillingService
from app.services.plan_limits import get_plan_limits, plan_meets_minimum


def get_billing_context(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> UserBillingSummary:
    service = BillingService(db)
    return service.get_billing_summary(current_user)


def require_plan_feature(feature: str, required_plan: PlanType = "pro"):
    def _dependency(billing: UserBillingSummary = Depends(get_billing_context)):
        limits = billing.limits
        allowed = {
            "share_study": limits.can_share_study,
            "analysis_export": limits.can_analysis_export,
            "basic_export": limits.can_basic_export,
        }.get(feature, True)

        if not allowed:
            raise PaymentRequired(
                code="plan_upgrade_required",
                message=f"This feature requires the {required_plan} plan or higher.",
                required_plan=required_plan,
                checkout_type="subscription",
            )
        return billing

    return _dependency


def require_minimum_plan(required_plan: PlanType):
    def _dependency(billing: UserBillingSummary = Depends(get_billing_context)):
        if not plan_meets_minimum(billing.plan, required_plan):
            raise PaymentRequired(
                code="plan_upgrade_required",
                message=f"This action requires the {required_plan} plan or higher.",
                required_plan=required_plan,
                checkout_type="subscription",
            )
        return billing

    return _dependency
