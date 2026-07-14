# app/services/template_service.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.cache import RedisCache, invalidate_template_cache
from app.models.template_model import LayerTemplate
from app.schemas.template_schema import TemplateCreate, TemplateUpdate

VALID_ASPECTS = {"9:16", "16:9", "1:1"}
MIN_LAYERS = 3
MIN_ELEMENTS_PER_LAYER = 3

PUBLISHED_LIST_CACHE_KEY = "templates:published:list"
PUBLISHED_ITEM_CACHE_PREFIX = "templates:published:item:"
PUBLISHED_CACHE_TTL = 300


def normalize_title(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def _count_layers_and_elements(layer_json: Dict[str, Any]) -> Tuple[int, int]:
    layers = layer_json.get("study_layers") or []
    if not isinstance(layers, list):
        return 0, 0
    layer_count = len(layers)
    element_count = 0
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        images = layer.get("images") or []
        if isinstance(images, list):
            element_count += len(images)
    return layer_count, element_count


def validate_layer_json(layer_json: Dict[str, Any], *, require_full: bool = True) -> None:
    if not isinstance(layer_json, dict):
        raise HTTPException(status_code=400, detail="layer_json must be a valid JSON object")

    aspect = layer_json.get("aspect_ratio")
    if require_full and aspect not in VALID_ASPECTS:
        raise HTTPException(status_code=400, detail="aspect_ratio must be one of 9:16, 16:9, 1:1")

    layers = layer_json.get("study_layers")
    if layers is None:
        layers = []
    if not isinstance(layers, list):
        raise HTTPException(status_code=400, detail="study_layers must be an array")

    if require_full and len(layers) < MIN_LAYERS:
        raise HTTPException(
            status_code=400,
            detail=f"Template must have at least {MIN_LAYERS} layers (found {len(layers)})",
        )

    layer_names: List[str] = []
    for idx, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise HTTPException(status_code=400, detail=f"Layer at index {idx} is invalid")

        name = (layer.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail=f"Layer {idx + 1} must have a name")
        normalized_name = name.lower()
        if normalized_name in layer_names:
            raise HTTPException(status_code=400, detail=f"Duplicate layer name: {name}")
        layer_names.append(normalized_name)

        images = layer.get("images") or []
        if not isinstance(images, list):
            raise HTTPException(status_code=400, detail=f"Layer '{name}' images must be an array")

        if require_full and len(images) < MIN_ELEMENTS_PER_LAYER:
            raise HTTPException(
                status_code=400,
                detail=f"Layer '{name}' must have at least {MIN_ELEMENTS_PER_LAYER} elements (found {len(images)})",
            )

        element_names: List[str] = []
        for img_idx, image in enumerate(images):
            if not isinstance(image, dict):
                raise HTTPException(status_code=400, detail=f"Element {img_idx + 1} in layer '{name}' is invalid")

            url = image.get("url") or image.get("secureUrl") or image.get("previewUrl")
            if require_full and not url:
                raise HTTPException(
                    status_code=400,
                    detail=f"Element {img_idx + 1} in layer '{name}' is missing an image URL",
                )

            el_name = (image.get("name") or image.get("alt_text") or "").strip()
            if el_name:
                el_key = el_name.lower()
                if el_key in element_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Duplicate element name '{el_name}' in layer '{name}'",
                    )
                element_names.append(el_key)

            # Require transform-like geometry (image-level or layer-level)
            has_img_transform = all(
                isinstance(image.get(k), (int, float)) for k in ("x", "y", "width", "height")
            )
            layer_transform = layer.get("transform") if isinstance(layer.get("transform"), dict) else None
            has_layer_transform = bool(
                layer_transform
                and all(isinstance(layer_transform.get(k), (int, float)) for k in ("x", "y", "width", "height"))
            )
            if require_full and not (has_img_transform or has_layer_transform):
                raise HTTPException(
                    status_code=400,
                    detail=f"Element {img_idx + 1} in layer '{name}' is missing transformation data",
                )

            # Preserve text metadata when present (do not strip)
            source_type = image.get("sourceType") or image.get("source_type")
            if source_type == "text" or layer.get("layer_type") == "text":
                # Prefer nested config or flat text fields — either is acceptable
                config = image.get("config") if isinstance(image.get("config"), dict) else {}
                has_text_meta = bool(
                    image.get("text_content")
                    or image.get("textContent")
                    or config.get("text_content")
                    or config.get("html_content")
                    or image.get("html_content")
                    or image.get("htmlContent")
                )
                if require_full and not has_text_meta:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Text element {img_idx + 1} in layer '{name}' is missing editable text metadata",
                    )


