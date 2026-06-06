from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.core.security import create_token_pair, refresh_access_token, verify_token
from app.models.user_model import User
from app.schemas.user_schema import UserResponse
from app.services.billing import BillingService


def build_user_response(db: Session, user: User) -> UserResponse:
    billing = BillingService(db).get_billing_summary(user)
    base = UserResponse.model_validate(user)
    return base.model_copy(
        update={
            "plan": billing.plan,
            "subscription_status": billing.subscription_status,
            "billing_limits": billing.limits,
            "has_active_subscription": billing.has_active_subscription,
        }
    )


def _billing_claims(db: Session, user: User) -> dict[str, str]:
    billing = BillingService(db).get_billing_summary(user)
    return {
        "plan": billing.plan,
        "subscription_status": billing.subscription_status,
    }


def create_tokens_for_user(db: Session, user: User) -> dict:
    claims = _billing_claims(db, user)
    return create_token_pair(
        user.id,
        user.email,
        plan=claims["plan"],
        subscription_status=claims["subscription_status"],
    )


def refresh_access_token_for_user(db: Session, refresh_token: str) -> Optional[dict]:
    payload = verify_token(refresh_token, "refresh")
    if not payload:
        return None

    user_id_str = payload.get("sub")
    if not user_id_str:
        return None

    try:
        user_id = uuid.UUID(str(user_id_str))
    except ValueError:
        return None

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        return None

    return refresh_access_token(refresh_token, token_data_override=_billing_claims(db, user))
