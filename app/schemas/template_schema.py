# app/schemas/template_schema.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


AspectRatio = Literal["9:16", "16:9", "1:1"]
TemplateStatus = Literal["draft", "published"]


class TemplateLayerJson(BaseModel):
    """Step-5 compatible payload stored in layer_json."""

    aspect_ratio: AspectRatio
    background_image_url: Optional[str] = None
    study_layers: List[Dict[str, Any]] = Field(default_factory=list)
    design_constraints: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class TemplateCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    aspect_ratio: AspectRatio
    status: TemplateStatus = "draft"
    layer_json: Dict[str, Any]
    preview_metadata: Optional[Dict[str, Any]] = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("Template title is required")
        return cleaned


class TemplateUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    aspect_ratio: Optional[AspectRatio] = None
    layer_json: Optional[Dict[str, Any]] = None
    preview_metadata: Optional[Dict[str, Any]] = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Template title is required")
        return cleaned


class TemplateStatusUpdate(BaseModel):
    status: TemplateStatus


class TemplateTitleValidateRequest(BaseModel):
    title: str
    exclude_id: Optional[UUID] = None


class TemplateTitleValidateResponse(BaseModel):
    available: bool
    message: Optional[str] = None


class TemplateCreatorOut(BaseModel):
    id: Optional[UUID] = None
    name: Optional[str] = None
    email: Optional[str] = None


class TemplateListItem(BaseModel):
    id: UUID
    title: str
    status: TemplateStatus
    aspect_ratio: AspectRatio
    layer_count: int
    element_count: int
    layer_json: Dict[str, Any]
    preview_metadata: Optional[Dict[str, Any]] = None
    created_by: Optional[TemplateCreatorOut] = None
    created_at: datetime
    updated_at: datetime
    published_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TemplateOut(TemplateListItem):
    pass


class TemplateListResponse(BaseModel):
    items: List[TemplateListItem]
    total: int
    page: int
    per_page: int


class TemplatePermissionResponse(BaseModel):
    can_manage: bool
