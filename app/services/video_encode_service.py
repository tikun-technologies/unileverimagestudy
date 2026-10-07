"""Record uploaded videos and enqueue HLS encode on the video Celery queue."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.study_model import StudyElement, StudyTaskAssignment
from app.models.video_asset_model import VideoAsset
from app.services.cloudinary_service import _public_url

logger = logging.getLogger(__name__)


def encode_enabled() -> bool:
    return bool(
        settings.VIDEO_ENCODE_ENABLED
        and settings.AZURE_STORAGE_CONNECTION_STRING
        and settings.AZURE_STORAGE_CONTAINER
    )


def video_encode_event(asset: VideoAsset) -> Dict[str, Any]:
    status = asset.status or "processing"
    playback = asset.hls_url if status == "ready" and asset.hls_url else asset.source_url
    return {
        "event": "video_encode",
        "public_id": asset.public_id,
        "url": asset.source_url,
        "status": status,
        "playback_url": playback,
        "poster_url": asset.poster_url,
    }


def publish_video_encode(user_id: str, asset: VideoAsset) -> None:
    """Push encode progress on the same user channel as the global jobs websocket."""
    if not user_id:
        return
    from app.core.redis import publish_user_job_update

    publish_user_job_update(str(user_id), video_encode_event(asset))


def list_owner_videos(db: Session, owner_id: str) -> List[Dict[str, Any]]:
    if not owner_id:
        return []
    assets = (
        db.query(VideoAsset)
        .filter(VideoAsset.owner_id == str(owner_id))
        .order_by(VideoAsset.updated_at.desc())
        .limit(400)
        .all()
    )
    return [video_encode_event(asset) for asset in assets]


def register_uploaded_video(
    db: Session,
    public_id: str,
    source_url: str,
    owner_id: Optional[str] = None,
) -> VideoAsset:
    container = settings.AZURE_STORAGE_CONTAINER or "studies"
    output_prefix = f"videos-hls/{uuid4().hex}"
    asset = db.get(VideoAsset, public_id)
    if asset is None:
        asset = VideoAsset(public_id=public_id)
        db.add(asset)
    asset.source_url = source_url
    if owner_id:
        asset.owner_id = str(owner_id)
    asset.output_prefix = output_prefix
    asset.hls_url = _public_url(container, f"{output_prefix}/master.m3u8")
    asset.poster_url = _public_url(container, f"{output_prefix}/poster.jpg")
    asset.status = "processing" if encode_enabled() else "ready"
    asset.error = None
    if not encode_enabled():
        asset.hls_url = source_url
        asset.poster_url = None
    db.commit()
    db.refresh(asset)
    return asset


def enqueue_encode(asset: VideoAsset, user_id: Optional[str] = None) -> None:
    if not encode_enabled():
        if user_id:
            publish_video_encode(str(user_id), asset)
        return
    from app.tasks.video_encode import encode_video

    encode_video.delay(asset.public_id, str(user_id) if user_id else None)
    logger.info("Queued video encode for %s", asset.public_id)
    if user_id:
        publish_video_encode(str(user_id), asset)


def apply_encode_result(
    db: Session,
    public_id: str,
    status: str,
    hls_url: Optional[str] = None,
    poster_url: Optional[str] = None,
    error: Optional[str] = None,
    user_id: Optional[str] = None,
) -> VideoAsset:
    asset = db.get(VideoAsset, public_id)
    if asset is None:
        raise ValueError("Unknown video")
    normalized = "ready" if status == "ready" else "failed"
    asset.status = normalized
    asset.error = error
    if hls_url:
        asset.hls_url = hls_url
    if poster_url:
        asset.poster_url = poster_url
    if normalized == "ready" and asset.hls_url and asset.source_url:
        _replace_playback_url(db, asset.source_url, asset.hls_url)
    db.commit()
    db.refresh(asset)
    owner = user_id or asset.owner_id
    if owner:
        publish_video_encode(str(owner), asset)
    return asset


def _looks_like_hls(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith(".m3u8")


def summarize_video_urls(db: Session, urls: List[str]) -> Dict[str, Any]:
    """Status and playback URL for each source or HLS link the creator already stored."""
    cleaned: List[str] = []
    seen = set()
    for url in urls or []:
        if not isinstance(url, str):
            continue
        item = url.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
        if len(cleaned) >= 400:
            break

    if not cleaned:
        return {
            "items": [],
            "ready": 0,
            "processing": 0,
            "failed": 0,
            "total": 0,
            "all_ready": True,
        }

    assets = (
        db.query(VideoAsset)
        .filter(
            (VideoAsset.source_url.in_(cleaned)) | (VideoAsset.hls_url.in_(cleaned))
        )
        .all()
    )
    by_source = {asset.source_url: asset for asset in assets if asset.source_url}
    by_hls = {asset.hls_url: asset for asset in assets if asset.hls_url}

    items = []
    ready = processing = failed = 0
    for url in cleaned:
        asset = by_source.get(url) or by_hls.get(url)
        if asset is None:
            status = "ready" if _looks_like_hls(url) else "unknown"
            playback = url
            poster = None
        else:
            status = asset.status or "processing"
            playback = asset.hls_url if status == "ready" and asset.hls_url else url
            poster = asset.poster_url
        if status in ("ready", "unknown"):
            ready += 1
        elif status == "failed":
            failed += 1
        else:
            processing += 1
            status = "processing"
        items.append({
            "url": url,
            "status": status,
            "playback_url": playback,
            "poster_url": poster,
        })

    return {
        "items": items,
        "ready": ready,
        "processing": processing,
        "failed": failed,
        "total": len(items),
        "all_ready": processing == 0 and failed == 0,
    }


def playback_url_for(db: Session, source_url: str) -> str:
    if not source_url:
        return source_url
    asset = (
        db.query(VideoAsset)
        .filter(VideoAsset.source_url == source_url, VideoAsset.status == "ready", VideoAsset.hls_url.isnot(None))
        .first()
    )
    if asset and asset.hls_url and asset.hls_url != source_url:
        return asset.hls_url
    return source_url


def rewrite_video_payload_urls(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    elements = payload.get("elements") or []
    if not elements:
        return payload
    rewritten = []
    for element in elements:
        if not isinstance(element, dict):
            rewritten.append(element)
            continue
        content = element.get("content")
        if element.get("element_type") == "video" and isinstance(content, str):
            element = {**element, "content": playback_url_for(db, content)}
        rewritten.append(element)
    return {**payload, "elements": rewritten}


def _replace_playback_url(db: Session, source_url: str, hls_url: str) -> None:
    elements = db.query(StudyElement).filter(StudyElement.content == source_url).all()
    study_ids = set()
    for element in elements:
        element.content = hls_url
        study_ids.add(element.study_id)
    if not study_ids:
        return
    assignments = (
        db.query(StudyTaskAssignment)
        .filter(StudyTaskAssignment.study_id.in_(study_ids))
        .all()
    )
    for assignment in assignments:
        assignment.elements_shown_content = _replace_in_json(assignment.elements_shown_content, source_url, hls_url)
        assignment.elements_shown = _replace_in_json(assignment.elements_shown, source_url, hls_url)


def _replace_in_json(value: Any, source_url: str, hls_url: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_in_json(item, source_url, hls_url) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_in_json(item, source_url, hls_url) for item in value]
    if isinstance(value, str) and value == source_url:
        return hls_url
    return value