def assert_title_available(db: Session, title: str, exclude_id: Optional[UUID] = None) -> None:
    normalized = normalize_title(title)
    if not normalized:
        raise HTTPException(status_code=400, detail="Template title is required")

    query = select(LayerTemplate.id).where(LayerTemplate.normalized_title == normalized)
    if exclude_id:
        query = query.where(LayerTemplate.id != exclude_id)
    existing = db.execute(query).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A template with this title already exists",
        )


def _creator_payload(template: LayerTemplate) -> Optional[Dict[str, Any]]:
    creator = getattr(template, "creator", None)
    if not creator:
        return None
    return {
        "id": creator.id,
        "name": getattr(creator, "name", None),
        "email": getattr(creator, "email", None),
    }


def serialize_template(template: LayerTemplate) -> Dict[str, Any]:
    return {
        "id": template.id,
        "title": template.title,
        "status": template.status,
        "aspect_ratio": template.aspect_ratio,
        "layer_count": template.layer_count,
        "element_count": template.element_count,
        "layer_json": template.layer_json or {},
        "preview_metadata": template.preview_metadata,
        "created_by": _creator_payload(template),
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "published_at": template.published_at,
    }


def create_template(db: Session, creator_id: UUID, payload: TemplateCreate) -> LayerTemplate:
    assert_title_available(db, payload.title)
    require_full = payload.status == "published"
    validate_layer_json(payload.layer_json, require_full=require_full)

    aspect = payload.aspect_ratio
    if payload.layer_json.get("aspect_ratio") and payload.layer_json["aspect_ratio"] != aspect:
        payload.layer_json = {**payload.layer_json, "aspect_ratio": aspect}

    layer_count, element_count = _count_layers_and_elements(payload.layer_json)
    now = datetime.now(timezone.utc)

    template = LayerTemplate(
        title=payload.title.strip(),
        normalized_title=normalize_title(payload.title),
        status=payload.status,
        aspect_ratio=aspect,
        layer_json=payload.layer_json,
        preview_metadata=payload.preview_metadata,
        layer_count=layer_count,
        element_count=element_count,
        created_by_id=creator_id,
        published_at=now if payload.status == "published" else None,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    invalidate_template_cache(template.id)
    return template


def update_template(db: Session, template_id: UUID, payload: TemplateUpdate) -> LayerTemplate:
    template = get_template_or_404(db, template_id)

    if payload.title is not None:
        assert_title_available(db, payload.title, exclude_id=template_id)
        template.title = payload.title.strip()
        template.normalized_title = normalize_title(payload.title)

    if payload.aspect_ratio is not None:
        template.aspect_ratio = payload.aspect_ratio

    if payload.layer_json is not None:
        require_full = template.status == "published"
        validate_layer_json(payload.layer_json, require_full=require_full)
        layer_json = dict(payload.layer_json)
        layer_json["aspect_ratio"] = template.aspect_ratio
        template.layer_json = layer_json
        layer_count, element_count = _count_layers_and_elements(layer_json)
        template.layer_count = layer_count
        template.element_count = element_count

    if payload.preview_metadata is not None:
        template.preview_metadata = payload.preview_metadata

    db.commit()
    db.refresh(template)
    invalidate_template_cache(template.id)
    return template


def set_template_status(db: Session, template_id: UUID, new_status: str) -> LayerTemplate:
    template = get_template_or_404(db, template_id)
    if new_status not in ("draft", "published"):
        raise HTTPException(status_code=400, detail="Invalid status")

    if new_status == "published":
        validate_layer_json(template.layer_json or {}, require_full=True)
        template.status = "published"
        template.published_at = datetime.now(timezone.utc)
    else:
        template.status = "draft"

    db.commit()
    db.refresh(template)
    invalidate_template_cache(template.id)
    return template


def delete_template(db: Session, template_id: UUID) -> None:
    template = get_template_or_404(db, template_id)
    db.delete(template)
    db.commit()
    invalidate_template_cache(template_id)


def get_template_or_404(db: Session, template_id: UUID) -> LayerTemplate:
    template = db.execute(
        select(LayerTemplate)
        .options(selectinload(LayerTemplate.creator))
        .where(LayerTemplate.id == template_id)
    ).scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


def get_template(db: Session, template_id: UUID, *, published_only: bool = False) -> Dict[str, Any]:
    if published_only:
        cached = RedisCache.get(f"{PUBLISHED_ITEM_CACHE_PREFIX}{template_id}")
        if cached:
            return cached

    template = get_template_or_404(db, template_id)
    if published_only and template.status != "published":
        raise HTTPException(status_code=404, detail="Template not found")

    data = serialize_template(template)
    if published_only:
        # JSON-serialize-friendly copy for Redis
        RedisCache.set(
            f"{PUBLISHED_ITEM_CACHE_PREFIX}{template_id}",
            _json_safe(data),
            ttl_seconds=PUBLISHED_CACHE_TTL,
        )
    return data


def _json_safe(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert UUIDs/datetimes for Redis JSON storage."""
    import json
    from uuid import UUID as UUIDType

    def default(obj: Any) -> Any:
        if isinstance(obj, UUIDType):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(type(obj))

    return json.loads(json.dumps(data, default=default))


def list_templates(
    db: Session,
    *,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "newest",
    page: int = 1,
    per_page: int = 50,
    published_only: bool = False,
) -> Dict[str, Any]:
    page = max(1, page)
    per_page = min(max(1, per_page), 100)

    # Cache published-only unfiltered first page lists
    cacheable = published_only and not search and status_filter in (None, "published") and page == 1 and sort == "newest"
    if cacheable:
        cached = RedisCache.get(PUBLISHED_LIST_CACHE_KEY)
        if cached:
            return cached

    query = select(LayerTemplate).options(selectinload(LayerTemplate.creator))

    if published_only:
        query = query.where(LayerTemplate.status == "published")
    elif status_filter in ("draft", "published"):
        query = query.where(LayerTemplate.status == status_filter)

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(LayerTemplate.title.ilike(term), LayerTemplate.normalized_title.ilike(term)))

    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.execute(count_query).scalar() or 0

    if sort == "oldest":
        query = query.order_by(LayerTemplate.created_at.asc())
    elif sort == "a-z":
        query = query.order_by(func.lower(LayerTemplate.title).asc())
    else:
        query = query.order_by(LayerTemplate.created_at.desc())

    offset = (page - 1) * per_page
    rows = db.execute(query.offset(offset).limit(per_page)).scalars().all()
    items = [serialize_template(t) for t in rows]

    result = {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }

    if cacheable:
        RedisCache.set(PUBLISHED_LIST_CACHE_KEY, _json_safe(result), ttl_seconds=PUBLISHED_CACHE_TTL)

    return result


def title_is_available(db: Session, title: str, exclude_id: Optional[UUID] = None) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return False
    query = select(LayerTemplate.id).where(LayerTemplate.normalized_title == normalized)
    if exclude_id:
        query = query.where(LayerTemplate.id != exclude_id)
    return db.execute(query).scalar_one_or_none() is None
