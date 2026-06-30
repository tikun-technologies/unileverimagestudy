"""Study analysis settings: rating mappings and intercept mode."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.models.study_model import StudyAnalysisSettings

ANALYSIS_SETTINGS_CACHE_TTL = 3600  # 1 hour


def analysis_settings_cache_key(study_id: UUID) -> str:
    return f"study_analysis_settings:{study_id}"


def invalidate_analysis_settings_cache(study_id: UUID) -> None:
    RedisCache.delete(analysis_settings_cache_key(study_id))

DEFAULT_ANALYSIS_SETTINGS: Dict[str, Any] = {
    "top": {"hundred": [4, 5], "zero": [1, 2, 3]},
    "bottom": {"hundred": [1, 2], "zero": [3, 4, 5]},
    "regression": {"include_intercept": True},
}


def default_analysis_settings(max_rating: int = 5) -> Dict[str, Any]:
    """Return defaults, clamped to the study rating scale when max_rating != 5."""
    if max_rating == 5:
        return {
            "top": {"hundred": list(DEFAULT_ANALYSIS_SETTINGS["top"]["hundred"]), "zero": list(DEFAULT_ANALYSIS_SETTINGS["top"]["zero"])},
            "bottom": {"hundred": list(DEFAULT_ANALYSIS_SETTINGS["bottom"]["hundred"]), "zero": list(DEFAULT_ANALYSIS_SETTINGS["bottom"]["zero"])},
            "regression": {"include_intercept": True},
        }
    ratings = list(range(1, max_rating + 1))
    mid = max(1, max_rating // 2)
    top_hundred = [r for r in ratings if r > mid]
    top_zero = [r for r in ratings if r <= mid]
    bottom_hundred = [r for r in ratings if r <= mid]
    bottom_zero = [r for r in ratings if r > mid]
    return {
        "top": {"hundred": top_hundred, "zero": top_zero},
        "bottom": {"hundred": bottom_hundred, "zero": bottom_zero},
        "regression": {"include_intercept": True},
    }


def _normalize_rating_list(values: Any) -> List[int]:
    if not values:
        return []
    out: List[int] = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def normalize_analysis_settings(
    raw: Optional[Dict[str, Any]],
    max_rating: int = 5,
) -> Dict[str, Any]:
    """Validate and normalize analysis settings payload."""
    base = default_analysis_settings(max_rating)
    if not raw:
        return base

    top = raw.get("top") or {}
    bottom = raw.get("bottom") or {}
    regression = raw.get("regression") or {}

    top_hundred = _normalize_rating_list(top.get("hundred"))
    top_zero = _normalize_rating_list(top.get("zero"))
    bottom_hundred = _normalize_rating_list(bottom.get("hundred"))
    bottom_zero = _normalize_rating_list(bottom.get("zero"))

    valid = set(range(1, max_rating + 1))

    def _fill_defaults(hundred: List[int], zero: List[int], default_h: List[int], default_z: List[int]) -> tuple[List[int], List[int]]:
        hundred = [r for r in hundred if r in valid]
        zero = [r for r in zero if r in valid]
        if not hundred and not zero:
            return default_h, default_z
        assigned = set(hundred) | set(zero)
        for r in valid:
            if r not in assigned:
                if r in default_h and r not in hundred:
                    hundred.append(r)
                elif r in default_z and r not in zero:
                    zero.append(r)
                else:
                    zero.append(r)
        overlap = set(hundred) & set(zero)
        for r in overlap:
            zero = [x for x in zero if x != r]
        return sorted(set(hundred)), sorted(set(zero))

    top_hundred, top_zero = _fill_defaults(
        top_hundred, top_zero, base["top"]["hundred"], base["top"]["zero"]
    )
    bottom_hundred, bottom_zero = _fill_defaults(
        bottom_hundred, bottom_zero, base["bottom"]["hundred"], base["bottom"]["zero"]
    )

    include_intercept = regression.get("include_intercept")
    if include_intercept is None:
        include_intercept = True
    else:
        include_intercept = bool(include_intercept)

    return {
        "top": {"hundred": top_hundred, "zero": top_zero},
        "bottom": {"hundred": bottom_hundred, "zero": bottom_zero},
        "regression": {"include_intercept": include_intercept},
    }


def get_max_rating_from_study(study) -> int:
    scale = getattr(study, "rating_scale", None) or {}
    try:
        val = int(scale.get("max_value", 5))
    except (TypeError, ValueError):
        val = 5
    if val not in (5, 7, 9):
        val = 5
    return val


def get_study_analysis_settings(db: Session, study_id: UUID, study=None) -> Dict[str, Any]:
    """Load saved analysis settings for a study, or defaults if none saved."""
    if study is None:
        from app.models.study_model import Study
        study = db.get(Study, study_id)
    max_rating = get_max_rating_from_study(study) if study else 5

    row = (
        db.query(StudyAnalysisSettings)
        .filter(StudyAnalysisSettings.study_id == study_id)
        .first()
    )
    if not row or not row.settings:
        return default_analysis_settings(max_rating)
    return normalize_analysis_settings(row.settings, max_rating=max_rating)


def build_analysis_settings_response(
    db: Session,
    study_id: UUID,
    study=None,
) -> Dict[str, Any]:
    """Build the API response payload for GET /analysis-settings."""
    if study is None:
        from app.models.study_model import Study
        study = db.get(Study, study_id)
    max_rating = get_max_rating_from_study(study) if study else 5
    defaults = default_analysis_settings(max_rating)

    row = (
        db.query(StudyAnalysisSettings)
        .filter(StudyAnalysisSettings.study_id == study_id)
        .first()
    )
    settings = get_study_analysis_settings(db, study_id, study=study)
    is_default = row is None or not row.settings

    return {
        "study_id": str(study_id),
        "settings": settings,
        "max_rating": max_rating,
        "is_default": is_default,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "defaults": defaults,
    }


def get_cached_analysis_settings_response(
    db: Session,
    study_id: UUID,
    study=None,
) -> Dict[str, Any]:
    cache_key = analysis_settings_cache_key(study_id)
    cached = RedisCache.get(cache_key)
    if cached is not None:
        return cached

    payload = build_analysis_settings_response(db, study_id, study=study)
    RedisCache.set(cache_key, payload, ttl_seconds=ANALYSIS_SETTINGS_CACHE_TTL)
    return payload


def save_study_analysis_settings(
    db: Session,
    study_id: UUID,
    settings: Dict[str, Any],
    user_id: UUID,
    study=None,
) -> Dict[str, Any]:
    """Persist analysis settings for a study (one row per study)."""
    if study is None:
        from app.models.study_model import Study
        study = db.get(Study, study_id)
    max_rating = get_max_rating_from_study(study) if study else 5
    normalized = normalize_analysis_settings(settings, max_rating=max_rating)

    row = (
        db.query(StudyAnalysisSettings)
        .filter(StudyAnalysisSettings.study_id == study_id)
        .first()
    )
    if row:
        row.settings = normalized
        row.updated_by_id = user_id
    else:
        row = StudyAnalysisSettings(
            study_id=study_id,
            settings=normalized,
            updated_by_id=user_id,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    invalidate_analysis_settings_cache(study_id)
    return normalized
