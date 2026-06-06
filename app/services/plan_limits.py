from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing_model import UserBillingProfile
from app.models.study_model import Study
from app.schemas.billing_schema import PlanLimitsOut, PlanType

logger = logging.getLogger(__name__)

PLAN_ORDER = {"free": 0, "pro": 1, "enterprise": 2}


def get_plan_limits(plan: PlanType) -> PlanLimitsOut:
    if plan == "enterprise":
        return PlanLimitsOut(
            max_categories=9999,
            max_elements_per_category=9999,
            max_layers=9999,
            max_images_per_layer=9999,
            ai_respondent_limit=999999,
            can_share_study=True,
            can_analysis_export=True,
            can_basic_export=True,
        )
    if plan == "pro":
        return PlanLimitsOut(
            max_categories=settings.PRO_MAX_CATEGORIES,
            max_elements_per_category=settings.PRO_MAX_ELEMENTS_PER_CATEGORY,
            max_layers=settings.PRO_MAX_LAYERS,
            max_images_per_layer=settings.PRO_MAX_IMAGES_PER_LAYER,
            ai_respondent_limit=settings.PRO_AI_RESPONDENT_LIMIT,
            can_share_study=True,
            can_analysis_export=True,
            can_basic_export=True,
        )
    return PlanLimitsOut(
        max_categories=settings.FREE_MAX_CATEGORIES,
        max_elements_per_category=settings.FREE_MAX_ELEMENTS_PER_CATEGORY,
        max_layers=settings.FREE_MAX_LAYERS,
        max_images_per_layer=settings.FREE_MAX_IMAGES_PER_LAYER,
        ai_respondent_limit=settings.FREE_AI_RESPONDENT_LIMIT,
        can_share_study=False,
        can_analysis_export=False,
        can_basic_export=True,
    )


def effective_plan(profile: Optional[UserBillingProfile]) -> PlanType:
    if not profile:
        return "free"
    if profile.plan == "enterprise":
        return "enterprise"
    if profile.plan == "pro" and profile.subscription_status in ("active", "trialing"):
        return "pro"
    return "free"


def plan_meets_minimum(current: PlanType, required: PlanType) -> bool:
    return PLAN_ORDER.get(current, 0) >= PLAN_ORDER.get(required, 0)


def normalize_audience_segmentation(seg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(seg, dict):
        return {}
    return {
        "number_of_respondents": int(seg.get("number_of_respondents") or 0),
        "country": (seg.get("country") or "").strip(),
        "gender_distribution": seg.get("gender_distribution") or {},
        "age_distribution": seg.get("age_distribution") or {},
        # ai_only = synthetic only; cint / real_panel / live = live participants (Cint paid separately)
        "respondent_source": (seg.get("respondent_source") or "cint").strip().lower(),
    }


def build_quote_hash(audience_snapshot: Dict[str, Any], study_id: Optional[UUID]) -> str:
    payload = {
        "study_id": str(study_id) if study_id else None,
        "audience": audience_snapshot,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_study_live_access_quote_hash(study_id: UUID) -> str:
    """Flat live-participant unlock fee is tied to the study, not audience filters."""
    raw = f"live_access:{study_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def study_uses_live_participants(audience_segmentation: Optional[Dict[str, Any]]) -> bool:
    """True when the study is meant for live/Cint participants (not AI-only)."""
    seg = normalize_audience_segmentation(audience_segmentation)
    return seg.get("respondent_source") != "ai_only"


def requires_real_panel_payment(audience_segmentation: Optional[Dict[str, Any]]) -> bool:
    """Alias kept for tests — live participant studies need unlock on Free plan."""
    return study_uses_live_participants(audience_segmentation)


def count_grid_structure(
    categories: Optional[List[Any]],
    elements: Optional[List[Any]],
) -> Tuple[int, int]:
    cat_count = len(categories or [])
    if not cat_count or not elements:
        return cat_count, 0
    per_cat: Dict[str, int] = {}
    for el in elements:
        cat_id = str(getattr(el, "category_id", None) or (el.get("category_id") if isinstance(el, dict) else ""))
        per_cat[cat_id] = per_cat.get(cat_id, 0) + 1
    max_elements = max(per_cat.values()) if per_cat else len(elements)
    return cat_count, max_elements


def count_layer_structure(study_layers: Optional[List[Any]]) -> Tuple[int, int]:
    layers = study_layers or []
    layer_count = len(layers)
    max_images = 0
    for layer in layers:
        images = getattr(layer, "images", None)
        if images is None and isinstance(layer, dict):
            images = layer.get("images")
        max_images = max(max_images, len(images or []))
    return layer_count, max_images


def validate_structure_limits(
    *,
    plan: PlanType,
    study_type: str,
    categories: Optional[List[Any]] = None,
    elements: Optional[List[Any]] = None,
    study_layers: Optional[List[Any]] = None,
) -> None:
    limits = get_plan_limits(plan)
    if study_type == "layer":
        layer_count, max_images = count_layer_structure(study_layers)
        if layer_count > limits.max_layers:
            raise ValueError(
                f"Plan '{plan}' allows at most {limits.max_layers} layers; study has {layer_count}."
            )
        if max_images > limits.max_images_per_layer:
            raise ValueError(
                f"Plan '{plan}' allows at most {limits.max_images_per_layer} images per layer; "
                f"study has up to {max_images}."
            )
        return

    cat_count, max_elements = count_grid_structure(categories, elements)
    if cat_count > limits.max_categories:
        raise ValueError(
            f"Plan '{plan}' allows at most {limits.max_categories} categories; study has {cat_count}."
        )
    if max_elements > limits.max_elements_per_category:
        raise ValueError(
            f"Plan '{plan}' allows at most {limits.max_elements_per_category} elements per category; "
            f"study has up to {max_elements}."
        )


def count_ai_respondents_used(db: Session, user_id: UUID) -> int:
    """Sum simulated AI respondents across creator-owned studies."""
    rows = db.scalars(
        select(Study.audience_segmentation).where(Study.creator_id == user_id)
    ).all()
    total = 0
    for seg in rows:
        if isinstance(seg, dict) and seg.get("respondent_source") == "ai_only":
            total += int(seg.get("ai_respondents_simulated") or seg.get("number_of_respondents") or 0)
    return total


def count_user_studies(db: Session, user_id: UUID) -> int:
    return int(
        db.scalar(select(func.count()).select_from(Study).where(Study.creator_id == user_id)) or 0
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
