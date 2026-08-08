# app/api/v1/response.py
from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple
from collections import Counter, defaultdict
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
import json
import asyncio
from sqlalchemy.orm import Session
from sqlalchemy import select, text

from app.core.cache import RedisCache, invalidate_study_cache
from app.core.dependencies import get_current_active_user
from app.core.domain import is_unilever_domain, FRAGRANCE_QUESTION_ID
from app.db.session import get_db
from app.models.user_model import User
from app.models.study_model import Study, StudyMember, StudyActiveFilter
from app.models.response_model import StudyResponse, ClassificationAnswer
from app.schemas.response_schema import (
    StudyResponseOut, StudyResponseDetail, StudyResponseListItem,
    StartStudyRequest, StartStudyResponse, SubmitTaskRequest, SubmitTaskResponse,
    BulkSubmitTasksRequest, BulkSubmitTasksResponse,
    SubmitClassificationRequest, SubmitClassificationResponse,
    AbandonStudyRequest, AbandonStudyResponse, UpdateUserDetailsRequest,
    SubmitProductIdRequest, SubmitProductIdResponse,
    SubmitPanelistRequest, SubmitPanelistResponse,
    CheckPanelistParticipationResponse,
    SubmitSyntheticRespondentRequest, SubmitSyntheticRespondentResponse,
    SyntheticRespondentPayload,
    StudyAnalytics, ResponseAnalytics, CompletedTaskOut,
    ClassificationAnswerOut, ElementInteractionOut, TaskSessionOut, TaskSessionCreate,
    ElementInteractionCreate, CompletedTaskCreate, ClassificationAnswerCreate, StudyResponseCreate,
    StudyFilterPayload,
    OptimizedAnalysisPayload,
    ActiveFilterPayload,
    ActiveFilterResponse,
    AnalyticsSessionResponse,
    StudyAnalysisSettingsPayload,
    ClassificationCohortPayload,
    ClassificationCohortResponse,
    SavedFilterReportCreate,
    SavedFilterReportUpdate,
    SavedFilterReportOut,
    FlattenedCsvExportPayload,
)
from app.services.analysis_filter import (
    clear_active_filter,
    filters_are_active,
    get_active_filter,
    save_active_filter,
)
from app.services.saved_filter_reports import (
    create_saved_report,
    delete_saved_report,
    list_saved_reports,
    update_saved_report_name,
)
from app.services.analysis import StudyAnalysisService
from app.services.response import StudyResponseService
from app.services.analysis_settings import (
    get_study_analysis_settings,
    save_study_analysis_settings,
    get_cached_analysis_settings_response,
    build_analysis_settings_response,
    get_max_rating_from_study,
    analysis_settings_cache_key,
    ANALYSIS_SETTINGS_CACHE_TTL,
)
from app.models.study_model import StudyAnalysisSettings

router = APIRouter()


def _authorize_study_access(db: Session, study_id: UUID, user_id: UUID) -> Study:
    from sqlalchemy.orm import defer

    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")

    if study_obj.creator_id == user_id:
        return study_obj

    member = db.scalar(
        select(StudyMember).where(
            StudyMember.study_id == study_id,
            StudyMember.user_id == user_id,
        )
    )
    if not member:
        raise HTTPException(status_code=403, detail="Access denied")
    return study_obj


def _build_study_data_dict(study_obj: Study) -> Dict[str, Any]:
    study_data = {
        "title": study_obj.title,
        "study_type": study_obj.study_type,
        "background": getattr(study_obj, "background_image_url", None) or "",
        "language": study_obj.language,
        "launched_at": study_obj.created_at.isoformat() if study_obj.created_at else "",
        "aspect_ratio": (
            (study_obj.audience_segmentation or {}).get("aspect_ratio")
            if isinstance(study_obj.audience_segmentation, dict)
            else None
        ),
        "categories": [],
        "elements": [],
        "classification_questions": [],
    }

    if str(study_obj.study_type) == "layer":
        sorted_layers = sorted(study_obj.layers, key=lambda x: x.order)
        for layer in sorted_layers:
            cat_id = str(layer.layer_id)
            study_data["categories"].append({
                "id": cat_id,
                "name": layer.name,
                "order": layer.order,
                "z_index": layer.z_index,
                "transform": layer.transform or {},
            })
            for img in sorted(layer.images, key=lambda x: x.order):
                study_data["elements"].append({
                    "id": str(img.image_id),
                    "name": img.name,
                    "content": img.url,
                    "category_id": cat_id,
                    "category": {
                        "name": layer.name,
                        "order": layer.order,
                        "z_index": layer.z_index,
                        "transform": layer.transform or {},
                    },
                    "z_index": layer.z_index,
                    "transform": layer.transform or {},
                    "layer_name": layer.name,
                    "layer_order": layer.order,
                    "image_order": img.order,
                    "alt_text": img.alt_text,
                })
    else:
        for cat in study_obj.categories:
            study_data["categories"].append({
                "id": str(cat.id),
                "name": cat.name,
                "order": cat.order,
            })
            for el in cat.elements:
                study_data["elements"].append({
                    "id": str(el.id),
                    "name": el.name,
                    "content": el.content,
                    "category_id": str(cat.id),
                    "category": {"name": cat.name, "order": cat.order},
                })

    for q in study_obj.classification_questions:
        study_data["classification_questions"].append({
            "question_id": q.question_id,
            "question_text": q.question_text,
            "answer_options": q.answer_options,
            "optional_classification_question": q.optional_classification_question,
        })

    return study_data


def _sanitize_analysis_json(obj: Any) -> Any:
    """Convert numpy/pandas scalars and NaN/Inf to JSON-serializable values."""
    import math
    import numpy as np

    def _json_scalar(val):
        if val is None:
            return None
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            f = float(val)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(val, (np.bool_,)):
            return bool(val)
        if isinstance(val, float):
            return None if (math.isnan(val) or math.isinf(val)) else val
        return val

    if obj is None:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            safe_k = _json_scalar(k) if not isinstance(k, (str, type(None))) else k
            if safe_k is None and k is not None:
                safe_k = str(k)
            out[safe_k] = _sanitize_analysis_json(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_analysis_json(item) for item in obj]
    if isinstance(obj, (np.integer, np.floating, np.bool_)):
        return _json_scalar(obj)
    if isinstance(obj, float):
        return _json_scalar(obj)
    return obj


def _generate_study_analysis_json(
    db: Session,
    study_obj: Study,
    df,
    *,
    include_raw_data: bool,
    filters_dict: Optional[Dict[str, Any]] = None,
    unilever_format: bool = False,
) -> Dict[str, Any]:
    study_data = _build_study_data_dict(study_obj)
    analysis_options = get_study_analysis_settings(db, study_obj.id, study=study_obj)
    analysis_service = StudyAnalysisService()
    json_report = analysis_service.generate_json_report(
        df,
        study_data,
        include_raw_data=include_raw_data,
        analysis_options=analysis_options,
        filters=filters_dict if filters_are_active(filters_dict) else None,
    )
    return _sanitize_analysis_json(json_report)


def _load_study_dataframe_for_analysis(
    db: Session,
    study_id: UUID,
    current_user: User,
):
    unilever_format = is_unilever_domain(current_user.email or "")
    response_service = StudyResponseService(db)
    df = response_service.get_study_dataframe(
        study_id,
        unilever_format=unilever_format,
        completed_only=True,
    )
    return df, unilever_format


def _build_analytics_session(
    db: Session,
    study_obj: Study,
    current_user: User,
) -> Dict[str, Any]:
    study_id = study_obj.id
    active = get_active_filter(db, study_id, current_user.id)
    df, unilever_format = _load_study_dataframe_for_analysis(db, study_id, current_user)
    analysis = _generate_study_analysis_json(
        db,
        study_obj,
        df,
        include_raw_data=False,
        filters_dict=active,
        unilever_format=unilever_format,
    )
    return {
        "study_id": str(study_id),
        "active_filters": active,
        "has_active_filter": filters_are_active(active),
        "analysis": analysis,
    }


