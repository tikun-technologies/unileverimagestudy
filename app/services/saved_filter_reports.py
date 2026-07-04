"""Named saved filter reports (StudyFilterHistory) with Redis-backed list reads."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.models.study_model import StudyFilterHistory

SAVED_REPORTS_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days


def saved_reports_cache_key(study_id: UUID | str, user_id: UUID | str) -> str:
    return f"saved_reports:{study_id}:{user_id}"


def _normalize_string_list(values: Optional[List[str]]) -> List[str]:
    return sorted(
        [v.strip() for v in (values or []) if isinstance(v, str) and v.strip()],
        key=lambda x: x.lower(),
    )


def _normalize_classification_filters(
    filters: Optional[Dict[str, List[str]]],
) -> Dict[str, List[str]]:
    if not filters:
        return {}
    out: Dict[str, List[str]] = {}
    for question, answers in filters.items():
        if not isinstance(question, str):
            continue
        normalized = _normalize_string_list(answers if isinstance(answers, list) else [])
        if normalized:
            out[question.strip()] = normalized
    return dict(sorted(out.items(), key=lambda item: item[0].lower()))


def filters_equal(
    a: Optional[Dict[str, Any]],
    b: Optional[Dict[str, Any]],
) -> bool:
    def signature(f: Optional[Dict[str, Any]]) -> tuple:
        if not f:
            f = {}
        age = tuple(_normalize_string_list(f.get("age_groups")))
        genders = tuple(_normalize_string_list(f.get("genders")))
        classification = _normalize_classification_filters(f.get("classification_filters"))
        class_part = tuple(
            (q, tuple(answers)) for q, answers in sorted(classification.items())
        )
        return (age, genders, class_part)

    return signature(a) == signature(b)


def _serialize_row(row: StudyFilterHistory) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "study_id": str(row.study_id),
        "name": row.name or "Untitled report",
        "filters": row.filters or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def invalidate_saved_reports_cache(study_id: UUID, user_id: UUID) -> None:
    RedisCache.delete(saved_reports_cache_key(study_id, user_id))


def list_saved_reports(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    *,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    cache_key = saved_reports_cache_key(study_id, user_id)
    if use_cache:
        cached = RedisCache.get(cache_key)
        if isinstance(cached, list):
            return cached

    rows = (
        db.query(StudyFilterHistory)
        .filter(
            StudyFilterHistory.study_id == study_id,
            StudyFilterHistory.user_id == user_id,
            StudyFilterHistory.name.isnot(None),
            StudyFilterHistory.name != "",
        )
        .order_by(StudyFilterHistory.created_at.desc())
        .all()
    )
    payload = [_serialize_row(r) for r in rows]
    RedisCache.set(cache_key, payload, ttl_seconds=SAVED_REPORTS_CACHE_TTL)
    return payload


def find_duplicate_report(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    filters_dict: Dict[str, Any],
    *,
    exclude_id: Optional[UUID] = None,
) -> Optional[StudyFilterHistory]:
    rows = (
        db.query(StudyFilterHistory)
        .filter(
            StudyFilterHistory.study_id == study_id,
            StudyFilterHistory.user_id == user_id,
            StudyFilterHistory.name.isnot(None),
            StudyFilterHistory.name != "",
        )
        .all()
    )
    for row in rows:
        if exclude_id and row.id == exclude_id:
            continue
        if filters_equal(row.filters or {}, filters_dict):
            return row
    return None


def create_saved_report(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    name: str,
    filters_dict: Dict[str, Any],
) -> Dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Report name is required")

    duplicate = find_duplicate_report(db, study_id, user_id, filters_dict)
    if duplicate:
        existing_name = duplicate.name or "Untitled report"
        raise HTTPException(
            status_code=409,
            detail=f"This filter is already saved as \"{existing_name}\".",
        )

    record = StudyFilterHistory(
        study_id=study_id,
        user_id=user_id,
        filters=filters_dict or {},
        name=clean_name[:255],
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    invalidate_saved_reports_cache(study_id, user_id)
    return _serialize_row(record)


def update_saved_report_name(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    report_id: UUID,
    name: str,
) -> Dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Report name is required")

    row = (
        db.query(StudyFilterHistory)
        .filter(
            StudyFilterHistory.id == report_id,
            StudyFilterHistory.study_id == study_id,
            StudyFilterHistory.user_id == user_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Saved report not found")

    row.name = clean_name[:255]
    db.commit()
    db.refresh(row)
    invalidate_saved_reports_cache(study_id, user_id)
    return _serialize_row(row)


def delete_saved_report(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    report_id: UUID,
) -> None:
    row = (
        db.query(StudyFilterHistory)
        .filter(
            StudyFilterHistory.id == report_id,
            StudyFilterHistory.study_id == study_id,
            StudyFilterHistory.user_id == user_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Saved report not found")
    db.delete(row)
    db.commit()
    invalidate_saved_reports_cache(study_id, user_id)
