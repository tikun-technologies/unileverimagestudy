# app/services/response.py
from __future__ import annotations

from typing import Optional, List, Dict, Any, Tuple, Iterable, Callable
from uuid import UUID, uuid4
from datetime import datetime, timedelta
import time
import random
import pandas as pd

from sqlalchemy.orm import Session, selectinload, load_only
from sqlalchemy import select, func, desc, and_, or_, text, delete
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status

from app.models.response_model import (
    StudyResponse, CompletedTask, ClassificationAnswer, 
    ElementInteraction, TaskSession
)
from app.models.panelist_model import Panelist
from app.models.study_model import Study, StudyClassificationQuestion, StudyElement
from app.schemas.response_schema import (
    StudyResponseCreate, StudyResponseUpdate, StudyResponseOut,
    CompletedTaskCreate, ClassificationAnswerCreate,
    ElementInteractionCreate, TaskSessionCreate,
    StartStudyRequest, StartStudyResponse, SubmitTaskRequest,
    SubmitTaskResponse, SubmitClassificationRequest, BulkSubmitTasksRequest, BulkSubmitTasksResponse,
    BulkSubmitTaskItem,
    AbandonStudyRequest, StudyAnalytics, ResponseAnalytics,
    SyntheticRespondentPayload,
)

# ---------- Study Response Service ----------