def _normalize_text_key(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _extract_answer_option_texts(answer_options: Any) -> List[str]:
    if not isinstance(answer_options, list):
        return []
    out: List[str] = []
    for opt in answer_options:
        if isinstance(opt, dict):
            txt = opt.get("text") or opt.get("label") or opt.get("name")
            if txt:
                out.append(str(txt))
    return out


def _build_answer_options_map(answer_options: Any) -> Dict[str, str]:
    options_map: Dict[str, str] = {}
    if not isinstance(answer_options, list):
        return options_map
    for opt in answer_options:
        if not isinstance(opt, dict):
            continue
        txt = opt.get("text") or opt.get("label") or opt.get("name")
        if not txt:
            continue
        txt_val = str(txt).strip()
        for key in ("id", "value", "code", "label"):
            raw_key = opt.get(key)
            if raw_key is not None:
                options_map[str(raw_key)] = txt_val
    return options_map


def _normalize_answer_value(raw_answer: Any, options_map: Dict[str, str]) -> Optional[str]:
    if raw_answer is None:
        return None
    if isinstance(raw_answer, str):
        raw = raw_answer.strip()
        if not raw:
            return None
        if options_map:
            left = raw.split(" - ", 1)[0]
            right = raw.split(" - ", 1)[1].strip() if " - " in raw else None
            if raw in options_map:
                return options_map[raw]
            if left in options_map:
                return options_map[left]
            if right and right in options_map:
                return options_map[right]
        parts = raw.split(" - ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
        return raw
    return str(raw_answer)


def _clean_demo_label(val: Any) -> Optional[str]:
    """Normalize gender/age labels for JSON and counting (handles numpy nan)."""
    if val is None:
        return None
    try:
        import math
        import numpy as np

        if isinstance(val, (float, np.floating)) and (math.isnan(val) or np.isnan(val)):
            return None
    except Exception:
        pass
    if isinstance(val, str):
        s = val.strip()
        return s if s and s.lower() != "nan" else None
    s = str(val).strip()
    return s if s and s.lower() != "nan" else None


def _extract_demographics(personal_info: Any, analysis_service: StudyAnalysisService) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(personal_info, dict):
        return None, None

    gender_raw = personal_info.get("gender") or personal_info.get("Gender")
    gender_norm = None
    if isinstance(gender_raw, str) and gender_raw.strip():
        gender_norm = _clean_demo_label(analysis_service._normalize_gender(gender_raw))

    age_val = personal_info.get("age")
    if age_val is None:
        dob = (
            personal_info.get("dob")
            or personal_info.get("date_of_birth")
            or personal_info.get("DateOfBirth")
        )
        if isinstance(dob, str) and dob.strip():
            try:
                import pandas as pd

                age_val = int((datetime.now() - pd.to_datetime(dob)).days / 365.25)
            except Exception:
                try:
                    dob_ts = datetime.fromisoformat(dob.strip().replace("Z", "+00:00"))
                    age_val = int((datetime.now(dob_ts.tzinfo) - dob_ts).days / 365.25)
                except Exception:
                    age_val = None

    age_bin = _clean_demo_label(analysis_service._normalize_age_to_bin(age_val))
    return gender_norm, age_bin


def _normalize_cohort_filters(
    filters: Dict[str, Any],
    analysis_service: StudyAnalysisService,
) -> Dict[str, Any]:
    age_vals = filters.get("age_groups") or []
    genders = filters.get("genders") or []
    class_filters = filters.get("classification_filters") or {}

    age_out: List[str] = []
    seen_age = set()
    for age in age_vals:
        if not isinstance(age, str):
            continue
        val = age.strip()
        if val == "13-18":
            val = "13-17"
        if val and val not in seen_age:
            seen_age.add(val)
            age_out.append(val)

    gender_out: List[str] = []
    seen_gender = set()
    for g in genders:
        if not isinstance(g, str):
            continue
        norm_g = analysis_service._normalize_gender(g)
        if norm_g and norm_g not in seen_gender:
            seen_gender.add(norm_g)
            gender_out.append(norm_g)

    class_out: Dict[str, List[str]] = {}
    for q_text, vals in class_filters.items():
        if not isinstance(q_text, str) or not isinstance(vals, list):
            continue
        normalized_vals: List[str] = []
        seen_vals = set()
        for v in vals:
            if not isinstance(v, str):
                continue
            sv = v.strip()
            if sv and sv not in seen_vals:
                seen_vals.add(sv)
                normalized_vals.append(sv)
        if normalized_vals:
            class_out[q_text.strip()] = normalized_vals

    out: Dict[str, Any] = {}
    if age_out:
        out["age_groups"] = age_out
    if gender_out:
        out["genders"] = gender_out
    if class_out:
        out["classification_filters"] = class_out
    return out


def _build_cohort_demographic_breakdown(
    matched_ids: List[Any],
    response_meta: Dict[Any, Dict[str, Any]],
    *,
    age_groups_filter: List[str],
    genders_filter: List[str],
) -> Dict[str, Dict[str, int]]:
    """Count gender/age in cohort; skip dimension when filter already has one value."""
    gender_counts: Counter = Counter()
    age_counts: Counter = Counter()
    for rid in matched_ids:
        meta = response_meta.get(rid) or {}
        g = _clean_demo_label(meta.get("gender"))
        a = _clean_demo_label(meta.get("age_group"))
        if g:
            gender_counts[g] += 1
        if a:
            age_counts[a] += 1

    out: Dict[str, Dict[str, int]] = {}
    if len(gender_counts) > 1 and len(genders_filter) != 1:
        out["gender"] = dict(sorted(gender_counts.items(), key=lambda x: (-x[1], x[0])))
    if len(age_counts) > 1 and len(age_groups_filter) != 1:
        age_order = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]

        def age_sort_key(item: Tuple[str, int]) -> Tuple[int, int, str]:
            label, count = item
            try:
                idx = age_order.index(label)
            except ValueError:
                idx = 999
            return (idx, -count, label)

        out["age_group"] = dict(sorted(age_counts.items(), key=age_sort_key))
    return out


@router.post("/start-study", response_model=StartStudyResponse)
def start_study(
    request: StartStudyRequest,
    http_request: Request,
    db: Session = Depends(get_db)
):
    """
    Start a new study session for a participant.
    This endpoint is public and doesn't require authentication.
    """
    # Extract client information
    ip_address = http_request.client.host if http_request.client else None
    user_agent = http_request.headers.get("user-agent")
    
    service = StudyResponseService(db)
    return service.start_study(request, ip_address, user_agent)


@router.get("/check-panelist-participation", response_model=CheckPanelistParticipationResponse)
def check_panelist_participation(
    study_id: UUID = Query(..., description="Study ID"),
    panelist_id: str = Query(..., min_length=1, max_length=50, description="Panelist ID"),
    db: Session = Depends(get_db),
):
    """
    Ultra-fast check: has this panelist already responded to this study (completed or not)?
    Uses indexed lookup on (study_id, panelist_id). No auth required (public participation flow).
    """
    row = db.execute(
        text(
            "SELECT 1 FROM study_responses "
            "WHERE study_id = :study_id AND panelist_id = :panelist_id LIMIT 1"
        ),
        {"study_id": str(study_id), "panelist_id": panelist_id.strip()},
    ).first()
    participated = row is not None
    if participated:
        return CheckPanelistParticipationResponse(
            ok=True,
            participated=True,
            message="This panelist has already responded to this study.",
        )
    return CheckPanelistParticipationResponse(ok=True, participated=False)


@router.post("/submit-task", response_model=SubmitTaskResponse)
def submit_task(
    session_id: str,
    request: SubmitTaskRequest,
    db: Session = Depends(get_db)
):
    """
    Submit a completed task for a study session.
    """
    service = StudyResponseService(db)
    result = service.submit_task(session_id, request)
    
    # Invalidate analytics caches since we have a new task submission
    # We need to get the study_id from the session to invalidate the cache
    try:
        response = service.get_response_detail_by_session(session_id)
        if response and response.study_id:
            invalidate_study_cache(response.study_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to invalidate cache after task submission: {e}")
        
    return result

@router.post("/submit-tasks-bulk", response_model=BulkSubmitTasksResponse)
def submit_tasks_bulk(
    session_id: str,
    request: BulkSubmitTasksRequest,
    db: Session = Depends(get_db)
):
    """
    Submit multiple completed tasks for a study session in one request.
    Tasks are applied in the order provided; progress and completion are updated accordingly.
    """
    service = StudyResponseService(db)
    result = service.submit_tasks_bulk(session_id, request)
    
    # Invalidate analytics caches
    try:
        response = service.get_response_detail_by_session(session_id)
        if response and response.study_id:
            invalidate_study_cache(response.study_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to invalidate cache after bulk task submission: {e}")
        
    return result

@router.post("/submit-classification", response_model=SubmitClassificationResponse)
def submit_classification(
    session_id: str,
    request: SubmitClassificationRequest,
    db: Session = Depends(get_db)
):
    """
    Submit classification answers for a study session.
    """
    service = StudyResponseService(db)
    success = service.submit_classification(session_id, request)
    
    # Invalidate analytics caches
    if success:
        try:
            response = service.get_response_detail_by_session(session_id)
            if response and response.study_id:
                invalidate_study_cache(response.study_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to invalidate cache after classification submission: {e}")
    
    return SubmitClassificationResponse(
        success=success,
        message="Classification answers submitted successfully" if success else "Failed to submit answers"
    )

@router.post("/submit-synthetic-respondent", response_model=SubmitSyntheticRespondentResponse)
async def submit_synthetic_respondent(
    http_request: Request,
    db: Session = Depends(get_db)
):
    """
    Store one synthetic respondent: classification answers and task ratings.
    Accepts body as object or as single-element array (e.g. [{ "ready_for_selenium": {...} }]).
    Accepts either: (study_id + payload) or ready_for_selenium { study_id, classification_answers, task_ratings, rating_scale? }.
    Uses the same storage as submit-classification and submit-tasks-bulk (no DB changes).
    """
    body = await http_request.json()
    # Client may send array with one object; unwrap so we expect a single object
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    request = SubmitSyntheticRespondentRequest.model_validate(body)
    service = StudyResponseService(db)
    if request.ready_for_selenium is not None:
        r = request.ready_for_selenium
        payload = SyntheticRespondentPayload(
            panelist_id=r.panelist_id or "selenium_1",
            panelist_number=r.panelist_number if r.panelist_number is not None else 1,
            classification_answers=r.classification_answers,
            task_ratings=r.task_ratings,
        )
        study_id = r.study_id
    else:
        study_id = request.study_id
        payload = request.payload
    result = await asyncio.to_thread(service.submit_synthetic_respondent, study_id, payload)

    # Invalidate analytics caches
    invalidate_study_cache(study_id)
    
    return SubmitSyntheticRespondentResponse(**result)

@router.post("/abandon-study", response_model=AbandonStudyResponse)
def abandon_study(
    session_id: str,
    request: AbandonStudyRequest,
    db: Session = Depends(get_db)
):
    """
    Mark a study session as abandoned.
    """
    service = StudyResponseService(db)
    success = service.abandon_study(session_id, request)
    
    return AbandonStudyResponse(
        success=success,
        message="Study marked as abandoned" if success else "Failed to abandon study"
    )

@router.get("/session/{session_id}/status")
def get_session_status(
    session_id: str,
    db: Session = Depends(get_db)
):
    """Lightweight status check - returns only completion state."""
    service = StudyResponseService(db)
    response = service.get_response_by_session(session_id)
    if not response:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "is_completed": response.is_completed,
        "completed_tasks_count": response.completed_tasks_count,
        "total_tasks_assigned": response.total_tasks_assigned,
    }

@router.get("/session/{session_id}", response_model=StudyResponseDetail)
def get_session(
    session_id: str,
    db: Session = Depends(get_db)
):
    """
    Get study session details by session ID.
    """
    service = StudyResponseService(db)
    response = service.get_response_detail_by_session(session_id)
    
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Session not found"
        )
    
    # Get study layer information with z-index for layer studies
    study_layers = []
    if response.study_id:
        from app.models.study_model import StudyLayer, LayerImage
        from sqlalchemy.orm import selectinload
        
        # Get layers with images and z-index information
        layers = db.execute(
            select(StudyLayer)
            .options(selectinload(StudyLayer.images))
            .where(StudyLayer.study_id == response.study_id)
            .order_by(StudyLayer.order)
        ).scalars().all()
        
        for layer in layers:
            layer_images = []
            for image in layer.images:
                layer_images.append({
                    "id": str(image.id),
                    "name": image.name,
                    "url": image.url,
                    "alt_text": image.alt_text,
                    "order": image.order,
                    "z_index": layer.z_index  # Use layer's z_index for all images in this layer
                })
            
            study_layers.append({
                "id": str(layer.id),
                "name": layer.name,
                "order": layer.order,
                "z_index": layer.z_index,
                "images": layer_images
            })
    
    # Add layer information to response
    response_dict = StudyResponseDetail.model_validate(response).model_dump()
    response_dict["study_layers"] = study_layers
    # Add optional study background image url
    try:
        if response and getattr(response, "study_id", None):
            from app.models.study_model import Study as StudyModel
            study_row = db.execute(
                select(StudyModel).where(StudyModel.id == response.study_id)
            ).scalar_one_or_none()
            if study_row is not None:
                response_dict["background_image_url"] = getattr(study_row, "background_image_url", None)
    except Exception:
        response_dict["background_image_url"] = None

    # Enrich completed tasks with elements_shown_content from task assignments if missing
    try:
        if response_dict.get("completed_tasks") and response_dict.get("study_id"):
            from app.services.task_service import TaskService
            task_service = TaskService(db)
            resp_id = response_dict.get("respondent_id")
            respondent_tasks = task_service.get_respondent_tasks(response.study_id, resp_id)
            
            index_to_content = {
                int(t.get("task_index")): t.get("elements_shown_content")
                for t in (respondent_tasks or []) if isinstance(t, dict)
            }
            for ct in response_dict.get("completed_tasks", []):
                if ct.get("elements_shown_content") is None:
                    task_index = ct.get("task_index")
                    if task_index in index_to_content:
                        ct["elements_shown_content"] = index_to_content[task_index]
    except Exception:
        # Non-fatal enrichment
        pass

    # Enrich layer study completed_tasks with transform, z_index, alt_text, layer_name (for synthetic or minimal content)
    try:
        if response_dict.get("completed_tasks") and response_dict.get("study_id"):
            from app.models.study_model import StudyLayer
            from sqlalchemy.orm import selectinload
            layers = db.execute(
                select(StudyLayer)
                .options(selectinload(StudyLayer.images))
                .where(StudyLayer.study_id == response.study_id)
                .order_by(StudyLayer.order)
            ).scalars().all()
            if layers:
                default_transform = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
                layer_meta: Dict[str, Dict[str, Any]] = {}
                name_to_alt: Dict[str, str] = {}
                for L in layers:
                    layer_meta[L.name] = {
                        "z_index": L.z_index,
                        "transform": L.transform if L.transform else default_transform,
                    }
                    for img in (L.images or []):
                        name_to_alt[img.name or ""] = (img.alt_text or "")
                for ct in response_dict.get("completed_tasks", []):
                    esc = ct.get("elements_shown_content")
                    if not isinstance(esc, dict):
                        continue
                    for key, val in esc.items():
                        if not isinstance(val, dict):
                            continue
                        # Parse "LayerName_Index" from key
                        layer_name = key.rsplit("_", 1)[0] if "_" in key else None
                        if layer_name and layer_name in layer_meta:
                            meta = layer_meta[layer_name]
                            if val.get("transform") is None:
                                val["transform"] = meta["transform"]
                            if val.get("z_index") is None:
                                val["z_index"] = meta["z_index"]
                            if val.get("layer_name") is None:
                                val["layer_name"] = layer_name
                            if val.get("alt_text") is None:
                                img_name = val.get("name")
                                val["alt_text"] = name_to_alt.get(img_name or "", "") or ""

    except Exception:
        pass

    # Map classification answer codes to human-readable labels using study configuration
    try:
        if response and getattr(response, "study_id", None):
            from app.models.study_model import StudyClassificationQuestion
            from app.models.study_model import Study as StudyModel
            # Build options map per question
            questions = db.execute(
                select(StudyClassificationQuestion)
                .where(StudyClassificationQuestion.study_id == response.study_id)
                .order_by(StudyClassificationQuestion.order)
            ).scalars().all()
            qid_to_options: Dict[str, Dict[str, str]] = {}
            for q in questions:
                options_map: Dict[str, str] = {}
                if isinstance(q.answer_options, list):
                    for opt in q.answer_options:
                        if not isinstance(opt, dict):
                            continue
                        text = opt.get("text") or opt.get("label") or opt.get("name")
                        if text is None:
                            continue
                        for key_name in ("id", "value", "code", "label"):
                            if key_name in opt and opt[key_name] is not None:
                                options_map[str(opt[key_name])] = text
                if options_map:
                    qid_to_options[q.question_id] = options_map

            # Convert ORM -> Pydantic dict
            resp_out = StudyResponseDetail.model_validate(response).model_dump()
            # Add layer information to the response
            resp_out["study_layers"] = study_layers
            # Ensure background_image_url is included (may be None)
            try:
                study_row2 = db.execute(
                    select(StudyModel).where(StudyModel.id == response.study_id)
                ).scalar_one_or_none()
                resp_out["background_image_url"] = getattr(study_row2, "background_image_url", None) if study_row2 else None
            except Exception:
                resp_out["background_image_url"] = None
            # Transform answers
            for ans in resp_out.get("classification_answers", []) or []:
                qid = ans.get("question_id")
                raw = ans.get("answer")
                mapped = raw
                try:
                    options_map = qid_to_options.get(qid)
                    if isinstance(raw, str) and options_map:
                        left_code = raw.split(' - ', 1)[0]
                        right_part = raw.split(' - ', 1)[1].strip() if ' - ' in raw else None
                        if raw in options_map:
                            mapped = options_map[raw]
                        elif left_code in options_map:
                            mapped = options_map[left_code]
                        elif right_part and right_part in options_map:
                            mapped = options_map[right_part]
                        else:
                            parts = raw.split(' - ', 1)
                            if len(parts) == 2 and parts[1].strip():
                                mapped = parts[1].strip()
                    elif isinstance(raw, str):
                        parts = raw.split(' - ', 1)
                        if len(parts) == 2 and parts[1].strip():
                            mapped = parts[1].strip()
                except Exception:
                    mapped = raw
                ans["answer"] = mapped
            # Carry over enriched completed_tasks (transform, z_index, etc.) from response_dict
            resp_out["completed_tasks"] = response_dict.get("completed_tasks", resp_out.get("completed_tasks"))
            return resp_out
    except Exception:
        # Fallback to raw response if mapping fails
        pass

    return response_dict

@router.put("/session/{session_id}/user-details")
def update_user_details(
    session_id: str,
    request: UpdateUserDetailsRequest,
    db: Session = Depends(get_db)
):
    """
    Update user details for a study session.
    This endpoint is public and doesn't require authentication.
    """
    service = StudyResponseService(db)
    user_details_dict = request.user_details.model_dump(exclude_none=True)
    success = service.update_user_details(session_id, user_details_dict)
    
    return {
        "success": success,
        "message": "User details updated successfully" if success else "Failed to update user details"
    }

@router.post("/session/{session_id}/product-id", response_model=SubmitProductIdResponse)
def submit_product_id(
    session_id: str,
    request: SubmitProductIdRequest,
    db: Session = Depends(get_db)
):
    """
    Submit a product ID for a study session.
    This endpoint is public and doesn't require authentication.
    """
    service = StudyResponseService(db)
    success = service.submit_product_id(session_id, request.product_id)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail="Session not found"
        )
    
    return SubmitProductIdResponse(
        success=True,
        message="Product ID submitted successfully"
    )

@router.post("/session/{session_id}/panelist", response_model=SubmitPanelistResponse)
def submit_panelist(
    session_id: str,
    request: SubmitPanelistRequest,
    db: Session = Depends(get_db)
):
    """
    Submit a panelist ID for a study session.
    This endpoint is public and doesn't require authentication.
    """
    service = StudyResponseService(db)
    panelist_info = service.submit_panelist_info(session_id, request.panelist_id)
    
    if not panelist_info:
        raise HTTPException(
            status_code=404,
            detail="Session or Panelist not found"
        )
    
    return SubmitPanelistResponse(
        success=True,
        panelist_age=panelist_info["age"],
        panelist_gender=panelist_info["gender"],
        message="Panelist information submitted successfully"
    )

# ---------- Study Response Management (Authenticated) ----------

@router.get("/", response_model=List[StudyResponseListItem])
def list_responses(
    study_id: Optional[UUID] = Query(None, description="Filter by study ID"),
    is_completed: Optional[bool] = Query(None, description="Filter by completion status"),
    is_abandoned: Optional[bool] = Query(None, description="Filter by abandonment status"),
    limit: int = Query(100, ge=1, le=10000, description="Number of responses to return"),
    offset: int = Query(0, ge=0, description="Number of responses to skip"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    List study responses with optional filtering.
    Only returns responses for studies owned by the current user.
    """
    service = StudyResponseService(db)
    
    if study_id:
        # Optimized: lightweight ownership check
        # Optimized: ownership/membership check
        from sqlalchemy import select
        from app.models.study_model import Study, StudyMember, StudyActiveFilter
        
        # Check if user is creator
        is_owner = db.scalar(
            select(Study.id).where(Study.id == study_id, Study.creator_id == current_user.id)
        )
        
        is_member = False
        if not is_owner:
            # Check if user is member
            is_member = db.scalar(
                select(StudyMember.id).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == current_user.id
                )
            )
        
        if not is_owner and not is_member:
            raise HTTPException(
                status_code=403,
                detail="Access denied to this study"
            )
        
        # Use SQL-level filtering for better performance
        responses = service.get_responses_by_study_filtered(
            study_id, limit, offset, is_completed, is_abandoned
        )
    else:
        # Get all studies owned by user and their responses
        from app.services import study as study_service
        user_studies, _, _ = study_service.list_studies(db, current_user.id, page=1, per_page=200)
        study_ids = [study.id for study in user_studies]
        
        if not study_ids:
            return []
        
        # Get responses for all user's studies with SQL-level filtering
        responses = []
        for study_id in study_ids:
            study_responses = service.get_responses_by_study_filtered(
                study_id, limit, offset, is_completed, is_abandoned
            )
            responses.extend(study_responses)
    
    return responses

@router.get("/{response_id}", response_model=StudyResponseDetail)
def get_response(
    response_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get detailed information about a specific study response.
    """
    service = StudyResponseService(db)
    response = service.get_response(response_id)
    
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Response not found"
        )
    
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=response.study_id, owner_id=current_user.id)
    
    return response

@router.delete("/{response_id}")
def delete_response(
    response_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Delete a study response.
    """
    service = StudyResponseService(db)
    response = service.get_response(response_id)
    
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Response not found"
        )
    
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=response.study_id, owner_id=current_user.id)
    
    success = service.delete_response(response_id)
    
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to delete response"
        )
    
    return {"message": "Response deleted successfully"}


@router.delete("/study/{study_id}/session/{session_id}")
def delete_response_by_study_session(
    study_id: UUID,
    session_id: str,
    db: Session = Depends(get_db),
):
    """
    Delete one response by study ID + session ID.
    Public endpoint with no ownership/member checks.
    """
    service = StudyResponseService(db)
    deleted = service.delete_response_by_study_and_session(study_id, session_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Response for this session_id in the study was not found",
        )

    invalidate_study_cache(study_id)
    return {
        "message": "Session response cleared successfully",
        "study_id": str(study_id),
        "session_id": session_id,
    }

# ---------- Analytics Endpoints ----------

@router.get("/analytics/study/{study_id}", response_model=StudyAnalytics)
def get_study_analytics(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get analytics data for a study - optimized for fast performance with rate limiting.
    """
    import time
    start_time = time.time()
    
    # Fast ownership verification - only check creator_id, don't load full study
    # Ownership/Membership verification
    from sqlalchemy import select
    from app.models.study_model import Study, StudyMember, StudyActiveFilter
    
    ownership_check = select(Study.creator_id).where(Study.id == study_id)
    result = db.execute(ownership_check).first()
    
    is_authorized = False
    if result and result.creator_id == current_user.id:
        is_authorized = True
    elif result:
        # Check membership
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id
            )
        )
        if member:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(
            status_code=404,
            detail="Study not found or access denied"
        )
    
    # No rate limiting - allow real-time updates
    
    # Check cache first
    cache_key = f"study_analytics:{study_id}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        end_time = time.time()
        print(f"Analytics cache hit took: {(end_time - start_time)*1000:.2f}ms")
        return cached_data
    
    service = StudyResponseService(db)
    analytics = service.get_study_analytics(study_id)
    
    # Cache the result
    RedisCache.set(cache_key, analytics.model_dump(), ttl_seconds=60)
    
    end_time = time.time()
    print(f"Analytics query took: {(end_time - start_time)*1000:.2f}ms")
    
    return analytics


