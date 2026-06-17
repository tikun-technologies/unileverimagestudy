# app/api/v1/study.py
from __future__ import annotations

from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException, status, Body, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, delete
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.models.study_model import Study, StudyMember, StudySavedDesign
from app.schemas.study_schema import (
    StudyCreate, StudyUpdate, StudyOut, StudyListItem, StudyLaunchOut,
    ChangeStatusPayload, RegenerateTasksResponse, ValidateTasksResponse, StudyStatus,
    GenerateTasksRequest, GenerateTasksResult, StudyPublicMinimal, StudyBasicDetails, StudyBasicDetailsV2, SimulateAIRespondentsRequest,
    ValidateDesignConstraintsResponse,
    StudyCreateMinimal, StudyCreateMinimalResponse, CopyStudyRequest,
    StudySavedDesignCreate, StudySavedDesignOut, StudySavedDesignCompareRequest,
    DesignConstraintIn
)
from app.services import study as study_service
from app.services.response import StudyResponseService
from app.services.task_generation_adapter import generate_grid_tasks, generate_layer_tasks
from app.services.study_member_service import study_member_service
from app.schemas.study_schema import StudyMemberInvite, StudyMemberOut, StudyMemberUpdate
from app.core.config import settings
from app.core.cache import RedisCache, invalidate_study_cache
from app.core.domain import FRAGRANCE_QUESTION_ID

router = APIRouter()


def _user_has_study_access(db: Session, study_id: UUID, user_id: UUID) -> bool:
    """Return True if user is creator or member of the study."""
    row = db.execute(select(Study.creator_id).where(Study.id == study_id)).first()
    if not row:
        return False
    if row[0] == user_id:
        return True
    member = db.scalar(
        select(StudyMember).where(
            StudyMember.study_id == study_id,
            StudyMember.user_id == user_id
        )
    )
    return member is not None


def _invalidate_user_studies_list_cache(user_id: UUID) -> None:
    """Invalidate studies-list cache for a single user."""
    RedisCache.delete_pattern(f"user_studies_list:{user_id}:*")


def _invalidate_project_public_studies_cache(project_id: Optional[UUID]) -> None:
    """Invalidate public studies list cache for a project."""
    if project_id is None:
        return
    RedisCache.delete(f"project_public_studies:{project_id}")


def _generate_preview_tasks(payload: GenerateTasksRequest, number_of_respondents: int) -> Dict[str, Any]:
    """Generate a small preview of tasks for immediate display while background job runs"""
    # Generate tasks for just 1-3 respondents as a preview
    preview_respondents = min(3, number_of_respondents)
    
    if payload.study_type == 'grid' or payload.study_type == 'text':
        # Create a minimal preview for grid and text studies (both use same structure)
        if not payload.elements or not payload.categories:
            return {}
        
        # Build categories data for preview
        categories_data = []
        for cat in payload.categories:
            cat_elements = [e for e in payload.elements if e.category_id == cat.category_id]
            categories_data.append({
                "category_name": cat.name,
                "elements": [
                    {
                        "element_id": str(el.element_id),
                        "name": el.name,
                        "content": el.content,
                        "alt_text": el.alt_text or el.name,
                        "element_type": el.element_type,
                    }
                    for el in cat_elements
                ]
            })
        
        from app.services.golden_task_generator import generate_grid_tasks_golden
        try:
            result = generate_grid_tasks_golden(
                categories_data=categories_data,
                number_of_respondents=preview_respondents,
                exposure_tolerance_cv=payload.exposure_tolerance_cv or 1.0,
                seed=payload.seed,
                tasks_per_respondent=int(payload.tasks_per_respondent or 0),
            )
            return result.get('tasks', {})
        except Exception:
            # If preview generation fails, return empty tasks
            return {}
    
    elif payload.study_type == 'layer':
        # Create a minimal preview for layer studies
        if not payload.study_layers:
            return {}
        
        # Build layers for preview
        layers = []
        for layer in payload.study_layers:
            layer_obj = type('Layer', (), {
                'layer_id': layer.layer_id,
                'name': layer.name,
                'images': [type('Image', (), {
                    'image_id': img.image_id,
                    'name': img.name,
                    'url': img.url
                }) for img in layer.images or []],
                'z_index': layer.z_index,
                'order': layer.order
            })()
            layers.append(layer_obj)
        
        # Generate preview tasks
        from app.services.task_generation_adapter import generate_layer_tasks
        try:
            result = generate_layer_tasks(
                layers=layers,
                number_of_respondents=preview_respondents,
                exposure_tolerance_pct=payload.exposure_tolerance_pct or 2.0,
                seed=payload.seed,
                tasks_per_consumer=int(payload.tasks_per_respondent or 0),
                design_constraints=[c.model_dump(exclude_none=True) for c in (payload.design_constraints or [])],
            )
            return result.get('tasks', {})
        except Exception:
            # If preview generation fails, return empty tasks
            return {}
    
    elif payload.study_type == 'hybrid':
        if not payload.elements or not payload.categories or not payload.phase_order:
            return {}
            
        combined_tasks = {}
        is_mix = "mix" in payload.phase_order
        target_phases = []
        if is_mix:
            target_phases = list(set(c.phase_type for c in payload.categories if c.phase_type))
        else:
            target_phases = payload.phase_order

        tasks_per_respondent_mix = {}

        for phase_type in target_phases:
            phase_categories = [c for c in payload.categories if c.phase_type == phase_type]
            if not phase_categories:
                continue
                
            cat_data = []
            for cat in phase_categories:
                cat_elements = [e for e in payload.elements if e.category_id == cat.category_id]
                cat_data.append({
                    "category_name": cat.name,
                    "elements": [
                        {
                            "element_id": str(el.element_id),
                            "name": el.name,
                            "content": el.content,
                            "alt_text": el.alt_text or el.name,
                            "element_type": el.element_type,
                        }
                        for el in cat_elements
                    ]
                })
            
            from app.services.golden_task_generator import generate_grid_tasks_golden
            try:
                phase_result = generate_grid_tasks_golden(
                    categories_data=cat_data,
                    number_of_respondents=preview_respondents,
                    exposure_tolerance_cv=payload.exposure_tolerance_cv or 1.0,
                    seed=payload.seed,
                    tasks_per_respondent=int(payload.tasks_per_respondent or 0),
                )
                phase_tasks = phase_result.get('tasks', {})
                
                if is_mix:
                    for resp_id, t_list in phase_tasks.items():
                        if resp_id not in tasks_per_respondent_mix:
                            tasks_per_respondent_mix[resp_id] = []
                        for t in t_list:
                            t['phase_type'] = phase_type
                            tasks_per_respondent_mix[resp_id].append(t)
                else:
                    for resp_id, t_list in phase_tasks.items():
                        if resp_id not in combined_tasks:
                            combined_tasks[resp_id] = []
                        
                        offset = len(combined_tasks[resp_id])
                        for t in t_list:
                            t['task_index'] = int(t.get('task_index', 0)) + offset
                            t['phase_type'] = phase_type
                        combined_tasks[resp_id].extend(t_list)
            except Exception:
                continue
        
        if is_mix:
            import random
            for resp_id, t_list in tasks_per_respondent_mix.items():
                random.shuffle(t_list)
                for idx, t in enumerate(t_list):
                    t['task_index'] = idx
                combined_tasks[resp_id] = t_list
                
        return combined_tasks

    return {}


def _ensure_study_exists(payload: GenerateTasksRequest, db: Session, current_user: User):
    """Helper function to ensure study exists, creating it if necessary"""
    from sqlalchemy import select
    from app.models.study_model import Study as StudyModel, StudyMember
    import logging
    logger = logging.getLogger(__name__)
    
    if payload.study_id is not None:
        study_row = db.scalars(
            select(StudyModel).where(StudyModel.id == payload.study_id, StudyModel.creator_id == current_user.id)
        ).first()

        if not study_row:
            # Check if the user is a member (Editor or Admin)
            stmt_member = (
                select(StudyModel)
                .join(StudyMember, StudyMember.study_id == StudyModel.id)
                .where(StudyModel.id == payload.study_id, StudyMember.user_id == current_user.id)
            )
            study_row = db.scalars(stmt_member).first()
            if study_row:
                logger.info(f"User {current_user.id} accessed study {payload.study_id} as a member.")

        if not study_row:
            logger.error(f"Access denied or study not found for user {current_user.id} and study {payload.study_id}")
            raise HTTPException(status_code=404, detail="Study not found or access denied.")
        return study_row
    else:
        # Create a new draft study if minimal required fields provided
        if not payload.title or not payload.background or not payload.language or not payload.main_question or not payload.orientation_text:
            raise HTTPException(status_code=400, detail="Missing required study fields for on-the-fly creation")
        return study_service.create_study(
            db=db,
            creator_id=current_user.id,
            payload=StudyCreate(
                title=payload.title,
                background=payload.background,
                language=payload.language,
                main_question=payload.main_question,
                orientation_text=payload.orientation_text,
                study_type=payload.study_type,
                background_image_url=payload.background_image_url,
                aspect_ratio=payload.aspect_ratio,
                rating_scale=payload.rating_scale,
                audience_segmentation=payload.audience_segmentation,
                categories=payload.categories,
                elements=payload.elements,
                study_layers=payload.study_layers,
                design_constraints=payload.design_constraints,
                classification_questions=payload.classification_questions,
                product_keys=getattr(payload, 'product_keys', None),
                product_id=getattr(payload, 'product_id', None),
            ),
            base_url_for_share=settings.BASE_URL,
        )