class StudyResponseService:
    """Service for managing study responses and participant sessions."""
    
    def __init__(self, db: Session):
        self.db = db
        # Lightweight in-process cache for analytics to accelerate repeated GETs
        # Keyed by study_id; values are (expires_at_epoch_seconds, StudyAnalytics)
        if not hasattr(StudyResponseService, "_analytics_cache"):
            StudyResponseService._analytics_cache: Dict[str, Tuple[float, Any]] = {}
        if not hasattr(StudyResponseService, "_analytics_ttl_seconds"):
            StudyResponseService._analytics_ttl_seconds: int = 15
    
    # ---------- Core CRUD Operations ----------
    
    def create_response(self, response_data: StudyResponseCreate) -> StudyResponse:
        """Create a new study response session."""
        # Validate study exists and is active
        study = self.db.get(Study, response_data.study_id)
        if not study:
            raise HTTPException(
                status_code=404, 
                detail="Study not found"
            )
        
        if study.status != 'active':
            raise HTTPException(
                status_code=400,
                detail="Study is not active"
            )
        
        # Generate unique session ID
        session_id = f"session_{uuid4().hex[:16]}"
        
        def _payload_builder(respondent_id: int) -> Dict[str, Any]:
            total_tasks_assigned = 0
            try:
                from app.services.task_service import TaskService
                task_service = TaskService(self.db)
                respondent_tasks = task_service.get_respondent_tasks(study.id, respondent_id)
                total_tasks_assigned = len(respondent_tasks)
            except Exception:
                total_tasks_assigned = 0

            return {
                "study_id": response_data.study_id,
                "session_id": session_id,
                "total_tasks_assigned": total_tasks_assigned,
                "session_start_time": response_data.session_start_time or datetime.utcnow(),
                "personal_info": response_data.personal_info,
                "ip_address": response_data.ip_address,
                "user_agent": response_data.user_agent,
                "browser_info": response_data.browser_info,
                "product_id": getattr(response_data, 'product_id', None),
                "last_activity": datetime.utcnow(),
                "status": "in_progress",
                "is_abandoned": False,
                "is_completed": False,
            }

        response = self._create_response_with_respondent_retry(
            study_id=response_data.study_id,
            payload_builder=_payload_builder,
            max_retries=3,
        )
        self.db.refresh(response)
        
        # Update study response counters
        self._update_study_counters(response_data.study_id)
        
        return response
    
    def get_response(self, response_id: UUID) -> Optional[StudyResponse]:
        """Get a study response by ID."""
        return self.db.get(StudyResponse, response_id)
    
    def get_response_by_session(self, session_id: str) -> Optional[StudyResponse]:
        """Get a study response by session ID."""
        stmt = select(StudyResponse).where(StudyResponse.session_id == session_id)
        return self.db.execute(stmt).scalars().first()

    def _optional_classification_question_ids(self, study_id: UUID) -> List[str]:
        """Return post-task classification question ids for a study."""
        questions = self.db.execute(
            select(StudyClassificationQuestion.question_id, StudyClassificationQuestion.config)
            .where(StudyClassificationQuestion.study_id == study_id)
        ).all()
        ids: List[str] = []
        for question_id, config in questions:
            if isinstance(config, dict) and bool(config.get("optional_classification_question", False)):
                ids.append(str(question_id))
        return ids

    def _has_unanswered_optional_classification(self, response: StudyResponse) -> bool:
        """Check whether a task-complete response still needs post-task answers."""
        optional_question_ids = set(self._optional_classification_question_ids(response.study_id))
        if not optional_question_ids:
            return False

        answered_question_ids = set(
            self.db.execute(
                select(ClassificationAnswer.question_id)
                .where(ClassificationAnswer.study_response_id == response.id)
            ).scalars().all()
        )
        return not optional_question_ids.issubset(answered_question_ids)

    def _get_response_by_session_for_update(self, session_id: str) -> Optional[StudyResponse]:
        """Get a study response by session ID with row-level lock for safe progress updates."""
        stmt = (
            select(StudyResponse)
            .where(StudyResponse.session_id == session_id)
            .with_for_update()
        )
        return self.db.execute(stmt).scalars().first()

    def _completed_task_exists(
        self,
        study_response_id: UUID,
        task_id: str,
        task_type: Optional[str] = None,
    ) -> bool:
        """Check if a task is already stored for a response (phase-aware idempotency guard)."""
        conditions = [
            CompletedTask.study_response_id == study_response_id,
            CompletedTask.task_id == task_id,
        ]
        if task_type is not None:
            conditions.append(CompletedTask.task_type == task_type)
        exists_stmt = select(CompletedTask.id).where(and_(*conditions)).limit(1)
        return self.db.execute(exists_stmt).scalar_one_or_none() is not None
    
    def get_response_by_respondent_id(self, respondent_id: int) -> Optional[StudyResponse]:
        """Get a study response by respondent ID."""
        stmt = select(StudyResponse).where(StudyResponse.respondent_id == respondent_id)
        return self.db.execute(stmt).scalars().first()
    
    def get_response_by_respondent_and_study(self, respondent_id: int, study_id: UUID) -> Optional[StudyResponse]:
        """Get a study response by respondent ID and study ID."""
        stmt = select(StudyResponse).where(
            and_(
                StudyResponse.respondent_id == respondent_id,
                StudyResponse.study_id == study_id
            )
        )
        return self.db.execute(stmt).scalars().first()
    
    def get_respondent_ids_for_study(self, study_id: UUID) -> List[int]:
        """Get all respondent IDs for a specific study."""
        stmt = select(StudyResponse.respondent_id).where(StudyResponse.study_id == study_id)
        results = self.db.execute(stmt).scalars().all()
        return list(results)
    
    def get_respondent_tasks(self, study_id: UUID, respondent_id: int) -> List[Dict[str, Any]]:
        """Get tasks assigned to a specific respondent."""
        from app.services.task_service import TaskService
        task_service = TaskService(self.db)
        return task_service.get_respondent_tasks(study_id, respondent_id)
    
    def get_response_detail_by_session(self, session_id: str) -> Optional[StudyResponse]:
        """Get a study response by session ID with related details, optimized for speed."""
        stmt = (
            select(StudyResponse)
            .where(StudyResponse.session_id == session_id)
            .options(
                load_only(
                    StudyResponse.id,
                    StudyResponse.study_id,
                    StudyResponse.session_id,
                    StudyResponse.respondent_id,
                    StudyResponse.current_task_index,
                    StudyResponse.completed_tasks_count,
                    StudyResponse.total_tasks_assigned,
                    StudyResponse.session_start_time,
                    StudyResponse.session_end_time,
                    StudyResponse.is_completed,
                    StudyResponse.personal_info,
                    StudyResponse.ip_address,
                    StudyResponse.user_agent,
                    StudyResponse.browser_info,
                    StudyResponse.completion_percentage,
                    StudyResponse.total_study_duration,
                    StudyResponse.last_activity,
                    StudyResponse.is_abandoned,
                    StudyResponse.abandonment_timestamp,
                    StudyResponse.abandonment_reason,
                    StudyResponse.created_at,
                    StudyResponse.updated_at,
                ),
                selectinload(StudyResponse.completed_tasks).load_only(
                    CompletedTask.id,
                    CompletedTask.study_response_id,
                    CompletedTask.task_id,
                    CompletedTask.respondent_id,
                    CompletedTask.task_index,
                    CompletedTask.elements_shown_in_task,
                    CompletedTask.elements_shown_content,
                    CompletedTask.layers_shown_in_task,
                    CompletedTask.task_type,
                    CompletedTask.task_context,
                    CompletedTask.task_start_time,
                    CompletedTask.task_completion_time,
                    CompletedTask.task_duration_seconds,
                    CompletedTask.rating_given,
                    CompletedTask.rating_timestamp,
                ),
                selectinload(StudyResponse.classification_answers).load_only(
                    ClassificationAnswer.id,
                    ClassificationAnswer.study_response_id,
                    ClassificationAnswer.question_id,
                    ClassificationAnswer.question_text,
                    ClassificationAnswer.answer,
                    ClassificationAnswer.answer_timestamp,
                    ClassificationAnswer.time_spent_seconds,
                ),
                selectinload(StudyResponse.element_interactions).load_only(
                    ElementInteraction.id,
                    ElementInteraction.study_response_id,
                    ElementInteraction.task_session_id,
                    ElementInteraction.element_id,
                    ElementInteraction.view_time_seconds,
                    ElementInteraction.hover_count,
                    ElementInteraction.click_count,
                    ElementInteraction.first_view_time,
                    ElementInteraction.last_view_time,
                ),
                selectinload(StudyResponse.task_sessions).load_only(
                    TaskSession.id,
                    TaskSession.study_response_id,
                    TaskSession.session_id,
                    TaskSession.task_id,
                    TaskSession.classification_page_time,
                    TaskSession.orientation_page_time,
                    TaskSession.individual_task_page_times,
                    TaskSession.page_transitions,
                    TaskSession.is_completed,
                    TaskSession.abandonment_timestamp,
                    TaskSession.abandonment_reason,
                    TaskSession.recovery_attempts,
                    TaskSession.browser_performance,
                    TaskSession.page_load_times,
                    TaskSession.device_info,
                    TaskSession.screen_resolution,
                    TaskSession.created_at,
                    TaskSession.updated_at,
                ),
            )
        )
        return self.db.execute(stmt).scalars().first()
    
    def get_responses_by_study(self, study_id: UUID, limit: int = 100, offset: int = 0) -> List[StudyResponse]:
        """Get all responses for a study."""
        stmt = (
            select(StudyResponse)
            .where(StudyResponse.study_id == study_id)
            .order_by(desc(StudyResponse.created_at))
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_responses_by_study_filtered(
        self,
        study_id: UUID,
        limit: int = 100,
        offset: int = 0,
        is_completed: Optional[bool] = None,
        is_abandoned: Optional[bool] = None,
    ) -> List[StudyResponse]:
        """Get responses for a study with optional SQL-level completion/abandon filters."""
        conditions = [StudyResponse.study_id == study_id]
        if is_completed is not None:
            conditions.append(StudyResponse.is_completed == is_completed)
        if is_abandoned is not None:
            conditions.append(StudyResponse.is_abandoned == is_abandoned)

        stmt = (
            select(StudyResponse)
            .where(and_(*conditions))
            .order_by(desc(StudyResponse.created_at))
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())
    
    def update_response(self, response_id: UUID, update_data: StudyResponseUpdate) -> Optional[StudyResponse]:
        """Update a study response."""
        response = self.get_response(response_id)
        if not response:
            return None
        
        update_dict = update_data.model_dump(exclude_unset=True)
        for field, value in update_dict.items():
            setattr(response, field, value)
        
        response.updated_at = datetime.utcnow()
        response.last_activity = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(response)
        return response
    
    def delete_response(self, response_id: UUID) -> bool:
        """Delete a study response."""
        response = self.get_response(response_id)
        if not response:
            return False
        
        self.db.delete(response)
        self.db.commit()
        return True

    def delete_response_by_study_and_session(self, study_id: UUID, session_id: str) -> bool:
        """
        Fast path delete by (study_id, session_id).
        Uses indexed filters and avoids loading ORM objects into memory.
        """
        stmt = delete(StudyResponse).where(
            StudyResponse.study_id == study_id,
            StudyResponse.session_id == session_id,
        )
        result = self.db.execute(stmt)
        deleted = (result.rowcount or 0) > 0
        if not deleted:
            self.db.rollback()
            return False

        self._update_study_counters(study_id)
        self.db.commit()
        return True
    
    # ---------- Study Participation Flow ----------
    
    def start_study(self, request: StartStudyRequest, ip_address: str = None, user_agent: str = None) -> StartStudyResponse:
        """Ultra-fast start study - minimal DB operations for instant response."""
        from app.core.merged_studies import is_merged_study

        # Single query to get only essential study data
        study_row = self.db.execute(
            select(Study.status, Study.title, Study.study_type, Study.main_question, Study.orientation_text, Study.rating_scale, Study.tasks)
            .where(Study.id == request.study_id)
        ).first()
        
        if not study_row:
            raise HTTPException(status_code=404, detail="Study not found")
        
        if study_row.status != 'active':
            raise HTTPException(status_code=400, detail="Study is not active")
        
        from app.services.task_service import TaskService
        task_service = TaskService(self.db)
        if not task_service.has_tasks(request.study_id):
            raise HTTPException(status_code=400, detail="Study has no tasks")
        
        # Generate IDs - session_id without DB query, respondent_id with proper sequential logic
        from uuid import uuid4 as _uuid4
        session_id = f"session_{_uuid4().hex[:16]}"
        done_by_id = None
        if is_merged_study(request.study_id):
            done_by_id = (request.done_by_id or "").strip() or f"done_{_uuid4().hex[:12]}"

        def _payload_builder(respondent_id: int) -> Dict[str, Any]:
            respondent_tasks = task_service.get_respondent_tasks(request.study_id, respondent_id)
            total_tasks_assigned = len(respondent_tasks)
            payload = {
                "study_id": request.study_id,
                "session_id": session_id,
                "total_tasks_assigned": total_tasks_assigned,
                "session_start_time": now_utc,
                "personal_info": combined_personal_info,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "product_id": getattr(request, 'product_id', None),
                "last_activity": now_utc,
                "status": "in_progress",
                "is_abandoned": False,
                "is_completed": False,
            }
            if done_by_id:
                payload["done_by_id"] = done_by_id
            if getattr(request, "panelist_id", None):
                payload["panelist_id"] = request.panelist_id
            return payload
        
        # Minimal personal info merge
        combined_personal_info = None
        if request.personal_info:
            combined_personal_info = request.personal_info
        elif request.user_details:
            combined_personal_info = request.user_details.model_dump(exclude_none=True)
        
        now_utc = datetime.utcnow()
        
        new_response = self._create_response_with_respondent_retry(
            study_id=request.study_id,
            payload_builder=_payload_builder,
            max_retries=3,
        )
        
        # Check if study should be auto-completed immediately after new response is created
        # This ensures immediate completion when total_responses equals expected_respondents
        self._check_and_complete_studies(study_id=request.study_id)
        
        # Return immediately without refresh or counter updates
        return StartStudyResponse(
            session_id=session_id,
            respondent_id=new_response.respondent_id,
            total_tasks_assigned=new_response.total_tasks_assigned,
            done_by_id=done_by_id,
            study_info={
                "id": str(request.study_id),
                "title": study_row.title,
                "study_type": study_row.study_type,
                "main_question": study_row.main_question,
                "orientation_text": study_row.orientation_text,
                "rating_scale": study_row.rating_scale
            }
        )
    
    def submit_task(self, session_id: str, request: SubmitTaskRequest) -> SubmitTaskResponse:
        """Submit a completed task."""
        response = self._get_response_by_session_for_update(session_id)
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if response.is_completed:
            raise HTTPException(status_code=400, detail="Study already completed")
        if response.is_abandoned or str(response.status or "") != "in_progress":
            raise HTTPException(status_code=400, detail="Session is no longer active")

        study_status = self.db.execute(
            select(Study.status).where(Study.id == response.study_id)
        ).scalar_one_or_none()
        if str(study_status or "") == "completed":
            raise HTTPException(status_code=400, detail="Study is no longer accepting submissions")

        # Create completed task record (fast path - no heavy enrichment)
        now_utc = datetime.utcnow()
        # Accept visibility payload from either elements_shown_in_task or elements_shown
        try:
            raw_visibility = (
                getattr(request, 'elements_shown_in_task', None)
                or getattr(request, 'elements_shown', None)
                or {}
            )
        except Exception:
            raw_visibility = {}

        # Build a name-keyed visibility map for grid studies
        name_visibility: Dict[str, Any] = {}
        study_row: Optional[Study] = self.db.get(Study, response.study_id)
        
        # For hybrid studies, determine the effective phase_type from the task definition
        effective_study_type = str(study_row.study_type) if study_row else None
        task_phase_type: Optional[str] = None
        if study_row and str(study_row.study_type) == 'hybrid':
            try:
                from app.services.task_service import TaskService
                task_service = TaskService(self.db)
                task_def = task_service.get_task_by_index(
                    study_row.id, response.respondent_id, response.current_task_index
                )
                if task_def:
                    task_phase_type = task_def.get('phase_type')
                    if task_phase_type in ('grid', 'text'):
                        effective_study_type = task_phase_type
            except Exception:
                pass
        
        if study_row and effective_study_type in ('grid', 'text'):
            # Load element id->name map in E-number order
            elements = self.db.execute(
                select(StudyElement).where(StudyElement.study_id == study_row.id)
            ).scalars().all()
            id_to_name: Dict[str, str] = {str(e.element_id).upper(): e.name for e in elements}

            # If client didn't send anything, try fallback from task service
            payload = raw_visibility
            if not isinstance(payload, dict) or len(payload) == 0:
                try:
                    from app.services.task_service import TaskService
                    task_service = TaskService(self.db)
                    task_def = task_service.get_task_by_index(
                        study_row.id, response.respondent_id, response.current_task_index
                    )
                    if task_def:
                        payload = task_def.get('elements_shown_in_task') or task_def.get('elements_shown') or {}
                except Exception:
                    payload = {}

            # If payload is a JSON string, parse
            if isinstance(payload, str):
                try:
                    import json
                    payload = json.loads(payload)
                except Exception:
                    payload = {}

            # Normalize to name->0/1
            if isinstance(payload, dict):
                for eid, name in id_to_name.items():
                    val = payload.get(eid)
                    if val is None:
                        # Try content-based or by name key
                        val = payload.get(f"{eid}_content") or payload.get(name) or payload.get(f"{name}_content")
                        if isinstance(val, str):
                            present = 1 if val.strip() else 0
                        else:
                            try:
                                present = int(val) if val in (0,1) else (1 if val else 0)
                            except Exception:
                                present = 0
                    else:
                        try:
                            present = int(val) if val in (0,1) else (1 if val else 0)
                        except Exception:
                            present = 0
                    name_visibility[name.replace(' ', '_')] = 1 if present == 1 else 0
        elif study_row and str(study_row.study_type) == 'layer':
            # For layer studies, include z-index information in elements_shown_in_task
            from app.models.study_model import StudyLayer, LayerImage
            
            # Get layer structure with z-index information
            layers = self.db.execute(
                select(StudyLayer)
                .where(StudyLayer.study_id == study_row.id)
                .order_by(StudyLayer.order)
            ).scalars().all()
            
            # Create layer name to z-index mapping
            layer_name_to_z_index: Dict[str, int] = {}
            for layer in layers:
                layer_name_to_z_index[layer.name] = layer.z_index
            
            # Process the raw visibility data to include z-index
            if isinstance(raw_visibility, dict):
                for key, value in raw_visibility.items():
                    if isinstance(key, str):
                        # Extract layer name from key (e.g., "background_1" -> "background")
                        import re
                        match = re.search(r"^(.+?)_(\d+)$", key)
                        if match:
                            layer_name = match.group(1)
                            image_num = match.group(2)
                            
                            # Get z-index for this layer
                            z_index = layer_name_to_z_index.get(layer_name, 0)
                            
                            # Create enhanced visibility data with z-index
                            if isinstance(value, dict):
                                # If already a dict, add z_index to it
                                enhanced_value = value.copy()
                                enhanced_value["z_index"] = z_index
                                name_visibility[key] = enhanced_value
                            else:
                                # If simple value, create dict with visibility and z_index
                                name_visibility[key] = {
                                    "visible": 1 if value else 0,
                                    "z_index": z_index
                                }
                        else:
                            # Fallback for keys that don't match pattern
                            name_visibility[key] = value
            else:
                # If not a dict, keep as-is
                name_visibility = raw_visibility if isinstance(raw_visibility, dict) else {}
        else:
            # Non-grid or no study: keep as-is if dict, else empty
            name_visibility = raw_visibility if isinstance(raw_visibility, dict) else {}
        # For hybrid studies, use the phase_type as task_type; otherwise use study_type
        task_type_value = task_phase_type if task_phase_type else (str(study_row.study_type) if study_row else None)

        # Idempotency: ignore duplicate of same task_id within the same task phase/type.
        if task_type_value in ('grid', 'text', 'layer') and self._completed_task_exists(
            response.id,
            request.task_id,
            task_type=task_type_value,
        ):
            self.db.commit()
            return SubmitTaskResponse(
                success=True,
                next_task_index=response.current_task_index if not response.is_completed else None,
                is_study_complete=bool(response.is_completed),
                completion_percentage=float(response.completion_percentage or 0.0),
            )
        
        task_data = CompletedTaskCreate(
            task_id=request.task_id,
            respondent_id=response.respondent_id,
            task_index=response.current_task_index,
            elements_shown_in_task=name_visibility,
            task_type=task_type_value,
            task_start_time=now_utc - timedelta(seconds=request.task_duration_seconds),
            task_completion_time=now_utc,
            task_duration_seconds=request.task_duration_seconds,
            rating_given=request.rating_given,
            rating_timestamp=now_utc
        )
        
        completed_task = CompletedTask(**task_data.model_dump())
        completed_task.study_response_id = response.id
        
        # For layer studies, persist which layers/images were shown for this task
        if study_row and str(study_row.study_type) == 'layer':
            layer_payload: Any = None
            # Try request fields first if present in future clients
            try:
                layer_payload = getattr(request, 'layers_shown_in_task', None) or getattr(request, 'elements_shown_content', None)
            except Exception:
                layer_payload = None
            # Fallback to task definition from task service
            if layer_payload is None or layer_payload == {}:
                try:
                    from app.services.task_service import TaskService
                    task_service = TaskService(self.db)
                    # Note: current_task_index is 1-based after increment, so use -1 for 0-based lookup
                    task_def = task_service.get_task_by_index(
                        study_row.id, response.respondent_id, response.current_task_index - 1
                    )
                    if task_def:
                        layer_payload = task_def.get('layers_shown_in_task') or task_def.get('elements_shown_content')
                except Exception:
                    layer_payload = None
            if layer_payload is not None:
                # Prefer saving to layers_shown_in_task, keep elements_shown_content for backward compatibility
                if completed_task.layers_shown_in_task is None:
                    completed_task.layers_shown_in_task = layer_payload
                if completed_task.elements_shown_content is None:
                    completed_task.elements_shown_content = layer_payload
        # For grid/text studies (or hybrid with grid/text phase), if elements_shown_content is missing, hydrate from generated tasks
        if study_row and effective_study_type in ('grid', 'text'):
            try:
                if completed_task.elements_shown_content is None:
                    from app.services.task_service import TaskService
                    task_service = TaskService(self.db)
                    
                    # Resolve respondent and task index from task_id when possible
                    rid_from_task = None
                    idx_from_task = None
                    try:
                        parts = str(request.task_id).split('_')
                        if len(parts) == 2:
                            rid_from_task = int(parts[0])
                            idx_from_task = int(parts[1])
                    except Exception:
                        rid_from_task = None
                        idx_from_task = None

                    # Determine respondent_id and task_index for lookup
                    lookup_respondent = rid_from_task if rid_from_task is not None else response.respondent_id
                    lookup_index = idx_from_task if isinstance(idx_from_task, int) else max(0, (response.current_task_index or 1) - 1)
                    
                    tdef = task_service.get_task_by_index(study_row.id, lookup_respondent, lookup_index)
                    if tdef:
                        esc = tdef.get('elements_shown_content')
                        if esc:
                            # Store name, url, visible per key; normalize key to CategoryName_ImageName
                            out_map = {}
                            vis_map = completed_task.elements_shown_in_task or {}
                            for k, v in esc.items():
                                if isinstance(v, dict):
                                    # Derive category name from key like "Category 1_4"
                                    cat_name = k.rsplit('_', 1)[0] if isinstance(k, str) and '_' in k else ''
                                    elem_name = v.get('name')
                                    new_key = f"{cat_name}_{elem_name}" if cat_name and elem_name else k
                                    out_map[new_key] = {
                                        "name": elem_name,
                                        "url": v.get('content'),
                                        "visible": 1 if vis_map.get(k) or vis_map.get(new_key) else 0,
                                    }
                                else:
                                    out_map[k] = None
                            completed_task.elements_shown_content = out_map
            except Exception:
                pass
        
        self.db.add(completed_task)
        
        # Update response progress (no extra reads)
        response.completed_tasks_count = (response.completed_tasks_count or 0) + 1
        response.current_task_index = (response.current_task_index or 0) + 1
        try:
            total_assigned = int(response.total_tasks_assigned or 0)
            done = int(response.completed_tasks_count or 0)
            response.completion_percentage = (done / total_assigned * 100.0) if total_assigned > 0 else 0.0
        except Exception:
            response.completion_percentage = 0.0
        response.last_activity = now_utc

        # Bulk insert element interactions if provided
        if request.element_interactions:
            interactions = []
            for interaction_data in request.element_interactions:
                interaction = ElementInteraction(**interaction_data.model_dump())
                interaction.study_response_id = response.id
                interactions.append(interaction)
            if interactions:
                self.db.bulk_save_objects(interactions)
        
        # Mark complete if all tasks are done and no post-task classification remains.
        tasks_complete = response.current_task_index >= (response.total_tasks_assigned or 0)
        response_completed = False
        if tasks_complete and not self._has_unanswered_optional_classification(response):
            self._mark_response_completed(response)
            response_completed = True
            # Check if study should be auto-completed after this response completion
            self._check_and_complete_studies(study_id=response.study_id)
        
        # Single commit at end
        self.db.commit()
        
        return SubmitTaskResponse(
            success=True,
            next_task_index=response.current_task_index if not tasks_complete else None,
            is_study_complete=response_completed,
            completion_percentage=response.completion_percentage
        )
    
    def submit_classification(self, session_id: str, request: SubmitClassificationRequest) -> bool:
        """Submit classification answers with upsert logic to prevent duplicates."""
        response = self.get_response_by_session(session_id)
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Add or update classification answers (upsert to prevent duplicates)
        for answer_data in request.answers:
            # Check if an answer already exists for this question
            existing_answers = self.db.execute(
                select(ClassificationAnswer).where(
                    and_(
                        ClassificationAnswer.study_response_id == response.id,
                        ClassificationAnswer.question_id == answer_data.question_id
                    )
                )
            ).scalars().all()
            
            if existing_answers:
                # Update existing answer instead of creating duplicate
                existing_answer = existing_answers[0]
                existing_answer.answer = answer_data.answer
                existing_answer.answer_timestamp = answer_data.answer_timestamp
                existing_answer.time_spent_seconds = answer_data.time_spent_seconds
                if answer_data.question_text:
                    existing_answer.question_text = answer_data.question_text
                
                # Clean up any duplicates that might have been created previously
                for extra in existing_answers[1:]:
                    self.db.delete(extra)
            else:
                # Create new answer
                answer = ClassificationAnswer(**answer_data.model_dump())
                answer.study_response_id = response.id
                self.db.add(answer)
        
        response.last_activity = datetime.utcnow()
        tasks_complete = (response.completed_tasks_count or 0) >= (response.total_tasks_assigned or 0)
        if (
            request.finalize_response
            and tasks_complete
            and not response.is_completed
            and not self._has_unanswered_optional_classification(response)
        ):
            self._mark_response_completed(response)
            self.db.flush()
            self._check_and_complete_studies(study_id=response.study_id)

        self.db.commit()
        
        return True

    def submit_product_id(self, session_id: str, product_id: str) -> bool:
        """Update product ID for a study session - optimized for speed."""
        from sqlalchemy import update
        stmt = (
            update(StudyResponse)
            .where(StudyResponse.session_id == session_id)
            .values(product_id=product_id, last_activity=datetime.utcnow())
        )
        result = self.db.execute(stmt)
        self.db.commit()
        return result.rowcount > 0

    def submit_panelist_info(self, session_id: str, panelist_id: str) -> Optional[Dict[str, Any]]:
        """
        Submit panelist ID, fetch details, and update study response.
        Returns dict with panelist info if successful, None if session/panelist not found.
        """
        response = self.get_response_by_session(session_id)
        if not response:
            return None
        
        # Look up panelist by ID within the study creator's domain (shared pool per company)
        from app.models.study_model import Study
        from app.models.user_model import User

        study = self.db.query(Study).filter(Study.id == response.study_id).first()
        if not study:
            return None
        creator = self.db.query(User).filter(User.id == study.creator_id).first()
        if not creator or not getattr(creator, "email", None):
            return None
        domain = (creator.email or "").strip()
        if "@" in domain:
            domain = domain.split("@")[-1].lower()
        else:
            domain = ""

        panelist = self.db.query(Panelist).filter(
            Panelist.id == panelist_id,
            Panelist.creator_domain == domain
        ).first()
        
        if not panelist:
            return None
            
        # Update response with panelist ID
        response.panelist_id = panelist_id
        
        # Update personal info with age and gender only (no name)
        current_info = response.personal_info or {}
        current_info['age'] = panelist.age
        current_info['gender'] = panelist.gender
        
        # Use simple assignment to trigger JSONB update in SQLAlchemy
        response.personal_info = dict(current_info)
        response.last_activity = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(response)
        
        return {
            "panelist_id": panelist.id,
            "age": panelist.age,
            "gender": panelist.gender
        }

    def submit_tasks_bulk(self, session_id: str, request: BulkSubmitTasksRequest) -> BulkSubmitTasksResponse:
        """Submit multiple completed tasks in a single transaction (optimized for large payloads)."""
        response = self._get_response_by_session_for_update(session_id)
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")
        if response.is_completed:
            raise HTTPException(status_code=400, detail="Study already completed")
        if response.is_abandoned or str(response.status or "") != "in_progress":
            raise HTTPException(status_code=400, detail="Session is no longer active")

        study_status = self.db.execute(
            select(Study.status).where(Study.id == response.study_id)
        ).scalar_one_or_none()
        if str(study_status or "") == "completed":
            raise HTTPException(status_code=400, detail="Study is no longer accepting submissions")

        now_utc = datetime.utcnow()
        study_row: Optional[Study] = self.db.get(Study, response.study_id)

        # Preload element name->url once for grid/text/hybrid enrichment
        name_to_url: Dict[str, str] = {}
        if study_row and str(study_row.study_type) in ('grid', 'text', 'hybrid'):
            elems = self.db.execute(
                select(StudyElement.name, StudyElement.content).where(StudyElement.study_id == response.study_id)
            ).all()
            name_to_url = {str(n).replace(' ', '_'): c for (n, c) in elems}

        # Preload tasks for hybrid phase_type lookup
        tasks_dict: Dict[str, Any] = {}
        if study_row and str(study_row.study_type) == 'hybrid':
            from app.services.task_service import TaskService
            task_service = TaskService(self.db)
            tasks_dict = task_service.get_all_tasks_as_dict(response.study_id)

        tasks_to_insert: List[CompletedTask] = []
        interactions_to_insert: List[ElementInteraction] = []
        submitted = 0
        response_just_completed = False
        existing_task_keys = set(
            self.db.execute(
                select(CompletedTask.task_id, CompletedTask.task_type).where(
                    CompletedTask.study_response_id == response.id
                )
            ).all()
        )
        seen_in_payload = set()

        for item in request.tasks or []:
            # Build task model
            elements_shown_in_task = item.elements_shown_in_task or {}
            
            # For hybrid studies, determine the effective phase_type from the task definition
            effective_study_type = str(study_row.study_type) if study_row else None
            task_phase_type: Optional[str] = None
            # Use the actual task index (response.current_task_index) for lookup, NOT parsed from task_id
            # because task_id can be duplicated across phases (e.g., "5_0" appears in both grid and text phases)
            actual_task_index = response.current_task_index
            if study_row and str(study_row.study_type) == 'hybrid':
                try:
                    # Use respondent_id to find the right tasks list
                    candidate_keys = [str(response.respondent_id), str(max(0, (response.respondent_id or 0) - 1))]
                    for rk in candidate_keys:
                        tasks_for_r = tasks_dict.get(rk)
                        if isinstance(tasks_for_r, list) and 0 <= actual_task_index < len(tasks_for_r):
                            task_def = tasks_for_r[actual_task_index] or {}
                            task_phase_type = task_def.get('phase_type')
                            if task_phase_type in ('grid', 'text'):
                                effective_study_type = task_phase_type
                            break
                    if task_phase_type not in ('grid', 'text'):
                        task_def_fallback = tasks_dict.get(str(actual_task_index), {}) or {}
                        if isinstance(task_def_fallback, dict):
                            phase_fallback = task_def_fallback.get('phase_type')
                            if phase_fallback in ('grid', 'text'):
                                task_phase_type = phase_fallback
                                effective_study_type = phase_fallback
                except Exception:
                    pass
            
            # For layer studies, enhance elements_shown_in_task with z-index information
            if study_row and str(study_row.study_type) == 'layer' and isinstance(elements_shown_in_task, dict):
                from app.models.study_model import StudyLayer, LayerImage
                
                # Get layer structure with z-index information
                layers = self.db.execute(
                    select(StudyLayer)
                    .where(StudyLayer.study_id == study_row.id)
                    .order_by(StudyLayer.order)
                ).scalars().all()
                
                # Create layer name to z-index mapping
                layer_name_to_z_index: Dict[str, int] = {}
                for layer in layers:
                    layer_name_to_z_index[layer.name] = layer.z_index
                
                # Enhance elements_shown_in_task with z-index information
                enhanced_elements_shown = {}
                for key, value in elements_shown_in_task.items():
                    if isinstance(key, str):
                        # Extract layer name from key (e.g., "background_1" -> "background")
                        import re
                        match = re.search(r"^(.+?)_(\d+)$", key)
                        if match:
                            layer_name = match.group(1)
                            image_num = match.group(2)
                            
                            # Get z-index for this layer
                            z_index = layer_name_to_z_index.get(layer_name, 0)
                            
                            # Create enhanced visibility data with z-index
                            if isinstance(value, dict):
                                # If already a dict, add z_index to it
                                enhanced_value = value.copy()
                                enhanced_value["z_index"] = z_index
                                enhanced_elements_shown[key] = enhanced_value
                            else:
                                # If simple value, create dict with visibility and z_index
                                enhanced_elements_shown[key] = {
                                    "visible": 1 if value else 0,
                                    "z_index": z_index
                                }
                        else:
                            # Fallback for keys that don't match pattern
                            enhanced_elements_shown[key] = value
                    else:
                        enhanced_elements_shown[key] = value
                
                elements_shown_in_task = enhanced_elements_shown
            
            # For hybrid studies, use the phase_type as task_type; otherwise use study_type
            task_type_value = task_phase_type if task_phase_type else (str(study_row.study_type) if study_row else None)
            task_key = (item.task_id, task_type_value)
            dedupe_allowed = task_type_value in ('grid', 'text', 'layer')
            if dedupe_allowed:
                # Idempotency guards (phase-aware):
                # 1) task_id+task_type already persisted for this session
                # 2) duplicate task_id+task_type repeated inside same bulk payload
                if task_key in existing_task_keys or task_key in seen_in_payload:
                    continue
                seen_in_payload.add(task_key)
            
            task_model = CompletedTask(
                task_id=item.task_id,
                respondent_id=response.respondent_id,
                task_index=response.current_task_index,
                elements_shown_in_task=elements_shown_in_task,
                elements_shown_content=item.elements_shown_content or None,
                task_start_time=now_utc - timedelta(seconds=item.task_duration_seconds),
                task_completion_time=now_utc,
                task_duration_seconds=item.task_duration_seconds,
                rating_given=item.rating_given,
                rating_timestamp=now_utc,
                task_type=task_type_value
            )
            task_model.study_response_id = response.id

            # Grid/text enrichment for content map if missing (also applies to hybrid grid/text phases)
            if study_row and effective_study_type in ('grid', 'text') and task_model.elements_shown_in_task and not task_model.elements_shown_content:
                enriched = None
                # Try: resolve from tasks_dict using actual_task_index (NOT parsed from task_id)
                # because task_id can be duplicated across phases (e.g., "5_0" in both grid and text)
                try:
                    # If tasks_dict not preloaded for hybrid, load now
                    if not tasks_dict:
                        from app.services.task_service import TaskService
                        _task_service = TaskService(self.db)
                        tasks_dict_local = _task_service.get_all_tasks_as_dict(response.study_id)
                    else:
                        tasks_dict_local = tasks_dict
                    
                    # Candidate respondent keys
                    candidate_keys = [
                        str(response.respondent_id), str(max(0, (response.respondent_id or 0) - 1))
                    ]
                    tasks_for_r = None
                    for key in candidate_keys:
                        val = tasks_dict_local.get(key)
                        if isinstance(val, list):
                            tasks_for_r = val
                            break
                    if isinstance(tasks_for_r, list) and 0 <= actual_task_index < len(tasks_for_r):
                        tdef = tasks_for_r[actual_task_index]
                        if isinstance(tdef, dict):
                            esc = tdef.get('elements_shown_content')
                            if esc:
                                out_map: Dict[str, Any] = {}
                                vis_map = task_model.elements_shown_in_task or {}
                                for k, v in esc.items():
                                    if isinstance(v, dict):
                                        # Normalize key to CategoryName_ImageName using category from k (CategoryName_Index)
                                        cat_name = k.rsplit('_', 1)[0] if isinstance(k, str) and '_' in k else ''
                                        elem_name = v.get('name')
                                        new_key = f"{cat_name}_{elem_name}" if cat_name and elem_name else k
                                        out_map[new_key] = {
                                            "name": elem_name,
                                            "url": v.get('content'),
                                            "visible": 1 if vis_map.get(k) or vis_map.get(new_key) else 0,
                                        }
                                    else:
                                        out_map[k] = None
                                enriched = out_map
                except Exception:
                    enriched = None
                # Fallback: use preloaded element name->url map, but we don't know element names from index; store minimal
                if not enriched and name_to_url:
                    out_map2: Dict[str, Any] = {}
                    for k, v in (task_model.elements_shown_in_task or {}).items():
                        try:
                            shown = int(v) == 1
                        except Exception:
                            shown = bool(v)
                        if shown:
                            url = name_to_url.get(str(k)) or name_to_url.get(str(k).replace(' ', '_'))
                            out_map2[str(k)] = {
                                "name": None,
                                "url": url,
                                "visible": 1,
                            }
                        else:
                            out_map2[str(k)] = {
                                "name": None,
                                "url": None,
                                "visible": 0,
                            }
                    enriched = out_map2
                if enriched:
                    task_model.elements_shown_content = enriched

            tasks_to_insert.append(task_model)
            if dedupe_allowed:
                existing_task_keys.add(task_key)

            # Queue interactions
            if item.element_interactions:
                for interaction_data in item.element_interactions:
                    interaction = ElementInteraction(**interaction_data.model_dump())
                    interaction.study_response_id = response.id
                    interactions_to_insert.append(interaction)

            # Update progress counters in-memory
            response.completed_tasks_count = (response.completed_tasks_count or 0) + 1
            response.current_task_index = (response.current_task_index or 0) + 1
            submitted += 1

            # Stop early if tasks are completed. Post-task classification can still keep the response open.
            if response.current_task_index >= (response.total_tasks_assigned or 0):
                if not self._has_unanswered_optional_classification(response):
                    self._mark_response_completed(response)
                    response_just_completed = True
                break

        # Bulk insert
        if tasks_to_insert:
            self.db.bulk_save_objects(tasks_to_insert)
        if interactions_to_insert:
            self.db.bulk_save_objects(interactions_to_insert)

        # Finalize response progress
        total_assigned = int(response.total_tasks_assigned or 0)
        done = int(response.completed_tasks_count or 0)
        response.completion_percentage = (done / total_assigned * 100.0) if total_assigned > 0 else 0.0
        response.last_activity = now_utc

        self.db.commit()

        if response_just_completed:
            self._check_and_complete_studies(study_id=response.study_id)

        is_complete = bool(response.is_completed)
        return BulkSubmitTasksResponse(
            success=True,
            submitted_count=submitted,
            next_task_index=None if is_complete else response.current_task_index,
            is_study_complete=is_complete,
            completion_percentage=float(response.completion_percentage or 0.0)
        )

    def submit_synthetic_respondent(self, study_id: UUID, payload: SyntheticRespondentPayload) -> Dict[str, Any]:
        """
        Store a synthetic respondent using the same tables as real data.
        Classification and tasks are stored via the same logic as submit-classification and submit-tasks-bulk.
        Truncates question_id/question_text to fit existing DB column lengths.
        Tasks without a rating are skipped.
        """
        study_row = self.db.get(Study, study_id)
        if not study_row:
            raise HTTPException(
                status_code=404,
                detail=f"Study not found: {study_id}. Use an existing study id from your studies list (e.g. from GET /api/v1/studies or the study detail page)."
            )

        tasks_with_rating = [t for t in (payload.task_ratings or []) if t.rating is not None]
        total_tasks_assigned = len(tasks_with_rating)

        session_id = f"synthetic_{payload.panelist_id}_{uuid4().hex[:12]}"
        now_utc = datetime.utcnow()

        personal_info = None
        if getattr(payload, "personal_info", None) and isinstance(payload.personal_info, dict):
            personal_info = dict(payload.personal_info)

        def _payload_builder(respondent_id: int) -> Dict[str, Any]:
            return {
                "study_id": study_id,
                "session_id": session_id,
                "total_tasks_assigned": total_tasks_assigned,
                "session_start_time": now_utc,
                "personal_info": personal_info,
                "ip_address": None,
                "user_agent": None,
                "last_activity": now_utc,
                "status": "in_progress",
                "is_abandoned": False,
                "is_completed": False,
                "panelist_id": payload.panelist_id,
            }

        new_response = self._create_response_with_respondent_retry(
            study_id=study_id,
            payload_builder=_payload_builder,
            max_retries=3,
        )
        self.db.refresh(new_response)

        # Truncate to fit existing DB columns (no migrations)
        def _truncate(s: str, max_len: int) -> str:
            return (s or "")[:max_len] if s else ""

        classification_answers = [
            ClassificationAnswerCreate(
                question_id=_truncate(v.question_id, 10),
                question_text=_truncate(v.question_text, 500),
                answer=_truncate(v.answer, 1000),
                answer_timestamp=now_utc,
                time_spent_seconds=0.0,
            )
            for v in (payload.classification_answers or {}).values()
        ]
        if classification_answers:
            self.submit_classification(
                session_id,
                SubmitClassificationRequest(answers=classification_answers),
            )

        bulk_tasks: List[BulkSubmitTaskItem] = []
        for tr in tasks_with_rating:
            elements_shown = tr.elements_shown or []
            elements_shown_in_task = {}
            for e in elements_shown:
                key = getattr(e, "key", None) or (e.get("key") if isinstance(e, dict) else None)
                if key:
                    elements_shown_in_task[key] = 1
            elements_shown_content = None
            if elements_shown:
                # Use same keyed-dict shape as human flow so session/frontend show elements (images) correctly
                out_map: Dict[str, Any] = {}
                for e in elements_shown:
                    key = getattr(e, "key", None) or (e.get("key") if isinstance(e, dict) else None)
                    if not key:
                        continue
                    obj = e.model_dump() if hasattr(e, "model_dump") else (e if isinstance(e, dict) else {})
                    url = obj.get("url") or obj.get("content")
                    out_map[key] = {
                        "name": obj.get("name"),
                        "url": url,
                        "visible": 1,
                    }
                elements_shown_content = out_map if out_map else None
            bulk_tasks.append(
                BulkSubmitTaskItem(
                    task_id=tr.task_id,
                    rating_given=tr.rating if tr.rating is not None else 1,
                    task_duration_seconds=0.0,
                    elements_shown_in_task=elements_shown_in_task or None,
                    elements_shown_content=elements_shown_content,
                )
            )

        if bulk_tasks:
            self.submit_tasks_bulk(session_id, BulkSubmitTasksRequest(tasks=bulk_tasks))

        # Align with human flow: update study counters and mark response completed so analysis sees it
        updated_response = self.get_response_by_session(session_id)
        if updated_response and updated_response.is_completed:
            self._update_study_counters(study_id)
        self.invalidate_analytics_cache(study_id)

        return {
            "success": True,
            "session_id": session_id,
            "message": "Synthetic respondent stored (classification and tasks in same tables as real data).",
        }

    # ---------- CSV Export ----------

    def generate_csv_rows_for_study(self, study_id: UUID) -> Iterable[List[Any]]:
        """
        Yield CSV rows for all responses in a study.

        Columns produced (order):
        - Panelist (session_id)
        - QQ1..QQN (classification answers in question order if present)
        - Gender
        - Age
        - Task (task index starting at 1)
        - Dynamic Layer_* columns (Layer_{taskIndex}_{layerIndex}) with 0/1 visibility
        - Rating
        - ResponseTime (seconds)

        Notes:
        - Age is derived from personal_info.dob or personal_info.date_of_birth (YYYY-MM-DD).
        - Gender comes from personal_info.gender if available.
        - Layer visibility is inferred from CompletedTask.elements_shown_content or layers_shown_in_task.
        """
        # Preload minimal study and responses
        study: Optional[Study] = self.db.get(Study, study_id)
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")

        # Fetch responses (single query)
        responses: List[StudyResponse] = list(self.db.execute(
            select(StudyResponse.id, StudyResponse.session_id, StudyResponse.personal_info)
            .where(StudyResponse.study_id == study_id)
        ).all())
        # Early return if no responses
        if not responses:
            yield ["Panelist"]  # minimal header to keep CSV valid
            return
        # Index positions for lightweight tuples
        RESP_ID_IDX = 0
        RESP_SESSION_IDX = 1
        RESP_PI_IDX = 2
        response_ids = [r[RESP_ID_IDX] for r in responses]

        # Build question ordering from study configuration (preferred over discovered answers)
        # Map question_id -> column header text (use question_text) and keep answer option maps for code->text
        question_id_to_col: Dict[str, str] = {}
        question_id_to_options: Dict[str, Dict[str, str]] = {}
        questions = self.db.execute(
            select(StudyClassificationQuestion)
            .where(StudyClassificationQuestion.study_id == study_id)
            .order_by(StudyClassificationQuestion.order)
        ).scalars().all()
        next_q_num = 1
        for q in questions:
            # Use full question text; fallback to QQn
            question_id_to_col[q.question_id] = (q.question_text or f"QQ{next_q_num}")
            next_q_num += 1
            options_map: Dict[str, str] = {}
            if isinstance(q.answer_options, list):
                for opt in q.answer_options:
                    if not isinstance(opt, dict):
                        continue
                    text = opt.get("text") or opt.get("label") or opt.get("name")
                    if text is None:
                        continue
                    # Support multiple keys that might appear in stored answers
                    for key_name in ("id", "value", "code", "label"):
                        if key_name in opt and opt[key_name] is not None:
                            options_map[str(opt[key_name])] = text
            if options_map:
                question_id_to_options[q.question_id] = options_map

        # If there are answers for questions not in config, append them in discovery order (batched)
        if not question_id_to_col and response_ids:
            discovered = self.db.execute(
                select(ClassificationAnswer.question_id)
                .where(ClassificationAnswer.study_response_id.in_(response_ids))
            ).all()
            for (qid,) in discovered:
                if qid not in question_id_to_col:
                    question_id_to_col[qid] = f"QQ{next_q_num}"
                    next_q_num += 1

        # Build consistent ordered headers for all layer slots using layer name + image name
        import re
        def parse_slot(key: str) -> tuple[str, int]:
            # returns (layer_label, image_index)
            m = re.search(r"^(.*?)[ _-]?(\d+)$", key)
            if m:
                return (m.group(1).strip(), int(m.group(2)))
            return (key.strip(), 0)

        # Accumulate first-seen names per slot and per layer
        slot_to_image_name: Dict[tuple[str, int], str] = {}
        layer_to_display: Dict[str, str] = {}
        layer_to_max_image: Dict[str, int] = {}

        # Preload all tasks for the study and group by response (single query)
        tasks_seq = self.db.execute(
            select(CompletedTask)
            .where(CompletedTask.study_response_id.in_(response_ids))
            .order_by(CompletedTask.study_response_id, CompletedTask.task_index)
            ).scalars().all()
        from collections import defaultdict
        tasks_by_response: Dict[UUID, List[CompletedTask]] = defaultdict(list)
        for t in tasks_seq:
            tasks_by_response[t.study_response_id].append(t)

        for resp in responses:
            tasks = tasks_by_response.get(resp[RESP_ID_IDX], [])
            for t in tasks:
                data = t.layers_shown_in_task or t.elements_shown_content or {}
                if not isinstance(data, dict):
                    continue
                for k, v in data.items():
                    if not isinstance(k, str):
                        continue
                    layer_label, image_idx = parse_slot(k)
                    # Prefer explicit layer_name when present
                    if isinstance(v, dict):
                        lbl = (v.get("layer_name") or layer_label or "").strip()
                        if lbl:
                            layer_to_display[layer_label] = lbl
                        img = (v.get("name") or v.get("alt_text") or "").strip()
                        if img and (layer_label, image_idx) not in slot_to_image_name:
                            slot_to_image_name[(layer_label, image_idx)] = img
                    # track max index for each layer
                    current_max = layer_to_max_image.get(layer_label, 0)
                    if image_idx > current_max:
                        layer_to_max_image[layer_label] = image_idx

        # Order layers by numeric index found inside their label; fallback to alpha
        def layer_order_key(label: str) -> tuple[int, str]:
            m = re.search(r"(\d+)", label)
            return (int(m.group(1)) if m else 9999, label)

        ordered_layers = sorted(layer_to_max_image.keys(), key=layer_order_key)

        # Build final layer columns list in order - use actual layer names
        layer_headers: List[str] = []
        layer_slots: List[tuple[str, int]] = []
        
        # Get layer structure from database to create proper ordering
        from app.models.study_model import StudyLayer, LayerImage
        
        # Get layers ordered by their order field
        layers = self.db.execute(
            select(StudyLayer)
            .where(StudyLayer.study_id == study_id)
            .order_by(StudyLayer.order)
        ).scalars().all()
        
        # Create ordered list of layer names based on database order
        ordered_layer_names = [layer.name for layer in layers]
        
        # Process layers in database order
        for layer_name in ordered_layer_names:
            if layer_name in layer_to_max_image:
                max_idx = layer_to_max_image[layer_name]
                for idx in range(1, max_idx + 1):
                    # Use actual layer name with image index
                    header = f"{layer_name}_{idx}"
                    layer_headers.append(header)
                    layer_slots.append((layer_name, idx))

        # Build header (for grid: CategoryName_ImageName columns)
        header: List[str] = ["Panelist"]
        header.extend([question_id_to_col[qid] for qid in question_id_to_col])
        header.extend(["Gender", "Age", "Task"])
        # If study is grid, add CategoryName_ImageName columns; else add layer columns
        study_obj: Optional[Study] = self.db.get(Study, study_id)
        is_grid = bool(study_obj and str(study_obj.study_type) in ('grid', 'text', 'hybrid'))
        grid_columns: List[tuple[str, str, int]] = []  # (cat_name, element_name, one_based_index_within_cat)
        if is_grid:
            # Load categories with elements to build deterministic headers
            from app.models.study_model import StudyCategory, StudyElement
            categories = self.db.execute(
                select(StudyCategory)
                .where(StudyCategory.study_id == study_id)
                .order_by(StudyCategory.order)
            ).scalars().all()
            for cat in categories:
                elements = self.db.execute(
                    select(StudyElement)
                    .where(StudyElement.study_id == study_id, StudyElement.category_id == cat.id)
                    .order_by(StudyElement.element_id)
                ).scalars().all()
                for idx, el in enumerate(elements, start=1):
                    grid_columns.append((cat.name, el.name, idx))
            # Headers as CategoryName_ImageName
            header.extend([f"{c}_{e}" for (c, e, _) in grid_columns])
        else:
            # Layer columns: all slots per layer in numeric order with attached image names
            header.extend(layer_headers)
        header.extend(["Rating", "ResponseTime"])

        # Yield header
        yield header

        # Helper to compute age
        def compute_age(personal_info: Optional[Dict[str, Any]]) -> Optional[int]:
            if not personal_info:
                return None
            dob_str = personal_info.get("dob") or personal_info.get("date_of_birth")
            if not dob_str:
                return personal_info.get("age")
            try:
                from datetime import date
                year, month, day = [int(x) for x in str(dob_str).split("-")[:3]]
                born = date(year, month, day)
                today = date.today()
                return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            except Exception:
                return None

        # Emit rows
        # Preload all answers and group by response (single query)
        answers_seq = self.db.execute(
            select(ClassificationAnswer)
            .where(ClassificationAnswer.study_response_id.in_(response_ids))
        ).scalars().all()
        answers_by_response: Dict[UUID, List[ClassificationAnswer]] = defaultdict(list)
        for a in answers_seq:
            answers_by_response[a.study_response_id].append(a)

        for resp in responses:
            # Build map for classification answers for this response
            answers = answers_by_response.get(resp[RESP_ID_IDX], [])
            answer_map: Dict[Any, Any] = {}
            for a in answers:
                options_map = question_id_to_options.get(a.question_id)
                answer_map[a.question_id] = self._format_classification_answer(a, options_map)

            gender = None
            pi = resp[RESP_PI_IDX]
            if isinstance(pi, dict):
                gender = pi.get("gender") or pi.get("Gender")
            age_val = compute_age(pi if isinstance(pi, dict) else None)

            # Fetch tasks ordered by task_index (preloaded)
            tasks = tasks_by_response.get(resp[RESP_ID_IDX], [])

            for t in tasks:
                row: List[Any] = []
                row.append(resp[RESP_SESSION_IDX])
                # Classification answers in header order
                for qid in question_id_to_col.keys():
                    row.append(answer_map.get(qid))
                row.append(gender)
                row.append(age_val)
                row.append(t.task_index + 1)

                if is_grid:
                    # Grid: presence determined by elements_shown_in_task keys like "CategoryName_Index"
                    grid_data = t.elements_shown_in_task or t.elements_shown or {}
                    presence_map: Dict[str, int] = {}
                    if isinstance(grid_data, dict):
                        for k, v in grid_data.items():
                            if not isinstance(k, str) or k.endswith('_content'):
                                continue
                            try:
                                val = int(v) if v in (0, 1) else (1 if v else 0)
                            except Exception:
                                val = 1 if v else 0
                            presence_map[k.strip()] = val
                            presence_map[k.strip().replace(' ', '_')] = val
                            presence_map[k.strip().lower()] = val
                    content_map = t.elements_shown_content if isinstance(t.elements_shown_content, dict) else {}
                    # Emit per header using the category + index mapping; fallback checks content_map by CategoryName_Index
                    for (cat_name, el_name, one_idx) in grid_columns:
                        candidates = [
                            f"{cat_name}_{one_idx}",
                            f"{cat_name.replace(' ', '_')}_{one_idx}",
                            f"{cat_name.lower()}_{one_idx}",
                        ]
                        present = 0
                        for cand in candidates:
                            if cand in presence_map:
                                present = presence_map[cand]
                                break
                            if cand.lower() in presence_map:
                                present = presence_map[cand.lower()]
                                break
                        if present == 0 and content_map:
                            # Fallback: content_map keys are usually CategoryName_Index
                            for cand in candidates:
                                v = content_map.get(cand)
                                if isinstance(v, dict) and (v.get('url') or v.get('name')):
                                    present = 1
                                    break
                        row.append(present)
                else:
                    # Layer: visibility per ordered slots
                    layer_data = t.layers_shown_in_task or t.elements_shown_content or {}
                    for base_label, idx in layer_slots:
                        value = 0
                        if isinstance(layer_data, dict):
                            # Find matching key regardless of spacing/hyphen style
                            candidates = [
                                f"{base_label} {idx}", f"{base_label}_{idx}", f"{base_label}-{idx}",
                                f"{base_label.strip()} {idx}", f"{base_label.strip()}_{idx}", f"{base_label.strip()}-{idx}"
                            ]
                            for cand in candidates:
                                if cand in layer_data and layer_data[cand]:
                                    value = 1
                                    break
                        row.append(value)

                row.append(float(t.rating_given))
                row.append(float(t.task_duration_seconds or 0.0))

                yield row

    def _extract_num_layers(self, task: CompletedTask) -> int:
        """Infer number of layers from task content."""
        data = task.elements_shown_content or task.layers_shown_in_task or {}
        try:
            # Expecting a dict like {"layers": [ {...}, {...} ]} or just a list
            if isinstance(data, dict) and isinstance(data.get("layers"), list):
                return len(data["layers"]) or 0
            if isinstance(data, list):
                return len(data) or 0
        except Exception:
            pass
        return 0

    def _extract_layer_visibility_map(self, task: CompletedTask) -> Dict[Tuple[int, int], bool]:
        """
        Build a map of (taskIndex, layerIndex) -> visible bool for a single task.
        We mark all layers present in the content for this task as visible (True).
        """
        visibility: Dict[Tuple[int, int], bool] = {}
        data = task.elements_shown_content or task.layers_shown_in_task or {}
        try:
            layers = None
            if isinstance(data, dict) and isinstance(data.get("layers"), list):
                layers = data.get("layers")
            elif isinstance(data, list):
                layers = data
            if isinstance(layers, list):
                for idx, _layer in enumerate(layers, start=1):
                    visibility[(task.task_index, idx)] = True
        except Exception:
            pass
        return visibility

    def _format_classification_answer(self, answer: ClassificationAnswer, options_map: Optional[Dict[str, str]] = None) -> Any:
        """
        Return a user-friendly classification answer for CSV output.
        If the stored answer contains an internal code like "9de47e73 - Option 1",
        we strip the code and keep only the human label after the hyphen.
        """
        try:
            raw = answer.answer
            if isinstance(raw, str):
                # If options map provided, try to map code -> text first
                if options_map:
                    # Accept exact code or left part of "code - label"
                    left_code = raw.split(' - ', 1)[0]
                    right_part = raw.split(' - ', 1)[1].strip() if ' - ' in raw else None
                    if raw in options_map:
                        return options_map[raw]
                    if left_code in options_map:
                        return options_map[left_code]
                    if right_part and right_part in options_map:
                        return options_map[right_part]
                # Common pattern: "<code> - <label>"; take the part after the first ' - '
                parts = raw.split(' - ', 1)
                if len(parts) == 2 and parts[1].strip():
                    return parts[1].strip()
                return raw.strip()
            return raw
        except Exception:
            return getattr(answer, 'answer', None)

    # ---------- Optimized CSV Export (bulk queries, no N+1) ----------
    def generate_csv_rows_for_study_optimized(self, study_id: UUID) -> Iterable[List[Any]]:
        """High-performance CSV flattener for a study.
        Minimizes queries by bulk-loading responses, tasks, and answers.
        """
        from sqlalchemy import select, func

        # Basic study and questions
        study: Optional[Study] = self.db.get(Study, study_id)
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")

        # Load responses for this study
        responses: List[StudyResponse] = self.db.execute(
            select(StudyResponse).where(StudyResponse.study_id == study_id)
        ).scalars().all()
        if not responses:
            # Header only
            yield ["Panelist", "Gender", "Age", "Task", "Rating", "ResponseTime"]
            return

        response_ids = [r.id for r in responses]

        # Bulk load tasks and answers
        all_tasks: List[CompletedTask] = self.db.execute(
            select(CompletedTask)
            .where(CompletedTask.study_response_id.in_(response_ids))
        ).scalars().all()

        all_answers: List[ClassificationAnswer] = self.db.execute(
            select(ClassificationAnswer)
            .where(ClassificationAnswer.study_response_id.in_(response_ids))
        ).scalars().all()

        # Group by response
        tasks_by_response: Dict[UUID, List[CompletedTask]] = {}
        for t in all_tasks:
            tasks_by_response.setdefault(t.study_response_id, []).append(t)
        for lst in tasks_by_response.values():
            lst.sort(key=lambda x: (x.task_index or 0))

        answers_by_response: Dict[UUID, List[ClassificationAnswer]] = {}
        for a in all_answers:
            answers_by_response.setdefault(a.study_response_id, []).append(a)

        # Questions and option maps
        question_id_to_col: Dict[str, str] = {}
        question_id_to_options: Dict[str, Dict[str, str]] = {}
        questions = self.db.execute(
            select(StudyClassificationQuestion)
            .where(StudyClassificationQuestion.study_id == study_id)
            .order_by(StudyClassificationQuestion.order)
        ).scalars().all()
        next_q_num = 1
        for q in questions:
            question_id_to_col[q.question_id] = (q.question_text or f"QQ{next_q_num}")
            next_q_num += 1
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
                question_id_to_options[q.question_id] = options_map

        # Discover layer keys across all tasks (only for layer studies). For grid we add per-element headers elsewhere.
        layer_keys_set: set[str] = set()
        # Map legacy layer-name prefixes (from keys) to observed z-index so we can
        # preserve ordering even if layer names were renamed after tasks were generated.
        legacy_prefix_to_z_index: Dict[str, int] = {}
        # Map (legacy_prefix, one_based_index) -> discovered image name from task content
        discovered_image_names: Dict[Tuple[str, int], str] = {}
        synthetic_layer_len = 0
        if str(study.study_type) == 'layer':
            for t in all_tasks:
                # Consider BOTH visibility map and content map so we do not miss z-index/name info
                vis_map = t.layers_shown_in_task if isinstance(t.layers_shown_in_task, dict) else {}
                content_map = t.elements_shown_content if isinstance(t.elements_shown_content, dict) else {}

                # Union of keys from both maps
                iter_items: List[Tuple[str, Any]] = []
                try:
                    iter_items.extend(list(vis_map.items()))
                except Exception:
                    pass
                try:
                    # Prefer content map later for z-index/name overrides
                    iter_items.extend(list(content_map.items()))
                except Exception:
                    pass

                if iter_items:
                    for k, v in iter_items:
                        if isinstance(k, str):
                            layer_keys_set.add(k)
                            # Capture z-index if present to map legacy prefix -> z-index
                            try:
                                # Keys are like "LayerName_Index"
                                if '_' in k:
                                    prefix = k.rsplit('_', 1)[0]
                                else:
                                    prefix = k
                                if isinstance(v, dict):
                                    z_val = v.get('z_index')
                                    if isinstance(z_val, int):
                                        # First seen wins; they should be consistent for a given prefix
                                        legacy_prefix_to_z_index.setdefault(prefix, z_val)
                                    # Capture discovered image name for this exact key index when available
                                    try:
                                        idx_str = k.rsplit('_', 1)[1] if '_' in k else ''
                                        if idx_str.isdigit():
                                            img_idx = int(idx_str)
                                            nm = v.get('name') or v.get('alt_text')
                                            if isinstance(nm, str) and nm.strip():
                                                discovered_image_names[(prefix, img_idx)] = nm.strip()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                elif isinstance(data, list):
                    try:
                        synthetic_layer_len = max(synthetic_layer_len, len(data))
                    except Exception:
                        pass

        def sort_key(layer_key: str) -> tuple[int, int]:
            import re
            m2 = re.search(r"(\d+)[ _-](\d+)$", layer_key)
            if m2:
                return (int(m2.group(1)), int(m2.group(2)))
            m1 = re.search(r"(\d+)$", layer_key)
            if m1:
                return (int(m1.group(1)), 0)
            return (9999, 9999)

        # Sort layer keys by the layer's z-index (robust to later renames); fallback to DB order
        layer_keys: List[str] = []
        if layer_keys_set:
            # Get layer structure from database to create proper ordering
            from app.models.study_model import StudyLayer, LayerImage

            layers = self.db.execute(
                select(StudyLayer)
                .where(StudyLayer.study_id == study_id)
                .order_by(StudyLayer.order)
            ).scalars().all()

            # Index DB layers by z-index and by name for fallbacks
            z_index_to_layer: Dict[int, StudyLayer] = {}
            name_to_layer: Dict[str, StudyLayer] = {}
            for L in layers:
                z_index_to_layer[getattr(L, 'z_index', 0)] = L
                name_to_layer[L.name] = L

            # Extract unique legacy prefixes from observed keys
            def _prefix(k: str) -> str:
                return k.rsplit('_', 1)[0] if '_' in k else k

            unique_prefixes = sorted({ _prefix(k) for k in layer_keys_set })

            # Compute an ordering key for each prefix: prefer z-index, else the DB order based on name match
            name_to_order: Dict[str, int] = {L.name: i for i, L in enumerate(layers, start=1)}

            def _prefix_sort_key(p: str) -> tuple[int, int, str]:
                # 1) primary: z-index when known
                z = legacy_prefix_to_z_index.get(p)
                if isinstance(z, int):
                    return (0, z, p)
                # 2) secondary: DB order when prefix matches current layer name
                ord_val = name_to_order.get(p)
                if isinstance(ord_val, int):
                    return (1, ord_val, p)
                # 3) fallback: large value + alpha to keep deterministic
                return (9, 9999, p)

            ordered_prefixes = sorted(unique_prefixes, key=_prefix_sort_key)

            # Build ordered keys per prefix, sorted by their one-based image index suffix
            layer_keys = []
            for p in ordered_prefixes:
                matching_keys = [key for key in layer_keys_set if key.startswith(p + "_")]
                matching_keys.sort(key=lambda x: int(x.rsplit("_", 1)[-1]) if x.rsplit("_", 1)[-1].isdigit() else 0)
                layer_keys.extend(matching_keys)
        # If no explicit dict keys found but list-based structure exists, synthesize generic keys Layer_1..N
        if not layer_keys and synthetic_layer_len > 0:
            layer_keys = [f"Layer_{i}" for i in range(1, synthetic_layer_len + 1)]

        # Friendly headers for layer keys - use actual layer names with image names (like category)
        layer_key_to_header: Dict[str, str] = {}
        if layer_keys:
            # Get layer structure from database
            from app.models.study_model import StudyLayer, LayerImage

            layers = self.db.execute(
                select(StudyLayer)
                .where(StudyLayer.study_id == study_id)
                .order_by(StudyLayer.order)
            ).scalars().all()

            # Index layers by z-index and keep their ordered images for name lookup
            z_index_to_layer: Dict[int, StudyLayer] = {}
            layer_id_to_images: Dict[UUID, List[LayerImage]] = {}
            for L in layers:
                z_index_to_layer[getattr(L, 'z_index', 0)] = L
                imgs = self.db.execute(
                    select(LayerImage)
                    .where(LayerImage.layer_id == L.id)
                    .order_by(LayerImage.order)
                ).scalars().all()
                layer_id_to_images[L.id] = imgs

            # Map discovered keys to proper layer names with image names
            import re
            for key in layer_keys:
                match = re.search(r"^(.+?)_(\d+)$", key)
                if not match:
                    layer_key_to_header[key] = key.replace('_', '-')
                    continue

                legacy_prefix = match.group(1)
                image_num = int(match.group(2))

                # Resolve current layer by z-index first (robust to renames)
                target_layer = None
                z_val = legacy_prefix_to_z_index.get(legacy_prefix)
                if isinstance(z_val, int):
                    target_layer = z_index_to_layer.get(z_val)
                # Fallback: try matching by current name
                if target_layer is None:
                    target_layer = next((L for L in layers if L.name == legacy_prefix), None)

                # Prefer discovered image names from task content when available
                discovered_name = discovered_image_names.get((legacy_prefix, image_num))
                if discovered_name:
                    header_name = f"{(target_layer.name if target_layer else legacy_prefix)}-{discovered_name}".replace('_', '-')
                    layer_key_to_header[key] = header_name
                    continue

                if target_layer is None:
                    # Last resort: keep legacy prefix with index
                    layer_key_to_header[key] = f"{legacy_prefix}-{image_num}".replace('_', '-')
                    continue

                # Lookup image name by 1-based index
                imgs = layer_id_to_images.get(target_layer.id) or []
                image_name = imgs[image_num - 1].name if 1 <= image_num <= len(imgs) else None

                if image_name:
                    header_name = f"{target_layer.name}-{image_name}".replace('_', '-')
                else:
                    header_name = f"{target_layer.name}-{image_num}".replace('_', '-')

                layer_key_to_header[key] = header_name

        # Grid headers (CategoryName_ImageName by category order and element_id)
        grid_columns_opt: List[tuple[str, str, int]] = []  # (cat_name, element_name, one_based_index)
        if str(study.study_type) in ('grid', 'text', 'hybrid'):
            from app.models.study_model import StudyCategory, StudyElement
            categories = self.db.execute(
                select(StudyCategory)
                .where(StudyCategory.study_id == study_id)
                .order_by(StudyCategory.order)
            ).scalars().all()
            for cat in categories:
                elements = self.db.execute(
                    select(StudyElement)
                    .where(StudyElement.study_id == study_id, StudyElement.category_id == cat.id)
                    .order_by(StudyElement.element_id)
                ).scalars().all()
                for idx, el in enumerate(elements, start=1):
                    grid_columns_opt.append((cat.name, el.name, idx))

        # Build header
        header: List[str] = ["Panelist"]
        header.extend([question_id_to_col[qid] for qid in question_id_to_col])
        header.extend(["Gender", "Age", "Task"])
        if grid_columns_opt:
            # Sanitize headers minimally for CSV and use hyphens between category and element
            def sanitize_header(s: str) -> str:
                return s.replace(',', '_').replace('\n', ' ').replace('\r', ' ').strip()
            header.extend([sanitize_header(f"{c}-{e}").replace('_', '-').replace(' ', '-') for (c, e, _) in grid_columns_opt])
        elif layer_keys:
            # Replace underscores with hyphens for layer headers (like category but with hyphens)
            header.extend([layer_key_to_header.get(k, k.replace('_', '-').replace(' ', '-')) for k in layer_keys])
        header.extend(["Rating", "ResponseTime"])
        yield header

        # Helper: age
        def compute_age(personal_info: Optional[Dict[str, Any]]) -> Optional[int]:
            if not personal_info:
                return None
            dob_str = personal_info.get("dob") or personal_info.get("date_of_birth")
            if not dob_str:
                return personal_info.get("age")
            try:
                from datetime import date
                year, month, day = [int(x) for x in str(dob_str).split("-")[:3]]
                born = date(year, month, day)
                today = date.today()
                return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            except Exception:
                return None

        # Emit rows
        for resp in responses:
            answer_list = answers_by_response.get(resp.id, [])
            answer_map: Dict[Any, Any] = {}
            for a in answer_list:
                options_map = question_id_to_options.get(a.question_id)
                answer_map[a.question_id] = self._format_classification_answer(a, options_map)

            gender = resp.personal_info.get("gender") if resp.personal_info else None
            age_val = compute_age(resp.personal_info)

            for t in tasks_by_response.get(resp.id, []):
                row: List[Any] = []
                row.append(resp.session_id)
                for qid in question_id_to_col.keys():
                    row.append(answer_map.get(qid))
                row.append(gender)
                row.append(age_val)
                row.append((t.task_index or 0) + 1)

                if grid_columns_opt:
                    # Use elements_shown_in_task CategoryName_Index keys; fallback to elements_shown_content
                    presence_raw = t.elements_shown_in_task or t.elements_shown or {}
                    presence_map: Dict[str, int] = {}
                    if isinstance(presence_raw, dict):
                        for k, v in presence_raw.items():
                            if isinstance(k, str):
                                try:
                                    presence_map[k.strip()] = int(v) if v in (0, 1) else (1 if v else 0)
                                except Exception:
                                    presence_map[k.strip()] = 1 if v else 0
                                # also store normalized spacings
                                presence_map[k.strip().replace(' ', '_')] = presence_map[k.strip()]
                                presence_map[k.strip().lower()] = presence_map[k.strip()]
                    content_map = t.elements_shown_content if isinstance(t.elements_shown_content, dict) else {}
                    for (cat_name, el_name, one_idx) in grid_columns_opt:
                        # primary: CategoryName_Index
                        candidates = [
                            f"{cat_name}_{one_idx}",
                            f"{cat_name.replace(' ', '_')}_{one_idx}",
                            f"{cat_name.lower()}_{one_idx}",
                        ]
                        present = 0
                        for cand in candidates:
                            if cand in presence_map:
                                present = presence_map[cand]
                                break
                            if cand.lower() in presence_map:
                                present = presence_map[cand.lower()]
                                break
                        if present == 0 and content_map:
                            # fallback: CategoryName_ImageName present in content_map
                            header_key = f"{cat_name}_{el_name}"
                            v = content_map.get(header_key) or content_map.get(header_key.replace(' ', '_'))
                            if isinstance(v, dict) and (v.get('url') or v.get('name')):
                                present = 1
                        row.append(present)
                elif layer_keys:
                    layer_data = t.layers_shown_in_task or t.elements_shown_in_task or t.elements_shown_content or {}
                    for key in layer_keys:
                        value = 0
                        if isinstance(layer_data, dict):
                            v = layer_data.get(key)
                            if isinstance(v, dict):
                                vis = v.get("visible")
                                if vis is None:
                                    vis = True if len(v.keys()) > 0 else False
                                value = 1 if bool(vis) else 0
                            else:
                                try:
                                    value = 1 if int(v) != 0 else 0
                                except Exception:
                                    value = 1 if bool(v) else 0
                        elif isinstance(layer_data, list) and key.startswith("Layer_"):
                            try:
                                idx = int(key.split("_", 1)[1]) - 1
                                if 0 <= idx < len(layer_data):
                                    item = layer_data[idx]
                                    if isinstance(item, dict):
                                        vis = item.get("visible")
                                        if vis is None:
                                            vis = True if len(item.keys()) > 0 else False
                                        value = 1 if bool(vis) else 0
                                    else:
                                        value = 1 if bool(item) else 0
                            except Exception:
                                value = 0
                        row.append(value)

                row.append(float(t.rating_given))
                row.append(float(t.task_duration_seconds or 0.0))

                yield row
    
    def abandon_study(self, session_id: str, request: AbandonStudyRequest) -> bool:
        """Mark a study response as abandoned."""
        response = self.get_response_by_session(session_id)
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if response.is_completed:
            raise HTTPException(status_code=400, detail="Study already completed")
        
        response.is_abandoned = True
        response.is_completed = False
        response.status = 'abandoned'  # Set status as abandoned
        response.abandonment_timestamp = datetime.utcnow()
        response.abandonment_reason = request.reason
        response.last_activity = datetime.utcnow()
        
        self.db.commit()
        
        # Update study counters
        self._update_study_counters(response.study_id)
        
        # Check if study should be auto-completed
        self._check_and_complete_studies(study_id=response.study_id)
        
        # Invalidate analytics cache since data changed
        self.invalidate_analytics_cache(response.study_id)
        
        return True
    
    def check_and_mark_abandoned_sessions(self) -> int:
        """Check for sessions that have been inactive for 60+ minutes and mark them as abandoned."""
        from datetime import timedelta
        
        # Find sessions that are in_progress but haven't been active for 60+ minutes
        cutoff_time = datetime.utcnow() - timedelta(minutes=60)
        
        abandoned_sessions = self.db.execute(
            select(StudyResponse)
            .where(
                and_(
                    StudyResponse.status == 'in_progress',
                    StudyResponse.last_activity < cutoff_time,
                    StudyResponse.is_completed == False
                )
            )
        ).scalars().all()
        
        count = 0
        for session in abandoned_sessions:
            session.is_abandoned = True
            session.is_completed = False
            session.status = 'abandoned'
            session.abandonment_timestamp = datetime.utcnow()
            session.abandonment_reason = 'Inactive for 60+ minutes'
            count += 1
        
        if count > 0:
            self.db.commit()
            # Update study counters for affected studies
            study_ids = set(session.study_id for session in abandoned_sessions)
            for study_id in study_ids:
                self._update_study_counters(study_id)
        
        # Check for study completion after marking abandoned sessions
        self._check_and_complete_studies()
        
        return count
    
    def _check_and_complete_studies(self, study_id: Optional[UUID] = None):
        """Check if studies should be automatically marked as completed.

        A study is auto-completed when either:
        - completed_responses reaches the configured respondent target, or
        - total_responses reaches the generated respondent pool capacity
          (meaning no more respondent task slots remain).

        Also marks any in_progress responses as abandoned when study is completed.

        If study_id is provided, only checks that specific study (optimized path).
        """
        import logging
        from app.models.study_model import Study
        from app.services.study import change_status_fast
        
        logger = logging.getLogger(__name__)
        
        # Determine which studies to check
        if study_id:
            # Single study check - much faster
            studies_to_check = self.db.execute(
                select(Study)
                .where(Study.id == study_id)
                .where(Study.status.in_(['active', 'paused', 'draft']))
            ).scalars().all()
        else:
            # Check all eligible studies
            studies_to_check = self.db.execute(
                select(Study)
                .where(Study.status.in_(['active', 'paused', 'draft']))
            ).scalars().all()
        
        for study in studies_to_check:
            # Get current response counts for this study in a single optimized query
            counts = self.db.execute(
                text("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE is_completed = true) as completed,
                        COUNT(*) FILTER (WHERE is_abandoned = true) as abandoned
                    FROM study_responses 
                    WHERE study_id = :study_id
                """),
                {"study_id": study.id}
            ).first()
            
            total_responses = counts.total or 0
            completed_responses = counts.completed or 0
            abandoned_responses = counts.abandoned or 0
            
            try:
                expected_respondents = int(
                    (study.audience_segmentation or {}).get('number_of_respondents') or 0
                )
            except Exception:
                expected_respondents = 0
            generated_pool = self._get_generated_respondent_capacity_for_study(
                study.id,
                getattr(study, 'tasks', None),
            )
            
            # Debug logging
            logger.info(f"Study {study.id} completion check:")
            logger.info(f"  - Expected respondents: {expected_respondents}")
            logger.info(f"  - Generated respondent pool: {generated_pool}")
            logger.info(f"  - Total responses: {total_responses}")
            logger.info(f"  - Completed: {completed_responses}")
            logger.info(f"  - Abandoned: {abandoned_responses}")
            logger.info(f"  - Current status: {study.status}")
            
            # Check if study should be completed
            should_complete = False
            completion_reason = ""
            
            if expected_respondents > 0:
                # Primary rule: complete once the target number of respondents is fully completed.
                if completed_responses >= expected_respondents:
                    should_complete = True
                    completion_reason = (
                        f"completed responses ({completed_responses}) reached expected respondents "
                        f"({expected_respondents})"
                    )
                # Edge case: if all generated respondent slots are already consumed, no more work remains.
                elif generated_pool > 0 and total_responses >= generated_pool:
                    should_complete = True
                    completion_reason = (
                        f"total responses ({total_responses}) reached generated respondent pool "
                        f"({generated_pool})"
                    )
                else:
                    logger.info(
                        f"Study {study.id} needs {max(0, expected_respondents - completed_responses)} "
                        "more completed respondents to reach target"
                    )
            else:
                # No explicit target: fall back to generated pool if available.
                if generated_pool > 0 and total_responses >= generated_pool:
                    should_complete = True
                    completion_reason = (
                        f"total responses ({total_responses}) reached generated respondent pool "
                        f"({generated_pool})"
                    )
                elif total_responses > 0 and (completed_responses + abandoned_responses) >= total_responses:
                    should_complete = True
                    completion_reason = f"all {total_responses} current respondents completed/abandoned"
                else:
                    logger.info(
                        f"Study {study.id} has {total_responses} total responses, "
                        f"{completed_responses} completed, {abandoned_responses} abandoned"
                    )
            
            if should_complete:
                logger.info(f"Study {study.id} auto-completion triggered: {completion_reason}")
                
                # First, mark any in_progress responses as abandoned when study is being completed
                in_progress_responses = self.db.execute(
                    select(StudyResponse)
                    .where(
                        and_(
                            StudyResponse.study_id == study.id,
                            StudyResponse.status == 'in_progress',
                            StudyResponse.is_completed == False
                        )
                    )
                ).scalars().all()
                
                if in_progress_responses:
                    logger.info(f"Marking {len(in_progress_responses)} in_progress responses as abandoned for study {study.id}")
                    for response in in_progress_responses:
                        response.is_abandoned = True
                        response.is_completed = False
                        response.status = 'abandoned'
                        response.abandonment_timestamp = datetime.utcnow()
                        response.abandonment_reason = 'Study completed - response was in progress'
                        response.last_activity = datetime.utcnow()
                    self.db.commit()
                
                # Mark study as completed (regardless of current status)
                try:
                    change_status_fast(self.db, study.id, study.creator_id, 'completed')
                    logger.info(f"Study {study.id} automatically marked as completed (was {study.status})")
                except Exception as e:
                    logger.error(f"Failed to auto-complete study {study.id}: {e}")
    
    def update_user_details(self, session_id: str, user_details: Dict[str, Any]) -> bool:
        """Update user details for a study session."""
        response = self.get_response_by_session(session_id)
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if response.is_completed:
            raise HTTPException(status_code=400, detail="Cannot update details for completed study")
        
        # Merge with existing personal_info
        existing_info = response.personal_info or {}
        existing_info.update(user_details)
        
        response.personal_info = existing_info
        response.last_activity = datetime.utcnow()
        
        self.db.commit()
        return True
    
    # ---------- Analytics and Reporting ----------
    
    def get_study_analytics(self, study_id: UUID) -> StudyAnalytics:
        """Get analytics data for a study - ultra-optimized with raw SQL for maximum speed."""
        # Caching disabled: always compute fresh analytics
        now = time.time()
        
        # Ultra-fast raw SQL query for all statistics
        from sqlalchemy import text
        
        stats_query = text("""
            SELECT 
                COUNT(*) as total_responses,
                COUNT(*) FILTER (WHERE is_completed = true) as completed_responses,
                COUNT(*) FILTER (WHERE is_abandoned = true) as abandoned_responses,
                COUNT(*) FILTER (WHERE status = 'in_progress') as in_progress_responses,
                AVG(total_study_duration) FILTER (WHERE is_completed = true) as avg_duration
            FROM study_responses 
            WHERE study_id = :study_id
        """)
        
        result = self.db.execute(stats_query, {"study_id": study_id}).first()
        
        total_responses = result.total_responses or 0
        completed_responses = result.completed_responses or 0
        abandoned_responses = result.abandoned_responses or 0
        in_progress_responses = result.in_progress_responses or 0
        avg_duration = float(result.avg_duration or 0)
        
        # Calculate rates
        completion_rate = (completed_responses / total_responses * 100) if total_responses > 0 else 0
        abandonment_rate = (abandoned_responses / total_responses * 100) if total_responses > 0 else 0
        
        # Ultra-fast analytics - return minimal data if no responses
        if total_responses == 0:
            return StudyAnalytics(
                total_responses=0,
                completed_responses=0,
                abandoned_responses=0,
                in_progress_responses=0,
                completion_rate=0,
                average_duration=0,
                abandonment_rate=0,
                element_heatmap={},
                timing_distributions={"task_durations": [], "average_duration": 0, "min_duration": 0, "max_duration": 0, "total_tasks": 0}
            )
        
        # Get minimal element heatmap (raw SQL)
        element_heatmap = self._get_element_heatmap_ultra_fast(study_id)
        
        # Get minimal timing distributions (raw SQL)
        timing_distributions = self._get_timing_distributions_ultra_fast(study_id)
        
        analytics = StudyAnalytics(
            total_responses=total_responses,
            completed_responses=completed_responses,
            abandoned_responses=abandoned_responses,
            in_progress_responses=in_progress_responses,
            completion_rate=completion_rate,
            average_duration=avg_duration,
            abandonment_rate=abandonment_rate,
            element_heatmap=element_heatmap,
            timing_distributions=timing_distributions
        )
        return analytics
    
    def _get_cached_analytics(self, study_id: UUID) -> Optional[StudyAnalytics]:
        """Caching disabled; always recompute."""
        return None
    
    def invalidate_analytics_cache(self, study_id: UUID) -> None:
        """Caching disabled; nothing to invalidate."""
        return None
    
    def get_response_analytics(self, response_id: UUID) -> Optional[ResponseAnalytics]:
        """Get detailed analytics for a specific response."""
        response = self.get_response(response_id)
        if not response:
            return None
        
        # Get completed tasks with interactions
        completed_tasks = self.db.execute(
            select(CompletedTask)
            .where(CompletedTask.study_response_id == response_id)
            .order_by(CompletedTask.task_index)
        ).scalars().all()
        
        # Get element interactions
        element_interactions = self.db.execute(
            select(ElementInteraction)
            .where(ElementInteraction.study_response_id == response_id)
        ).scalars().all()
        
        # Build task analytics
        task_analytics = []
        for task in completed_tasks:
            task_analytics.append({
                "task_id": task.task_id,
                "task_index": task.task_index,
                "rating_given": task.rating_given,
                "duration_seconds": task.task_duration_seconds,
                "completion_time": task.task_completion_time.isoformat()
            })
        
        # Abandonment analysis
        abandonment_analysis = None
        if response.is_abandoned:
            abandonment_analysis = {
                "timestamp": response.abandonment_timestamp.isoformat() if response.abandonment_timestamp else None,
                "reason": response.abandonment_reason,
                "completion_percentage": response.completion_percentage,
                "tasks_completed": response.completed_tasks_count
            }
        
        return ResponseAnalytics(
            response_id=response.id,
            session_id=response.session_id,
            completion_percentage=response.completion_percentage,
            total_duration=response.total_study_duration,
            task_analytics=task_analytics,
            element_interactions=[ElementInteractionOut.model_validate(interaction) for interaction in element_interactions],
            abandonment_analysis=abandonment_analysis
        )
    
    # ---------- Helper Methods ----------
    
    def _get_next_respondent_id(self, study_id: UUID) -> int:
        """Get the next available respondent ID for a study, starting from 1.
        Optimized to use MAX(respondent_id) instead of COUNT(*) for speed.
        """
        # Get the highest respondent ID current assigned for this study
        stmt = select(func.max(StudyResponse.respondent_id)).where(StudyResponse.study_id == study_id)
        max_id = self.db.execute(stmt).scalar()
        
        return (max_id or 0) + 1

    def _is_respondent_unique_conflict(self, exc: IntegrityError) -> bool:
        """Return True if IntegrityError was caused by duplicate (study_id, respondent_id)."""
        msg = str(getattr(exc, "orig", exc)).lower()
        return (
            "uq_study_responses_study_respondent" in msg
            or ("study_id" in msg and "respondent_id" in msg and ("unique" in msg or "duplicate" in msg))
        )

    def _create_response_with_respondent_retry(
        self,
        *,
        study_id: UUID,
        payload_builder: Callable[[int], Dict[str, Any]],
        max_retries: int = 3,
    ) -> StudyResponse:
        """
        Create StudyResponse with optimistic respondent_id allocation.
        Retries quickly on unique conflicts for (study_id, respondent_id).
        """
        for attempt in range(max_retries + 1):
            respondent_id = self._get_next_respondent_id(study_id)
            payload = payload_builder(respondent_id) or {}
            response = StudyResponse(respondent_id=respondent_id, **payload)
            self.db.add(response)
            try:
                self.db.commit()
                return response
            except IntegrityError as exc:
                self.db.rollback()
                if not self._is_respondent_unique_conflict(exc):
                    raise
                if attempt < max_retries:
                    # Tiny jitter keeps concurrent retries from colliding again.
                    time.sleep((0.001 * (2 ** attempt)) + random.uniform(0.0, 0.002))
                    continue
                raise HTTPException(
                    status_code=503,
                    detail="High traffic while assigning respondent id. Please retry.",
                )

    def _get_generated_respondent_capacity(self, tasks: Any) -> int:
        """Count how many respondent slots were generated in the task matrix."""
        if not isinstance(tasks, dict):
            return 0

        try:
            return sum(1 for key in tasks if str(key).isdigit())
        except Exception:
            return 0

    def _get_generated_respondent_capacity_for_study(self, study_id: UUID, legacy_tasks: Any = None) -> int:
        """Count generated respondent slots from normalized rows, falling back to legacy JSON."""
        try:
            from app.services.task_service import TaskService
            task_service = TaskService(self.db)
            count = task_service.get_respondent_count(study_id)
            if count > 0:
                return count
        except Exception:
            pass

        return self._get_generated_respondent_capacity(legacy_tasks)
    
    def _calculate_respondent_task_count(self, tasks: Any, respondent_id: int) -> int:
        """Calculate the number of tasks assigned to a specific respondent."""
        if not tasks:
            return 0
        
        if isinstance(tasks, list):
            # Flat list of tasks - all respondents get all tasks
            return len(tasks)
        elif isinstance(tasks, dict):
            # Per-respondent tasks or index-keyed tasks
            respondent_key = str(respondent_id)
            if respondent_key in tasks:
                respondent_tasks = tasks[respondent_key]
                if isinstance(respondent_tasks, list):
                    return len(respondent_tasks)
                else:
                    return 1 if respondent_tasks else 0
            else:
                # Fallback: count all numeric keys (0, 1, 2, ...), skip design_matrix
                numeric_count = 0
                for key, value in tasks.items():
                    if str(key).isdigit() and value:
                        numeric_count += 1
                return numeric_count
        
        return 0
    
    def _mark_response_completed(self, response: StudyResponse) -> None:
        """Mark a response as completed, handling timezone-aware timestamps."""
        from datetime import timezone
        response.is_completed = True
        response.is_abandoned = False
        response.status = 'completed'  # Set status as completed
        end_time = datetime.now(timezone.utc)
        response.session_end_time = end_time
        response.completion_percentage = 100.0
        response.last_activity = end_time  # Update last activity
        
        if response.session_start_time:
            start_time = response.session_start_time
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            response.total_study_duration = (end_time - start_time).total_seconds()
    
    def _update_study_counters(self, study_id: UUID) -> None:
        """Update study response counters."""
        study = self.db.get(Study, study_id)
        if not study:
            return
        
        # Count responses
        total_responses = self.db.execute(
            select(func.count(StudyResponse.id))
            .where(StudyResponse.study_id == study_id)
        ).scalar()
        
        completed_responses = self.db.execute(
            select(func.count(StudyResponse.id))
            .where(and_(
                StudyResponse.study_id == study_id,
                StudyResponse.is_completed == True
            ))
        ).scalar()
        
        abandoned_responses = self.db.execute(
            select(func.count(StudyResponse.id))
            .where(and_(
                StudyResponse.study_id == study_id,
                StudyResponse.is_abandoned == True
            ))
        ).scalar()
        
        # Update study
        study.total_responses = total_responses
        study.completed_responses = completed_responses
        study.abandoned_responses = abandoned_responses
        
        self.db.commit()

        # Response data changed → drop the assistant's cached analysis so it
        # never serves stale numbers. Best-effort; never block the write path.
        try:
            from app.core.cache import invalidate_assistant_analysis_cache

            invalidate_assistant_analysis_cache(study_id)
        except Exception:
            pass
    
    def _get_element_heatmap_ultra_fast(self, study_id: UUID) -> Dict[str, Any]:
        """Get element interaction heatmap data - ultra-fast with raw SQL and minimal data."""
        from sqlalchemy import text
        
        # Ultra-fast raw SQL with minimal data
        heatmap_query = text("""
            SELECT 
                ei.element_id,
                SUM(ei.view_time_seconds) as total_view_time,
                SUM(ei.hover_count) as total_hovers,
                SUM(ei.click_count) as total_clicks,
                COUNT(ei.id) as interaction_count
            FROM element_interactions ei
            JOIN study_responses sr ON ei.study_response_id = sr.id
            WHERE sr.study_id = :study_id
            GROUP BY ei.element_id
            LIMIT 20
        """)
        
        results = self.db.execute(heatmap_query, {"study_id": study_id}).fetchall()
        
        heatmap = {}
        for result in results:
            heatmap[result.element_id] = {
                "total_view_time": float(result.total_view_time or 0),
                "total_hovers": int(result.total_hovers or 0),
                "total_clicks": int(result.total_clicks or 0),
                "interaction_count": int(result.interaction_count or 0)
            }
        
        return heatmap
    
    def _get_timing_distributions_ultra_fast(self, study_id: UUID) -> Dict[str, Any]:
        """Get timing distribution data - ultra-fast with raw SQL and minimal sampling."""
        from sqlalchemy import text
        
        # Ultra-fast raw SQL for timing statistics
        timing_query = text("""
            SELECT 
                AVG(ct.task_duration_seconds) as avg_duration,
                MIN(ct.task_duration_seconds) as min_duration,
                MAX(ct.task_duration_seconds) as max_duration,
                COUNT(ct.id) as total_tasks
            FROM completed_tasks ct
            JOIN study_responses sr ON ct.study_response_id = sr.id
            WHERE sr.study_id = :study_id
        """)
        
        result = self.db.execute(timing_query, {"study_id": study_id}).first()
        
        if not result or result.total_tasks == 0:
            return {"task_durations": [], "average_duration": 0, "min_duration": 0, "max_duration": 0, "total_tasks": 0}
        
        # Get minimal sample for distribution (ultra-small limit for speed)
        sample_query = text("""
            SELECT ct.task_duration_seconds
            FROM completed_tasks ct
            JOIN study_responses sr ON ct.study_response_id = sr.id
            WHERE sr.study_id = :study_id
            ORDER BY ct.task_completion_time DESC
            LIMIT 20
        """)
        
        sample_durations = [row[0] for row in self.db.execute(sample_query, {"study_id": study_id}).fetchall()]
        
        return {
            "task_durations": sample_durations,
            "average_duration": float(result.avg_duration or 0),
            "min_duration": float(result.min_duration or 0),
            "max_duration": float(result.max_duration or 0),
            "total_tasks": int(result.total_tasks or 0)
        }

    def get_study_dataframe(
        self,
        study_id: UUID,
        unilever_format: bool = False,
        completed_only: bool = False,
    ) -> pd.DataFrame:
        from sqlalchemy.orm import noload, load_only

        """
        High-performance CSV flattener using Pandas.
        Replicates the exact logic of generate_csv_rows_for_study_optimized but uses vectorized operations.
        When unilever_format=True (caller is unilever.com), adds Panelist (panelist_id|session_id),
        Product ID, product key columns, and Task type. No extra DB round-trips.
        """
        # 1. Fetch Data (Bulk)
        study = self.db.get(Study, study_id)
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        from app.core.merged_studies import is_merged_study
        include_done_by_id = is_merged_study(study_id)

        response_cols = [
            StudyResponse.id,
            StudyResponse.session_id,
            StudyResponse.personal_info,
        ]
        if include_done_by_id:
            response_cols.append(StudyResponse.done_by_id)
        if unilever_format:
            response_cols.append(StudyResponse.panelist_id)
            response_cols.append(StudyResponse.session_start_time)  # For panelist deduplication (keep first completed)
        if str(study.study_type) in ('grid', 'text', 'hybrid'):
            response_cols.append(StudyResponse.respondent_id)
        responses_query = select(*response_cols).where(StudyResponse.study_id == study_id)
        if completed_only:
            responses_query = responses_query.where(StudyResponse.is_completed.is_(True))
        responses_df = pd.read_sql(responses_query, self.db.bind)
        
        # Deduplicate by panelist_id: keep only the first completed response per panelist
        # This only applies for unilever_format studies with completed_only=True
        if unilever_format and completed_only and not responses_df.empty and 'panelist_id' in responses_df.columns:
            # Separate rows with and without panelist_id
            has_panelist = responses_df['panelist_id'].notna() & (responses_df['panelist_id'] != '')
            with_panelist = responses_df[has_panelist].copy()
            without_panelist = responses_df[~has_panelist].copy()
            
            if not with_panelist.empty:
                # Sort by session_start_time and keep first (earliest) per panelist_id
                with_panelist = with_panelist.sort_values('session_start_time')
                with_panelist = with_panelist.drop_duplicates(subset=['panelist_id'], keep='first')
            
            # Combine back
            responses_df = pd.concat([with_panelist, without_panelist], ignore_index=True)
            
            # Drop session_start_time - it was only needed for sorting, not for output
            if 'session_start_time' in responses_df.columns:
                responses_df = responses_df.drop(columns=['session_start_time'])

        if responses_df.empty:
            if unilever_format:
                key_names = [k.get("name") for k in (study.product_keys or []) if k and k.get("name")]
                done_by_cols = ["Done By ID"] if include_done_by_id else []
                cols = ["Panelist"] + done_by_cols + ["Product ID", "Age", "Gender"] + key_names + ["Task", "Task type", "Rating", "ResponseTime"]
                return pd.DataFrame(columns=cols)
            done_by_cols = ["Done By ID"] if include_done_by_id else []
            return pd.DataFrame(columns=["Panelist"] + done_by_cols + ["Gender", "Age", "Task", "Rating", "ResponseTime"])

        response_ids = responses_df['id'].tolist()

        task_cols = [
            CompletedTask.study_response_id,
            CompletedTask.task_index,
            CompletedTask.rating_given,
            CompletedTask.task_duration_seconds,
            CompletedTask.elements_shown_in_task,
            CompletedTask.elements_shown_content,
            CompletedTask.layers_shown_in_task,
        ]
        if unilever_format or str(study.study_type) in ('hybrid', 'grid', 'text'):
            task_cols.append(CompletedTask.task_type)
        tasks_query = select(*task_cols).where(CompletedTask.study_response_id.in_(response_ids))
        tasks_df = pd.read_sql(tasks_query, self.db.bind)

        # Fetch answers
        answers_query = select(
            ClassificationAnswer.study_response_id,
            ClassificationAnswer.question_id,
            ClassificationAnswer.answer
        ).where(ClassificationAnswer.study_response_id.in_(response_ids))
        answers_df = pd.read_sql(answers_query, self.db.bind)

        # 2. Process Questions & Answers
        # Get questions config - use load_only to avoid loading unnecessary columns
        from sqlalchemy.orm import noload, load_only
        questions = self.db.execute(
            select(StudyClassificationQuestion)
            .where(StudyClassificationQuestion.study_id == study_id)
            .order_by(StudyClassificationQuestion.order)
            .options(
                load_only(
                    StudyClassificationQuestion.question_id,
                    StudyClassificationQuestion.question_text,
                    StudyClassificationQuestion.answer_options,
                    StudyClassificationQuestion.order
                ),
                noload(StudyClassificationQuestion.study)  # Don't load the Study relationship
            )
        ).scalars().all()
        
        question_id_to_col = {}

        question_id_to_col = {}
        question_id_to_options = {}
        question_id_yes_no_cols = set()  # column names for questions with Yes/No options → export as 0/1
        next_q_num = 1
        _yes_variants = frozenset(('yes', 'y'))
        _no_variants = frozenset(('no', 'n'))
        for q in questions:
            col_name = q.question_text or f"QQ{next_q_num}"
            question_id_to_col[q.question_id] = col_name
            next_q_num += 1
            
            options_map = {}
            if isinstance(q.answer_options, list):
                for opt in q.answer_options:
                    if isinstance(opt, dict):
                        text = opt.get("text") or opt.get("label") or opt.get("name")
                        if text:
                            for key in ("id", "value", "code", "label"):
                                if opt.get(key) is not None:
                                    options_map[str(opt[key])] = text
            if options_map:
                question_id_to_options[q.question_id] = options_map
            # Detect Yes/No (or Y/N) questions for 0/1 mapping in raw export
            if isinstance(q.answer_options, list) and len(q.answer_options) == 2:
                texts = []
                for opt in q.answer_options:
                    if isinstance(opt, dict):
                        t = (opt.get("text") or opt.get("label") or opt.get("name") or "").strip().lower()
                        if t:
                            texts.append(t)
                if len(texts) == 2:
                    s0, s1 = texts[0], texts[1]
                    if (s0 in _yes_variants and s1 in _no_variants) or (s0 in _no_variants and s1 in _yes_variants):
                        question_id_yes_no_cols.add(col_name)

        # Pivot answers: one row per response, columns are question_ids
        if not answers_df.empty:
            # Format answers first
            def format_answer(row):
                qid = row['question_id']
                raw = row['answer']
                options_map = question_id_to_options.get(qid)
                if isinstance(raw, str):
                    if options_map:
                        left = raw.split(' - ', 1)[0]
                        right = raw.split(' - ', 1)[1].strip() if ' - ' in raw else None
                        if raw in options_map: return options_map[raw]
                        if left in options_map: return options_map[left]
                        if right and right in options_map: return options_map[right]
                    parts = raw.split(' - ', 1)
                    if len(parts) == 2 and parts[1].strip():
                        return parts[1].strip()
                    return raw.strip()
                return raw

            answers_df['formatted_answer'] = answers_df.apply(format_answer, axis=1)
            # Use pivot_table instead of pivot to handle duplicate entries
            # (same question answered multiple times by same respondent)
            answers_pivot = answers_df.pivot_table(
                index='study_response_id', 
                columns='question_id', 
                values='formatted_answer',
                aggfunc='first'  # Take the first answer if duplicates exist
            )
        else:
            answers_pivot = pd.DataFrame(index=response_ids)

        # Ensure all configured questions exist as columns
        for qid in question_id_to_col.keys():
            if qid not in answers_pivot.columns:
                answers_pivot[qid] = None

        # Rename columns to friendly names
        answers_pivot = answers_pivot.rename(columns=question_id_to_col)
        
        # 3. Process Demographics (Gender, Age)
        def extract_gender(pi):
            if isinstance(pi, dict):
                return pi.get("gender") or pi.get("Gender")
            return None

        def extract_age(pi):
            if not isinstance(pi, dict): return None
            dob = pi.get("dob") or pi.get("date_of_birth")
            if not dob: return pi.get("age")
            try:
                # Simple age calc
                return int((datetime.now() - pd.to_datetime(dob)).days / 365.25)
            except:
                return None

        responses_df['Gender'] = responses_df['personal_info'].apply(extract_gender)
        responses_df['Age'] = responses_df['personal_info'].apply(extract_age)

        # 4. Process Tasks & Layers/Grid
        # Expand tasks
        if tasks_df.empty:
             # Should return at least headers if no tasks? 
             # The old code loops responses. If a response has no tasks, it doesn't yield rows for it? 
             # Actually old code: "for t in tasks: ... yield row". So no tasks = no rows for that response.
             pass
        
        # Join tasks with response info
        # tasks_df has study_response_id.
        # Merge responses_df (id, session_id, Gender, Age) onto tasks_df
        full_df = tasks_df.merge(responses_df, left_on='study_response_id', right_on='id', how='left')
        
        # Sort by response and task index to ensure correct order
        full_df = full_df.sort_values(by=['study_response_id', 'task_index'], ascending=[True, True])
        
        # Add 1-based task index
        full_df['Task'] = full_df['task_index'].fillna(0).astype(int) + 1
        
        # Determine dynamic columns
        is_grid = str(study.study_type) in ('grid', 'text', 'hybrid')
        dynamic_cols = []
                
        if is_grid:
            from app.models.study_model import StudyCategory, StudyElement
            from sqlalchemy.orm import noload, load_only
            from collections import defaultdict

            # Fetch categories
            cat_load = [StudyCategory.id, StudyCategory.name, StudyCategory.order]
            cats = self.db.execute(
                select(StudyCategory)
                .where(StudyCategory.study_id == study_id)
                .order_by(StudyCategory.order)
                .options(
                    load_only(*cat_load),
                    noload(StudyCategory.study),
                    noload(StudyCategory.elements)
                )
            ).scalars().all()

            # Fetch all elements (order doesn't matter for name-based matching)
            all_elements = self.db.execute(
                select(StudyElement)
                .where(StudyElement.study_id == study_id)
                .options(
                    load_only(StudyElement.category_id, StudyElement.name, StudyElement.element_id),
                    noload(StudyElement.study),
                    noload(StudyElement.category)
                )
            ).scalars().all()

            # Group elements by category
            elems_by_cat = defaultdict(list)
            for el in all_elements:
                elems_by_cat[el.category_id].append(el)

            # Build column headers: one per (category, element) for all categories
            # col_header -> (cat_name, el_name) mapping for name-based lookup
            col_headers = []
            col_to_cat_el = {}
            for cat in cats:
                elems = elems_by_cat.get(cat.id, [])
                for el in elems:
                    col_header = f"{cat.name}-{el.name}".replace('_', '-').replace(' ', '-')
                    col_headers.append(col_header)
                    col_to_cat_el[col_header] = (cat.name, el.name)

            # Load tasks using TaskService (supports both new table and legacy study.tasks)
            from app.services.task_service import TaskService
            task_service = TaskService(self.db)
            tasks_dict = task_service.get_all_tasks_as_dict(study_id)
            use_planned = isinstance(tasks_dict, dict) and len(tasks_dict) > 0 and 'respondent_id' in full_df.columns

            if use_planned:
                # --- NAME-BASED MATCHING using Study.tasks (always correct) ---
                # Pre-build per-row: set of (cat_name, el_name) that were shown (visible=1)
                shown_names_by_row: list = []
                for i in range(len(full_df)):
                    row = full_df.iloc[i]
                    rid = row.get('respondent_id')
                    tidx = int(row.get('task_index', 0))
                    tasks_list = tasks_dict.get(str(rid)) or tasks_dict.get(str(max(0, int(rid or 0) - 1))) or []
                    task_def = (tasks_list[tidx] if isinstance(tasks_list, list) and 0 <= tidx < len(tasks_list) else {}) or {}

                    es = task_def.get('elements_shown', {})
                    esc = task_def.get('elements_shown_content', {})

                    shown_set = set()
                    for key, visible in (es.items() if isinstance(es, dict) else []):
                        if int(visible) != 1:
                            continue
                        content = esc.get(key) if isinstance(esc, dict) else None
                        if content and isinstance(content, dict):
                            cat_name = content.get('category_name') or (key.rsplit('_', 1)[0] if '_' in key else '')
                            el_name = content.get('name')
                            if cat_name and el_name:
                                shown_set.add((cat_name, el_name))
                    shown_names_by_row.append(shown_set)

                # Set columns using pre-built name sets
                for col_header in col_headers:
                    cat_name, el_name = col_to_cat_el[col_header]
                    full_df[col_header] = pd.Series(
                        [1 if (cat_name, el_name) in shown_names_by_row[i] else 0 for i in range(len(full_df))],
                        index=full_df.index,
                        dtype=int
                    )
                    dynamic_cols.append(col_header)

            else:
                # --- FALLBACK: index-based matching (when Study.tasks is unavailable) ---
                grid_defs = []
                for cat in cats:
                    elems = elems_by_cat.get(cat.id, [])
                    for idx, el in enumerate(elems, start=1):
                        grid_defs.append((cat.name, el.name, idx))

                esi_list = [x if x is not None else {} for x in full_df['elements_shown_in_task'].tolist()]
                esc_list = [x if x is not None else {} for x in full_df['elements_shown_content'].tolist()]
                esi_df = pd.DataFrame(esi_list)
                esc_df = pd.DataFrame(esc_list)

                def normalize_value(x):
                    if isinstance(x, bool):
                        return int(x)
                    if isinstance(x, (int, float)) and not pd.isna(x):
                        return 1 if x > 0 else 0
                    if isinstance(x, str):
                        return 1 if x.lower() in ("1", "true", "yes", "visible") else 0
                    return 0

                for cat_name, el_name, idx in grid_defs:
                    col_header = f"{cat_name}-{el_name}".replace('_', '-').replace(' ', '-')
                    candidates = [
                        f"{cat_name}_{idx}",
                        f"{cat_name.replace(' ', '_')}_{idx}",
                        f"{cat_name.lower()}_{idx}",
                    ]
                    valid_cols = [c for c in candidates if c in esi_df.columns]

                    full_df[col_header] = 0
                    for c in valid_cols:
                        full_df[col_header] |= esi_df[c].apply(normalize_value)

                    header_key = f"{cat_name}_{el_name}"
                    header_key_alt = header_key.replace(" ", "_")
                    if header_key in esc_df.columns:
                        full_df[col_header] |= esc_df[header_key].notna().astype(int)
                    elif header_key_alt in esc_df.columns:
                        full_df[col_header] |= esc_df[header_key_alt].notna().astype(int)

                    full_df[col_header] = full_df[col_header].astype(int)
                    dynamic_cols.append(col_header)



        else:
            # Layer logic
            from app.models.study_model import StudyLayer, LayerImage
            from sqlalchemy.orm import selectinload
            
            layers = self.db.execute(
                select(StudyLayer)
                .where(StudyLayer.study_id == study_id)
                .order_by(StudyLayer.order)
                .options(selectinload(StudyLayer.images))
            ).scalars().all()
            layer_key_to_header = {}
            layer_keys = []
            
            for layer in layers:
                for idx, img in enumerate(layer.images, start=1):
                    layer_key = f"{layer.name}_{idx}"
                    layer_keys.append(layer_key)
                
                    col_header = f"{layer.name}-{img.name}".replace('_', '-').replace(' ', '-')
                    layer_key_to_header[layer_key] = col_header
                    dynamic_cols.append(col_header)
            
            # Debug: print what we're looking for
            print("Expected layer keys:", layer_keys)
            if not full_df.empty:
                sample_data = full_df.iloc[0]['elements_shown_in_task']
                print("Sample elements_shown_in_task:", sample_data)
            
            # Process layer visibility for each layer key
            for layer_key in layer_keys:
                col_header = layer_key_to_header[layer_key]
                
                def get_layer_visibility(row, key=layer_key):
                    """Extract visibility for this specific layer key from a task row."""
                    # Priority: elements_shown_in_task first (where data actually is)
                    layer_data = (
                        row.get('elements_shown_in_task') or 
                        row.get('layers_shown_in_task') or 
                        row.get('elements_shown_content') or 
                        {}
                    )
                    
                    if not isinstance(layer_data, dict):
                        return 0
                    
                    # Check if this exact key exists (e.g., "cap_1", "cool_2")
                    if key in layer_data:
                        v = layer_data[key]
                        
                        if isinstance(v, dict):
                            # Check the visible flag
                            vis = v.get("visible")
                            if vis is None:
                                # If no explicit visible flag, assume visible if dict has content
                                vis = True if len(v.keys()) > 0 else False
                            return 1 if bool(vis) else 0
                        else:
                            # Non-dict value, treat as boolean/integer
                            try:
                                return 1 if int(v) != 0 else 0
                            except:
                                return 1 if bool(v) else 0
                    
                    return 0
                
                # Apply function to each row using apply on axis=1
                full_df[col_header] = full_df.apply(get_layer_visibility, axis=1)

        # 5. Unilever-only columns (no extra queries; study already loaded)
        if unilever_format:
            # Panelist: panelist_id if present, else session_id
            panelist_ok = full_df['panelist_id'].notna() & (full_df['panelist_id'].astype(str).str.strip() != '')
            full_df['_panelist'] = full_df['panelist_id'].where(panelist_ok, full_df['session_id'])
            # Product ID (study-level, same for all rows)
            full_df['Product ID'] = study.product_id if getattr(study, 'product_id', None) else ''
            # Product keys: one column per key, value = percentage
            product_keys = getattr(study, 'product_keys', None) or []
            for k in product_keys:
                if isinstance(k, dict) and k.get('name'):
                    full_df[k['name']] = k.get('percentage') if k.get('percentage') is not None else ''
            # Task type: grid -> Image, text -> Text, layer/hybrid grid -> Pict, hybrid text -> Text
            st = str(study.study_type)
            if st == 'grid':
                full_df['Task type'] = 'Image'
            elif st == 'text':
                full_df['Task type'] = 'Text'
            elif st == 'layer':
                full_df['Task type'] = 'Pict'
            elif st == 'hybrid':
                def _task_type_label(tt):
                    if pd.isna(tt) or tt is None:
                        return ''
                    t = str(tt).strip().lower()
                    return 'Pict' if t == 'grid' else ('Text' if t == 'text' else '')
                full_df['Task type'] = full_df['task_type'].map(_task_type_label)
            else:
                full_df['Task type'] = ''

        # 6. Assemble Final DataFrame
        # Columns: Panelist, [Product ID], [keys], [Questions], Gender, Age, Task, [Task type], [Dynamic], Rating, ResponseTime

        # Join answers
        # answers_pivot index is study_response_id
        full_df = full_df.merge(answers_pivot, left_on='study_response_id', right_index=True, how='left')

        # Map Yes/No (and Y/N) classification answers to 0/1 only for unilever.com exports
        if unilever_format:
            def _yes_no_to_01(v):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return v
                s = str(v).strip().lower()
                if s in ('yes', 'y'):
                    return 1
                if s in ('no', 'n'):
                    return 0
                return v
            for col in question_id_yes_no_cols:
                if col in full_df.columns:
                    full_df[col] = full_df[col].apply(_yes_no_to_01)

        if unilever_format:
            key_cols = [k['name'] for k in (getattr(study, 'product_keys', None) or []) if isinstance(k, dict) and k.get('name')]
            done_by_cols = ['done_by_id'] if include_done_by_id else []
            final_cols = (
                ['_panelist'] + done_by_cols + ['Product ID', 'Age', 'Gender'] + key_cols +
                list(question_id_to_col.values()) + ['Task', 'Task type'] +
                dynamic_cols + ['rating_given', 'task_duration_seconds']
            )
            export_rename = {
                '_panelist': 'Panelist',
                'done_by_id': 'Done By ID',
                'rating_given': 'Rating',
                'task_duration_seconds': 'ResponseTime'
            }
        else:
            done_by_cols = ['done_by_id'] if include_done_by_id else []
            final_cols = ['session_id'] + done_by_cols + ['Age', 'Gender'] + list(question_id_to_col.values()) + ['Task'] + dynamic_cols + ['rating_given', 'task_duration_seconds']
            export_rename = {
                'session_id': 'Panelist',
                'done_by_id': 'Done By ID',
                'rating_given': 'Rating',
                'task_duration_seconds': 'ResponseTime'
            }

        # NOW ensure all columns exist - fill with 0 for dynamic columns (layers/grid), None for others
        for c in final_cols:
            if c not in full_df.columns:
                # Use 0 for dynamic columns (layer/grid visibility), None for others
                if c in dynamic_cols:
                    full_df[c] = 0
                else:
                    full_df[c] = None
                    
        # Select and rename
        export_df = full_df[final_cols].rename(columns=export_rename)
        return export_df
        # # 6. Yield CSV
        # # Yield header first? to_csv(index=False) includes header.
        # # We can yield chunks.
        
        # # Convert to CSV string
        # # Using a buffer
        # import io
        # output = io.StringIO()
        # # Write header + first chunk
        # # Actually, just dump the whole thing if it fits in memory (Pandas approach usually implies memory fit).
        # # If we want streaming, we can chunk the DF.
        
        # chunk_size = 5000
        # for i in range(0, len(export_df), chunk_size):
        #     chunk = export_df.iloc[i:i+chunk_size]
        #     is_first = (i == 0)
        #     yield chunk.to_csv(index=False, header=is_first, lineterminator='\n')



# ---------- Task Session Service ----------

class TaskSessionService:
    """Service for managing individual task sessions."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_task_session(self, session_data: TaskSessionCreate) -> TaskSession:
        """Create a new task session."""
        # Resolve study_response_id from the provided session_id
        response = self.db.execute(
            select(StudyResponse).where(StudyResponse.session_id == session_data.session_id)
        ).scalar_one_or_none()
        if not response:
            raise HTTPException(status_code=404, detail="Session not found")

        task_session = TaskSession(**session_data.model_dump())
        task_session.study_response_id = response.id

        self.db.add(task_session)
        self.db.commit()
        self.db.refresh(task_session)
        return task_session
    
    def get_task_session(self, session_id: str, task_id: str) -> Optional[TaskSession]:
        """Get a task session by session ID and task ID."""
        stmt = select(TaskSession).where(
            and_(
                TaskSession.session_id == session_id,
                TaskSession.task_id == task_id
            )
        )
        return self.db.execute(stmt).scalars().first()
    
    def update_task_session(self, session_id: str, task_id: str, update_data: Dict[str, Any]) -> Optional[TaskSession]:
        """Update a task session."""
        task_session = self.get_task_session(session_id, task_id)
        if not task_session:
            return None
        
        for field, value in update_data.items():
            if hasattr(task_session, field):
                setattr(task_session, field, value)
        
        task_session.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(task_session)
        return task_session
    
    def add_page_transition(self, session_id: str, task_id: str, page_name: str) -> bool:
        """Add a page transition to a task session."""
        task_session = self.get_task_session(session_id, task_id)
        if not task_session:
            return False
        
        if not task_session.page_transitions:
            task_session.page_transitions = []
        
        transition = {
            "page": page_name,
            "timestamp": datetime.utcnow().isoformat()
        }
        task_session.page_transitions.append(transition)
        task_session.updated_at = datetime.utcnow()
        
        self.db.commit()
        return True
    
    def add_element_interaction(self, session_id: str, task_id: str, interaction_data: ElementInteractionCreate) -> bool:
        """Add an element interaction to a task session."""
        task_session = self.get_task_session(session_id, task_id)
        if not task_session:
            return False
        
        interaction = ElementInteraction(**interaction_data.model_dump())
        interaction.task_session_id = task_session.id
        
        self.db.add(interaction)
        self.db.commit()
        return True