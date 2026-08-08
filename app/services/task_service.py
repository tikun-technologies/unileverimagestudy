"""
TaskService: Central service for all task assignment operations.

This service handles reading and writing task assignments to/from the 
study_task_assignments table, with fallback support for legacy study.tasks JSONB.
"""

from typing import Dict, List, Any, Optional, Sequence
from uuid import UUID
import logging

from sqlalchemy.orm import Session
from sqlalchemy import select, delete, func, distinct

from app.models.study_model import Study, StudyTaskAssignment

logger = logging.getLogger(__name__)


class TaskService:
    """Service for managing study task assignments."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_respondent_task_list(
        self, study_id: UUID, respondent_id: int
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Per-respondent equivalent of ``get_all_tasks_as_dict(study_id).get(str(respondent_id))``.

        Returns the same list that the full-study dict would yield for this respondent,
        or None when the respondent has no entry — mirroring ``dict.get`` so callers can
        fall through to their next candidate key. Reading one respondent instead of the
        whole study keeps submission cost flat as a study's respondent pool grows.

        Unlike the other readers this never calls rollback(): it runs inside request
        transactions that already hold pending writes.
        """
        try:
            rows = self.db.execute(
                select(StudyTaskAssignment)
                .where(
                    StudyTaskAssignment.study_id == study_id,
                    StudyTaskAssignment.respondent_id == respondent_id,
                )
                .order_by(StudyTaskAssignment.task_index)
            ).scalars().all()
            if rows:
                return [self._assignment_to_dict(a) for a in rows]

            # No rows for this respondent. Distinguish "study uses normalized storage but
            # this respondent isn't in it" (-> None, same as a missing dict key) from
            # "study has no normalized rows at all" (-> fall back to legacy JSONB).
            study_has_assignments = self.db.execute(
                select(StudyTaskAssignment.id)
                .where(StudyTaskAssignment.study_id == study_id)
                .limit(1)
            ).scalar_one_or_none()
            if study_has_assignments is not None:
                return None
        except Exception as e:
            # Table may not exist on older databases - fall through to legacy.
            logger.debug(f"study_task_assignments lookup failed, using legacy: {e}")

        legacy_value = self._get_legacy_tasks(study_id).get(str(respondent_id))
        return legacy_value if isinstance(legacy_value, list) else None

    def get_preview_tasks_as_dict(
        self, study_id: UUID, *, preview_respondent_id: int = 1
    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """
        Tasks for preview endpoints: one respondent in the same dict shape as
        get_all_tasks_as_dict, without loading every respondent.

        Prefers ``preview_respondent_id`` (default 1). When that respondent has no
        tasks, falls back to the lowest respondent id present — matching the prior
        preview behavior of picking the first numeric dict key.
        """
        tasks = self.get_respondent_tasks(study_id, preview_respondent_id)
        if tasks:
            return {str(preview_respondent_id): tasks}

        try:
            first_id = self.db.execute(
                select(StudyTaskAssignment.respondent_id)
                .where(StudyTaskAssignment.study_id == study_id)
                .order_by(StudyTaskAssignment.respondent_id)
                .limit(1)
            ).scalar_one_or_none()
            if first_id is not None and int(first_id) != preview_respondent_id:
                fallback_tasks = self.get_respondent_tasks(study_id, int(first_id))
                if fallback_tasks:
                    return {str(first_id): fallback_tasks}
        except Exception as e:
            logger.debug(f"preview first respondent lookup failed: {e}")
            self.db.rollback()

        legacy_tasks = self._get_legacy_tasks(study_id)
        if isinstance(legacy_tasks, dict):
            first_key = next((k for k in legacy_tasks if str(k).isdigit()), None)
            if first_key is not None:
                val = legacy_tasks[first_key]
                if isinstance(val, list) and val:
                    return {first_key: val}
                if val and not isinstance(val, list):
                    return {first_key: [val]}

        return None

    def get_all_tasks_as_dict(self, study_id: UUID) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all tasks for a study in the original dict format:
        {"1": [task1, task2, ...], "2": [...], ...}
        
        Falls back to legacy study.tasks if new table is empty or doesn't exist.
        """
        try:
            assignments = self.db.execute(
                select(StudyTaskAssignment)
                .where(StudyTaskAssignment.study_id == study_id)
                .order_by(StudyTaskAssignment.respondent_id, StudyTaskAssignment.task_index)
            ).scalars().all()
            
            if assignments:
                return self._group_assignments_to_dict(assignments)
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        return self._get_legacy_tasks(study_id)
    
    def get_respondent_tasks(self, study_id: UUID, respondent_id: int) -> List[Dict[str, Any]]:
        """
        Get tasks for a single respondent.
        
        Falls back to legacy study.tasks if new table is empty or doesn't exist.
        """
        try:
            assignments = self.db.execute(
                select(StudyTaskAssignment)
                .where(
                    StudyTaskAssignment.study_id == study_id,
                    StudyTaskAssignment.respondent_id == respondent_id
                )
                .order_by(StudyTaskAssignment.task_index)
            ).scalars().all()
            
            if assignments:
                return [self._assignment_to_dict(a) for a in assignments]
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        legacy_tasks = self._get_legacy_tasks(study_id)
        respondent_key = str(respondent_id)
        if respondent_key in legacy_tasks:
            tasks = legacy_tasks[respondent_key]
            if isinstance(tasks, list):
                return tasks
            return [tasks] if tasks else []
        
        return []
    
    def get_task_by_index(self, study_id: UUID, respondent_id: int, task_index: int) -> Optional[Dict[str, Any]]:
        """Get a specific task by respondent and index."""
        try:
            assignment = self.db.execute(
                select(StudyTaskAssignment)
                .where(
                    StudyTaskAssignment.study_id == study_id,
                    StudyTaskAssignment.respondent_id == respondent_id,
                    StudyTaskAssignment.task_index == task_index
                )
            ).scalars().first()
            
            if assignment:
                return self._assignment_to_dict(assignment)
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        legacy_tasks = self._get_legacy_tasks(study_id)
        respondent_key = str(respondent_id)
        if respondent_key in legacy_tasks:
            tasks = legacy_tasks[respondent_key]
            if isinstance(tasks, list) and 0 <= task_index < len(tasks):
                return tasks[task_index]
        
        return None
    
    def get_task_count(self, study_id: UUID) -> int:
        """Get total task count for a study."""
        try:
            count = self.db.execute(
                select(func.count()).select_from(StudyTaskAssignment)
                .where(StudyTaskAssignment.study_id == study_id)
            ).scalar() or 0
            
            if count > 0:
                return count
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        legacy_tasks = self._get_legacy_tasks(study_id)
        if legacy_tasks:
            return sum(
                len(tasks) if isinstance(tasks, list) else 1
                for key, tasks in legacy_tasks.items()
                if str(key).isdigit()
            )
        return 0
    
    def get_respondent_count(self, study_id: UUID) -> int:
        """Get number of unique respondents with tasks."""
        try:
            count = self.db.execute(
                select(func.count(distinct(StudyTaskAssignment.respondent_id)))
                .where(StudyTaskAssignment.study_id == study_id)
            ).scalar() or 0
            
            if count > 0:
                return count
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        legacy_tasks = self._get_legacy_tasks(study_id)
        if legacy_tasks:
            return len([k for k in legacy_tasks if str(k).isdigit()])
        return 0
    
    def get_tasks_per_respondent(self, study_id: UUID) -> int:
        """Get tasks per respondent (from first respondent)."""
        try:
            result = self.db.execute(
                select(func.count())
                .select_from(StudyTaskAssignment)
                .where(
                    StudyTaskAssignment.study_id == study_id,
                    StudyTaskAssignment.respondent_id == 1
                )
            ).scalar() or 0
            
            if result > 0:
                return result
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        legacy_tasks = self._get_legacy_tasks(study_id)
        if legacy_tasks:
            first_key = next((k for k in legacy_tasks if str(k).isdigit()), None)
            if first_key is not None:
                tasks = legacy_tasks[first_key]
                if isinstance(tasks, list):
                    return len(tasks)
                return 1 if tasks else 0
        return 0
    
    def has_tasks(self, study_id: UUID) -> bool:
        """Check if study has any tasks (in new table or legacy)."""
        try:
            exists = self.db.execute(
                select(StudyTaskAssignment.id)
                .where(StudyTaskAssignment.study_id == study_id)
                .limit(1)
            ).scalar_one_or_none()
            
            if exists:
                return True
        except Exception as e:
            # Table may not exist in older databases - fall back to legacy
            logger.debug(f"study_task_assignments query failed, using legacy: {e}")
            self.db.rollback()
        
        study = self.db.get(Study, study_id)
        legacy_tasks = getattr(study, "tasks", None) if study is not None else None
        if legacy_tasks:
            if isinstance(legacy_tasks, dict):
                return any(str(k).isdigit() for k in legacy_tasks)
            return bool(legacy_tasks)
        return False
    
    def save_tasks(self, study_id: UUID, tasks: Dict[str, List[Dict[str, Any]]]) -> int:
        """
        Save tasks from dict format to the new table.
        Deletes existing tasks for this study before inserting.
        
        Returns number of rows inserted.

        Raises RuntimeError when the normalized task table cannot be written.
        Generation callers must fail loudly rather than report success with no
        persisted tasks.
        """
        try:
            self.db.execute(
                delete(StudyTaskAssignment).where(StudyTaskAssignment.study_id == study_id)
            )
            
            count = 0
            batch = []
            BATCH_SIZE = 5000
            
            for respondent_id_str, task_list in tasks.items():
                if not str(respondent_id_str).isdigit():
                    continue
                respondent_id = int(respondent_id_str)
                
                if not isinstance(task_list, list):
                    continue
                    
                for task in task_list:
                    batch.append(StudyTaskAssignment(
                        study_id=study_id,
                        respondent_id=respondent_id,
                        task_index=task.get("task_index", 0),
                        task_id=task.get("task_id", f"{respondent_id}_{task.get('task_index', 0)}"),
                        elements_shown=task.get("elements_shown", {}),
                        elements_shown_content=task.get("elements_shown_content"),
                        phase_type=task.get("phase_type")
                    ))
                    count += 1
                    
                    if len(batch) >= BATCH_SIZE:
                        self.db.bulk_save_objects(batch)
                        batch = []
            
            if batch:
                self.db.bulk_save_objects(batch)
            
            self.db.flush()
            logger.info(f"Saved {count} task assignments for study {study_id}")
            return count
        except Exception as e:
            logger.error(f"Could not save task assignments for study {study_id}: {e}")
            self.db.rollback()
            raise RuntimeError(f"Failed to save task assignments for study {study_id}") from e
    
    def delete_tasks(self, study_id: UUID) -> int:
        """Delete all task assignments for a study."""
        try:
            result = self.db.execute(
                delete(StudyTaskAssignment).where(StudyTaskAssignment.study_id == study_id)
            )
            count = result.rowcount
            self.db.flush()
            logger.info(f"Deleted {count} task assignments for study {study_id}")
            return count
        except Exception as e:
            # Table may not exist in older databases
            logger.debug(f"Could not delete from study_task_assignments (table may not exist): {e}")
            self.db.rollback()
            return 0
    
    def _get_legacy_tasks(self, study_id: UUID) -> Dict[str, List[Dict[str, Any]]]:
        """Get tasks from legacy study.tasks JSONB column."""
        study = self.db.get(Study, study_id)
        legacy_tasks = getattr(study, "tasks", None) if study is not None else None
        if not legacy_tasks:
            return {}
        
        if isinstance(legacy_tasks, dict):
            return legacy_tasks
        elif isinstance(legacy_tasks, list):
            return {"0": legacy_tasks}
        return {}
    
    def _assignment_to_dict(self, assignment: StudyTaskAssignment) -> Dict[str, Any]:
        """Convert a StudyTaskAssignment to dict format."""
        result = {
            "task_id": assignment.task_id,
            "task_index": assignment.task_index,
            "elements_shown": assignment.elements_shown,
            "elements_shown_content": assignment.elements_shown_content,
        }
        phase_type = getattr(assignment, "phase_type", None)
        if phase_type not in (None, ""):
            result["phase_type"] = phase_type
        return result
    
    def _group_assignments_to_dict(self, assignments: Sequence[StudyTaskAssignment]) -> Dict[str, List[Dict[str, Any]]]:
        """Group assignments into dict format keyed by respondent_id."""
        result: Dict[str, List[Dict[str, Any]]] = {}
        for a in assignments:
            key = str(a.respondent_id)
            if key not in result:
                result[key] = []
            result[key].append(self._assignment_to_dict(a))
        return result