@router.get("/analytics/study/{study_id}/stream")
def stream_study_analytics(
    study_id: UUID,
    interval_seconds: int = Query(10, ge=5, le=60),  # Increased minimum for scalability
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Server-Sent Events (SSE) stream of study analytics.
    Emits an event every `interval_seconds` with the same payload shape
    as /analytics/study/{study_id}.
    Optimized for scalability with 100+ users.
    """
    # Ownership check (fast)
    # Ownership/Membership verification
    from sqlalchemy import select
    from app.models.study_model import Study, StudyMember, StudyActiveFilter
    ownership_check = select(Study.creator_id).where(Study.id == study_id)
    result = db.execute(ownership_check).first()
    
    is_authorized = False
    if result and result.creator_id == current_user.id:
        is_authorized = True
    elif result:
        # Check membership
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id
            )
        )
        if member:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(
            status_code=404,
            detail="Study not found or access denied"
        )

    service = StudyResponseService(db)

    async def event_generator():
        last_payload: str | None = None
        request_count = 0
        
        while True:
            # Smart caching: Use cache for most requests, refresh occasionally
            cache_key = f"study_analytics:{study_id}"
            if request_count % 2 == 0:  # Refresh every 2nd request (every 20 seconds with 10s interval)
                analytics = await asyncio.to_thread(service.get_study_analytics, study_id)
                RedisCache.set(cache_key, analytics.model_dump(), ttl_seconds=60)
            else:
                # Use cached data for faster response
                cached_data = RedisCache.get(cache_key)
                if cached_data:
                    analytics = StudyAnalytics(**cached_data)
                else:
                    analytics = await asyncio.to_thread(service.get_study_analytics, study_id)
                    RedisCache.set(cache_key, analytics.model_dump(), ttl_seconds=60)
            
            payload = json.dumps(analytics.model_dump())
            # Only send when changed to reduce client work
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            
            request_count += 1
            await asyncio.sleep(interval_seconds)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/analytics/response/{response_id}", response_model=ResponseAnalytics)
def get_response_analytics(
    response_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get detailed analytics for a specific response.
    """
    service = StudyResponseService(db)
    response = service.get_response(response_id)
    
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Response not found"
        )
    
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=response.study_id, owner_id=current_user.id)
    
    analytics = service.get_response_analytics(response_id)
    if not analytics:
        raise HTTPException(
            status_code=404,
            detail="Analytics not found"
        )
    
    return analytics


