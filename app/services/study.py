# app/services/study.py
from __future__ import annotations

import re
from typing import Optional, Tuple, List, Dict, Any
from uuid import UUID, uuid4
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select, func, desc, and_, or_
from fastapi import HTTPException, status
import logging

from app.models.billing_model import UserBillingProfile
from app.models.study_model import Study, StudyElement, StudyLayer, LayerImage, StudyClassificationQuestion, StudyCategory, StudyMember
from app.models.response_model import StudyResponse
from app.schemas.study_schema import (
    StudyCreate, StudyUpdate, StudyOut, StudyListItem,
    StudyStatus, StudyType, RegenerateTasksResponse, ValidateTasksResponse,
    StudyPublicMinimal
)
from app.services.task_generation_adapter import generate_grid_tasks, generate_layer_tasks
from app.services.cloudinary_service import upload_base64, delete_public_id
from app.core.config import settings
from app.core.cache import invalidate_study_cache
from app.core.domain import (
    is_unilever_domain,
    FRAGRANCE_QUESTION_ID,
    FRAGRANCE_QUESTION_TEXT,
    FRAGRANCE_ANSWER_OPTIONS,
    FRAGRANCE_ORDER,
)

logger = logging.getLogger(__name__)

# If you have an adapter wrapping your existing IPED task generation algorithms,
# import it here. The adapter should provide two callables with these signatures:
# - generate_grid_tasks(num_elements, tasks_per_consumer, number_of_respondents, exposure_tolerance_cv, seed, elements) -> dict
# - generate_layer_tasks(layers, number_of_respondents, exposure_tolerance_pct, seed) -> dict
#
# from app.services.task_generation_adapter import generate_grid_tasks, generate_layer_tasks

# ---------- Helpers ----------

def _ensure_study_type_constraints(payload: StudyCreate) -> None:
    if payload.study_type == 'grid':
        if not payload.elements or len(payload.elements) < 1:
            raise HTTPException(status_code=400, detail="Grid study requires non-empty elements.")
    elif payload.study_type == 'layer':
        if not payload.study_layers or len(payload.study_layers) < 1:
            raise HTTPException(status_code=400, detail="Layer study requires at least one layer with images.")
    elif payload.study_type == 'text':
        if not payload.elements or len(payload.elements) < 1:
            raise HTTPException(status_code=400, detail="Text study requires non-empty elements (statements).")
    elif payload.study_type == 'hybrid':
        if not payload.elements or len(payload.elements) < 1:
            raise HTTPException(status_code=400, detail="Hybrid study requires non-empty elements.")
        if not payload.phase_order or len(payload.phase_order) < 1:
            raise HTTPException(status_code=400, detail="Hybrid study requires phase_order (e.g., ['grid', 'text'] or ['mix']).")
    else:
        raise HTTPException(status_code=400, detail="Unsupported study_type. Must be 'grid', 'layer', 'text', or 'hybrid'.")

def _validate_rating_scale(rating_scale: Dict[str, Any]) -> None:
    logger.info(f"Validating rating scale: {rating_scale}")
    
    required = ['min_value', 'max_value', 'min_label', 'max_label']
    for k in required:
        if k not in rating_scale:
            logger.error(f"rating_scale missing '{k}'")
            raise HTTPException(status_code=400, detail=f"rating_scale missing '{k}'")
    
    if rating_scale['max_value'] not in [5, 7, 9]:
        logger.error(f"rating_scale.max_value {rating_scale['max_value']} not in [5, 7, 9]")
        raise HTTPException(status_code=400, detail="rating_scale.max_value must be one of 5, 7, 9.")
    
    if not (1 <= int(rating_scale['min_value']) <= 9):
        logger.error(f"rating_scale.min_value {rating_scale['min_value']} not in range 1-9")
        raise HTTPException(status_code=400, detail="rating_scale.min_value must be between 1 and 9 inclusive.")
    
    if int(rating_scale['min_value']) > int(rating_scale['max_value']):
        logger.error(f"rating_scale.min_value {rating_scale['min_value']} > max_value {rating_scale['max_value']}")
        raise HTTPException(status_code=400, detail="rating_scale.min_value cannot exceed max_value.")
    
    logger.info("Rating scale validation passed")

def _generate_share_token() -> str:
    return uuid4().hex

def _build_share_url(base_url: Optional[str], study_id: str) -> str:
    # Use provided base_url or fall back to settings
    url = base_url or settings.BASE_URL
    return f"{url.rstrip('/')}/participate/{study_id}"


def _maybe_upload_data_url_to_cloudinary(url_or_data: Optional[str]) -> tuple[str, Optional[str]]:
    """If value is a data URL, upload to Cloudinary and return (secure_url, public_id).
    Otherwise return (original_value, None).
    """
    if not url_or_data:
        return "", None
    val = url_or_data.strip()
    if val.startswith("data:image"):
        secure_url, public_id = upload_base64(val, folder="studies")
        return secure_url, public_id
    return val, None


def _get_effective_edit_permission(db: Session, study: Study, user_id: UUID) -> bool:
    """
    Check if user can edit a study, respecting project-level role hierarchy.
    
    Permission hierarchy:
    1. Project owner can always edit studies in their project
    2. Study creator can edit ONLY IF their project role is 'editor' (not 'viewer')
    3. Study members with 'admin' or 'editor' role can edit
    4. Studies without a project: creator can always edit
    
    Returns True if user can edit, False otherwise.
    """
    from app.models.project_model import Project, ProjectMember
    
    # 1. Check project-level permissions if study belongs to a project
    if study.project_id:
        project = db.get(Project, study.project_id)
        if project:
            # Project owner can always edit
            if project.creator_id == user_id:
                return True
            
            # Check project member role
            project_member = db.scalars(
                select(ProjectMember).where(
                    ProjectMember.project_id == study.project_id,
                    ProjectMember.user_id == user_id
                )
            ).first()
            
            if project_member:
                # Only project editors can edit studies (even if they created them)
                if project_member.role != 'editor':
                    return False
                return True
            
            # User is not a project member - fall through to study-level checks
    
    # 2. Study creator: can edit only if standalone study, or if still in project.
    #    If study has a project and user is creator but not project member, they were
    #    removed from project and demoted to viewer → use StudyMember role (no edit).
    if study.creator_id == user_id:
        if study.project_id:
            study_member_role = db.scalar(
                select(StudyMember.role).where(
                    StudyMember.study_id == study.id,
                    StudyMember.user_id == user_id
                )
            )
            return study_member_role in ('admin', 'editor')
        return True  # Standalone study: creator can always edit

    # 3. Check study-level member role
    study_member_role = db.scalar(
        select(StudyMember.role).where(
            StudyMember.study_id == study.id,
            StudyMember.user_id == user_id
        )
    )
    
    return study_member_role in ('admin', 'editor')


