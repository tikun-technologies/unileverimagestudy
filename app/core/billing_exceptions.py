from fastapi import HTTPException, status
from typing import Any, Dict, Optional

from app.schemas.billing_schema import PaymentRequiredDetail


class PaymentRequired(HTTPException):
    """Raised when an action requires payment or plan upgrade."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_plan: Optional[str] = None,
        checkout_type: Optional[str] = None,
        checkout_payload: Optional[Dict[str, Any]] = None,
    ):
        detail = PaymentRequiredDetail(
            code=code,
            message=message,
            required_plan=required_plan,  # type: ignore[arg-type]
            checkout_type=checkout_type,  # type: ignore[arg-type]
            checkout_payload=checkout_payload,
        )
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=detail.model_dump(exclude_none=True),
        )


class LiveStudyAccessBlocked(HTTPException):
    """Raised when a live participant tries to open a study that is not yet paid/unlocked."""

    def __init__(self, *, study_id: str, message: Optional[str] = None):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "live_study_locked",
                "message": message
                or (
                    "This study is not open for live participants yet. "
                    "The study owner must pay the activation fee or upgrade to Pro."
                ),
                "study_id": study_id,
            },
        )