@router.post("/check-abandoned-sessions")
def check_abandoned_sessions(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Manually trigger check for sessions that should be marked as abandoned (60+ minutes inactive).
    This endpoint can be called periodically or by a background task.
    """
    service = StudyResponseService(db)
    count = service.check_and_mark_abandoned_sessions()
    
    return {
        "message": f"Checked for abandoned sessions",
        "sessions_marked_abandoned": count,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.post("/check-study-completion")
def check_study_completion(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Manually trigger check for studies that should be auto-completed.
    This checks if all expected respondents have completed or abandoned their sessions.
    """
    service = StudyResponseService(db)
    service._check_and_complete_studies()
    
    return {
        "message": "Study completion check completed",
        "status": "success"
    }

# ---------- Task Session Endpoints ----------

@router.post("/task-sessions/", response_model=TaskSessionOut)
def create_task_session(
    session_data: TaskSessionCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new task session. Public, no authentication required.
    """
    service = TaskSessionService(db)
    return service.create_task_session(session_data)

@router.get("/task-sessions/{session_id}/{task_id}", response_model=TaskSessionOut)
def get_task_session(
    session_id: str,
    task_id: str,
    db: Session = Depends(get_db)
):
    """
    Get a task session by session ID and task ID. Public, no authentication required.
    """
    service = TaskSessionService(db)
    task_session = service.get_task_session(session_id, task_id)
    
    if not task_session:
        raise HTTPException(
            status_code=404,
            detail="Task session not found"
        )
    
    return task_session

@router.post("/task-sessions/{session_id}/{task_id}/page-transition")
def add_page_transition(
    session_id: str,
    task_id: str,
    page_name: str,
    db: Session = Depends(get_db)
):
    """
    Add a page transition to a task session. Public, no authentication required.
    """
    service = TaskSessionService(db)
    success = service.add_page_transition(session_id, task_id, page_name)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail="Task session not found"
        )
    
    return {"message": "Page transition added successfully"}

@router.post("/task-sessions/{session_id}/{task_id}/element-interaction")
def add_element_interaction(
    session_id: str,
    task_id: str,
    interaction_data: ElementInteractionCreate,
    db: Session = Depends(get_db)
):
    """
    Add an element interaction to a task session. Public, no authentication required.
    """
    service = TaskSessionService(db)
    success = service.add_element_interaction(session_id, task_id, interaction_data)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail="Task session not found"
        )
    
    return {"message": "Element interaction added successfully"}

# ---------- Export Endpoints ----------

@router.get("/export/study/{study_id}/responses")
def export_study_responses(
    study_id: UUID,
    format: str = Query("csv", regex="^(csv|json)$", description="Export format"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Export all responses for a study in CSV or JSON format.
    """
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    
    service = StudyResponseService(db)
    responses = service.get_responses_by_study(study_id, limit=10000)  # Large limit for export
    
    if format == "csv":
        # Generate CSV export
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            "Response ID", "Session ID", "Respondent ID", "Is Completed", 
            "Is Abandoned", "Completion Percentage", "Total Duration",
            "Session Start", "Session End", "Last Activity"
        ])
        
        # Write data
        for response in responses:
            writer.writerow([
                str(response.id),
                response.session_id,
                response.respondent_id,
                response.is_completed,
                response.is_abandoned,
                response.completion_percentage,
                response.total_study_duration,
                response.session_start_time.isoformat() if response.session_start_time else "",
                response.session_end_time.isoformat() if response.session_end_time else "",
                response.last_activity.isoformat() if response.last_activity else ""
            ])
        
        content = output.getvalue()
        output.close()
        
        return {
            "content": content,
            "filename": f"study_{study_id}_responses.csv",
            "content_type": "text/csv"
        }
    
    else:  # JSON format
        return {
            "study_id": str(study_id),
            "study_title": study.title,
            "export_timestamp": datetime.utcnow().isoformat(),
            "total_responses": len(responses),
            "responses": [StudyResponseOut.model_validate(response) for response in responses]
        }

