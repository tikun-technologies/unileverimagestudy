# app/services/project_service.py
from __future__ import annotations

from typing import List, Optional, Tuple, Any, Dict

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select, func, text, or_, and_, true, desc, cast, Integer
from fastapi import HTTPException, status
from uuid import UUID
from datetime import datetime

from app.models.project_model import Project
from app.models.study_model import Study, StudyLayer
from app.models.response_model import StudyResponse
from app.schemas.project_schema import (
    ProjectCreate, ProjectUpdate, ProjectOut, ProjectListItem,
    ValidateProductRequest, ValidateProductResponse,
    AssignStudyRequest, AssignStudyResponse,
)
from app.schemas.study_schema import StudyStatus, StudyType


def create_project(
    db: Session,
    creator_id: UUID,
    payload: ProjectCreate
) -> Project:
    """
    Create a new project (ultra-fast, <100ms).
    
    Optimizations:
    - Minimal fields (name, description, creator_id)
    - Single INSERT statement
    - No relationship loading
    - Returns project directly
    """
    project = Project(
        name=payload.name,
        description=payload.description,
        creator_id=creator_id
    )
    
    db.add(project)
    db.commit()
    db.refresh(project)
    
    return project


def get_projects_for_user(
    db: Session,
    user_id: UUID,
    page: int = 1,
    per_page: int = 50
) -> List[ProjectOut]:
    """
    Fetch all projects for a user (owned + shared) with study counts (optimized for <200ms).
    
    Optimizations:
    - Single query with LEFT JOIN and COUNT
    - Includes both owned projects and shared projects
    - No relationship loading (lazy="selectin" disabled for this query)
    - Pagination support
    - Returns list of dicts directly
    """
    from app.models.project_model import ProjectMember
    
    offset = (page - 1) * per_page
    
    from sqlalchemy import case, String, and_
    
    # Optimized query: fetch projects (owned or shared) with study count in one go
    # Use CASE to determine role: 'admin' if creator, else use role from ProjectMember
    query = (
        select(
            Project.id,
            Project.name,
            Project.description,
            Project.creator_id,
            Project.created_at,
            Project.updated_at,
            func.count(Study.id).label('study_count'),
            case(
                (Project.creator_id == user_id, 'admin'),
                else_=func.max(func.cast(ProjectMember.role, String))
            ).label('role')
        )
        .outerjoin(Study, Study.project_id == Project.id)
        .outerjoin(ProjectMember, and_(ProjectMember.project_id == Project.id, ProjectMember.user_id == user_id))
        .where(
            or_(
                Project.creator_id == user_id,
                ProjectMember.user_id == user_id
            )
        )
        .group_by(Project.id, Project.name, Project.description, Project.creator_id, Project.created_at, Project.updated_at)
        .order_by(Project.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    
    result = db.execute(query).all()
    
    # Convert to list of ProjectOut
    projects = []
    for row in result:
        projects.append(ProjectOut(
            id=row.id,
            name=row.name,
            description=row.description,
            creator_id=row.creator_id,
            role=row.role or 'viewer', # Fallback if somehow null
            created_at=row.created_at,
            updated_at=row.updated_at,
            study_count=int(row.study_count or 0)
        ))
    
    return projects


def get_project_studies(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    page: int = 1,
    per_page: int = 10,
    *,
    status_filter: Optional[StudyStatus] = None,
    study_type: Optional[StudyType] = None,
    search: Optional[str] = None,
    created_after: Optional[datetime] = None,
) -> Tuple[List[dict], int, Dict[str, int]]:
    """
    Paginated project studies with optional search/type/status/time filters.
    Allows access for both project owner and project members.
    Uses page-then-aggregate counts (same pattern as list_studies) for speed.
    Returns (items, total, status_counts).
    """
    # Verify the project exists and user has access (owner or member)
    get_project(db, project_id, user_id)

    page = max(1, int(page or 1))
    per_page = min(max(1, int(per_page or 10)), 200)
    offset = (page - 1) * per_page

    base_filters = [Study.project_id == project_id]
    if study_type:
        base_filters.append(Study.study_type == study_type)
    if search and search.strip():
        term = f"%{search.strip()}%"
        base_filters.append(or_(Study.title.ilike(term), Study.product_id.ilike(term)))
    if created_after is not None:
        base_filters.append(Study.created_at >= created_after)

    status_count_stmt = (
        select(Study.status, func.count(Study.id))
        .where(*base_filters)
        .group_by(Study.status)
    )
    status_rows = db.execute(status_count_stmt).all()
    status_counts: Dict[str, int] = {
        "total": 0,
        "active": 0,
        "draft": 0,
        "completed": 0,
        "paused": 0,
    }
    for status_value, cnt in status_rows:
        key = status_value if isinstance(status_value, str) else getattr(status_value, "value", str(status_value))
        count_int = int(cnt or 0)
        if key in status_counts:
            status_counts[key] = count_int
        status_counts["total"] += count_int

    # Derive total from status_counts — avoids a separate COUNT round-trip
    if status_filter:
        status_key = (
            status_filter
            if isinstance(status_filter, str)
            else getattr(status_filter, "value", str(status_filter))
        )
        total = int(status_counts.get(status_key, 0))
    else:
        total = int(status_counts["total"])

    filtered = list(base_filters)
    if status_filter:
        filtered.append(Study.status == status_filter)

    respondents_target_expr = func.coalesce(
        cast(Study.audience_segmentation["number_of_respondents"].astext, Integer),
        0,
    ).label("respondents_target")

    rows = db.execute(
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.status,
            Study.created_at,
            Study.updated_at,
            Study.last_step,
            Study.project_id,
            Study.product_id,
            Study.product_keys,
            respondents_target_expr,
        )
        .where(*filtered)
        .order_by(desc(Study.created_at), desc(Study.id))
        .offset(offset)
        .limit(per_page)
    ).all()

    if not rows:
        return [], int(total), status_counts

    study_ids = [row.id for row in rows]
    count_rows = db.execute(
        select(
            StudyResponse.study_id.label("study_id"),
            func.count(StudyResponse.id).label("total_responses_calc"),
            func.count(StudyResponse.id).filter(StudyResponse.is_completed == True).label("completed_responses_calc"),
            func.count(StudyResponse.id).filter(StudyResponse.is_abandoned == True).label("abandoned_responses_calc"),
        )
        .where(StudyResponse.study_id.in_(study_ids))
        .group_by(StudyResponse.study_id)
    ).all()
    counts_by_study = {
        c.study_id: {
            "total_responses": int(c.total_responses_calc or 0),
            "completed_responses": int(c.completed_responses_calc or 0),
            "abandoned_responses": int(c.abandoned_responses_calc or 0),
        }
        for c in count_rows
    }

    studies = []
    for row in rows:
        counts = counts_by_study.get(
            row.id,
            {"total_responses": 0, "completed_responses": 0, "abandoned_responses": 0},
        )
        total_calc = counts["total_responses"]
        completed_calc = counts["completed_responses"]
        abandoned_calc = counts["abandoned_responses"]
        respondents_target = int(getattr(row, "respondents_target", 0) or 0)
        studies.append({
            "id": str(row.id),
            "title": row.title,
            "study_type": row.study_type,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "last_step": row.last_step,
            "project_id": str(row.project_id) if row.project_id else None,
            "product_id": row.product_id,
            "product_keys": row.product_keys,
            "total_responses": total_calc,
            "completed_responses": completed_calc,
            "abandoned_responses": abandoned_calc,
            "respondents_target": respondents_target,
            "respondents_completed": completed_calc,
            "completion_rate": (completed_calc / total_calc * 100) if total_calc else 0,
            "abandonment_rate": (abandoned_calc / total_calc * 100) if total_calc else 0,
            "average_duration": 0,
        })
    return studies, int(total), status_counts


def get_project_studies_for_export(
    db: Session,
    project_id: UUID,
    user_id: UUID,
) -> List[Study]:
    """
    Fetch all studies for a project ordered by product_id (asc, nulls last).
    Verifies project access (owner or member). Returns full Study objects with
    categories, elements, layers.images, classification_questions loaded for building study_data.
    """
    get_project(db, project_id, user_id)
    stmt = (
        select(Study)
        .options(
            selectinload(Study.categories),
            selectinload(Study.elements),
            selectinload(Study.layers).selectinload(StudyLayer.images),
            selectinload(Study.classification_questions),
        )
        .where(Study.project_id == project_id)
        .order_by(Study.product_id.asc().nulls_last())
    )
    return list(db.scalars(stmt).all())


def get_project(
    db: Session,
    project_id: UUID,
    user_id: UUID
) -> Project:
    """
    Get a single project by ID (with access check for owner or member).
    """
    from app.models.project_model import ProjectMember
    
    # Check if user is creator
    project = db.scalar(
        select(Project)
        .where(Project.id == project_id, Project.creator_id == user_id)
    )
    
    if not project:
        # Check if user is a member
        stmt_member = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(Project.id == project_id, ProjectMember.user_id == user_id)
        )
        project = db.scalars(stmt_member).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or access denied"
        )
    
    return project


