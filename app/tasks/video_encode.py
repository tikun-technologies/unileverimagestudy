"""Celery task for HLS encode. Consumed only by the video worker (-Q video)."""
from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="celery_job.encode_video", queue="video", acks_late=True)
def encode_video(self, public_id: str, user_id: str | None = None) -> dict:
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.models.video_asset_model import VideoAsset
    from app.services.video_encode_service import apply_encode_result
    from app.services.video_transcode import encode_hls

    db = SessionLocal()
    try:
        asset = db.get(VideoAsset, public_id)
        if asset is None:
            logger.error("Unknown video asset %s", public_id)
            return {"success": False, "error": "Unknown video"}
        source_url = asset.source_url
        output_prefix = asset.output_prefix
        hls_url = asset.hls_url
        poster_url = asset.poster_url
    finally:
        db.close()

    if not source_url or not output_prefix:
        db = SessionLocal()
        try:
            apply_encode_result(db, public_id, "failed", error="Missing source or output prefix", user_id=user_id)
        finally:
            db.close()
        return {"success": False, "error": "Missing source or output prefix"}

    try:
        encode_hls(
            source_url,
            output_prefix,
            settings.AZURE_STORAGE_CONNECTION_STRING or "",
            settings.AZURE_STORAGE_CONTAINER or "",
        )
        db = SessionLocal()
        try:
            apply_encode_result(db, public_id, "ready", hls_url=hls_url, poster_url=poster_url, user_id=user_id)
        finally:
            db.close()
        return {"success": True, "public_id": public_id}
    except Exception as exc:
        logger.exception("Video encode failed for %s", public_id)
        db = SessionLocal()
        try:
            apply_encode_result(db, public_id, "failed", error=str(exc), user_id=user_id)
        except Exception:
            logger.exception("Could not mark video %s failed", public_id)
        finally:
            db.close()
        raise
