"""
Build a study_data dict in the shape expected by synthetic_respondents
(panelist_generator and ai_respondent) from a Study ORM instance.
"""
from __future__ import annotations

from typing import Dict, Any, List, Optional
from uuid import UUID

# Study is typed generically to avoid circular imports; caller passes loaded Study with relationships
def build_study_data_for_synthetic(study: Any, tasks: Optional[Dict[str, Any]] = None, db: Any = None) -> Dict[str, Any]:
    """
    Build a single dict in the shape expected by app.synthetic.panelist_generator
    and app.synthetic.ai_respondent from a loaded Study ORM instance.
    
    The study must have categories, elements, classification_questions, and tasks
    loaded (e.g. via selectinload or access that triggers load).
    
    Returns:
        Dict with keys: id, title, background, main_question, language,
        orientation_text, objectives_text, rating_scale, audience_segmentation,
        classification_questions, categories, elements, tasks.
    """
    study_id = study.id
    id_str = str(study_id) if isinstance(study_id, UUID) else study_id
    
    # Ensure audience_segmentation has gender_distribution and age_distribution
    audience_seg = dict(study.audience_segmentation or {})
    if "gender_distribution" not in audience_seg:
        audience_seg["gender_distribution"] = {"male": 50.0, "female": 50.0}
    if "age_distribution" not in audience_seg:
        audience_seg["age_distribution"] = {
            "18 - 24": 25.0,
            "25 - 34": 25.0,
            "35 - 44": 25.0,
            "45 - 54": 25.0,
        }
    
    # Rating scale: ensure min_label, middle_label, max_label for ai_respondent
    rating_scale = dict(study.rating_scale or {})
    if "min_label" not in rating_scale and "min_value" in rating_scale:
        rating_scale.setdefault("min_label", "Does not appeal")
    if "max_label" not in rating_scale and "max_value" in rating_scale:
        rating_scale.setdefault("max_label", "Strongly appeals")
    if "middle_label" not in rating_scale:
        rating_scale.setdefault("middle_label", "Neutral")
    
    # Classification questions: panelist_generator expects question_id, question_text, order, answer_options with "text"
    classification_questions: List[Dict[str, Any]] = []
    for q in sorted(study.classification_questions or [], key=lambda x: (x.order, x.question_id)):
        opts = q.answer_options or []
        answer_options = []
        for opt in opts:
            if isinstance(opt, dict):
                text = opt.get("text") or opt.get("label") or opt.get("name") or str(opt)
                answer_options.append({"text": text, **opt})
            else:
                answer_options.append({"text": str(opt)})
        classification_questions.append({
            "question_id": q.question_id,
            "question_text": (q.question_text or "")[:500],
            "order": q.order,
            "answer_options": answer_options,
            "optional_classification_question": q.optional_classification_question,
        })
    
    # Categories and elements: grid/text use StudyCategory and StudyElement; layer uses StudyLayer and LayerImage
    categories: List[Dict[str, Any]] = []
    elements: List[Dict[str, Any]] = []
    
    study_type_str = str(study.study_type) if study.study_type else "grid"
    
    if study_type_str == "layer":
        for layer in sorted(study.layers or [], key=lambda x: x.order):
            cat_id = str(layer.layer_id)
            categories.append({
                "id": cat_id,
                "category_id": cat_id,
                "name": layer.name,
                "order": layer.order,
            })
            for img in sorted(layer.images or [], key=lambda x: x.order):
                elements.append({
                    "element_id": str(img.image_id),
                    "id": str(img.id),
                    "name": img.name,
                    "content": img.url,
                    "category_id": cat_id,
                    "element_type": str(layer.layer_type) if layer.layer_type else "image",
                    "category": {"name": layer.name, "order": layer.order},
                    "category_name": layer.name,
                })
    else:
        # grid, text, hybrid: use study_categories and study_elements
        all_elements = list(study.elements or [])
        for cat in sorted(study.categories or [], key=lambda x: x.order):
            cid = str(cat.id)
            categories.append({
                "id": cid,
                "category_id": str(cat.category_id),
                "name": cat.name,
                "order": cat.order,
            })
            cat_elements = [e for e in all_elements if e.category_id == cat.id]
            for el in sorted(cat_elements, key=lambda x: (x.name or "")):
                elements.append({
                    "element_id": str(el.element_id),
                    "id": str(el.id),
                    "name": el.name,
                    "content": el.content or "",
                    "category_id": cid,
                    "element_type": str(el.element_type) if el.element_type else "text",
                    "category": {"name": cat.name, "order": cat.order},
                    "category_name": cat.name,
                })
    
    # Tasks: use provided tasks, or load using TaskService if db is available
    if tasks is None:
        if db is not None:
            try:
                from app.services.task_service import TaskService
                task_service = TaskService(db)
                tasks = task_service.get_all_tasks_as_dict(study.id)
            except Exception:
                tasks = {}
        else:
            # Fallback to legacy study.tasks
            tasks = study.tasks
    if tasks is not None and not isinstance(tasks, dict):
        tasks = {}
    
    return {
        "id": id_str,
        "title": study.title or "",
        "background": study.background or "",
        "main_question": study.main_question or "",
        "language": getattr(study, "language", "en") or "en",
        "orientation_text": study.orientation_text or "",
        "objectives_text": study.orientation_text or "",
        "rating_scale": rating_scale,
        "audience_segmentation": audience_seg,
        "classification_questions": classification_questions,
        "categories": categories,
        "elements": elements,
        "tasks": tasks or {},
    }