def _key_signature(keys: Optional[List[Any]]) -> Optional[Tuple[Tuple[str, ...], Tuple[float, ...]]]:
    """Build canonical signature (names_sorted, percentages_sorted) for key combination comparison."""
    if not keys:
        return None
    pairs = []
    for k in keys:
        if isinstance(k, dict):
            name = k.get("name")
            if not name:
                continue
            pairs.append((name, float(k.get("percentage", 0))))
        else:
            name = getattr(k, "name", None)
            pct = getattr(k, "percentage", 0)
            if name is not None:
                pairs.append((name, float(pct)))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    return (tuple(p[0] for p in pairs), tuple(p[1] for p in pairs))


def validate_product_uniqueness(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    payload: ValidateProductRequest,
) -> ValidateProductResponse:
    """
    Check that product_id and key percentage combination are unique within the project.
    Single lightweight query (id, product_id, product_keys only); response in double-digit ms.
    """
    get_project(db, project_id, user_id)

    product_id_taken = False
    key_combination_taken = False

    check_product_id = payload.product_id is not None and (payload.product_id or "").strip() != ""
    request_sig = _key_signature(payload.product_keys) if payload.product_keys else None
    check_keys = request_sig is not None

    if not check_product_id and not check_keys:
        return ValidateProductResponse(valid=True)

    query = (
        select(Study.id, Study.product_id, Study.product_keys)
        .where(Study.project_id == project_id)
    )
    rows = db.execute(query).all()

    for row in rows:
        if payload.study_id and row.id == payload.study_id:
            continue
        if check_product_id and row.product_id and (row.product_id or "").strip() == (payload.product_id or "").strip():
            product_id_taken = True
        if check_keys and row.product_keys:
            study_sig = _key_signature(row.product_keys)
            if study_sig and study_sig == request_sig:
                key_combination_taken = True
        if product_id_taken and key_combination_taken:
            break

    return ValidateProductResponse(
        valid=not (product_id_taken or key_combination_taken),
        product_id_taken=product_id_taken,
        key_combination_taken=key_combination_taken,
    )