@router.post("", response_model=StudyOut, status_code=status.HTTP_201_CREATED)
def create_study_endpoint(
    payload: StudyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Use settings.BASE_URL as default for share URL generation
    study = study_service.create_study(
        db=db,
        creator_id=current_user.id,
        payload=payload,
        base_url_for_share=settings.BASE_URL,
    )
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(getattr(study, "project_id", None))
    # Inject aspect_ratio from audience_segmentation for output
    try:
        ar = (study.audience_segmentation or {}).get('aspect_ratio')
        out = StudyOut.model_validate(study).model_dump()
        out['aspect_ratio'] = ar
        return out
    except Exception:
        return study


@router.post("/minimal", response_model=StudyCreateMinimalResponse, status_code=status.HTTP_201_CREATED)
def create_study_minimal_endpoint(
    payload: StudyCreateMinimal,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Ultra-fast study creation endpoint - creates a draft study with only essential fields.

    This endpoint is optimized for maximum speed (< 1 second):
    - Only requires title, background (description), and language
    - Single raw SQL INSERT (bypasses ORM overhead)
    - Returns only the study ID
    - No relationship loading or object serialization
    - Default values for all other required fields

    The study is created in 'draft' status with default values that can be updated later.
    Use PUT /studies/{study_id} to add remaining details like main_question, elements, etc.
    """
    product_keys_list = [k.model_dump() for k in payload.product_keys] if getattr(payload, 'product_keys', None) else None
    study_id = study_service.create_study_minimal(
        db=db,
        creator_id=current_user.id,
        title=payload.title,
        background=payload.background,
        language=payload.language,
        last_step=payload.last_step or 1,
        project_id=getattr(payload, 'project_id', None),
        product_keys=product_keys_list,
        product_id=getattr(payload, 'product_id', None),
    )
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(getattr(payload, "project_id", None))
    return StudyCreateMinimalResponse(id=study_id)


@router.post("/{study_id}/copy", response_model=StudyOut, status_code=status.HTTP_201_CREATED)
def copy_study_endpoint(
    study_id: UUID,
    payload: Optional[CopyStudyRequest] = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create an exact copy of a study (config only; no tasks).
    The copy is in draft with last_step=6. project_id is not copied from the source.
    Send project_id in the body to associate the copy with a project; otherwise the copy is standalone.
    """
    body = payload or CopyStudyRequest()
    new_study = study_service.copy_study(
        db=db,
        study_id=study_id,
        user_id=current_user.id,
        project_id=body.project_id,
    )
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(getattr(new_study, "project_id", None))
    # Reload with relations so StudyOut has categories, elements, layers, classification_questions
    study = study_service.get_study(db=db, study_id=new_study.id, owner_id=current_user.id)
    try:
        ar = (study.audience_segmentation or {}).get('aspect_ratio')
        out = StudyOut.model_validate(study).model_dump()
        out['aspect_ratio'] = ar
        return out
    except Exception:
        return study


def _user_role_for_study(
    study_id: UUID,
    creator_id: UUID,
    project_id: Optional[UUID],
    project_creator_map: Dict[UUID, UUID],
    project_role_map: Dict[UUID, str],
    study_role_map: Dict[UUID, str],
    current_user_id: UUID,
) -> str:
    """Compute current user's role for a study (same hierarchy as preview endpoint). No DB calls."""
    is_project_creator = (
        project_id is not None
        and project_creator_map.get(project_id) == current_user_id
    )
    project_member_role = project_role_map.get(project_id) if project_id else None
    if is_project_creator:
        return "admin"
    if project_member_role:
        return project_member_role if project_member_role == "editor" else "viewer"
    if creator_id == current_user_id and project_id:
        return study_role_map.get(study_id) or "viewer"
    if creator_id == current_user_id:
        return "admin"
    return study_role_map.get(study_id) or "viewer"


@router.get("", response_model=List[StudyListItem])
def list_studies_endpoint(
    status_filter: Optional[StudyStatus] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    response: Response = None,
):
    """
    List studies for the current user with live response counts.
    Response includes total_responses, completed_responses, completion_rate, etc.
    Clients should replace any cached studies list (e.g. in local storage) with this response
    so cached data does not show stale zeros.
    """
    # Prevent caching so clients always get fresh counts (avoids stale zeros in cached/local storage)
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        
    # Short 15-second Redis cache to "debounce" heavy dashboard loads
    # while still feeling "live" to the user.
    status_str = status_filter.value if status_filter else "all"
    cache_key = f"user_studies_list:{current_user.id}:{status_str}:{page}:{per_page}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return [StudyListItem.model_validate(d) for d in cached_data]

    rows, _total = study_service.list_studies(
        db=db,
        owner_id=current_user.id,
        status_filter=status_filter,
        page=page,
        per_page=per_page,
    )
    # Lightweight enrichment: only the counters needed for the list card
    if not rows:
        return []
    
    # Row structure from optimized query:
    # (id, title, study_type, status, created_at, last_step, jobid, project_id, 
    #  creator_id, product_keys, product_id, audience_segmentation,
    #  total_responses_calc, completed_responses_calc, abandoned_responses_calc, avg_duration_calc)
    study_ids = [row.id for row in rows]
    project_ids = list({row.project_id for row in rows if row.project_id is not None})
    project_creator_map: Dict[UUID, UUID] = {}
    project_role_map: Dict[UUID, str] = {}
    study_role_map: Dict[UUID, str] = {}
    if project_ids:
        from app.models.project_model import Project, ProjectMember
        projects = db.execute(
            select(Project.id, Project.creator_id).where(Project.id.in_(project_ids))
        ).all()
        project_creator_map = {p.id: p.creator_id for p in projects}
        project_members = db.execute(
            select(ProjectMember.project_id, ProjectMember.role).where(
                ProjectMember.project_id.in_(project_ids),
                ProjectMember.user_id == current_user.id,
            )
        ).all()
        project_role_map = {
            r.project_id: (r.role if isinstance(r.role, str) else getattr(r.role, "value", str(r.role)))
            for r in project_members
        }
    study_members = db.execute(
        select(StudyMember.study_id, StudyMember.role).where(
            StudyMember.study_id.in_(study_ids),
            StudyMember.user_id == current_user.id,
        )
    ).all()
    study_role_map = {
        r.study_id: (r.role if isinstance(r.role, str) else getattr(r.role, "value", str(r.role)))
        for r in study_members
    }
    enriched: List[StudyListItem] = []
    for row in rows:
        # Extract values from denormalized counters (no JOINs needed!)
        total_calc = int(row.total_responses_calc or 0)
        completed_calc = int(row.completed_responses_calc or 0)
        abandoned_calc = int(row.abandoned_responses_calc or 0)
        respondents_target = int((row.audience_segmentation or {}).get("number_of_respondents") or 0)
        
        user_role = _user_role_for_study(
            row.id,
            row.creator_id,
            row.project_id,
            project_creator_map,
            project_role_map,
            study_role_map,
            current_user.id,
        )
        
        enriched.append(StudyListItem(
            id=row.id,
            title=row.title,
            study_type=row.study_type,
            status=row.status,
            created_at=row.created_at,
            last_step=row.last_step,
            jobid=row.jobid,
            project_id=row.project_id,
            product_keys=row.product_keys,
            product_id=row.product_id,
            total_responses=total_calc,
            completed_responses=completed_calc,
            abandoned_responses=abandoned_calc,
            respondents_target=respondents_target,
            respondents_completed=completed_calc,
            average_duration=0,  # Not needed for list view, calculated on detail view
            completion_rate=(completed_calc / total_calc * 100) if total_calc else 0,
            abandonment_rate=(abandoned_calc / total_calc * 100) if total_calc else 0,
            user_role=user_role,
        ))

    # Cache for 15 seconds to debounce dashboard loads (store JSON-serializable dicts)
    RedisCache.set(
        cache_key,
        [x.model_dump(mode="json") for x in enriched],
        ttl_seconds=7,
    )
    return enriched


@router.post("/{study_id}/simulate-ai-respondents")
def simulate_ai_respondents_endpoint(
    study_id: UUID,
    body: Optional[SimulateAIRespondentsRequest] = Body(None),
    run_async: bool = Query(True, description="If True and respondent count exceeds threshold, run in background"),
    max_respondents: Optional[int] = Query(None, description="Cap on number of respondents to simulate (default from study)"),
    max_panelist_workers: int = Query(10, ge=1, le=50, description="Number of panelists to run in parallel (AI phase); same as standalone main.py max_workers=10"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Simulate AI respondents for a study: generate panelists (gender, age, classification answers),
    run AI rating for each respondent's tasks in order, and persist via the same storage as human respondents.
    Body (optional): max_respondents, is_special_creator, randomize. If is_special_creator=true, AI rates only 1 or 5. If randomize=true, use fallback (random) ratings instead of ChatGPT.
    """
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    if not task_service.has_tasks(study_id):
        raise HTTPException(status_code=400, detail="Study has no tasks. Generate tasks first.")
    if not study.classification_questions or len(study.classification_questions) == 0:
        raise HTTPException(status_code=400, detail="Study has no classification questions.")

    # Body overrides query for max_respondents, is_special_creator, randomize
    body_max = body.max_respondents if body else None
    is_special_creator = body.is_special_creator if body and body.is_special_creator is not None else False
    randomize = body.randomize if body and body.randomize is not None else False

    seg = study.audience_segmentation or {}
    N = body_max if body_max is not None and body_max >= 1 else (max_respondents if max_respondents is not None and max_respondents >= 1 else int(seg.get("number_of_respondents") or 0))
    if N <= 0:
        try:
            N = task_service.get_respondent_count(study_id)
        except Exception:
            N = 0
    if N <= 0:
        raise HTTPException(status_code=400, detail="Could not determine number of respondents (set audience_segmentation.number_of_respondents or ensure tasks have numeric keys).")

    from app.services.synthetic_simulation_service import get_max_panelist_combinations
    max_combinations = get_max_panelist_combinations(db, study_id)
    if max_combinations is not None and N > max_combinations:
        raise HTTPException(
            status_code=400,
            detail=f"AI cannot process more than {max_combinations} respondents. Classification combinations allow at most {max_combinations}.",
        )

    if run_async and N > settings.MAX_RESPONDENTS_FOR_SYNC:
        import logging
        from app.services.background_task_service import background_task_service

        logger = logging.getLogger(__name__)
        job_id = background_task_service.create_job(
            study_id=str(study_id),
            user_id=str(current_user.id),
            payload={
                "job_type": "simulate_ai_respondents",
                "max_respondents": N,
                "max_panelist_workers": max_panelist_workers,
                "is_special_creator": is_special_creator,
                "randomize": randomize,
            },
        )

        if getattr(settings, "USE_CELERY", False):
            from app.tasks.celery_jobs import simulate_synthetic_respondents_celery
            simulate_synthetic_respondents_celery.delay(
                job_id=job_id,
                study_id=str(study_id),
                user_id=str(current_user.id),
                payload={
                    "max_respondents": N,
                    "max_panelist_workers": max_panelist_workers,
                    "is_special_creator": is_special_creator,
                    "randomize": randomize,
                },
            )
            logger.info(f"Dispatched simulate_synthetic_respondents to Celery: job_id={job_id}")
        else:
            import threading
            import asyncio

            def run_background_job():
                from app.db.session import SessionLocal
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                db_bg = SessionLocal()
                try:
                    task = loop.run_until_complete(background_task_service.start_job(job_id, db_bg))
                    if task:
                        loop.run_until_complete(task)
                except Exception as e:
                    logger.error("Background simulate_ai_respondents job failed: %s", e)
                finally:
                    try:
                        db_bg.close()
                    except Exception:
                        pass
                    loop.close()

            thread = threading.Thread(target=run_background_job, daemon=True)
            thread.start()

        return {
            "job_id": job_id,
            "message": "Simulation started in background.",
            "study_id": str(study_id),
            "respondents_requested": N,
            "stream_url": f"/api/v1/studies/simulate-ai-respondents/stream/{job_id}",
            "status_url": f"/api/v1/studies/simulate-ai-respondents/status/{job_id}",
        }

    from app.services.synthetic_simulation_service import run_simulation
    result = run_simulation(
        db=db,
        study_id=study_id,
        study_data=None,
        max_respondents=N,
        max_panelist_workers=max_panelist_workers,
        is_special_creator=is_special_creator,
        randomize=randomize,
    )
    return result


@router.get("/{study_id}", response_model=StudyOut)
def get_study_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    ar = (study.audience_segmentation or {}).get('aspect_ratio')

    # Build response dict via pydantic but ensure classification_questions are included
    out = StudyOut.model_validate(study).model_dump()
    # Ensure aspect_ratio is present
    out['aspect_ratio'] = ar

    # Load tasks through TaskService so normalized rows win and legacy JSON is only fallback.
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    tasks_data = task_service.get_all_tasks_as_dict(study_id) or None
    out['tasks'] = tasks_data

    # If classification_questions missing or empty in the serialized output, populate from ORM
    try:
        serialized_cq = out.get('classification_questions')
    except Exception:
        serialized_cq = None

    if not serialized_cq:
        cq_list = []
        try:
            for q in getattr(study, 'classification_questions', []) or []:
                cq_list.append({
                    'id': q.id,
                    'question_id': q.question_id,
                    'question_text': q.question_text,
                    'question_type': q.question_type,
                    'is_required': True if (str(q.is_required).upper() == 'Y' or bool(q.is_required)) else False,
                    'order': q.order,
                    'answer_options': q.answer_options,
                    'optional_classification_question': q.optional_classification_question,
                    'config': q.config,
                })
        except Exception:
            cq_list = []
        out['classification_questions'] = cq_list

    # For draft studies, do not expose the system fragrance question (Q0) in edit/preview
    if getattr(study, "status", None) == "draft" and out.get("classification_questions"):
        out["classification_questions"] = [
            q for q in out["classification_questions"]
            if q.get("question_id") != FRAGRANCE_QUESTION_ID
        ]

    return out


@router.get("/{study_id}/preview", response_model=StudyOut)
def get_study_preview_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get study with tasks limited to only 1 respondent (preview mode)"""
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    ar = (study.audience_segmentation or {}).get('aspect_ratio')

    # Determine user role (respects project-level role hierarchy)
    user_role = "viewer"
    
    # Check if project creator (project owner always has admin access)
    is_project_creator = False
    project_member_role = None
    
    if study.project_id:
        from app.models.project_model import Project, ProjectMember
        
        # Check if project relation is loaded, else fetch
        project = study.project if study.project else db.get(Project, study.project_id)
        
        if project:
            if project.creator_id == current_user.id:
                is_project_creator = True
            else:
                # Check project member role
                project_member = db.scalars(
                    select(ProjectMember).where(
                        ProjectMember.project_id == study.project_id,
                        ProjectMember.user_id == current_user.id
                    )
                ).first()
                if project_member:
                    project_member_role = project_member.role

    # Determine final user_role based on hierarchy
    if is_project_creator:
        # Project owner always has admin access
        user_role = "admin"
    elif project_member_role:
        # Project member role takes precedence over study creator status
        # If they're a project viewer, they're a viewer even if they created the study
        user_role = project_member_role if project_member_role == 'editor' else 'viewer'
    elif study.creator_id == current_user.id and study.project_id:
        # Study creator who was removed from project: demoted to viewer (use StudyMember role)
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study.id,
                StudyMember.user_id == current_user.id
            )
        )
        user_role = member.role if member else "viewer"
    elif study.creator_id == current_user.id:
        # Study creator, standalone study (no project) is admin
        user_role = "admin"
    else:
        # Check study member role
        member = db.scalar(
            select(StudyMember).where(
                StudyMember.study_id == study.id,
                StudyMember.user_id == current_user.id
            )
        )
        if member:
            user_role = member.role


    # Build response dict via pydantic but ensure classification_questions are included
    out = StudyOut.model_validate(study).model_dump()
    out['user_role'] = user_role
    # Ensure aspect_ratio is present
    out['aspect_ratio'] = ar

    # Load tasks through TaskService so preview uses the same source as runtime.
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    all_tasks = task_service.get_all_tasks_as_dict(study_id)
    first_respondent = next((k for k in all_tasks if str(k).isdigit()), None) if isinstance(all_tasks, dict) else None
    tasks_data = {first_respondent: all_tasks[first_respondent]} if first_respondent else None
    out['tasks'] = tasks_data

    # If classification_questions missing or empty in the serialized output, populate from ORM
    try:
        serialized_cq = out.get('classification_questions')
    except Exception:
        serialized_cq = None

    if not serialized_cq:
        cq_list = []
        try:
            for q in getattr(study, 'classification_questions', []) or []:
                cq_list.append({
                    'id': q.id,
                    'question_id': q.question_id,
                    'question_text': q.question_text,
                    'question_type': q.question_type,
                    'is_required': True if (str(q.is_required).upper() == 'Y' or bool(q.is_required)) else False,
                    'order': q.order,
                    'answer_options': q.answer_options,
                    'optional_classification_question': q.optional_classification_question,
                    'config': q.config,
                })
        except Exception:
            cq_list = []
        out['classification_questions'] = cq_list

    # For draft studies, do not expose the system fragrance question (Q0) in edit/preview
    if getattr(study, "status", None) == "draft" and out.get("classification_questions"):
        out["classification_questions"] = [
            q for q in out["classification_questions"]
            if q.get("question_id") != FRAGRANCE_QUESTION_ID
        ]

    return out


@router.get("/{study_id}/basic", response_model=StudyBasicDetails, response_model_exclude_none=True)
def get_study_basic_details_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Get basic study details (no authentication required).
    Returns core study information including:
    - Study title, status, type, created date
    - Background, main question, orientation text
    - Rating scale configuration
    - Study config (audience segmentation)
    - Classification questions
    """
    cache_key = f"study_config:basic:{study_id}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return cached_data

    details = study_service.get_study_basic_details_public(db=db, study_id=study_id)
    if not details:
        raise HTTPException(status_code=404, detail="Study not found")
        
    RedisCache.set(cache_key, details, ttl_seconds=7)
    return details


@router.get("/{study_id}/basic-2", response_model=StudyBasicDetailsV2, response_model_exclude_none=True)
def get_study_basic_details_v2_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Same as basic but also includes total_responses (how many opted in/started the study).
    No authentication required.
    """
    cache_key = f"study_config:basic_v2:{study_id}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return cached_data

    details = study_service.get_study_basic_details_public_v2(db=db, study_id=study_id)
    if not details:
        raise HTTPException(status_code=404, detail="Study not found")
        
    RedisCache.set(cache_key, details, ttl_seconds=7)
    return details


@router.get("/private/{study_id}", response_model=StudyOut)
def get_study_private_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get complete study details for the study creator only.
    This endpoint provides full access to all study information including:
    - All study metadata and configuration
    - Complete elements/layers with images
    - Classification questions
    - Tasks and response data
    - Share URL and tokens
    """
    return study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)


@router.get("/{study_id}/is-owner")
def check_study_ownership_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Check if the logged-in user is the owner of the study and if the study is active.
    Returns ownership status and active status in a single optimized query.
    """
    from sqlalchemy import select
    
    # Single optimized query to check ownership and status
    stmt = select(Study.creator_id, Study.status).where(Study.id == study_id)
    result = db.execute(stmt).first()
    
    if not result:
        return {"is_owner": False, "is_active": False}
    
    is_owner = result.creator_id == current_user.id
    is_active = result.status == 'active'
    
    return {"is_owner": is_owner, "is_active": is_active}


@router.get("/public/preview/{study_id}")
def get_study_public_preview_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Get study information for public preview (no authentication required).
    Bypasses status checks to allow viewing draft or paused studies.
    """
    result = study_service.get_study_public_preview(db=db, study_id=study_id)
    
    # Check if there's an error in the result
    if "error" in result:
        if result["error"] == "Study not found":
            raise HTTPException(
                status_code=404, 
                detail=result["message"]
            )
    
    return result


@router.get("/public/{study_id}")
def get_study_public_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Get study information for public access (no authentication required).
    Handles different study statuses with appropriate messaging.
    """
    cache_key = f"study_config:public:{study_id}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return cached_data

    result = study_service.get_study_public_with_status_check(db=db, study_id=study_id)
    
    # Check if there's an error in the result
    if "error" in result:
        if result["error"] == "Study not found":
            raise HTTPException(
                status_code=404, 
                detail=result["message"]
            )
        elif result["error"] == "Study is paused":
            raise HTTPException(
                status_code=403, 
                detail=result["message"]
            )
        elif result["error"] == "Study is completed":
            raise HTTPException(
                status_code=410, 
                detail=result["message"]
            )
        else:
            raise HTTPException(
                status_code=403, 
                detail=result["message"]
            )
    
    # Cache the successful response
    RedisCache.set(cache_key, result, ttl_seconds=7)
    
    # Return the study data if no errors
    return result


@router.get("/public/{study_id}/details", response_model=StudyOut)
def get_study_public_details_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Get complete study details for public access (no authentication required).
    Returns full study information including elements, layers, and classification questions.
    Only returns studies that are active and have a share_token.
    """
    cache_key = f"study_config:public_details:{study_id}"
    cached_data = RedisCache.get(cache_key)
    if cached_data:
        return cached_data

    study = study_service.get_study_public(db=db, study_id=study_id)
    if not study:
        raise HTTPException(
            status_code=404, 
            detail="Study not found or not publicly accessible"
        )
    try:
        ar = (study.audience_segmentation or {}).get('aspect_ratio')
        out = StudyOut.model_validate(study).model_dump()
        out['aspect_ratio'] = ar
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        out['tasks'] = task_service.get_all_tasks_as_dict(study_id) or None
        
        # Cache the successful response
        RedisCache.set(cache_key, out, ttl_seconds=7)
        return out
    except Exception:
        return study


@router.get("/share/details")
def get_study_share_details_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
):
    """
    Public: fetch only share_url, status, title, and study_type for a study.
    """
    info = study_service.get_study_share_details(db=db, study_id=study_id)
    if not info:
        raise HTTPException(status_code=404, detail="Study not found")
    return {
        "id": str(info.get("id")),
        "title": info.get("title"),
        "study_type": info.get("study_type"),
        "status": info.get("status"),
        "share_url": info.get("share_url"),
    }


@router.put("/{study_id}", response_model=StudyOut)
def update_study_endpoint(
    study_id: UUID,
    payload: StudyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    previous_project_id = db.scalar(select(Study.project_id).where(Study.id == study_id))
    
    try:
        logger.info(f"PUT /studies/{study_id} - User: {current_user.id}")
        logger.info(f"Payload fields: {list(payload.model_dump(exclude_none=True).keys())}")
        
        # Log specific fields that might cause validation errors
        if payload.rating_scale:
            logger.info(f"Rating scale: {payload.rating_scale.model_dump()}")
        if payload.status:
            logger.info(f"Status: {payload.status}")
        if payload.title:
            logger.info(f"Title length: {len(payload.title)}")
        
        result = study_service.update_study(
            db=db, study_id=study_id, owner_id=current_user.id, payload=payload
        )
        
        # Invalidate cache after successful update
        invalidate_study_cache(study_id)
        _invalidate_user_studies_list_cache(current_user.id)
        _invalidate_project_public_studies_cache(previous_project_id)
        _invalidate_project_public_studies_cache(getattr(result, "project_id", None))
        
        logger.info(f"Study {study_id} updated successfully")
        return result
        
    except HTTPException as e:
        logger.error(f"HTTPException in PUT /studies/{study_id}: {e.detail} (status: {e.status_code})")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in PUT /studies/{study_id}: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.put("/{study_id}/design-constraints/{constraint_id}")
def upsert_design_constraint_endpoint(
    study_id: UUID,
    constraint_id: str,
    payload: DesignConstraintIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if payload.id and payload.id != constraint_id:
        raise HTTPException(status_code=400, detail="Constraint id in path and payload must match.")
    payload.id = constraint_id
    constraints = study_service.upsert_design_constraint(
        db=db,
        study_id=study_id,
        owner_id=current_user.id,
        constraint=payload,
    )
    invalidate_study_cache(study_id)
    return {"design_constraints": constraints}


@router.delete("/{study_id}/design-constraints/{constraint_id}")
def delete_design_constraint_endpoint(
    study_id: UUID,
    constraint_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    constraints = study_service.delete_design_constraint(
        db=db,
        study_id=study_id,
        owner_id=current_user.id,
        constraint_id=constraint_id,
    )
    invalidate_study_cache(study_id)
    return {"design_constraints": constraints}


@router.delete("/{study_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_study_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    previous_project_id = db.scalar(select(Study.project_id).where(Study.id == study_id))
    study_service.delete_study(db=db, study_id=study_id, owner_id=current_user.id)
    invalidate_study_cache(study_id)
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(previous_project_id)
    return None


@router.post("/{study_id}/status", response_model=StudyOut)
def change_status_endpoint(
    study_id: UUID,
    payload: ChangeStatusPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    project_id = db.scalar(select(Study.project_id).where(Study.id == study_id))
    result = study_service.change_status(
        db=db, study_id=study_id, owner_id=current_user.id, new_status=payload.status
    )
    invalidate_study_cache(study_id)
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(project_id)
    return result


@router.post("/{study_id}/regenerate-tasks", response_model=RegenerateTasksResponse)
def regenerate_tasks_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    generator: Dict[str, Any] = {
        "grid": generate_grid_tasks,
        "layer": generate_layer_tasks,
    }
    result = study_service.regenerate_tasks(
        db=db, study_id=study_id, owner_id=current_user.id, generator=generator
    )
    invalidate_study_cache(study_id)
    return result


@router.get("/{study_id}/validate-tasks", response_model=ValidateTasksResponse)
def validate_tasks_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return study_service.validate_tasks(db=db, study_id=study_id, owner_id=current_user.id)


# ---------- Saved Design Configurations ----------

def _normalize_saved_design_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


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


@router.post("/{study_id}/saved-designs", response_model=StudySavedDesignOut, status_code=status.HTTP_201_CREATED)
def create_saved_design_endpoint(
    study_id: UUID,
    payload: StudySavedDesignCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study_service.check_study_access(db=db, study_id=study_id, user_id=current_user.id)

    normalized_name = _normalize_saved_design_name(payload.name)
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
        created_by_id=current_user.id,
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A saved design with this name already exists.")

    db.refresh(design)
    return _saved_design_to_out(design)


@router.get("/{study_id}/saved-designs", response_model=List[StudySavedDesignOut])
def list_saved_designs_endpoint(
    study_id: UUID,
    design_type: str = Query("configurator", pattern="^(configurator|input)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study_service.check_study_access(db=db, study_id=study_id, user_id=current_user.id)
    designs = db.scalars(
        select(StudySavedDesign)
        .where(
            StudySavedDesign.study_id == study_id,
            StudySavedDesign.design_type == design_type,
        )
        .order_by(desc(StudySavedDesign.created_at))
    ).all()
    return [_saved_design_to_out(design) for design in designs]


@router.post("/{study_id}/saved-designs/compare", response_model=List[StudySavedDesignOut])
def compare_saved_designs_endpoint(
    study_id: UUID,
    payload: StudySavedDesignCompareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study_service.check_study_access(db=db, study_id=study_id, user_id=current_user.id)
    unique_ids = list(dict.fromkeys(payload.design_ids))
    if len(unique_ids) < 2 or len(unique_ids) > 4:
        raise HTTPException(status_code=400, detail="Select between 2 and 4 designs to compare.")

    designs = db.scalars(
        select(StudySavedDesign).where(
            StudySavedDesign.study_id == study_id,
            StudySavedDesign.design_type == payload.design_type,
            StudySavedDesign.id.in_(unique_ids),
        )
    ).all()
    by_id = {design.id: design for design in designs}
    if len(by_id) != len(unique_ids):
        raise HTTPException(status_code=404, detail="One or more saved designs were not found.")

    return [_saved_design_to_out(by_id[design_id]) for design_id in unique_ids]


@router.delete("/{study_id}/saved-designs/{design_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_design_endpoint(
    study_id: UUID,
    design_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study_service.check_study_access(db=db, study_id=study_id, user_id=current_user.id)
    result = db.execute(
        delete(StudySavedDesign).where(
            StudySavedDesign.study_id == study_id,
            StudySavedDesign.id == design_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Saved design not found.")
    db.commit()
    return None


# ---------- Sharing Endpoints ----------

@router.post("/{study_id}/members/invite", response_model=StudyMemberOut)
def invite_study_member_endpoint(
    study_id: UUID,
    payload: StudyMemberInvite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Invite a new member to the study by email."""
    return study_member_service.invite_member(
        db=db, study_id=study_id, inviter=current_user, payload=payload
    )


@router.get("/{study_id}/members", response_model=List[StudyMemberOut])
def list_study_members_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all members of a study, including the creator as admin."""
    # Ensure current user has access to see members
    # (Creators or existing members can see others)
    study_service.check_study_access(db=db, study_id=study_id, user_id=current_user.id)
    
    members = study_member_service.list_members(db=db, study_id=study_id)
    
    # Enrich members with user details (name, is_registered)
    enriched_members = []
    for member in members:
        member_dict = {
            "id": member.id,
            "user_id": member.user_id,
            "email": member.invited_email,
            "role": member.role,
            "created_at": member.created_at,
            "updated_at": member.updated_at,
            "name": None,
            "is_registered": False
        }
        
        # If user relationship is loaded, get the name
        if hasattr(member, 'user') and member.user:
            member_dict["name"] = member.user.name
            member_dict["is_registered"] = True
        
        enriched_members.append(member_dict)
    
    return enriched_members


@router.patch("/{study_id}/members/{member_id}", response_model=StudyMemberOut)
def update_study_member_endpoint(
    study_id: UUID,
    member_id: UUID,
    payload: StudyMemberUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a study member's role."""
    return study_member_service.update_member_role(
        db=db, study_id=study_id, member_id=member_id, current_user=current_user, payload=payload
    )


@router.delete("/{study_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_study_member_endpoint(
    study_id: UUID,
    member_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a member from the study."""
    study_member_service.remove_member(
        db=db, study_id=study_id, member_id=member_id, current_user=current_user
    )
    return None


@router.get("/{study_id}/stats")
def study_stats_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    return {
        "total_responses": study.total_responses,
        "completed_responses": study.completed_responses,
        "abandoned_responses": study.abandoned_responses,
        "status": study.status,
        "created_at": study.created_at,
        "launched_at": study.launched_at,
        "completed_at": study.completed_at,
    }


@router.get("/{study_id}/share-url")
def share_url_endpoint(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    study = study_service.get_study(db=db, study_id=study_id, owner_id=current_user.id)
    return {"share_token": study.share_token, "share_url": study.share_url}


def _validate_design_constraints_payload(payload: GenerateTasksRequest) -> ValidateDesignConstraintsResponse:
    if payload.study_type != "layer":
        return ValidateDesignConstraintsResponse(
            feasible=True,
            reason="Design constraints apply only to layer studies.",
            valid_row_variety=0,
            required_row_variety=0,
            tasks_per_respondent=0,
            constraints_checked=0,
            incompatible_pairs_count=0,
            skipped_constraint_refs=0,
            suggestions=[],
        )

    design_constraints = [c.model_dump(exclude_none=True) for c in (payload.design_constraints or [])]
    if not design_constraints:
        return ValidateDesignConstraintsResponse(
            feasible=True,
            reason="No design constraints to validate.",
            valid_row_variety=0,
            required_row_variety=0,
            tasks_per_respondent=int(payload.tasks_per_respondent or 0),
            constraints_checked=0,
            incompatible_pairs_count=0,
            skipped_constraint_refs=0,
            suggestions=[],
        )

    if not payload.study_layers:
        return ValidateDesignConstraintsResponse(
            feasible=False,
            reason="Layer study requires study_layers before validating constraints.",
            valid_row_variety=0,
            required_row_variety=0,
            tasks_per_respondent=int(payload.tasks_per_respondent or 0),
            constraints_checked=len(design_constraints),
            incompatible_pairs_count=0,
            skipped_constraint_refs=0,
            suggestions=["Add layers and images before generating tasks."],
        )

    layers_data = [
        {
            "layer_id": layer.layer_id,
            "name": layer.name,
            "z_index": layer.z_index,
            "order": layer.order,
            "images": [
                {
                    "image_id": image.image_id,
                    "name": image.name,
                    "url": image.url,
                    "alt_text": image.alt_text or "",
                }
                for image in (layer.images or [])
            ],
        }
        for layer in payload.study_layers
    ]

    from app.services.golden_task_generator import validate_layer_design_constraints_fast
    result = validate_layer_design_constraints_fast(
        layers_data=layers_data,
        design_constraints=design_constraints,
        tasks_per_respondent=int(payload.tasks_per_respondent or 0),
    )
    return ValidateDesignConstraintsResponse(**result)


@router.post("/validate-design-constraints", response_model=ValidateDesignConstraintsResponse)
def validate_design_constraints_endpoint(
    payload: GenerateTasksRequest,
    current_user: User = Depends(get_current_active_user),
):
    return _validate_design_constraints_payload(payload)


@router.post("/generate-tasks", response_model=GenerateTasksResult)
def generate_tasks_from_body_endpoint(
    payload: GenerateTasksRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Check if we should use async processing for large studies
    number_of_respondents = int(payload.audience_segmentation.number_of_respondents or 0) if payload.audience_segmentation else 0
    
    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Requested respondents: {number_of_respondents}, threshold: {settings.MAX_RESPONDENTS_FOR_SYNC}")
    logger.info(f"Using background processing: {number_of_respondents > settings.MAX_RESPONDENTS_FOR_SYNC}")

    constraint_validation = _validate_design_constraints_payload(payload)
    if not constraint_validation.feasible:
        raise HTTPException(status_code=400, detail=constraint_validation.model_dump())
    
    if number_of_respondents > settings.MAX_RESPONDENTS_FOR_SYNC:
        # Use async background processing for large studies
        from app.services.background_task_service import background_task_service
        
        # Create study first if needed
        study_row = _ensure_study_exists(payload, db, current_user)
        if payload.design_constraints is not None:
            study_row.design_constraints = [c.model_dump(exclude_none=True) for c in payload.design_constraints]
        
        # Log whether this is a new study or existing study
        if payload.study_id:
            logger.info(f"Regenerating tasks for existing study {payload.study_id}")
        else:
            logger.info(f"Creating new study for task generation")
        
        # Create background job
        job_id = background_task_service.create_job(
            study_id=str(study_row.id),
            user_id=str(current_user.id),
            payload=payload.model_dump()
        )

        # Store jobid in the study database
        study_row.jobid = job_id
        db.commit()

        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Created background job {job_id} for {number_of_respondents} respondents")

        if getattr(settings, "USE_CELERY", False):
            from app.tasks.celery_jobs import generate_tasks_celery
            generate_tasks_celery.delay(
                job_id=job_id,
                study_id=str(study_row.id),
                user_id=str(current_user.id),
                payload=payload.model_dump(mode="json"),
            )
            logger.info(f"Dispatched job {job_id} to Celery worker")
        else:
            # Start the job asynchronously using a background thread
            import threading
            import asyncio

            def run_background_job():
                """Run the background job in a separate thread with its own event loop"""
                import logging
                _logger = logging.getLogger(__name__)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                from app.db.session import SessionLocal
                db_bg = SessionLocal()
                try:
                    _logger.info(f"Starting background job {job_id}")
                    task = loop.run_until_complete(background_task_service.start_job(job_id, db_bg))
                    _logger.info(f"Background job {job_id} started, awaiting completion...")
                    loop.run_until_complete(task)
                    _logger.info(f"Background job {job_id} completed successfully")
                except Exception as e:
                    _logger.error(f"Background job {job_id} failed: {e}")
                    import traceback
                    _logger.error(f"Traceback: {traceback.format_exc()}")
                    from app.db.session import SessionLocal as _SessionLocal
                    from app.models.job_model import Job, JobStatus
                    _db = _SessionLocal()
                    try:
                        _job = _db.query(Job).filter(Job.job_id == job_id).first()
                        if _job:
                            _job.status = JobStatus.FAILED
                            _job.error = str(e)
                            _job.message = f"Background job failed: {str(e)}"
                            _job.completed_at = datetime.utcnow()
                            _db.commit()
                    finally:
                        _db.close()
                finally:
                    try:
                        db_bg.close()
                    except Exception:
                        pass
                    loop.close()

            thread = threading.Thread(target=run_background_job, daemon=True)
            thread.start()
        
        # Return only job metadata; frontend will poll for status/results
        logger.info(f"Returning background job response for job {job_id}")
        return GenerateTasksResult(
            last_step=payload.last_step,  # Return the last_step from payload
            tasks={},
            metadata={
                "job_id": job_id,
                "status": "started",
                "message": f"Task generation started in background for {number_of_respondents} respondents",
                "study_id": str(study_row.id)
            }
        )
    
    # Use synchronous processing for small studies (existing behavior)
    # Upsert draft study if requested (create if no study_id provided, else load to update)
    from sqlalchemy import select
    from app.models.study_model import Study as StudyModel
    study_row = None
    if payload.study_id is not None:
        study_row = db.scalars(
            select(StudyModel).where(StudyModel.id == payload.study_id, StudyModel.creator_id == current_user.id)
        ).first()

        if not study_row:
            # Check if user is a member
            stmt_member = (
                select(StudyModel)
                .join(StudyMember, StudyMember.study_id == StudyModel.id)
                .where(StudyModel.id == payload.study_id, StudyMember.user_id == current_user.id)
            )
            study_row = db.scalars(stmt_member).first()
            if study_row:
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"User {current_user.id} (Member) is regenerating tasks for study {payload.study_id}")

        if not study_row:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Sync task generation: Study not found or access denied for user {current_user.id}, study {payload.study_id}")
            raise HTTPException(status_code=404, detail="Study not found or access denied.")
    else:
        # Create a new draft study if minimal required fields provided
        if not payload.title or not payload.background or not payload.language or not payload.main_question or not payload.orientation_text:
            raise HTTPException(status_code=400, detail="Missing required study fields for on-the-fly creation")
        study_row = study_service.create_study(
            db=db,
            creator_id=current_user.id,
            payload=StudyCreate(
                title=payload.title,
                background=payload.background,
                language=payload.language,
                main_question=payload.main_question,
                orientation_text=payload.orientation_text,
                study_type=payload.study_type,
                background_image_url=payload.background_image_url,
                aspect_ratio=payload.aspect_ratio,
                rating_scale=payload.rating_scale,
                audience_segmentation=payload.audience_segmentation,
                categories=payload.categories,
                elements=payload.elements,
                study_layers=payload.study_layers,
                design_constraints=payload.design_constraints,
                classification_questions=payload.classification_questions,
            ),
            base_url_for_share=settings.BASE_URL,
        )

    if payload.design_constraints is not None:
        study_row.design_constraints = [c.model_dump(exclude_none=True) for c in payload.design_constraints]

    if payload.study_type in ('grid', 'text'):
        if not payload.elements or len(payload.elements) == 0:
            raise HTTPException(status_code=400, detail="Grid study requires elements")
        if not payload.categories or len(payload.categories) == 0:
            raise HTTPException(status_code=400, detail="Grid study requires categories")
        
        num_elements = len(payload.elements)
        elements = []
        # Use StudyElement dataclass shape lightly: only content is used in generator for grid
        for idx, e in enumerate(payload.elements):
            class _Tmp:
                content: str = e.content
            elements.append(_Tmp())
        
        # Build categories_data from payload for v2 functions
        categories_data = []
        for cat in payload.categories:
            cat_elements = [e for e in payload.elements if e.category_id == cat.category_id]
            categories_data.append({
                "category_name": cat.name,
                "elements": [
                    {
                        "element_id": str(el.element_id),  # Convert UUID to string
                        "name": el.name,
                        "content": el.content,
                        "alt_text": el.alt_text or el.name,
                        "element_type": el.element_type,
                    }
                    for el in cat_elements
                ]
            })

        # Feasibility preflight: try planning and locking T quickly; if it fails, raise 400 with guidance
        try:
            from app.services.task_generation_core import plan_T_E_auto, preflight_lock_T, MIN_ACTIVE_PER_ROW, GRID_MAX_ACTIVE
            # Convert to category_info: {CategoryName: [CategoryName_1, ...]}
            category_info: Dict[str, List[str]] = {}
            for c in categories_data:
                cname = c["category_name"]
                category_info[cname] = [f"{cname}_{j+1}" for j in range(len(c["elements"]))]

            if any(len(v) == 0 for v in category_info.values()):
                raise HTTPException(status_code=400, detail="Each category must have at least 1 element")

            mode = "grid"
            max_active_per_row = min(GRID_MAX_ACTIVE, len(category_info))
            T, E, A_map, avg_k, A_min_used = plan_T_E_auto(category_info, mode, max_active_per_row)
            # Attempt to lock T (cheap attempts). If it raises, convert to HTTP 400 with advice
            try:
                _T_locked, _A_map_locked = preflight_lock_T(T, category_info, E, A_min_used, mode, max_active_per_row)
            except RuntimeError as e:
                # Build a simple advisory message for the frontend
                # Heuristic: suggest adding at least 1 more element to any category with only 1
                thin_cats = [cn for cn, arr in category_info.items() if len(arr) < 2]
                advice = "; ".join([
                    "Add more elements per category (≥2 recommended)",
                    "Reduce tasks per respondent (T)",
                    "Relax absence ratio"
                ])
                if thin_cats:
                    advice = f"Categories with very few elements: {', '.join(thin_cats)}. " + advice
                raise HTTPException(status_code=400, detail=f"Task generation not feasible with current elements/categories. {advice}")
        except HTTPException:
            raise
        except Exception:
            # If the quick preflight itself fails for unexpected reasons, proceed to generation as before
            pass
        
        from app.services.golden_task_generator import generate_grid_tasks_golden
        result = generate_grid_tasks_golden(
            categories_data=categories_data,
            number_of_respondents=payload.audience_segmentation.number_of_respondents,
            exposure_tolerance_cv=payload.exposure_tolerance_cv or 1.0,
            seed=payload.seed,
            tasks_per_respondent=int(payload.tasks_per_respondent or 0),
        )
        # Persist tasks to new table
        tasks = result.get('tasks', {})
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study_row.id, tasks)
        
        # Update last_step if provided and greater than current
        if payload.last_step is not None:
            current_step = getattr(study_row, 'last_step', 1) or 1
            if payload.last_step > current_step:
                setattr(study_row, 'last_step', payload.last_step)
        db.commit()
        # Refresh to get the updated last_step value
        db.refresh(study_row)
        return GenerateTasksResult(
            last_step=getattr(study_row, 'last_step', None),
            tasks=tasks,
            metadata={
                **result.get('metadata', {}),
                "study_id": str(study_row.id)
            }
        )
    elif payload.study_type == 'layer':
        if not payload.study_layers or len(payload.study_layers) == 0:
            raise HTTPException(status_code=400, detail="Layer study requires study_layers")
        # Build a minimal StudyLayer-like structure for adapter
        class _TmpImg:
            def __init__(self, image_id: str, name: str, url: str):
                self.image_id = image_id
                self.name = name
                self.url = url
        class _TmpLayer:
            def __init__(self, layer_id: str, name: str, images: list, z_index: int, order: int):
                self.layer_id = layer_id
                self.name = name
                self.images = images
                self.z_index = z_index
                self.order = order
        layers = []
        for L in payload.study_layers:
            imgs = [_TmpImg(img.image_id, img.name, img.url) for img in L.images or []]
            layers.append(_TmpLayer(L.layer_id, L.name, imgs, L.z_index, L.order))

        # Feasibility preflight for layer mode
        try:
            from app.services.task_generation_core import plan_T_E_auto, preflight_lock_T
            # Convert to category_info: {LayerName: [LayerName_1, ...]}
            category_info: Dict[str, List[str]] = {}
            for L in payload.study_layers:
                count = len(L.images or [])
                category_info[L.name] = [f"{L.name}_{i+1}" for i in range(count)]
            if any(len(v) == 0 for v in category_info.values()):
                raise HTTPException(status_code=400, detail="Each layer must have at least 1 image")
            mode = "layout"
            max_active_per_row = len(category_info)
            T, E, A_map, avg_k, A_min_used = plan_T_E_auto(category_info, mode, max_active_per_row)
            try:
                _T_locked, _A_map_locked = preflight_lock_T(T, category_info, E, A_min_used, mode, max_active_per_row)
            except RuntimeError as e:
                thin_layers = [cn for cn, arr in category_info.items() if len(arr) < 2]
                advice = "; ".join([
                    "Add more images per layer (≥2 recommended)",
                    "Reduce tasks per respondent (T)",
                    "Relax absence ratio"
                ])
                if thin_layers:
                    advice = f"Layers with very few images: {', '.join(thin_layers)}. " + advice
                raise HTTPException(status_code=400, detail=f"Task generation not feasible with current layers/images. {advice}")
        except HTTPException:
            raise
        except Exception:
            # On unexpected preflight errors, continue to generation
            pass
        result = generate_layer_tasks(
            layers=layers,
            number_of_respondents=payload.audience_segmentation.number_of_respondents,
            exposure_tolerance_pct=payload.exposure_tolerance_pct or 2.0,
            seed=payload.seed,
            tasks_per_consumer=int(payload.tasks_per_respondent or 0),
            design_constraints=[c.model_dump(exclude_none=True) for c in (payload.design_constraints or [])],
        )
        # Persist tasks to new table
        tasks = result.get('tasks', {})
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study_row.id, tasks)
        
        # Update last_step if provided and greater than current
        if payload.last_step is not None:
            current_step = getattr(study_row, 'last_step', 1) or 1
            if payload.last_step > current_step:
                setattr(study_row, 'last_step', payload.last_step)
        db.commit()
        # Refresh to get the updated last_step value
        db.refresh(study_row)
        # Include aspect_ratio in metadata if present (from study_row.audience_segmentation)
        ar = None
        try:
            ar = (study_row.audience_segmentation or {}).get('aspect_ratio')
        except Exception:
            ar = None
        meta = {
            **result.get('metadata', {}),
            "study_id": str(study_row.id)
        }
        if ar:
            meta['aspect_ratio'] = ar
        return GenerateTasksResult(
            last_step=getattr(study_row, 'last_step', None),
            tasks=tasks,
            metadata=meta
        )
    elif payload.study_type == 'hybrid':
        if not payload.elements or not payload.categories or not payload.phase_order:
            raise HTTPException(status_code=400, detail="Hybrid study requires elements, categories, and phase_order")

        import random
        from app.services.golden_task_generator import generate_grid_tasks_golden

        combined_tasks = {}
        combined_metadata = {"phases": []}
        is_mix = "mix" in payload.phase_order
        if is_mix:
            target_phases = list({c.phase_type for c in payload.categories if c.phase_type})
        else:
            target_phases = payload.phase_order

        if not target_phases:
            raise HTTPException(status_code=400, detail="Hybrid study requires at least one configured phase")

        tasks_per_respondent_mix = {}
        for phase_type in target_phases:
            phase_categories = [c for c in payload.categories if c.phase_type == phase_type]
            if not phase_categories:
                continue

            categories_data = []
            for cat in phase_categories:
                cat_elements = [e for e in payload.elements if e.category_id == cat.category_id]
                categories_data.append({
                    "category_name": cat.name,
                    "elements": [
                        {
                            "element_id": str(el.element_id),
                            "name": el.name,
                            "content": el.content,
                            "alt_text": el.alt_text or el.name,
                            "element_type": el.element_type,
                        }
                        for el in cat_elements
                    ]
                })

            phase_result = generate_grid_tasks_golden(
                categories_data=categories_data,
                number_of_respondents=payload.audience_segmentation.number_of_respondents,
                exposure_tolerance_cv=payload.exposure_tolerance_cv or 1.0,
                seed=payload.seed,
                tasks_per_respondent=int(payload.tasks_per_respondent or 0),
            )
            phase_tasks = phase_result.get('tasks', {})

            if is_mix:
                for resp_id, task_list in phase_tasks.items():
                    tasks_per_respondent_mix.setdefault(resp_id, [])
                    for task in task_list:
                        task['phase_type'] = phase_type
                        tasks_per_respondent_mix[resp_id].append(task)
            else:
                for resp_id, task_list in phase_tasks.items():
                    combined_tasks.setdefault(resp_id, [])
                    offset = len(combined_tasks[resp_id])
                    for task in task_list:
                        task['task_index'] = int(task.get('task_index', 0)) + offset
                        task['phase_type'] = phase_type
                    combined_tasks[resp_id].extend(task_list)

            combined_metadata["phases"].append({
                "phase_type": phase_type,
                "metadata": phase_result.get('metadata', {})
            })

        if is_mix:
            for resp_id, task_list in tasks_per_respondent_mix.items():
                random.shuffle(task_list)
                for idx, task in enumerate(task_list):
                    task['task_index'] = idx
                combined_tasks[resp_id] = task_list

        if not combined_tasks:
            raise HTTPException(status_code=400, detail="No hybrid tasks could be generated for the configured phases")

        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study_row.id, combined_tasks)

        if payload.last_step is not None:
            current_step = getattr(study_row, 'last_step', 1) or 1
            if payload.last_step > current_step:
                setattr(study_row, 'last_step', payload.last_step)
        db.commit()
        db.refresh(study_row)
        return GenerateTasksResult(
            last_step=getattr(study_row, 'last_step', None),
            tasks=combined_tasks,
            metadata={
                **combined_metadata,
                "study_id": str(study_row.id)
            }
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported study_type")


@router.put("/{study_id}/launch", response_model=StudyLaunchOut)
def update_and_launch_study_endpoint(
    study_id: UUID,
    payload: StudyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Ultra-fast path: update basic study details (if provided) and set study to active in one call.
    Uses optimized functions to minimize database round trips and avoid unnecessary data loading.
    Does not regenerate tasks; intended for non-task-affecting edits (e.g., title, text).
    """
    # Check if payload is empty (launch-only)
    previous_project_id = db.scalar(select(Study.project_id).where(Study.id == study_id))
    payload_dict = payload.model_dump(exclude_none=True)
    if not payload_dict:
        # Ultra-fast launch-only path
        study = study_service.launch_study_ultra_fast(
            db=db,
            study_id=study_id,
            owner_id=current_user.id,
        )
    else:
        # Use optimized function that combines update and launch in a single transaction
        study = study_service.update_and_launch_study_fast(
            db=db,
            study_id=study_id,
            owner_id=current_user.id,
            payload=payload,
        )
    invalidate_study_cache(study_id)
    _invalidate_user_studies_list_cache(current_user.id)
    _invalidate_project_public_studies_cache(previous_project_id)
    _invalidate_project_public_studies_cache(getattr(study, "project_id", None))
    return study


@router.get("/simulate-ai-respondents/status/{job_id}")
def get_simulate_ai_respondents_status(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """
    Get simulate-ai-respondents job progress. Returns in milliseconds.
    Safe for Azure - no long-lived connection. Poll every 2 seconds until status is completed/failed/cancelled.
    Same response shape as the stream events.
    """
    from app.services.background_task_service import background_task_service

    job = background_task_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    result = job.result or {}
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="Not a simulate-ai-respondents job")
    inner_payload = result.get("payload") or {}
    run_result = result.get("run_result") or {}
    # Accept either payload.job_type (before/during run) or run_result.respondents_simulated (after completion)
    is_simulate = (isinstance(inner_payload, dict) and inner_payload.get("job_type") == "simulate_ai_respondents") or (
        "respondents_simulated" in run_result
    )
    if not is_simulate:
        raise HTTPException(status_code=400, detail="Not a simulate-ai-respondents job")

    respondents_requested = inner_payload.get("max_respondents") if isinstance(inner_payload, dict) else None

    return {
        "job_id": job.job_id,
        "study_id": job.study_id,
        "status": job.status.value,
        "progress": float(job.progress or 0),
        "message": job.message or "",
        "respondents_requested": respondents_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error,
        "result": job.result,
    }


@router.get("/simulate-ai-respondents/stream/{job_id}")
async def stream_simulate_ai_respondents_progress(
    job_id: str,
    interval_seconds: float = Query(1.5, ge=0.5, le=10, description="Poll interval in seconds"),
    current_user: User = Depends(get_current_active_user),
):
    """
    Server-Sent Events (SSE) stream for simulate-ai-respondents job progress.
    Emits events with progress, message (e.g. "1/200 respondent completed"), status.
    Frontend can use EventSource to show real-time loader.
    """
    import asyncio
    import json
    from app.services.background_task_service import background_task_service
    from app.db.session import SessionLocal
    from app.models.job_model import Job

    job = background_task_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    result = job.result or {}
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="Not a simulate-ai-respondents job")
    inner_payload = result.get("payload") or {}
    if not isinstance(inner_payload, dict) or inner_payload.get("job_type") != "simulate_ai_respondents":
        raise HTTPException(status_code=400, detail="Not a simulate-ai-respondents job")

    async def event_generator():
        last_msg = None
        while True:
            db = SessionLocal()
            try:
                j = db.query(Job).filter_by(job_id=job_id).first()
                if not j:
                    break
                status_val = j.status.value if hasattr(j.status, "value") else str(j.status)
                msg = j.message or ""
                prog = float(j.progress or 0)
                if (msg, prog, status_val) != last_msg:
                    data = {
                        "job_id": job_id,
                        "status": status_val,
                        "progress": prog,
                        "message": msg,
                        "respondents_requested": inner_payload.get("max_respondents"),
                    }
                    yield f"data: {json.dumps(data)}\n\n"
                    last_msg = (msg, prog, status_val)
                if status_val in ("completed", "failed", "cancelled"):
                    break
            finally:
                db.close()
            await asyncio.sleep(interval_seconds)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/generate-tasks/status/{job_id}")
def get_task_generation_status(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get the status of a task generation job. Any study creator or member can view."""
    from app.services.background_task_service import background_task_service
    
    job = background_task_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if not _user_has_study_access(db, UUID(job.study_id), current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "job_id": job.job_id,
        "study_id": job.study_id,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error,
        "result": job.result
    }


@router.post("/generate-tasks/cancel/{job_id}")
def cancel_task_generation(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Cancel a running task generation job. Any study creator or member can cancel."""
    from app.services.background_task_service import background_task_service
    
    job = background_task_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if not _user_has_study_access(db, UUID(job.study_id), current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    
    success = background_task_service.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")
    
    return {"message": "Job cancelled successfully"}


@router.get("/generate-tasks/jobs")
def get_user_task_generation_jobs(
    current_user: User = Depends(get_current_active_user),
):
    """Get all task generation jobs for the current user"""
    from app.services.background_task_service import background_task_service
    
    jobs = background_task_service.get_user_jobs(str(current_user.id))
    
    return [
        {
            "job_id": job.job_id,
            "study_id": job.study_id,
            "status": job.status.value,
            "progress": job.progress,
            "message": job.message,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "error": job.error
        }
        for job in jobs
    ]


@router.get("/generate-tasks/result/{job_id}")
def get_task_generation_result(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get the full generated tasks once the background job is complete"""
    from app.services.background_task_service import background_task_service
    
    job = background_task_service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if not _user_has_study_access(db, UUID(job.study_id), current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if job is completed
    from app.services.background_task_service import JobStatus
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=400, 
            detail=f"Job is not completed yet. Current status: {job.status.value}"
        )
    
    # Get the study with the generated tasks (includes layers and images)
    study = study_service.get_study(db=db, study_id=UUID(job.study_id), owner_id=current_user.id)

    # Clear the jobid from the database now that we're retrieving the results
    study.jobid = None
    
    # Job is NOT deleted here to prevent race conditions (404s) on multiple requests.
    # The background system cleanup (every 24h) will handle deletion.
    
    db.commit()

    # Prepare lightweight layer metadata including transform
    layers = []
    try:
        for L in (study.layers or []):
            default_transform = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
            layers.append({
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
    except Exception:
        # If any unexpected serialization issue occurs, fall back without layers
        layers = []

    # Load tasks using TaskService
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    enriched_tasks = task_service.get_all_tasks_as_dict(UUID(job.study_id))
    
    # Enrich tasks with transform inside each non-null elements_shown_content entry
    try:
        # Build layer_name -> transform map (default when missing)
        transform_map = {}
        for L in (study.layers or []):
            default_transform = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
            transform_map[L.name] = (L.transform or default_transform)

        new_tasks = {}
        for respondent_id, task_list in (enriched_tasks.items() if isinstance(enriched_tasks, dict) else {}).items():
            new_task_list = []
            for task in task_list:
                esc = task.get("elements_shown_content") or {}
                for key, val in esc.items():
                    if isinstance(val, dict):
                        layer_name = val.get("layer_name")
                        if layer_name and layer_name in transform_map:
                            val["transform"] = transform_map[layer_name]
                new_task_list.append(task)
            new_tasks[respondent_id] = new_task_list
        enriched_tasks = new_tasks
    except Exception:
        # On any enrichment error, keep tasks as loaded
        pass

    return {
        "job_id": job.job_id,
        "study_id": job.study_id,
        "status": job.status.value,
        "tasks": enriched_tasks,
        "metadata": {
            "total_respondents": len([k for k in enriched_tasks if str(k).isdigit()]) if isinstance(enriched_tasks, dict) else 0,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "message": "Task generation completed successfully"
        },
        "layers": layers
    }