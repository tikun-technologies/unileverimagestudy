"""Active analytics filter persistence (Redis-backed read-through for filter selection only)."""
from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.models.study_model import StudyActiveFilter

ACTIVE_FILTER_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days


def active_filter_cache_key(study_id: UUID | str, user_id: UUID | str) -> str:
    return f"active_filter:{study_id}:{user_id}"


def filters_are_active(filters: Optional[Dict[str, Any]]) -> bool:
    if not filters:
        return False
    if filters.get("age_groups") or filters.get("genders"):
        return True
    class_f = filters.get("classification_filters") or {}
    return any(vals for vals in class_f.values())


def get_active_filter(
    db: Session,
    study_id: UUID,
    user_id: UUID,
) -> Optional[Dict[str, Any]]:
    cache_key = active_filter_cache_key(study_id, user_id)
    cached = RedisCache.get(cache_key)
    if cached is not None:
        if cached == {}:
            return None
        if isinstance(cached, dict) and filters_are_active(cached):
            return cached
        if isinstance(cached, dict) and not filters_are_active(cached):
            return None

    row = (
        db.query(StudyActiveFilter)
        .filter(
            StudyActiveFilter.study_id == study_id,
            StudyActiveFilter.user_id == user_id,
        )
        .first()
    )
    if not row or not filters_are_active(row.filters):
        RedisCache.set(cache_key, {}, ttl_seconds=ACTIVE_FILTER_CACHE_TTL)
        return None

    filters = dict(row.filters or {})
    RedisCache.set(cache_key, filters, ttl_seconds=ACTIVE_FILTER_CACHE_TTL)
    return filters


def save_active_filter(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    filters_dict: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    cache_key = active_filter_cache_key(study_id, user_id)
    if not filters_are_active(filters_dict):
        clear_active_filter(db, study_id, user_id)
        return None

    filters = dict(filters_dict or {})
    row = (
        db.query(StudyActiveFilter)
        .filter(
            StudyActiveFilter.study_id == study_id,
            StudyActiveFilter.user_id == user_id,
        )
        .first()
    )
    if row:
        row.filters = filters
    else:
        row = StudyActiveFilter(
            study_id=study_id,
            user_id=user_id,
            filters=filters,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    RedisCache.set(cache_key, filters, ttl_seconds=ACTIVE_FILTER_CACHE_TTL)
    return filters


def clear_active_filter(db: Session, study_id: UUID, user_id: UUID) -> None:
    cache_key = active_filter_cache_key(study_id, user_id)
    (
        db.query(StudyActiveFilter)
        .filter(
            StudyActiveFilter.study_id == study_id,
            StudyActiveFilter.user_id == user_id,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    RedisCache.set(cache_key, {}, ttl_seconds=ACTIVE_FILTER_CACHE_TTL)