def validate_product_by_study(
    db: Session,
    user_id: UUID,
    payload: ValidateProductRequest,
) -> ValidateProductResponse:
    """
    Validate product_id and key combination uniqueness within the study's project.
    Derives project_id from study_id. If study has no project, returns valid=True (nothing to validate).
    Single lightweight query; response in double-digit ms.
    """
    from app.services.study import check_study_access

    check_study_access(db, payload.study_id, user_id)

    row = db.execute(select(Study.project_id).where(Study.id == payload.study_id)).first()
    project_id = row.project_id if row and row.project_id else None

    if project_id is None:
        return ValidateProductResponse(valid=True)

    return validate_product_uniqueness(db, project_id, user_id, payload)


def assign_study_to_project(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    payload: AssignStudyRequest,
) -> AssignStudyResponse:
    """
    Assign a standalone study (no project) to a project.
    Ultra-fast: single batch query for validation, minimal updates.

    Rules:
    - Study must not already be in any project
    - User must be editor or admin in study (viewer cannot assign)
    - User must be editor or admin in project (viewer cannot assign)
    - Project creator becomes study admin; study creator demoted per project logic
    """
    from app.models.project_model import ProjectMember
    from app.models.study_model import StudyMember
    from app.services.project_member_service import project_member_service
    from app.models.user_model import User

    study_id = payload.study_id

    # Single query: study (id, project_id, creator_id, product_id, product_keys) + project (creator_id)
    row = db.execute(
        select(
            Study.id,
            Study.project_id,
            Study.creator_id,
            Study.product_id,
            Study.product_keys,
            Project.creator_id.label("project_creator_id"),
        )
        .select_from(Study)
        .join(Project, true())
        .where(Study.id == study_id, Project.id == project_id)
    ).first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Study or project not found",
        )

    if row.project_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Study is already in a project",
        )

    study_creator_id = row.creator_id

    # Product uniqueness: if study has product_id or product_keys, ensure no project study conflicts
    check_product_id = row.product_id is not None and (row.product_id or "").strip() != ""
    study_key_sig = _key_signature(row.product_keys)
    check_keys = study_key_sig is not None

    if check_product_id or check_keys:
        project_studies = db.execute(
            select(Study.id, Study.product_id, Study.product_keys).where(
                Study.project_id == project_id
            )
        ).all()
        for ps in project_studies:
            if check_product_id and ps.product_id and (ps.product_id or "").strip() == (row.product_id or "").strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A study in this project already has the same product ID",
                )
            if check_keys and ps.product_keys:
                ps_sig = _key_signature(ps.product_keys)
                if ps_sig and ps_sig == study_key_sig:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="A study in this project already has the same key combination",
                    )
    project_creator_id = row.project_creator_id

    # User's study role: creator/admin/editor can assign; viewer cannot
    if study_creator_id == user_id:
        study_role_ok = True
    else:
        sm = db.execute(
            select(StudyMember.role).where(
                StudyMember.study_id == study_id,
                StudyMember.user_id == user_id,
            )
        ).first()
        study_role_ok = sm is not None and sm.role in ("admin", "editor")
    if not study_role_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only editors and admins can assign studies. Viewers cannot.",
        )

    # User's project role: admin/editor can assign; viewer cannot
    if project_creator_id == user_id:
        project_role_ok = True
    else:
        pm = db.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        ).first()
        project_role_ok = pm is not None and pm.role == "editor"
    if not project_role_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only editors and admins can assign studies in this project. Viewers cannot.",
        )

    # Update study.project_id and sync
    study = db.get(Study, study_id)
    study.project_id = project_id
    db.flush()

    project_member_service.sync_new_study_to_project_members(
        db=db, study_id=study_id, project_id=project_id
    )

    # Demote study creator if not project creator: set their StudyMember role to project role or viewer
    # When study creator IS a project member: sync already added them - do NOT add again (would duplicate)
    # When study creator is NOT a project member: add them as viewer so they keep access but can't edit
    if study_creator_id != project_creator_id:
        pm_creator = db.execute(
            select(ProjectMember.role, ProjectMember.invited_email).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == study_creator_id,
            )
        ).first()
        if pm_creator:
            # Study creator is a project member - sync already added them with project role. Skip.
            pass
        else:
            # Study creator is NOT in project - add as viewer (demoted)
            user_row = db.execute(
                select(User.email).where(User.id == study_creator_id)
            ).first()
            creator_email = user_row.email if user_row else ""

            existing = db.scalars(
                select(StudyMember).where(
                    StudyMember.study_id == study_id,
                    StudyMember.user_id == study_creator_id,
                )
            ).first()

            if existing:
                existing.role = "viewer"
            else:
                from uuid import uuid4
                db.add(
                    StudyMember(
                        id=uuid4(),
                        study_id=study_id,
                        user_id=study_creator_id,
                        role="viewer",
                        invited_email=creator_email,
                    )
                )

    db.commit()
    return AssignStudyResponse(message="Study assigned to project successfully")


def update_project(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    payload: ProjectUpdate
) -> Project:
    """
    Update a project (owner only).
    """
    project = get_project(db, project_id, user_id)
    
    # Only creator can update project settings
    if project.creator_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the project creator can update project settings"
        )
    
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    
    db.commit()
    db.refresh(project)
    
    return project


def delete_project(
    db: Session,
    project_id: UUID,
    user_id: UUID
) -> None:
    """
    Delete a project (owner only).
    """
    project = get_project(db, project_id, user_id)
    
    # Only creator can delete project
    if project.creator_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the project creator can delete the project"
        )
    
    db.delete(project)
    db.commit()
