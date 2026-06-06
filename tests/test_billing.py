import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.billing_exceptions import PaymentRequired
from app.services.plan_limits import (
    build_study_live_access_quote_hash,
    get_plan_limits,
    requires_real_panel_payment,
    study_uses_live_participants,
)
from app.services.plan_enforcement import enforce_analysis_export, enforce_structure_limits
from app.schemas.billing_schema import UserBillingSummary
from app.services.billing import BillingService
from app.services.study_unlock_fee import calculate_live_study_access_fee


def test_free_plan_limits():
    limits = get_plan_limits("free")
    assert limits.max_categories == 4
    assert limits.ai_respondent_limit == 50
    assert limits.can_share_study is False
    assert limits.can_basic_export is True
    assert limits.can_analysis_export is False


def test_enforce_analysis_export_blocks_free_plan():
    billing = UserBillingSummary(
        plan="free",
        subscription_status="none",
        limits=get_plan_limits("free"),
        has_active_subscription=False,
    )
    with pytest.raises(PaymentRequired) as exc:
        enforce_analysis_export(billing)
    assert exc.value.status_code == 402


def test_study_uses_live_participants():
    assert study_uses_live_participants({"respondent_source": "cint"}) is True
    assert study_uses_live_participants({"respondent_source": "ai_only"}) is False


def test_requires_real_panel_payment_ai_only():
    assert requires_real_panel_payment({"respondent_source": "ai_only"}) is False
    assert requires_real_panel_payment({"respondent_source": "cint"}) is True


def test_calculate_live_study_access_fee_free(monkeypatch):
    monkeypatch.setattr(
        "app.services.study_unlock_fee.settings.PLATFORM_BASE_FEE_CENTS",
        1000,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.study_unlock_fee.settings.BILLING_CURRENCY",
        "usd",
        raising=False,
    )
    result = calculate_live_study_access_fee("free")
    assert result["total_cents"] == 1000
    assert result["platform_fee_cents"] == 1000


def test_calculate_live_study_access_fee_pro(monkeypatch):
    monkeypatch.setattr(
        "app.services.study_unlock_fee.settings.BILLING_CURRENCY",
        "usd",
        raising=False,
    )
    result = calculate_live_study_access_fee("pro")
    assert result["total_cents"] == 0


def test_study_live_access_quote_hash_is_stable():
    study_id = uuid4()
    assert build_study_live_access_quote_hash(study_id) == build_study_live_access_quote_hash(study_id)


def test_enforce_structure_limits_raises_payment_required():
    billing = UserBillingSummary(
        plan="free",
        subscription_status="none",
        limits=get_plan_limits("free"),
        has_active_subscription=False,
    )
    layers = [type("L", (), {"images": [1]})() for _ in range(5)]
    with pytest.raises(PaymentRequired) as exc:
        enforce_structure_limits(billing, study_type="layer", study_layers=layers)
    assert exc.value.status_code == 402


class _FakeBillingDb:
    def __init__(self, *, profile=None, payment=None):
        self.profile = profile
        self.payment = payment

    def get(self, _model, _id):
        return SimpleNamespace(id=_id)

    def scalar(self, _stmt):
        if self.profile is not None:
            profile = self.profile
            self.profile = None
            return profile
        return self.payment

    def flush(self):
        return None


def _study(*, respondent_source="cint", paid=False):
    return SimpleNamespace(
        id=uuid4(),
        creator_id=uuid4(),
        audience_segmentation={"respondent_source": respondent_source},
        live_participants_paid=paid,
        live_participants_unlocked=paid,
    )


def test_pro_plan_allows_existing_unpaid_live_study_without_marking_paid():
    profile = SimpleNamespace(plan="pro", subscription_status="active")
    study = _study(paid=False)
    status = BillingService(_FakeBillingDb(profile=profile)).study_live_access_status(study)
    assert status["allowed"] is True
    assert status["included_by_plan"] is True
    assert status["paid"] is False
    assert status["unlock_source"] == "plan"


def test_free_plan_requires_paid_flag_for_live_study():
    profile = SimpleNamespace(plan="free", subscription_status="none")
    study = _study(paid=False)
    status = BillingService(_FakeBillingDb(profile=profile)).study_live_access_status(study)
    assert status["allowed"] is False
    assert status["paid"] is False
    assert status["unlock_source"] == "none"


def test_paid_live_study_stays_allowed_on_free_plan():
    profile = SimpleNamespace(plan="free", subscription_status="none")
    study = _study(paid=True)
    status = BillingService(_FakeBillingDb(profile=profile)).study_live_access_status(study)
    assert status["allowed"] is True
    assert status["paid"] is True
    assert status["unlock_source"] == "paid"


def test_ai_only_study_does_not_need_live_unlock_payment():
    study = _study(respondent_source="ai_only", paid=False)
    status = BillingService(_FakeBillingDb()).study_live_access_status(study)
    assert status["allowed"] is True
    assert status["paid"] is False
    assert status["unlock_source"] == "ai_only"
