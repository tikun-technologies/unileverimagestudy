from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PlanType = Literal["free", "pro", "enterprise"]
SubscriptionStatus = Literal["none", "active", "trialing", "past_due", "canceled", "incomplete"]
StudyPaymentStatus = Literal["pending", "succeeded", "failed", "expired", "canceled"]


class PlanLimitsOut(BaseModel):
    max_categories: int
    max_elements_per_category: int
    max_layers: int
    max_images_per_layer: int
    ai_respondent_limit: int
    can_share_study: bool
    can_analysis_export: bool
    can_basic_export: bool = True


class PlanPricingOut(BaseModel):
    plan: PlanType
    monthly_fee_cents: int
    platform_base_fee_cents: int
    currency: str
    limits: PlanLimitsOut
    features: List[str]


class BillingPlansResponse(BaseModel):
    plans: List[PlanPricingOut]
    currency: str


class BillingUsageOut(BaseModel):
    ai_respondents_used: int = 0
    studies_created: int = 0


class BillingStatusOut(BaseModel):
    plan: PlanType
    subscription_status: SubscriptionStatus
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    limits: PlanLimitsOut
    usage: BillingUsageOut
    stripe_customer_id: Optional[str] = None
    has_active_subscription: bool = False


class UserBillingSummary(BaseModel):
    plan: PlanType = "free"
    subscription_status: SubscriptionStatus = "none"
    limits: PlanLimitsOut
    has_active_subscription: bool = False


class SubscriptionCheckoutRequest(BaseModel):
    success_url: str = Field(..., description="URL to redirect after successful checkout")
    cancel_url: str = Field(..., description="URL to redirect if checkout is cancelled")


class SubscriptionCheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class StudyCheckoutRequest(BaseModel):
    study_id: UUID
    success_url: str
    cancel_url: str


class StudyQuoteOut(BaseModel):
    study_id: UUID
    respondent_count: int
    cint_cpi_cents: float = 0.0  # legacy field; Cint panel is paid to Cint, not MindSurve
    platform_fee_cents: int
    panel_cost_cents: int = 0
    total_cents: int
    currency: str
    quote_hash: str
    audience_snapshot: Dict[str, Any]


class StudyCheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
    payment_id: UUID
    quote: StudyQuoteOut


class BillingPortalRequest(BaseModel):
    return_url: str


class BillingPortalResponse(BaseModel):
    portal_url: str


class StudyLiveAccessOut(BaseModel):
    study_id: UUID
    live_participants_allowed: bool
    live_participants_paid: bool
    live_participants_included_by_plan: bool
    live_participants_unlocked: bool
    requires_payment: bool
    amount_cents: int
    plan: PlanType
    currency: str
    unlock_source: Literal["none", "paid", "plan", "ai_only"] = "none"


class PaymentRequiredDetail(BaseModel):
    code: str
    message: str
    required_plan: Optional[PlanType] = None
    checkout_type: Optional[Literal["subscription", "study"]] = None
    checkout_payload: Optional[Dict[str, Any]] = None