@router.get("/export/response/{response_id}/detailed")
def export_response_detailed(
    response_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Export detailed data for a specific response.
    """
    service = StudyResponseService(db)
    response = service.get_response(response_id)
    
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Response not found"
        )
    
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=response.study_id, owner_id=current_user.id)
    
    # Get detailed response data
    detailed_response = service.get_response(response_id)
    
    return {
        "response": StudyResponseDetail.model_validate(detailed_response),
        "analytics": service.get_response_analytics(response_id),
        "export_timestamp": datetime.utcnow().isoformat()
    }


@router.get("/export/study/{study_id}/flattened-csv2")
def export_study_flattened_csv(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Export a flattened CSV where each row is a respondent-task with columns:
    Panelist(session_id), QQ* classification answers, Gender, Age, Task, Layer_* visibility flags, Rating, ResponseTime.
    """
    # Verify user owns the study
    from app.services import study as study_service
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)

    service = StudyResponseService(db)

    def csv_generator():
        for chunk in service.generate_csv_rows_for_study_pandas(study_id):
            yield chunk

    filename = f"study_{study_id}_flattened_export.csv"
    headers = {
        "Content-Disposition": f"attachment; filename={filename}"
    }
    return StreamingResponse(csv_generator(), media_type="text/csv", headers=headers)

def _generate_study_excel_export_response(
    db: Session,
    study_obj: Study,
    current_user: User,
    filters_dict: Optional[Dict[str, Any]] = None,
) -> StreamingResponse:
    """Build the full Excel analysis report, optionally filtered to a respondent cohort."""
    study_id = study_obj.id
    unilever_format = is_unilever_domain(current_user.email or "")
    response_service = StudyResponseService(db)
    df = response_service.get_study_dataframe(
        study_id,
        unilever_format=unilever_format,
        completed_only=True,
    )
    study_data = _build_study_data_dict(study_obj)
    analysis_options = get_study_analysis_settings(db, study_id, study=study_obj)
    analysis_service = StudyAnalysisService()
    has_filters = filters_are_active(filters_dict)

    try:
        excel_file = analysis_service.generate_report(
            df,
            study_data,
            analysis_options=analysis_options,
            filters=filters_dict if has_filters else None,
        )
    except ValueError as e:
        if "No respondents match the applied filters." in str(e):
            raise HTTPException(status_code=404, detail=str(e)) from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"Analysis generation failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to generate analysis report: {str(e)}")

    filename = (
        f"study_{study_id}_filtered_analysis.xlsx"
        if has_filters
        else f"study_{study_id}_analysis.xlsx"
    )
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/export/study/{study_id}/flattened-csv")
def export_study_analysis(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Export a comprehensive Excel report with regression analysis, segmentation, and clustering.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    return _generate_study_excel_export_response(db, study_obj, current_user)


@router.post("/export/study/{study_id}/flattened-csv")
def export_study_analysis_filtered(
    study_id: UUID,
    payload: FlattenedCsvExportPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Export the full Excel analysis report for a filtered respondent cohort.
    Uses the same filter logic as the analytics page.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    filters_dict = payload.filters.model_dump(exclude_none=True) if payload.filters else None
    if not filters_are_active(filters_dict):
        return _generate_study_excel_export_response(db, study_obj, current_user)
    return _generate_study_excel_export_response(
        db,
        study_obj,
        current_user,
        filters_dict=filters_dict,
    )

def _authorize_study_for_analysis(db: Session, study_id: UUID, current_user: User) -> Study:
    from sqlalchemy.orm import defer

    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")

    if study_obj.creator_id != current_user.id:
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id,
            )
        )
        if not member:
            raise HTTPException(status_code=403, detail="Access denied")
    return study_obj


@router.get("/study/{study_id}/analysis-json")
def export_study_analysis_json(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    include_raw_data: bool = True,
):
    """
    Export a comprehensive JSON report with regression analysis, segmentation, and clustering.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    unilever_format = is_unilever_domain(current_user.email or "")
    response_service = StudyResponseService(db)
    df = response_service.get_study_dataframe(
        study_id,
        unilever_format=unilever_format,
        completed_only=True,
    )
    try:
        return _generate_study_analysis_json(
            db,
            study_obj,
            df,
            include_raw_data=include_raw_data,
            unilever_format=unilever_format,
        )
    except Exception as e:
        print(f"JSON Analysis generation failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to generate JSON analysis report: {str(e)}")


@router.get("/study/{study_id}/analysis-settings")
def get_study_analysis_settings_endpoint(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Return saved analysis settings for a study, or defaults if none saved."""
    study_obj = _authorize_study_access(db, study_id, current_user.id)
    return get_cached_analysis_settings_response(db, study_id, study=study_obj)


