from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing_model import BillingEvent, StudyPayment, UserBillingProfile
from app.models.study_model import Study
from app.models.user_model import User
from app.schemas.billing_schema import (
    BillingPlansResponse,
    BillingPortalResponse,
    BillingStatusOut,
    BillingUsageOut,
    PlanPricingOut,
    StudyCheckoutResponse,
    StudyQuoteOut,
    SubscriptionCheckoutResponse,
    UserBillingSummary,
)
from app.services.study_unlock_fee import calculate_live_study_access_fee
from app.services.plan_limits import (
    build_study_live_access_quote_hash,
    count_ai_respondents_used,
    count_user_studies,
    effective_plan,
    get_plan_limits,
    normalize_audience_segmentation,
    study_uses_live_participants,
    utcnow,
)

logger = logging.getLogger(__name__)


def _configure_stripe() -> None:
    if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY


class BillingService:
    def __init__(self, db: Session):
        self.db = db
        _configure_stripe()

    def get_or_create_profile(self, user: User) -> UserBillingProfile:
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.user_id == user.id)
        )
        if profile:
            return profile
        profile = UserBillingProfile(user_id=user.id, plan="free", subscription_status="none")
        self.db.add(profile)
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def get_billing_summary(self, user: User) -> UserBillingSummary:
        profile = self.get_or_create_profile(user)
        plan = effective_plan(profile)
        return UserBillingSummary(
            plan=plan,
            subscription_status=profile.subscription_status,  # type: ignore[arg-type]
            limits=get_plan_limits(plan),
            has_active_subscription=profile.subscription_status in ("active", "trialing"),
        )

    def get_plans_catalog(self) -> BillingPlansResponse:
        currency = settings.BILLING_CURRENCY.lower()
        plans: List[PlanPricingOut] = [
            PlanPricingOut(
                plan="free",
                monthly_fee_cents=0,
                platform_base_fee_cents=settings.PLATFORM_BASE_FEE_CENTS,
                currency=currency,
                limits=get_plan_limits("free"),
                features=[
                    "Basic export report",
                    "50 AI respondents",
                    "$10 per study to open live participant link (Cint panel paid separately)",
                ],
            ),
            PlanPricingOut(
                plan="pro",
                monthly_fee_cents=settings.PRO_MONTHLY_FEE_CENTS,
                platform_base_fee_cents=0 if settings.PRO_WAIVE_PLATFORM_FEE else settings.PLATFORM_BASE_FEE_CENTS,
                currency=currency,
                limits=get_plan_limits("pro"),
                features=[
                    "Share study",
                    "Analysis export",
                    "Higher AI respondent limits",
                    "Priority email support",
                ],
            ),
            PlanPricingOut(
                plan="enterprise",
                monthly_fee_cents=0,
                platform_base_fee_cents=0,
                currency=currency,
                limits=get_plan_limits("enterprise"),
                features=["Custom limits", "SSO", "Dedicated support", "Custom exports"],
            ),
        ]
        return BillingPlansResponse(plans=plans, currency=currency)

    def get_status(self, user: User) -> BillingStatusOut:
        profile = self.get_or_create_profile(user)
        plan = effective_plan(profile)
        return BillingStatusOut(
            plan=plan,
            subscription_status=profile.subscription_status,  # type: ignore[arg-type]
            current_period_start=profile.current_period_start,
            current_period_end=profile.current_period_end,
            limits=get_plan_limits(plan),
            usage=BillingUsageOut(
                ai_respondents_used=count_ai_respondents_used(self.db, user.id),
                studies_created=count_user_studies(self.db, user.id),
            ),
            stripe_customer_id=profile.stripe_customer_id,
            has_active_subscription=profile.subscription_status in ("active", "trialing"),
        )

    def _ensure_stripe_customer(self, user: User, profile: UserBillingProfile) -> str:
        if profile.stripe_customer_id:
            return profile.stripe_customer_id
        if not settings.STRIPE_SECRET_KEY:
            raise ValueError("Stripe is not configured.")
        customer = stripe.Customer.create(
            email=user.email,
            name=user.name,
            metadata={"user_id": str(user.id)},
        )
        profile.stripe_customer_id = customer.id
        self.db.commit()
        self.db.refresh(profile)
        return customer.id

    def create_subscription_checkout(
        self, user: User, *, success_url: str, cancel_url: str
    ) -> SubscriptionCheckoutResponse:
        if not settings.STRIPE_PRO_PRICE_ID:
            raise ValueError("Pro subscription price is not configured.")
        profile = self.get_or_create_profile(user)
        customer_id = self._ensure_stripe_customer(user, profile)
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": settings.STRIPE_PRO_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": str(user.id), "checkout_type": "subscription", "plan": "pro"},
        )
        return SubscriptionCheckoutResponse(checkout_url=session.url, session_id=session.id)

    def create_study_checkout(
        self, user: User, *, study_id: UUID, success_url: str, cancel_url: str
    ) -> StudyCheckoutResponse:
        study = self.db.scalar(
            select(Study).where(Study.id == study_id, Study.creator_id == user.id)
        )
        if not study:
            raise ValueError("Study not found or access denied.")
        if not study_uses_live_participants(study.audience_segmentation):
            raise ValueError("AI-only studies do not need a live participant unlock payment.")

        profile = self.get_or_create_profile(user)
        plan = effective_plan(profile)
        if plan in ("pro", "enterprise"):
            raise ValueError("Live participant access is included in your plan. No payment required.")

        pricing = calculate_live_study_access_fee(plan)
        if pricing["total_cents"] <= 0:
            raise ValueError("No payment required for this study.")

        audience = normalize_audience_segmentation(study.audience_segmentation)
        respondent_count = int(audience.get("number_of_respondents") or 0)
        quote_hash = build_study_live_access_quote_hash(study_id)

        existing = self.db.scalar(
            select(StudyPayment).where(
                StudyPayment.study_id == study_id,
                StudyPayment.user_id == user.id,
                StudyPayment.status == "succeeded",
                StudyPayment.quote_hash == quote_hash,
            )
        )
        if existing:
            raise ValueError("This study is already unlocked for live participants.")
        if self.study_live_participants_paid(study):
            raise ValueError("This study is already unlocked for live participants.")

        self._expire_stale_pending_payments(study_id=study_id, user_id=user.id, quote_hash=quote_hash)

        payment = StudyPayment(
            id=uuid4(),
            user_id=user.id,
            study_id=study_id,
            amount_cents=pricing["total_cents"],
            platform_fee_cents=pricing["platform_fee_cents"],
            panel_cost_cents=0,
            cint_cpi_cents=Decimal("0"),
            respondent_count=respondent_count,
            currency=pricing["currency"],
            audience_snapshot={"payment_type": "live_access_unlock"},
            quote_hash=quote_hash,
            status="pending",
        )
        self.db.add(payment)
        self.db.flush()

        customer_id = self._ensure_stripe_customer(user, profile)
        session = stripe.checkout.Session.create(
            mode="payment",
            customer=customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": pricing["currency"],
                        "unit_amount": pricing["total_cents"],
                        "product_data": {
                            "name": f"Unlock live participants: {study.title[:80]}",
                            "description": (
                                f"One-time ${pricing['total_cents'] / 100:.2f} MindSurve fee to open this study "
                                "for live participants (Cint panel costs are paid to Cint separately)."
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "user_id": str(user.id),
                "study_id": str(study_id),
                "payment_id": str(payment.id),
                "checkout_type": "study",
                "quote_hash": quote_hash,
            },
        )
        payment.stripe_checkout_session_id = session.id
        self.db.commit()
        self.db.refresh(payment)

        quote = StudyQuoteOut(
            study_id=study_id,
            respondent_count=respondent_count,
            cint_cpi_cents=0.0,
            platform_fee_cents=pricing["platform_fee_cents"],
            panel_cost_cents=0,
            total_cents=pricing["total_cents"],
            currency=pricing["currency"],
            quote_hash=quote_hash,
            audience_snapshot={"payment_type": "live_access_unlock"},
        )
        return StudyCheckoutResponse(
            checkout_url=session.url,
            session_id=session.id,
            payment_id=payment.id,
            quote=quote,
        )

    def quote_study(self, user: User, study_id: UUID) -> StudyQuoteOut:
        study = self.db.scalar(
            select(Study).where(Study.id == study_id, Study.creator_id == user.id)
        )
        if not study:
            raise ValueError("Study not found or access denied.")
        profile = self.get_or_create_profile(user)
        plan = effective_plan(profile)
        if not study_uses_live_participants(study.audience_segmentation):
            pricing = {"platform_fee_cents": 0, "total_cents": 0, "currency": settings.BILLING_CURRENCY.lower()}
        else:
            pricing = calculate_live_study_access_fee(plan)
        audience = normalize_audience_segmentation(study.audience_segmentation)
        respondent_count = int(audience.get("number_of_respondents") or 0)
        quote_hash = build_study_live_access_quote_hash(study_id)
        return StudyQuoteOut(
            study_id=study_id,
            respondent_count=respondent_count,
            cint_cpi_cents=0.0,
            platform_fee_cents=pricing["platform_fee_cents"],
            panel_cost_cents=0,
            total_cents=pricing["total_cents"],
            currency=pricing["currency"],
            quote_hash=quote_hash,
            audience_snapshot={"payment_type": "live_access_unlock", "plan": plan},
        )

    def create_portal_session(self, user: User, *, return_url: str) -> BillingPortalResponse:
        profile = self.get_or_create_profile(user)
        if not profile.stripe_customer_id:
            raise ValueError("No Stripe customer found for this user.")
        session = stripe.billing_portal.Session.create(
            customer=profile.stripe_customer_id,
            return_url=return_url,
        )
        return BillingPortalResponse(portal_url=session.url)

    def mark_study_live_participants_paid(self, study_id: UUID) -> None:
        study = self.db.get(Study, study_id)
        if study and not getattr(study, "live_participants_paid", False):
            study.live_participants_paid = True
            study.live_participants_unlocked = True
            self.db.flush()

    def mark_study_live_participants_unlocked(self, study_id: UUID) -> None:
        """Compatibility wrapper for older call sites."""
        self.mark_study_live_participants_paid(study_id)

    def study_live_participants_paid(self, study: Study) -> bool:
        """True only when the one-time $10 live-access fee was paid for this study."""
        if getattr(study, "live_participants_paid", False):
            return True

        creator_id = study.creator_id
        quote_hash = build_study_live_access_quote_hash(study.id)
        payment = self.db.scalar(
            select(StudyPayment).where(
                StudyPayment.study_id == study.id,
                StudyPayment.user_id == creator_id,
                StudyPayment.status == "succeeded",
                StudyPayment.quote_hash == quote_hash,
            )
        )
        if payment is not None:
            study.live_participants_paid = True
            study.live_participants_unlocked = True
            self.db.flush()
            return True
        return False

    def study_live_access_status(self, study: Study) -> Dict[str, Any]:
        """Return both stored payment state and current effective access for one study."""
        if not study_uses_live_participants(study.audience_segmentation):
            return {
                "paid": False,
                "included_by_plan": False,
                "allowed": True,
                "unlock_source": "ai_only",
            }

        creator = self.db.get(User, study.creator_id)
        profile = None
        if creator:
            profile = self.db.scalar(
                select(UserBillingProfile).where(UserBillingProfile.user_id == creator.id)
            )
        plan = effective_plan(profile)
        included_by_plan = plan in ("pro", "enterprise")
        paid = self.study_live_participants_paid(study)
        return {
            "paid": paid,
            "included_by_plan": included_by_plan,
            "allowed": included_by_plan or paid,
            "unlock_source": "plan" if included_by_plan else ("paid" if paid else "none"),
        }

    def live_participants_allowed(self, study: Study) -> bool:
        """True when live participants can open this study's share link."""
        return bool(self.study_live_access_status(study)["allowed"])

    def has_valid_study_payment(self, user_id: UUID, study: Study) -> bool:
        return self.live_participants_allowed(study)

    def _expire_stale_pending_payments(
        self, *, study_id: UUID, user_id: UUID, quote_hash: str
    ) -> None:
        pending = self.db.scalars(
            select(StudyPayment).where(
                StudyPayment.study_id == study_id,
                StudyPayment.user_id == user_id,
                StudyPayment.status == "pending",
            )
        ).all()
        for payment in pending:
            if payment.quote_hash != quote_hash:
                payment.status = "expired"
        self.db.flush()

    def set_enterprise_plan(self, user_id: UUID) -> UserBillingProfile:
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.user_id == user_id)
        )
        if not profile:
            profile = UserBillingProfile(user_id=user_id)
            self.db.add(profile)
        profile.plan = "enterprise"
        profile.subscription_status = "active"
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def handle_webhook_event(self, payload: bytes, sig_header: Optional[str]) -> Dict[str, Any]:
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise ValueError("Stripe webhook secret is not configured.")
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
        existing = self.db.scalar(
            select(BillingEvent).where(BillingEvent.stripe_event_id == event.id)
        )
        if existing:
            return {"received": True, "duplicate": True}

        self.db.add(
            BillingEvent(
                stripe_event_id=event.id,
                event_type=event.type,
                payload=event.to_dict(),
            )
        )

        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "customer.subscription.created": self._on_subscription_updated,
            "customer.subscription.updated": self._on_subscription_updated,
            "customer.subscription.deleted": self._on_subscription_deleted,
            "invoice.payment_failed": self._on_invoice_payment_failed,
            "payment_intent.succeeded": self._on_payment_intent_succeeded,
            "payment_intent.payment_failed": self._on_payment_intent_failed,
        }.get(event.type)

        if handler:
            handler(event.data.object)

        self.db.commit()
        return {"received": True, "event_type": event.type}

    def _on_checkout_completed(self, session: Dict[str, Any]) -> None:
        metadata = session.get("metadata") or {}
        checkout_type = metadata.get("checkout_type")
        if checkout_type == "subscription":
            self._sync_subscription_from_session(session)
        elif checkout_type == "study":
            if session.get("payment_status") not in (None, "paid"):
                logger.warning("Ignoring study checkout session without paid status: %s", session.get("id"))
                return
            payment_id = metadata.get("payment_id")
            if payment_id:
                payment = self.db.get(StudyPayment, UUID(payment_id))
                if payment and payment.status != "succeeded":
                    if payment.amount_cents != int(session.get("amount_total") or payment.amount_cents):
                        logger.warning("Ignoring study checkout with amount mismatch: %s", session.get("id"))
                        return
                    if str(payment.study_id) != str(metadata.get("study_id")):
                        logger.warning("Ignoring study checkout with study mismatch: %s", session.get("id"))
                        return
                    if payment.quote_hash != metadata.get("quote_hash"):
                        logger.warning("Ignoring study checkout with quote mismatch: %s", session.get("id"))
                        return
                    payment.status = "succeeded"
                    payment.paid_at = utcnow()
                    payment.stripe_checkout_session_id = session.get("id")
                    payment.stripe_payment_intent_id = session.get("payment_intent")
                    if payment.study_id:
                        self.mark_study_live_participants_paid(payment.study_id)

    def _sync_subscription_from_session(self, session: Dict[str, Any]) -> None:
        user_id = metadata_user_id(session.get("metadata"))
        if not user_id:
            return
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.user_id == user_id)
        )
        if not profile:
            profile = UserBillingProfile(user_id=user_id)
            self.db.add(profile)
        profile.stripe_customer_id = session.get("customer") or profile.stripe_customer_id
        subscription_id = session.get("subscription")
        if subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id)
            self._apply_subscription(profile, subscription)

    def _on_subscription_updated(self, subscription: Dict[str, Any]) -> None:
        customer_id = subscription.get("customer")
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.stripe_customer_id == customer_id)
        )
        if not profile:
            user_id = metadata_user_id(subscription.get("metadata"))
            if not user_id:
                return
            profile = UserBillingProfile(user_id=user_id, stripe_customer_id=customer_id)
            self.db.add(profile)
        self._apply_subscription(profile, subscription)

    def _apply_subscription(self, profile: UserBillingProfile, subscription: Dict[str, Any]) -> None:
        profile.stripe_subscription_id = subscription.get("id")
        status = subscription.get("status") or "none"
        profile.subscription_status = status if status in {
            "none", "active", "trialing", "past_due", "canceled", "incomplete"
        } else "none"
        if profile.subscription_status in ("active", "trialing"):
            profile.plan = "pro"
        elif profile.plan != "enterprise":
            profile.plan = "free"
        profile.current_period_start = _ts_to_dt(subscription.get("current_period_start"))
        profile.current_period_end = _ts_to_dt(subscription.get("current_period_end"))

    def _on_subscription_deleted(self, subscription: Dict[str, Any]) -> None:
        customer_id = subscription.get("customer")
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.stripe_customer_id == customer_id)
        )
        if not profile or profile.plan == "enterprise":
            return
        profile.subscription_status = "canceled"
        profile.plan = "free"
        profile.stripe_subscription_id = None

    def _on_invoice_payment_failed(self, invoice: Dict[str, Any]) -> None:
        customer_id = invoice.get("customer")
        profile = self.db.scalar(
            select(UserBillingProfile).where(UserBillingProfile.stripe_customer_id == customer_id)
        )
        if profile and profile.plan != "enterprise":
            profile.subscription_status = "past_due"

    def _on_payment_intent_succeeded(self, payment_intent: Dict[str, Any]) -> None:
        payment = self.db.scalar(
            select(StudyPayment).where(
                StudyPayment.stripe_payment_intent_id == payment_intent.get("id")
            )
        )
        if payment and payment.status != "succeeded":
            payment.status = "succeeded"
            payment.paid_at = utcnow()
            if payment.study_id:
                self.mark_study_live_participants_paid(payment.study_id)

    def _on_payment_intent_failed(self, payment_intent: Dict[str, Any]) -> None:
        payment = self.db.scalar(
            select(StudyPayment).where(
                StudyPayment.stripe_payment_intent_id == payment_intent.get("id")
            )
        )
        if payment and payment.status == "pending":
            payment.status = "failed"


def metadata_user_id(metadata: Optional[Dict[str, Any]]) -> Optional[UUID]:
    if not metadata:
        return None
    raw = metadata.get("user_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def _ts_to_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None
