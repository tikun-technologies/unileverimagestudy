"""Video task generation.

Separate from grid generation on purpose. The current algorithm is the grid
Golden Matrix (categories -> elements shown together per task). Replace
`generate_video_tasks_golden` when the video-specific matrix is ready.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.study_model import StudyElement
from app.services.golden_adapter import generate_grid_tasks
from app.services.golden_task_generator import generate_grid_tasks_golden


def _stamp_video_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(result.get("metadata") or {})
    metadata["study_type"] = "video"
    metadata["generator"] = "video_matrix"
    metadata["algorithm"] = "grid_golden_temporary"
    return {**result, "metadata": metadata}


def _use_ready_hls(categories_data: List[Dict[str, Any]]) -> None:
    """Swap raw MP4 URLs for HLS when that video has already finished encoding."""
    from app.db.session import SessionLocal
    from app.services.video_encode_service import playback_url_for

    db = SessionLocal()
    try:
        for category in categories_data:
            for element in category.get("elements") or []:
                content = element.get("content")
                if isinstance(content, str) and content:
                    element["content"] = playback_url_for(db, content)
    finally:
        db.close()


def generate_video_tasks_golden(
    categories_data: List[Dict[str, Any]],
    number_of_respondents: int,
    exposure_tolerance_cv: float,
    seed: Optional[int],
    tasks_per_respondent: int = 0,
    progress_callback=None,
) -> Dict[str, Any]:
    """Category-based video tasks. Same shape as grid tasks, stamped as video."""
    _use_ready_hls(categories_data)
    result = generate_grid_tasks_golden(
        categories_data=categories_data,
        number_of_respondents=number_of_respondents,
        exposure_tolerance_cv=exposure_tolerance_cv,
        seed=seed,
        tasks_per_respondent=tasks_per_respondent,
        progress_callback=progress_callback,
    )
    return _stamp_video_metadata(result)


def generate_video_tasks(
    num_elements: int,
    tasks_per_consumer: Optional[int],
    number_of_respondents: int,
    exposure_tolerance_cv: float,
    seed: Optional[int],
    elements: List[StudyElement],
    db: Session,
    study_id: str,
) -> Dict[str, Any]:
    """DB-backed entry used by regenerate-tasks. Loads categories like grid."""
    from app.services.video_encode_service import playback_url_for

    for element in elements:
        content = getattr(element, "content", None)
        if isinstance(content, str) and content:
            element.content = playback_url_for(db, content)
    result = generate_grid_tasks(
        num_elements=num_elements,
        tasks_per_consumer=tasks_per_consumer,
        number_of_respondents=number_of_respondents,
        exposure_tolerance_cv=exposure_tolerance_cv,
        seed=seed,
        elements=elements,
        db=db,
        study_id=study_id,
    )
    return _stamp_video_metadata(result)
