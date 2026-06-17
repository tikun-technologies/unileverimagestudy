# app/services/golden_adapter.py
"""Study/task-generation adapter backed by Golden Matrix (golden_task_generator)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.study_model import StudyCategory, StudyElement, StudyLayer
from app.services.golden_task_generator import (
    generate_grid_tasks_golden,
    generate_layer_tasks_golden,
)


def generate_grid_tasks(
    num_elements: int,
    tasks_per_consumer: Optional[int],
    number_of_respondents: int,
    exposure_tolerance_cv: float,
    seed: Optional[int],
    elements: List[StudyElement],
    db: Session,
    study_id: str,
) -> Dict[str, Any]:
    tpr = int(tasks_per_consumer) if tasks_per_consumer is not None else 0
    if tpr < 0:
        tpr = 0

    categories = db.scalars(
        select(StudyCategory)
        .options(selectinload(StudyCategory.elements))
        .where(StudyCategory.study_id == study_id)
        .order_by(StudyCategory.order)
    ).all()

    if categories:
        categories_data = [
            {
                "category_name": cat.name,
                "elements": [
                    {
                        "element_id": str(el.element_id),
                        "name": el.name,
                        "content": el.content,
                        "alt_text": el.alt_text or el.name,
                        "element_type": el.element_type,
                    }
                    for el in cat.elements
                ],
            }
            for cat in categories
        ]
    else:
        categories_data = [
            {
                "category_name": "All",
                "elements": [
                    {
                        "element_id": getattr(el, "id", getattr(el, "element_id", None)),
                        "name": str(getattr(el, "name", f"E{i + 1}")),
                        "content": str(getattr(el, "content", "")),
                        "alt_text": str(
                            getattr(el, "alt_text", getattr(el, "name", f"E{i + 1}"))
                        ),
                        "element_type": str(getattr(el, "element_type", "image")),
                    }
                    for i, el in enumerate(elements[:num_elements])
                ],
            }
        ]

    return generate_grid_tasks_golden(
        categories_data=categories_data,
        number_of_respondents=number_of_respondents,
        exposure_tolerance_cv=exposure_tolerance_cv,
        seed=seed,
        tasks_per_respondent=tpr,
    )


def generate_layer_tasks(
    layers: List[StudyLayer],
    number_of_respondents: int,
    exposure_tolerance_pct: float,
    seed: Optional[int],
    tasks_per_consumer: Optional[int] = None,
    design_constraints: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    tpr = int(tasks_per_consumer) if tasks_per_consumer is not None else 0
    if tpr < 0:
        tpr = 0

    layers_data: List[Dict[str, Any]] = []
    for layer in layers:
        layers_data.append(
            {
                "layer_id": str(getattr(layer, "layer_id", getattr(layer, "id", ""))),
                "name": str(getattr(layer, "name")),
                "z_index": int(getattr(layer, "z_index", 0)),
                "order": int(getattr(layer, "order", 0)),
                "images": [
                    {
                        "image_id": str(getattr(img, "image_id", getattr(img, "id", ""))),
                        "name": str(getattr(img, "name")),
                        "url": str(getattr(img, "url", "")),
                        "alt_text": str(getattr(img, "alt_text", "")),
                    }
                    for img in getattr(layer, "images", [])
                ],
            }
        )

    return generate_layer_tasks_golden(
        layers_data=layers_data,
        number_of_respondents=number_of_respondents,
        exposure_tolerance_pct=exposure_tolerance_pct,
        seed=seed,
        tasks_per_respondent=tpr,
        design_constraints=design_constraints,
    )
