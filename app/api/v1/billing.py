import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from uuid import UUID

from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.schemas.billing_schema import (
    BillingPlansResponse,
    BillingPortalRequest,
    BillingPortalResponse,
    BillingStatusOut,
    StudyCheckoutRequest,
    StudyCheckoutResponse,
    StudyLiveAccessOut,
    StudyQuoteOut,
    SubscriptionCheckoutRequest,
    SubscriptionCheckoutResponse,
)
from app.services.billing import BillingService

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/plans", response_model=BillingPlansResponse)
def get_plans(db: Session = Depends(get_db)):
    return BillingService(db).get_plans_catalog()


@router.get("/status", response_model=BillingStatusOut)
def get_billing_status(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return BillingService(db).get_status(current_user)


@router.get("/study/{study_id}/live-access", response_model=StudyLiveAccessOut)
def get_study_live_access(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    from sqlalchemy import select
    from app.models.study_model import Study
    from app.services.study_unlock_fee import calculate_live_study_access_fee

    study = db.scalar(
        select(Study).where(Study.id == study_id, Study.creator_id == current_user.id)
    )
    if not study:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study not found")
    service = BillingService(db)
    summary = service.get_billing_summary(current_user)
    pricing = calculate_live_study_access_fee(summary.plan)
    access = service.study_live_access_status(study)
    allowed = bool(access["allowed"])
    return StudyLiveAccessOut(
        study_id=study_id,
        live_participants_allowed=allowed,
        live_participants_paid=bool(access["paid"]),
        live_participants_included_by_plan=bool(access["included_by_plan"]),
        live_participants_unlocked=allowed,
        requires_payment=not allowed and pricing["total_cents"] > 0,
        amount_cents=pricing["total_cents"],
        plan=summary.plan,
        currency=pricing["currency"],
        unlock_source=access["unlock_source"],
    )


@router.get("/study/{study_id}/quote", response_model=StudyQuoteOut)
def get_study_quote(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        return BillingService(db).quote_study(current_user, study_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/subscription-checkout", response_model=SubscriptionCheckoutResponse)
def create_subscription_checkout(
    payload: SubscriptionCheckoutRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        return BillingService(db).create_subscription_checkout(
            current_user,
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        logger.exception("Failed to create subscription checkout")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe checkout failed")


@router.post("/study-checkout", response_model=StudyCheckoutResponse)
def create_study_checkout(
    payload: StudyCheckoutRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        return BillingService(db).create_study_checkout(
            current_user,
            study_id=payload.study_id,
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        logger.exception("Failed to create study checkout")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe checkout failed")


@router.post("/portal", response_model=BillingPortalResponse)
def create_billing_portal(
    payload: BillingPortalRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        return BillingService(db).create_portal_session(current_user, return_url=payload.return_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        logger.exception("Failed to create billing portal session")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe portal failed")


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        result = BillingService(db).handle_webhook_event(payload, sig_header)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        logger.exception("Stripe webhook processing failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook error")