def _load_owned_study(db: Session, study_id: UUID, owner_id: UUID, for_update: bool = False) -> Study:

    stmt = (
        select(Study)
        .options(
            selectinload(Study.categories),
            selectinload(Study.elements),
            selectinload(Study.layers).selectinload(StudyLayer.images),
            selectinload(Study.classification_questions),
        )
        .where(Study.id == study_id, Study.creator_id == owner_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    study = db.scalars(stmt).first()

    if not study:
        # Check if the user is a member (Editor or Viewer)
        stmt_member = (
            select(Study)
            .join(StudyMember, StudyMember.study_id == Study.id)
            .options(
                selectinload(Study.categories),
                selectinload(Study.elements),
                selectinload(Study.layers).selectinload(StudyLayer.images),
                selectinload(Study.classification_questions),
            )
            .where(Study.id == study_id, StudyMember.user_id == owner_id)
        )
        study = db.scalars(stmt_member).first()
        
    if not study:
        raise HTTPException(status_code=404, detail="Study not found or access denied.")
    
    # If for_update is True, check effective edit permission (respects project-level roles)
    if for_update:
        if not _get_effective_edit_permission(db, study, owner_id):
            raise HTTPException(status_code=403, detail="You do not have permission to edit this study.")

                
    return study

def _load_owned_study_minimal(db: Session, study_id: UUID, owner_id: UUID, for_update: bool = False) -> Study:
    """Optimized study loading for operations that don't need related data (like launch)."""
    stmt = (
        select(Study)
        .where(Study.id == study_id, Study.creator_id == owner_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    study = db.scalars(stmt).first()

    if not study:
        # Check if user is a member
        stmt_member = (
            select(Study)
            .join(StudyMember, StudyMember.study_id == Study.id)
            .where(Study.id == study_id, StudyMember.user_id == owner_id)
        )
        study = db.scalars(stmt_member).first()

    if not study:
        raise HTTPException(status_code=404, detail="Study not found or access denied.")
    return study

def _load_owned_study_launch_only(db: Session, study_id: UUID, owner_id: UUID) -> Study:
    """Ultra-minimal loading for launch-only operations - only loads essential fields."""
    stmt = (
        select(Study.id, Study.title, Study.status, Study.launched_at, Study.creator_id)
        .where(Study.id == study_id, Study.creator_id == owner_id)
        .with_for_update()
    )
    result = db.execute(stmt).first()
    
    if not result:
        # Check if user is a member
        stmt_member = (
            select(Study.id, Study.title, Study.status, Study.launched_at, Study.creator_id)
            .join(StudyMember, StudyMember.study_id == Study.id)
            .where(Study.id == study_id, StudyMember.user_id == owner_id)
            .with_for_update()
        )
        result = db.execute(stmt_member).first()

    if not result:
        raise HTTPException(status_code=404, detail="Study not found or access denied.")
    
    # Create minimal study object with only needed fields
    study = Study()
    study.id = result.id
    study.title = result.title
    study.status = result.status
    study.launched_at = result.launched_at
    study.creator_id = result.creator_id
    return study

# ---------- CRUD ----------

def create_study(
    db: Session,
    creator_id: UUID,
    payload: StudyCreate,
    *,
    base_url_for_share: Optional[str] = None
) -> Study:
    _ensure_study_type_constraints(payload)
    _validate_rating_scale(payload.rating_scale.model_dump())

    share_token = _generate_share_token()
    
    # Persist aspect_ratio inside audience_segmentation JSON to avoid migrations
    audience_segmentation_json = payload.audience_segmentation.model_dump(exclude_none=True)
    # Prefer top-level aspect_ratio; else accept inside segmentation
    ar_top = getattr(payload, 'aspect_ratio', None)
    ar_in_seg = audience_segmentation_json.get('aspect_ratio') if isinstance(audience_segmentation_json, dict) else None
    try:
        if ar_top:
            audience_segmentation_json['aspect_ratio'] = ar_top
        elif ar_in_seg:
            # already present; keep as-is
            pass
    except Exception:
        pass

    study = Study(
        id=uuid4(),
        title=payload.title,
        background=payload.background,
        language=payload.language,
        main_question=payload.main_question,
        orientation_text=payload.orientation_text,
        study_type=payload.study_type,  # enum compatible
        background_image_url=payload.background_image_url,
        rating_scale=payload.rating_scale.model_dump(),
        audience_segmentation=audience_segmentation_json,
        tasks=None,
        creator_id=creator_id,
        project_id=getattr(payload, 'project_id', None),
        status='draft',
        share_token=share_token,
        share_url=None,  # Will be set after study.id is available
        total_responses=0,
        completed_responses=0,
        abandoned_responses=0,
        phase_order=payload.phase_order,
        toggle_shuffle=payload.toggle_shuffle,
        last_step=payload.last_step or 1,
        product_keys=[k.model_dump() for k in payload.product_keys] if getattr(payload, 'product_keys', None) else None,
        product_id=getattr(payload, 'product_id', None),
    )
    db.add(study)
    # UUID is already assigned above; avoid early flush to reduce DB round-trips
    study.share_url = _build_share_url(base_url_for_share, str(study.id))
    # Ensure parent row exists before inserting FK children in bulk
    db.flush()

    # Children (optimize by avoiding intermediate flushes and using bulk saves)
    with db.no_autoflush:
        if payload.study_type in ('grid', 'text', 'hybrid') and payload.elements:
            # Create categories first if provided
            category_map = {}  # category_id -> category_uuid
            if payload.categories:
                new_categories: List[StudyCategory] = []
                seen_category_ids = set()
                for cat in payload.categories:
                    if cat.category_id in seen_category_ids:
                        raise HTTPException(status_code=409, detail=f"Duplicate category_id: {cat.category_id}")
                    seen_category_ids.add(cat.category_id)
                    category_uuid = uuid4()
                    category_map[cat.category_id] = category_uuid
                    new_categories.append(StudyCategory(
                        id=category_uuid,
                        study_id=study.id,
                        category_id=cat.category_id,  # Now using UUID directly
                        name=cat.name,
                        order=cat.order,
                        phase_type=cat.phase_type
                    ))
                if new_categories:
                    db.bulk_save_objects(new_categories)
                    db.flush()  # Ensure categories exist before linking elements
            
            # Ensure element_id uniqueness within the study
            seen_ids = set()
            new_elements: List[StudyElement] = []
            for elem in payload.elements:
                if elem.element_id in seen_ids:
                    raise HTTPException(status_code=409, detail=f"Duplicate element_id: {elem.element_id}")
                seen_ids.add(elem.element_id)
                
                # Validate category_id exists
                if elem.category_id not in category_map:
                    raise HTTPException(status_code=400, detail=f"Category {elem.category_id} not found in provided categories")
                
                content_url, public_id = _maybe_upload_data_url_to_cloudinary(elem.content)
                new_elements.append(StudyElement(
                    id=uuid4(),
                    study_id=study.id,
                    category_id=category_map[elem.category_id],
                    element_id=elem.element_id,
                    name=elem.name,
                    description=elem.description,
                    element_type=elem.element_type,
                    content=content_url or elem.content,
                    alt_text=elem.alt_text,
                    cloudinary_public_id=public_id
                ))
            if new_elements:
                db.bulk_save_objects(new_elements)

        if payload.study_type == 'layer' and payload.study_layers:
            # Each layer with images
            seen_layer_ids = set()
            new_layers: List[StudyLayer] = []
            for layer in payload.study_layers:
                if layer.layer_id in seen_layer_ids:
                    raise HTTPException(status_code=409, detail=f"Duplicate layer_id: {layer.layer_id}")
                seen_layer_ids.add(layer.layer_id)
                new_layers.append(StudyLayer(
                    id=uuid4(),
                    study_id=study.id,
                    layer_id=layer.layer_id,
                    name=layer.name,
                    description=layer.description,
                    layer_type=layer.layer_type,
                    z_index=layer.z_index,
                    order=layer.order,
                    transform=layer.transform.model_dump() if getattr(layer, 'transform', None) else None
                ))
            if new_layers:
                db.bulk_save_objects(new_layers)
                # Persist layers before inserting their images to satisfy FKs
                db.flush()

            # Build images for all layers without per-layer flushes (ids are preassigned)
            new_images: List[LayerImage] = []
            for layer_row, layer in zip(new_layers, payload.study_layers or []):
                seen_image_ids = set()
                for img in layer.images or []:
                    if img.image_id in seen_image_ids:
                        raise HTTPException(status_code=409, detail=f"Duplicate image_id in layer {layer.layer_id}: {img.image_id}")
                    seen_image_ids.add(img.image_id)
                    url, public_id = _maybe_upload_data_url_to_cloudinary(img.url)
                    new_images.append(LayerImage(
                        id=uuid4(),
                        layer_id=layer_row.id,
                        image_id=img.image_id,
                        name=img.name,
                        url=url or img.url,
                        alt_text=img.alt_text,
                        order=img.order,
                        cloudinary_public_id=public_id,
                        config=img.config
                    ))
            if new_images:
                db.bulk_save_objects(new_images)

    # Handle classification questions (for both grid and layer studies)
    # For Unilever (special creator) studies, inject "Do you like this fragrance?" with order=0 so it appears first.
    creator_email = ""
    try:
        from app.models.user_model import User as UserModel
        row = db.execute(select(UserModel.email).where(UserModel.id == creator_id)).first()
        if row:
            creator_email = (row[0] or "") if hasattr(row, "__getitem__") else (getattr(row, "email", None) or "")
    except Exception:
        pass
    is_special_creator = is_unilever_domain(creator_email)

    if payload.classification_questions or is_special_creator:
        seen_question_ids = set()
        new_questions: List[StudyClassificationQuestion] = []

        if is_special_creator:
            new_questions.append(StudyClassificationQuestion(
                id=uuid4(),
                study_id=study.id,
                question_id=FRAGRANCE_QUESTION_ID,
                question_text=FRAGRANCE_QUESTION_TEXT,
                question_type="multiple_choice",
                is_required="Y",
                order=FRAGRANCE_ORDER,
                answer_options=FRAGRANCE_ANSWER_OPTIONS,
                config={"system": True},
            ))
            seen_question_ids.add(FRAGRANCE_QUESTION_ID)

        if payload.classification_questions:
            for question in payload.classification_questions:
                if question.question_id == FRAGRANCE_QUESTION_ID:
                    continue
                if question.question_id in seen_question_ids:
                    raise HTTPException(status_code=409, detail=f"Duplicate question_id: {question.question_id}")
                seen_question_ids.add(question.question_id)
                order_offset = 1 if is_special_creator else 0
                answer_options_json = [option.model_dump() for option in question.answer_options] if question.answer_options else None
                new_questions.append(StudyClassificationQuestion(
                    id=uuid4(),
                    study_id=study.id,
                    question_id=question.question_id,
                    question_text=question.question_text,
                    question_type=question.question_type,
                    is_required='Y' if question.is_required else 'N',
                    order=question.order + order_offset,
                    answer_options=answer_options_json,
                    config=question.config
                ))
        if new_questions:
            db.bulk_save_objects(new_questions)

    db.commit()
    db.refresh(study)
    
    # If study is part of a project, automatically add all project members to this study
    if study.project_id:
        from app.services.project_member_service import project_member_service
        project_member_service.sync_new_study_to_project_members(db, study.id, study.project_id)
        db.commit()
    
    return study

def create_study_minimal(
    db: Session,
    creator_id: UUID,
    title: str,
    background: str,
    language: str = 'en',
    last_step: int = 1,
    project_id: Optional[UUID] = None,
    product_keys: Optional[List[Dict[str, Any]]] = None,
    product_id: Optional[str] = None,
) -> UUID:
    """
    Ultra-fast study creation with only essential fields.
    Creates a draft study with minimal database operations.
    Returns only the study ID for maximum performance.

    Optimizations:
    - Single database INSERT with no SELECT
    - No ORM object creation overhead
    - No relationship loading
    - Returns UUID directly (no model conversion)
    """
    from sqlalchemy import insert

    share_token = _generate_share_token()
    study_id = uuid4()

    # Use raw SQL insert for maximum speed - bypasses ORM overhead
    stmt = insert(Study).values(
        id=study_id,
        title=title,
        background=background,
        language=language,
        main_question="",  # Will be filled in later
        orientation_text="",  # Will be filled in later
        study_type='grid',  # Default to grid, can be updated later
        rating_scale={
            "min_value": 1,
            "max_value": 5,
            "min_label": "",
            "max_label": ""
        },
        audience_segmentation={},
        creator_id=creator_id,
        project_id=project_id,
        status='draft',
        share_token=share_token,
        share_url=_build_share_url(settings.BASE_URL, str(study_id)),
        toggle_shuffle=False,
        last_step=last_step,
        product_keys=product_keys,
        product_id=product_id,
    )

    db.execute(stmt)
    db.commit()

    # If study is part of a project, automatically add all project members
    if project_id:
        from app.services.project_member_service import project_member_service
        project_member_service.sync_new_study_to_project_members(db, study_id, project_id)
        db.commit()

    # Return UUID directly - no ORM object needed
    return study_id

def get_study(db: Session, study_id: UUID, owner_id: UUID) -> Study:
    return _load_owned_study(db, study_id, owner_id, for_update=False)


def _next_copy_title(db: Session, source_title: str, creator_id: UUID) -> str:
    """
    Compute title for a study copy: "{base_name} (copy 1)", "{base_name} (copy 2)", etc.
    Base name is derived from source_title (strips "Copy of " / " - N" or " (copy N)" if present).
    """
    copy_prefix = "Copy of "
    if source_title.startswith(copy_prefix):
        base = source_title[len(copy_prefix):].strip()
        base = re.sub(r"\s+-\s+\d+$", "", base).strip()
    else:
        base = source_title
    # Strip trailing " (copy N)" so we get the same base when copying a copy
    base = re.sub(r"\s+\(copy\s+\d+\)$", "", base, flags=re.IGNORECASE).strip()
    base_name = base or "Study"

    prefix = base_name + " (copy "
    # Escape LIKE special chars in prefix so we only match literal " (copy "
    prefix_escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    stmt = select(Study.title).where(
        Study.creator_id == creator_id,
        Study.title.like(prefix_escaped + "%", escape="\\"),
    )
    titles = db.execute(stmt).scalars().all()
    max_n = 0
    for title in titles:
        m = re.match(r"^" + re.escape(base_name) + r"\s+\(copy\s+(\d+)\)\s*$", title, re.IGNORECASE)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{base_name} (copy {max_n + 1})"


def copy_study(db: Session, study_id: UUID, user_id: UUID, project_id: Optional[UUID] = None) -> Study:
    """
    Create an exact copy of a study (layer, grid, hybrid, text config) without tasks.
    The copy is in draft with last_step=6 so the user can generate tasks.
    Any user with access (creator or member) can copy any study (active, draft, completed).
    Copy title: "{name} (copy 1)", "{name} (copy 2)", etc.
    project_id is not copied from the source; pass project_id in the request payload to associate the copy with a project.
    """
    source = get_study(db, study_id, user_id)

    copy_title = _next_copy_title(db, source.title, user_id)

    new_id = uuid4()
    share_token = _generate_share_token()
    new_study = Study(
        id=new_id,
        title=copy_title,
        background=source.background,
        language=source.language,
        main_question=source.main_question,
        orientation_text=source.orientation_text,
        study_type=source.study_type,
        background_image_url=source.background_image_url,
        rating_scale=source.rating_scale or {},
        audience_segmentation=source.audience_segmentation or {},
        phase_order=source.phase_order,
        toggle_shuffle=source.toggle_shuffle or False,
        tasks=None,
        creator_id=user_id,
        project_id=project_id,
        status='draft',
        share_token=share_token,
        share_url=_build_share_url(settings.BASE_URL, str(new_id)),
        launched_at=None,
        completed_at=None,
        total_responses=0,
        completed_responses=0,
        abandoned_responses=0,
        last_step=6,
        jobid=None,
        # Copy product keys with names only; use percentage=0 so response schema validates
        product_keys=(
            [{"name": k.get("name"), "percentage": 0} for k in (source.product_keys or []) if isinstance(k, dict) and k.get("name")]
            if source.product_keys else None
        ),
        product_id=None,
    )
    db.add(new_study)
    db.flush()

    # Map old category id -> new category (for elements)
    category_map = {}
    if source.categories:
        new_categories: List[StudyCategory] = []
        for cat in source.categories:
            new_cat = StudyCategory(
                id=uuid4(),
                study_id=new_study.id,
                category_id=cat.category_id,
                name=cat.name,
                order=cat.order,
                phase_type=cat.phase_type,
            )
            category_map[cat.id] = new_cat
            new_categories.append(new_cat)
        db.bulk_save_objects(new_categories)
        db.flush()

    if source.elements:
        new_elements: List[StudyElement] = []
        for elem in source.elements:
            new_elements.append(StudyElement(
                id=uuid4(),
                study_id=new_study.id,
                category_id=category_map[elem.category_id].id,
                element_id=elem.element_id,
                name=elem.name,
                description=elem.description,
                element_type=elem.element_type,
                content=elem.content,
                cloudinary_public_id=elem.cloudinary_public_id,
                alt_text=elem.alt_text,
            ))
        db.bulk_save_objects(new_elements)

    if source.layers:
        new_layers: List[StudyLayer] = []
        for layer in source.layers:
            new_layer = StudyLayer(
                id=uuid4(),
                study_id=new_study.id,
                layer_id=layer.layer_id,
                name=layer.name,
                description=layer.description,
                layer_type=layer.layer_type,
                z_index=layer.z_index,
                order=layer.order,
                transform=layer.transform,
            )
            new_layers.append(new_layer)
        db.bulk_save_objects(new_layers)
        db.flush()

        new_images: List[LayerImage] = []
        for old_layer, new_layer in zip(source.layers, new_layers):
            for img in old_layer.images or []:
                new_images.append(LayerImage(
                    id=uuid4(),
                    layer_id=new_layer.id,
                    image_id=img.image_id,
                    name=img.name,
                    url=img.url,
                    cloudinary_public_id=img.cloudinary_public_id,
                    alt_text=img.alt_text,
                    order=img.order,
                    config=img.config,
                ))
        if new_images:
            db.bulk_save_objects(new_images)

    if source.classification_questions:
        new_questions: List[StudyClassificationQuestion] = []
        for q in source.classification_questions:
            new_questions.append(StudyClassificationQuestion(
                id=uuid4(),
                study_id=new_study.id,
                question_id=q.question_id,
                question_text=q.question_text,
                question_type=q.question_type,
                is_required=q.is_required,
                order=q.order,
                answer_options=q.answer_options,
                config=q.config,
            ))
        db.bulk_save_objects(new_questions)

    db.commit()
    db.refresh(new_study)

    if new_study.project_id:
        from app.services.project_member_service import project_member_service
        project_member_service.sync_new_study_to_project_members(db, new_study.id, new_study.project_id)
        db.commit()

    return new_study


def build_study_data_for_analysis(study: Study) -> Dict[str, Any]:
    """
    Build the study_data dict expected by StudyAnalysisService (categories, elements, classification_questions).
    Handles layer vs grid/text/hybrid study types. Study must have categories, elements, layers, classification_questions loaded.
    """
    study_data: Dict[str, Any] = {
        "title": study.title,
        "study_type": study.study_type,
        "background": getattr(study, "background_image_url", None) or "",
        "language": study.language,
        "launched_at": study.created_at.isoformat() if study.created_at else "",
        "categories": [],
        "elements": [],
        "classification_questions": [],
    }
    if str(study.study_type) == "layer":
        sorted_layers = sorted(study.layers or [], key=lambda x: x.order)
        for layer in sorted_layers:
            cat_id = str(layer.layer_id)
            study_data["categories"].append({"id": cat_id, "name": layer.name, "order": layer.order})
            for img in sorted(layer.images or [], key=lambda x: x.order):
                study_data["elements"].append({
                    "id": str(img.image_id),
                    "name": img.name,
                    "content": img.url,
                    "category_id": cat_id,
                    "category": {"name": layer.name, "order": layer.order},
                })
    else:
        elements_by_cat = {}
        for el in study.elements or []:
            cid = getattr(el, "category_id", None)
            if cid not in elements_by_cat:
                elements_by_cat[cid] = []
            elements_by_cat[cid].append(el)
        for cat in study.categories or []:
            study_data["categories"].append({
                "id": str(cat.id),
                "name": cat.name,
                "order": cat.order,
            })
            for el in elements_by_cat.get(cat.id, []):
                study_data["elements"].append({
                    "id": str(el.id),
                    "name": el.name,
                    "content": el.content,
                    "category_id": str(cat.id),
                    "category": {"name": cat.name, "order": cat.order},
                })
    for q in study.classification_questions or []:
        study_data["classification_questions"].append({
            "question_id": q.question_id,
            "question_text": q.question_text,
            "answer_options": q.answer_options,
        })
    return study_data


def get_study_exists(db: Session, study_id: UUID, owner_id: UUID) -> bool:
    """Lightweight check if study exists and is owned by user."""
    stmt = select(Study.id).where(Study.id == study_id, Study.creator_id == owner_id)
    return db.scalars(stmt).first() is not None

def check_study_access(db: Session, study_id: UUID, user_id: UUID) -> None:
    """
    Verify if a user has access to a study (Creator, Project Owner, or Member).
    Raises HTTPException if access is denied.
    Does NOT load full study data.
    """
    from app.models.project_model import Project

    # Check study existence and ownership (Study Creator OR Project Creator)
    stmt = (
        select(Study.creator_id, Project.creator_id)
        .outerjoin(Project, Study.project_id == Project.id)
        .where(Study.id == study_id)
    )
    result = db.execute(stmt).first()
    
    if not result:
        # Study doesn't exist
        raise HTTPException(status_code=404, detail="Study not found")
        
    study_creator_id, project_creator_id = result
    
    # 1. Access allowed if user is Study Creator
    if study_creator_id == user_id:
        return

    # 2. Access allowed if user is Project Creator (Owner)
    if project_creator_id == user_id:
        return

    # 3. Check if member
    stmt_member = select(StudyMember.id).where(
        StudyMember.study_id == study_id,
        StudyMember.user_id == user_id
    )
    if not db.scalar(stmt_member):
        raise HTTPException(status_code=403, detail="Access denied")

def get_study_public(db: Session, study_id: UUID) -> Optional[Study]:
    """
    Get study information for public access (no authentication required).
    Only returns studies that are active and have a share_token.
    """
    stmt = (
        select(Study)
        .options(
            selectinload(Study.elements),
            selectinload(Study.layers).selectinload(StudyLayer.images),
            selectinload(Study.classification_questions),
        )
        .where(
            Study.id == study_id,
            Study.status == 'active',
            Study.share_token.isnot(None),
            Study.share_token != ''
        )
    )
    return db.scalars(stmt).first()

def get_study_public_minimal(db: Session, study_id: UUID) -> Optional[StudyPublicMinimal]:
    """Lightweight public fetch: only title, type, respondents target."""
    row = db.execute(
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.audience_segmentation,
            Study.status,
            Study.share_token,
            Study.orientation_text,
            Study.language,
            Study.product_keys,
            Study.product_id,
        ).where(Study.id == study_id)
    ).first()
    if not row:
        return None
    if row.status != 'active' or not row.share_token:
        return None
    respondents_target = 0
    try:
        seg = row.audience_segmentation or {}
        respondents_target = int(seg.get('number_of_respondents') or 0)
    except Exception:
        respondents_target = 0
    
    # Get number of tasks per respondent using TaskService
    tasks_per_respondent = 0
    try:
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        tasks_per_respondent = task_service.get_tasks_per_respondent(study_id)
    except Exception:
        tasks_per_respondent = 0
    
    return StudyPublicMinimal(
        id=row.id,
        title=row.title,
        study_type=row.study_type,
        respondents_target=respondents_target,
        tasks_per_respondent=tasks_per_respondent,
        status=row.status,
        orientation_text=row.orientation_text,
        language=row.language,
        product_keys=row.product_keys,
        product_id=row.product_id,
    )

def get_study_public_with_status_check(db: Session, study_id: UUID) -> Dict[str, Any]:
    """Get study with status checking and appropriate messaging."""
    from app.models.user_model import User as UserModel
    row = db.execute(
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.audience_segmentation,
            Study.status,
            Study.share_token,
            Study.orientation_text,
            Study.language,
            UserModel.email.label("creator_email")
        ).join(UserModel, UserModel.id == Study.creator_id)
        .where(Study.id == study_id)
    ).first()
    
    if not row:
        return {
            "error": "Study not found",
            "message": "The study you are looking for does not exist."
        }
    
    # Handle different statuses
    if row.status in ['draft', 'paused']:
        return {
            "error": "Study is paused",
            "message": "This study is currently paused. Please ask the owner to activate the study.",
            "status": row.status
        }
    
    if row.status == 'completed':
        return {
            "error": "Study is completed",
            "message": "This study has been completed.",
            "status": row.status
        }
    
    if row.status != 'active' or not row.share_token:
        return {
            "error": "Study not accessible",
            "message": "This study is not currently accessible.",
            "status": row.status
        }
    
    # Study is active and accessible
    respondents_target = 0
    try:
        seg = row.audience_segmentation or {}
        respondents_target = int(seg.get('number_of_respondents') or 0)
    except Exception:
        respondents_target = 0
    
    # Get number of tasks per respondent using TaskService
    tasks_per_respondent = 0
    try:
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        tasks_per_respondent = task_service.get_tasks_per_respondent(study_id)
    except Exception:
        tasks_per_respondent = 0
    
    require_panelist = is_unilever_domain(row.creator_email or "")
    return {
        "id": str(row.id),
        "title": row.title,
        "study_type": row.study_type,
        "respondents_target": respondents_target,
        "tasks_per_respondent": tasks_per_respondent,
        "status": row.status,
        "orientation_text": row.orientation_text,
        "language": row.language,
        "creator_email": row.creator_email,
        "require_panelist_selection": require_panelist,
        "show_fragrance_question": require_panelist,
    }


def get_study_public_preview(db: Session, study_id: UUID) -> Dict[str, Any]:
    """
    Get study information for public access, bypassing status checks.
    Used for previewing studies that are in draft or paused status.
    """
    from app.models.user_model import User as UserModel
    row = db.execute(
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.audience_segmentation,
            Study.status,
            Study.share_token,
            Study.orientation_text,
            Study.language,
            UserModel.email.label("creator_email")
        ).join(UserModel, UserModel.id == Study.creator_id)
        .where(Study.id == study_id)
    ).first()
    
    if not row:
        return {
            "error": "Study not found",
            "message": "The study you are looking for does not exist."
        }
    
    # Bypass all status checks
    
    respondents_target = 0
    try:
        seg = row.audience_segmentation or {}
        respondents_target = int(seg.get('number_of_respondents') or 0)
    except Exception:
        respondents_target = 0
    
    # Get number of tasks per respondent using TaskService
    tasks_per_respondent = 0
    try:
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        tasks_per_respondent = task_service.get_tasks_per_respondent(study_id)
    except Exception:
        tasks_per_respondent = 0
    
    require_panelist = is_unilever_domain(row.creator_email or "")
    return {
        "id": str(row.id),
        "title": row.title,
        "study_type": row.study_type,
        "respondents_target": respondents_target,
        "tasks_per_respondent": tasks_per_respondent,
        "status": row.status,
        "orientation_text": row.orientation_text,
        "language": row.language,
        "creator_email": row.creator_email,
        "require_panelist_selection": require_panelist,
        "show_fragrance_question": require_panelist,
    }


def get_study_share_details(db: Session, study_id: UUID) -> Optional[Dict[str, Any]]:
    """Lightweight fetch of share URL and basic status info for a study."""
    row = db.execute(
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.status,
            Study.share_url,
            Study.live_participants_paid,
            UserBillingProfile.plan,
            UserBillingProfile.subscription_status,
        )
        .outerjoin(UserBillingProfile, UserBillingProfile.user_id == Study.creator_id)
        .where(Study.id == study_id)
    ).first()
    if not row:
        return None
    raw_plan = getattr(row.plan, "value", row.plan) or "free"
    raw_status = getattr(row.subscription_status, "value", row.subscription_status) or "none"
    if raw_plan == "enterprise":
        user_plan = "enterprise"
    elif raw_plan == "pro" and raw_status in ("active", "trialing"):
        user_plan = "pro"
    else:
        user_plan = "free"
    return {
        "id": row.id,
        "title": row.title,
        "study_type": row.study_type,
        "status": row.status,
        "share_url": row.share_url,
        "user_plan": user_plan,
        "live_participants_paid": bool(row.live_participants_paid),
    }