@router.put("/study/{study_id}/analysis-settings")
def save_study_analysis_settings_endpoint(
    study_id: UUID,
    payload: StudyAnalysisSettingsPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Save analysis settings for a study (rating mappings + intercept mode)."""
    study_obj = _authorize_study_access(db, study_id, current_user.id)
    settings_dict = payload.model_dump()
    save_study_analysis_settings(
        db,
        study_id,
        settings_dict,
        current_user.id,
        study=study_obj,
    )
    response = build_analysis_settings_response(db, study_id, study=study_obj)
    RedisCache.set(analysis_settings_cache_key(study_id), response, ttl_seconds=ANALYSIS_SETTINGS_CACHE_TTL)
    return response


@router.get("/study/{study_id}/analytics-session")
def get_study_analytics_session(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Single bootstrap call for the analytics page: returns the user's saved
    active filter (if any) and freshly computed optimized analysis JSON.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    try:
        return _build_analytics_session(db, study_obj, current_user)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load analytics session: {str(e)}",
        )


@router.get("/study/{study_id}/active-filter")
def get_study_active_filter(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Return the persisted active filter for this user on the study (Redis-backed)."""
    _authorize_study_for_analysis(db, study_id, current_user)
    active = get_active_filter(db, study_id, current_user.id)
    row = (
        db.query(StudyActiveFilter)
        .filter(
            StudyActiveFilter.study_id == study_id,
            StudyActiveFilter.user_id == current_user.id,
        )
        .first()
    )
    return {
        "study_id": str(study_id),
        "filters": active,
        "has_active_filter": filters_are_active(active),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.post("/study/{study_id}/active-filter")
def save_study_active_filter(
    study_id: UUID,
    payload: ActiveFilterPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Save the user's active analytics filter and return filtered analysis JSON.
    Empty filters clears the active filter and returns full-study analysis.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    filters_dict = payload.filters.model_dump(exclude_none=True) if payload.filters else None

    if filters_are_active(filters_dict):
        saved = save_active_filter(db, study_id, current_user.id, filters_dict)
    else:
        clear_active_filter(db, study_id, current_user.id)
        saved = None

    df, unilever_format = _load_study_dataframe_for_analysis(db, study_id, current_user)
    try:
        analysis = _generate_study_analysis_json(
            db,
            study_obj,
            df,
            include_raw_data=False,
            filters_dict=saved,
            unilever_format=unilever_format,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate analysis for active filter: {str(e)}",
        )

    row = (
        db.query(StudyActiveFilter)
        .filter(
            StudyActiveFilter.study_id == study_id,
            StudyActiveFilter.user_id == current_user.id,
        )
        .first()
    )
    return {
        "study_id": str(study_id),
        "filters": saved,
        "has_active_filter": filters_are_active(saved),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "analysis": analysis,
    }


@router.delete("/study/{study_id}/active-filter")
def reset_study_active_filter(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Clear saved active filter and return full-study optimized analysis."""
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    clear_active_filter(db, study_id, current_user.id)

    df, unilever_format = _load_study_dataframe_for_analysis(db, study_id, current_user)
    try:
        analysis = _generate_study_analysis_json(
            db,
            study_obj,
            df,
            include_raw_data=False,
            filters_dict=None,
            unilever_format=unilever_format,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset analytics filter: {str(e)}",
        )

    return {
        "study_id": str(study_id),
        "filters": None,
        "has_active_filter": False,
        "updated_at": None,
        "analysis": analysis,
    }


@router.get("/study/{study_id}/optimized-analysis-json")
def export_study_optimized_analysis_json(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Export analytics JSON for the frontend without the heavy RawData payload.
    Raw-derived overview widgets read the compact dashboard_summary instead.
    """
    return export_study_analysis_json(
        study_id=study_id,
        current_user=current_user,
        db=db,
        include_raw_data=False,
    )


@router.post("/study/{study_id}/optimized-analysis-json")
def post_study_optimized_analysis_json(
    study_id: UUID,
    payload: OptimizedAnalysisPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Export analytics JSON for the frontend, optionally filtered by age, gender,
    and classification question answers. Returns the same shape as GET
    optimized-analysis-json, with filters_applied / filter_meta when filtered.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    filters_dict = payload.filters.model_dump(exclude_none=True) if payload.filters else None
    has_filters = filters_are_active(filters_dict)

    if has_filters:
        save_active_filter(db, study_id, current_user.id, filters_dict)
    else:
        clear_active_filter(db, study_id, current_user.id)

    df, unilever_format = _load_study_dataframe_for_analysis(db, study_id, current_user)

    try:
        json_report = _generate_study_analysis_json(
            db,
            study_obj,
            df,
            include_raw_data=False,
            filters_dict=filters_dict if has_filters else None,
            unilever_format=unilever_format,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate filtered analysis JSON: {str(e)}",
        )

    if has_filters and payload.save_to_history:
        try:
            from app.models.study_model import StudyFilterHistory
            record = StudyFilterHistory(
                study_id=study_id,
                user_id=current_user.id,
                filters=filters_dict or {},
                name=payload.name[:255] if payload.name else None,
            )
            db.add(record)
            db.commit()
        except Exception:
            db.rollback()

    return json_report


@router.post("/study/{study_id}/classification-cohort", response_model=ClassificationCohortResponse)
def get_classification_cohort(
    study_id: UUID,
    payload: ClassificationCohortPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Drill-down endpoint for Prelim option clicks.
    Returns only respondent-level classification profiles for the selected cohort.
    """
    study_obj = _authorize_study_for_analysis(db, study_id, current_user)
    analysis_service = StudyAnalysisService()
    raw_filters = payload.filters.model_dump(exclude_none=True) if payload.filters else {}
    normalized_filters = _normalize_cohort_filters(raw_filters, analysis_service)
    age_groups = normalized_filters.get("age_groups") or []
    genders = normalized_filters.get("genders") or []
    class_filters = normalized_filters.get("classification_filters") or {}

    questions_sorted = sorted(
        list(study_obj.classification_questions or []),
        key=lambda q: (q.order if q.order is not None else 0, q.question_text or ""),
    )
    if not questions_sorted:
        raise HTTPException(status_code=400, detail="Study has no classification questions")

    ordered_questions: List[str] = []
    question_lookup: Dict[str, str] = {}
    option_maps_by_question: Dict[str, Dict[str, str]] = {}
    question_options: Dict[str, List[str]] = {}
    for q in questions_sorted:
        q_text = (q.question_text or "").strip()
        if not q_text:
            continue
        ordered_questions.append(q_text)
        question_lookup[_normalize_text_key(q_text)] = q_text
        option_maps_by_question[q_text] = _build_answer_options_map(q.answer_options)
        question_options[q_text] = _extract_answer_option_texts(q.answer_options)

    if not ordered_questions:
        raise HTTPException(status_code=400, detail="Classification questions are not configured")

    clicked_question = question_lookup.get(_normalize_text_key(payload.question_text))
    if not clicked_question:
        raise HTTPException(status_code=400, detail="Invalid classification question")

    clicked_answer = _normalize_answer_value(
        payload.answer,
        option_maps_by_question.get(clicked_question, {}),
    )
    if not clicked_answer:
        raise HTTPException(status_code=400, detail="Invalid classification answer")

    response_rows = db.execute(
        select(
            StudyResponse.id,
            StudyResponse.session_id,
            StudyResponse.panelist_id,
            StudyResponse.personal_info,
        ).where(
            StudyResponse.study_id == study_id,
            StudyResponse.is_completed.is_(True),
        )
    ).all()

    if not response_rows:
        return {
            "meta": {
                "cohort_size": 0,
                "question_text": clicked_question,
                "answer": clicked_answer,
                "limit": payload.limit,
                "offset": payload.offset,
                "has_more": False,
                "filters_applied": normalized_filters if filters_are_active(normalized_filters) else None,
            },
            "questions": [
                {"question_text": q, "options": question_options.get(q, [])}
                for q in ordered_questions
            ],
            "respondents": [],
            "cross_tabs": {},
            "demographic_breakdown": None,
        }

    response_meta: Dict[Any, Dict[str, Any]] = {}
    response_ids: List[Any] = []
    for row in response_rows:
        gender, age_group = _extract_demographics(row.personal_info, analysis_service)
        response_meta[row.id] = {
            "session_id": row.session_id,
            "panelist_id": row.panelist_id,
            "gender": gender,
            "age_group": age_group,
        }
        response_ids.append(row.id)

    answer_rows = db.execute(
        select(
            ClassificationAnswer.study_response_id,
            ClassificationAnswer.question_text,
            ClassificationAnswer.answer,
        ).where(ClassificationAnswer.study_response_id.in_(response_ids))
    ).all()

    answers_by_response: Dict[Any, Dict[str, str]] = defaultdict(dict)
    for row in answer_rows:
        q_raw = (row.question_text or "").strip()
        q_text = question_lookup.get(_normalize_text_key(q_raw), q_raw)
        options_map = option_maps_by_question.get(q_text, {})
        normalized_answer = _normalize_answer_value(row.answer, options_map)
        if q_text and normalized_answer and q_text not in answers_by_response[row.study_response_id]:
            answers_by_response[row.study_response_id][q_text] = normalized_answer

    normalized_class_filters: Dict[str, set] = {}
    for raw_q, vals in class_filters.items():
        q_text = question_lookup.get(_normalize_text_key(raw_q))
        if not q_text or not vals:
            continue
        options_map = option_maps_by_question.get(q_text, {})
        allowed_set = {
            norm_val
            for norm_val in (_normalize_answer_value(v, options_map) for v in vals)
            if norm_val
        }
        if allowed_set:
            normalized_class_filters[q_text] = allowed_set

    matched_ids: List[Any] = []
    for rid in response_ids:
        meta = response_meta.get(rid) or {}
        if age_groups and meta.get("age_group") not in age_groups:
            continue
        if genders and meta.get("gender") not in genders:
            continue

        ans_map = answers_by_response.get(rid, {})
        failed = False
        for q_text, allowed in normalized_class_filters.items():
            if ans_map.get(q_text) not in allowed:
                failed = True
                break
        if failed:
            continue

        if ans_map.get(clicked_question) != clicked_answer:
            continue
        matched_ids.append(rid)

    matched_ids.sort(key=lambda rid: str((response_meta.get(rid) or {}).get("session_id") or ""))
    cohort_size = len(matched_ids)
    page_ids = matched_ids[payload.offset : payload.offset + payload.limit]

    respondents: List[Dict[str, Any]] = []
    for idx, rid in enumerate(page_ids, start=payload.offset + 1):
        meta = response_meta.get(rid) or {}
        ans_map = answers_by_response.get(rid, {})
        respondents.append(
            {
                "id": str(rid),
                "label": f"Respondent {idx}",
                "session_id": str(meta.get("session_id") or ""),
                "panelist_id": str(meta.get("panelist_id")) if meta.get("panelist_id") else None,
                "gender": meta.get("gender"),
                "age_group": meta.get("age_group"),
                "answers": {q: ans_map.get(q) for q in ordered_questions},
            }
        )

    cross_tabs: Dict[str, Dict[str, int]] = {}
    for q_text in ordered_questions:
        if q_text == clicked_question:
            continue
        counts = Counter(
            answer_val
            for rid in matched_ids
            for answer_val in [answers_by_response.get(rid, {}).get(q_text)]
            if isinstance(answer_val, str) and answer_val
        )
        if counts:
            sorted_counts = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            cross_tabs[q_text] = {k: v for k, v in sorted_counts}

    demo_raw = _build_cohort_demographic_breakdown(
        matched_ids,
        response_meta,
        age_groups_filter=age_groups,
        genders_filter=genders,
    )
    demographic_breakdown = demo_raw if demo_raw else None

    return {
        "meta": {
            "cohort_size": cohort_size,
            "question_text": clicked_question,
            "answer": clicked_answer,
            "limit": payload.limit,
            "offset": payload.offset,
            "has_more": payload.offset + len(page_ids) < cohort_size,
            "filters_applied": normalized_filters if filters_are_active(normalized_filters) else None,
        },
        "questions": [
            {"question_text": q, "options": question_options.get(q, [])}
            for q in ordered_questions
        ],
        "respondents": respondents,
        "cross_tabs": cross_tabs,
        "demographic_breakdown": demographic_breakdown,
    }


@router.get("/study/{study_id}/saved-reports", response_model=List[SavedFilterReportOut])
def get_study_saved_reports(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """List named saved filter reports for this study (current user). Redis-backed."""
    _authorize_study_for_analysis(db, study_id, current_user)
    return list_saved_reports(db, study_id, current_user.id)


@router.post("/study/{study_id}/saved-reports", response_model=SavedFilterReportOut)
def create_study_saved_report(
    study_id: UUID,
    payload: SavedFilterReportCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Save a named filter report. Returns 409 if the same filters are already saved."""
    _authorize_study_for_analysis(db, study_id, current_user)
    filters_dict = payload.filters.model_dump(exclude_none=True) if payload.filters else {}
    if not filters_are_active(filters_dict):
        raise HTTPException(status_code=400, detail="Select at least one filter before saving a report.")
    return create_saved_report(
        db,
        study_id,
        current_user.id,
        payload.name,
        filters_dict,
    )


@router.put("/study/{study_id}/saved-reports/{report_id}", response_model=SavedFilterReportOut)
def rename_study_saved_report(
    study_id: UUID,
    report_id: UUID,
    payload: SavedFilterReportUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Rename a saved filter report."""
    _authorize_study_for_analysis(db, study_id, current_user)
    return update_saved_report_name(
        db,
        study_id,
        current_user.id,
        report_id,
        payload.name,
    )


@router.delete("/study/{study_id}/saved-reports/{report_id}")
def delete_study_saved_report(
    study_id: UUID,
    report_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Delete a saved filter report."""
    _authorize_study_for_analysis(db, study_id, current_user)
    delete_saved_report(db, study_id, current_user.id, report_id)
    return {"ok": True, "id": str(report_id)}


@router.get("/study/{study_id}/filters")
def list_study_filter_history(
    study_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    List saved filter history for the study (current user only).
    Returns filters ordered by created_at descending.
    """
    from sqlalchemy.orm import defer
    from app.models.study_model import Study, StudyFilterHistory

    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")

    is_authorized = study_obj.creator_id == current_user.id
    if not is_authorized:
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id,
            )
        )
        if not member:
            raise HTTPException(status_code=403, detail="Access denied")

    rows = (
        db.query(StudyFilterHistory)
        .filter(
            StudyFilterHistory.study_id == study_id,
            StudyFilterHistory.user_id == current_user.id,
        )
        .order_by(StudyFilterHistory.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(r.id),
            "study_id": str(r.study_id),
            "filters": r.filters or {},
            "name": r.name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/study/{study_id}/filter")
def filter_study_regression_report(
    study_id: UUID,
    payload: StudyFilterPayload,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run panel regressions (TOP, BOTTOM, RESPONSE) on a filtered subset of respondents.
    Returns meta (counts, filters), top/bottom/response coefficient_means, and optionally per_panelist.
    """
    from sqlalchemy.orm import defer

    study_obj = (
        db.query(Study)
        .options(defer(Study.tasks))
        .filter(Study.id == study_id)
        .first()
    )
    if not study_obj:
        raise HTTPException(status_code=404, detail="Study not found")

    is_authorized = False
    if study_obj.creator_id == current_user.id:
        is_authorized = True
    else:
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == current_user.id
            )
        )
        if member:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(status_code=403, detail="Access denied")

    unilever_format = is_unilever_domain(current_user.email or "")
    response_service = StudyResponseService(db)
    df = response_service.get_study_dataframe(study_id, unilever_format=unilever_format)

    study_data = {
        "title": study_obj.title,
        "study_type": study_obj.study_type,
        "background": getattr(study_obj, "background_image_url", None) or "",
        "language": study_obj.language,
        "launched_at": study_obj.created_at.isoformat() if study_obj.created_at else "",
        "categories": [],
        "elements": [],
        "classification_questions": [],
    }

    if str(study_obj.study_type) == "layer":
        sorted_layers = sorted(study_obj.layers, key=lambda x: x.order)
        for layer in sorted_layers:
            cat_id = str(layer.layer_id)
            study_data["categories"].append({
                "id": cat_id,
                "name": layer.name,
                "order": layer.order,
            })
            for img in sorted(layer.images, key=lambda x: x.order):
                study_data["elements"].append({
                    "id": str(img.image_id),
                    "name": img.name,
                    "content": img.url,
                    "category_id": cat_id,
                    "category": {"name": layer.name, "order": layer.order},
                })
    else:
        for cat in study_obj.categories:
            study_data["categories"].append({
                "id": str(cat.id),
                "name": cat.name,
                "order": cat.order,
            })
            for el in cat.elements:
                study_data["elements"].append({
                    "id": str(el.id),
                    "name": el.name,
                    "content": el.content,
                    "category_id": str(cat.id),
                    "category": {"name": cat.name, "order": cat.order},
                })

    for q in study_obj.classification_questions:
        study_data["classification_questions"].append({
            "question_id": q.question_id,
            "question_text": q.question_text,
            "answer_options": q.answer_options,
            "optional_classification_question": q.optional_classification_question,
        })

    filters_dict = payload.filters.model_dump(exclude_none=True) if payload.filters else None
    analysis_options = get_study_analysis_settings(db, study_id, study=study_obj)

    analysis_service = StudyAnalysisService()
    try:
        report = analysis_service.run_filtered_regression_report(
            study_data=study_data,
            df=df,
            filters=filters_dict,
            include_per_panelist=payload.include_per_panelist,
            analysis_options=analysis_options,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run filtered regression report: {str(e)}",
        )

    # Enrich response with element content and category/element structure
    element_columns = report.get("meta", {}).get("element_columns") or []
    col_to_detail = {}
    for el in study_data.get("elements") or []:
        cat_obj = el.get("category") or next(
            (c for c in (study_data.get("categories") or []) if c.get("id") == el.get("category_id")),
            {},
        )
        cat_name = cat_obj.get("name")
        el_name = el.get("name")
        content = el.get("content")
        if content is None:
            content = ""
        if not cat_name or not el_name:
            continue
        candidates = [
            f"{cat_name}_{el_name}",
            f"{cat_name}-{el_name}",
            f"{cat_name}-{el_name}".replace("_", "-").replace(" ", "-"),
            f"{cat_name}_{el_name}".replace(" ", "_"),
        ]
        for cand in candidates:
            if cand in element_columns:
                col_to_detail[cand] = {
                    "category_name": cat_name,
                    "element_name": el_name,
                    "content": content,
                }
                break

    element_details = []
    for col in element_columns:
        detail = col_to_detail.get(col)
        if detail:
            element_details.append({
                "column": col,
                "category_name": detail["category_name"],
                "element_name": detail["element_name"],
                "content": detail["content"],
            })
        else:
            element_details.append({
                "column": col,
                "category_name": "",
                "element_name": col,
                "content": "",
            })
    report["element_details"] = element_details

    # by_category: group by category_name with coefficients per element
    cat_order = {c["name"]: c.get("order", 0) for c in (study_data.get("categories") or [])}
    by_category_map = {}
    top_cm = report.get("top", {}).get("coefficient_means") or {}
    bottom_cm = report.get("bottom", {}).get("coefficient_means") or {}
    response_cm = report.get("response", {}).get("coefficient_means") or {}
    for ed in element_details:
        cat_name = ed["category_name"] or "Other"
        if cat_name not in by_category_map:
            by_category_map[cat_name] = []
        col = ed["column"]
        by_category_map[cat_name].append({
            "element_name": ed["element_name"],
            "content": ed["content"],
            "top": top_cm.get(col),
            "bottom": bottom_cm.get(col),
            "response": response_cm.get(col),
        })
    report["by_category"] = [
        {"category_name": cat_name, "elements": elements}
        for cat_name, elements in sorted(by_category_map.items(), key=lambda x: (cat_order.get(x[0], 999), x[0]))
    ]

    # Save to filter history only after report is ready (keeps API fast; insert is trivial)
    if payload.save_to_history:
        try:
            from app.models.study_model import StudyFilterHistory
            filters_json = filters_dict if filters_dict else {}
            record = StudyFilterHistory(
                study_id=study_id,
                user_id=current_user.id,
                filters=filters_json,
                name=payload.name[:255] if payload.name else None,
            )
            db.add(record)
            db.commit()
        except Exception:
            db.rollback()
            # Don't fail the request if save fails

    import math
    import numpy as np

    def _json_scalar(val):
        if val is None:
            return None
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            f = float(val)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(val, (np.bool_,)):
            return bool(val)
        if isinstance(val, float):
            return None if (math.isnan(val) or math.isinf(val)) else val
        return val

    def sanitize_for_json(obj):
        if obj is None:
            return None
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                safe_k = _json_scalar(k) if not isinstance(k, (str, type(None))) else k
                if safe_k is None and k is not None:
                    safe_k = str(k)
                out[safe_k] = sanitize_for_json(v)
            return out
        if isinstance(obj, list):
            return [sanitize_for_json(item) for item in obj]
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return _json_scalar(obj)
        if isinstance(obj, float):
            return _json_scalar(obj)
        return obj

    return sanitize_for_json(report)


from app.core.cache import RedisCache

@router.get("/respondent/preview/study/{study_id}/info")
def get_preview_study_info(
    study_id: UUID,
    db: Session = Depends(get_db)
):
    """
    Replica of the respondent info API for previewing a study.
    Always uses respondent_id=1 and works even for draft studies.
    """
    # Check cache first
    cache_key = f"respondent_study_info:{study_id}:1"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return cached_data
        
    # Reuse the logic from get_respondent_study_info with respondent_id=1
    result = get_respondent_study_info(respondent_id=1, study_id=study_id, db=db, skip_cache=True)
    
    # Cache the result
    RedisCache.set(cache_key, result, ttl_seconds=300)
    return result


@router.get("/respondent/{respondent_id}/study/{study_id}/info")
def get_respondent_study_info(
    respondent_id: int,
    study_id: UUID,
    db: Session = Depends(get_db),
    skip_cache: bool = False
):
    """
    Get study information for a specific respondent including classification questions 
    and tasks assigned to that respondent (tasks[respondent_id]).
    This endpoint is public and doesn't require authentication.
    """
    if not skip_cache:
        cache_key = f"respondent_study_info:{study_id}:{respondent_id}"
        cached_data = RedisCache.get(cache_key)
        if cached_data:
            return cached_data
            
    service = StudyResponseService(db)
    
    # Get study details
    from app.services import study as study_service
    study = study_service.get_study_basic_details_public(db, study_id)
    if not study:
        raise HTTPException(
            status_code=404,
            detail="Study not found"
        )
    
    # Get classification questions for this study
    from app.models.study_model import StudyClassificationQuestion
    classification_questions = db.execute(
        select(StudyClassificationQuestion)
        .where(StudyClassificationQuestion.study_id == study_id)
        .order_by(StudyClassificationQuestion.order)
    ).scalars().all()
    # For draft studies (e.g. create-study preview), do not expose the system fragrance question (Q0)
    if study.get("status") == "draft":
        classification_questions = [q for q in classification_questions if q.question_id != FRAGRANCE_QUESTION_ID]

    # Get tasks assigned to this specific respondent (tasks[respondent_id])
    respondent_tasks = service.get_respondent_tasks(study_id, respondent_id)

    # Build layer data (with transform) and enrich respondent_tasks with transform inline
    layers_payload = []
    try:
        from app.models.study_model import StudyLayer
        from sqlalchemy.orm import selectinload
        layers = db.execute(
            select(StudyLayer)
            .options(selectinload(StudyLayer.images))
            .where(StudyLayer.study_id == study_id)
            .order_by(StudyLayer.order)
        ).scalars().all()
        default_transform = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
        for L in layers:
            layers_payload.append({
                "layer_id": L.layer_id,
                "name": L.name,
                "description": L.description,
                "z_index": L.z_index,
                "order": L.order,
                "transform": L.transform or default_transform,
                "images": [
                    {
                        "image_id": I.image_id,
                        "name": I.name,
                        "url": I.url,
                        "alt_text": I.alt_text,
                        "order": I.order,
                    }
                    for I in (L.images or [])
                ],
            })

        # Enrich assigned_tasks elements_shown_content with transform copied from its layer
        name_to_transform = {it["name"]: it["transform"] for it in layers_payload}

        def _enrich_container(container: Any) -> None:
            if isinstance(container, dict):
                esc = container.get("elements_shown_content") or {}
                if isinstance(esc, dict):
                    for _, val in esc.items():
                        if isinstance(val, dict):
                            lname = val.get("layer_name")
                            if lname and lname in name_to_transform:
                                val["transform"] = name_to_transform[lname]
            elif isinstance(container, list):
                for item in container:
                    _enrich_container(item)

        _enrich_container(respondent_tasks)
    except Exception:
        pass

    # Build lightweight metadata: tasks_per_consumer, respondents target, background image url
    from app.models.study_model import Study as StudyModel
    meta_row = db.execute(
        select(StudyModel.background_image_url, StudyModel.audience_segmentation)
        .where(StudyModel.id == study_id)
    ).first()
    background_image_url = None
    respondents_target = 0
    aspect_ratio = None
    if meta_row:
        background_image_url = meta_row.background_image_url
        try:
            seg = meta_row.audience_segmentation or {}
            respondents_target = int(seg.get('number_of_respondents') or 0)
            aspect_ratio = seg.get('aspect_ratio')
        except Exception:
            respondents_target = 0
            aspect_ratio = None
    tasks_per_consumer = len(respondent_tasks or [])

    result = {
        "respondent_id": respondent_id,
        "study_id": str(study_id),
        "study_info": {
            "id": str(study["id"]),
            "title": study["title"],
            "study_type": study["study_type"],
            "main_question": study["main_question"],
            "orientation_text": study["orientation_text"],
            "rating_scale": study["rating_scale"],
            "language": study["language"],
            "toggle_shuffle": study.get("toggle_shuffle", False)
        },
        "metadata": {
            "tasks_per_consumer": tasks_per_consumer,
            "number_of_respondents": respondents_target,
            "background_image_url": background_image_url,
            "aspect_ratio": aspect_ratio,
        },
        "classification_questions": [
            {
                "question_id": q.question_id,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "answer_options": q.answer_options,
                "order": q.order,
                "is_required": q.is_required,
                "optional_classification_question": q.optional_classification_question,
            }
            for q in classification_questions
        ],
        "assigned_tasks": respondent_tasks
    }
    
    if not skip_cache:
        RedisCache.set(f"respondent_study_info:{study_id}:{respondent_id}", result, ttl_seconds=300)
        
    return result
