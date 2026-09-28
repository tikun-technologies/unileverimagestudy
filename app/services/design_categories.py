"""Study-scoped categories of saved configurator combinations.

The list is one join. Each item includes the saved-design configuration so
callers get element urls, z-index, transforms, and the layer background
without a second request.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.study_model import StudyDesignCategory, StudyDesignCategoryItem, StudySavedDesign
from app.schemas.study_schema import DesignCategoryAssignRequest, DesignCategoryRenameRequest

MAX_CATEGORIES_PER_STUDY = 200
MAX_DESIGNS_PER_ASSIGN = 100


def normalize_category_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


def _category_columns():
    return (
        StudyDesignCategory.id.label("category_id"),
        StudyDesignCategory.study_id.label("study_id"),
        StudyDesignCategory.name.label("category_name"),
        StudyDesignCategory.position.label("category_position"),
        StudyDesignCategory.created_at.label("category_created_at"),
        StudyDesignCategory.updated_at.label("category_updated_at"),
        StudyDesignCategoryItem.id.label("item_id"),
        StudyDesignCategoryItem.saved_design_id.label("saved_design_id"),
        StudyDesignCategoryItem.position.label("item_position"),
        StudySavedDesign.name.label("design_name"),
        StudySavedDesign.design_type.label("design_type"),
        StudySavedDesign.study_type.label("study_type"),
        StudySavedDesign.metric.label("metric"),
        StudySavedDesign.segment_label.label("segment_label"),
        StudySavedDesign.selection_count.label("selection_count"),
        StudySavedDesign.total_coefficient.label("total_coefficient"),
        StudySavedDesign.configuration.label("configuration"),
    )


def _rows_to_categories(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    categories: List[Dict[str, Any]] = []
    index_by_id: Dict[Any, int] = {}
    for row in rows:
        category_id = row.category_id
        if category_id not in index_by_id:
            index_by_id[category_id] = len(categories)
            categories.append(
                {
                    "id": category_id,
                    "study_id": row.study_id,
                    "name": row.category_name,
                    "position": row.category_position,
                    "created_at": row.category_created_at,
                    "updated_at": row.category_updated_at,
                    "items": [],
                }
            )
        if row.item_id is None or row.saved_design_id is None or row.design_name is None:
            continue
        categories[index_by_id[category_id]]["items"].append(
            {
                "id": row.item_id,
                "saved_design_id": row.saved_design_id,
                "name": row.design_name,
                "design_type": row.design_type,
                "study_type": row.study_type,
                "metric": row.metric,
                "segment_label": row.segment_label,
                "selection_count": row.selection_count or 0,
                "total_coefficient": row.total_coefficient,
                "position": row.item_position or 0,
                "configuration": row.configuration or {},
            }
        )
    return categories


def _category_query(study_id: UUID, category_id: Optional[UUID] = None):
    query = (
        select(*_category_columns())
        .select_from(StudyDesignCategory)
        .outerjoin(
            StudyDesignCategoryItem,
            StudyDesignCategoryItem.category_id == StudyDesignCategory.id,
        )
        .outerjoin(
            StudySavedDesign,
            StudySavedDesign.id == StudyDesignCategoryItem.saved_design_id,
        )
        .where(StudyDesignCategory.study_id == study_id)
        .order_by(
            StudyDesignCategory.position,
            StudyDesignCategory.created_at,
            StudyDesignCategoryItem.position,
            StudyDesignCategoryItem.created_at,
        )
    )
    if category_id is not None:
        query = query.where(StudyDesignCategory.id == category_id)
    return query


def list_design_categories(db: Session, study_id: UUID) -> List[Dict[str, Any]]:
    rows = db.execute(_category_query(study_id)).all()
    return _rows_to_categories(rows)


def get_design_category(db: Session, study_id: UUID, category_id: UUID) -> Dict[str, Any]:
    rows = db.execute(_category_query(study_id, category_id)).all()
    categories = _rows_to_categories(rows)
    if not categories:
        raise HTTPException(status_code=404, detail="Category not found.")
    return categories[0]


def _saved_design_to_out(design: StudySavedDesign) -> Dict[str, Any]:
    return {
        "id": design.id,
        "study_id": design.study_id,
        "created_by_id": design.created_by_id,
        "name": design.name,
        "design_type": design.design_type,
        "study_type": design.study_type,
        "metric": design.metric,
        "segment_label": design.segment_label,
        "selection_count": design.selection_count,
        "total_coefficient": design.total_coefficient,
        "configuration": design.configuration or {},
        "created_at": design.created_at,
        "updated_at": design.updated_at,
    }


def _create_saved_design(db: Session, study_id: UUID, user_id: UUID, payload) -> StudySavedDesign:
    normalized_name = normalize_category_name(payload.name)
    design_type = payload.design_type or "configurator"
    if not normalized_name:
        raise HTTPException(status_code=400, detail="Design name is required")

    existing_id = db.scalar(
        select(StudySavedDesign.id).where(
            StudySavedDesign.study_id == study_id,
            StudySavedDesign.design_type == design_type,
            StudySavedDesign.normalized_name == normalized_name,
        )
    )
    if existing_id:
        raise HTTPException(status_code=409, detail="A saved design with this name already exists.")

    configuration = payload.configuration.model_dump(exclude_none=True)
    segment = configuration.get("segment") or {}
    selected_elements = configuration.get("selected_elements") or []
    selected_by_category = configuration.get("selected_by_category") or {}
    selection_count = len(selected_elements) if isinstance(selected_elements, list) else len(selected_by_category)

    design = StudySavedDesign(
        study_id=study_id,
        created_by_id=user_id,
        name=payload.name,
        normalized_name=normalized_name,
        design_type=design_type,
        study_type=payload.configuration.study_type,
        metric=payload.configuration.metric,
        segment_label=segment.get("label") if isinstance(segment, dict) else None,
        selection_count=selection_count,
        total_coefficient=payload.configuration.total_coefficient,
        configuration=configuration,
    )
    db.add(design)
    db.flush()
    return design


def _attach_designs(
    db: Session,
    category: StudyDesignCategory,
    design_ids: List[UUID],
) -> None:
    if not design_ids:
        return
    existing = set(
        db.scalars(
            select(StudyDesignCategoryItem.saved_design_id).where(
                StudyDesignCategoryItem.category_id == category.id,
                StudyDesignCategoryItem.saved_design_id.in_(design_ids),
            )
        ).all()
    )
    max_position = db.scalar(
        select(func.coalesce(func.max(StudyDesignCategoryItem.position), 0)).where(
            StudyDesignCategoryItem.category_id == category.id
        )
    ) or 0
    for design_id in design_ids:
        if design_id in existing:
            continue
        max_position += 1
        db.add(
            StudyDesignCategoryItem(
                category_id=category.id,
                saved_design_id=design_id,
                position=max_position,
            )
        )


def assign_designs_to_category(
    db: Session,
    study_id: UUID,
    user_id: UUID,
    payload: DesignCategoryAssignRequest,
) -> Dict[str, Any]:
    design_ids = list(dict.fromkeys(payload.saved_design_ids))
    if len(design_ids) > MAX_DESIGNS_PER_ASSIGN:
        raise HTTPException(status_code=400, detail="Too many combinations in one request.")

    if design_ids:
        found = set(
            db.scalars(
                select(StudySavedDesign.id).where(
                    StudySavedDesign.study_id == study_id,
                    StudySavedDesign.id.in_(design_ids),
                )
            ).all()
        )
        if len(found) != len(design_ids):
            raise HTTPException(status_code=404, detail="One or more saved designs were not found.")

    created_design: Optional[StudySavedDesign] = None
    if payload.design is not None:
        created_design = _create_saved_design(db, study_id, user_id, payload.design)
        design_ids.append(created_design.id)

    category: Optional[StudyDesignCategory] = None
    if payload.category_id is not None:
        category = db.scalar(
            select(StudyDesignCategory).where(
                StudyDesignCategory.study_id == study_id,
                StudyDesignCategory.id == payload.category_id,
            )
        )
        if category is None:
            raise HTTPException(status_code=404, detail="Category not found.")
    else:
        name = payload.category_name or ""
        normalized = normalize_category_name(name)
        if not normalized:
            raise HTTPException(status_code=400, detail="Category name is required")
        existing = db.scalar(
            select(StudyDesignCategory).where(
                StudyDesignCategory.study_id == study_id,
                StudyDesignCategory.normalized_name == normalized,
            )
        )
        if existing is not None:
            category = existing
        else:
            count = db.scalar(
                select(func.count()).select_from(StudyDesignCategory).where(StudyDesignCategory.study_id == study_id)
            ) or 0
            if count >= MAX_CATEGORIES_PER_STUDY:
                raise HTTPException(status_code=400, detail="This study already has the maximum number of categories.")
            next_position = db.scalar(
                select(func.coalesce(func.max(StudyDesignCategory.position), -1)).where(
                    StudyDesignCategory.study_id == study_id
                )
            )
            category = StudyDesignCategory(
                study_id=study_id,
                created_by_id=user_id,
                name=name,
                normalized_name=normalized,
                position=int(next_position if next_position is not None else -1) + 1,
            )
            db.add(category)
            db.flush()

    _attach_designs(db, category, design_ids)
    created_out = None
    try:
        db.flush()
        if created_design is not None:
            db.refresh(created_design)
            created_out = _saved_design_to_out(created_design)
        category_id = category.id
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That category name is already in use.")

    return {
        "category": get_design_category(db, study_id, category_id),
        "created_design": created_out,
    }


def rename_design_category(
    db: Session,
    study_id: UUID,
    category_id: UUID,
    payload: DesignCategoryRenameRequest,
) -> Dict[str, Any]:
    category = db.scalar(
        select(StudyDesignCategory).where(
            StudyDesignCategory.study_id == study_id,
            StudyDesignCategory.id == category_id,
        )
    )
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found.")

    normalized = normalize_category_name(payload.name)
    clash = db.scalar(
        select(StudyDesignCategory.id).where(
            StudyDesignCategory.study_id == study_id,
            StudyDesignCategory.normalized_name == normalized,
            StudyDesignCategory.id != category_id,
        )
    )
    if clash:
        raise HTTPException(status_code=409, detail="A category with this name already exists.")

    category.name = payload.name
    category.normalized_name = normalized
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A category with this name already exists.")
    return get_design_category(db, study_id, category_id)


def delete_design_category(db: Session, study_id: UUID, category_id: UUID) -> None:
    result = db.execute(
        delete(StudyDesignCategory).where(
            StudyDesignCategory.study_id == study_id,
            StudyDesignCategory.id == category_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Category not found.")
    db.commit()


def remove_design_from_category(
    db: Session,
    study_id: UUID,
    category_id: UUID,
    saved_design_id: UUID,
) -> None:
    category_exists = db.scalar(
        select(StudyDesignCategory.id).where(
            StudyDesignCategory.study_id == study_id,
            StudyDesignCategory.id == category_id,
        )
    )
    if category_exists is None:
        raise HTTPException(status_code=404, detail="Category not found.")
    result = db.execute(
        delete(StudyDesignCategoryItem).where(
            StudyDesignCategoryItem.category_id == category_id,
            StudyDesignCategoryItem.saved_design_id == saved_design_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="That combination is not in this category.")
    db.commit()
