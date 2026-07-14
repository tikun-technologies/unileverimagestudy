# app/api/v1/template.py
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user
from app.core.template_permissions import can_manage_templates, require_template_manager
from app.db.session import get_db
from app.models.user_model import User
from app.schemas.template_schema import (
    TemplateCreate,
    TemplateListResponse,
    TemplateOut,
    TemplatePermissionResponse,
    TemplateStatusUpdate,
    TemplateTitleValidateRequest,
    TemplateTitleValidateResponse,
    TemplateUpdate,
)
from app.services import template_service

router = APIRouter()


@router.get("/permissions", response_model=TemplatePermissionResponse)
def get_template_permissions(
    current_user: User = Depends(get_current_active_user),
):
    """Whether the current user can manage templates (for UI gating)."""
    return TemplatePermissionResponse(can_manage=can_manage_templates(current_user.email))


@router.get("/published", response_model=TemplateListResponse)
def list_published_templates(
    search: Optional[str] = Query(None),
    sort: str = Query("newest", pattern="^(newest|oldest|a-z)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Published templates for Create Study → Select Template.
    Available to any authenticated user.
    """
    _ = current_user
    result = template_service.list_templates(
        db,
        status_filter="published",
        search=search,
        sort=sort,
        page=page,
        per_page=per_page,
        published_only=True,
    )
    return TemplateListResponse(**result)


@router.get("/published/{template_id}", response_model=TemplateOut)
def get_published_template(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _ = current_user
    data = template_service.get_template(db, template_id, published_only=True)
    return TemplateOut(**data)


@router.post("/validate-title", response_model=TemplateTitleValidateResponse)
def validate_template_title(
    payload: TemplateTitleValidateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    available = template_service.title_is_available(db, payload.title, payload.exclude_id)
    return TemplateTitleValidateResponse(
        available=available,
        message=None if available else "A template with this title already exists",
    )


@router.get("", response_model=TemplateListResponse)
def list_all_templates(
    status_filter: Optional[str] = Query(None, alias="status", pattern="^(draft|published)$"),
    search: Optional[str] = Query(None),
    sort: str = Query("newest", pattern="^(newest|oldest|a-z)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    result = template_service.list_templates(
        db,
        status_filter=status_filter,
        search=search,
        sort=sort,
        page=page,
        per_page=per_page,
        published_only=False,
    )
    return TemplateListResponse(**result)


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    template = template_service.create_template(db, current_user.id, payload)
    template = template_service.get_template_or_404(db, template.id)
    return TemplateOut(**template_service.serialize_template(template))


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    data = template_service.get_template(db, template_id, published_only=False)
    return TemplateOut(**data)


@router.put("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: UUID,
    payload: TemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    template_service.update_template(db, template_id, payload)
    data = template_service.get_template(db, template_id, published_only=False)
    return TemplateOut(**data)


@router.post("/{template_id}/publish", response_model=TemplateOut)
def publish_template(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    template_service.set_template_status(db, template_id, "published")
    data = template_service.get_template(db, template_id, published_only=False)
    return TemplateOut(**data)


@router.post("/{template_id}/draft", response_model=TemplateOut)
def move_template_to_draft(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    template_service.set_template_status(db, template_id, "draft")
    data = template_service.get_template(db, template_id, published_only=False)
    return TemplateOut(**data)


@router.patch("/{template_id}/status", response_model=TemplateOut)
def update_template_status(
    template_id: UUID,
    payload: TemplateStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    template_service.set_template_status(db, template_id, payload.status)
    data = template_service.get_template(db, template_id, published_only=False)
    return TemplateOut(**data)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_template_manager),
):
    _ = current_user
    template_service.delete_template(db, template_id)
    return None
