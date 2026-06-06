"""Flat MindSurve study unlock fee (not Cint panel costs — those are paid to Cint separately)."""

from __future__ import annotations

from typing import Any, Dict

from app.core.config import settings


def calculate_live_study_access_fee(plan: str) -> Dict[str, Any]:
    """
  Free plan: one-time PLATFORM_BASE_FEE_CENTS ($10) per study to open the share link for live participants.
  Pro / Enterprise: included — no unlock fee.
  """
    currency = settings.BILLING_CURRENCY.lower()
    if plan in ("pro", "enterprise"):
        return {
            "platform_fee_cents": 0,
            "total_cents": 0,
            "currency": currency,
        }
    fee = int(settings.PLATFORM_BASE_FEE_CENTS)
    return {
        "platform_fee_cents": fee,
        "total_cents": fee,
        "currency": currency,
    }