def list_studies(
    db: Session,
    owner_id: UUID,
    *,
    status_filter: Optional[StudyStatus] = None,
    page: int = 1,
    per_page: int = 10
) -> Tuple[List, int]:
    """
    Study listing optimized for dashboard cards.
    Uses live response counts from study_responses with a paged aggregate:
    1) fetch page of studies
    2) fetch grouped counts for only those study IDs

    This keeps counts accurate while avoiding heavy per-row correlated subqueries.
    """
    # Select only fields needed by StudyListItem; counters are added via grouped aggregate query
    base_stmt = (
        select(
            Study.id,
            Study.title,
            Study.study_type,
            Study.status,
            Study.created_at,
            Study.last_step,
            Study.jobid,
            Study.project_id,
            Study.creator_id,
            Study.product_keys,
            Study.product_id,
            Study.audience_segmentation,  # Needed for respondents_target extraction
            Study.live_participants_paid,
            Study.live_participants_unlocked,
        )
        .outerjoin(StudyMember, and_(StudyMember.study_id == Study.id, StudyMember.user_id == owner_id))
        .where(
            or_(
                Study.creator_id == owner_id,
                StudyMember.user_id == owner_id
            )
        )
    )
    
    count_stmt = (
        select(func.count(Study.id.distinct()))
        .select_from(Study)
        .outerjoin(StudyMember, and_(StudyMember.study_id == Study.id, StudyMember.user_id == owner_id))
        .where(
            or_(
                Study.creator_id == owner_id,
                StudyMember.user_id == owner_id
            )
        )
    )
    
    if status_filter:
        base_stmt = base_stmt.where(Study.status == status_filter)
        count_stmt = count_stmt.where(Study.status == status_filter)

    total = db.scalar(count_stmt) or 0
    rows = db.execute(
        base_stmt
        .order_by(desc(Study.created_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    if not rows:
        return [], int(total)

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

    counts_by_study: Dict[UUID, Dict[str, int]] = {
        c.study_id: {
            "total_responses_calc": int(c.total_responses_calc or 0),
            "completed_responses_calc": int(c.completed_responses_calc or 0),
            "abandoned_responses_calc": int(c.abandoned_responses_calc or 0),
        }
        for c in count_rows
    }

    enriched_rows: List[Any] = []
    for row in rows:
        row_dict = dict(row._mapping)
        counts = counts_by_study.get(
            row.id,
            {
                "total_responses_calc": 0,
                "completed_responses_calc": 0,
                "abandoned_responses_calc": 0,
            },
        )
        row_dict.update(counts)
        enriched_rows.append(SimpleNamespace(**row_dict))

    return enriched_rows, int(total)

def update_study(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    payload: StudyUpdate
) -> Study:
    logger.info(f"update_study called for study {study_id} by user {owner_id}")
    logger.info(f"Payload: {payload.model_dump(exclude_none=True)}")

    study = _load_owned_study(db, study_id, owner_id, for_update=True)
    # Allow editing even when active, but only for users with edit permission.
    # Project viewers cannot edit even if they created the study.
    if study.status == 'active' and not _get_effective_edit_permission(db, study, owner_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the study has already been launched by the admin please go to home page"
        )

    # Handle status change if provided (align with change_status rules)
    if payload.status is not None:
        logger.info(f"Updating status to: {payload.status}")
        if payload.status not in ['draft', 'active', 'paused', 'completed']:
            logger.error(f"Invalid status: {payload.status}")
            raise HTTPException(status_code=400, detail="Invalid status.")
        # transitions timestamps
        if payload.status == 'active' and (study.launched_at is None):
            study.launched_at = datetime.utcnow()
        if payload.status == 'completed':
            study.completed_at = datetime.utcnow()
        study.status = payload.status
        # Allow status changes without task validation

    # Apply scalar updates
    if payload.title is not None:
        study.title = payload.title
    if payload.background is not None:
        study.background = payload.background
    if payload.language is not None:
        study.language = payload.language
    if payload.main_question is not None:
        study.main_question = payload.main_question
    if payload.orientation_text is not None:
        study.orientation_text = payload.orientation_text
    if payload.study_type is not None:
        study.study_type = payload.study_type
    if payload.background_image_url is not None:
        study.background_image_url = payload.background_image_url
    if payload.last_step is not None:
        # Only update last_step if the new value is greater (forward progress only)
        current_step = getattr(study, 'last_step', 1) or 1
        if payload.last_step > current_step:
            study.last_step = payload.last_step
    if payload.rating_scale is not None:
        _validate_rating_scale(payload.rating_scale.model_dump())
        study.rating_scale = payload.rating_scale.model_dump()
    if payload.audience_segmentation is not None:
        # Merge with existing to preserve aspect_ratio when omitted
        existing_seg = study.audience_segmentation or {}
        incoming_seg = payload.audience_segmentation.model_dump(exclude_none=True)
        merged_seg = {**existing_seg, **incoming_seg}
        # If top-level aspect_ratio is provided, override; otherwise preserve existing
        ar_top = getattr(payload, 'aspect_ratio', None)
        if ar_top:
            merged_seg['aspect_ratio'] = ar_top
        elif 'aspect_ratio' not in incoming_seg and 'aspect_ratio' in existing_seg:
            # Preserve existing aspect_ratio if not in incoming payload
            merged_seg['aspect_ratio'] = existing_seg['aspect_ratio']
        study.audience_segmentation = merged_seg

    if payload.phase_order is not None:
        study.phase_order = payload.phase_order

    if payload.toggle_shuffle is not None:
        study.toggle_shuffle = payload.toggle_shuffle

    if payload.project_id is not None:
        study.project_id = payload.project_id

    if payload.product_keys is not None:
        study.product_keys = [k.model_dump() for k in payload.product_keys]

    if payload.product_id is not None:
        study.product_id = payload.product_id

    # Replace children collections if provided
    if payload.elements is not None:
        # Only valid for grid, text, and hybrid
        if study.study_type not in ('grid', 'text', 'hybrid'):
            raise HTTPException(status_code=400, detail="elements can only be set for grid, text, and hybrid studies.")

        # Handle categories first - they must be provided when updating elements
        category_map = {}  # category_id -> category_uuid
        if hasattr(payload, 'categories') and payload.categories:
            # Delete old categories (cascade will handle elements)
            db.query(StudyCategory).filter(StudyCategory.study_id == study.id).delete()
            db.flush()

            # Create new categories
            seen_category_ids = set()
            for cat in payload.categories:
                if cat.category_id in seen_category_ids:
                    raise HTTPException(status_code=409, detail=f"Duplicate category_id: {cat.category_id}")
                seen_category_ids.add(cat.category_id)
                category_uuid = uuid4()
                category_map[cat.category_id] = category_uuid
                db.add(StudyCategory(
                    id=category_uuid,
                    study_id=study.id,
                    category_id=cat.category_id,
                    name=cat.name,
                    order=cat.order,
                    phase_type=cat.phase_type
                ))
            db.flush()
        else:
            # If categories not provided in payload, load existing categories
            existing_categories = db.scalars(
                select(StudyCategory).where(StudyCategory.study_id == study.id)
            ).all()
            for cat in existing_categories:
                category_map[cat.category_id] = cat.id

        # Clear and re-add elements
        # Delete old assets best-effort
        old_elements_seq = db.scalars(
            select(StudyElement).where(StudyElement.study_id == study.id)
        ).all()
        old_elements: List[StudyElement] = list(old_elements_seq)
        for oe in old_elements:
            if oe.cloudinary_public_id is not None and oe.cloudinary_public_id != "":
                delete_public_id(str(oe.cloudinary_public_id))
        db.query(StudyElement).filter(StudyElement.study_id == study.id).delete()
        db.flush()

        seen_ids = set()
        for elem in payload.elements:
            if elem.element_id in seen_ids:
                raise HTTPException(status_code=409, detail=f"Duplicate element_id: {elem.element_id}")
            seen_ids.add(elem.element_id)

            # Validate category_id exists
            if elem.category_id not in category_map:
                raise HTTPException(status_code=400, detail=f"Category {elem.category_id} not found. Please provide categories when updating elements.")

            content_url, public_id = _maybe_upload_data_url_to_cloudinary(elem.content)
            db.add(StudyElement(
                id=uuid4(),
                study_id=study.id,
                category_id=category_map[elem.category_id],
                element_id=elem.element_id,
                name=elem.name,
                description=elem.description,
                element_type=elem.element_type,
                content=content_url or elem.content,
                alt_text=elem.alt_text,
                cloudinary_public_id=public_id
            ))

    if payload.study_layers is not None:
        # Only valid for layer
        if study.study_type != 'layer':
            raise HTTPException(status_code=400, detail="study_layers can only be set for layer studies.")
        # Clear and re-add (delete assets best-effort)
        old_layer_images_seq = db.scalars(
            select(LayerImage).join(StudyLayer, StudyLayer.id == LayerImage.layer_id).where(StudyLayer.study_id == study.id)
        ).all()
        old_layer_images: List[LayerImage] = list(old_layer_images_seq)
        for oi in old_layer_images:
            if oi.cloudinary_public_id is not None and oi.cloudinary_public_id != "":
                delete_public_id(str(oi.cloudinary_public_id))
        subq = db.query(StudyLayer.id).filter(StudyLayer.study_id == study.id).subquery()
        db.query(LayerImage).filter(LayerImage.layer_id.in_(select(subq.c.id))).delete(synchronize_session=False)
        db.query(StudyLayer).filter(StudyLayer.study_id == study.id).delete()
        db.flush()

        seen_layer_ids = set()
        for layer in payload.study_layers:
            if layer.layer_id in seen_layer_ids:
                raise HTTPException(status_code=409, detail=f"Duplicate layer_id: {layer.layer_id}")
            seen_layer_ids.add(layer.layer_id)

            layer_row = StudyLayer(
                id=uuid4(),
                study_id=study.id,
                layer_id=layer.layer_id,
                name=layer.name,
                description=layer.description,
                layer_type=layer.layer_type,
                z_index=layer.z_index,
                order=layer.order,
                transform=layer.transform.model_dump() if getattr(layer, 'transform', None) else None
            )
            db.add(layer_row)
            db.flush()

            seen_image_ids = set()
            for img in layer.images or []:
                if img.image_id in seen_image_ids:
                    raise HTTPException(status_code=409, detail=f"Duplicate image_id in layer {layer.layer_id}: {img.image_id}")
                seen_image_ids.add(img.image_id)
                db.add(LayerImage(
                    id=uuid4(),
                    layer_id=layer_row.id,
                    image_id=img.image_id,
                    name=img.name,
                    url=img.url,
                    alt_text=img.alt_text,
                    order=img.order,
                    config=img.config
                ))

    # Handle classification questions updates
    if payload.classification_questions is not None:
        # Clear existing classification questions
        db.query(StudyClassificationQuestion).filter(StudyClassificationQuestion.study_id == study.id).delete()
        db.flush()

        # For Unilever (special creator) studies, re-inject fragrance question with order=0
        creator_email_update = ""
        try:
            from app.models.user_model import User as UserModel
            creator_row = db.execute(select(UserModel.email).where(UserModel.id == study.creator_id)).first()
            if creator_row:
                creator_email_update = (creator_row[0] or "") if hasattr(creator_row, "__getitem__") else (getattr(creator_row, "email", None) or "")
        except Exception:
            pass
        is_special_update = is_unilever_domain(creator_email_update)

        seen_question_ids = set()
        if is_special_update:
            db.add(StudyClassificationQuestion(
                id=uuid4(),
                study_id=study.id,
                question_id=FRAGRANCE_QUESTION_ID,
                question_text=FRAGRANCE_QUESTION_TEXT,
                question_type="multiple_choice",
                is_required="Y",
                order=FRAGRANCE_ORDER,
                answer_options=FRAGRANCE_ANSWER_OPTIONS,
                config={"system": True},
            ))
            seen_question_ids.add(FRAGRANCE_QUESTION_ID)

        for question in payload.classification_questions:
            if question.question_id == FRAGRANCE_QUESTION_ID:
                continue
            if question.question_id in seen_question_ids:
                raise HTTPException(status_code=409, detail=f"Duplicate question_id: {question.question_id}")
            seen_question_ids.add(question.question_id)
            order_offset = 1 if is_special_update else 0
            answer_options_json = None
            if question.answer_options:
                answer_options_json = [option.model_dump() for option in question.answer_options]
            db.add(StudyClassificationQuestion(
                id=uuid4(),
                study_id=study.id,
                question_id=question.question_id,
                question_text=question.question_text,
                question_type=question.question_type,
                is_required='Y' if question.is_required else 'N',
                order=question.order + order_offset,
                answer_options=answer_options_json,
                config=question.config
            ))

    db.commit()
    db.refresh(study)
    invalidate_study_cache(study_id)
    return study

def update_study_fast(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    payload: StudyUpdate
) -> Study:
    """Optimized version of update_study that uses minimal loading for simple updates."""
    logger.info(f"update_study_fast called for study {study_id} by user {owner_id}")
    logger.info(f"Payload: {payload.model_dump(exclude_none=True)}")
    
    # Check if we need full loading (for classification_questions updates)
    needs_full_loading = payload.classification_questions is not None
    
    if needs_full_loading:
        study = _load_owned_study(db, study_id, owner_id, for_update=True)
    else:
        study = _load_owned_study_minimal(db, study_id, owner_id, for_update=True)
    
    # Prevent non-authorized users from editing active/launched studies (respects project-level roles)
    if study.status == 'active' and not _get_effective_edit_permission(db, study, owner_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the study has already been launched by the admin please go to home page"
        )

    # Handle status change if provided (align with change_status rules)
    if payload.status is not None:
        logger.info(f"Updating status to: {payload.status}")
        if payload.status not in ['draft', 'active', 'paused', 'completed']:
            logger.error(f"Invalid status: {payload.status}")
            raise HTTPException(status_code=400, detail="Invalid status.")
        # transitions timestamps
        if payload.status == 'active' and (study.launched_at is None):
            study.launched_at = datetime.utcnow()
        if payload.status == 'completed':
            study.completed_at = datetime.utcnow()
        study.status = payload.status
        # Allow status changes without task validation

    # Apply scalar updates
    if payload.title is not None:
        study.title = payload.title
    if payload.background is not None:
        study.background = payload.background
    if payload.language is not None:
        study.language = payload.language
    if payload.main_question is not None:
        study.main_question = payload.main_question
    if payload.orientation_text is not None:
        study.orientation_text = payload.orientation_text
    if payload.study_type is not None:
        study.study_type = payload.study_type
    if payload.background_image_url is not None:
        study.background_image_url = payload.background_image_url
    if payload.last_step is not None:
        # Only update last_step if the new value is greater (forward progress only)
        current_step = getattr(study, 'last_step', 1) or 1
        if payload.last_step > current_step:
            study.last_step = payload.last_step
    if payload.rating_scale is not None:
        _validate_rating_scale(payload.rating_scale.model_dump())
        study.rating_scale = payload.rating_scale.model_dump()
    if payload.audience_segmentation is not None:
        # Merge with existing to avoid dropping keys like aspect_ratio when omitted
        existing_seg = study.audience_segmentation or {}
        incoming_seg = payload.audience_segmentation.model_dump(exclude_none=True)
        merged_seg = {**existing_seg, **incoming_seg}
        # If top-level aspect_ratio is provided, override; otherwise preserve existing
        ar_top = getattr(payload, 'aspect_ratio', None)
        if ar_top:
            merged_seg['aspect_ratio'] = ar_top
        study.audience_segmentation = merged_seg

    if payload.phase_order is not None:
        study.phase_order = payload.phase_order

    if payload.product_keys is not None:
        study.product_keys = [k.model_dump() for k in payload.product_keys]

    if payload.product_id is not None:
        study.product_id = payload.product_id

    # For fast updates, we only handle simple scalar fields
    # Complex operations (elements, layers, classification_questions) fall back to full loading
    if payload.elements is not None or payload.study_layers is not None:
        # Fall back to full update_study for complex operations
        return update_study(db, study_id, owner_id, payload)

    # Handle classification questions updates (requires full loading)
    if payload.classification_questions is not None:
        # Fall back to full update_study for classification questions
        return update_study(db, study_id, owner_id, payload)

    db.commit()
    db.refresh(study)
    invalidate_study_cache(study_id)
    return study

def update_and_launch_study_fast(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    payload: StudyUpdate
) -> Study:
    """Ultra-optimized function that combines update and launch in a single transaction."""
    # Load study with minimal data for maximum performance
    study = _load_owned_study_minimal(db, study_id, owner_id, for_update=True)
    
    # Prevent non-authorized users from editing active/launched studies (respects project-level roles)
    if study.status == 'active' and not _get_effective_edit_permission(db, study, owner_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the study has already been launched by the admin please go to home page"
        )
    
    # Apply scalar updates
    if payload.title is not None:
        study.title = payload.title
    if payload.background is not None:
        study.background = payload.background
    if payload.language is not None:
        study.language = payload.language
    if payload.main_question is not None:
        study.main_question = payload.main_question
    if payload.orientation_text is not None:
        study.orientation_text = payload.orientation_text
    if payload.background_image_url is not None:
        study.background_image_url = payload.background_image_url
    if payload.rating_scale is not None:
        _validate_rating_scale(payload.rating_scale.model_dump())
        study.rating_scale = payload.rating_scale.model_dump()
    if payload.audience_segmentation is not None:
        # Merge to preserve existing keys (e.g., aspect_ratio) when omitted in update
        existing_seg = study.audience_segmentation or {}
        incoming_seg = payload.audience_segmentation.model_dump(exclude_none=True)
        merged_seg = {**existing_seg, **incoming_seg}
        # If top-level aspect_ratio is provided, override; otherwise preserve existing
        ar_top = getattr(payload, 'aspect_ratio', None)
        if ar_top:
            merged_seg['aspect_ratio'] = ar_top
        elif 'aspect_ratio' not in incoming_seg and 'aspect_ratio' in existing_seg:
            # Explicitly preserve existing aspect_ratio if not in incoming payload
            merged_seg['aspect_ratio'] = existing_seg['aspect_ratio']
        study.audience_segmentation = merged_seg
    
    if payload.phase_order is not None:
        study.phase_order = payload.phase_order

    if payload.product_keys is not None:
        study.product_keys = [k.model_dump() for k in payload.product_keys]

    if payload.product_id is not None:
        study.product_id = payload.product_id

    # Handle complex updates that require full loading
    if payload.elements is not None or payload.study_layers is not None or payload.classification_questions is not None:
        # Fall back to full update_study for complex operations
        updated_study = update_study(db, study_id, owner_id, payload)
        # Then launch the study
        return change_status_fast(db, study_id, owner_id, 'active')

    # Set status to active (launch the study)
    if study.launched_at is None:
        study.launched_at = datetime.utcnow()
        # Update share_url when study is launched (becomes active)
        study.share_url = _build_share_url(None, str(study.id))
    
    study.status = 'active'
    
    # Single commit for both update and launch
    db.commit()
    db.refresh(study)
    invalidate_study_cache(study_id)
    return study

def launch_study_ultra_fast(
    db: Session,
    study_id: UUID,
    owner_id: UUID
) -> Study:
    """Ultra-fast launch-only function - no updates, just launch."""
    # Use direct SQL UPDATE for maximum speed
    from sqlalchemy import update
    from datetime import datetime
    
    now = datetime.utcnow()
    stmt = (
        update(Study)
        .where(Study.id == study_id, Study.creator_id == owner_id)
        .values(
            status='active',
            launched_at=now,
            share_url=_build_share_url(None, str(study_id)),
            updated_at=now
        )
        .returning(Study.id, Study.title, Study.status, Study.launched_at, Study.share_url, Study.updated_at)
    )
    
    result = db.execute(stmt).first()
    if not result:
        raise HTTPException(status_code=404, detail="Study not found or access denied.")
    
    # Create minimal study object for response
    study = Study()
    study.id = result.id
    study.title = result.title
    study.status = result.status
    study.launched_at = result.launched_at
    study.share_url = result.share_url
    study.updated_at = result.updated_at
    
    db.commit()
    invalidate_study_cache(study_id)
    return study

def delete_study(db: Session, study_id: UUID, owner_id: UUID) -> None:
    """Delete a study (non-active only). Uses minimal load for speed; CASCADE removes related rows."""
    study = _load_owned_study_minimal(db, study_id, owner_id, for_update=True)
    if not _get_effective_edit_permission(db, study, owner_id):
        raise HTTPException(status_code=403, detail="You do not have permission to delete this study.")
    if study.status == 'active':
        raise HTTPException(status_code=400, detail="Cannot delete active studies.")
    db.delete(study)
    db.commit()
    invalidate_study_cache(study_id)


def delete_draft_study(db: Session, study_id: UUID, owner_id: UUID) -> None:
    """Delete a study only if it is in draft status. Raises 400 if not draft."""
    study = _load_owned_study_minimal(db, study_id, owner_id, for_update=True)
    if study.status != 'draft':
        raise HTTPException(
            status_code=400,
            detail="Only draft studies can be deleted. Study status is '%s'." % study.status,
        )
    db.delete(study)
    db.commit()
    invalidate_study_cache(study_id)

def change_status(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    new_status: StudyStatus
) -> Study:
    study = _load_owned_study(db, study_id, owner_id, for_update=True)

    if new_status not in ['draft', 'active', 'paused', 'completed']:
        raise HTTPException(status_code=400, detail="Invalid status.")

    # transitions timestamps and share_url update
    if new_status == 'active' and (study.launched_at is None):
        study.launched_at = datetime.utcnow()
        # Update share_url when study is launched (becomes active)
        study.share_url = _build_share_url(None, str(study.id))
    if new_status == 'completed':
        study.completed_at = datetime.utcnow()

    study.status = new_status
    db.commit()
    db.refresh(study)
    invalidate_study_cache(study_id)
    return study

def change_status_fast(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    new_status: StudyStatus
) -> Study:
    """Optimized version of change_status that avoids loading related data."""
    study = _load_owned_study_minimal(db, study_id, owner_id, for_update=True)

    if new_status not in ['draft', 'active', 'paused', 'completed']:
        raise HTTPException(status_code=400, detail="Invalid status.")

    # transitions timestamps and share_url update
    if new_status == 'active' and (study.launched_at is None):
        study.launched_at = datetime.utcnow()
        # Update share_url when study is launched (becomes active)
        study.share_url = _build_share_url(None, str(study.id))
    if new_status == 'completed':
        study.completed_at = datetime.utcnow()

    study.status = new_status
    db.commit()
    db.refresh(study)
    invalidate_study_cache(study_id)
    return study

# ---------- Tasks (Regenerate / Validate) ----------

def regenerate_tasks(
    db: Session,
    study_id: UUID,
    owner_id: UUID,
    *,
    generator: Optional[Dict[str, Any]] = None
) -> RegenerateTasksResponse:
    # Optimized study loading - only load what we need
    study = db.scalars(
        select(Study)
        .where(Study.id == study_id, Study.creator_id == owner_id)
        .with_for_update()
    ).first()

    if not study:
        # Check if user is a member
        stmt_member = (
            select(Study)
            .join(StudyMember, StudyMember.study_id == Study.id)
            .where(Study.id == study_id, StudyMember.user_id == owner_id)
            .with_for_update()
        )
        study = db.scalars(stmt_member).first()

    if not study:
        raise HTTPException(status_code=404, detail="Study not found or access denied.")
    
    # Allow task regeneration for all study statuses
    # Note: Regenerating tasks for active studies may affect ongoing participants

    # Default to adapter-backed generators if none provided
    generator = generator or {"grid": generate_grid_tasks, "layer": generate_layer_tasks}

    if 'grid' not in generator or 'layer' not in generator:
        raise HTTPException(status_code=500, detail="Task generator not configured.")

    # Cache audience values to avoid repeated dict lookups
    audience = study.audience_segmentation or {}
    number_of_respondents = audience.get('number_of_respondents')
    computed_total: int = 0
    if study.study_type in ('grid', 'text'):
        # Cache constants to avoid repeated lookups
        exposure_tolerance_cv = 1.0
        seed = None

        # Optimized element loading - load all elements but with better query
        elements = db.scalars(
            select(StudyElement)
            .where(StudyElement.study_id == study.id)
            .order_by(StudyElement.element_id.asc())
        ).all()
        if not elements:
            raise HTTPException(status_code=400, detail="Elements missing for grid study.")
        num_elements = len(elements)

        logger.debug(
            "Regenerate grid: num_elements=%s respondents=%s elements_loaded=%s",
            num_elements, number_of_respondents, len(elements)
        )

        result = generator['grid'](
            num_elements=num_elements,
            tasks_per_consumer=None,
            number_of_respondents=number_of_respondents,
            exposure_tolerance_cv=exposure_tolerance_cv,
            seed=seed,
            elements=elements,
            db=db,
            study_id=str(study.id)
        )
        tasks = result.get('tasks', {})
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study.id, tasks)
        
        meta = result.get('metadata', {})
        tpc = meta.get('tasks_per_consumer')
        computed = meta.get('capacity')
        if not computed and isinstance(tasks, dict):
            try:
                computed = sum(len(v or []) for k, v in tasks.items() if str(k).isdigit())
            except Exception:
                computed = 0
        resp_count = len([k for k in tasks if isinstance(tasks, dict) and str(k).isdigit()]) if isinstance(tasks, dict) else 'n/a'
        logger.debug("Regenerate grid computed total_tasks=%s (tpc=%s, nresp=%s, tasks_keys=%s)",
                     computed, tpc, number_of_respondents, resp_count)
        computed_total = int(computed or 0)

    elif study.study_type == 'layer':
        # Cache constants to avoid repeated lookups
        exposure_tolerance_pct = 2.0
        seed = None

        # Optimized layer loading - load layers with images efficiently
        layers = db.scalars(
            select(StudyLayer)
            .options(selectinload(StudyLayer.images))
            .where(StudyLayer.study_id == study.id)
            .order_by(StudyLayer.order.asc())
        ).all()
        if not layers:
            raise HTTPException(status_code=400, detail="Layer configuration missing.")

        logger.debug(
            "Regenerate layer: respondents=%s exposure_pct=%s layers_loaded=%s",
            number_of_respondents, exposure_tolerance_pct, len(layers)
        )
        result = generator['layer'](
            layers=layers,
            number_of_respondents=number_of_respondents,
            exposure_tolerance_pct=exposure_tolerance_pct,
            seed=seed
        )
        # Attach optional background image URL to metadata for client rendering
        try:
            if isinstance(result, dict):
                meta_tmp = result.get('metadata') or {}
                if isinstance(meta_tmp, dict):
                    meta_tmp['background_image_url'] = getattr(study, 'background_image_url', None)
                    result['metadata'] = meta_tmp
        except Exception:
            pass
        tasks = result.get('tasks', {})
        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study.id, tasks)
        
        meta = result.get('metadata', {})
        tpc = meta.get('tasks_per_consumer')
        computed = meta.get('capacity')
        if not computed and isinstance(tasks, dict):
            try:
                computed = sum(len(v or []) for k, v in tasks.items() if str(k).isdigit())
            except Exception:
                computed = 0
        resp_count = len([k for k in tasks if isinstance(tasks, dict) and str(k).isdigit()]) if isinstance(tasks, dict) else 'n/a'
        logger.debug("Regenerate layer computed total_tasks=%s (tpc=%s, nresp=%s, tasks_keys=%s)",
                     computed, tpc, number_of_respondents, resp_count)
        computed_total = int(computed or 0)

    elif study.study_type == 'hybrid':
        exposure_tolerance_cv = 1.0
        seed = None
        phase_order = study.phase_order or []
        if not phase_order:
            raise HTTPException(status_code=400, detail="Hybrid study requires phase_order.")
        
        combined_tasks = {}
        combined_metadata = {"phases": []}
        
        # Load all categories and elements once
        categories = db.scalars(
            select(StudyCategory).where(StudyCategory.study_id == study.id)
        ).all()
        elements = db.scalars(
            select(StudyElement).where(StudyElement.study_id == study.id)
        ).all()

        is_mix = "mix" in phase_order
        # If mix, we need to gather tasks from ALL present phase types, ignoring the specific order list (except to find types)
        # Actually, "mix" might be the ONLY item, or mixed with others? 
        # Requirement: "if user choses mix than task will be generated randomly means there will be no bound grid will appear first or tet any one one can ppear at any time"
        # So if "mix" is selected, we assume ALL configured phases should be generated and mixed.
        # We can detect relevant phases by looking at the categories' phase_type.
        
        target_phases = []
        if is_mix:
            # unique phase types from categories
            target_phases = list(set(c.phase_type for c in categories if c.phase_type))
            # If for some reason categories don't have phase_type appropriately set, fallback? 
            # Assuming StudyCategory.phase_type is reliable.
        else:
            target_phases = phase_order

        # Temporary storage for mix shuffling
        tasks_per_respondent_mix = {} # resp_id -> list of tasks

        for phase_type in target_phases:
            # Generate tasks for this phase
            phase_categories = [c for c in categories if c.phase_type == phase_type]
            if not phase_categories:
                continue
                
            cat_data = []
            for cat in phase_categories:
                cat_elements = [e for e in elements if e.category_id == cat.id]
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
            phase_result = generate_grid_tasks_golden(
                categories_data=cat_data,
                number_of_respondents=number_of_respondents,
                exposure_tolerance_cv=exposure_tolerance_cv,
                seed=seed,
                tasks_per_respondent=0,
            )
            
            phase_tasks = phase_result.get('tasks', {})
            
            if is_mix:
                # Accumulate for shuffling later
                for resp_id, t_list in phase_tasks.items():
                    if resp_id not in tasks_per_respondent_mix:
                        tasks_per_respondent_mix[resp_id] = []
                    
                    for t in t_list:
                        # Tag with phase type but don't set final index yet
                        t['phase_type'] = phase_type
                        tasks_per_respondent_mix[resp_id].append(t)
            else:
                # Sequential appending
                for resp_id, t_list in phase_tasks.items():
                    if resp_id not in combined_tasks:
                        combined_tasks[resp_id] = []
                    
                    offset = len(combined_tasks[resp_id])
                    for t in t_list:
                        t['task_index'] = int(t.get('task_index', 0)) + offset
                        t['phase_type'] = phase_type
                    combined_tasks[resp_id].extend(t_list)
            
            combined_metadata['phases'].append({
                "phase_type": phase_type,
                "metadata": phase_result.get('metadata', {})
            })
            
        if is_mix:
            import random
            # Shuffle and assign indices
            combined_tasks = {}
            for resp_id, t_list in tasks_per_respondent_mix.items():
                random.shuffle(t_list)
                # Re-index
                for idx, t in enumerate(t_list):
                    t['task_index'] = idx
                combined_tasks[resp_id] = t_list

        from app.services.task_service import TaskService
        task_service = TaskService(db)
        task_service.save_tasks(study.id, combined_tasks)
        # Simple count for computed_total
        computed_total = sum(len(v) for k, v in combined_tasks.items() if str(k).isdigit()) if combined_tasks else 0

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported study type: {study.study_type}. Must be 'grid', 'layer', 'text', or 'hybrid'.")

    # Optimized commit and response generation
    try:
        db.commit()
        invalidate_study_cache(study_id)
        # Cache logging to avoid repeated string operations
        log_msg = f"Regenerate completed: study_id={study_id} total_tasks={computed_total}"
        logger.info(log_msg)
        print(f"[regenerate] study_id={study_id} total_tasks={computed_total}")
    except Exception as e:
        logger.error(f"Failed to commit regenerate tasks: {e}")
        raise HTTPException(status_code=500, detail="Failed to save regenerated tasks")
    
    # Cache metadata to avoid repeated lookups
    response_meta = locals().get('meta', {}) if 'meta' in locals() else {}
    return RegenerateTasksResponse(
        success=True,
        message="Task matrix regenerated successfully",
        total_tasks=int(computed_total or 0),
        metadata=response_meta if isinstance(response_meta, dict) else {}
    )

def validate_tasks(
    db: Session,
    study_id: UUID,
    owner_id: UUID
) -> ValidateTasksResponse:
    study = _load_owned_study(db, study_id, owner_id, for_update=False)
    
    from app.services.task_service import TaskService
    task_service = TaskService(db)
    tasks = task_service.get_all_tasks_as_dict(study_id)
    
    if not tasks:
        return ValidateTasksResponse(validation_passed=False, issues=["No tasks generated"], totals={})

    issues: List[str] = []
    # Simplified validation without IPED parameters
    totals: Dict[str, Any] = {}

    if study.study_type in ('grid', 'text', 'hybrid'):
        # Basic structural checks (skip design_matrix key)
        for respondent, task_list in tasks.items():
            if not str(respondent).isdigit():
                continue
            if not isinstance(task_list, list):
                continue
            for task in task_list:
                elements_shown = task.get("elements_shown", {})
                active_count = sum(1 for k, v in elements_shown.items() if not k.endswith('_content') and int(v or 0) == 1)

        totals['respondents'] = len([k for k in tasks if str(k).isdigit()])

    elif study.study_type == 'layer':
        # Check structure presence only (skip design_matrix key)
        for respondent, task_list in tasks.items():
            if not str(respondent).isdigit():
                continue
            if not isinstance(task_list, list):
                continue
            for task in task_list:
                if "elements_shown" not in task and "elements_shown_content" not in task:
                    issues.append(f"Task {task.get('task_id')} missing elements_shown/_content.")

        totals['respondents'] = len([k for k in tasks if str(k).isdigit()])

    else:
        issues.append(f"Unsupported study type {study.study_type}")

    return ValidateTasksResponse(validation_passed=len(issues) == 0, issues=issues, totals=totals)


def get_study_basic_details(db: Session, study_id: UUID, owner_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Get basic study details for authenticated users.
    Returns core study information without heavy data like tasks, elements, or layers.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    
    # Optimized query to get basic study details with classification questions
    study = db.scalars(
        select(Study)
        .options(selectinload(Study.classification_questions))
        .where(Study.id == study_id, Study.creator_id == owner_id)
    ).first()
    
    if not study:
        return None
    
    # Build study config from audience segmentation
    study_config = {}
    if study.audience_segmentation:
        study_config.update(study.audience_segmentation)
    
    return {
        "id": study.id,
        "title": study.title,
        "status": study.status,
        "study_type": study.study_type,
        "created_at": study.created_at,
        "background": study.background,
        "main_question": study.main_question,
        "orientation_text": study.orientation_text,
        "rating_scale": study.rating_scale,
        "study_config": study_config,
        "classification_questions": study.classification_questions,
        "toggle_shuffle": study.toggle_shuffle
    }


def get_study_basic_details_public(db: Session, study_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Get basic study details for public access (no authentication required).
    Ultra-fast query with minimal data loading using raw SQL for maximum speed.
    Includes element_count: number of images (grid/layer) or statements (text).
    """
    from sqlalchemy import text
    
    # Ultra-fast raw SQL query - only essential fields (product_id/product_keys fetched via ORM below)
    query = text("""
        SELECT id, title, status, study_type, created_at, background, 
               main_question, orientation_text, rating_scale, iped_parameters, language, toggle_shuffle
        FROM studies 
        WHERE id = :study_id
    """)
    
    result = db.execute(query, {"study_id": study_id}).first()
    
    if not result:
        return None
    
    # Build study config from audience segmentation (stored as iped_parameters)
    study_config = {}
    if result.iped_parameters:
        study_config.update(result.iped_parameters)
    
    # Load classification questions separately with a fast query
    classification_query = text("""
        SELECT id, question_id, question_text, question_type, is_required, 
               "order", answer_options, config
        FROM study_classification_questions 
        WHERE study_id = :study_id
        ORDER BY "order"
    """)
    
    classification_results = db.execute(classification_query, {"study_id": study_id}).fetchall()
    classification_questions = []
    for row in classification_results:
        classification_questions.append({
            "id": row.id,
            "question_id": row.question_id,
            "question_text": row.question_text,
            "question_type": row.question_type,
            "is_required": row.is_required,
            "order": row.order,
            "answer_options": row.answer_options,
            "config": row.config
        })
    # For draft studies, do not expose the system fragrance question (Q0) in preview/editing
    if result.status == "draft":
        classification_questions = [q for q in classification_questions if q.get("question_id") != FRAGRANCE_QUESTION_ID]

    # Count elements based on study type
    element_count = 0
    study_type = result.study_type
    
    if study_type == 'grid' or study_type == 'text':
        # For grid and text studies, count elements (images for grid, statements for text)
        element_count_query = text("""
            SELECT COUNT(*) as count
            FROM study_elements
            WHERE study_id = :study_id
        """)
        count_result = db.execute(element_count_query, {"study_id": study_id}).first()
        element_count = count_result.count if count_result else 0
        
    elif study_type == 'layer':
        # For layer studies, count total images across all layers
        layer_image_count_query = text("""
            SELECT COUNT(li.id) as count
            FROM layer_images li
            INNER JOIN study_layers sl ON li.layer_id = sl.id
            WHERE sl.study_id = :study_id
        """)
        count_result = db.execute(layer_image_count_query, {"study_id": study_id}).first()
        element_count = count_result.count if count_result else 0
    
    out = {
        "id": result.id,
        "title": result.title,
        "status": result.status,
        "study_type": result.study_type,
        "created_at": result.created_at,
        "background": result.background,
        "main_question": result.main_question,
        "orientation_text": result.orientation_text,
        "rating_scale": result.rating_scale,
        "language": result.language,
        "study_config": study_config,
        "classification_questions": classification_questions,
        "element_count": element_count,
        "toggle_shuffle": result.toggle_shuffle
    }
    # Fetch product_id and product_keys via ORM (reliable across local/hosted; raw SQL Row can differ)
    product_row = db.execute(
        select(Study.product_id, Study.product_keys).where(Study.id == study_id)
    ).first()
    if product_row:
        pid, pkeys = product_row
        if pid is not None and (pid or "").strip() != "":
            out["product_id"] = pid
        if pkeys is not None and len(pkeys) > 0:
            out["product_keys"] = pkeys
    return out


def get_study_basic_details_public_v2(db: Session, study_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Same as get_study_basic_details_public but also includes total_responses
    (how many opted in/started the study: complete + abandoned + in progress).
    """
    from sqlalchemy import text
    out = get_study_basic_details_public(db=db, study_id=study_id)
    if not out:
        return None
    total_responses_query = text("""
        SELECT COUNT(*)::int AS total
        FROM study_responses
        WHERE study_id = :study_id
    """)
    rc = db.execute(total_responses_query, {"study_id": study_id}).first()
    out["total_responses"] = rc.total if rc else 0
    return out